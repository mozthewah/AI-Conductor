"""Fixes from a real session: hands-off composer, background servers, show_user, fetch visibility."""

import asyncio
import socket
import time

import pytest

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.llm import Accumulator
from conductor.tools import ToolContext, ToolError

from fakes import FakeLLM, text, tool_use
from test_composing import L, Scripted, make


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_composer_has_no_hands_but_specialists_do(tmp_path):
    def respond(system, messages, model):
        if "You are conducting" in system:
            return [tool_use("assign", parts=[{"id": "x", "title": "Do it", "skill": "coding", "domain": "general",
                                               "instructions": "do"}])] if len(messages) == 1 else [text("ok")]
        return [text("done")]

    c = make(tmp_path, respond, L("coding", "medium"))
    tid = c.start_task("build it")
    await c.wait(tid)
    composer = next(cl for cl in c.llm.calls if "You are conducting" in cl["system"])
    specialist = next(cl for cl in c.llm.calls if "on a team run by a composer" in cl["system"])
    for hands in ("run_shell", "write_file", "edit_file", "processes"):
        assert hands not in composer["tools"] and hands in specialist["tools"]
    assert "assign" in composer["tools"] and "show_user" in composer["tools"]
    assert "You don't have hands here" in composer["system"]


async def test_composer_trying_to_use_hands_is_refused(tmp_path):
    def respond(system, messages, model):
        if "You are conducting" in system and len(messages) == 1:
            return [tool_use("run_shell", command="ls")]
        return [text("ok")]

    c = make(tmp_path, respond, L("coding", "medium"))
    tid = c.start_task("x")
    await c.wait(tid)
    res = c.llm.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "Unknown tool" in res["content"]


def ctx_for(tmp_path):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, learning=False)
    c = Conductor(s, llm=FakeLLM([]))
    tid = c.store.create_task("t")
    return c, ToolContext(conductor=c, task_id=tid)


async def test_server_started_with_ampersand_no_longer_hangs(tmp_path):
    c, ctx = ctx_for(tmp_path)
    port = free_port()
    t0 = time.time()
    out = await c.tools["run_shell"].handler(ctx, {"command": f"(python3 -m http.server {port} --bind 127.0.0.1 >/dev/null 2>&1 &); echo started"})
    assert time.time() - t0 < 10 and "started" in out and "background processes running" in out
    assert len(c.processes) == 1
    await c.shutdown()


async def test_background_process_reports_url_and_can_be_stopped(tmp_path):
    c, ctx = ctx_for(tmp_path)
    port = free_port()
    out = await c.tools["run_shell"].handler(ctx, {"command": f"python3 -u -m http.server {port} --bind 127.0.0.1",
                                                   "background": True, "name": "file server"})
    pid = next(iter(c.processes))
    assert f"process {pid}" in out and f"127.0.0.1:{port}" in out
    listing = await c.tools["processes"].handler(ctx, {"action": "list"})
    assert "file server - running" in listing
    assert "Serving HTTP" in await c.tools["processes"].handler(ctx, {"action": "logs", "id": pid})
    assert "Stopped" in await c.tools["processes"].handler(ctx, {"action": "stop", "id": pid})
    assert not c.processes
    await c.shutdown()


async def test_timeout_stops_the_command_cleanly(tmp_path):
    c, ctx = ctx_for(tmp_path)
    with pytest.raises(ToolError, match="background: true"):
        await c.tools["run_shell"].handler(ctx, {"command": "echo begin; sleep 30", "timeout": 1})


async def test_show_user_links_workspace_files(tmp_path):
    c, ctx = ctx_for(tmp_path)
    img = tmp_path / "ws" / "mocks" / "a.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\\x89PNG")
    await c.tools["show_user"].handler(ctx, {"target": "mocks/a.png", "title": "Night Tape"})
    await c.tools["show_user"].handler(ctx, {"target": "http://127.0.0.1:41963/", "note": "pick one"})
    shows = [e["data"] for e in c.store.load_events(ctx.task_id) if e["type"] == "show"]
    assert shows[0]["image"] and shows[0]["href"] == "/api/files/raw?path=mocks/a.png"
    assert shows[1]["href"] == "http://127.0.0.1:41963/" and not shows[1]["image"]
    with pytest.raises(ToolError):
        await c.tools["show_user"].handler(ctx, {"target": "../../etc/passwd"})


async def test_fetch_results_are_reported():
    events = []

    async def on_event(k, d):
        events.append((k, d))
    acc = Accumulator(on_event)
    acc.blocks = {
        0: {"type": "server_tool_use", "id": "a", "name": "web_fetch", "input": {"url": "https://x"}},
        1: {"type": "web_fetch_tool_result", "tool_use_id": "a", "content": {"type": "web_fetch_tool_error", "error_code": "url_not_accessible"}},
        2: {"type": "server_tool_use", "id": "b", "name": "web_fetch", "input": {"url": "https://y"}},
        3: {"type": "web_fetch_tool_result", "tool_use_id": "b", "content": {"type": "web_fetch_result", "url": "https://y",
            "content": {"type": "document", "source": {"type": "text", "data": "hello world"}}}},
    }
    await acc.emit_extras()
    results = {d["id"]: d for k, d in events if k == "server_tool_result"}
    assert results["a"] == {"id": "a", "ok": False, "detail": "url_not_accessible"}
    assert results["b"]["ok"] and "characters" in results["b"]["detail"]


async def test_view_image_returns_the_image_for_vision_models(tmp_path):
    import base64
    c, ctx = ctx_for(tmp_path)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
    (tmp_path / "ws" / "mock.png").write_bytes(png)
    out = await c.tools["view_image"].handler(ctx, {"path": "mock.png"})
    assert out[1]["type"] == "image" and out[1]["source"]["media_type"] == "image/png"
    from conductor.models import ModelInfo
    c.catalog.models["x/blind"] = ModelInfo("x/blind", "openrouter", "Blind", vision=False)
    c.store.update_task(ctx.task_id, model="x/blind")
    with pytest.raises(ToolError, match="can't see images"):
        await c.tools["view_image"].handler(ctx, {"path": "mock.png"})
