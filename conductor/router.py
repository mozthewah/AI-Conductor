"""The router: decides which model handles each piece of work, and learns.

Every piece of work is labeled with a *skill* (coding, ui_design, research...)
and a *domain* (travel, finance, general...). For each candidate model the
router estimates quality on that (skill, domain):

  start   - public benchmarks for the skill (Design Arena for ui_design, coding
            indexes for coding, search evals for research...), or the model's
            price tier if there's no benchmark data
  learn   - your results update it, Beta-Bernoulli style:
              exact (skill, domain) results count fully,
              the same skill in other domains counts half,
              the same domain with other skills counts a little
            so "Kimi is great at finance" carries over to new finance work.
  pick    - expected quality minus a cost penalty (by budget), with Thompson
            sampling now and then so less-proven models still get tried.

Outcome signals, strongest first: your thumbs up/down, the conductor's review
of a specialist's work, then simply finishing without errors.

Whole requests are classified first: a single kind of work goes straight to the
best model for it ("direct"); multi-part or hard requests get a conductor that
splits the work and hires a specialist per part ("conduct").
"""

from __future__ import annotations

import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .models import TIERS, Catalog, ModelInfo

SKILLS = ["coding", "ui_design", "architecture", "research", "writing", "analysis", "planning",
          "browsing", "chat", "other"]
DIFFICULTIES = ["easy", "medium", "hard"]
SKILL_HELP = (
    "coding (writing/debugging code), ui_design (visual/UI/UX design, layouts, styling, mockups), "
    "architecture (software design: structure, data models, APIs, tech choices), research (finding and "
    "synthesizing information), writing (prose, emails, docs, copy), analysis (data, math, comparing "
    "options, finance/numbers reasoning), planning (itineraries, schedules, project plans), browsing "
    "(operating websites, forms), chat (quick questions), other")

TOKEN_ESTIMATE = {"easy": (15_000, 1_500), "medium": (90_000, 6_000), "hard": (350_000, 25_000)}
COST_WEIGHT = {"frugal": 0.45, "balanced": 0.18, "best": 0.03}
PRIOR_STRENGTH = 3.0
SPILLOVER = {"same_skill": 0.5, "same_domain": 0.25}

CLASSIFY_SYSTEM = (
    "You triage incoming requests for an AI assistant so the right models can be assigned. "
    "Judge difficulty by how much reasoning, steps, and care the work needs, not by length.")

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "enum": SKILLS},
        "domain": {"type": "string"},
        "difficulty": {"type": "string", "enum": DIFFICULTIES},
        "needs_vision": {"type": "boolean"},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"skill": {"type": "string", "enum": SKILLS}, "domain": {"type": "string"}},
                "required": ["skill", "domain"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["skill", "domain", "difficulty", "needs_vision", "parts", "summary"],
    "additionalProperties": False,
}

CLASSIFY_PROMPT = """<request>
{prompt}
</request>

Triage this request:
- skill: the main kind of work. One of: {skills}.
- domain: the subject area in one lowercase word or short slug (e.g. travel, finance, health, \
legal, cooking, gaming, marketing, education), or "general".
- difficulty: easy (one or two steps), medium (several steps or real judgment), hard (long \
multi-step work, tricky reasoning, or high stakes).
- needs_vision: true only if it requires looking at images or screenshots.
- parts: the distinct kinds of work the request needs, as skill + domain pairs. A request to \
"design and build an app" has two (ui_design and coding); "find flight deals and budget the trip" \
has research/travel and analysis/finance. A single kind of work is one entry.
- summary: five words or fewer."""

_RULES = [
    ("coding", r"\b(code|script|python|javascript|bug|debug|function|api|regex|sql|refactor|compile|repo|app)\b"),
    ("ui_design", r"\b(design|ui|ux|layout|mockup|wireframe|style|css|logo|landing page)\b"),
    ("browsing", r"\b(log ?in|website|fill (out|in)|form|click|book|order|checkout|browser)\b"),
    ("research", r"\b(research|find out|latest|compare|news|sources|look up|best .* (for|under))\b"),
    ("writing", r"\b(write|draft|email|essay|blog|post|letter|rewrite|edit)\b"),
    ("planning", r"\b(plan|itinerary|schedule|trip|roadmap|agenda)\b"),
    ("analysis", r"\b(analy[sz]e|data|csv|spreadsheet|calculate|chart|statistics|budget)\b"),
]
_DOMAINS = [
    ("travel", r"\b(trip|travel|flight|hotel|itinerary|vacation|airline|points|miles)\b"),
    ("finance", r"\b(finance|budget|invest|stock|tax|money|loan|mortgage|crypto|savings)\b"),
    ("health", r"\b(health|diet|workout|fitness|medical|sleep)\b"),
    ("legal", r"\b(legal|contract|lease|law|lawyer)\b"),
]


