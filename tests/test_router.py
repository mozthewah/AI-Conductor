import json
import random

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.models import Catalog, ModelInfo, parse_openrouter
from conductor.router import Router, heuristic_label

from fakes import FakeLLM, text

FRONTIER = ModelInfo("lab/frontier", "openrouter", "Frontier", 4, 20, tier="frontier", vision=True)
STRONG = ModelInfo("lab/strong", "openrouter", "Strong", 1, 5, tier="strong")
FAST = ModelInfo("lab/fast", "openrouter", "Fast", 0.05, 0.4, tier="fast", vision=True)


def conductor(tmp_path, label, script=(), **kw):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="auto", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", **kw)
    c = Conductor(s, llm=FakeLLM(list(script), label=label))
    c.catalog.models = {m.id: m for m in (FRONTIER, STRONG, FAST)}
    c.catalog.set_roster([FRONTIER.id, STRONG.id, FAST.id])
    return c


async def test_easy_chat_goes_cheap_hard_coding_goes_strong(tmp_path):
    c = conductor(tmp_path, {"category": "chat", "difficulty": "easy", "needs_vision": False, "summary": ""})
    d = await c.router.route("what's 2+2", seed=1)
    assert d.model == FAST.id and d.category == "chat"
    c.llm.label = {"category": "coding", "difficulty": "hard", "needs_vision": False, "summary": ""}
    d = await c.router.route("refactor my compiler", seed=1)
    assert d.model == FRONTIER.id
    d = await c.router.route("refactor my compiler", seed=1, budget="frugal")
    assert d.model in (STRONG.id, FRONTIER.id)
    assert len(d.candidates) == 3 and d.reason


async def test_vision_filters_candidates(tmp_path):
    c = conductor(tmp_path, {"category": "analysis", "difficulty": "medium", "needs_vision": True, "summary": ""})
    d = await c.router.route("what's in this screenshot", seed=3)
    assert d.model in (FRONTIER.id, FAST.id)


async def test_router_learns_from_feedback(tmp_path):
    label = {"category": "writing", "difficulty": "medium", "needs_vision": False, "summary": ""}
    c = conductor(tmp_path, label)
    before = (await c.router.route("draft an email", seed=0)).model
    loser = before
    winner = next(m for m in (FRONTIER.id, STRONG.id, FAST.id) if m != loser)
    for i in range(6):  # user keeps thumbing down `loser` and up `winner` on writing tasks
        for m, v in ((loser, -1), (winner, 1)):
            tid = c.store.create_task("x")
            c.store.start_run(tid, m, "writing", "medium", "router")
            c.store.finish_run(tid, "done", 0.01, 5)
            c.store.set_feedback(tid, v)
    after = await c.router.route("draft an email", seed=0)
    assert after.model == winner
    # other categories are unaffected by writing feedback
    stats = {r["model"]: r for r in c.store.model_stats("writing")}
    assert stats[winner]["up"] == 6 and stats[loser]["down"] == 6


async def test_pinned_model_skips_classifier(tmp_path):
    c = conductor(tmp_path, {"category": "chat", "difficulty": "easy", "needs_vision": False, "summary": ""})
    d = await c.router.route("hello", pinned=STRONG.id)
    assert d.model == STRONG.id and d.by == "pinned" and not c.llm.structured_calls


async def test_classifier_failure_falls_back_to_rules(tmp_path):
    c = conductor(tmp_path, {"category": "bogus"})
    d = await c.router.route("please debug this python function that crashes")
    assert d.category == "coding"
    assert heuristic_label("book me a table on opentable")["category"] == "browsing"


async def test_task_records_route_cost_and_compare(tmp_path):
    label = {"category": "chat", "difficulty": "easy", "needs_vision": False, "summary": ""}
    c = conductor(tmp_path, label, script=[[text("4")], [text("four")]])
    tid = c.start_task("what's 2+2")
    await c.wait(tid)
    t = c.store.get_task(tid)
    assert t["model"] == FAST.id and t["route"]["category"] == "chat" and t["cost_usd"] > 0
    run = c.store.get_run(tid)
    assert run["status"] == "done" and run["seconds"] >= 0
    assert c.llm.calls[0]["model"] == FAST.id
    # side-by-side on another model
    cid = c.compare(tid, FRONTIER.id)
    await c.wait(cid)
    ct = c.store.get_task(cid)
    assert ct["model"] == FRONTIER.id and ct["compare_of"] == tid and ct["route"]["by"] == "compare"
    assert {r["id"] for r in c.store.compares(cid)} == {tid, cid}
    assert c.feedback(cid, 1) and c.store.get_run(cid)["feedback"] == 1


async def test_subagents_are_routed_frugally(tmp_path):
    from fakes import tool_use
    label = {"category": "research", "difficulty": "medium", "needs_vision": False, "summary": ""}
    c = conductor(tmp_path, label, script=[
        [tool_use("delegate", tasks=[{"title": "A", "instructions": "look up A"}])],
        [text("A report")], [text("done")]])
    c.store.update_task  # noqa
    tid = c.start_task("research A", model=FRONTIER.id)
    await c.wait(tid)
    child = c.store.child_tasks(tid)[0]
    assert c.store.get_task(tid)["model"] == FRONTIER.id
    assert c.store.get_task(child["id"])["model"] in (STRONG.id, FAST.id)


def test_parse_openrouter_entry_and_roster(tmp_path):
    entry = {"id": "openai/gpt-x", "name": "GPT X", "context_length": 1000, "pricing": {"prompt": "0.000002",
             "completion": "0.00001"}, "supported_parameters": ["tools", "reasoning", "structured_outputs"],
             "architecture": {"input_modalities": ["text", "image"]}, "top_provider": {"max_completion_tokens": 999}}
    m = parse_openrouter(entry)
    assert (m.price_in, m.price_out, m.tier, m.vision, m.tools, m.max_output) == (2.0, 10.0, "frontier", True, True, 999)
    meta = parse_openrouter({**entry, "id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}})
    assert meta.price_in == 0
    (tmp_path / "openrouter_models.json").write_text(json.dumps({"fetched_at": 9e12, "data": [entry]}))
    cat = Catalog(tmp_path, has_openrouter=True, has_anthropic=False, fetch=False).load()
    assert [m.id for m in cat.roster()] == []  # default roster ids aren't in this tiny catalog
    cat.add("openai/gpt-x")
    assert [m.id for m in cat.roster()] == ["openai/gpt-x"]
    assert cat.search("gpt")[0].id == "openai/gpt-x"
