"""Your own model servers: Ollama (native /api/chat), OpenAI-compatible (/v1/chat/completions) and
Anthropic-compatible (/v1/messages), discovered and spoken to over real HTTP against a fake server."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from conductor import local
from conductor.agent import Conductor
from conductor.config import Settings
from conductor.credits import CreditError
from conductor.llm import MessagesLLM
from conductor.models import Catalog, ModelInfo
from conductor.server import create_app

from fakes import FakeLLM, text, tool_use

TOOLS = [{"name": "list_files", "description": "List files", "eager_input_streaming": True,
          "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}]


class Fake(BaseHTTPRequestHandler):
    kind = "ollama"
    seen: list = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, chunks, sse=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/x-ndjson")
        self.end_headers()
        for c in chunks:
            line = (f"data: {json.dumps(c)}\n\n" if sse else json.dumps(c) + "\n") if not isinstance(c, str) else c
            self.wfile.write(line.encode())
            self.wfile.flush()

    def do_GET(self):
        k = type(self).kind
        if self.path == "/api/tags" and k == "ollama":
            return self._json({"models": [{"model": "qwen3:8b", "details": {"parameter_size": "8.2B"}},
                                          {"model": "nomic-embed-text:latest"},
                                          {"model": "tiny:1b", "details": {"parameter_size": "1B"}}]})
        if self.path == "/v1/models" and k == "openai":
            return self._json({"object": "list", "data": [{"id": "llama-3.3-8b", "object": "model",
                                                           "max_model_len": 65536}]})
        if self.path == "/v1/models" and k == "anthropic":
            return self._json({"data": [{"type": "model", "id": "gemma-4-e4b", "display_name": "Gemma"}]})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).seen.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        if self.path == "/api/show":
            if body["model"].startswith("tiny"):
                return self._json({"capabilities": ["completion"], "model_info": {"llama.context_length": 8192}})
            return self._json({"capabilities": ["completion", "tools", "thinking"],
                               "model_info": {"qwen3.context_length": 40960}})
        if self.path == "/api/chat":
            if not body.get("stream", True):
                return self._json({"message": {"role": "assistant", "content": '<think>hm</think>{"skill": "chat"}'},
                                   "done": True})
            if body["messages"][-1]["role"] == "tool" or not body.get("tools"):
                return self._stream([{"message": {"content": "", "thinking": "checking"}, "done": False},
                                     {"message": {"content": "All "}, "done": False},
                                     {"message": {"content": "done."}, "done": False},
                                     {"message": {"content": ""}, "done": True, "done_reason": "stop",
                                      "prompt_eval_count": 120, "eval_count": 7}], sse=False)
            return self._stream([{"message": {"content": "Looking.", "tool_calls": [
                {"function": {"name": "list_files", "arguments": {"path": "."}}}]}, "done": False},
                {"done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 9}], sse=False)
        if self.path == "/v1/chat/completions":
            if any(x["role"] == "tool" for x in body["messages"]):
                return self._stream([{"choices": [{"delta": {"content": "Fin"}}]},
                                     {"choices": [{"delta": {"content": "ished."}, "finish_reason": "stop"}]},
                                     {"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 3}},
                                     "data: [DONE]\n\n"])
            return self._stream([
                {"choices": [{"delta": {"role": "assistant", "content": "<think>plan</think>Sure"}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {
                    "name": "list_files", "arguments": '{"pa'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "src"}'}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                "data: [DONE]\n\n"])
        if self.path == "/v1/messages":
            return self._stream([
                {"type": "message_start", "message": {"model": "gemma-4-e4b", "usage": {"input_tokens": 30}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi from Gemma"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
                {"type": "message_stop"}])
        self._json({"error": {"message": "nope"}}, 404)


@pytest.fixture
def server():
    started = []

    def start(kind):
        handler = type(f"Fake_{kind}", (Fake,), {"kind": kind, "seen": []})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_port}", handler

    yield start
    for s in started:
        s.shutdown()


async def collect(llm, model, messages, tools=TOOLS):
    events = []

    async def on_event(k, d):
        events.append((k, d))

    r = await llm.turn(model=model, system="sys", messages=messages, tools=tools, on_event=on_event)
    return r, events


def test_normalize_and_parse():
    assert local.normalize("http://h:1234/v1/chat/completions") == "http://h:1234"
    assert local.normalize("localhost:11434/") == "http://localhost:11434"
    assert local.normalize("http://h:8000/v1/messages") == "http://h:8000"
    eps = local.parse_endpoints("http://a:11434, openai=http://b:1234/v1 anthropic=http://c:18000", key="k")
    assert [(e.api, e.root) for e in eps] == [("auto", "http://a:11434"), ("openai", "http://b:1234"),
                                              ("anthropic", "http://c:18000")]
    assert all(e.key == "k" for e in eps)


def test_discovery_detects_each_api(tmp_path, server):
    o, _ = server("ollama")
    a, _ = server("openai")
    n, _ = server("anthropic")
    cat = Catalog(tmp_path, has_openrouter=False, has_anthropic=False, fetch=False, local_url=f"{o},{a},{n}").load()
    assert {st["api"] for st in cat.local_status} == {"ollama", "openai", "anthropic"}
    q = cat.models["local/qwen3:8b"]
    assert q.provider == "local" and q.api == "ollama" and q.tools and q.reasoning and q.context == 32768
    assert "local/nomic-embed-text:latest" not in cat.models  # embedding models aren't chat models
    assert not cat.models["local/tiny:1b"].tools  # no tool calling: not in the roster
    assert cat.models["local/llama-3.3-8b"].context == 65536 and cat.models["local/llama-3.3-8b"].api == "openai"
    assert cat.models["local/gemma-4-e4b"].api == "anthropic"
    roster = [m.id for m in cat.roster()]  # no cloud keys: your own models are the roster
    assert "local/qwen3:8b" in roster and "local/tiny:1b" not in roster
    assert "api_key" not in q.to_dict() and q.to_dict()["is_local"]


def test_cloud_keys_keep_local_models_opt_in(tmp_path, server):
    o, _ = server("ollama")
    cat = Catalog(tmp_path, has_openrouter=False, has_anthropic=True, fetch=False, local_url=o).load()
    assert "local/qwen3:8b" in cat.models and all(m.provider == "anthropic" for m in cat.roster())
    cat.add("local/qwen3:8b")
    assert "local/qwen3:8b" in [m.id for m in cat.roster()]


def test_down_server_and_named_models(tmp_path):
    cat = Catalog(tmp_path, has_openrouter=False, has_anthropic=False, fetch=False,
                  local_url="openai=http://127.0.0.1:9", local_models=["my-model"]).load()
    assert cat.models["local/my-model"].api == "openai" and cat.local_status[0]["error"]
    assert cat.get("local/gone").provider == "local"


async def test_ollama_turn_tools_and_context(tmp_path, server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    o, h = server("ollama")
    cat = Catalog(tmp_path, has_openrouter=False, has_anthropic=False, fetch=False, local_url=o, local_context=16384).load()
    llm = MessagesLLM(anthropic_enabled=False)
    m = cat.models["local/qwen3:8b"]
    r, events = await collect(llm, m, [{"role": "user", "content": "what's here?"}])
    assert r["stop_reason"] == "tool_use" and r["usage"]["cost"] == 0.0
    use = [b for b in r["content"] if b["type"] == "tool_use"][0]
    assert use["name"] == "list_files" and use["input"] == {"path": "."}
    sent = h.seen[-1]
    assert sent["body"]["options"]["num_ctx"] == 16384 and sent["body"]["think"] is True
    assert sent["body"]["tools"][0]["function"]["name"] == "list_files"
    assert "sk-ant-secret" not in json.dumps(sent["headers"])  # never your cloud credentials
    msgs = [{"role": "user", "content": "what's here?"}, {"role": "assistant", "content": r["content"], "model": m.id},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "a.py"}]}]
    r2, events = await collect(llm, m, msgs)
    assert r2["content"] == [{"type": "text", "text": "All done."}] and r2["usage"]["input_tokens"] == 120
    sent = h.seen[-1]["body"]["messages"]
    assert sent[-1] == {"role": "tool", "content": "a.py", "tool_name": "list_files"}
    assert sent[-2]["tool_calls"][0]["function"]["arguments"] == {"path": "."}
    assert ("progress_delta", {"index": 0, "text": "checking"}) in events


async def test_openai_turn_streams_split_tool_args(tmp_path, server):
    a, h = server("openai")
    cat = Catalog(tmp_path, has_openrouter=False, has_anthropic=False, fetch=False, local_url=a, local_key="lk").load()
    llm = MessagesLLM(anthropic_enabled=False)
    m = cat.models["local/llama-3.3-8b"]
    r, events = await collect(llm, m, [{"role": "user", "content": [{"type": "text", "text": "go"}]}])
    assert r["content"][0] == {"type": "text", "text": "Sure"}  # <think> went to progress, not the answer
    assert r["content"][1]["input"] == {"path": "src"} and r["content"][1]["id"] == "call_a"
    assert any(k == "progress_delta" and d["text"] == "plan" for k, d in events)
    assert h.seen[-1]["headers"]["Authorization"] == "Bearer lk"
    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": r["content"], "model": m.id},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_a", "content": [
                {"type": "text", "text": "x.py"}]}, {"type": "text", "text": "and then?"}]}]
    r2, _ = await collect(llm, m, msgs)
    assert r2["content"][0]["text"] == "Finished." and r2["usage"]["output_tokens"] == 3
    sent = h.seen[-1]["body"]["messages"]
    assert [x["role"] for x in sent] == ["system", "user", "assistant", "tool", "user"]
    assert sent[3] == {"role": "tool", "tool_call_id": "call_a", "content": "x.py"}


async def test_anthropic_style_server(tmp_path, server, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    n, h = server("anthropic")
    cat = Catalog(tmp_path, has_openrouter=False, has_anthropic=False, fetch=False, local_url=n).load()
    llm = MessagesLLM(anthropic_enabled=False)
    r, _ = await collect(llm, cat.models["local/gemma-4-e4b"], [{"role": "user", "content": "hi"}])
    assert r["content"] == [{"type": "text", "text": "Hi from Gemma"}]
    sent = h.seen[-1]
    assert set(sent["body"]) <= {"model", "max_tokens", "stream", "messages", "system", "tools"}  # core fields only
    assert "eager_input_streaming" not in json.dumps(sent["body"]["tools"])
    assert "sk-ant-secret" not in json.dumps(sent["headers"])


async def test_structured_and_quick_on_local(tmp_path, server):
    o, h = server("ollama")
    cat = Catalog(tmp_path, has_openrouter=False, has_anthropic=False, fetch=False, local_url=o).load()
    llm = MessagesLLM(anthropic_enabled=False)
    out = await llm.structured(model=cat.models["local/qwen3:8b"], system="s", prompt="p",
                               schema={"type": "object", "properties": {"skill": {"type": "string"}}})
    assert out == {"skill": "chat"} and h.seen[-1]["body"]["format"]["type"] == "object"
    assert await llm.quick(model=cat.models["local/qwen3:8b"], system="s", prompt="p") == '{"skill": "chat"}'


async def test_local_only_conductor_runs_a_task(tmp_path, server):
    o, _ = server("ollama")
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, compose="off", learning=False, local_url=o)
    c = Conductor(s)
    assert s.local_default == "local/qwen3:8b" and s.fallback_model == "local/qwen3:8b"
    assert s.default_router_model == "local/qwen3:8b"
    tid = c.start_task("say something")
    await asyncio.wait_for(c.wait(tid), 20)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["model"] == "local/qwen3:8b"


class OutOfCredits(FakeLLM):
    async def turn(self, **kw):
        self.calls.append({"model": kw["model"].id})
        if kw["model"].provider == "local":
            return {"content": [text("Done locally.")], "stop_reason": "end_turn", "usage": {}}
        raise CreditError("out", "Insufficient credits")


async def test_out_of_credits_carries_on_locally(tmp_path, server):
    o, _ = server("ollama")
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", compose="off", learning=False, local_url=o)
    c = Conductor(s, llm=OutOfCredits([], label={"skill": "coding", "domain": "general", "difficulty": "medium",
                                                 "needs_vision": False, "parts": [], "summary": ""}))
    gpt = ModelInfo("z-ai/glm-5.3", "openrouter", "GLM", 1, 2)
    c.catalog.models[gpt.id] = gpt
    c.catalog.set_roster([gpt.id])
    tid = c.start_task("build it")
    await asyncio.wait_for(c.wait(tid), 20)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["answer"] == "Done locally." and t["model"] == "local/qwen3:8b"
    esc = [e["data"] for e in c.store.load_events(tid) if e["type"] == "escalated"][-1]
    assert esc["by"] == "local" and "own model server" in esc["reason"]


def test_models_page_and_setup_probe(tmp_path, server):
    o, _ = server("ollama")
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", browser=False, fetch_catalog=False,
                 anthropic_key=True, learning=False, local_url=o)
    c = Conductor(s, llm=FakeLLM([]))
    with TestClient(create_app(c, run_scheduler=False)) as client:
        data = client.get("/api/models").json()["local"]
        assert data["configured"] and "local/qwen3:8b" in [m["id"] for m in data["available"]]
        assert all("api_key" not in m for m in data["available"])
        assert client.post("/api/models/local/refresh").json()["servers"][0]["api"] == "ollama"
        r = client.post("/api/setup/local/probe", json={"url": o}).json()
        assert r["result"]["api"] == "ollama" and "qwen3:8b" in r["result"]["models"]


def test_copilot_never_takes_local_models():
    from conductor.copilot_cli import serves
    assert not serves("local/gpt-oss:20b") and serves("openai/gpt-6.1-sol")
