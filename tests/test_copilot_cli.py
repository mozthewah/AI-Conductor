"""GitHub Copilot CLI on your Copilot subscription, as a fallback worker (optionally through Headroom)."""

import asyncio
import json
import stat
import subprocess
import sys

from fastapi.testclient import TestClient

from conductor import copilot_cli
from conductor.agent import Conductor
from conductor.config import Settings
from conductor.credits import CreditError
from conductor.models import ModelInfo
from conductor.server import create_app

from fakes import FakeLLM, text

GPT = ModelInfo("openai/gpt-6.1-sol", "openrouter", "GPT-6.1 Sol", 2, 10, tier="frontier")
OPUS = ModelInfo("anthropic/claude-opus-5.5", "openrouter", "Claude Opus 5.5", 5, 25, tier="frontier")

FAKE_COPILOT = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_COPILOT_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "hook": os.environ.get("CONDUCTOR_HOOK_URL")}) + "\n")
mode = os.environ.get("FAKE_COPILOT_MODE", "")
model = args[args.index("--model") + 1]
if mode == "model" and model != "auto":
    print(f"Error: Model '{model}' is not available.", file=sys.stderr); sys.exit(1)
if mode == "quota":
    print("Error: You have exceeded your premium request quota.", file=sys.stderr); sys.exit(1)
