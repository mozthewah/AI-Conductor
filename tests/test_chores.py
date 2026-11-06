"""Free OpenRouter models for small chores: chat titles and follow-up suggestions."""

import asyncio

from conductor.agent import Conductor
from conductor.chores import clean_title
from conductor.config import Settings
from conductor.models import ModelInfo

from fakes import FakeLLM, text

FREE_A = ModelInfo("google/gemma-4-31b-it:free", "openrouter", "Gemma 4 31B (free)", 0, 0, tier="fast")
FREE_B = ModelInfo("nvidia/nemotron-3.5-lightning:free", "openrouter", "Nemotron (free)", 0, 0, tier="fast")
MUSIC = ModelInfo("google/lyria-3-pro-preview:free", "openrouter", "Lyria", 0, 0, tier="fast")
WORK = ModelInfo("lab/worker", "openrouter", "Worker", 1, 5, tier="strong")
ROUTER = ModelInfo("anthropic/claude-haiku-5.5", "openrouter", "Haiku", 1, 5, tier="fast")

LONG = "# Burrow redesign: handoff summary (as of Thu 5 Nov 2026)\nPlease finish the Night Tape templates and run QA."


class Quick(FakeLLM):
    def __init__(self, script, replies=None, fail=(), **kw):
        super().__init__(script, **kw)
        self.replies, self.fail, self.quick_calls = replies or {}, set(fail), []

    async def quick(self, *, model, system, prompt, max_tokens=300):
        self.quick_calls.append((model.id, system[:20]))
        if model.id in self.fail:
            raise RuntimeError("429 rate limited")
        return self.replies.get("title" if "name chats" in system else "suggest", "")


def L():
    return {"skill": "coding", "domain": "general", "difficulty": "medium", "needs_vision": False,
            "parts": [{"skill": "coding", "domain": "general"}], "summary": ""}


def make(tmp_path, llm, free_models="auto"):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", compose="off", learning=False,
                 free_models=free_models)
    c = Conductor(s, llm=llm)
    c.catalog.models = {m.id: m for m in (FREE_A, FREE_B, MUSIC, WORK, ROUTER)}
    c.catalog.set_roster([WORK.id])
    return c


def test_clean_title():
    assert clean_title('"Burrow Night Tape Redesign".') == "Burrow Night Tape Redesign"
    assert clean_title("Title: **Fix login bug**") == "Fix login bug"
    assert clean_title("<think>hmm</think>\n# QA for Night Tape") == "QA for Night Tape"
    assert clean_title("x") is None


def test_picks_text_free_models_only(tmp_path):
    c = make(tmp_path, Quick([]))
    cands = c.chores.candidates()
    assert cands[0] == FREE_A.id and MUSIC.id not in cands and WORK.id not in cands


async def test_titles_and_suggestions(tmp_path):
    llm = Quick([[text("All done: templates finished.")]], label=L(),
                replies={"title": "Burrow Night Tape Redesign", "suggest": "1. Run the tests again\n- Commit the changes\nShow me the screenshots"})
    c = make(tmp_path, llm)
    tid = c.start_task(LONG)
    await asyncio.wait_for(c.wait(tid), 5)
    for _ in range(50):
        await asyncio.sleep(0.02)
        ev = c.store.load_events(tid)
        if any(e["type"] == "suggestions" for e in ev):
            break
    assert c.store.get_task(tid)["title"] == "Burrow Night Tape Redesign"
    sugg = [e["data"]["items"] for e in c.store.load_events(tid) if e["type"] == "suggestions"][0]
    assert sugg == ["Run the tests again", "Commit the changes", "Show me the screenshots"]
    assert all(mid.endswith(":free") for mid, _ in llm.quick_calls)


async def test_a_failing_free_model_is_skipped(tmp_path):
    llm = Quick([], replies={"title": "Night Tape QA"}, fail={FREE_A.id})
    c = make(tmp_path, llm)
    assert await c.chores.title(LONG) == "Night Tape QA"
    assert [m for m, _ in llm.quick_calls] == [FREE_A.id, FREE_B.id]
    assert FREE_A.id not in c.chores.candidates()  # cooling down for a while


async def test_off_keeps_the_first_line_title(tmp_path):
    llm = Quick([[text("ok")]], label=L(), replies={"title": "Nope"})
    c = make(tmp_path, llm, free_models="off")
    tid = c.start_task(LONG)
    await asyncio.wait_for(c.wait(tid), 5)
    await asyncio.sleep(0.05)
    assert c.store.get_task(tid)["title"].startswith("# Burrow redesign") and llm.quick_calls == []


async def test_short_prompts_keep_their_own_title(tmp_path):
    llm = Quick([[text("4")]], label=L(), replies={"title": "Math"})
    c = make(tmp_path, llm)
    tid = c.start_task("what's 2+2")
    await asyncio.wait_for(c.wait(tid), 5)
    await asyncio.sleep(0.05)
    assert c.store.get_task(tid)["title"] == "what's 2+2"
