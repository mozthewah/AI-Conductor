"""The router: picks which model runs each task, and learns from how it goes.

1. Classify - a small, cheap model labels the request (category, difficulty,
   whether it needs images or the browser). Falls back to keyword rules.
2. Score    - each roster model gets an estimated quality for that category:
   a prior from its price tier vs. the task's difficulty, updated with your
   actual outcomes (finished OK, thumbs up/down) - a Beta-Bernoulli posterior.
3. Pick     - Thompson sampling over that posterior (so it keeps exploring a
   little), minus a cost penalty set by the budget (frugal/balanced/best).

Routing happens once per task; the model then sticks for the whole task.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field

from .models import TIERS, Catalog, ModelInfo

CATEGORIES = ["coding", "research", "writing", "analysis", "browsing", "planning", "chat", "other"]
DIFFICULTIES = ["easy", "medium", "hard"]

# Rough tokens a task of each difficulty uses (input incl. re-sent context, output).
TOKEN_ESTIMATE = {"easy": (15_000, 1_500), "medium": (90_000, 6_000), "hard": (350_000, 25_000)}
COST_WEIGHT = {"frugal": 0.45, "balanced": 0.18, "best": 0.03}
PRIOR_STRENGTH = 3.0

CLASSIFY_SYSTEM = (
    "You label incoming tasks for an AI assistant so the right model can be chosen. "
    "Judge difficulty by how much reasoning, steps, and care the task needs, not by length.")

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "difficulty": {"type": "string", "enum": DIFFICULTIES},
        "needs_vision": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["category", "difficulty", "needs_vision", "summary"],
    "additionalProperties": False,
}

CLASSIFY_PROMPT = """<task>
{prompt}
</task>

Label this task:
- category: coding (writing/debugging code, scripts), research (finding and synthesizing \
information from the web), writing (drafting prose, emails, docs), analysis (data, math, \
comparing options, reasoning over given material), browsing (operating websites, forms, \
logins), planning (itineraries, schedules, project plans), chat (quick questions, small \
talk, simple lookups), other.
- difficulty: easy (one or two steps, little judgment), medium (several steps or real \
judgment), hard (long multi-step work, tricky reasoning, or high stakes).
- needs_vision: true only if it requires looking at images or screenshots.
- summary: five words or fewer."""

_RULES = [
    ("coding", r"\b(code|script|python|javascript|bug|debug|function|api|regex|sql|refactor|compile|repo)\b"),
    ("browsing", r"\b(log ?in|website|fill (out|in)|form|click|book|order|checkout|browser)\b"),
    ("research", r"\b(research|find out|latest|compare|news|sources|look up|best .* (for|under))\b"),
    ("writing", r"\b(write|draft|email|essay|blog|post|letter|rewrite|edit)\b"),
    ("planning", r"\b(plan|itinerary|schedule|trip|roadmap|agenda)\b"),
    ("analysis", r"\b(analy[sz]e|data|csv|spreadsheet|calculate|chart|statistics)\b"),
]


def heuristic_label(prompt: str) -> dict:
    p = prompt.lower()
    category = next((c for c, rx in _RULES if re.search(rx, p)), "chat" if len(p) < 120 else "other")
    words = len(p.split())
    difficulty = "easy" if words < 15 and category == "chat" else "hard" if words > 120 else "medium"
    return {"category": category, "difficulty": difficulty,
            "needs_vision": bool(re.search(r"\b(image|photo|screenshot|picture)\b", p)),
            "summary": "", "by": "rules"}


@dataclass
class Decision:
    model: str
    category: str
    difficulty: str
    reason: str
    by: str = "router"  # router | pinned | compare | fallback
    est_cost: float = 0.0
    candidates: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


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
                model=model, system=CLASSIFY_SYSTEM, prompt=CLASSIFY_PROMPT.format(prompt=prompt[:6000]),
                schema=CLASSIFY_SCHEMA, effort="low")
            if out.get("category") in CATEGORIES and out.get("difficulty") in DIFFICULTIES:
                return {**out, "by": model.id}
        except Exception:
            pass
        return heuristic_label(prompt)

    def stats(self, category: str) -> dict[str, dict]:
        rows = self.c.store.model_stats(category)
        return {r["model"]: r for r in rows}

    def score(self, model: ModelInfo, label: dict, stats: dict[str, dict], budget: str,
              rng: random.Random, explore: bool) -> dict:
        d = DIFFICULTIES.index(label["difficulty"])
        t = TIERS.index(model.tier) if model.tier in TIERS else 1
        prior = 0.8 if t >= d else 0.8 - 0.25 * (d - t)
        if t > d + 1:
            prior -= 0.03  # overkill: fine, but no better
        s = stats.get(model.id, {})
        n, reward = s.get("runs", 0), s.get("reward", 0.0)
        a = prior * PRIOR_STRENGTH + reward
        b = (1 - prior) * PRIOR_STRENGTH + (n - reward)
        quality = a / (a + b)
        sampled = rng.betavariate(a, b) if explore else quality
        tin, tout = TOKEN_ESTIMATE[label["difficulty"]]
        cost = model.cost(tin, tout, cache_read=int(tin * 0.6))
        penalty = COST_WEIGHT.get(budget, 0.18) * math.log10(1 + cost / 0.005) / 3
        return {"model": model.id, "quality": round(quality, 3), "score": round(sampled - penalty, 3),
                "est_cost": round(cost, 4), "runs": n}

    async def route(self, prompt: str, *, budget: str | None = None, pinned: str | None = None,
                    seed: int | None = None) -> Decision:
        settings = self.c.settings
        budget = budget or settings.router_budget
        roster = self.catalog.roster()
        label = await self.classify(prompt) if (not pinned or pinned == "auto") else heuristic_label(prompt)
        if pinned and pinned != "auto":
            m = self.catalog.get(pinned)
            tin, tout = TOKEN_ESTIMATE[label["difficulty"]]
            return Decision(m.id, label["category"], label["difficulty"], "You picked this model.", "pinned",
                            m.cost(tin, tout, int(tin * 0.6)))
        candidates = [m for m in roster if m.tools and (m.vision or not label.get("needs_vision"))]
        if not candidates:
            fb = settings.fallback_model
            return Decision(fb, label["category"], label["difficulty"],
                            "No roster model fits, so using the default.", "fallback")
        rng = random.Random(seed)
        explore = rng.random() < settings.explore
        stats = self.stats(label["category"])
        scored = sorted((self.score(m, label, stats, budget, rng, explore) for m in candidates),
                        key=lambda r: r["score"], reverse=True)
        best = scored[0]
        name = self.catalog.get(best["model"]).name or best["model"]
        history = (f"{best['runs']} past {label['category']} task(s), est. quality {best['quality']:.0%}"
                   if best["runs"] else "no history yet, so judged by its tier")
        reason = (f"{label['category'].title()}, {label['difficulty']} - {name} fits a {budget} budget "
                  f"({history}).")
        if explore:
            reason += " Trying something a bit different this time to keep learning."
        return Decision(best["model"], label["category"], label["difficulty"], reason,
                        "explore" if explore else "router", best["est_cost"], scored[:4])

