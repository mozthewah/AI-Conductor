"""Other devices on the network need the passcode; this computer doesn't. Plus the audit log API."""

from fastapi.testclient import TestClient

from conductor.access import is_local, load_token, same_origin
from conductor.agent import Conductor
from conductor.config import Settings
from conductor.server import create_app

from fakes import FakeLLM, text

PHONE = ("192.168.1.50", 40000)


def make(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_ACCESS_TOKEN", raising=False)
    s = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "ws", approval="high", browser=False,
                 fetch_catalog=False, learning=False)
    return Conductor(s, llm=FakeLLM([[text("ok")]]))


def test_helpers(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_ACCESS_TOKEN", raising=False)
    assert is_local("127.0.0.1") and is_local("::1") and not is_local("192.168.1.5")
    t = load_token(tmp_path)
    assert len(t) == 19 and load_token(tmp_path) == t  # stable across restarts
    assert (tmp_path / "access_token").stat().st_mode & 0o077 == 0
    assert same_origin(None, "x:1") and same_origin("http://x:1", "x:1") and not same_origin("http://evil.com", "x:1")


def test_phone_needs_the_passcode(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    app = create_app(c, run_scheduler=False)
    with TestClient(app) as local:  # this computer: no passcode, and it can see the passcode
        token = local.get("/api/access").json()["token"]
        assert local.get("/api/tasks").status_code == 200
    with TestClient(app, client=PHONE, follow_redirects=False) as phone:
        assert phone.get("/api/tasks").status_code == 401
        r = phone.get("/models")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert phone.get("/login").status_code == 200
        assert phone.get("/api/access").status_code == 401  # not even signed-in devices see it... until signed in
        bad = phone.post("/login", content="passcode=nope&next=/models",
                         headers={"content-type": "application/x-www-form-urlencoded"})
        assert bad.status_code == 401
        ok = phone.post("/login", content=f"passcode={token}&next=/models",
                        headers={"content-type": "application/x-www-form-urlencoded"})
        assert ok.status_code == 303 and ok.headers["location"] == "/models"
        assert phone.get("/api/tasks").status_code == 200  # cookie works now
        assert phone.get("/api/access").status_code == 403  # the passcode is only shown on the computer
    with TestClient(app, client=("192.168.1.51", 1), follow_redirects=False) as tablet:
        r = tablet.get(f"/t/abc?key={token}&x=1")
        assert r.status_code == 303 and r.headers["location"] == "/t/abc?x=1"
        assert tablet.get("/api/tasks").status_code == 200
    kinds = [e["kind"] for e in c.audit.read()]
    assert "auth.wrong_passcode" in kinds and kinds.count("auth.signed_in") == 2
    assert token not in (tmp_path / "d" / "logs").joinpath(next(p.name for p in (tmp_path / "d" / "logs").iterdir())).read_text()


def test_lockout_after_many_wrong_tries(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    app = create_app(c, run_scheduler=False)
    with TestClient(app, client=PHONE, follow_redirects=False) as phone:
        for _ in range(10):
            phone.post("/login", content="passcode=nope", headers={"content-type": "application/x-www-form-urlencoded"})
        token = load_token(c.settings.data_dir)
        r = phone.post("/login", content=f"passcode={token}", headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 429


def test_cross_site_posts_are_blocked(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    with TestClient(create_app(c, run_scheduler=False)) as local:
        r = local.post("/api/memories", json={"content": "x"}, headers={"origin": "http://evil.example"})
        assert r.status_code == 403
        assert local.post("/api/memories", json={"content": "x"}, headers={"origin": "http://testserver"}).status_code == 200


def test_reset_passcode_signs_devices_out(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    app = create_app(c, run_scheduler=False)
    with TestClient(app) as local:
        old = local.get("/api/access").json()["token"]
        new = local.post("/api/access/reset").json()["token"]
        assert new != old
    with TestClient(app, client=PHONE, follow_redirects=False) as phone:
        phone.cookies.set("conductor_access", old)
        assert phone.get("/api/tasks").status_code == 401


def test_audit_api_filters_and_files(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    with TestClient(create_app(c, run_scheduler=False)) as local:
        local.post("/api/audit/ui", json={"events": [{"action": "click", "label": "Models"},
                                                     {"action": "view", "label": "/audit"}]})
        d = local.get("/api/audit?kind=ui.").json()
        assert [e["kind"] for e in d["entries"]] == ["ui.view", "ui.click"]  # newest first
        assert local.get("/api/audit?q=models&kind=ui.").json()["entries"][0]["label"] == "Models"
        assert local.get("/api/audit?kind=system.").json()["entries"][0]["kind"] == "system.start"
        f = d["files"][0]
        r = local.get(f"/api/audit/file/{f}")
        assert r.status_code == 200 and b"ui.click" in r.content
        assert local.get("/api/audit/file/..%2Fconductor.db").status_code == 404
        older = local.get("/api/audit?limit=1").json()
        assert older["more"] and len(older["entries"]) == 1
