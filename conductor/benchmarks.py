"""Public benchmark scores, used as the router's starting point ("prior").

Before you've rated anything, the router still needs a sensible idea of which
model is good at what. OpenRouter's /benchmarks endpoint aggregates:
  - Artificial Analysis: coding, intelligence, and agentic indexes
  - Design Arena: head-to-head ELO for UI/web/visual design
  - OpenRouter's own evals: web-search research benchmarks, tau-bench
We turn those into a 0-1 percentile per model per skill. Your own results
then take over as they accumulate (see router.py).

Needs an OpenRouter key; cached for a day in data/benchmarks.json.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("conductor.benchmarks")

URL = "https://openrouter.ai/api/v1/benchmarks"
DATE_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")
TTL = 24 * 3600

# Which signals speak to which skill (weights).
SKILL_SIGNALS: dict[str, dict[str, float]] = {
    "coding": {"coding": 1.0, "agentic": 0.3},
    "ui_design": {"design": 1.0, "coding": 0.3},
    "architecture": {"coding": 0.6, "intelligence": 0.6},
    "research": {"search": 1.0, "intelligence": 0.5},
    "writing": {"intelligence": 1.0},
    "analysis": {"intelligence": 1.0, "coding": 0.2},
    "planning": {"intelligence": 1.0, "agentic": 0.4},
    "browsing": {"agentic": 1.0, "search": 0.3},
    "orchestration": {"agentic": 1.0, "intelligence": 1.0},
    "chat": {"intelligence": 1.0},
    "other": {"intelligence": 1.0},
}


def _fetch(key: str, params: dict) -> list[dict]:
    url = f"{URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "ai-conductor"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("data", [])


def parse(items: list[dict]) -> dict[str, dict[str, float]]:
    """Raw benchmark items -> {signal: {model_id: raw score}}."""
    raw: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for it in items:
        mid = it.get("model_permaslug")
        if not mid:
            continue
        src = it.get("source")
        if src == "artificial-analysis":
            for field, signal in (("coding_index", "coding"), ("intelligence_index", "intelligence"),
                                  ("agentic_index", "agentic")):
                if isinstance(it.get(field), (int, float)):
                    raw[signal][mid].append(float(it[field]))
        elif src == "design-arena":
            if isinstance(it.get("elo"), (int, float)):
                raw["design"][mid].append(float(it["elo"]))
        elif src == "openrouter":
            bt = str(it.get("benchmark_type", ""))
            score = it.get("primary_score", it.get("accuracy"))
            if isinstance(score, (int, float)):
                raw["search" if bt.startswith("search_") else "agentic_eval"][mid].append(float(score))
    out = {sig: {m: sum(v) / len(v) for m, v in models.items()} for sig, models in raw.items()}
    if "agentic_eval" in out:  # tau-bench etc. supplement the agentic signal
        for m, v in out.pop("agentic_eval").items():
            out.setdefault("agentic", {}).setdefault(m, v * 100)
    return out


class Benchmarks:
    def __init__(self, data_dir: Path, key: str = "", fetch: bool = True):
        self.path = Path(data_dir) / "benchmarks.json"
        self.key = key
        self.fetch = fetch
        self.signals: dict[str, dict[str, float]] = {}
        self.fetched_at = 0.0
        self.error = "" if key else "No OpenRouter key, so no benchmark data."

    def load(self) -> "Benchmarks":
        cached = None
        if self.path.exists():
            try:
                cached = json.loads(self.path.read_text())
            except ValueError:
                cached = None
        if cached and time.time() - cached.get("fetched_at", 0) < TTL:
            self.signals, self.fetched_at = cached["signals"], cached["fetched_at"]
            return self
        if self.fetch and self.key:
            try:
                items = []
                items += _fetch(self.key, {"source": "artificial-analysis"})
                items += _fetch(self.key, {"source": "design-arena", "arena": "models"})
                items += _fetch(self.key, {"source": "openrouter"})
                self.signals, self.fetched_at = parse(items), time.time()
                self.path.write_text(json.dumps({"fetched_at": self.fetched_at, "signals": self.signals}))
                return self
            except Exception as e:
                detail = ""
                if hasattr(e, "read"):
                    try:
                        detail = e.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                self.error = f"{type(e).__name__}: {e} {detail}".strip()
                log.warning("couldn't fetch benchmarks (%s); using cache or price-based guesses", self.error)
        if cached:
            self.signals, self.fetched_at = cached["signals"], cached.get("fetched_at", 0)
        return self

    def prior(self, model_id: str, skill: str, pool: list[str]) -> float | None:
        """Percentile (0-1) of this model among `pool` for a skill, or None if no data.

        A signal the model has no score for counts as an unknown middle (0.5) rather than being
        skipped - otherwise a strong coder with no design scores would look like a top designer.
        """
        weights = SKILL_SIGNALS.get(skill, SKILL_SIGNALS["other"])
        total, wsum, known = 0.0, 0.0, 0
        for signal, w in weights.items():
            scores = self.signals.get(signal) or {}
            ranked = [scores[m] for m in pool if m in scores]
            if len(ranked) < 2:
                continue
            if model_id in scores:
                below = sum(1 for s in ranked if s < scores[model_id])
                ties = sum(1 for s in ranked if s == scores[model_id]) - 1
                total += w * (below + 0.5 * ties) / (len(ranked) - 1)
                known += 1
            else:
                total += w * 0.5
            wsum += w
        return total / wsum if wsum and known else None

    def remap(self, models: dict) -> int:
        """Benchmark feeds name models by permanent dated ids (e.g. "...-20260921"); the catalog and
        roster use short ids. Translate via the catalog's canonical slugs (date-stripping as a
        fallback) so scores land on the models we actually route to. Returns how many were mapped."""
        alias: dict[str, str] = {}
        for mid, m in models.items():
            alias[mid] = mid
            canon = getattr(m, "canonical", "") or ""
            if canon:
                alias.setdefault(canon, mid)
                alias.setdefault(DATE_SUFFIX.sub("", canon), mid)
        mapped = 0
        for sig, scores in self.signals.items():
            for key, score in list(scores.items()):
                target = (alias.get(key) or alias.get(DATE_SUFFIX.sub("", key))
                          or alias.get(key.split(":")[0]))
                if target and target != key:
                    scores[target] = max(scores.get(target, score), score)
                    mapped += 1
        return mapped

    def status(self) -> dict:
        models = {m for sig in self.signals.values() for m in sig}
        return {"models": len(models), "signals": sorted(self.signals), "fetched_at": self.fetched_at,
                "error": "" if models else (self.error or "Benchmark feed returned no usable scores.")}

    def summary(self, model_id: str) -> dict[str, float]:
        return {sig: round(v[model_id], 1) for sig, v in self.signals.items() if model_id in v}
