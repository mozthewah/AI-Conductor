"""Headroom: compress bulky tool output once, as it arrives, so the prompt cache stays intact."""

import asyncio
import json

import pytest

from conductor.agent import Conductor
from conductor.compression import Compressor
from conductor.config import Settings
from conductor.models import ModelInfo

from fakes import FakeLLM, text, tool_use

headroom = pytest.importorskip("headroom")

M = ModelInfo("anthropic/claude-sonnet-5.5", "openrouter", "Sonnet", 3, 15, tier="strong")
LOGS = "\n".join(f"2026-11-06 01:{i % 60:02d}:00 INFO request id={i} path=/api/x status=200 ms={i % 50}"
                 for i in range(800)) + "\nERROR something broke: KeyError: 'user_id'\n"


def blocks(name, tid="t1", **inp):
    return [{"type": "tool_use", "id": tid, "name": name, "input": inp}]


def test_compresses_big_shell_output_and_keeps_the_original(tmp_path):
    c = Compressor("auto", tmp_path)
    assert c.available
    res = [{"type": "tool_result", "tool_use_id": "t1", "content": LOGS}]
    out, stats = c.compress_results("task1", "find what failed", blocks("run_shell", command="cat app.log"), res, M.id)
    body = out[0]["content"]
    assert len(body) < len(LOGS) / 5 and "KeyError" in body and "Shortened by Headroom" in body
    assert stats and stats[0]["tokens_saved"] > 1000
    full = tmp_path / stats[0]["full"]
    assert full.read_text() == LOGS  # nothing lost
    assert res[0]["content"] == LOGS  # the caller's list isn't mutated


def test_leaves_file_reads_errors_and_small_output_alone(tmp_path):
    c = Compressor("auto", tmp_path)
    reads = [{"type": "tool_result", "tool_use_id": "t1", "content": LOGS}]
    assert c.compress_results("t", "x", blocks("read_file", path="app.log"), reads, M.id)[0] == reads
    err = [{"type": "tool_result", "tool_use_id": "t1", "content": LOGS, "is_error": True}]
    assert c.compress_results("t", "x", blocks("run_shell", command="x"), err, M.id)[0] == err
    small = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok\n" * 20}]
    assert c.compress_results("t", "x", blocks("run_shell", command="x"), small, M.id)[0] == small


def test_off_means_off(tmp_path):
    assert not Compressor("off", tmp_path).available


async def test_in_the_loop_history_stays_byte_stable(tmp_path):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, openrouter_key="k", compose="off", learning=False)
    (tmp_path / "ws").mkdir(parents=True)
    (tmp_path / "ws" / "app.log").write_text(LOGS)
    llm = FakeLLM([[tool_use("run_shell", command="cat app.log")],
                   [tool_use("run_shell", command="echo second")],
                   [text("It was a KeyError.")]],
                  label={"skill": "coding", "domain": "general", "difficulty": "medium", "needs_vision": False,
                         "parts": [{"skill": "coding", "domain": "general"}], "summary": ""})
    c = Conductor(s, llm=llm)
    c.catalog.models = {M.id: M}
    c.catalog.set_roster([M.id])
    tid = c.start_task("find what failed in app.log")
    await asyncio.wait_for(c.wait(tid), 20)
    assert c.store.get_task(tid)["answer"] == "It was a KeyError."
    second, third = llm.calls[1]["messages"], llm.calls[2]["messages"]
    sent = json.dumps(second[-1]["content"])
    assert "Shortened by Headroom" in sent and "KeyError" in sent and len(sent) < len(LOGS) / 3
    # the turn the model already saw is sent again unchanged (prompt cache keeps hitting)
    assert json.dumps(third[:len(second)]) == json.dumps(second)
    assert any(e["kind"] == "tool.compressed" for e in c.audit.read())


def test_proxy_manager_decisions(monkeypatch):
    from conductor import compression
    from conductor.compression import HeadroomProxy
    started = []
    monkeypatch.setattr(HeadroomProxy, "start", lambda self: started.append(self.setting) or True)
    # already running (yours or ours): just use it
    monkeypatch.setattr(compression, "proxy_url", lambda *a, **k: "http://127.0.0.1:8787")
    assert HeadroomProxy("auto").ensure() == "http://127.0.0.1:8787" and started == []
    # not running: auto starts it; connect / off never do
    monkeypatch.setattr(compression, "proxy_url", lambda *a, **k: None)
    assert HeadroomProxy("auto").ensure() is None and started == ["auto"]
    HeadroomProxy("connect").ensure()
    HeadroomProxy("off").ensure()
    assert started == ["auto"]
    # a proxy Conductor didn't start is never stopped
    HeadroomProxy("auto").stop()


async def test_cli_fallback_starts_a_dead_proxy_for_next_time(tmp_path, monkeypatch):
    from conductor.compression import HeadroomProxy
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", browser=False, fetch_catalog=False)
    c = Conductor(s, llm=FakeLLM([]))
    starts = []
    monkeypatch.setattr(HeadroomProxy, "start", lambda self: starts.append(1) or True)
    assert c._proxy_for_cli() is None and starts == [1]   # not up: start it, this run goes direct
    c.settings.headroom_proxy = "connect"
    c.hr_proxy = HeadroomProxy("connect")
    assert c._proxy_for_cli() is None and starts == [1]   # yours to run: never started by Conductor
