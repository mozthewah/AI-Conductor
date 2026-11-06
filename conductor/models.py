"""Model catalog: which models Conductor can use, what they cost, what they can do.

Two providers, one wire format (the Anthropic Messages API):
  - Anthropic direct  - ids like "claude-opus-5-5" (needs ANTHROPIC_API_KEY)
  - OpenRouter        - ids like "openai/gpt-6.1-sol" (needs OPENROUTER_API_KEY)

OpenRouter's public model list is fetched live (and cached) so prices and
capabilities stay current. The *roster* is the short list the router picks
from; edit it in the UI (Models page) or in data/roster.json.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("conductor.models")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CATALOG_TTL = 24 * 3600

TIERS = ["fast", "strong", "frontier"]


@dataclass
class ModelInfo:
    id: str
    provider: str  # "anthropic" | "openrouter"
    name: str = ""
    price_in: float = 0.0   # USD per million input tokens
    price_out: float = 0.0  # USD per million output tokens
    context: int = 200_000
    max_output: int = 32_000
    tools: bool = True
    vision: bool = False
    reasoning: bool = False
    structured: bool = False
    tier: str = "strong"
    notes: str = ""

    @property
    def is_claude(self) -> bool:
        return self.provider == "anthropic" or self.id.startswith("anthropic/")

    def cost(self, input_tokens: int, output_tokens: int, cache_read: int = 0) -> float:
        fresh = max(input_tokens - cache_read, 0)
        return (fresh * self.price_in + cache_read * self.price_in * 0.1 + output_tokens * self.price_out) / 1e6

    def to_dict(self) -> dict:
        return {**asdict(self), "is_claude": self.is_claude}


def tier_for(price_out: float) -> str:
    if price_out >= 8:
        return "frontier"
    if price_out >= 1.5:
        return "strong"
    return "fast"


# Direct-from-Anthropic models (prices per the Claude API, USD per MTok).
ANTHROPIC_DIRECT = {
    "claude-opus-5-5": ModelInfo("claude-opus-5-5", "anthropic", "Claude Opus 5.5", 4, 20, 1_000_000, 128_000,
                                 vision=True, reasoning=True, structured=True, tier="frontier"),
    "claude-sonnet-5-5": ModelInfo("claude-sonnet-5-5", "anthropic", "Claude Sonnet 5.5", 2, 10, 1_000_000, 128_000,
                                   vision=True, reasoning=True, structured=True, tier="strong"),
    "claude-haiku-5-5": ModelInfo("claude-haiku-5-5", "anthropic", "Claude Haiku 5.5", 0.1, 0.5, 1_000_000, 128_000,
                                  vision=True, reasoning=True, structured=True, tier="fast"),
    "claude-fable-5-1": ModelInfo("claude-fable-5-1", "anthropic", "Claude Fable 5.1", 10, 50, 1_000_000, 128_000,
                                  vision=True, reasoning=True, structured=True, tier="frontier"),
}

# A starting roster across labs and price points (all support tool calling on
# OpenRouter as of Nov 2026). The router learns which of these work best for you.
DEFAULT_OPENROUTER_ROSTER = [
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-haiku-5.5",
    "openai/gpt-6.1-sol",
    "openai/gpt-6.1-terra",
    "google/gemini-4-argon",
    "google/gemini-3.8-flash",
    "x-ai/grok-4.7",
    "moonshotai/kimi-k3.1-pro",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4.1-flash",
    "qwen/qwen3.8-max-0902",
    "z-ai/glm-5.3",
    "minimax/minimax-m3",
]

# Snapshot used only when the live catalog can't be fetched and nothing is cached.
_SNAPSHOT = {
    "anthropic/claude-opus-5.5": (4.0, 20.0, True, True, True),
    "anthropic/claude-sonnet-5.5": (2.0, 10.0, True, True, True),
    "anthropic/claude-haiku-5.5": (0.10, 0.50, True, True, True),
    "openai/gpt-6.1-sol": (2.0, 10.0, True, True, True),
    "openai/gpt-6.1-terra": (1.0, 5.0, True, True, True),
    "google/gemini-4-argon": (2.0, 10.0, True, True, True),
    "google/gemini-3.8-flash": (0.83, 4.12, True, True, True),
    "x-ai/grok-4.7": (2.0, 6.0, True, True, True),
    "moonshotai/kimi-k3.1-pro": (2.0, 10.0, True, True, True),
    "deepseek/deepseek-v4-pro": (0.96, 1.91, False, True, True),
    "deepseek/deepseek-v4.1-flash": (0.04, 0.50, True, True, True),
    "qwen/qwen3.8-max-0902": (2.0, 6.0, True, True, True),
    "z-ai/glm-5.3": (0.07, 1.40, False, True, True),
    "minimax/minimax-m3": (0.30, 1.20, True, True, True),
}


def parse_openrouter(entry: dict) -> ModelInfo | None:
    try:
        params = set(entry.get("supported_parameters") or [])
        pricing = entry.get("pricing") or {}
        pin = float(pricing.get("prompt", 0) or 0) * 1e6
        pout = float(pricing.get("completion", 0) or 0) * 1e6
        if pin < 0 or pout < 0:  # meta-routers like openrouter/auto report -1
            pin = pout = 0.0
        top = entry.get("top_provider") or {}
        mods = (entry.get("architecture") or {}).get("input_modalities") or []
        return ModelInfo(
            id=entry["id"], provider="openrouter", name=str(entry.get("name") or entry["id"]).split(": ", 1)[-1],
            price_in=round(pin, 4), price_out=round(pout, 4),
            context=int(entry.get("context_length") or 128_000),
            max_output=int(top.get("max_completion_tokens") or 32_000),
            tools="tools" in params, vision="image" in mods,
            reasoning="reasoning" in params, structured="structured_outputs" in params,
            tier=tier_for(pout),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class Catalog:
    data_dir: Path
    has_openrouter: bool
    has_anthropic: bool
    fetch: bool = True
    models: dict[str, ModelInfo] = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "openrouter_models.json"

    @property
    def roster_path(self) -> Path:
        return self.data_dir / "roster.json"

    def load(self) -> "Catalog":
        self.models = {}
        if self.has_anthropic:
            self.models.update(ANTHROPIC_DIRECT)
        if self.has_openrouter:
            self.models.update(self._openrouter_models())
        return self

    def _openrouter_models(self) -> dict[str, ModelInfo]:
        raw: list[dict] | None = None
        cached = None
        if self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text())
            except ValueError:
                cached = None
        fresh = cached and time.time() - cached.get("fetched_at", 0) < CATALOG_TTL
        if fresh:
            raw, self.fetched_at = cached["data"], cached["fetched_at"]
        elif self.fetch:
            try:
                req = urllib.request.Request(OPENROUTER_MODELS_URL, headers={"User-Agent": "ai-conductor"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = json.loads(r.read())["data"]
                self.fetched_at = time.time()
                self.cache_path.write_text(json.dumps({"fetched_at": self.fetched_at, "data": raw}))
            except Exception as e:
                log.warning("couldn't fetch the OpenRouter catalog (%s); using cached/snapshot data", e)
        if raw is None and cached:
            raw, self.fetched_at = cached["data"], cached.get("fetched_at", 0)
        if raw is None:
            return {mid: ModelInfo(mid, "openrouter", mid, pin, pout, 1_000_000, 32_000, True, vis, rsn, so,
                                   tier_for(pout)) for mid, (pin, pout, vis, rsn, so) in _SNAPSHOT.items()}
        out = {}
        for entry in raw:
            m = parse_openrouter(entry)
            if m:
                out[m.id] = m
        return out

    # -- roster ----------------------------------------------------------
    def default_roster(self) -> list[str]:
        roster = []
        if self.has_openrouter:
            roster += [m for m in DEFAULT_OPENROUTER_ROSTER if m in self.models]
        if self.has_anthropic and not self.has_openrouter:
            roster += ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
        return roster

    def roster(self) -> list[ModelInfo]:
        ids = None
        if self.roster_path.exists():
            try:
                ids = json.loads(self.roster_path.read_text())
            except ValueError:
                ids = None
        if ids is None:
            ids = self.default_roster()
        return [self.models[i] for i in ids if i in self.models and self.models[i].tools]

    def set_roster(self, ids: list[str]) -> None:
        self.roster_path.write_text(json.dumps(list(dict.fromkeys(ids)), indent=2))

    def add(self, model_id: str) -> None:
        if model_id not in self.models:
            raise KeyError(model_id)
        self.set_roster([m.id for m in self.roster()] + [model_id])

    def remove(self, model_id: str) -> None:
        self.set_roster([m.id for m in self.roster() if m.id != model_id])

    def get(self, model_id: str) -> ModelInfo:
        if model_id in self.models:
            return self.models[model_id]
        # Unknown id: guess the provider so a typo'd or brand-new model still gets a clear API error.
        provider = "openrouter" if "/" in model_id else "anthropic"
        return ModelInfo(model_id, provider, model_id, notes="not in catalog")

    def search(self, q: str, limit: int = 40) -> list[ModelInfo]:
        q = q.lower().strip()
        hits = [m for m in self.models.values() if m.tools and (q in m.id.lower() or q in m.name.lower())]
        return sorted(hits, key=lambda m: (m.price_out, m.id))[:limit]
