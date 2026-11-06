"""Exercise MessagesLLM against mocked HTTP endpoints: checks the exact requests we
send to Anthropic and to OpenRouter, and that real-shaped SSE streams parse."""

import json

import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient

from conductor.llm import MessagesLLM
from conductor.models import ANTHROPIC_DIRECT, ModelInfo


def sse(events, comments=False):
    out = ""
    for e in events:
        if comments:
            out += ": OPENROUTER PROCESSING\n\n"
        out += f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
    return out.encode()


def message_start(model):
    return {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 50,
                  "cache_creation_input_tokens": 0}}}


TOOL_TURN = [
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Checking the file."}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "abc"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"a.txt\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]

TOOLS = [{"name": "read_file", "description": "d", "eager_input_streaming": True,
          "input_schema": {"type": "object", "properties": {}}}]


def mock(stream_bytes, seen):
    def handler(request: httpx2.Request):
        seen.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=stream_bytes)
    return DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))


async def collect(llm, model, **kw):
    events = []

    async def on_event(kind, data):
        events.append((kind, data))
    out = await llm.turn(model=model, system="sys", messages=kw.pop("messages", [{"role": "user", "content": "hi"}]),
                         tools=TOOLS, on_event=on_event, session_id="t_123", **kw)
    return out, events


async def test_anthropic_direct_request_and_parsing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = []
    llm = MessagesLLM(anthropic_enabled=True, http_client=mock(sse([message_start("claude-opus-5-5"), *TOOL_TURN]), seen))
    out, events = await collect(llm, ANTHROPIC_DIRECT["claude-opus-5-5"], effort="high")
    req = seen[0]
    assert req["url"].startswith("https://api.anthropic.com/v1/messages")
    body = req["body"]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "display": "updates"}
    assert body["output_config"] == {"effort": "high"} and body["fallbacks"] == "default"
    assert {"type": "web_search_20260209", "name": "web_search"} in body["tools"]
    assert body["tools"][0]["eager_input_streaming"] is True
    assert "thinking-display-updates-2026-08-18" in req["headers"]["anthropic-beta"]
    assert "budget_tokens" not in json.dumps(body) and "temperature" not in body

    assert out["stop_reason"] == "tool_use"
    assert out["content"][0] == {"type": "thinking", "thinking": "Checking the file.", "signature": "abc"}
    assert out["content"][1]["input"] == {"path": "a.txt"}
    assert out["usage"]["input_tokens"] == 150 and out["usage"]["output_tokens"] == 42
    assert out["usage"]["cost"] == pytest.approx((100 * 4 + 50 * 0.4 + 42 * 20) / 1e6)
    assert ("progress_delta", {"index": 0, "text": "Checking the file."}) in events


OR_STREAM = [
    message_start("openai/gpt-6.1-sol"),
    {"type": "content_block_start", "index": 0, "content_block": {
        "type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"kb\"}"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {
        "type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
        "content": [{"type": "web_search_result", "title": "RTINGS", "url": "https://rtings.com/x"}]}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "brand_new_block", "payload": 1}},
    {"type": "content_block_stop", "index": 2},
    {"type": "content_block_start", "index": 3, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 3, "delta": {"type": "text_delta", "text": "Done."}},
    {"type": "content_block_stop", "index": 3},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
    {"type": "message_stop"},
]


async def test_openrouter_request_and_parsing():
    seen = []
    gpt = ModelInfo("openai/gpt-6.1-sol", "openrouter", "GPT", 2, 10, 1_000_000, 128_000,
                    vision=False, reasoning=True, structured=True, tier="frontier")
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="or-key",
                      http_client=mock(sse(OR_STREAM, comments=True), seen))
    img_history = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": [
        {"type": "text", "text": "page"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}]}]}]
    out, events = await collect(llm, gpt, effort="medium", messages=img_history)
    req = seen[0]
    assert req["url"] == "https://openrouter.ai/api/v1/messages"
    assert req["headers"]["authorization"] == "Bearer or-key" and "x-api-key" not in req["headers"]
    assert req["headers"]["http-referer"].startswith("https://github.com/")
    assert "anthropic-beta" not in req["headers"]
    body = req["body"]
    assert body["model"] == "openai/gpt-6.1-sol" and body["session_id"] == "t_123"
    assert {"type": "openrouter:web_search", "parameters": {"max_results": 6}} in body["tools"]
    assert all("eager_input_streaming" not in t for t in body["tools"])
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "medium"} and "fallbacks" not in body
    # non-vision model: screenshot replaced with a note
    sent = body["messages"][0]["content"][0]["content"]
    assert sent[1] == {"type": "text", "text": "[image omitted: this model can't see images]"}

    kinds = [b["type"] for b in out["content"]]
    assert kinds == ["server_tool_use", "web_search_tool_result", "brand_new_block", "text"]
    assert out["content"][0]["input"] == {"query": "kb"} and out["content"][3]["text"] == "Done."
    assert ("sources", {"results": [{"title": "RTINGS", "url": "https://rtings.com/x"}]}) in events
    assert any(k == "server_tool_input" and d["input"] == {"query": "kb"} for k, d in events)


async def test_openrouter_non_reasoning_model_gets_no_thinking():
    seen = []
    llama = ModelInfo("meta-llama/llama-4-scout", "openrouter", "Llama", 0.1, 0.3, tier="fast")
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", http_client=mock(sse(OR_STREAM), seen))
    await collect(llm, llama)
    assert "thinking" not in seen[0]["body"] and "output_config" not in seen[0]["body"]


async def test_stream_error_event_raises():
    from conductor.llm import StreamError
    seen = []
    events = [message_start("x"), {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}]
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", http_client=mock(sse(events), seen))
    with pytest.raises(StreamError, match="busy"):
        await collect(llm, ModelInfo("a/b", "openrouter"))


async def test_structured_without_schema_support_parses_loose_json():
    seen = []
    events = [message_start("z-ai/glm"),
              {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
              {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "Sure! {\"category\": \"coding\"} hope that helps"}},
              {"type": "content_block_stop", "index": 0},
              {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
              {"type": "message_stop"}]
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", http_client=mock(sse(events), seen))
    out = await llm.structured(model=ModelInfo("z-ai/glm", "openrouter", structured=False),
                               system="s", prompt="p", schema={"type": "object"})
    assert out == {"category": "coding"}
    assert "output_config" not in seen[0]["body"] and "JSON" in seen[0]["body"]["messages"][0]["content"]
