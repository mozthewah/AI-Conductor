"""The conductor: split a request, hire the best model per part, review, integrate."""

import pytest

from conductor.agent import Conductor
from conductor.benchmarks import Benchmarks, parse
from conductor.config import Settings
from conductor.models import ModelInfo

from fakes import FakeLLM, text, tool_use

GEM = ModelInfo("google/gem", "openrouter", "Gem", 2, 10, tier="frontier", vision=True)
OPUS = ModelInfo("anthropic/opus", "openrouter", "Opus", 2, 10, tier="frontier", vision=True)
KIMI = ModelInfo("moonshotai/kimi", "openrouter", "Kimi", 2, 10, tier="frontier")
SEEK = ModelInfo("deepseek/seek", "openrouter", "Seek", 2, 10, tier="frontier")
ALL = (GEM, OPUS, KIMI, SEEK)


class Scripted(FakeLLM):
    """Answers by role instead of call order, since specialists run in parallel."""

    def __init__(self, respond, label):
        super().__init__([], label=label)
        self.respond = respond

    async def turn(self, *, system, messages, tools, on_event, effort=None, model=None, web=True, session_id=None):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "model": model.id,
                           "tools": [t["name"] for t in tools], "effort": effort})
        blocks = self.respond(system, messages, model.id)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return {"content": blocks, "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.001}}


def make(tmp_path, respond, label):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="auto", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", learning=False)
    c = Conductor(s, llm=Scripted(respond, label))
    c.catalog.models = {m.id: m for m in ALL}
    c.catalog.set_roster([m.id for m in ALL])
    # Public benchmarks: Gem leads design, Opus leads coding, everyone ties on the rest.
    c.benchmarks.signals = {
        "design": {GEM.id: 1350, OPUS.id: 1200, KIMI.id: 1100, SEEK.id: 1000},
        "coding": {OPUS.id: 72, GEM.id: 60, KIMI.id: 55, SEEK.id: 50},
        "agentic": {OPUS.id: 70, GEM.id: 60, KIMI.id: 50, SEEK.id: 40},
        "intelligence": {OPUS.id: 70, GEM.id: 70, KIMI.id: 70, SEEK.id: 70},
    }
    return c


TWO_PARTS = {"skill": "coding", "domain": "general", "difficulty": "medium", "needs_vision": False,
             "parts": [{"skill": "ui_design", "domain": "general"}, {"skill": "coding", "domain": "general"}],
             "summary": "design and build"}


def user_text(messages):
    last = messages[-1]["content"]
    return last if isinstance(last, str) else " ".join(b.get("text", "") for b in last if b.get("type") == "text")


async def test_design_then_code_each_by_its_best_model(tmp_path):
    def respond(system, messages, model):
        if "You are conducting" in system:  # the conductor
            if len(messages) == 1:
                return [tool_use("assign", parts=[
                    {"id": "design", "title": "Design the UI", "skill": "ui_design", "domain": "general",
                     "instructions": "Design the habit tracker UI into design/ui.md"},
                    {"id": "code", "title": "Build it", "skill": "coding", "domain": "general",
                     "instructions": "Implement the design", "depends_on": ["design"]}])]
            if len(messages) == 3:
                return [tool_use("review", part="design", verdict="accept"),
                        tool_use("review", part="code", verdict="accept")]
            return [text("Shipped: design by Gem, code by Opus.")]
        if "ui design specialist" in system:
            return [text("DESIGN: two columns, teal accents")]
        if "coding specialist" in system:
            assert "DESIGN: two columns, teal accents" in user_text(messages)  # got the design as input
            return [text("CODE: built per design")]
        raise AssertionError(system[:200])

    c = make(tmp_path, respond, TWO_PARTS)
    tid = c.start_task("Design and build me a habit tracker app")
    await c.wait(tid)
    task = c.store.get_task(tid)
    assert task["status"] == "done", task["answer"]
    assert task["route"]["mode"] == "conduct"
    parts = {p["route"]["part_id"]: p for p in c.part_tasks(tid)}
    assert parts["design"]["model"] == GEM.id      # best at design
    assert parts["code"]["model"] == OPUS.id       # best at code
    order = [cl["model"] for cl in c.llm.calls if "on a team run by Conductor" in cl["system"]]
    assert order == [GEM.id, OPUS.id]              # code waited for the design
    assert c.store.get_run(parts["design"]["id"])["review"] == pytest.approx(0.9)
    assert c.store.get_run(parts["code"]["id"])["mode"] == "part"
    types = [e["type"] for e in c.store.load_events(tid)]
    assert "plan_parts" in types and types.count("part") >= 6


async def test_revise_goes_back_to_same_model_and_reassign_switches(tmp_path):
    state = {"conductor_turn": 0}

    def respond(system, messages, model):
        if "You are conducting" in system:
            state["conductor_turn"] += 1
            n = state["conductor_turn"]
            if n == 1:
                return [tool_use("assign", parts=[{"id": "ui", "title": "UI", "skill": "ui_design",
                                                   "domain": "general", "instructions": "design it"}])]
            if n == 2:
                return [tool_use("review", part="ui", verdict="revise", notes="needs a dark mode")]
            if n == 3:
                return [tool_use("review", part="ui", verdict="reassign", notes="still off-brand")]
            if n == 4:
                return [tool_use("review", part="ui", verdict="accept")]
            return [text("done")]
        if "wants changes" in user_text(messages):
            return [text(f"revised by {model}")]
        return [text(f"first draft by {model}")]

    c = make(tmp_path, respond, TWO_PARTS)
    tid = c.start_task("design and build")
    await c.wait(tid)
    runs = c.part_tasks(tid, "ui")
    assert len(runs) == 2
    first, second = runs
    assert first["model"] == GEM.id and second["model"] != GEM.id      # reassigned elsewhere
    assert first["route"]["revisions"] == 1
    # the revision continued the same specialist conversation (append-only)
    revision_call = next(cl for cl in c.llm.calls if "wants changes" in user_text(cl["messages"]))
    assert revision_call["model"] == GEM.id and len(revision_call["messages"]) == 3
    assert c.store.get_run(first["id"])["review"] == pytest.approx(0.1)
    assert c.store.get_run(second["id"])["review"] == pytest.approx(0.9)


async def test_domain_expertise_is_learned_and_carries_over(tmp_path):
    c = make(tmp_path, lambda *a: [text("x")], TWO_PARTS)
    # Kimi keeps nailing finance analysis; Seek keeps nailing travel research.
    for model, skill, domain in [(KIMI.id, "analysis", "finance"), (SEEK.id, "research", "travel")]:
        for _ in range(5):
            t = c.store.create_task("hist")
            c.store.start_run(t, model, skill, "medium", "router", domain=domain)
            c.store.finish_run(t, "done", 0.01, 5)
            c.store.set_feedback(t, 1)
    pick = lambda skill, domain: c.router.route_part(skill=skill, domain=domain).model
    assert pick("analysis", "finance") == KIMI.id
    assert pick("research", "travel") == SEEK.id
    # a new kind of finance work leans on Kimi's finance record
    assert pick("planning", "finance") == KIMI.id
    # unrelated work isn't swayed by it
    assert pick("ui_design", "general") == GEM.id


async def test_assign_rejects_bad_plans(tmp_path):
    def respond(system, messages, model):
        if "You are conducting" in system and len(messages) == 1:
            return [tool_use("assign", parts=[
                {"id": "a", "title": "A", "skill": "coding", "domain": "general", "instructions": "x", "depends_on": ["b"]},
                {"id": "b", "title": "B", "skill": "coding", "domain": "general", "instructions": "y", "depends_on": ["a"]}])]
        return [text("ok")]

    c = make(tmp_path, respond, TWO_PARTS)
    tid = c.start_task("x")
    await c.wait(tid)
    res = c.llm.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "circular" in res["content"]


async def test_simple_requests_skip_the_conductor(tmp_path):
    label = {"skill": "ui_design", "domain": "general", "difficulty": "medium", "needs_vision": False,
             "parts": [{"skill": "ui_design", "domain": "general"}], "summary": ""}
    c = make(tmp_path, lambda *a: [text("here's a palette")], label)
    tid = c.start_task("suggest a color palette")
    await c.wait(tid)
    t = c.store.get_task(tid)
    assert t["route"]["mode"] == "direct" and t["model"] == GEM.id
    assert "You are conducting" not in t["system_prompt"]


def test_benchmark_parsing_and_percentiles(tmp_path):
    items = [
        {"source": "artificial-analysis", "model_permaslug": "a/x", "coding_index": 70, "intelligence_index": 60},
        {"source": "artificial-analysis", "model_permaslug": "a/y", "coding_index": 50, "intelligence_index": 65},
        {"source": "design-arena", "model_permaslug": "a/x", "elo": 1200, "category": "website"},
        {"source": "design-arena", "model_permaslug": "a/x", "elo": 1300, "category": "uicomponent"},
        {"source": "design-arena", "model_permaslug": "a/y", "elo": 1100},
        {"source": "openrouter", "model_permaslug": "a/y", "benchmark_type": "search_browsecomp", "primary_score": 0.6},
    ]
    sig = parse(items)
    assert sig["design"]["a/x"] == 1250 and sig["search"]["a/y"] == 0.6
    b = Benchmarks(tmp_path)
    b.signals = sig
    assert b.prior("a/x", "coding", ["a/x", "a/y"]) > b.prior("a/y", "coding", ["a/x", "a/y"])
    assert b.prior("a/x", "ui_design", ["a/x", "a/y"]) == pytest.approx(1.0)
    assert b.prior("a/z", "coding", ["a/x", "a/y"]) is None
