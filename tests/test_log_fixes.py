"""Fixes from reading a real audit log: provider failures, false 'stuck', supervisor context."""

import asyncio

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.llm import StreamError
from conductor.models import ModelInfo

from fakes import FakeLLM, text, tool_use

A = ModelInfo("lab/a", "openrouter", "A", 4, 20, tier="frontier")
B = ModelInfo("lab/b", "openrouter", "B", 2, 10, tier="strong")


def L(skill="coding", difficulty="hard"):
    return {"skill": skill, "domain": "general", "difficulty": difficulty, "needs_vision": False,
            "parts": [{"skill": skill, "domain": "general"}], "summary": ""}


def make(tmp_path, llm):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", compose="off", learning=False)
    c = Conductor(s, llm=llm)
    c.catalog.models = {A.id: A, B.id: B}
    c.catalog.set_roster([A.id, B.id])
    return c


class UpstreamDown(FakeLLM):
    """Model A's provider fails every time (after the LLM layer's own retries)."""

    async def turn(self, **kw):
        if kw["model"].id == A.id:
            self.calls.append({"model": A.id, "failed": True})
            raise StreamError("upstream returned an invalid response", "api_error")
        return await super().turn(**kw)


async def test_provider_failure_switches_model_instead_of_failing(tmp_path):
    c = make(tmp_path, UpstreamDown([[text("done by B")]], label=L()))
    tid = c.start_task("build it")
    await asyncio.wait_for(c.wait(tid), 5)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["answer"] == "done by B" and t["model"] == B.id
    esc = [e["data"] for e in c.store.load_events(tid) if e["type"] == "escalated"]
    assert esc[-1]["direction"] == "sideways" and "upstream" in esc[-1]["reason"]


async def test_rereading_after_a_change_is_not_stuck(tmp_path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "a.txt").write_text("x")
    script = []
    for i in range(3):  # edit, then re-read the same file: three times
        script.append([tool_use("write_file", path="a.txt", content=str(i)), tool_use("read_file", path="a.txt")])
    script.append([text("ok")])
    c = make(tmp_path, FakeLLM(script, label=L()))
    tid = c.start_task("iterate")
    await asyncio.wait_for(c.wait(tid), 5)
    assert not any(e["type"] == "status" and "Stuck" in e["data"].get("text", "") for e in c.store.load_events(tid))


async def test_reading_the_same_unchanged_file_three_times_is_stuck(tmp_path):
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "a.txt").write_text("x")
    script = [[tool_use("read_file", path="a.txt")] for _ in range(3)] + [[text("ok")]]
    c = make(tmp_path, FakeLLM(script, label=L()))
    tid = c.start_task("loop")
    await asyncio.wait_for(c.wait(tid), 5)
    assert any(e["type"] == "status" and "Stuck" in e["data"].get("text", "") for e in c.store.load_events(tid))


async def test_supervisor_sees_its_own_processes_and_files(tmp_path):
    seen = {}

    class Spy(FakeLLM):
        async def structured(self, *, system, prompt, schema, effort="low", model=None):
            if "supervise" in system:
                seen["prompt"], seen["system"] = prompt, system
                return {"on_track": True, "issue": "", "steer": ""}
            return await super().structured(model=model, system=system, prompt=prompt, schema=schema, effort=effort)

    c = make(tmp_path, Spy([], label=L()))
    tid = c.store.create_task("part")
    await c.emit(tid, "process", {"action": "started", "id": 4242, "name": "burrow-admin-8032"})
    await c.emit(tid, "artifact", {"path": "x/capture-admin.py", "action": "created"})
    await c._supervise(tid, [{"role": "user", "content": "Do the admin pages"}], [])
    assert "4242 (burrow-admin-8032)" in seen["prompt"] and "capture-admin.py" in seen["prompt"]
    assert "helper scripts" in seen["system"]


# ---------------------------------------------------------------- second log
def test_safety_ratings_from_the_log(tmp_path):
    from conductor.safety import assess_shell
    rate = lambda c: assess_shell(c, tmp_path).level
    assert rate("cat a; sed -n 25,40p b.html; grep -n x c.go") == "safe"   # was "It edits text."
    assert rate("sed -i 's/a/b/' x.html") == "low"
    assert rate("ps aux | grep burrow || true") == "safe"                 # was "It no-op."
    assert rate("sleep 1; curl -sI http://127.0.0.1:8041/setup") == "safe"  # was "makes a network request"
    assert rate("curl -X POST http://localhost:8000/api -d x") == "low"
    assert rate("curl -s https://example.com") == "medium"
    assert rate("curl -X POST https://example.com -d x") == "high"
    assert rate("curl -s http://127.0.0.1:1/ https://example.com") == "medium"


def test_low_credits_cap_the_reply_reservation():
    from conductor.llm import LOW_CREDIT_MAX_TOKENS, MessagesLLM
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", max_tokens=64000)
    m = ModelInfo("a/b", "openrouter", max_output=64000)
    assert llm._request(m, "s", [{"role": "user", "content": "hi"}], [], "high", False, None)["max_tokens"] == 64000
    llm.credit_low = lambda: True
    assert llm._request(m, "s", [{"role": "user", "content": "hi"}], [], "high", False, None)["max_tokens"] == LOW_CREDIT_MAX_TOKENS


async def test_in_flight_retry_reserves_less(monkeypatch):
    from test_credits import err, scripted_http
    from test_llm_wire import collect
    from conductor.llm import MessagesLLM

    async def no_sleep(s):
        pass
    monkeypatch.setattr("conductor.llm.asyncio.sleep", no_sleep)
    seen = []
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", http_client=scripted_http(
        [(402, err("in_flight_budget_exhausted", "openrouter_in_flight_budget"), {"retry-after": "90"}), (200, None, None)], seen))
    await collect(llm, ModelInfo("a/b", "openrouter", max_output=64000))
    assert seen[0]["max_tokens"] == 64000 and seen[1]["max_tokens"] == 32000


async def test_side_calls_are_logged_against_their_task(tmp_path):
    c = make(tmp_path, FakeLLM([[tool_use("run_shell", command="frobnicate --all")], [text("ok")]], label=L()))
    c.settings.approval = "high"
    tid = c.start_task("go")
    await asyncio.wait_for(c.wait(tid), 5)
    from conductor.llm import CURRENT_TASK
    assert CURRENT_TASK.get() is None  # nothing leaks out of the run
    assert c.store.get_task(tid)["status"] == "done"


def test_favicon_needs_no_sign_in(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from conductor.server import create_app
    monkeypatch.delenv("CONDUCTOR_ACCESS_TOKEN", raising=False)
    c = make(tmp_path, FakeLLM([]))
    with TestClient(create_app(c, run_scheduler=False), client=("192.168.1.9", 1), follow_redirects=False) as phone:
        r = phone.get("/favicon.ico")
        assert r.status_code == 200 and "svg" in r.headers["content-type"]
    assert not any(e["kind"] == "auth.sign_in_needed" for e in c.audit.read())
