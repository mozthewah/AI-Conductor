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

import asyncio
import time

import anthropic
import httpx2

from .credits import AdaptiveLimiter, CreditError, classify_402
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
                   session_id: str | None = None, providers: list[str] | None = None) -> dict: ...

    async def structured(self, *, model: ModelInfo, system: str, prompt: str, schema: dict,
                         effort: str = "low") -> dict: ...


class StreamError(Exception):
    """An error event arrived mid-stream (after HTTP 200)."""

    def __init__(self, message: str, etype: str = "error"):
        super().__init__(message)
        self.etype = etype


TRANSIENT_STREAM_ERRORS = {"api_error", "overloaded_error", "rate_limit_error", "timeout_error"}
RETRY_DELAYS = [2, 5, 12, 25]  # seconds; network drops and provider hiccups are usually brief


def is_transient(exc: Exception) -> bool:
    """Worth retrying: the connection dropped, or the provider had a momentary problem."""
    if isinstance(exc, StreamError):
        return exc.etype in TRANSIENT_STREAM_ERRORS or "upstream" in str(exc).lower()
    if isinstance(exc, (httpx2.TransportError, anthropic.APIConnectionError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in (408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529)
    return isinstance(exc, (ConnectionError, TimeoutError))


class MessagesLLM:
    def __init__(self, *, anthropic_enabled: bool, openrouter_key: str = "", max_tokens: int = 64000,
                 http_client=None):
        self.max_tokens = max_tokens
        extra = {"http_client": http_client} if http_client is not None else {}
        self._anthropic = anthropic.AsyncAnthropic(**extra) if anthropic_enabled else None
        self._openrouter = anthropic.AsyncAnthropic(
            auth_token=openrouter_key, base_url=OPENROUTER_BASE_URL, default_headers=OPENROUTER_HEADERS, **extra,
        ) if openrouter_key else None
        self.limiter = AdaptiveLimiter()  # parallel OpenRouter calls; narrows when the in-flight budget bites
        self.audit = None  # set by the Conductor; logs the small one-shot calls (routing, checks, reflection)

    def _client(self, model: ModelInfo):
        client = self._anthropic if model.provider == "anthropic" else self._openrouter
        if client is None:
            key = "ANTHROPIC_API_KEY" if model.provider == "anthropic" else "OPENROUTER_API_KEY"
            raise RuntimeError(f"{model.id} needs {key} - add it to .env or pick another model.")
        return client

    # ------------------------------------------------------------------ requests
    def _request(self, model: ModelInfo, system: str, messages: list[dict], tools: list[dict],
                 effort: str, web: bool, session_id: str | None, providers: list[str] | None = None) -> dict:
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
            extra: dict[str, Any] = {}
            if session_id:  # sticky provider routing for prompt-cache hits
                extra["session_id"] = session_id[:256]
            if providers:  # run only on these providers (your own BYOK keys when credits are out)
                extra["provider"] = {"only": providers, "allow_fallbacks": False}
            if extra:
                kw["extra_body"] = extra
        return kw

    async def turn(self, *, model: ModelInfo, system: str, messages: list[dict], tools: list[dict],
                   on_event: OnEvent, effort: str = "high", web: bool = True,
                   session_id: str | None = None, providers: list[str] | None = None) -> dict:
        client = self._client(model)
        kw = self._request(model, system, messages, tools, effort, web, session_id, providers)
        retries = 0
        plain = False
        waits = 0
        drops = 0
        while True:
            try:
                if model.provider == "openrouter":
                    async with self.limiter:
                        result = await self._stream(client, model, kw, on_event)
                    await self.limiter.succeeded()
                    return result
                return await self._stream(client, model, kw, on_event)
            except anthropic.APIStatusError as e:
                credit = classify_402(e.status_code, e.body, getattr(e.response, "headers", None), e.message)
                if credit is None:
                    if is_transient(e) and drops < len(RETRY_DELAYS):
                        drops = await self._retry_after_drop(e, drops, on_event)
                        continue
                    if not isinstance(e, anthropic.BadRequestError):
                        raise
                    if plain or model.provider != "openrouter" or "messages" not in str(e.body or e.message):
                        raise
                    plain = True
                    kw["messages"] = adapt_messages(messages, model, plain=True)
                    await on_event("status", {"text": "The provider rejected part of the history; retrying with a simplified copy."})
                    continue
                if credit.kind == "in_flight" and waits < 8:
                    waits += 1
                    self.limiter.shrink()  # too much running at once for this balance: run fewer
                    delay = min(credit.retry_after or 10.0, 60.0)
                    await on_event("status", {"text": f"Lots of requests in flight for your balance; retrying in {delay:.0f}s."})
                    await asyncio.sleep(delay)
                    continue
                if credit.kind == "too_big" and kw["max_tokens"] > 4096:
                    target = int(credit.affordable * 0.9) if credit.affordable else kw["max_tokens"] // 2
                    kw["max_tokens"] = max(4096, min(target, kw["max_tokens"] // 2 if not credit.affordable else target))
                    await on_event("status", {"text": f"Credits are tight; retrying with a smaller reply budget ({kw['max_tokens']:,} tokens)."})
                    continue
                raise credit
            except (StreamError, httpx2.TransportError, anthropic.APIConnectionError, ConnectionError, TimeoutError) as e:
                if not is_transient(e) or drops >= len(RETRY_DELAYS):
                    raise
                drops = await self._retry_after_drop(e, drops, on_event)
            except ValueError:
                # Tool-input JSON that couldn't be parsed at all. It broke before the
                # tool_use block completed, so there's nothing to answer; re-issue the turn.
                retries += 1
                if retries > 2:
                    raise
                await on_event("status", {"text": "Retrying a garbled tool call..."})

    async def _retry_after_drop(self, exc: Exception, drops: int, on_event: OnEvent) -> int:
        delay = RETRY_DELAYS[drops]
        what = "The connection dropped" if isinstance(exc, (httpx2.TransportError, anthropic.APIConnectionError,
                                                             ConnectionError)) else "The model provider had a hiccup"
        await on_event("turn_reset", {})  # discard the half-streamed attempt in the UI
        detail = " ".join(str(getattr(exc, "message", "") or exc).split())[:200] or type(exc).__name__
        await on_event("status", {"text": f"{what}; retrying in {delay}s (attempt {drops + 2} of {len(RETRY_DELAYS) + 1}).",
                                  "error": f"{type(exc).__name__}: {detail}"})
        await asyncio.sleep(delay)
        return drops + 1

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
        started = time.time()
        try:
            async with client.messages.stream(**kw) as stream:
                final = await stream.get_final_message()
        except Exception as e:
            if self.audit:
                self.audit.write("model.aux_error", "system", model=model.id, purpose=system[:60],
                                 error=f"{type(e).__name__}: {e}")
            raise
        if self.audit:
            u = final.usage
            self.audit.write("model.aux_call", "system", model=model.id, purpose=system[:60],
                             tokens_in=u.input_tokens, tokens_out=u.output_tokens,
                             cost_usd=round(model.cost(u.input_tokens or 0, u.output_tokens or 0), 6),
                             ms=int((time.time() - started) * 1000))
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
            raise StreamError(f"{err.get('type', 'error')}: {err.get('message', 'stream failed')}",
                              etype=err.get("type", "error"))

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
            elif t and t.endswith("_tool_result"):
                content = b.get("content")
                if isinstance(content, list):
                    results = [{"title": r.get("title", ""), "url": r.get("url", "")}
                               for r in content if isinstance(r, dict) and r.get("url")]
                    if results:
                        await self.on_event("sources", {"results": results[:8]})
                    await self.on_event("server_tool_result", {"id": b.get("tool_use_id"), "ok": True,
                                                               "detail": f"{len(results)} results"})
                elif isinstance(content, dict):
                    err = content.get("error_code") or content.get("error")
                    if err:
                        await self.on_event("server_tool_result", {"id": b.get("tool_use_id"), "ok": False,
                                                                   "detail": str(err)})
                    else:
                        parts: list[str] = []
                        _collect_text(content, parts, 10**9)
                        size = sum(len(p) for p in parts)
                        await self.on_event("server_tool_result", {
                            "id": b.get("tool_use_id"), "ok": size > 0,
                            "detail": f"{size:,} characters" if size else "came back empty"})
            elif t == "text":
                urls = [c for c in b.get("citations") or [] if isinstance(c, dict) and c.get("url")]
                if urls:
                    await self.on_event("sources", {"results": [
                        {"title": c.get("title", ""), "url": c["url"]} for c in urls][:8]})
            elif t == "fallback":
                await self.on_event("status", {"text": "Switched to a fallback model for this step."})


def adapt_messages(messages: list[dict], model: ModelInfo, plain: bool = False) -> list[dict]:
    """Per-model view of the stored transcript.

    Deterministic for a given model, so its prefix stays cache-stable and append-only.
    When a task switches models mid-way, turns written by the earlier model are shown to
    the new one without that model's private reasoning blocks (which only the model that
    wrote them can read) and with provider-specific web-tool blocks turned into plain text.
    """
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            if m["role"] == "assistant" and (plain or (m.get("model") and m["model"] != model.id)):
                content = _foreign_turn(content)
            if not model.vision:
                content = [_strip_images(b) for b in content]
            if model.provider == "openrouter":
                content = sanitize_for_openrouter(content)
        out.append({"role": m["role"], "content": content})
    return out


# Block types OpenRouter's Messages endpoint accepts as *input*. Its responses can include
# others (e.g. web-fetch results) that it then rejects when sent back, so we translate those.
OPENROUTER_INPUT_BLOCKS = {
    "text", "image", "document", "tool_use", "tool_result", "thinking", "redacted_thinking",
    "server_tool_use", "web_search_tool_result", "search_result", "compaction", "advisor_tool_result",
    "tool_addition", "tool_removal", "openrouter_shell_tool_result", "openrouter_bash_tool_result",
    "openrouter_tool_search_result",
}


def _collect_text(node, out: list[str], budget: int = 6000) -> None:
    """Pull readable text (titles, urls, page text) out of a provider-specific result block."""
    if sum(len(x) for x in out) >= budget:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for key in ("title", "url", "text", "data", "content", "source", "result", "results", "error_code"):
            if key in node and key not in ("type",):
                v = node[key]
                if key in ("title", "url", "error_code") and isinstance(v, str):
                    out.append(f"{key}: {v}")
                else:
                    _collect_text(v, out, budget)
    elif isinstance(node, list):
        for item in node:
            _collect_text(item, out, budget)


def _as_text(block: dict) -> dict:
    t = block.get("type", "block")
    if t.endswith("tool_use"):
        body = f"[{block.get('name', t)} call: {json.dumps(block.get('input', {}))[:500]}]"
    else:
        parts: list[str] = []
        _collect_text(block.get("content", block), parts)
        text = "\n".join(p for p in parts if p.strip())
        if not text:  # unfamiliar shape: keep a compact copy rather than dropping it
            text = json.dumps({k: v for k, v in block.items() if k != "type"}, ensure_ascii=False)[:2000]
        if len(text) > 6000:
            text = text[:6000] + "\n... [truncated]"
        body = f"[{t.replace('_', ' ')}]\n{text}" if text else f"[{t.replace('_', ' ')}]"
    return {"type": "text", "text": body}


def sanitize_for_openrouter(content: list[dict]) -> list[dict]:
    """Turn blocks OpenRouter won't accept back into plain text (keeping what they said).
    A server_tool_use whose result can't be sent is converted too, so no call is left unanswered."""
    bad_ids = {b.get("tool_use_id") for b in content
               if isinstance(b, dict) and b.get("type") not in OPENROUTER_INPUT_BLOCKS and b.get("tool_use_id")}
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        ok = t in OPENROUTER_INPUT_BLOCKS and not (t == "server_tool_use" and b.get("id") in bad_ids)
        out.append(b if ok else _as_text(b))
    if not out:
        out = [{"type": "text", "text": "(no content)"}]
    return out


def _foreign_turn(blocks: list[dict]) -> list[dict]:
    kept: list[dict] = []
    notes: list[str] = []
    for b in blocks:
        t = b.get("type", "")
        if t in ("thinking", "redacted_thinking", "fallback"):
            continue
        if t == "server_tool_use":
            notes.append(f"[{b.get('name', 'tool')}: {json.dumps(b.get('input', {}))[:300]}]")
            continue
        if t.endswith("_tool_result"):
            items = b.get("content") if isinstance(b.get("content"), list) else []
            found = [f"- {r.get('title', '')} {r.get('url', '')}".strip() for r in items if isinstance(r, dict)][:8]
            notes.append("[results]\n" + "\n".join(found) if found else "[result received]")
            continue
        if t in ("text", "tool_use"):
            if notes and t == "text":
                kept.append({"type": "text", "text": "\n".join(notes)})
                notes = []
            kept.append({k: v for k, v in b.items() if k != "citations"} if t == "text" else b)
            continue
    if notes:
        kept.insert(0, {"type": "text", "text": "\n".join(notes)})
    if not kept:
        kept = [{"type": "text", "text": "(continuing)"}]
    # tool_use must come last in an assistant turn
    texts = [b for b in kept if b["type"] == "text"]
    uses = [b for b in kept if b["type"] == "tool_use"]
    return texts + uses


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
