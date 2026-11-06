"""Web server: JSON API + live event stream (SSE) + the single-page UI."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import scheduler
from .agent import Conductor
from .config import Settings

WEB = Path(__file__).parent / "web"


class Prompt(BaseModel):
    prompt: str


class Resolve(BaseModel):
    approved: bool = True
    text: str = ""


class MemoryIn(BaseModel):
    kind: str = "note"
    content: str


class SkillIn(BaseModel):
    name: str
    description: str
    instructions: str


def create_app(conductor: Conductor | None = None, run_scheduler: bool = True) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["c"] = conductor or Conductor(Settings.from_env())
        sched = asyncio.create_task(scheduler.run_forever(state["c"])) if run_scheduler else None
        yield
        if sched:
            sched.cancel()
        await state["c"].shutdown()

    app = FastAPI(title="AI Conductor", lifespan=lifespan)

    def c() -> Conductor:
        return state["c"]

    def task_or_404(task_id: str) -> dict:
        t = c().store.get_task(task_id)
        if not t:
            raise HTTPException(404, "No such task")
        return t

    # ---------------------------------------------------------------- tasks
    @app.get("/api/config")
    def config():
        s = c().settings
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        return {"model": s.model, "effort": s.effort, "approval": s.approval, "learning": s.learning,
                "web_tools": s.web_tools, "tools": sorted(c().tools), "workspace": str(s.workspace),
                "credentials_hint": has_key}

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
        return {"id": c().start_task(body.prompt)}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        t = task_or_404(task_id)
        t.pop("system_prompt", None)
        t["running"] = c().is_running(task_id)
        t["children"] = c().store.child_tasks(task_id)
        t["pending"] = c().pending_requests(task_id)
        return t

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        task_or_404(task_id)
        c().cancel(task_id)
        c().store.delete_task(task_id)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/messages")
    async def follow_up(task_id: str, body: Prompt):
        task_or_404(task_id)
        try:
            c().send_message(task_id, body.prompt)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        return {"ok": c().cancel(task_id)}

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
        if page and page not in ("memory", "skills", "schedules", "files"):
            raise HTTPException(404)
        return (WEB / "index.html").read_text()

    return app
