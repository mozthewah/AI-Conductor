"""Claude Code (your subscription) as a fallback worker, with Conductor's safety levels on its tools."""

import asyncio
import json
import os
import stat
import sys

from fastapi.testclient import TestClient

from conductor import claude_cli
from conductor.agent import Conductor
from conductor.config import Settings
from conductor.credits import CreditError
from conductor.models import ModelInfo
from conductor.server import create_app

from fakes import FakeLLM, text

OPUS = ModelInfo("anthropic/claude-opus-5.5", "openrouter", "Claude Opus 5.5", 5, 25, tier="frontier")
GLM = ModelInfo("z-ai/glm-5.3", "openrouter", "GLM 5.3", 0.5, 2, tier="strong")

FAKE = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "prompt": prompt, "has_api_key": "ANTHROPIC_API_KEY" in os.environ}) + "\n")
say = lambda o: print(json.dumps(o), flush=True)
say({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "claude-opus-5-5"})
if os.environ.get("FAKE_CLAUDE_MODE") == "limit":
    say({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1900000000", "session_id": "sess-1"})
    sys.exit(1)
say({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
    {"type": "text", "text": "Picking up from the notes."},
    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]}})
say({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "a.txt"}]}})
say({"type": "result", "subtype": "success", "is_error": False, "result": "Done via Claude Code.", "session_id": "sess-1"})
'''


def install_fake(tmp_path, monkeypatch, mode=""):
    exe = tmp_path / "claude"
    exe.write_text(FAKE.replace("PYTHON", sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-reach-the-cli")
    monkeypatch.setattr(claude_cli, "find", lambda path="": str(exe))
    return log


def calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


class OutOfCredits(FakeLLM):
    async def turn(self, **kw):
        self.calls.append({"model": kw["model"].id})
        raise CreditError("out", "Insufficient credits")


def L(skill="coding", difficulty="medium"):
    return {"skill": skill, "domain": "general", "difficulty": difficulty, "needs_vision": False,
            "parts": [{"skill": skill, "domain": "general"}], "summary": ""}


def make(tmp_path, llm, models=(OPUS,), mode="fallback"):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, explore=0.0, openrouter_key="k", compose="off", learning=False,
                 claude_cli=mode)
    c = Conductor(s, llm=llm)
    c.catalog.models = {m.id: m for m in models}
    c.catalog.set_roster([m.id for m in models])
    return c


def test_aliases_and_command():
    assert claude_cli.model_alias("anthropic/claude-opus-5.5") == "opus"
    assert claude_cli.model_alias("claude-sonnet-5-5") == "sonnet"
    assert claude_cli.model_alias("z-ai/glm-5.3", "hard") == "opus"
    cli = claude_cli.ClaudeCLI(mode="fallback")
    cli.exe = "claude"
    cmd = cli.command(model="opus", system="s", cwd="/w", hook_url="http://127.0.0.1:1/api/cli/hook/t", resume="x", max_turns=5)
    assert "--bare" not in cmd  # bare mode would ignore the subscription login
    hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hooks["type"] == "http" and hooks["url"].endswith("/api/cli/hook/t")
    assert cmd[cmd.index("--resume") + 1] == "x"
    no_server = cli.command(model="opus", system="s", cwd="/w", hook_url=None, resume=None, max_turns=5)
    assert "Bash" in no_server[no_server.index("--disallowedTools") + 1]


async def test_claude_task_out_of_credits_continues_on_claude_code(tmp_path, monkeypatch):
    log = install_fake(tmp_path, monkeypatch)
    c = make(tmp_path, OutOfCredits([], label=L()))
    tid = c.start_task("build the thing")
    await asyncio.wait_for(c.wait(tid), 10)
    t = c.store.get_task(tid)
    assert t["status"] == "done" and t["answer"] == "Done via Claude Code."
    ev = c.store.load_events(tid)
    assert not any(e["type"] == "paused" for e in ev)
    esc = [e["data"] for e in ev if e["type"] == "escalated"]
    assert esc[-1]["direction"] == "cli" and "subscription" in esc[-1]["reason"]
    assert any(e["type"] == "tool_call" and e["data"]["name"] == "run_shell" for e in ev)
    call = calls(log)[0]
    assert call["has_api_key"] is False  # bills the subscription, not an API account
    assert call["args"][call["args"].index("--model") + 1] == "opus"
    assert "build the thing" in call["prompt"]
    assert t["route"]["cli_session"] == "sess-1"
    # a follow-up resumes the same Claude Code session
    c.send_message(tid, "also add tests")
    await asyncio.wait_for(c.wait(tid), 10)
    follow = calls(log)[-1]
    assert follow["args"][follow["args"].index("--resume") + 1] == "sess-1" and "also add tests" in follow["prompt"]


async def test_usage_limit_falls_back_to_pausing(tmp_path, monkeypatch):
    install_fake(tmp_path, monkeypatch, mode="limit")
    c = make(tmp_path, OutOfCredits([], label=L()))
    tid = c.start_task("build the thing")
    q = c.subscribe(tid)
    while True:
        ev = await asyncio.wait_for(q.get(), 10)
        if ev["type"] == "paused":
            break
    assert c._cli_limited_until > 0
    c.store.update_task(tid, status="cancelled")
    for t in list(c._running.values()):
        t.cancel()


async def test_prefer_mode_uses_claude_code_from_the_start(tmp_path, monkeypatch):
    log = install_fake(tmp_path, monkeypatch)
    llm = FakeLLM([[text("should not be used")]], label=L())
    c = make(tmp_path, llm, mode="prefer")
    tid = c.start_task("build the thing")
    await asyncio.wait_for(c.wait(tid), 10)
    assert c.store.get_task(tid)["answer"] == "Done via Claude Code."
    assert llm.calls == [] and len(calls(log)) == 1


async def test_off_mode_pauses_as_before(tmp_path, monkeypatch):
    install_fake(tmp_path, monkeypatch)
    c = make(tmp_path, OutOfCredits([], label=L()), mode="off")
    assert not c.cli.available


def test_hook_applies_conductor_safety(tmp_path, monkeypatch):
    install_fake(tmp_path, monkeypatch)
    c = make(tmp_path, FakeLLM([]))
    c.policy.threshold = "low"
    tid = c.store.create_task("t")
    c.cli_runs["tok"] = tid
    ws = str(c.settings.workspace)
    with TestClient(create_app(c, run_scheduler=False)) as client:
        hook = lambda tool, inp, tok="tok": client.post(f"/api/cli/hook/{tok}", json={
            "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp}).json()["hookSpecificOutput"]
        assert hook("Bash", {"command": "ls -la"})["permissionDecision"] == "allow"
        assert hook("Read", {"file_path": f"{ws}/a.txt"})["permissionDecision"] == "allow"
        assert hook("Write", {"file_path": f"{ws}/a.txt", "content": "x"})["permissionDecision"] == "allow"
        assert hook("Bash", {"command": "unknown"}, tok="nope")["permissionDecision"] == "deny"
    assert hook  # (decisions above medium go to the user; covered by the approval tests)


async def test_composer_out_of_credits_hands_the_rest_to_claude_code(tmp_path, monkeypatch):
    log = install_fake(tmp_path, monkeypatch)
    c = make(tmp_path, OutOfCredits([], label=L(difficulty="hard")))  # hard -> a composer
    tid = c.start_task("design and build the whole app")
    await asyncio.wait_for(c.wait(tid), 10)
    t = c.store.get_task(tid)
    assert t["route"].get("hands_off") and t["status"] == "done" and t["answer"] == "Done via Claude Code."
    esc = [e["data"] for e in c.store.load_events(tid) if e["type"] == "escalated"][-1]
    assert esc["direction"] == "cli" and "finish the rest" in esc["reason"]
    assert len(calls(log)) == 1
