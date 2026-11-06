"""Streaming model client for every provider Conductor supports.

Both providers speak the Anthropic Messages API: Anthropic natively, and
OpenRouter through its Messages-compatible endpoint (which translates for
OpenAI, Google, DeepSeek, etc.). So the agent loop, transcript format, and
tool protocol are identical no matter which model runs a task.

The agent loop only ever sees plain dicts ({"content": [...], "stop_reason": ...}),
which keeps transcripts easy to persist and lets tests swap in a fake model.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Protocol

import anthropic

from .models import ModelInfo

OnEvent = Callable[[str, dict], Awaitable[None]]

OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/mozthewah/AI-Conductor",
    "X-OpenRouter-Title": "AI Conductor",
}
# Direct-Anthropic betas: progress-update thinking blocks (what the model just found /
# will do next) and server-side fallback when a safety classifier declines a request.
ANTHROPIC_BETAS = ["thinking-display-updates-2026-08-18", "server-side-fallback-2026-07-01"]

INTERRUPTED_SENTINEL = "This part of the response was interrupted before it finished."


class LLM(Protocol):
    async def turn(self, *, model: ModelInfo, system: str, messages: list[dict], tools: list[dict],
                   on_event: OnEvent, effort: str = "high", web: bool = True,
                   session_id: str | None = None) -> dict: ...

    async def structured(self, *, model: ModelInfo, system: str, prompt: str, schema: dict,
                         effort: str = "low") -> dict: ...


class StreamError(Exception):
    """An error event arrived mid-stream (after HTTP 200)."""


class MessagesLLM:
    def __init__(self, *, anthropic_enabled: bool, openrouter_key: str = "", max_tokens: int = 64000,
                 http_client=None):
        self.max_tokens = max_tokens
        extra = {"http_client": http_client} if http_client is not None else {}
        self._anthropic = anthropic.AsyncAnthropic(**extra) if anthropic_enabled else None
        self._openrouter = anthropic.AsyncAnthropic(
            auth_token=openrouter_key, base_url=OPENROUTER_BASE_URL, default_headers=OPENROUTER_HEADERS, **extra,
        ) if openrouter_key else None

    def _client(self, model: ModelInfo):
        client = self._anthropic if model.provider == "anthropic" else self._openrouter
        if client is None:
            key = "ANTHROPIC_API_KEY" if model.provider == "anthropic" else "OPENROUTER_API_KEY"
            raise RuntimeError(f"{model.id} needs {key} - add it to .env or pick another model.")
        return client

    # ------------------------------------------------------------------ requests
    def _request(self, model: ModelInfo, system: str, messages: list[dict], tools: list[dict],
                 effort: str, web: bool, session_id: str | None) -> dict:
        direct = model.provider == "anthropic"
        kw: dict[str, Any] = {
            "model": model.id,
            "max_tokens": min(self.max_tokens, model.max_output or self.max_tokens),
            "system": [{"type": "text", "text": system}],
            "messages": adapt_messages(messages, model),
            "cache_control": {"type": "ephemeral"},  # auto-cache the growing transcript
            "stream": True,
        }
        if direct:
            all_tools = list(tools)
            if web:
                all_tools += [{"type": "web_search_20260209", "name": "web_search"},
                              {"type": "web_fetch_20260209", "name": "web_fetch"}]
            kw.update(tools=all_tools, thinking={"type": "adaptive", "display": "updates"},
                      output_config={"effort": effort}, fallbacks="default", betas=ANTHROPIC_BETAS)
        else:
            # OpenRouter's custom-tool schema has no eager_input_streaming field.
            all_tools = [{k: v for k, v in t.items() if k != "eager_input_streaming"} for t in tools]
            if web:  # OpenRouter-run web tools work with any model (native search where available)
                all_tools += [{"type": "openrouter:web_search", "parameters": {"max_results": 6}},
                              {"type": "openrouter:web_fetch"}]
            kw["tools"] = all_tools
            if model.reasoning:
                kw["thinking"] = {"type": "adaptive", "display": "updates" if model.is_claude else "summarized"}
                kw["output_config"] = {"effort": effort}
            if session_id:  # sticky provider routing for prompt-cache hits
                kw["extra_body"] = {"session_id": session_id[:256]}
        return kw

    async def turn(self, *, model: ModelInfo, system: str, messages: list[dict], tools: list[dict],
                   on_event: OnEvent, effort: str = "high", web: bool = True,
                   session_id: str | None = None) -> dict:
        client = self._client(model)
        kw = self._request(model, system, messages, tools, effort, web, session_id)
        retries = 0
        while True:
            try:
                return await self._stream(client, model, kw, on_event)
            except ValueError:
                # Tool-input JSON that couldn't be parsed at all. It broke before the
                # tool_use block completed, so there's nothing to answer; re-issue the turn.
                retries += 1
                if retries > 2:
                    raise
                await on_event("status", {"text": "Retrying a garbled tool call..."})

    async def _stream(self, client, model: ModelInfo, kw: dict, on_event: OnEvent) -> dict:
        api = client.beta.messages if "betas" in kw else client.messages
        acc = Accumulator(on_event)
        async with api.with_streaming_response.create(**kw) as resp:
            event_name = None
            async for line in resp.iter_lines():
                if not line or line.startswith(":"):  # keep-alives like ": OPENROUTER PROCESSING"
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    data = json.loads(payload)
                    await acc.feed(data.get("type") or event_name, data)
        result = acc.result()
        result["model"] = result.get("model") or model.id
        u = result["usage"]
        u["cost"] = model.cost(u["input_tokens"], u["output_tokens"], u.get("cache_read", 0))
        await acc.emit_extras()
        return result

    # ------------------------------------------------------------------ one-shot JSON
    async def structured(self, *, model: ModelInfo, system: str, prompt: str, schema: dict,
                         effort: str = "low") -> dict:
        client = self._client(model)
        kw: dict[str, Any] = {"model": model.id, "max_tokens": min(16000, model.max_output or 16000),
                              "system": system, "messages": [{"role": "user", "content": prompt}]}
        if model.structured:
            kw["output_config"] = {"format": {"type": "json_schema", "schema": schema},
                                   **({"effort": effort} if model.reasoning else {})}
        else:
            kw["messages"][0]["content"] = (
                f"{prompt}\n\nReply with only a JSON object matching this schema:\n{json.dumps(schema)}")
        async with client.messages.stream(**kw) as stream:
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            return {}
        text = "".join(b.text for b in final.content if b.type == "text")
        return parse_json_loose(text)


class Accumulator:
    """Builds the final message from raw Messages-API stream events.

    Written by hand (rather than the SDK's accumulator) so blocks it doesn't know
    about - OpenRouter server-tool results, fallback markers - pass through intact.
    """

    def __init__(self, on_event: OnEvent):
        self.on_event = on_event
        self.blocks: dict[int, dict] = {}
        self.partial: dict[int, str] = {}
        self.stop_reason: str | None = None
        self.stop_details: dict | None = None
        self.model: str | None = None
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0}

    def _usage(self, u: dict | None) -> None:
        if not u:
            return
        cache_read = u.get("cache_read_input_tokens") or 0
        cache_write = u.get("cache_creation_input_tokens") or 0
        if u.get("input_tokens") is not None:
            self.usage["input_tokens"] = (u.get("input_tokens") or 0) + cache_read + cache_write
            self.usage["cache_read"] = cache_read
        if u.get("output_tokens") is not None:
            self.usage["output_tokens"] = u["output_tokens"]

    async def feed(self, etype: str | None, e: dict) -> None:
        if etype == "message_start":
            msg = e.get("message") or {}
            self.model = msg.get("model")
            self._usage(msg.get("usage"))
        elif etype == "content_block_start":
            i, block = e["index"], dict(e["content_block"])
            self.blocks[i] = block
            bt = block.get("type")
            if bt == "tool_use":
                await self.on_event("tool_start", {"index": i, "name": block.get("name"), "id": block.get("id")})
            elif bt == "server_tool_use":
                await self.on_event("server_tool", {"name": block.get("name"), "id": block.get("id")})
            elif bt == "text":
                await self.on_event("text_start", {"index": i})
        elif etype == "content_block_delta":
            i, d = e["index"], e["delta"]
            b = self.blocks.setdefault(i, {"type": "text", "text": ""})
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d["text"]
                await self.on_event("text_delta", {"index": i, "text": d["text"]})
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d["thinking"]
                if d["thinking"]:
                    await self.on_event("progress_delta", {"index": i, "text": d["thinking"]})
            elif dt == "signature_delta":
                b["signature"] = b.get("signature", "") + d["signature"]
            elif dt == "input_json_delta":
                self.partial[i] = self.partial.get(i, "") + d.get("partial_json", "")
            elif dt == "citations_delta":
                b.setdefault("citations", []).append(d["citation"])
        elif etype == "content_block_stop":
            i = e["index"]
            if i in self.partial:
                raw = self.partial.pop(i)
                if raw.strip():
                    self.blocks[i]["input"] = json.loads(raw)  # ValueError -> caller retries the turn
        elif etype == "message_delta":
            d = e.get("delta") or {}
            self.stop_reason = d.get("stop_reason") or self.stop_reason
            self.stop_details = d.get("stop_details") or self.stop_details
            self._usage(e.get("usage"))
        elif etype == "error":
            err = e.get("error") or {}
            raise StreamError(f"{err.get('type', 'error')}: {err.get('message', 'stream failed')}")

    def result(self) -> dict:
        content = [self.blocks[i] for i in sorted(self.blocks)]
        for b in content:
            if b.get("type") == "text" and b.get("citations") is None:
                b.pop("citations", None)
        return {"content": content, "stop_reason": self.stop_reason, "stop_details": self.stop_details,
                "model": self.model, "usage": dict(self.usage)}

    async def emit_extras(self) -> None:
        """Surface web activity so the UI can show queries and sources."""
        for b in self.result()["content"]:
            t = b.get("type")
            if t == "server_tool_use":
                await self.on_event("server_tool_input", {"id": b.get("id"), "name": b.get("name"),
                                                          "input": b.get("input", {})})
            elif t and t.endswith("_tool_result") and isinstance(b.get("content"), list):
                results = [{"title": r.get("title", ""), "url": r.get("url", "")}
                           for r in b["content"] if isinstance(r, dict) and r.get("url")]
                if results:
                    await self.on_event("sources", {"results": results[:8]})
            elif t == "text":
                urls = [c for c in b.get("citations") or [] if isinstance(c, dict) and c.get("url")]
                if urls:
                    await self.on_event("sources", {"results": [
                        {"title": c.get("title", ""), "url": c["url"]} for c in urls][:8]})
            elif t == "fallback":
                await self.on_event("status", {"text": "Switched to a fallback model for this step."})


def adapt_messages(messages: list[dict], model: ModelInfo) -> list[dict]:
    """Per-model view of the stored transcript. Deterministic, so the prefix stays cache-stable."""
    if model.vision:
        return messages
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = [_strip_images(b) for b in content]
        out.append({**m, "content": content})
    return out


def _strip_images(block: dict) -> dict:
    if block.get("type") == "image":
        return {"type": "text", "text": "[image omitted: this model can't see images]"}
    if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
        return {**block, "content": [_strip_images(b) for b in block["content"]]}
    return block


def parse_json_loose(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return {}


def text_of(content: list[dict]) -> str:
    return "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text")).strip()


def describe_api_error(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return ("The model provider rejected the credentials. Check ANTHROPIC_API_KEY / OPENROUTER_API_KEY "
                "(OpenRouter keys start with sk-or-v1-).")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"Permission denied by the provider: {exc.message}"
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the model provider - try again in a minute."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The provider rejected the request: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402:
            return "OpenRouter says the account is out of credits."
        return f"Provider error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the model provider (network error)."
    return f"{type(exc).__name__}: {exc}"


def tool_spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }
