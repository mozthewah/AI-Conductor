"""Small chores on OpenRouter's free models: chat titles and follow-up suggestions.

Free models (ids ending in `:free`) cost nothing but are rate-limited (OpenRouter allows a modest number of
free requests per day) and some providers log or train on prompts, so they only get short snippets for
low-stakes jobs. Conductor tries the best-looking free models in turn, skips one for a while after it
fails, and if none work it uses the cheap router model (unless credits are out) or simply skips the chore.
CONDUCTOR_FREE_MODELS=off turns this off (titles then fall back to the first line of the message).
"""

from __future__ import annotations

import logging
import re
import time

log = logging.getLogger("conductor.chores")

# Small, quick, instruction-following families first; then anything else that's free.
PREFERRED = ("gemma", "nemotron-3.5-lightning", "llama", "qwen", "mistral", "deepseek", "nemotron", "dots")
NOT_TEXT = ("lyria", "image", "tts", "audio", "embed", "rerank", "whisper", "video")
COOLDOWN = 15 * 60

TITLE_SYSTEM = ("You name chats. Reply with only a short, specific title of 3 to 6 words for the user's "
                "request: no quotes, no trailing punctuation, no markdown, no emoji.")
SUGGEST_SYSTEM = ("You suggest what the user might say next. Given their request and the assistant's final "
                  "reply, write 3 short follow-ups the user is likely to send (each under 10 words, written "
                  "as the user, specific to this work). One per line, no numbering, no quotes.")


class Chores:
    def __init__(self, conductor):
        self.c = conductor
        self.mode = conductor.settings.free_models
        self._cool: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.c.settings.openrouter_key or self.c.settings.local_default) and hasattr(self.c.llm, "quick")

    def candidates(self) -> list[str]:
        if self.mode not in ("auto", "on", "off") and ":" in self.mode:
            return [self.mode]  # a specific free model was chosen
        free = [m for m in self.c.catalog.models.values()
                if m.provider == "openrouter" and m.id.endswith(":free")
                and not any(k in m.id.lower() for k in NOT_TEXT)]
        rank = lambda m: (next((i for i, k in enumerate(PREFERRED) if k in m.id.lower()), len(PREFERRED)),
                          -m.context)
        now = time.time()
        return [m.id for m in sorted(free, key=rank) if self._cool.get(m.id, 0) <= now]

    async def ask(self, system: str, prompt: str, max_tokens: int = 300) -> str | None:
        if not self.enabled:
            return None
        for mid in self.candidates()[:3]:
            try:
                out = (await self.c.llm.quick(model=self.c.catalog.get(mid), system=system, prompt=prompt,
                                              max_tokens=max_tokens)).strip()
                if out:
                    return out
            except Exception as e:  # rate limited, data-policy block, provider down...
                log.info("free model %s failed: %s", mid, e)
            self._cool[mid] = time.time() + COOLDOWN
        if self.c.credits.status()["level"] == "out":
            return None
        try:  # last resort: the cheap router model (fractions of a cent)
            return (await self.c.llm.quick(model=self.c.catalog.get(self.c.settings.default_router_model),
                                           system=system, prompt=prompt, max_tokens=max_tokens)).strip() or None
        except Exception:
            return None

    async def title(self, prompt: str) -> str | None:
        raw = await self.ask(TITLE_SYSTEM, prompt[:3000], max_tokens=300)
        return clean_title(raw) if raw else None

    async def suggestions(self, prompt: str, answer: str) -> list[str]:
        raw = await self.ask(SUGGEST_SYSTEM, f"Request:\n{prompt[:2000]}\n\nFinal reply:\n{answer[-3000:]}",
                             max_tokens=400)
        if not raw:
            return []
        out = []
        for line in raw.splitlines():
            line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"“”').strip()
            if 2 <= len(line) <= 90 and not line.lower().startswith(("here are", "follow-up", "sure")):
                out.append(line)
        return out[:3]


def clean_title(raw: str) -> str | None:
    """Keep the first sensible line, without quotes, markdown, or a 'Title:' label."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    for line in raw.splitlines():
        t = re.sub(r"^(?:#+\s*|title\s*:\s*|\*\*)", "", line.strip(), flags=re.I).strip()
        for _ in range(2):  # quotes and trailing punctuation, in either order
            t = t.strip('*"“”\'`').rstrip(".!:;").strip()
        if 2 <= len(t) <= 80 and len(t.split()) <= 12:
            return t[:60]
    return None
