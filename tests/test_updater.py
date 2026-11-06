"""The update button: git pull --ff-only, reinstall when dependencies change, then restart."""

import asyncio
import subprocess

from fastapi.testclient import TestClient

from conductor import updater
from conductor.agent import Conductor
from conductor.config import Settings
from conductor.server import create_app

from fakes import FakeLLM, text


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def repos(tmp_path):
    remote, work, me = tmp_path / "remote.git", tmp_path / "work", tmp_path / "me"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    for r in (work,):
        git(r, "config", "user.email", "t@t"); git(r, "config", "user.name", "t")
    (work / "a.txt").write_text("1")
    git(work, "add", "."); git(work, "commit", "-qm", "first"); git(work, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(remote), str(me)], check=True, capture_output=True)
    return work, me


def test_status_and_pull(tmp_path, monkeypatch):
    work, me = repos(tmp_path)
    monkeypatch.setattr(updater, "REPO", me)
    st = updater.status()
    assert st["can_update"] and st["behind"] == 0 and st["branch"] == "main"
    (work / "a.txt").write_text("2")
    git(work, "commit", "-qam", "second change"); git(work, "push", "-q", "origin", "HEAD:main")
    st = updater.status()
    assert st["behind"] == 1 and st["changes"][0].endswith("second change")
    r = updater.update()
    assert r["ok"] and r["updated"] and (me / "a.txt").read_text() == "2"
    assert updater.update() == {"ok": True, "updated": False, "log": "Already up to date."}


def test_dependency_change_reinstalls(tmp_path, monkeypatch):
    work, me = repos(tmp_path)
    monkeypatch.setattr(updater, "REPO", me)
    (work / "pyproject.toml").write_text("[project]\nname='x'\n")
    git(work, "add", "."); git(work, "commit", "-qm", "deps"); git(work, "push", "-q", "origin", "HEAD:main")
    ran = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if "pip" in cmd:
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "installed", "")
        return real_run(cmd, **kw)
    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    r = updater.update()
    assert r["ok"] and ran and "-e" in ran[0] and "pip install" in r["log"]


def test_local_edits_that_conflict_stop_the_pull(tmp_path, monkeypatch):
    work, me = repos(tmp_path)
    monkeypatch.setattr(updater, "REPO", me)
    (work / "a.txt").write_text("remote")
    git(work, "commit", "-qam", "remote edit"); git(work, "push", "-q", "origin", "HEAD:main")
    (me / "a.txt").write_text("my local edit")
    r = updater.update()
    assert not r["ok"] and "failed" in r["error"] and (me / "a.txt").read_text() == "my local edit"


def test_not_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "REPO", tmp_path)
    assert updater.status()["can_update"] is False


def test_update_endpoint_restarts_and_guards_running_tasks(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, learning=False)
    c = Conductor(s, llm=FakeLLM([[text("ok")]]))
    restarted = []
    monkeypatch.setattr(updater, "update", lambda: {"ok": True, "updated": True, "from": "a", "to": "b", "log": ""})
    monkeypatch.setattr(updater, "restart", lambda: restarted.append(1))
    with TestClient(create_app(c, run_scheduler=False)) as client:
        c._running["fake"] = asyncio.Future(loop=asyncio.new_event_loop())
        assert client.post("/api/update", json={}).status_code == 409  # a task is running: ask first
        r = client.post("/api/update", json={"force": True}).json()
        assert r["restarting"]
        c._running.clear()
        import time
        time.sleep(1.2)
    assert restarted == [1]
    kinds = [e["kind"] for e in c.audit.read()]
    assert "system.update" in kinds and "system.restart" in kinds