print("Done via Copilot.")
'''

FAKE_HEADROOM = r'''#!PYTHON
import os, sys, subprocess, shutil
print("  Starting Headroom proxy...")           # Headroom's own status lines go to stdout
args = sys.argv[sys.argv.index("--") + 1:]
code = subprocess.call([shutil.which("copilot"), *args])
print("  Proxy stopped.")
sys.exit(code)
'''


def write(path, body):
    path.write_text(body.replace("PYTHON", sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def install(tmp_path, monkeypatch, mode=""):
    exe = write(tmp_path / "copilot", FAKE_COPILOT)
    log = tmp_path / "copilot.jsonl"
    monkeypatch.setenv("FAKE_COPILOT_LOG", str(log))
    monkeypatch.setenv("FAKE_COPILOT_MODE", mode)
    monkeypatch.setattr(copilot_cli, "find", lambda path="": str(exe))
    return log


def calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


class OutOfCredits(FakeLLM):
    async def turn(self, **kw):
        self.calls.append({"model": kw["model"].id})
        raise CreditError("out", "Insufficient credits")


def L(difficulty="medium"):
    return {"skill": "coding", "domain": "general", "difficulty": difficulty, "needs_vision": False,
            "parts": [{"skill": "coding", "domain": "general"}], "summary": ""}


def make(tmp_path, llm, models=(GPT,), **kw):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", compose="off", learning=False, **kw)
    c = Conductor(s, llm=llm)
    c.catalog.models = {m.id: m for m in models}
    c.catalog.set_roster([m.id for m in models])
    return c


def test_names_and_families():
    assert copilot_cli.model_name("openai/gpt-6.1-sol") == "gpt-6.1-sol"
    assert copilot_cli.model_name("anthropic/claude-opus-5.5") == "claude-opus-5.5"
    assert copilot_cli.model_name("z-ai/glm-5.3") == "auto"
    assert copilot_cli.model_name("openai/gpt-6.1-sol", "gpt-5.5") == "gpt-5.5"
    assert copilot_cli.serves("google/gemini-4-argon") and not copilot_cli.serves("z-ai/glm-5.3")
    cmd = copilot_cli.CopilotCLI(mode="fallback").command(prompt="hi", model="auto")
    assert "--allow-all-tools" not in cmd and "--yolo" not in cmd and "-s" in cmd  # the hook decides


async def test_gpt_task_out_of_credits_continues_on_copilot(tmp_path, monkeypatch):
    log = install(tmp_path, monkeypatch)
    c = make(tmp_path, OutOfCredits([], label=L()))
    tid = c.start_task("build the thing")
    await asyncio.wait_for(c.wait(tid), 10)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["answer"] == "Done via Copilot."
    esc = [e["data"] for e in c.store.load_events(tid) if e["type"] == "escalated"][-1]
    assert esc["by"] == "copilot" and "Copilot subscription" in esc["reason"]
    call = calls(log)[0]
    assert call["args"][call["args"].index("--model") + 1] == "gpt-6.1-sol"
    assert "build the thing" in call["args"][call["args"].index("-p") + 1]


async def test_unknown_model_falls_back_to_auto(tmp_path, monkeypatch):
    log = install(tmp_path, monkeypatch, mode="model")
    c = make(tmp_path, OutOfCredits([], label=L()))
    tid = c.start_task("build the thing")
    await asyncio.wait_for(c.wait(tid), 10)
    assert c.store.get_task(tid)["answer"] == "Done via Copilot."
    assert [x["args"][x["args"].index("--model") + 1] for x in calls(log)] == ["gpt-6.1-sol", "auto"]


async def test_claude_task_uses_copilot_when_claude_code_is_missing(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)  # (the `claude` CLI is hidden by conftest)
    c = make(tmp_path, OutOfCredits([], label=L()), models=(OPUS,))
    tid = c.start_task("build the thing")
    await asyncio.wait_for(c.wait(tid), 10)
    assert c.store.get_task(tid)["answer"] == "Done via Copilot."


async def test_quota_explains_and_pauses(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, mode="quota")
    c = make(tmp_path, OutOfCredits([], label=L()))
    tid = c.start_task("build the thing")
    q = c.subscribe(tid)
    seen = []
    while True:
        ev = await asyncio.wait_for(q.get(), 10)
        seen.append(ev)
        if ev["type"] == "paused":
            break
    texts = [e["data"].get("text", "") for e in seen if e["type"] == "status"]
    assert any("Copilot couldn't continue" in t for t in texts)
    assert any("Couldn't hand this to a subscription" in t for t in texts)
    assert c._copilot_limited_until > 0
    for t in list(c._running.values()):
        t.cancel()


async def test_through_headroom_the_answer_is_clean(tmp_path, monkeypatch):
    from conductor.compression import HeadroomProxy
    install(tmp_path, monkeypatch)
    hr = write(tmp_path / "headroom", FAKE_HEADROOM)
    monkeypatch.setattr(HeadroomProxy, "executable", staticmethod(lambda: str(hr)))
    c = make(tmp_path, OutOfCredits([], label=L()))
    monkeypatch.setattr(c.copilot, "use_headroom", lambda: True)
    tid = c.start_task("build the thing")
    await asyncio.wait_for(c.wait(tid), 10)
    assert c.store.get_task(tid)["answer"] == "Done via Copilot."  # no "Starting Headroom proxy" noise
    assert any(e["kind"] == "model.call" and e.get("headroom") for e in c.audit.read())


def test_hook_relay(tmp_path, monkeypatch):
    run = lambda env: subprocess.run([sys.executable, "-m", "conductor.hook_relay"], input=json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}}), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ".", **env}).stdout.strip()
    assert run({}) == "{}"  # your own Copilot sessions: no decision
    assert json.loads(run({"CONDUCTOR_HOOK_URL": "http://127.0.0.1:9/nope"}))["behavior"] == "deny"  # fail closed


def test_hook_endpoint_shows_copilot_steps(tmp_path, monkeypatch):
    c = make(tmp_path, FakeLLM([]))
    tid = c.store.create_task("t")
    c.cli_runs["tok"] = tid
    c._copilot_tokens.add("tok")
    with TestClient(create_app(c, run_scheduler=False)) as client:
        r = client.post("/api/cli/hook/tok", json={"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                                                   "tool_input": {"command": "ls -la"}}).json()
    assert r["hookSpecificOutput"]["permissionDecision"] == "allow"
    shown = [e["data"] for e in c.store.load_events(tid) if e["type"] == "tool_call"]
    assert shown and shown[0]["summary"] == "$ ls -la" and shown[0]["via"] == "copilot"


def test_hook_file_is_inert_outside_conductor(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "cp"))
    p = copilot_cli.install_hook()
    assert p == tmp_path / "cp" / "hooks" / "conductor-approvals.json"
    cfg = json.loads(p.read_text())
    entry = cfg["hooks"]["PermissionRequest"][0]
    assert cfg["version"] == 1 and entry["args"] == ["-m", "conductor.hook_relay"] and entry["timeoutSec"] >= 600
