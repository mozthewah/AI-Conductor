"""The composer: delegates by default, supervises specialists, and handles blockers."""

import asyncio

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.models import ModelInfo
from conductor.router import needs_conductor

from fakes import FakeLLM, text, tool_use

TOP = ModelInfo("lab/top", "openrouter", "Top", 4, 20, tier="frontier", vision=True)
MID = ModelInfo("lab/mid", "openrouter", "Mid", 1, 5, tier="strong", vision=True)


def L(skill, difficulty, parts=None):
    return {"skill": skill, "domain": "general", "difficulty": difficulty, "needs_vision": False,
            "parts": parts or [{"skill": skill, "domain": "general"}], "summary": ""}


class Scripted(FakeLLM):
    def __init__(self, respond, label, supervise=None):
        super().__init__([], label=label)
        self.respond, self.supervise = respond, supervise or (lambda p: {"on_track": True, "issue": "", "steer": ""})

    async def turn(self, *, system, messages, tools, on_event, effort=None, model=None, web=True, session_id=None):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "model": model.id,
                           "tools": [t["name"] for t in tools]})
        blocks = self.respond(system, messages, model.id)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return {"content": blocks, "stop_reason": stop, "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0.0}}

    async def structured(self, *, system, prompt, schema, effort="low", model=None):
        if "on_track" in schema["properties"]:
            self.structured_calls.append(prompt)
            return self.supervise(prompt)
        return await super().structured(system=system, prompt=prompt, schema=schema, effort=effort, model=model)


def make(tmp_path, respond, label, supervise=None, **kw):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", learning=False, **kw)
    c = Conductor(s, llm=Scripted(respond, label, supervise))
    c.catalog.models = {m.id: m for m in (TOP, MID)}
    c.catalog.set_roster([TOP.id, MID.id])
    return c


def last_text(messages):
    c = messages[-1]["content"]
    return c if isinstance(c, str) else " ".join(b.get("text", "") for b in c if b.get("type") == "text")


def test_compose_by_default_except_quick_questions():
    assert needs_conductor(L("coding", "medium"))
    assert needs_conductor(L("research", "medium"))
    assert not needs_conductor(L("chat", "easy"))
    assert not needs_conductor(L("research", "easy"))
    assert not needs_conductor(L("coding", "medium"), compose="off")
    assert needs_conductor(L("chat", "easy"), compose="always")


async def test_specialist_drifting_gets_steered(tmp_path):
    state = {"spec": 0}

    def respond(system, messages, model):
        if "You are conducting" in system:
            if len(messages) == 1:
                return [tool_use("assign", parts=[{"id": "fix", "title": "Fix login bug", "skill": "coding",
                                                   "domain": "general", "instructions": "Fix the login bug"}])]
            return [text("done")]
        state["spec"] += 1
        if state["spec"] <= 4:
            return [tool_use("list_files", path=f"themes/{state['spec']}")]  # wandering off into themes
        return [text("Fixed the login bug.")]

    c = make(tmp_path, respond, L("coding", "medium"),
             supervise=lambda p: {"on_track": False, "issue": "it's redesigning themes instead of fixing login",
                                  "steer": "Go back to the login handler."}, check_every=2)
    tid = c.start_task("fix the login bug")
    await c.wait(tid)
    spec_calls = [cl for cl in c.llm.calls if "on a team run by a composer" in cl["system"]]
    steer = spec_calls[2]["messages"][-1]["content"][-1]["text"]
    assert "drifting" in steer and "login handler" in steer
    assert any(e["type"] == "steer" for e in c.store.load_events(tid))  # the composer sees it


async def test_blocked_specialist_is_resumed_by_the_composer(tmp_path):
    state = {"composer": 0}

    def respond(system, messages, model):
        if "You are conducting" in system:
            state["composer"] += 1
            if state["composer"] == 1:
                return [tool_use("assign", parts=[
                    {"id": "deploy", "title": "Deploy", "skill": "coding", "domain": "general", "instructions": "Deploy it"},
                    {"id": "docs", "title": "Write docs", "skill": "writing", "domain": "general", "instructions": "Docs"}])]
            if state["composer"] == 2:
                report = last_text(messages) + str(messages[-1]["content"])
                assert "BLOCKED" in report and "staging URL" in report  # blocker surfaced, docs still done
                assert "Docs written" in report
                return [tool_use("review", part="deploy", verdict="revise", notes="Staging URL is https://staging.x")]
            return [text("All done.")]
        if "wants changes" in last_text(messages):
            assert "staging.x" in last_text(messages)
            return [text("Deployed to staging.")]
        if "Deploy" in last_text(messages):
            return [tool_use("report_blocked", what="no staging target", needs="the staging URL")]
        return [text("Docs written.")]

    c = make(tmp_path, respond, L("coding", "medium"))
    tid = c.start_task("deploy and document")
    await c.wait(tid)
    deploy = c.part_tasks(tid, "deploy")[-1]
    assert deploy["status"] == "done" and "Deployed" in deploy["answer"]
    assert c.store.get_task(tid)["status"] == "done"


async def test_specialists_can_delegate_one_level_down_but_not_forever(tmp_path):
    def respond(system, messages, model):
        if "You are conducting" in system:
            return [tool_use("assign", parts=[{"id": "app", "title": "Build app", "skill": "coding",
                                               "domain": "general", "instructions": "Build the app"}])] \
                if len(messages) == 1 else [text("ok")]
        return [text("done")]

    c = make(tmp_path, respond, L("coding", "medium"))
    tid = c.start_task("build an app")
    await c.wait(tid)
    by_depth = {}
    for cl in c.llm.calls:
        depth = 0 if "You are conducting" in cl["system"] else 1
        by_depth[depth] = cl["tools"]
    assert "assign" in by_depth[0] and "assign" in by_depth[1]
    assert all(t.name != "assign" for t in c.tool_list(2))  # max_depth=2: no further hand-offs
