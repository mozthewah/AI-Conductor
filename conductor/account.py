"""What the OpenRouter management key can tell Conductor about your account.

With OPENROUTER_MANAGEMENT_KEY set, Conductor reads (never changes):

  GET /byok                          your own provider keys (BYOK): which providers, and their filters
  GET /keys                          your API keys - to find the one Conductor runs on (its hash, limits)
  GET /guardrails, /guardrails/assignments/keys
                                     the guardrail on that key: allowed/ignored models and providers
  GET /activity                      the last 30 days, per model: spend, BYOK spend, requests, tokens
  GET /models/{id}/endpoints         which providers serve each roster model (no management key needed)

and uses it to:

  - know which models your own keys cover. BYOK requests cost only a 5% fee in OpenRouter credits
    (waived up to a monthly allowance) - the rest bills to your provider account - so when credits
    run out, work moves to a BYOK-covered model instead of pausing, pinned to your providers.
  - optionally prefer BYOK models while credits are fine (CONDUCTOR_BYOK=prefer).
  - keep models your guardrail blocks out of the roster, so they're never picked only to fail.
  - show 30-day spend per model, and BYOK-covered models you haven't added to the roster yet.

Everything is cached in data/account.json and refreshed every 30 minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://openrouter.ai/api/v1"
BYOK_FEE = 0.05          # share of the normal price charged in OpenRouter credits for BYOK requests
REFRESH_EVERY = 30 * 60
ENDPOINTS_EVERY = 6 * 3600
BYOK_SETTINGS_URL = "https://openrouter.ai/settings/integrations"

# Which model-id authors a BYOK provider can serve (used only to suggest models; actual coverage
# is confirmed from each model's endpoint list).
PROVIDER_AUTHORS = {
    "openai": ["openai"], "azure": ["openai"], "anthropic": ["anthropic"], "amazon-bedrock": ["anthropic", "amazon",
    "meta-llama", "mistralai", "deepseek", "qwen"], "google-vertex": ["google", "anthropic"],
    "google-ai-studio": ["google"], "mistral": ["mistralai"], "deepseek": ["deepseek"], "xai": ["x-ai"],
    "x-ai": ["x-ai"], "moonshotai": ["moonshotai"], "z-ai": ["z-ai"], "cohere": ["cohere"],
    "groq": ["meta-llama", "qwen", "moonshotai", "openai"], "together": ["meta-llama", "qwen", "deepseek"],
    "fireworks": ["meta-llama", "qwen", "deepseek", "moonshotai"], "minimax": ["minimax"], "xiaomi": ["xiaomi"],
}


def provider_slug(tag: str) -> str:
    """Endpoint tags can carry a region ("amazon-bedrock/us"); BYOK keys are per provider."""
    return (tag or "").split("/")[0].strip().lower()


class Account:
    def __init__(self, data_dir: Path, key: str = "", management_key: str = "", fetch: bool = True,
                 mode: str = "auto", key_name: str = ""):
        self.path = Path(data_dir) / "account.json"
        self.key_name = key_name.strip()
        self.failed: dict[str, str] = {}  # model -> why a BYOK request failed (this session)
        self.key, self.management_key, self.fetch = key, management_key, fetch
        self.mode = mode if mode in ("auto", "prefer", "off") else "auto"
        self.data: dict = {"checked": 0.0, "byok": [], "key": None, "guardrail": None, "activity": [],
                           "endpoints": {}, "errors": {}}
        self._load()

    # ------------------------------------------------------------------ cache
    def _load(self) -> None:
        try:
            self.data.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, indent=1))
        except OSError:
            pass

    # ------------------------------------------------------------------ fetching
    def _get(self, path: str, key: str, **params) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{API}{path}{'?' + q if q else ''}",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": "ai-conductor"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def _paged(self, path: str, **params) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            page = self._get(path, self.management_key, offset=offset, limit=100, **params)
            items = page.get("data") or []
            out += items
            total = page.get("total_count")
            offset += len(items)
            if not items or len(items) < 100 or (isinstance(total, int) and offset >= total) or offset > 2000:
                return out

    def _own_key(self, keys: list[dict]) -> dict | None:
        """Which of the account's API keys is the one Conductor runs on.

        Tried in order: CONDUCTOR_KEY_NAME, the key's SHA-256 hash, the masked label (in whatever form
        the list shows it: same prefix and suffix as the real key), then matching usage and limit
        against what the key reports about itself."""
        if not self.key:
            return None
        if self.key_name:
            for k in keys:
                if self.key_name in (k.get("name"), k.get("label")):
                    return k
        digest = hashlib.sha256(self.key.encode()).hexdigest()
        for k in keys:
            if k.get("hash") == digest:
                return k
        body = self.key.removeprefix("sk-or-v1-")

        def masked(k: dict) -> bool:
            for v in (k.get("label"), k.get("name")):
                if not isinstance(v, str) or "..." not in v and "…" not in v:
                    continue
                left, _, right = v.replace("…", "...").partition("...")
                left = left.removeprefix("sk-or-v1-")
                if left and right and body.startswith(left) and body.endswith(right) and len(left + right) >= 4:
                    return True
            return False
        hits = [k for k in keys if masked(k)]
        if len(hits) == 1:
            return hits[0]
        try:
            me = self._get("/key", self.key).get("data") or {}
        except Exception:
            me = {}
        self.data["key_label"] = me.get("label")
        if me.get("label"):
            for k in keys:
                if me["label"] in (k.get("label"), k.get("name")):
                    return k
        if me:  # same spend and limit as the key reports about itself (spend can tick up between calls)
            close = [k for k in (hits or keys)
                     if abs(float(k.get("usage") or 0) - float(me.get("usage") or 0)) < 0.02
                     and k.get("limit") == me.get("limit")
                     and abs(float(k.get("usage_daily") or 0) - float(me.get("usage_daily") or 0)) < 0.02]
            if len(close) == 1:
                return close[0]
        return None

    def refresh_sync(self, roster_ids: list[str] | None = None, force: bool = False) -> dict:
        errors: dict[str, str] = {}
        d = self.data
        if self.management_key:
            try:
                d["byok"] = [{k: b.get(k) for k in ("id", "provider", "name", "label", "disabled", "is_fallback",
                                                   "is_byok_only", "is_required", "allowed_models",
                                                   "allowed_api_key_hashes", "declared_zdr", "sort_order")}
                             for b in self._paged("/byok")]
            except Exception as e:
                errors["byok"] = _err(e)
            try:
                keys = self._paged("/keys")
                own = self._own_key(keys)
                d["key"] = None if not own else {k: own.get(k) for k in (
                    "hash", "name", "label", "limit", "limit_remaining", "limit_reset", "usage", "usage_daily",
                    "usage_weekly", "usage_monthly", "byok_usage", "byok_usage_monthly", "include_byok_in_limit",
                    "disabled", "expires_at")}
                d["key_count"] = len(keys)
                d["key_names"] = [k.get("name") or k.get("label") for k in keys][:20]
            except Exception as e:
                errors["keys"] = _err(e)
            try:
                d["guardrail"] = self._guardrail((d.get("key") or {}).get("hash"))
            except Exception as e:
                errors["guardrails"] = _err(e)
            try:
                d["activity"] = self._activity()
            except Exception as e:
                errors["activity"] = _err(e)
        if self.key and roster_ids:
            stale = force or time.time() - d.get("endpoints_checked", 0) > ENDPOINTS_EVERY
            wanted = [m for m in roster_ids + self.suggestion_ids() if "/" in m]
            missing = [m for m in wanted if m not in d["endpoints"]]
            for mid in (wanted if stale else missing)[:60]:
                try:
                    data = self._get(f"/models/{mid}/endpoints", self.key).get("data") or {}
                    d["endpoints"][mid] = sorted({provider_slug(e.get("tag") or e.get("provider_name", ""))
                                                  for e in data.get("endpoints") or []} - {""})
                except Exception as e:
                    errors.setdefault("endpoints", _err(e))
            if stale:
                d["endpoints_checked"] = time.time()
        d["errors"], d["checked"] = errors, time.time()
        self._save()
        return d

    def _guardrail(self, key_hash: str | None) -> dict | None:
        if not key_hash:
            return None
        assigned = [a for a in self._paged("/guardrails/assignments/keys") if a.get("key_hash") == key_hash]
        if not assigned:
            return None
        gid = assigned[0].get("guardrail_id")
        for g in self._paged("/guardrails"):
            if g.get("id") == gid:
                return {k: g.get(k) for k in ("id", "name", "allowed_models", "ignored_models", "allowed_providers",
                                              "ignored_providers", "limit_usd", "reset_interval", "enforce_zdr",
                                              "include_byok_in_budgets")}
        return None

    def _activity(self) -> list[dict]:
        rows = self._get("/activity", self.management_key).get("data") or []
        by_model: dict[str, dict] = {}
        for r in rows:
            m = by_model.setdefault(r.get("model") or "?", {"model": r.get("model") or "?", "usage": 0.0,
                                                           "byok_usage": 0.0, "requests": 0, "tokens": 0,
                                                           "providers": set()})
            m["usage"] += float(r.get("usage") or 0)
            m["byok_usage"] += float(r.get("byok_usage_inference") or 0)
            m["requests"] += int(r.get("requests") or 0)
            m["tokens"] += int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
            if r.get("provider_name"):
                m["providers"].add(r["provider_name"])
        out = [{**m, "usage": round(m["usage"], 4), "byok_usage": round(m["byok_usage"], 4),
                "providers": sorted(m["providers"])} for m in by_model.values()]
        return sorted(out, key=lambda m: m["usage"] + m["byok_usage"], reverse=True)

    async def refresh(self, roster_ids: list[str] | None = None, force: bool = False) -> dict:
        if not self.fetch or not (self.key or self.management_key):
            return self.data
        if not force and time.time() - self.data.get("checked", 0) < REFRESH_EVERY:
            return self.data
        return await asyncio.to_thread(self.refresh_sync, roster_ids, force)

    # ------------------------------------------------------------------ BYOK
    def byok_keys(self) -> list[dict]:
        """BYOK credentials Conductor's key may actually use."""
        own = (self.data.get("key") or {}).get("hash")
        out = []
        for b in self.data.get("byok") or []:
            if b.get("disabled"):
                continue
            hashes = b.get("allowed_api_key_hashes")
            if hashes and own and own not in hashes:
                continue  # restricted to other API keys
            out.append(b)
        return out

    @property
    def has_byok(self) -> bool:
        return self.mode != "off" and bool(self.byok_keys())

    def byok_for(self, model_id: str, canonical: str = "") -> list[str]:
        """Your providers (with a usable key) that serve this model."""
        if self.mode == "off" or model_id in self.failed:
            return []
        served = set(self.data.get("endpoints", {}).get(model_id) or [])
        out = []
        for b in self.byok_keys():
            p = provider_slug(b.get("provider", ""))
            allowed = b.get("allowed_models")
            if p in served and (not allowed or model_id in allowed or (canonical and canonical in allowed)):
                out.append(p)
        return sorted(set(out))

    def credit_cost(self, model_id: str, cost: float, canonical: str = "") -> float:
        """What a step costs in OpenRouter credits: BYOK-covered work is only the fee."""
        return cost * BYOK_FEE if self.byok_for(model_id, canonical) else cost

    def suggestion_ids(self) -> list[str]:
        """Model ids worth checking for BYOK coverage (from your providers' known authors)."""
        authors = {a for b in self.byok_keys() for a in PROVIDER_AUTHORS.get(provider_slug(b.get("provider", "")), [])}
        by_author = self.data.get("catalog_ids_by_author", {})
        return sorted({mid for a in authors for mid in by_author.get(a, [])})

    # ------------------------------------------------------------------ guardrail
    def blocked(self, model_id: str, canonical: str = "") -> str | None:
        """Why the guardrail on Conductor's key would reject this model, if it would."""
        g = self.data.get("guardrail") or {}
        if not g:
            return None
        names = {model_id, canonical} - {""}
        if g.get("allowed_models") and not names & set(g["allowed_models"]):
            return f"not in the models guardrail '{g.get('name')}' allows"
        if g.get("ignored_models") and names & set(g["ignored_models"]):
            return f"excluded by guardrail '{g.get('name')}'"
        served = set(self.data.get("endpoints", {}).get(model_id) or [])
        if served:
            usable = served
            if g.get("allowed_providers"):
                usable = usable & {provider_slug(p) for p in g["allowed_providers"]}
            if g.get("ignored_providers"):
                usable = usable - {provider_slug(p) for p in g["ignored_providers"]}
            if not usable:
                return f"guardrail '{g.get('name')}' allows none of the providers that serve it"
        return None

    # ------------------------------------------------------------------ for people and models
    def summary(self, catalog=None) -> dict:
        roster = catalog.roster() if catalog else []
        covered = [{"model": m.id, "name": m.name or m.id, "providers": self.byok_for(m.id, m.canonical)}
                   for m in roster]
        covered = [c for c in covered if c["providers"]]
        blocked = [{"model": m.id, "name": m.name or m.id, "why": self.blocked(m.id, m.canonical)}
                   for m in (list(catalog.models.values()) if catalog else []) if m.provider == "openrouter"]
        blocked = [b for b in blocked if b["why"]]
        in_roster = {m.id for m in roster}
        suggest = []
        if catalog:
            for mid in self.suggestion_ids():
                m = catalog.models.get(mid)
                if m and mid not in in_roster and m.tools and self.byok_for(mid, m.canonical):
                    suggest.append({"model": mid, "name": m.name or mid, "providers": self.byok_for(mid, m.canonical)})
        return {
            "has_management_key": bool(self.management_key), "mode": self.mode, "checked": self.data.get("checked"),
            "byok": [{k: b.get(k) for k in ("provider", "name", "label", "is_fallback", "is_byok_only",
                                             "is_required", "allowed_models")} for b in self.byok_keys()],
            "byok_disabled": [b.get("provider") for b in self.data.get("byok") or [] if b.get("disabled")],
            "byok_covered": covered, "byok_suggestions": suggest[:8], "key": self.data.get("key"),
            "key_count": self.data.get("key_count"), "guardrail": self.data.get("guardrail"),
            "guardrail_blocked": blocked[:40], "activity": (self.data.get("activity") or [])[:15],
            "errors": self.data.get("errors") or {}, "byok_settings_url": BYOK_SETTINGS_URL,
        }

    def note_catalog(self, catalog) -> None:
        """Remember the newest model per author, so BYOK suggestions can be checked."""
        by_author: dict[str, list[str]] = {}
        for m in catalog.models.values():
            if m.provider == "openrouter" and m.tools and "/" in m.id and ":" not in m.id:
                by_author.setdefault(m.id.split("/")[0], []).append(m.id)
        # catalog order is newest first on OpenRouter; keep a few per author
        self.data["catalog_ids_by_author"] = {a: ids[:3] for a, ids in by_author.items()}


def _err(e: Exception) -> str:
    code = getattr(e, "code", None)
    if code == 403:
        return "the management key isn't allowed to read this"
    if code == 401:
        return "the key was rejected"
    return f"{type(e).__name__}: {str(e)[:160]}"
