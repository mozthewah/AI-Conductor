"""A scripted stand-in for Claude so the agent loop can be tested offline."""

from __future__ import annotations

import itertools

_ids = itertools.count(1)


def tool_use(_tool: str, **inp) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": _tool, "input": inp}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def progress(t: str) -> dict:
    return {"type": "thinking", "thinking": t, "signature": "sig"}


class FakeLLM:
    model = "fake"

    def __init__(self, script: list, reflection: dict | None = None):
        self.script = list(script)
        self.calls: list[dict] = []
        self.reflection = reflection or {"summary": "Did a thing.", "memories": [], "save_skill": False,
                                         "skill_name": "", "skill_description": "", "skill_instructions": ""}
        self.structured_calls: list[dict] = []

    async def turn(self, *, system, messages, tools, on_event, effort=None):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages],
                           "tools": [t["name"] for t in tools], "effort": effort})
        step = self.script.pop(0)
        if callable(step):
            step = step(messages)
        blocks = step if isinstance(step, list) else step["content"]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                await on_event("text_delta", {"index": i, "text": b["text"]})
        stop = step.get("stop_reason") if isinstance(step, dict) else None
        if stop is None:
            stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return {"content": blocks, "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 5}}

    async def structured(self, *, system, prompt, schema, effort="low"):
        self.structured_calls.append({"prompt": prompt})
        return self.reflection
