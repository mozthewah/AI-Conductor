"""First-time setup and importing memories/skills from other assistants."""

import json

from fastapi.testclient import TestClient

from conductor import onboarding
from conductor.agent import Conductor
from conductor.config import Settings
from conductor.server import create_app

from fakes import FakeLLM

SKILL = "---\nname: deploy-checklist\ndescription: Steps to ship safely\n---\n1. Run tests\n2. Tag release\n"


def fake_home(tmp_path):
    home = tmp_path / "home"
    ws = home / ".openclaw" / "workspace"
    ws.mkdir(parents=True)
    (ws / "USER.md").write_text("# User\n- Name: Sam\n- Prefers short answers with code first\n- Works at a small studio\n")
    (ws / "MEMORY.md").write_text("## Projects\n- Building Burrow, a self-hosted error tracker\n```\ncode block ignored\n```\n")
    (home / ".openclaw" / "skills" / "deploy").mkdir(parents=True)
    (home / ".openclaw" / "skills" / "deploy" / "SKILL.md").write_text(SKILL)
    (home / ".openclaw" / ".env").write_text("OPENROUTER_API_KEY=sk-or-v1-abcdef0123456789abcdef\nOTHER=x\n")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("- Always use uv for Python projects\n")
    return home


def test_extract_items_and_kinds():
    items = onboarding.extract_items("""---
title: x
---
# About me
- My name is Sam and I live in Lisbon
- I prefer TypeScript over JavaScript
1. Working on a Go project called Burrow
- ok
""")
    got = {i["content"]: i["kind"] for i in items}
    assert got == {"My name is Sam and I live in Lisbon": "person", "I prefer TypeScript over JavaScript": "preference",
                   "Working on a Go project called Burrow": "project"}


def test_scan_finds_agents_files_skills_and_masked_keys(tmp_path):
    home = fake_home(tmp_path)
    srcs = {s["id"]: s for s in onboarding.scan(home)}
    assert set(srcs) == {"openclaw", "claude"}
    oc = srcs["openclaw"]
    assert [s["name"] for s in oc["skills"]] == ["deploy-checklist"]
    assert oc["keys"] == {"OPENROUTER_API_KEY": "sk-or-v1-a…cdef"}  # masked
    items = onboarding.preview(oc)
    assert any("Prefers short answers" in i["content"] and i["kind"] == "preference" for i in items)
    assert not any("code block" in i["content"] for i in items)


def test_write_env_replaces_and_keeps(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nOPENROUTER_API_KEY=old\nCONDUCTOR_BUDGET=frugal\n")
    onboarding.write_env({"OPENROUTER_API_KEY": "new", "ANTHROPIC_API_KEY": "a"}, env)
    assert env.read_text() == "# comment\nOPENROUTER_API_KEY=new\nCONDUCTOR_BUDGET=frugal\nANTHROPIC_API_KEY=a\n"
    assert env.stat().st_mode & 0o077 == 0


def make(tmp_path):
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", browser=False, fetch_catalog=False)
    return Conductor(s, llm=FakeLLM([]))


def test_setup_import_flow(tmp_path, monkeypatch):
    home = fake_home(tmp_path)
    monkeypatch.setattr(onboarding, "HOME", home)
    monkeypatch.chdir(tmp_path)
    c = make(tmp_path)
    with TestClient(create_app(c, run_scheduler=False)) as local, \
            TestClient(create_app(c, run_scheduler=False), client=("192.168.1.5", 1)) as phone:
        st = local.get("/api/setup").json()
        assert st["done"] is False and {s["id"] for s in st["sources"]} == {"openclaw", "claude"}
        assert st["sources"][0]["keys"] or st["sources"][1]["keys"]
        # preview + import (skills only from paths the scan found)
        pv = local.post("/api/setup/preview", json={"source": "openclaw"}).json()
        skill = pv["skills"][0]["path"]
        r = local.post("/api/setup/import", json={"memories": pv["memories"][:2], "skills": [skill, "/etc/passwd"],
                                                  "source": "openclaw"}).json()
        assert r == {"memories": 2, "skills": 1}
        assert c.skills.get("deploy-checklist") and "Run tests" in c.skills.get("deploy-checklist").body
        # pasted from ChatGPT
        pasted = local.post("/api/setup/preview", json={"text": "- I'm vegetarian\n- I prefer metric units"}).json()
        assert [m["kind"] for m in pasted["memories"]] == ["person", "preference"]
        # keys: computer only (even for a signed-in phone); from another agent's .env
        phone.cookies.set("conductor_access", local.get("/api/access").json()["token"])
        assert phone.post("/api/setup/keys", json={"values": {"OPENROUTER_API_KEY": "x" * 20}}).status_code == 403
        assert phone.get("/api/setup").json()["sources"][0]["keys"] == {}
        r = local.post("/api/setup/keys", json={"from_source": "openclaw", "keys": ["OPENROUTER_API_KEY"]}).json()
        assert r["saved"] == ["OPENROUTER_API_KEY"] and r["restart_needed"]
        assert "OPENROUTER_API_KEY=sk-or-v1-abcdef0123456789abcdef" in (tmp_path / ".env").read_text()
        # about you + budget, then finish
        r = local.post("/api/setup/prefs", json={"about": "I prefer concise replies\nI work at a studio", "budget": "frugal"}).json()
        assert r["memories"] == 2 and c.settings.router_budget == "frugal"
        local.post("/api/setup/done")
        assert local.get("/api/setup").json()["done"] is True
    assert any(e["kind"] == "setup.imported" for e in c.audit.read())
    assert not any("sk-or-v1-abcdef0123456789abcdef" in json.dumps(e) for e in c.audit.read())
