import json

from fastapi.testclient import TestClient

from conductor.agent import Conductor
from conductor.audit import AuditLog, scrub
from conductor.config import Settings
from conductor.server import create_app

from fakes import FakeLLM, text, tool_use


def settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                    fetch_catalog=False, **kw)


def entries(c: Conductor) -> list[dict]:
    return c.audit.read()


def test_scrub_redacts_secrets_and_trims():
    s = scrub({"cmd": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuv' x",
               "k": "OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef0123", "long": "x" * 1000})
    assert "abcdefghijklmnopqrstuv" not in s["cmd"]
    assert "0123456789abcdef0123" not in s["k"]
    assert "[redacted]" in s["k"]
    assert len(s["long"]) < 400 and "1000 chars" in s["long"]


def test_disabled_writes_nothing(tmp_path):
    log = AuditLog(tmp_path / "logs", enabled=False)
    log.write("x")
    assert not (tmp_path / "logs").exists()


async def test_task_activity_is_audited(tmp_path):
    llm = FakeLLM([[tool_use("write_file", path="a.txt", content="hi")], [text("done")]])
    c = Conductor(settings(tmp_path), llm=llm)
    tid = c.start_task("make a file")
    await c.wait(tid)
    rows = entries(c)
    kinds = [r["kind"] for r in rows]
    for k in ("task.message", "route.decided", "model.call", "tool.call", "tool.result", "file.changed", "task.done"):
        assert k in kinds, k
    assert all(r["ts"] and r["task"] == tid for r in rows if r["kind"].startswith(("tool.", "task.")))
    user = next(r for r in rows if r["kind"] == "task.message")
    assert user["actor"] == "user"
    result = next(r for r in rows if r["kind"] == "tool.result")
    assert "ms" in result
    # the file on disk is the real one; the log is one JSON object per line
    raw = next((tmp_path / "d" / "logs").glob("audit-*.jsonl")).read_text().splitlines()
    assert all(json.loads(line) for line in raw)


async def test_audit_off(tmp_path):
    c = Conductor(settings(tmp_path, audit=False), llm=FakeLLM([[text("ok")]]))
    await c.wait(c.start_task("hi"))
    assert not list((tmp_path / "d" / "logs").glob("*.jsonl")) if (tmp_path / "d" / "logs").exists() else True


def test_http_requests_and_ui_events_are_logged(tmp_path):
    c = Conductor(settings(tmp_path), llm=FakeLLM([[text("ok")]]))
    with TestClient(create_app(c, run_scheduler=False)) as client:
        r = client.post("/api/memories", json={"content": "my secret plans for the weekend"})
        assert r.status_code == 200, r.text
        # the endpoint still got the full body after the middleware read it
        assert any("weekend" in m["content"] for m in c.store.list_memories())
        client.get("/api/tasks")  # routine polling: not logged
        client.post("/api/audit/ui", json={"events": [
            {"action": "click", "label": "Upgrade", "element": "button", "path": "/t/abc", "t": "2026-11-06T10:00:00Z"},
            {"action": "view", "label": "/models", "path": "/models"}]})
    rows = entries(c)
    kinds = [r["kind"] for r in rows]
    assert "system.start" in kinds and "system.stop" in kinds
    http = [r for r in rows if r["kind"] == "http.request"]
    mem = next(r for r in http if r["path"] == "/api/memories")
    assert mem["body"]["content"] == "<31 chars>"  # that it happened, not what it said
    assert not any(r["path"] == "/api/tasks" for r in http)
    click = next(r for r in rows if r["kind"] == "ui.click")
    assert click["label"] == "Upgrade" and click["actor"] == "user" and click["page"] == "/t/abc"
    assert any(r["kind"] == "ui.view" for r in rows)
