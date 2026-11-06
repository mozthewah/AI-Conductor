"""Exercise ClaudeLLM against a mocked HTTP endpoint: checks the request we send
and that we parse a real-shaped SSE stream (progress updates, tool calls)."""

import json

import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from conductor.llm import ClaudeLLM


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = [
    {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 50,
                  "cache_creation_input_tokens": 0}}},
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


async def test_request_shape_and_stream_parsing():
    seen = {}

    def handler(request: httpx2.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(STREAM))

    llm = ClaudeLLM("claude-opus-5-5", effort="high")
    llm.client = AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(
        transport=httpx2.MockTransport(handler)))

    events = []

    async def on_event(kind, data):
        events.append((kind, data))

    out = await llm.turn(system="sys", messages=[{"role": "user", "content": "hi"}],
                         tools=[{"name": "read_file", "description": "d", "eager_input_streaming": True,
                                 "input_schema": {"type": "object", "properties": {}}}],
                         on_event=on_event)

    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive", "display": "updates"}
    assert body["output_config"] == {"effort": "high"}
    assert body["fallbacks"] == "default"
    assert body["stream"] is True
    assert "thinking-display-updates-2026-08-18" in seen["headers"]["anthropic-beta"]
    assert "server-side-fallback-2026-07-01" in seen["headers"]["anthropic-beta"]
    assert "budget_tokens" not in json.dumps(body) and "temperature" not in body

    assert out["stop_reason"] == "tool_use"
    assert out["content"][0] == {"type": "thinking", "thinking": "Checking the file.", "signature": "abc"}
    assert out["content"][1]["input"] == {"path": "a.txt"}
    assert out["usage"] == {"input_tokens": 150, "output_tokens": 42}
    kinds = [k for k, _ in events]
    assert ("progress_delta", {"index": 0, "text": "Checking the file."}) in events
    assert "tool_start" in kinds
