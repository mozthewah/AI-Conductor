import asyncio
import time

import pytest

from conductor.agent import Conductor
from conductor.config import Settings
from conductor import scheduler

from fakes import FakeLLM, progress, text, tool_use


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", workspace=tmp_path / "ws", approval="auto", browser=False)


async def run_to_end(c: Conductor, prompt: str) -> str:
    tid = c.start_task(prompt)
    await c.wait(tid)
    return tid


async def test_simple_answer(settings):
    llm = FakeLLM([[text("Hello there!")]])
    c = Conductor(settings, llm=llm)
    tid = await run_to_end(c, "hi")
    task = c.store.get_task(tid)
    assert task["status"] == "done"
    assert task["answer"] == "Hello there!"
    types = [e["type"] for e in c.store.load_events(tid)]
    assert types[:3] == ["user", "assistant_text", "done"]
    assert "learned" in types  # reflection ran
    assert c.store.list_memories(kind="episode")


async def test_tool_loop_files_plan_and_parallel_results(settings):
    llm = FakeLLM([
        [progress("Planning it out."),
         tool_use("update_plan", steps=[{"step": "Write file", "status": "in_progress"}]),
         tool_use("write_file", path="out/report.md", content="# Report\nok")],
        [tool_use("read_file", path="out/report.md")],
        [text("Done - see out/report.md")],
    ])
    c = Conductor(settings, llm=llm)
    tid = await run_to_end(c, "write a report")
    assert (settings.workspace / "out/report.md").read_text() == "# Report\nok"
    assert c.store.get_task(tid)["plan"][0]["step"] == "Write file"
    # both tool results of turn 1 go back in ONE user message
    second_call = llm.calls[1]["messages"]
    results = second_call[-1]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    third = llm.calls[2]["messages"][-1]["content"][0]
    assert "# Report" in third["content"]
    ev = c.store.load_events(tid)
    assert any(e["type"] == "progress" and e["data"]["text"] == "Planning it out." for e in ev)
    assert any(e["type"] == "artifact" for e in ev)


async def test_workspace_escape_is_blocked(settings):
    llm = FakeLLM([[tool_use("write_file", path="../../etc/evil", content="x")], [text("ok")]])
    c = Conductor(settings, llm=llm)
    await run_to_end(c, "x")
    res = llm.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "outside the workspace" in res["content"]


async def test_invalid_tool_input_returns_error(settings):
    llm = FakeLLM([[tool_use("write_file", path="a.txt")], [text("ok")]])
    c = Conductor(settings, llm=llm)
    await run_to_end(c, "x")
    res = llm.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "content" in res["content"]


async def test_memory_is_injected_into_next_task(settings):
    llm = FakeLLM([
        [tool_use("remember", kind="preference", content="Prefers metric units")],
        [text("Noted.")],
        [text("Sure.")],
    ])
    c = Conductor(settings, llm=llm)
    await run_to_end(c, "I like metric")
    await run_to_end(c, "what's the weather")
    assert "Prefers metric units" in llm.calls[2]["system"]


async def test_reflection_saves_skill_and_memory(settings):
    llm = FakeLLM([[text("All done.")]], reflection={
        "summary": "Built a CSV cleaner.", "memories": [{"kind": "project", "content": "Runs a bakery called Rye"}],
        "save_skill": True, "skill_name": "Clean CSV", "skill_description": "Clean messy CSVs",
        "skill_instructions": "1. Load with pandas\n2. Strip whitespace"})
    c = Conductor(settings, llm=llm)
    tid = await run_to_end(c, "clean my csv")
    assert c.skills.get("clean-csv").description == "Clean messy CSVs"
    assert any("bakery" in m["content"] for m in c.store.list_memories())
    # skill now advertised in the next task's system prompt
    assert "clean-csv: Clean messy CSVs" in c.build_system_prompt("anything")
    learned = [e for e in c.store.load_events(tid) if e["type"] == "learned"][0]
    assert learned["data"]["skill"]["name"] == "clean-csv"


async def test_use_skill_and_followup_message(settings):
    llm = FakeLLM([
        [tool_use("save_skill", name="greet", description="Say hi", instructions="Wave.")],
        [text("Saved.")],
        [tool_use("use_skill", name="greet")],
        [text("Waving.")],
    ])
    c = Conductor(settings, llm=llm)
    tid = await run_to_end(c, "learn to greet")
    c.send_message(tid, "now greet")
    await c.wait(tid)
    res = llm.calls[3]["messages"][-1]["content"][0]
    assert "Wave." in res["content"]
    # follow-up continues the same append-only transcript
    assert len(llm.calls[2]["messages"]) == 5
    assert c.skills.get("greet").uses == 1