def slug(domain: str) -> str:
    d = re.sub(r"[^a-z0-9]+", "-", (domain or "").lower()).strip("-")[:30]
    return d or "general"


def heuristic_label(prompt: str) -> dict:
    p = prompt.lower()
    hits = [s for s, rx in _RULES if re.search(rx, p)]
    skill = hits[0] if hits else ("chat" if len(p) < 120 else "other")
    domain = next((d for d, rx in _DOMAINS if re.search(rx, p)), "general")
    words = len(p.split())
    difficulty = "easy" if words < 15 and skill == "chat" else "hard" if words > 120 else "medium"
    parts = [{"skill": s, "domain": domain} for s in dict.fromkeys(hits)] or [{"skill": skill, "domain": domain}]
    return {"skill": skill, "domain": domain, "difficulty": difficulty, "parts": parts,
            "needs_vision": bool(re.search(r"\b(image|photo|screenshot|picture)\b", p)), "summary": "", "by": "rules"}


def needs_conductor(label: dict, compose: str = "auto") -> bool:
    """Does this request get a composer (who hands the work to specialists) or one direct model?"""
    kinds = {(p.get("skill"), slug(p.get("domain", ""))) for p in label.get("parts") or []}
    multi = len(kinds) > 1 or label.get("difficulty") == "hard"
    if compose == "off":
        return multi
    if compose == "always":
        return True
    quick = label.get("skill") == "chat" or label.get("difficulty") == "easy"
    return multi or not quick


@dataclass
class Decision:
    model: str
    skill: str
    domain: str
    difficulty: str
    reason: str
    by: str = "router"  # router | explore | pinned | compare | fallback
    mode: str = "direct"  # direct | conduct | part
    est_cost: float = 0.0
    candidates: list[dict] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)

    @property
    def category(self) -> str:  # older name for skill
        return self.skill

    def to_dict(self) -> dict:
        return {**self.__dict__, "category": self.skill}


