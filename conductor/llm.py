"""Thin streaming wrapper around the Claude Messages API.

The agent loop only ever sees plain dicts ({"content": [...], "stop_reason": ...}),
which keeps it easy to persist transcripts and to swap in a fake model for tests.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Protocol

import anthropic

OnEvent = Callable[[str, dict], Awaitable[None]]

# Progress-update thinking blocks (what the model just found / will do next) come
# back as readable text under display="updates"; real reasoning stays hidden.
BETAS = ["thinking-display-updates-2026-08-18", "server-side-fallback-2026-07-01"]

INTERRUPTED_SENTINEL = "This part of the response was interrupted before it finished."


class LLM(Protocol):
    model: str

    async def turn(self, *, system: str, messages: list[dict], tools: list[dict],
                   on_event: OnEvent, effort: str | None = None) -> dict: ...

    async def structured(self, *, system: str, prompt: str, schema: dict,
                         effort: str = "low") -> dict: ...


class ClaudeLLM:
    def __init__(self, model: str, effort: str = "high", max_tokens: int = 64000):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = anthropic.AsyncAnthropic()

    async def turn(self, *, system: str, messages: list[dict], tools: list[dict],
                   on_event: OnEvent, effort: str | None = None) -> dict:
        json_retries = 0
        while True:
            try:
                return await self._stream_once(system, messages, tools, on_event, effort or self.effort)
            except ValueError:
                # Tool-input JSON the SDK couldn't parse at all (eager input streaming).
                # Raised before the tool_use block completed, so just re-issue the turn.
                json_retries += 1
                if json_retries > 2:
                    raise
                await on_event("status", {"text": "Retrying a garbled tool call..."})

    async def _stream_once(self, system: str, messages: list[dict], tools: list[dict],
                           on_event: OnEvent, effort: str) -> dict:
        block_types: dict[int, str] = {}
        async with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system}],
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive", "display": "updates"},
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},  # auto-cache the growing transcript
            fallbacks="default",
            betas=BETAS,
        ) as stream:
            async for event in stream:
                et = event.type
                if et == "content_block_start":
                    block = event.content_block
                    block_types[event.index] = block.type
                    if block.type == "tool_use":
                        await on_event("tool_start", {"index": event.index, "name": block.name, "id": block.id})
                    elif block.type == "server_tool_use":
                        await on_event("server_tool", {"name": block.name, "id": block.id})
                    elif block.type == "text":
                        await on_event("text_start", {"index": event.index})
                elif et == "content_block_delta":
                    d = event.delta
                    if d.type == "thinking_delta" and d.thinking:
                        await on_event("progress_delta", {"index": event.index, "text": d.thinking})
                    elif d.type == "text_delta":
                        await on_event("text_delta", {"index": event.index, "text": d.text})
            final = await stream.get_final_message()

        content = [b.to_dict(exclude_none=True) for b in final.content]
        for b in content:  # surface web results so the UI can show sources
            if b.get("type") == "server_tool_use":
                await on_event("server_tool_input", {"id": b.get("id"), "name": b.get("name"),
                                                     "input": b.get("input", {})})
            elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
                await on_event("sources", {"results": [
                    {"title": r.get("title", ""), "url": r.get("url", "")}
                    for r in b["content"] if isinstance(r, dict) and r.get("url")][:8]})
            elif b.get("type") == "fallback":
                await on_event("status", {"text": "Switched to a fallback model for this step."})
        usage = final.usage
        return {
            "content": content,
            "stop_reason": final.stop_reason,
            "stop_details": final.stop_details.to_dict() if getattr(final, "stop_details", None) else None,
            "model": final.model,
            "usage": {
                "input_tokens": (usage.input_tokens or 0) + (usage.cache_read_input_tokens or 0)
                + (usage.cache_creation_input_tokens or 0),
                "output_tokens": usage.output_tokens or 0,
            },
        }

    async def structured(self, *, system: str, prompt: str, schema: dict, effort: str = "low") -> dict:
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            return {}
        text = next((b.text for b in final.content if b.type == "text"), "{}")
        return json.loads(text)


def text_of(content: list[dict]) -> str:
    return "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text")).strip()


def describe_api_error(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude rejected the credentials. Set ANTHROPIC_API_KEY (or run `ant auth login`)."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Claude API - try again in a minute."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The API rejected the request: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API (network error)."
    return f"{type(exc).__name__}: {exc}"


def tool_spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }
