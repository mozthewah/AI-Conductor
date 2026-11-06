"""Sending messages while a task is running: they join the run instead of cancelling it."""

import asyncio

from conductor.agent import Conductor
from conductor.config import Settings

from fakes import FakeLLM, text, tool_use


def make(tmp_path, script, approval="high", learning=False):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval=approval, browser=False,
                 fetch_catalog=False, learning=learning, compose="off")
    return Conductor(s, llm=FakeLLM(script))


def texts(msg):
    return " ".join(b.get("text", "") for b in msg["content"] if isinstance(b, dict) and b.get("type") == "text")


async def test_message_mid_run_reaches_the_next_step(tmp_path):
    holder = {}

    def step1(messages):
        assert holder["c"].send_message(holder["tid"], "use Postgres instead") == "queued"
        return [tool_use("list_files", path=".")]

    c = make(tmp_path, [step1, [text("Switched to Postgres.")]])
    holder["c"] = c
    holder["tid"] = tid = c.start_task("set up the database")
    await c.wait(tid)
    delivered = c.llm.calls[1]["messages"][-1]
    assert delivered["content"][0]["type"] == "tool_result" and "use Postgres instead" in texts(delivered)
    assert c.store.get_task(tid)["status"] == "done"
    types = [e["type"] for e in c.store.load_events(tid)]
    assert types.count("user") == 2 and "user_delivered" in types


async def test_message_just_before_finishing_keeps_it_going(tmp_path):
    holder = {}

    def final(messages):
        holder["c"].send_message(holder["tid"], "also add a README")
        return [text("All done.")]

    c = make(tmp_path, [final, [text("Added the README too.")]])
    holder["c"] = c
    holder["tid"] = tid = c.start_task("make the project")
    await c.wait(tid)
    assert len(c.llm.calls) == 2 and "also add a README" in texts(c.llm.calls[1]["messages"][-1])
    assert c.store.get_task(tid)["answer"] == "Added the README too."


async def test_message_during_wrap_up_is_not_lost(tmp_path):
    c = make(tmp_path, [[text("Done.")], [text("Handled the late note.")]], learning=True)
    original = c.reflect

    async def slow_reflect(task_id):
        c.send_message(task_id, "one more thing")  # arrives while it's reflecting
        return await original(task_id)

    c.reflect = slow_reflect
    tid = c.start_task("x")
    await c.wait(tid)
    assert "one more thing" in texts(c.llm.calls[1]["messages"][-1])
    assert c.store.get_task(tid)["answer"] == "Handled the late note."


async def test_typing_while_it_asks_a_question_answers_it(tmp_path):
    c = make(tmp_path, [[tool_use("ask_user", question="Which database?")], [text("Using MySQL.")]])
    tid = c.start_task("set up a db")
    q = c.subscribe(tid)
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "request":
            assert c.send_message(tid, "MySQL please") == "answered"
            break
    await c.wait(tid)
    assert "User replied: MySQL please" in c.llm.calls[1]["messages"][-1]["content"][0]["content"]


async def test_typing_instead_of_approving_declines_and_redirects(tmp_path):
    c = make(tmp_path, [[tool_use("run_shell", command="pip install django")], [text("OK, using Flask.")]],
             approval="low")
    tid = c.start_task("set up a web app")
    q = c.subscribe(tid)
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "request":
            assert c.send_message(tid, "no, use flask") == "queued"
            break
    await c.wait(tid)
    last = c.llm.calls[1]["messages"][-1]
    assert last["content"][0]["is_error"] and "no, use flask" in texts(last)
