"""Calling up to a stronger model: follow-up rerouting, struggle detection, experts, manual upgrades."""

import asyncio

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.llm import adapt_messages
from conductor.models import ModelInfo

from fakes import FakeLLM, progress, text, tool_use

FRONTIER = ModelInfo("lab/frontier", "openrouter", "Frontier", 4, 20, tier="frontier", vision=True)
STRONG = ModelInfo("lab/strong", "openrouter", "Strong", 1, 5, tier="strong", vision=True)
FAST = ModelInfo("lab/fast", "openrouter", "Fast", 0.05, 0.4, tier="fast", vision=True)


def L(skill, difficulty):
    return {"skill": skill, "domain": "general", "difficulty": difficulty, "needs_vision": False,
            "parts": [{"skill": skill, "domain": "general"}], "summary": ""}


def make(tmp_path, script, label, **kw):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", learning=False, **kw)
    c = Conductor(s, llm=FakeLLM(script, label=label))
    c.catalog.models = {m.id: m for m in (FRONTIER, STRONG, FAST)}
    c.catalog.set_roster([FRONTIER.id, STRONG.id, FAST.id])
    return c


async def test_follow_up_that_outgrows_the_model_moves_up(tmp_path):
    c = make(tmp_path, [[progress("thinking on fast"), text("Yes, gh is logged in.")], [text("Cloned and set up.")]],
             L("chat", "easy"))
    tid = c.start_task("Do you have access to my GitHub repos?")
    await c.wait(tid)
    assert c.store.get_task(tid)["model"] == FAST.id
    c.llm.label = L("coding", "hard")
    c.send_message(tid, "Clone my Go repo, install Go, run the tests, and get the UI design reviewed")
    await c.wait(tid)
    # one step up the ladder, not straight to the most expensive model
    assert c.llm.calls[1]["model"] == STRONG.id
    t = c.store.get_task(tid)
    assert t["model"] == STRONG.id and t["route"]["escalations"][0]["from"] == FAST.id
    assert any(e["type"] == "escalated" for e in c.store.load_events(tid))
    # the fast model is marked down for coding work
    assert any(r["model"] == FAST.id and r["category"] == "coding" for r in c.store.run_stats())


def test_new_model_gets_a_clean_view_of_earlier_turns():
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "model": FAST.id, "content": [
            {"type": "thinking", "thinking": "secret", "signature": "s"},
            {"type": "server_tool_use", "id": "srv", "name": "web_search", "input": {"query": "go"}},
            {"type": "web_search_tool_result", "tool_use_id": "srv",
             "content": [{"type": "web_search_result", "title": "Go", "url": "https://go.dev"}]},
            {"type": "text", "text": "Found it."},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "data"}]},
    ]
    view = adapt_messages(history, FRONTIER)
    turn = view[1]["content"]
    assert [b["type"] for b in turn] == ["text", "text", "tool_use"]
    assert "go.dev" in turn[0]["text"] and all("model" not in m for m in view)
    # the same model still sees its own blocks untouched
    assert adapt_messages(history, FAST)[1]["content"] == history[1]["content"]


async def test_repeating_itself_triggers_a_stronger_model(tmp_path):
    same = lambda: [tool_use("list_files", path=".")]
    c = make(tmp_path, [same() for _ in range(6)] + [[text("Different approach worked.")]], L("research", "medium"))
    tid = c.start_task("look around", model=STRONG.id)
    await c.wait(tid)
    models = [cl["model"] for cl in c.llm.calls]
    # first time stuck: step back (same model); stuck again: move up
    step_back = c.llm.calls[3]["messages"][-1]["content"][-1]
    assert "Move backwards" in step_back["text"] and models[3] == STRONG.id
    assert models[:6] == [STRONG.id] * 6 and models[6] == FRONTIER.id
    note = c.llm.calls[6]["messages"][-1]["content"][-1]
    assert note["type"] == "text" and "taking over this task from Strong" in note["text"]


async def test_consult_expert_asks_a_stronger_model(tmp_path):
    c = make(tmp_path, [[tool_use("consult_expert", question="Is Go 1.27 able to build a go 1.26 module?",
                                  context="go.mod says go 1.26.0")],
                        [text("Yes: newer toolchains build older go directives.")],  # the expert
                        [text("Confirmed, building now.")]], L("coding", "medium"))
    tid = c.start_task("build it", model=FAST.id)
    await c.wait(tid)
    expert_call = c.llm.calls[1]
    assert expert_call["model"] == STRONG.id and expert_call["tools"] == []
    assert "go 1.26.0" in expert_call["messages"][0]["content"]
    result = c.llm.calls[2]["messages"][-1]["content"][0]["content"]
    assert "Advice from Strong" in result and "newer toolchains" in result
    assert c.store.get_task(tid)["model"] == FAST.id  # advice only; the task stays put


