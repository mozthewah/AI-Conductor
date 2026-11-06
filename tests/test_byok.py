"""The management key: BYOK coverage, guardrails, activity - and moving to your own keys when credits run out."""

import asyncio
import hashlib

from conductor.account import Account, provider_slug
from conductor.agent import Conductor
from conductor.config import Settings
from conductor.credits import CreditError
from conductor.models import ModelInfo

from fakes import FakeLLM, text, tool_use

FRONTIER = ModelInfo("lab/frontier", "openrouter", "Frontier", 4, 20, tier="frontier")
GPT = ModelInfo("openai/gpt-x", "openrouter", "GPT X", 2, 10, tier="frontier")
FAST = ModelInfo("lab/fast", "openrouter", "Fast", 0.05, 0.4, tier="fast")


def L(skill, difficulty="medium", domain="general"):
    return {"skill": skill, "domain": domain, "difficulty": difficulty, "needs_vision": False,
            "parts": [{"skill": skill, "domain": domain}], "summary": ""}


def conductor(tmp_path, script=(), label=None, **kw):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="sk-or-v1-mine", compose="off", learning=False, **kw)
    c = Conductor(s, llm=FakeLLM(list(script), label=label or L("coding", "hard")))
    c.catalog.models = {m.id: m for m in (FRONTIER, GPT, FAST)}
    c.catalog.set_roster([FRONTIER.id, GPT.id, FAST.id])
    c.account.data.update({
        "byok": [{"provider": "openai", "name": "My OpenAI", "disabled": False, "allowed_models": None,
                  "allowed_api_key_hashes": None}],
        "endpoints": {FRONTIER.id: ["lab"], GPT.id: ["openai", "azure"], FAST.id: ["lab", "groq"]},
        "checked": 9e18,  # don't refresh in tests
    })
    return c


# ---------------------------------------------------------------- reading the account
def test_refresh_reads_byok_key_guardrail_activity_and_endpoints(tmp_path):
    a = Account(tmp_path, key="sk-or-v1-mine", management_key="mgmt")
    mine = hashlib.sha256(b"sk-or-v1-mine").hexdigest()
    pages = {
        "/byok": {"data": [{"id": "1", "provider": "openai", "label": "sk-...AbCd", "disabled": False},
                           {"id": "2", "provider": "mistral", "disabled": True},
                           {"id": "3", "provider": "anthropic", "allowed_api_key_hashes": ["someone-else"]}],
                  "total_count": 3},
        "/keys": {"data": [{"hash": "other", "name": "Other"}, {"hash": mine, "name": "Conductor", "limit": 20,
                                                                "limit_remaining": 5, "usage_monthly": 15}]},
        "/guardrails/assignments/keys": {"data": [{"key_hash": mine, "guardrail_id": "g1"}]},
        "/guardrails": {"data": [{"id": "g1", "name": "Home", "ignored_providers": ["deepinfra"], "limit_usd": 50}]},
        "/activity": {"data": [
            {"model": "openai/gpt-x", "usage": 0.5, "byok_usage_inference": 2.0, "requests": 3, "prompt_tokens": 100,
             "completion_tokens": 50, "provider_name": "OpenAI"},
            {"model": "openai/gpt-x", "usage": 0.25, "requests": 1, "prompt_tokens": 10, "completion_tokens": 5},
            {"model": "lab/fast", "usage": 0.1, "requests": 9}]},
        "/models/openai/gpt-x/endpoints": {"data": {"endpoints": [{"tag": "openai"}, {"tag": "azure/eastus"}]}},
        "/models/lab/cheap/endpoints": {"data": {"endpoints": [{"tag": "deepinfra/fp8"}]}},
    }
    seen = []

    def fake_get(path, key, **params):
        seen.append((path, key))
        return pages[path]
    a._get = fake_get
    a.refresh_sync(["openai/gpt-x", "lab/cheap"])
    assert a.data["key"]["name"] == "Conductor" and a.data["key_count"] == 2
    assert [b["provider"] for b in a.byok_keys()] == ["openai"]  # disabled + restricted-to-another-key dropped
    assert a.byok_for("openai/gpt-x") == ["openai"] and a.byok_for("lab/cheap") == []
    assert a.data["endpoints"]["openai/gpt-x"] == ["azure", "openai"]
    assert a.data["guardrail"]["name"] == "Home"
    assert "none of the providers" in a.blocked("lab/cheap")  # only served by a provider the guardrail ignores
    assert a.blocked("openai/gpt-x") is None
    gpt = a.data["activity"][0]
    assert gpt["model"] == "openai/gpt-x" and gpt["usage"] == 0.75 and gpt["byok_usage"] == 2.0 and gpt["requests"] == 4
    assert all(k == "mgmt" for p, k in seen if not p.startswith("/models/"))
    assert all(k == "sk-or-v1-mine" for p, k in seen if p.startswith("/models/"))
    # cached for next start
    b = Account(tmp_path, key="sk-or-v1-mine", management_key="mgmt")
    assert b.byok_for("openai/gpt-x") == ["openai"]


def test_byok_model_filters_and_fee():
    a = Account("/nonexistent", mode="auto")
    a.data.update({"byok": [{"provider": "openai", "allowed_models": ["openai/gpt-y"]}],
                   "endpoints": {"openai/gpt-x": ["openai"], "openai/gpt-y": ["openai"]}})
    assert a.byok_for("openai/gpt-x") == [] and a.byok_for("openai/gpt-y") == ["openai"]
    assert a.credit_cost("openai/gpt-y", 1.0) == 0.05 and a.credit_cost("openai/gpt-x", 1.0) == 1.0
    a.mode = "off"
    assert a.byok_for("openai/gpt-y") == [] and not a.has_byok
    assert provider_slug("amazon-bedrock/us") == "amazon-bedrock"