class Router:
    def __init__(self, conductor):
        self.c = conductor

    @property
    def catalog(self) -> Catalog:
        return self.c.catalog

    async def classify(self, prompt: str) -> dict:
        model = self.catalog.get(self.c.settings.default_router_model)
        try:
            out = await self.c.llm.structured(
                model=model, system=CLASSIFY_SYSTEM,
                prompt=CLASSIFY_PROMPT.format(prompt=prompt[:6000], skills=SKILL_HELP),
                schema=CLASSIFY_SCHEMA, effort="low")
            if out.get("skill") in SKILLS and out.get("difficulty") in DIFFICULTIES:
                out["domain"] = slug(out.get("domain", ""))
                out["parts"] = [{"skill": p["skill"], "domain": slug(p.get("domain", ""))}
                                for p in out.get("parts") or [] if p.get("skill") in SKILLS] \
                    or [{"skill": out["skill"], "domain": out["domain"]}]
                return {**out, "by": model.id}
        except Exception:
            pass
        return heuristic_label(prompt)

    # ------------------------------------------------------------------ scoring
    def _stats(self) -> dict:
        """(model, skill, domain) -> [runs, reward] from the run log."""
        table: dict = defaultdict(lambda: [0, 0.0])
        for r in self.c.store.run_stats():
            table[(r["model"], r["category"], r["domain"])] = [r["runs"], r["reward"] or 0.0]
        return table

    def estimate(self, model: ModelInfo, skill: str, domain: str, difficulty: str, pool: list[str],
                 table: dict) -> tuple[float, float, float, int, float | None]:
        """Returns (posterior mean, a, b, exact runs, benchmark percentile)."""
        d = DIFFICULTIES.index(difficulty)
        t = TIERS.index(model.tier) if model.tier in TIERS else 1
        bench = self.c.benchmarks.prior(model.id, skill, pool) if self.c.benchmarks else None
        if bench is not None:
            prior = 0.5 + 0.4 * bench
            if t < d:
                prior -= 0.1 * (d - t)
        else:
            prior = 0.8 if t >= d else 0.8 - 0.25 * (d - t)
            if t > d + 1:
                prior -= 0.03
        a = prior * PRIOR_STRENGTH
        b = (1 - prior) * PRIOR_STRENGTH
        exact_n = 0
        for (m, s, dom), (n, reward) in table.items():
            if m != model.id:
                continue
            if s == skill and dom == domain:
                w = 1.0
                exact_n += n
            elif s == skill:
                w = SPILLOVER["same_skill"]
            elif dom == domain and domain != "general":
                w = SPILLOVER["same_domain"]
            else:
                continue
            a += w * reward
            b += w * (n - reward)
        return a / (a + b), a, b, exact_n, bench

    def pick(self, *, skill: str, domain: str, difficulty: str, budget: str, needs_vision: bool = False,
             exclude: set[str] | None = None, seed: int | None = None) -> Decision:
        settings = self.c.settings
        roster = [m for m in self.catalog.roster() if m.tools and (m.vision or not needs_vision)
                  and m.id not in (exclude or set())]
        if not roster:
            return Decision(settings.fallback_model, skill, domain, difficulty,
                            "No roster model fits, so using the default.", "fallback")
        rng = random.Random(seed)
        explore = rng.random() < settings.explore
        table = self._stats()
        pool = [m.id for m in roster]
        scored = []
        for m in roster:
            quality, a, b, n, bench = self.estimate(m, skill, domain, difficulty, pool, table)
            sampled = rng.betavariate(a, b) if explore else quality
            tin, tout = TOKEN_ESTIMATE[difficulty]
            cost = m.cost(tin, tout, cache_read=int(tin * 0.6))
            penalty = COST_WEIGHT.get(budget, 0.18) * math.log10(1 + cost / 0.005) / 3
            scored.append({"model": m.id, "quality": round(quality, 3), "score": round(sampled - penalty, 3),
                           "est_cost": round(cost, 4), "runs": n,
                           "benchmark": None if bench is None else round(bench, 2)})
        scored.sort(key=lambda r: r["score"], reverse=True)
        best = scored[0]
        name = self.catalog.get(best["model"]).name or best["model"]
        basis = (f"{best['runs']} past {skill.replace('_', ' ')}"
                 f"{'' if domain == 'general' else '/' + domain} task(s)" if best["runs"]
                 else "public benchmarks" if best["benchmark"] is not None else "its price tier")
        reason = (f"{name} looks best for {skill.replace('_', ' ')}"
                  f"{'' if domain == 'general' else ' (' + domain + ')'} at a {budget} budget, "
                  f"based on {basis} (est. quality {best['quality']:.0%}).")
        if explore:
            reason += " Trying something a bit different this time to keep learning."
        return Decision(best["model"], skill, domain, difficulty, reason, "explore" if explore else "router",
                        est_cost=best["est_cost"], candidates=scored[:4])

    # ------------------------------------------------------------------ entry points
    async def route(self, prompt: str, *, budget: str | None = None, pinned: str | None = None,
                    seed: int | None = None) -> Decision:
        """Route a whole request: straight to a specialist, or to a conductor."""
        settings = self.c.settings
        budget = budget or settings.router_budget
        auto = not pinned or pinned == "auto"
        label = await self.classify(prompt) if auto else heuristic_label(prompt)
        conduct = needs_conductor(label, settings.compose)
        if not auto:
            m = self.catalog.get(pinned)
            tin, tout = TOKEN_ESTIMATE[label["difficulty"]]
            return Decision(m.id, label["skill"], label["domain"], label["difficulty"], "You picked this model.",
                            "pinned", "conduct" if conduct else "direct", m.cost(tin, tout, int(tin * 0.6)),
                            parts=label.get("parts", []))
        if conduct:
            d = self.pick(skill="orchestration", domain="general", difficulty="hard",
                          budget=settings.conductor_budget, needs_vision=label.get("needs_vision", False), seed=seed)
            kinds = ", ".join(f"{p['skill'].replace('_', ' ')}"
                              f"{'' if p['domain'] == 'general' else ' (' + p['domain'] + ')'}"
                              for p in label.get("parts", []))
            d.reason = (f"This needs several kinds of work ({kinds or label['skill']}), so {self.catalog.get(d.model).name or d.model} "
                        f"will conduct: split it up, hire the best model for each part, and check their work.")
            d.skill, d.domain, d.difficulty, d.mode = label["skill"], label["domain"], label["difficulty"], "conduct"
            d.parts = label.get("parts", [])
            return d
        d = self.pick(skill=label["skill"], domain=label["domain"], difficulty=label["difficulty"],
                      budget=budget, needs_vision=label.get("needs_vision", False), seed=seed)
        d.parts = label.get("parts", [])
        return d

    def route_part(self, *, skill: str, domain: str, difficulty: str = "medium", priority: str = "core",
                   needs_vision: bool = False, exclude: set[str] | None = None, pinned: str | None = None) -> Decision:
        """Route one part of a conducted request (the conductor already labeled it)."""
        skill = skill if skill in SKILLS else "other"
        domain = slug(domain)
        difficulty = difficulty if difficulty in DIFFICULTIES else "medium"
        if pinned and pinned != "auto":
            return Decision(pinned, skill, domain, difficulty, "Pinned by settings.", "pinned", "part")
        budget = self.c.settings.part_budget if priority == "core" else self.c.settings.subagent_budget
        d = self.pick(skill=skill, domain=domain, difficulty=difficulty, budget=budget,
                      needs_vision=needs_vision, exclude=exclude)
        d.mode = "part"
        return d
