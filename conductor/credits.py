"""OpenRouter credits: watch the balance, and pause/resume work when it runs out.

What we can see:
  GET /api/v1/key      (your normal key)  - the key's own limit, and any budgets that apply to it
  GET /api/v1/credits  (management key)   - the account balance; set OPENROUTER_MANAGEMENT_KEY to enable

Three kinds of "can't spend" come back as HTTP 402 (error.metadata.limit_source / reason):
  in-flight budget full   transient: wait Retry-After, retry           (CreditError kind="in_flight")
  request too big        retry with a smaller max_tokens               (kind="too_big")
  out of credits/limit    pause the task until credits come back        (kind="out")
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request

log = logging.getLogger("conductor.credits")

KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
TOP_UP_URL = "https://openrouter.ai/settings/credits"
IDLE_MAX_AGE = 15 * 60       # seconds a cached balance is good for while nothing is running
PAUSED_MAX_POLL = 15 * 60    # longest wait between balance checks while work is paused


class CreditError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None, affordable: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.affordable = affordable


def classify_402(status: int, body, headers=None, message: str = "") -> CreditError | None:
    """Turn a provider error into a CreditError if it's about money, else None."""
    meta = {}
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        meta = (err or {}).get("metadata") or {}
        message = (err or {}).get("message") or message
    text = f"{message} {json.dumps(meta) if meta else ''}".lower()
    retry_after = None
    if headers is not None:
        try:
            retry_after = float(headers.get("retry-after"))
        except (TypeError, ValueError):
            retry_after = None
    affordable = None
    m = re.search(r"can only afford (\d+)", text)
    if m:
        affordable = int(m.group(1))
    source, reason = meta.get("limit_source", ""), meta.get("reason", "")
    money_words = ("credit", "afford", "balance", "insufficient", "budget", "payment")
    if status != 402 and not ("credit balance is too low" in text or (status == 400 and "credit" in text and "low" in text)):
        return None
    if status == 402 and not (source or any(w in text for w in money_words)):
        return CreditError("out", message or "Payment required.")
    if (source == "openrouter_in_flight_budget" or reason == "in_flight_budget_exhausted"
            or "in-flight" in text or "in flight" in text):
        return CreditError("in_flight", message, retry_after or 10.0)
    if (reason == "weight_exceeds_budget" or affordable is not None or "fewer max_tokens" in text
            or "lower max_tokens" in text or "maximum cost exceeds" in text or "prompt size" in text):
        return CreditError("too_big", message, affordable=affordable)
    return CreditError("out", message or "Out of credits.")


