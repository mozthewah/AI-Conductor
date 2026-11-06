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
