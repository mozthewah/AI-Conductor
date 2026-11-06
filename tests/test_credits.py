"""Running low or out of OpenRouter credits: retry what's retryable, pause the rest, resume later."""

import asyncio
import json

import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient

from conductor.agent import Conductor
from conductor.config import Settings
from conductor.credits import CreditError, CreditMonitor, classify_402
from conductor.llm import MessagesLLM
from conductor.models import ModelInfo

from fakes import FakeLLM, text, tool_use
from test_llm_wire import OR_STREAM, collect, sse


def err(reason=None, source=None, message="Insufficient credits"):
    meta = {k: v for k, v in (("reason", reason), ("limit_source", source)) if v}
    return {"error": {"code": 402, "message": message, "metadata": meta}}


def test_classify_the_kinds_of_402():
    assert classify_402(402, err("in_flight_budget_exhausted", "openrouter_in_flight_budget"), {"retry-after": "7"}).kind == "in_flight"
    assert classify_402(402, err("in_flight_budget_exhausted", "openrouter_in_flight_budget"), {"retry-after": "7"}).retry_after == 7
    assert classify_402(402, err("weight_exceeds_budget", "openrouter_credits")).kind == "too_big"
    big = classify_402(402, err(message="This request requires more credits, or fewer max_tokens. You requested up to 64000 tokens, but can only afford 9000."))
    assert big.kind == "too_big" and big.affordable == 9000
    assert classify_402(402, err(source="openrouter_key_limit")).kind == "out"
    assert classify_402(402, err(source="openrouter_credits")).kind == "out"
    assert classify_402(400, {"error": {"message": "Your credit balance is too low to access the Anthropic API."}}).kind == "out"
    assert classify_402(400, {"error": {"message": "max_tokens too large"}}) is None
    assert classify_402(429, {"error": {"message": "rate limited"}}) is None


def scripted_http(responses, seen):
    def handler(request):
        seen.append(json.loads(request.content))
        status, body, headers = responses.pop(0)
        if status == 200:
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(OR_STREAM))
        return httpx2.Response(status, json=body, headers=headers or {})
    return DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))


async def test_in_flight_budget_waits_and_retries(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr("conductor.llm.asyncio.sleep", fake_sleep)
    seen = []
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", http_client=scripted_http(
        [(402, err("in_flight_budget_exhausted", "openrouter_in_flight_budget"), {"retry-after": "3"}), (200, None, None)], seen))
    out, events = await collect(llm, ModelInfo("a/b", "openrouter"))
    assert out["stop_reason"] == "end_turn" and slept == [3.0] and len(seen) == 2


async def test_too_big_request_retries_with_a_smaller_reply_budget():
    seen = []
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", http_client=scripted_http(
        [(402, err(message="You requested up to 64000 tokens, but can only afford 20000."), None), (200, None, None)], seen))
    out, events = await collect(llm, ModelInfo("a/b", "openrouter", max_output=64000))
    assert seen[0]["max_tokens"] == 64000 and seen[1]["max_tokens"] == 18000


async def test_truly_out_raises_for_the_agent_to_pause():
    llm = MessagesLLM(anthropic_enabled=False, openrouter_key="k", http_client=scripted_http(
        [(402, err(source="openrouter_credits", message="Insufficient credits"), None)], []))
    with pytest.raises(CreditError) as e:
        await collect(llm, ModelInfo("a/b", "openrouter"))
    assert e.value.kind == "out"


class OutOnce(FakeLLM):
    """Fails the first N calls for lack of credits, then behaves."""

    def __init__(self, script, fail=1, **kw):
        super().__init__(script, **kw)
        self.fail = fail

    async def turn(self, **kw):
        if self.fail:
            self.fail -= 1
            raise CreditError("out", "Insufficient credits")
        return await super().turn(**kw)


def make(tmp_path, llm, **kw):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, learning=False, compose="off", **kw)
    return Conductor(s, llm=llm)


async def test_task_pauses_then_resumes_where_it_left_off(tmp_path):
    c = make(tmp_path, OutOnce([[tool_use("list_files", path=".")], [text("Finished.")]]))
    tid = c.start_task("do it")
    q = c.subscribe(tid)
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "paused":
            assert c.store.get_task(tid)["status"] == "paused" and "Insufficient" in ev["data"]["reason"]
            assert c.resume(tid) == "resumed"  # user topped up and pressed Resume
            break
    await asyncio.wait_for(c.wait(tid), 5)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["answer"] == "Finished."
    assert len(c.llm.calls) == 2  # the failed step re-ran; no duplicate turns in the transcript
    assert [m["role"] for m in c.store.load_messages(tid)] == ["user", "assistant", "user", "assistant"]
    assert any(e["type"] == "resumed" for e in c.store.load_events(tid))


async def test_paused_specialist_shows_on_the_main_task_and_resumes(tmp_path):
    from test_composing import L, Scripted

    calls = {"spec": 0}

    def respond(system, messages, model):
        if "You are conducting" in system:
            return [tool_use("assign", parts=[{"id": "p", "title": "Part", "skill": "coding", "domain": "general",
                                               "instructions": "x"}])] if len(messages) == 1 else [text("all done")]
        calls["spec"] += 1
        if calls["spec"] == 1:
            raise CreditError("out", "Insufficient credits")
        return [text("part done")]

    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, learning=False)
    c = Conductor(s, llm=Scripted(respond, L("coding", "medium")))
    tid = c.start_task("build")
    q = c.subscribe(tid)
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "paused":
            assert c.store.get_task(tid)["status"] == "paused"
            c.credits.resume_now()
            break
    await asyncio.wait_for(c.wait(tid), 5)
    assert c.store.get_task(tid)["status"] == "done"