class AdaptiveLimiter:
    """Caps how many model calls run at once. OpenRouter's in-flight budget (on smaller balances) rejects
    requests whose combined worst-case cost exceeds a share of your balance, so when that happens we
    run fewer at a time, and slowly widen again while calls succeed."""

    def __init__(self, start: int = 6, floor: int = 1, recover_after: float = 90.0):
        self.limit, self.ceiling, self.floor = start, start, floor
        self.active = 0
        self.recover_after = recover_after
        self._last_change = 0.0
        self._cond: asyncio.Condition | None = None

    def _c(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def __aenter__(self):
        c = self._c()
        async with c:
            await c.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        c = self._c()
        async with c:
            self.active -= 1
            c.notify_all()

    def shrink(self) -> None:
        self.limit = max(self.floor, min(self.limit, self.active) - 1 if self.active > 1 else self.floor)
        self._last_change = time.time()

    async def succeeded(self) -> None:
        if self.limit < self.ceiling and time.time() - self._last_change > self.recover_after:
            self.limit += 1
            self._last_change = time.time()
            c = self._c()
            async with c:
                c.notify_all()


class CreditMonitor:
    def __init__(self, key: str = "", management_key: str = "", low: float = 2.0, check_every: float = 60.0,
                 fetch: bool = True):
        self.key, self.management_key = key, management_key
        self.low_threshold = low
        self.check_every = check_every
        self.fetch = fetch
        self.state = {"available": None, "source": None, "checked": 0.0, "error": "", "spent_today": None,
                      "out": False}
        self._resume: asyncio.Event | None = None  # created lazily inside the running loop

    # ------------------------------------------------------------------ reading the balance
    def _get(self, url: str, key: str) -> dict:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "ai-conductor"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("data", {})

    def _check_sync(self) -> dict:
        known: list[tuple[float, str]] = []
        spent_today = None
        err = ""
        if self.key:
            try:
                k = self._get(KEY_URL, self.key)
                spent_today = k.get("usage_daily")
                if isinstance(k.get("limit_remaining"), (int, float)):
                    known.append((float(k["limit_remaining"]), "key limit"))
                eff = k.get("effective_budget") or {}
                if isinstance(eff.get("remaining_usd"), (int, float)):
                    known.append((float(eff["remaining_usd"]), f"{eff.get('reset_interval') or ''} budget".strip()))
            except Exception as e:
                err = f"key check failed: {e}"
        if self.management_key:
            try:
                c = self._get(CREDITS_URL, self.management_key)
                known.append((float(c.get("total_credits", 0)) - float(c.get("total_usage", 0)), "account balance"))
            except Exception as e:
                err = (err + "; " if err else "") + f"balance check failed: {e}"
        available, source = (min(known) if known else (None, None))
        return {"available": available, "source": source, "checked": time.time(), "error": err,
                "spent_today": spent_today}

    async def check(self, force: bool = False, max_age: float | None = None) -> dict:
        """Balance, re-read from OpenRouter only when the cached value is older than max_age
        (default: check_every). Callers pass a long max_age when nothing is running."""
        if not self.fetch or not self.key:
            return self.status()
        if force or time.time() - self.state["checked"] > (self.check_every if max_age is None else max_age):
            fresh = await asyncio.to_thread(self._check_sync)
            out = self.state.get("out", False)
            if fresh["available"] is not None:
                out = fresh["available"] <= 0.01
            self.state = {**fresh, "out": out}
        return self.status()

    def status(self) -> dict:
        a = self.state["available"]
        if self.state.get("out"):
            level = "out"
        elif a is None:
            level = "unknown"
        elif a < self.low_threshold:
            level = "low"
        else:
            level = "ok"
        return {**self.state, "level": level, "low_threshold": self.low_threshold,
                "has_management_key": bool(self.management_key), "top_up_url": TOP_UP_URL}

    @property
    def low(self) -> bool:
        return self.status()["level"] in ("low", "out")

    def mark_out(self) -> None:
        self.state["out"] = True

    # ------------------------------------------------------------------ pausing until credits are back
    def _event(self) -> asyncio.Event:
        if self._resume is None:
            self._resume = asyncio.Event()
        return self._resume

    def resume_now(self) -> None:
        """The user says credits are back (or wants to try anyway)."""
        self.state["out"] = False
        self._event().set()

    async def wait_for_credits(self, poll: float | None = None, need: float | None = None) -> str:
        """Block until credits look available again (polling) or the user resumes. Returns how it resumed.

        `need` is the balance the paused step actually requires (or the balance at pause time plus a
        margin): resuming on less would only fail again and loop."""
        ev = self._event()
        ev.clear()
        poll = poll or self.check_every
        while True:
            try:
                await asyncio.wait_for(ev.wait(), timeout=poll)
                return "user"
            except asyncio.TimeoutError:
                pass
            # Credits rarely come back on their own minutes later; check less and less often
            # (up to every 15 minutes) so a task paused overnight doesn't poll all night.
            poll = min(poll * 1.6, PAUSED_MAX_POLL)
            st = await self.check(max_age=30)  # several paused parts share one look at the balance
            if st["available"] is None:
                return "probe"  # can't see the balance: retrying the step is free (rejected before any model runs)
            if st["available"] > max(0.05, self.low_threshold / 4, need or 0.0):
                self.state["out"] = False
                return "credits"
