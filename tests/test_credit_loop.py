"""The pause/resume loop from the phone screenshot: a call too big for the balance kept pausing,
'resuming' at the same balance, and failing again."""

import asyncio

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.credits import CreditError, CreditMonitor, classify_402
from conductor.models import ModelInfo

from fakes import FakeLLM, text

BIG = ModelInfo("lab/big", "openrouter", "Big", 5, 25, tier="frontier", context=400_000)
SMALL = ModelInfo("lab/small", "openrouter", "Small", 0.2, 1, tier="strong", context=400_000)

MSG = ("This request's maximum cost exceeds your available credits. Add credits, or lower max_tokens "
       "or prompt size.")


def L(skill="coding", difficulty="hard"):
    return {"skill": skill, "domain": "general", "difficulty": difficulty, "needs_vision": False,
            "parts": [{"skill": skill, "domain": "general"}], "summary": ""}


def test_the_message_is_too_big_not_out():
    err = classify_402(402, {"error": {"code": 402, "message": MSG}})
    assert err.kind == "too_big"


class TooBigFor(FakeLLM):
    def __init__(self, script, expensive: set[str], **kw):
        super().__init__(script, **kw)
        self.expensive = expensive

    async def turn(self, **kw):
        if kw["model"].id in self.expensive:
            self.calls.append({"model": kw["model"].id, "failed": True})
            raise CreditError("too_big", MSG)
        return await super().turn(**kw)


def make(tmp_path, llm, available=1.5):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", compose="off", learning=False)
    c = Conductor(s, llm=llm)
    c.catalog.models = {BIG.id: BIG, SMALL.id: SMALL}
    c.catalog.set_roster([BIG.id, SMALL.id])
    c.credits.state["available"] = available
    c.credits.state["checked"] = 9e18
    return c


async def test_switches_to_a_model_that_fits_instead_of_pausing(tmp_path):
    c = make(tmp_path, TooBigFor([[text("done cheaply")]], {BIG.id}, label=L()))
    tid = c.start_task("big job")
    await asyncio.wait_for(c.wait(tid), 5)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["model"] == SMALL.id
    ev = c.store.load_events(tid)
    assert not any(e["type"] == "paused" for e in ev)
    esc = [e["data"] for e in ev if e["type"] == "escalated"]
    assert esc and esc[-1]["by"] == "credits" and "fits the balance" in esc[-1]["reason"]


async def test_nothing_fits_pauses_and_does_not_resume_at_the_same_balance(tmp_path):
    c = make(tmp_path, TooBigFor([[text("ok")]], {BIG.id, SMALL.id}, label=L()), available=0.01)
    looks = []

    async def same_balance(force=False, max_age=None):
        looks.append(1)
        return c.credits.status()  # still $0.01
    c.credits.check = same_balance
    tid = c.start_task("big job")
    q = c.subscribe(tid)
    paused = None
    while paused is None:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "paused":
            paused = ev["data"]
    assert paused["need"] and paused["need"] > 0.01
    await asyncio.sleep(0.2)
    assert c.store.get_task(tid)["status"] == "paused"  # no "credits are back" at the same balance
    c.credits.resume_now()  # the user can always resume by hand
    c.llm.expensive = set()
    await asyncio.wait_for(c.wait(tid), 5)
    assert c.store.get_task(tid)["status"] == "done"
    assert sum(1 for e in c.store.load_events(tid) if e["type"] == "paused") == 1


async def test_wait_needs_the_balance_to_actually_cover_the_step(monkeypatch):
    m = CreditMonitor(key="k", low=2.0)
    balances = iter([1.5, 1.5, 3.0])

    async def look(force=False, max_age=None):
        m.state["available"] = next(balances)
        return m.status()

    async def no_wait(aw, timeout):
        aw.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(m, "check", look)
    monkeypatch.setattr("conductor.credits.asyncio.wait_for", no_wait)
    assert await m.wait_for_credits(poll=1, need=1.75) == "credits"
    assert m.state["available"] == 3.0  # it skipped the two looks at $1.50