async def test_model_can_ask_to_escalate(tmp_path):
    c = make(tmp_path, [[tool_use("escalate", reason="this needs deep Go expertise")], [text("On it.")]],
             L("coding", "medium"))
    tid = c.start_task("refactor the grouping engine", model=FAST.id)
    await c.wait(tid)
    assert c.llm.calls[1]["model"] == STRONG.id


async def test_manual_upgrade_and_limits(tmp_path):
    c = make(tmp_path, [[text("hello")], [text("better hello")]], L("chat", "easy"))
    tid = c.start_task("hi")
    await c.wait(tid)
    assert await c.escalate(tid, "You asked.", by="user", target="auto") == STRONG.id  # next step up
    assert await c.escalate(tid, "You asked.", by="user", target="best") == FRONTIER.id  # straight to the top
    assert await c.escalate(tid, "again", by="auto") is None  # nothing stronger left
    c.llm.label = L("chat", "easy")
    c.send_message(tid, "and again")
    await c.wait(tid)
    assert c.llm.calls[-1]["model"] == FRONTIER.id


async def test_models_are_told_who_they_can_call_up_to(tmp_path):
    c = make(tmp_path, [[text("hi")]], L("chat", "easy"))
    tid = c.start_task("hello")
    await c.wait(tid)
    system = c.llm.calls[0]["system"]
    assert "You're running as Fast" in system and "big brother" in system and "Frontier" in system
    # the top model is told it's at the top, and who gives second opinions
    c2 = make(tmp_path / "b", [[text("hi")]], L("chat", "easy"))
    tid2 = c2.start_task("hello", model=FRONTIER.id)
    await c2.wait(tid2)
    assert "among the strongest" in c2.llm.calls[0]["system"]


async def test_hedging_gets_a_nudge_then_a_stronger_model(tmp_path):
    from fakes import progress
    hedge = lambda i: [progress(f"Hmm, actually wait - I'm not sure about step {i}."), tool_use("list_files", path=f"d{i}")]
    c = make(tmp_path, [hedge(1), hedge(2), hedge(3), [text("Clear answer.")]], L("coding", "medium"))
    tid = c.start_task("tricky thing", model=FAST.id)
    await c.wait(tid)
    nudge = c.llm.calls[1]["messages"][-1]["content"][-1]
    assert nudge["type"] == "text" and "consult_expert" in nudge["text"] and "“hmm”" in nudge["text"]
    models = [cl["model"] for cl in c.llm.calls]
    assert models[:2] == [FAST.id] * 2 and models[2] == STRONG.id  # nudge first, then one step up
    handoff = c.llm.calls[2]["messages"][-1]["content"][0]["text"]
    assert "Strong is taking over this task from Fast" in handoff and "kept hedging" in handoff


async def test_follow_up_handoff_note_tells_the_new_model_why(tmp_path):
    c = make(tmp_path, [[text("yes")], [text("on it")]], L("chat", "easy"))
    tid = c.start_task("can you see my repos?")
    await c.wait(tid)
    c.llm.label = L("coding", "hard")
    c.send_message(tid, "clone it and refactor the ingest pipeline")
    await c.wait(tid)
    last = c.llm.calls[1]["messages"][-1]
    assert last["role"] == "user" and "taking over this task from Fast" in last["content"][0]["text"]


async def test_find_model_then_hand_off_to_the_chosen_one(tmp_path):
    c = make(tmp_path, [[tool_use("find_model", need="tricky Go concurrency bug")],
                        [tool_use("escalate", reason="race condition beyond me", model=FRONTIER.id)],
                        [text("Fixed the race.")]], L("coding", "medium"))
    tid = c.start_task("fix the flaky test", model=FAST.id)
    await c.wait(tid)
    options = c.llm.calls[1]["messages"][-1]["content"][0]["content"]
    assert options.index("lab/strong") < options.index("lab/frontier")  # smallest step first
    assert "tier(s) up" in options
    assert c.llm.calls[2]["model"] == FRONTIER.id  # it chose to go straight to the top


async def test_escalating_to_an_unknown_model_is_refused(tmp_path):
    c = make(tmp_path, [[tool_use("escalate", reason="x", model="nope/model")], [text("ok")]], L("coding", "medium"))
    tid = c.start_task("x", model=FAST.id)
    await c.wait(tid)
    res = c.llm.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "find_model" in res["content"]