async def test_low_credits_warn_once_and_route_cheaper(tmp_path):
    c = make(tmp_path, FakeLLM([[text("a")], [text("b")]]), openrouter_key="k")
    c.credits.state.update({"available": 0.8, "source": "account balance", "checked": 9e12})
    assert c.credits.status()["level"] == "low"
    from conductor.models import ModelInfo as MI
    c.catalog.models = {"x/top": MI("x/top", "openrouter", "Top", 4, 20, tier="frontier"),
                        "x/cheap": MI("x/cheap", "openrouter", "Cheap", 0.05, 0.4, tier="fast")}
    c.catalog.set_roster(["x/top", "x/cheap"])
    d = c.router.pick(skill="coding", domain="general", difficulty="medium", budget="best")
    assert "Credits are low" in d.reason
    tid = c.start_task("hi", model="x/top")
    await c.wait(tid)
    c.send_message(tid, "again")
    await c.wait(tid)
    assert [e["type"] for e in c.store.load_events(tid)].count("credits_low") == 1


async def test_without_visibility_it_probes_instead_of_waiting_forever():
    m = CreditMonitor(key="", fetch=False)
    assert await m.wait_for_credits(poll=0.01) == "probe"



def test_in_flight_wording_is_recognized_without_metadata():
    body = {"error": {"code": 402, "message": "This request would exceed your available credits given your "
                      "current in-flight requests. Retry after in-flight requests settle, or add credits."}}
    assert classify_402(402, body).kind == "in_flight"


async def test_in_flight_errors_never_pause_everything(tmp_path, monkeypatch):
    """In-flight limits are about parallel requests, not an empty balance: no global 'out', no pause cards."""
    async def no_sleep(s):
        return None
    monkeypatch.setattr("conductor.agent.asyncio.sleep", no_sleep)

    class InFlightOnce(FakeLLM):
        hits = 1

        async def turn(self, **kw):
            if self.hits:
                self.hits -= 1
                raise CreditError("in_flight", "would exceed your available credits given your current in-flight requests")
            return await super().turn(**kw)

    c = make(tmp_path, InFlightOnce([[text("done")]]))
    tid = c.start_task("x")
    await asyncio.wait_for(c.wait(tid), 5)
    types = [e["type"] for e in c.store.load_events(tid)]
    assert "paused" not in types and c.store.get_task(tid)["status"] == "done"
    assert c.credits.status()["level"] != "out"


async def test_limiter_narrows_on_in_flight_and_recovers():
    from conductor.credits import AdaptiveLimiter
    lim = AdaptiveLimiter(start=4, recover_after=0)
    async with lim:
        async with lim:
            lim.shrink()
    assert lim.limit == 1
    await lim.succeeded()
    assert lim.limit == 2


async def test_idle_balance_checks_are_cached(monkeypatch):
    m = CreditMonitor(key="k", management_key="m", low=2.0)
    calls = []
    monkeypatch.setattr(m, "_check_sync", lambda: calls.append(1) or {
        "available": 5.0, "source": "account balance", "checked": __import__("time").time(), "error": "", "spent_today": 0})
    await m.check()
    await m.check(max_age=15 * 60)      # idle: the cached value is fine
    assert len(calls) == 1
    m.state["checked"] -= 120
    await m.check(max_age=15 * 60)      # still within 15 minutes
    assert len(calls) == 1
    await m.check()                      # busy (60s default): stale, so re-read
    assert len(calls) == 2


async def test_paused_wait_backs_off(monkeypatch):
    m = CreditMonitor(key="k", low=2.0)
    waits = []

    async def fake_wait_for(aw, timeout):
        waits.append(timeout)
        aw.close()
        raise asyncio.TimeoutError
    checks = iter([0.0, 0.0, 0.0, 9.0])

    async def fake_check(force=False, max_age=None):
        m.state["available"] = next(checks)
        return m.status()
    monkeypatch.setattr("conductor.credits.asyncio.wait_for", fake_wait_for)
    monkeypatch.setattr(m, "check", fake_check)
    assert await m.wait_for_credits(poll=60) == "credits"
    assert waits[0] == 60 and waits[1] > waits[0] and waits[2] > waits[1]


def test_idle_page_polling_does_not_hit_openrouter(tmp_path):
    from fastapi.testclient import TestClient
    from conductor.server import create_app
    c = make(tmp_path, FakeLLM([]))
    c.credits.fetch, c.credits.key = True, "k"
    hits = []
    c.credits._check_sync = lambda: hits.append(1) or {"available": 5.0, "source": "x",
                                                         "checked": __import__("time").time(), "error": "", "spent_today": 0}
    with TestClient(create_app(c, run_scheduler=False)) as client:
        for _ in range(5):
            client.get("/api/credits")
    assert len(hits) == 1