def test_guardrail_allowed_models():
    a = Account("/nonexistent")
    a.data["guardrail"] = {"name": "G", "allowed_models": ["openai/gpt-x"]}
    assert a.blocked("lab/fast") and a.blocked("openai/gpt-x") is None


# ---------------------------------------------------------------- routing
def test_guardrail_blocked_models_are_never_picked(tmp_path):
    c = conductor(tmp_path)
    c.account.data["guardrail"] = {"name": "G", "ignored_models": [FRONTIER.id]}
    d = c.router.pick(skill="coding", domain="general", difficulty="hard", budget="best")
    assert d.model != FRONTIER.id and FRONTIER.id not in [x["model"] for x in d.candidates]


def test_prefer_mode_and_low_credits_lean_on_byok(tmp_path):
    c = conductor(tmp_path, byok="prefer")
    d = c.router.pick(skill="coding", domain="general", difficulty="hard", budget="frugal")
    assert d.model == GPT.id and d.byok == ["openai"] and "your own openai key" in d.reason
    c2 = conductor(tmp_path / "2")  # auto: normal routing ignores BYOK...
    d2 = c2.router.pick(skill="coding", domain="general", difficulty="hard", budget="frugal")
    c2.credits.state["available"] = 0.5  # ...until credits run low
    d3 = c2.router.pick(skill="coding", domain="general", difficulty="hard", budget="frugal")
    assert d3.model == GPT.id
    assert d2.candidates[0]["byok"] in ([], ["openai"])


# ---------------------------------------------------------------- out of credits
async def test_out_of_credits_moves_to_a_byok_model_instead_of_pausing(tmp_path):
    c = conductor(tmp_path, [[tool_use("list_files", path=".")], [text("Finished.")]])
    c.credits.state["out"] = True
    tid = c.start_task("refactor everything")
    await asyncio.wait_for(c.wait(tid), 5)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["model"] == GPT.id
    ev = [e for e in c.store.load_events(tid) if e["type"] == "escalated"]
    if ev:  # the router may already have picked the covered model at the start
        assert ev[-1]["data"]["direction"] == "byok" and ev[-1]["data"]["providers"] == ["openai"]
    assert not any(e["type"] == "paused" for e in c.store.load_events(tid))
    assert all(call["providers"] == ["openai"] and call["web"] is False for call in c.llm.calls)


class OutOnce(FakeLLM):
    def __init__(self, script, **kw):
        super().__init__(script, **kw)
        self.failed = False

    async def turn(self, **kw):
        if not self.failed:
            self.failed = True
            self.calls.append({"model": kw["model"].id, "providers": kw.get("providers"), "failed": True})
            raise CreditError("out", "Insufficient credits")
        return await super().turn(**kw)


async def test_credit_error_mid_task_switches_and_reruns_the_step(tmp_path):
    c = conductor(tmp_path)
    c.catalog.models[GPT.id] = ModelInfo(GPT.id, "openrouter", "GPT X", 2, 10, tier="strong")  # not the first pick
    c.llm = OutOnce([[text("Done on my own key.")]], label=L("coding", "hard"))
    tid = c.start_task("refactor everything")
    await asyncio.wait_for(c.wait(tid), 5)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["answer"] == "Done on my own key."
    first, second = c.llm.calls[0], c.llm.calls[1]
    assert first["model"] == FRONTIER.id and first["providers"] is None
    assert second["model"] == GPT.id and second["providers"] == ["openai"]
    esc = [e["data"] for e in c.store.load_events(tid) if e["type"] == "escalated"]
    assert esc and esc[-1]["direction"] == "byok"
    # the new model is told why it's here
    assert any("BYOK" in str(m["content"]) for m in c.store.load_messages(tid) if m["role"] == "user")


async def test_no_byok_coverage_still_pauses(tmp_path):
    c = conductor(tmp_path)
    c.account.data["byok"] = []
    c.llm = OutOnce([[text("ok")]], label=L("coding", "hard"))
    tid = c.start_task("refactor")
    q = c.subscribe(tid)
    while True:
        ev = await asyncio.wait_for(q.get(), 5)
        if ev["type"] == "paused":
            c.credits.resume_now()
            break
    await asyncio.wait_for(c.wait(tid), 5)
    assert c.store.get_task(tid)["status"] == "done"


def test_account_and_models_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from conductor.server import create_app
    c = conductor(tmp_path, openrouter_management_key="mgmt")
    c.account.management_key = "mgmt"
    c.account.data["guardrail"] = {"name": "G", "ignored_models": [FAST.id]}
    c.account.data["catalog_ids_by_author"] = {"openai": ["openai/gpt-new"]}
    c.catalog.models["openai/gpt-new"] = ModelInfo("openai/gpt-new", "openrouter", "GPT New", 3, 12)
    c.account.data["endpoints"]["openai/gpt-new"] = ["openai"]
    with TestClient(create_app(c, run_scheduler=False)) as client:
        a = client.get("/api/account").json()
        roster = {m["id"]: m for m in client.get("/api/models").json()["roster"]}
        credits = client.get("/api/credits").json()
    assert a["has_management_key"] and [b["provider"] for b in a["byok"]] == ["openai"]
    assert [x["model"] for x in a["byok_covered"]] == [GPT.id]
    assert [x["model"] for x in a["byok_suggestions"]] == ["openai/gpt-new"]
    assert roster[GPT.id]["byok"] == ["openai"] and roster[FAST.id]["blocked"]
    assert credits["byok"] == ["openai"]
