"""Web server: JSON API + live event stream (SSE) + the single-page UI."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import os
import secrets
from urllib.parse import parse_qs, urlencode

from . import access, scheduler, updater
from .agent import Conductor
from .config import Settings

WEB = Path(__file__).parent / "web"


class Prompt(BaseModel):
    prompt: str
    model: str | None = None  # None/"auto" = router decides


class ModelChoice(BaseModel):
    model: str


class Feedback(BaseModel):
    value: int  # 1, -1, or 0 to clear


class Resolve(BaseModel):
    approved: bool = True
    text: str = ""


class MemoryIn(BaseModel):
    kind: str = "note"
    content: str


class SafetyIn(BaseModel):
    threshold: str | None = None
    unattended: str | None = None


class SkillIn(BaseModel):
    name: str
    description: str
    instructions: str


def create_app(conductor: Conductor | None = None, run_scheduler: bool = True) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["c"] = conductor or Conductor(Settings.from_env())
        s = state["c"].settings
        state["c"].audit.write("system.start", "system", model=s.model, approval=state["c"].policy.threshold,
                               compose=s.compose, budget=s.router_budget, port=s.port, workspace=str(s.workspace),
                               providers={"openrouter": bool(s.openrouter_key), "anthropic": s.anthropic_key})
        state["token"] = access.load_token(s.data_dir)  # passcode for other devices on your network
        state["c"].refresh_account()  # BYOK keys, guardrails, activity (background; cached 30 min)
        sched = asyncio.create_task(scheduler.run_forever(state["c"])) if run_scheduler else None
        yield
        if sched:
            sched.cancel()
        state["c"].audit.write("system.stop", "system")
        try:
            await state["c"].shutdown()
        except Exception:
            pass

    app = FastAPI(title="AI Conductor", lifespan=lifespan)

    def c() -> Conductor:
        return state["c"]

    # Routine polling the page does every few seconds; logging it would bury everything else.
    quiet_gets = re.compile(r"^/api/(tasks|tasks/[\w-]+|credits|account|access|audit|update|config|models|leaderboard|safety|schedules|skills|memories|files)$")

    @app.middleware("http")
    async def audit_requests(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path == "/api/audit/ui" or "c" not in state:
            return await call_next(request)
        started = time.time()
        body = None
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/"):
            raw = await request.body()
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = f"<{len(raw)} bytes>"
            if isinstance(body, dict):  # what was asked matters less than that it happened
                body = {k: (f"<{len(v)} chars>" if k in ("prompt", "text", "content", "instructions") and isinstance(v, str)
                            else v) for k, v in body.items()}
        response = await call_next(request)
        if not (request.method == "GET" and quiet_gets.match(path)):
            state["c"].audit.write("http.request", "user", None, method=request.method, path=path,
                                   query=urlencode([(k, v) for k, v in request.query_params.multi_items()
                                                    if k != "key"]) or None, status=response.status_code,
                                   ms=int((time.time() - started) * 1000), body=body,
                                   client=request.client.host if request.client else None)
        return response

    # ---------------------------------------------------------------- access from other devices
    failures: dict[str, list[float]] = {}

    def _signed_in(resp: Response, token: str) -> Response:
        resp.set_cookie(access.COOKIE, token, max_age=access.COOKIE_AGE, httponly=True, samesite="lax")
        return resp

    def _too_many(ip: str) -> bool:
        recent = [t for t in failures.get(ip, []) if time.time() - t < 600]
        failures[ip] = recent
        return len(recent) >= 10

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        """This computer: always allowed. Other devices: need the passcode once (then a cookie)."""
        if "c" not in state:
            return await call_next(request)
        path = request.url.path
        ip = request.client.host if request.client else ""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            src = request.headers.get("origin") or request.headers.get("referer")
            if not access.same_origin(src, request.headers.get("host")):
                state["c"].audit.write("auth.cross_site_blocked", "system", None, path=path, origin=src, client=ip)
                return JSONResponse({"detail": "Blocked: request came from another site."}, status_code=403)
        if access.is_local(ip) or path in ("/login", "/manifest.webmanifest"):
            return await call_next(request)
        token = state["token"]
        key = request.query_params.get("key")
        if key is not None:
            if not _too_many(ip) and secrets.compare_digest(key, token):
                state["c"].audit.write("auth.signed_in", "user", None, client=ip, how="link",
                                       device=request.headers.get("user-agent", "")[:120])
                rest = {k: v for k, v in request.query_params.items() if k != "key"}
                return _signed_in(RedirectResponse(path + ("?" + urlencode(rest) if rest else ""), 303), token)
            failures.setdefault(ip, []).append(time.time())
        if secrets.compare_digest(request.cookies.get(access.COOKIE, ""), token) or \
                secrets.compare_digest(request.headers.get("authorization", "").removeprefix("Bearer "), token):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Sign in first (access passcode)."}, status_code=401)
        state["c"].audit.write("auth.sign_in_needed", "system", None, path=path, client=ip)
        return RedirectResponse("/login?" + urlencode({"next": path}), 303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(next: str = "/"):
        return access.login_page(next)

    @app.post("/login")
    async def login(request: Request):
        ip = request.client.host if request.client else ""
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        code = (form.get("passcode") or [""])[0].strip().lower()
        nxt = (form.get("next") or ["/"])[0]
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if _too_many(ip):
            c().audit.write("auth.locked_out", "system", None, client=ip)
            return HTMLResponse(access.login_page(nxt, "Too many wrong tries. Wait 10 minutes."), status_code=429)
        if secrets.compare_digest(code, state["token"].lower()):
            c().audit.write("auth.signed_in", "user", None, client=ip, how="passcode",
                            device=request.headers.get("user-agent", "")[:120])
            return _signed_in(RedirectResponse(nxt, 303), state["token"])
        failures.setdefault(ip, []).append(time.time())
        c().audit.write("auth.wrong_passcode", "user", None, client=ip)
        return HTMLResponse(access.login_page(nxt, "That passcode isn't right."), status_code=401)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(access.COOKIE)
        return resp

    def _local_only(request: Request) -> None:
        if not access.is_local(request.client.host if request.client else ""):
            raise HTTPException(403, "Only from the computer running Conductor.")

    @app.get("/api/access")
    async def access_info(request: Request):
        """The passcode, phone links, and a QR code - shown only on the computer itself."""
        _local_only(request)
        port = c().settings.port
        host = os.environ.get("CONDUCTOR_HOST", "127.0.0.1")
        lan = not access.is_local(host)
        links = [f"http://{a}:{port}/?key={state['token']}" for a in access.lan_addresses()] if lan else []
        return {"token": state["token"], "on_network": lan, "host": host, "port": port, "links": links,
                "qr": access.qr_svg(links[0]) if links else None}

    @app.post("/api/access/reset")
    async def access_reset(request: Request):
        _local_only(request)
        state["token"] = access.reset_token(c().settings.data_dir)
        c().audit.write("auth.passcode_changed", "user", None)
        return await access_info(request)

    # ---------------------------------------------------------------- audit log on the web
    @app.get("/api/audit")
    async def audit_entries(days: int = 1, kind: str = "", task: str = "", actor: str = "", q: str = "",
                            limit: int = 300, before: str = ""):
        rows = c().audit.read(days=max(1, min(days, 31)))
        q = q.lower().strip()
        out = []
        for r in reversed(rows):  # newest first
            if before and str(r.get("ts", "")) >= before:
                continue
            if kind and not str(r.get("kind", "")).startswith(kind):
                continue
            if task and not str(r.get("task", "")).startswith(task):
                continue
            if actor and r.get("actor") != actor:
                continue
            if q and q not in json.dumps(r, ensure_ascii=False).lower():
                continue
            out.append(r)
            if len(out) >= max(1, min(limit, 1000)):
                break
        files = sorted(p.name for p in c().audit.dir.glob("audit-*.jsonl")) if c().audit.dir.exists() else []
        return {"entries": out, "more": len(out) >= limit, "files": files, "dir": str(c().audit.dir),
                "enabled": c().audit.enabled}

    @app.get("/api/audit/file/{name}")
    async def audit_file(name: str):
        if not re.fullmatch(r"audit-\d{4}-\d{2}-\d{2}\.jsonl", name) or not (c().audit.dir / name).is_file():
            raise HTTPException(404)
        return FileResponse(c().audit.dir / name, media_type="application/x-ndjson", filename=name)

    # ---------------------------------------------------------------- self-update
    @app.get("/api/update")
    async def update_status(fetch: bool = True):
        st = await asyncio.to_thread(updater.status, fetch)
        st["running_tasks"] = len(c()._running)
        return st

    @app.post("/api/update")
    async def update_now(request: Request):
        body = json.loads(await request.body() or b"{}")
        running = len(c()._running)
        if running and not body.get("force"):
            raise HTTPException(409, f"{running} task(s) are running; updating will stop them.")
        c().audit.write("system.update_started", "user", None, running_tasks=running)
        result = await asyncio.to_thread(updater.update)
        c().audit.write("system.update", "system", None, ok=result.get("ok"), updated=result.get("updated"),
                        from_=result.get("from"), to=result.get("to"), error=result.get("error"))
        if result.get("ok") and (result.get("updated") or body.get("restart")):
            async def later():
                await asyncio.sleep(0.8)  # let this response reach the page first
                c().audit.write("system.restart", "system", None, reason="update")
                try:
                    await c().shutdown()
                except Exception:
                    pass
                updater.restart()
            asyncio.get_running_loop().create_task(later())
            result["restarting"] = True
        return result

    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse({"name": "Conductor", "short_name": "Conductor", "start_url": "/", "display": "standalone",
                             "background_color": "#141412", "theme_color": "#146b6b",
                             "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]},
                            media_type="application/manifest+json")

    @app.post("/api/audit/ui")
    async def audit_ui(request: Request):
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(400, "bad payload")
        for ev in (payload.get("events") or [])[:200]:
            if isinstance(ev, dict):
                c().audit.write(f"ui.{str(ev.get('action', 'event'))[:20]}", "user", ev.get("task"),
                                label=ev.get("label"), element=ev.get("element"), value=ev.get("value"),
                                page=ev.get("path"), client_ts=ev.get("t"))
        return {"ok": True}

    def task_or_404(task_id: str) -> dict:
        t = c().store.get_task(task_id)
        if not t:
            raise HTTPException(404, "No such task")
        return t

    # ---------------------------------------------------------------- tasks
    @app.get("/api/config")
    def config():
        s = c().settings
        return {"model": s.model, "effort": s.effort, "approval": c().policy.threshold, "learning": s.learning,
                "web_tools": s.web_tools, "tools": sorted(c().tools), "workspace": str(s.workspace),
                "budget": s.router_budget, "router_model": s.default_router_model,
                "providers": {"anthropic": s.anthropic_key, "openrouter": bool(s.openrouter_key)},
                "credentials_hint": s.anthropic_key or bool(s.openrouter_key)}

    @app.get("/api/tasks")
    def list_tasks():
        rows = c().store.list_tasks()
        for r in rows:
            r["running"] = c().is_running(r["id"])
        return rows

    @app.post("/api/tasks")
    async def create_task(body: Prompt):
        if not body.prompt.strip():
            raise HTTPException(400, "Empty prompt")
        return {"id": c().start_task(body.prompt, model=body.model or None)}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        t = task_or_404(task_id)
        t.pop("system_prompt", None)
        t["running"] = c().is_running(task_id)
        t["children"] = c().store.child_tasks(task_id)
        team = [c().store.get_task(ch["id"]) for ch in t["children"]]
        t["team_cost_usd"] = sum(x["cost_usd"] for x in team if x)
        t["team_tokens"] = sum(x["input_tokens"] + x["output_tokens"] for x in team if x)
        t["pending"] = c().pending_requests(task_id)
        t["compares"] = c().store.compares(task_id)
        run = c().store.get_run(task_id)
        t["feedback"] = run["feedback"] if run else None
        return t

    @app.post("/api/tasks/{task_id}/compare")
    async def compare(task_id: str, body: ModelChoice):
        task_or_404(task_id)
        try:
            return {"id": c().compare(task_id, body.model)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/tasks/{task_id}/escalate")
    async def escalate(task_id: str, body: ModelChoice):
        task_or_404(task_id)
        down = body.model == "down"
        reason = "You asked for a cheaper model." if down else "You asked for a stronger model."
        by, target = ("down", "auto") if down else ("user", body.model)
        if c().request_escalation(task_id, reason, by=by, target=target):
            return {"ok": True, "pending": True}
        new = await c().escalate(task_id, reason, by=by, target=target)
        if not new:
            raise HTTPException(409, "No cheaper model looks good enough for this." if down
                                else "Already on the strongest available model for this kind of work.")
        return {"ok": True, "model": new}

    @app.post("/api/tasks/{task_id}/feedback")
    def feedback(task_id: str, body: Feedback):
        task_or_404(task_id)
        if not c().feedback(task_id, body.value if body.value in (1, -1) else None):
            raise HTTPException(409, "This task hasn't been routed yet.")
        return {"ok": True}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        task_or_404(task_id)
        c().cancel(task_id)
        c().store.delete_task(task_id)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/messages")
    async def follow_up(task_id: str, body: Prompt):
        task_or_404(task_id)
        if not body.prompt.strip():
            raise HTTPException(400, "Empty message")
        return {"ok": True, "status": c().send_message(task_id, body.prompt)}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        return {"ok": c().cancel(task_id)}

    @app.get("/api/credits")
    async def credits():
        st = await c().credits.check()
        acct = c().account
        return {**st, "byok": [b.get("provider") for b in acct.byok_keys()] if acct.has_byok else []}

    @app.get("/api/account")
    async def account():
        """What the management key shows: BYOK keys and coverage, the key's guardrail, 30-day spend."""
        return c().account.summary(c().catalog)

    @app.post("/api/account/refresh")
    async def account_refresh():
        roster = [m.id for m in c().catalog.roster() if m.provider == "openrouter"]
        await c().account.refresh(roster, force=True)
        return c().account.summary(c().catalog)

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: str):
        task_or_404(task_id)
        return {"ok": True, "status": c().resume(task_id)}

    @app.post("/api/requests/{request_id}")
    async def resolve(request_id: str, body: Resolve):
        if not c().resolve_request(request_id, body.approved, body.text):
            raise HTTPException(404, "Request not pending")
        return {"ok": True}

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = -1):
        task_or_404(task_id)
        conductor_ = c()
        q = conductor_.subscribe(task_id)  # subscribe before replay so nothing slips between

        async def gen():
            last = after
            try:
                for ev in conductor_.store.load_events(task_id, after):
                    last = ev["seq"]
                    yield f"data: {json.dumps(ev)}\n\n"
                yield f"data: {json.dumps({'type': 'replayed', 'data': {}})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if ev.get("seq") is not None and ev["seq"] <= last:
                        continue
                    if ev.get("seq") is not None:
                        last = ev["seq"]
                    yield f"data: {json.dumps(ev)}\n\n"
            finally:
                conductor_.unsubscribe(task_id, q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- models & router
    @app.get("/api/models")
    def models():
        overall = {r["model"]: r for r in c().store.model_stats()}
        return {
            "roster": [{**m.to_dict(), "stats": overall.get(m.id), "benchmarks": c().benchmarks.summary(m.id),
                        "byok": c().account.byok_for(m.id, m.canonical) if m.provider == "openrouter" else [],
                        "blocked": c().account.blocked(m.id, m.canonical) if m.provider == "openrouter" else None}
                       for m in c().catalog.roster()],
            "benchmarks_fetched_at": c().benchmarks.fetched_at,
            "benchmarks": c().benchmarks.status(),
            "catalog_size": len(c().catalog.models), "catalog_fetched_at": c().catalog.fetched_at,
        }

    @app.get("/api/models/search")
    def search_models(q: str = ""):
        return [m.to_dict() for m in c().catalog.search(q)]

    @app.post("/api/models/roster")
    def add_model(body: ModelChoice):
        try:
            c().catalog.add(body.model)
        except KeyError:
            raise HTTPException(404, "Not in the catalog")
        return {"ok": True}

    @app.delete("/api/models/roster/{model_id:path}")
    def remove_model(model_id: str):
        c().catalog.remove(model_id)
        return {"ok": True}

    @app.post("/api/models/roster/reset")
    def reset_roster():
        c().catalog.set_roster(c().catalog.default_roster())
        return {"ok": True}

    @app.get("/api/leaderboard")
    def leaderboard():
        rows = c().store.leaderboard()
        for r in rows:
            m = c().catalog.get(r["model"])
            r["name"] = m.name or m.id
            r["skill"] = r["category"]
            r["score"] = round(r["reward"] / r["runs"], 3) if r["runs"] else None
        return rows

    # ---------------------------------------------------------------- safety
    @app.get("/api/safety")
    def safety():
        p = c().policy
        return {"threshold": p.threshold, "unattended": p.unattended, "rules": p.rules}

    @app.put("/api/safety")
    def update_safety(body: SafetyIn):
        c().policy.update(body.threshold, body.unattended)
        return {"ok": True}

    @app.delete("/api/safety/rules/{index}")
    def forget_rule(index: int):
        if not c().policy.forget(index):
            raise HTTPException(404)
        return {"ok": True}

    # ---------------------------------------------------------------- memory
    @app.get("/api/memories")
    def memories(q: str = ""):
        return c().store.search_memories(q, limit=50) if q else c().store.list_memories()

    @app.post("/api/memories")
    def add_memory(body: MemoryIn):
        return {"id": c().store.add_memory(body.kind, body.content)}

    @app.put("/api/memories/{mid}")
    def edit_memory(mid: int, body: MemoryIn):
        return {"ok": c().store.update_memory(mid, body.content)}

    @app.delete("/api/memories/{mid}")
    def forget(mid: int):
        return {"ok": c().store.delete_memory(mid)}

    # ---------------------------------------------------------------- skills
    @app.get("/api/skills")
    def skills():
        return [s.__dict__ for s in c().skills.list()]

    @app.post("/api/skills")
    def save_skill(body: SkillIn):
        return c().skills.save(body.name, body.description, body.instructions).__dict__

    @app.delete("/api/skills/{name}")
    def delete_skill(name: str):
        return {"ok": c().skills.delete(name)}

    # ---------------------------------------------------------------- schedules
    @app.get("/api/schedules")
    def schedules():
        return c().store.list_schedules()

    @app.delete("/api/schedules/{sid}")
    def delete_schedule(sid: str):
        return {"ok": c().store.delete_schedule(sid)}

    @app.post("/api/schedules/{sid}/toggle")
    def toggle_schedule(sid: str):
        rows = [s for s in c().store.list_schedules() if s["id"] == sid]
        if not rows:
            raise HTTPException(404)
        c().store.update_schedule(sid, enabled=0 if rows[0]["enabled"] else 1)
        return {"ok": True}

    # ---------------------------------------------------------------- workspace files
    def safe_path(rel: str) -> Path:
        root = c().settings.workspace.resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root):
            raise HTTPException(400, "Outside workspace")
        return p

    @app.get("/api/files")
    def files(path: str = "."):
        base = safe_path(path)
        if not base.is_dir():
            raise HTTPException(404)
        root = c().settings.workspace.resolve()
        out = []
        for p in sorted(base.rglob("*")):
            relp = p.relative_to(root)
            if p.is_file() and not any(part.startswith(".") for part in relp.parts):
                out.append({"path": str(relp), "size": p.stat().st_size, "modified": p.stat().st_mtime})
            if len(out) >= 500:
                break
        return sorted(out, key=lambda f: -f["modified"])

    @app.get("/api/files/raw")
    def raw(path: str):
        p = safe_path(path)
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    # ---------------------------------------------------------------- UI
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", response_class=HTMLResponse)
    @app.get("/t/{task_id}", response_class=HTMLResponse)
    @app.get("/{page}", response_class=HTMLResponse)
    def index(task_id: str | None = None, page: str | None = None):
        if page and page not in ("memory", "skills", "schedules", "files", "models", "safety", "audit"):
            raise HTTPException(404)
        return (WEB / "index.html").read_text()

    return app