async def test_subagents_run_in_parallel(settings):
    def lead(_):
        return [tool_use("delegate", tasks=[{"title": "A", "instructions": "do A"},
                                              {"title": "B", "instructions": "do B"}])]
    sub = lambda msgs: [text(f"report for {msgs[0]['content'][-1]['text']}")]
    llm = FakeLLM([lead, sub, sub, [text("Combined.")]])
    c = Conductor(settings, llm=llm)
    tid = await run_to_end(c, "research")
    kids = c.store.child_tasks(tid)
    assert {k["title"] for k in kids} == {"A", "B"} and all(k["status"] == "done" for k in kids)
    # sub-agents don't get delegate / schedule tools, and run at sub-agent effort
    assert "delegate" not in llm.calls[1]["tools"] and llm.calls[1]["effort"] == "medium"
    final_input = llm.calls[3]["messages"][-1]["content"][0]["content"]
    assert "report for do A" in final_input and "report for do B" in final_input


async def test_approval_flow_denied(settings):
    settings.approval = "ask"
    llm = FakeLLM([[tool_use("run_shell", command="echo hi")], [text("ok")]])
    c = Conductor(settings, llm=llm)
    tid = c.start_task("run it")
    q = c.subscribe(tid)
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "request":
            assert c.store.get_task(tid)["status"] == "waiting"
            c.resolve_request(ev["data"]["id"], approved=False)
            break
    await c.wait(tid)
    res = llm.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "declined" in res["content"]


async def test_shell_runs_when_auto(settings):
    llm = FakeLLM([[tool_use("run_shell", command="echo $((6*7))")], [text("ok")]])
    c = Conductor(settings, llm=llm)
    await run_to_end(c, "math")
    assert "42" in llm.calls[1]["messages"][-1]["content"][0]["content"]


async def test_cancel_then_followup_closes_dangling_tool_calls(settings):
    gate = asyncio.Event()

    async def slow(ctx, args):
        await gate.wait()
        return "never"

    llm = FakeLLM([[tool_use("list_files")], [text("resumed")]])
    c = Conductor(settings, llm=llm)
    c.tools["list_files"].handler = slow
    tid = c.start_task("x")
    await asyncio.sleep(0.1)
    c.cancel(tid)
    await c.wait(tid)
    assert c.store.get_task(tid)["status"] == "cancelled"
    c.send_message(tid, "try again")
    await c.wait(tid)
    last_user = llm.calls[1]["messages"][-1]["content"]
    assert last_user[0]["type"] == "tool_result" and last_user[0]["is_error"]
    assert last_user[1] == {"type": "text", "text": "try again"}


async def test_scheduler_fires_due_tasks(settings):
    llm = FakeLLM([[text("Briefing.")]])
    c = Conductor(settings, llm=llm)
    sid = c.store.add_schedule("morning briefing", time.time() - 1, 1440)
    started = await scheduler.tick(c)
    assert len(started) == 1
    await c.wait(started[0])
    assert c.store.get_task(started[0])["origin"] == "schedule"
    nxt = [s for s in c.store.list_schedules() if s["id"] == sid][0]["next_run"]
    assert nxt > time.time() + 1400 * 60


async def test_unattended_tasks_dont_block_on_questions(settings):
    settings.approval = "ask"
    llm = FakeLLM([[tool_use("run_shell", command="echo hi")], [text("skipped")]])
    c = Conductor(settings, llm=llm)
    tid = c.start_task("x", origin="schedule")
    await asyncio.wait_for(c.wait(tid), 5)
    assert llm.calls[1]["messages"][-1]["content"][0]["is_error"]


async def test_refusal_and_api_error(settings):
    llm = FakeLLM([{"content": [], "stop_reason": "refusal"}])
    c = Conductor(settings, llm=llm)
    tid = await run_to_end(c, "x")
    assert c.store.get_task(tid)["status"] == "done"

    class Boom(FakeLLM):
        async def turn(self, **kw):
            raise RuntimeError("kaboom")
    c2 = Conductor(settings, llm=Boom([]))
    tid2 = await run_to_end(c2, "x")
    assert c2.store.get_task(tid2)["status"] == "error"
