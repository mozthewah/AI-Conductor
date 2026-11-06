"""Files and shell, confined to the workspace directory."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
import uuid
from pathlib import Path

from ..safety import assess_shell
from . import Tool, ToolContext, ToolError, clip

TEXT_LIMIT = 200_000


def resolve(ctx: ToolContext, rel: str) -> Path:
    """Model-supplied paths are untrusted: keep them inside the workspace."""
    root = ctx.settings.workspace.resolve()
    target = (root / rel.lstrip("/")).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    if not target.is_relative_to(root):
        raise ToolError(f"'{rel}' is outside the workspace. Use paths relative to the workspace root.")
    return target


def rel(ctx: ToolContext, p: Path) -> str:
    return str(p.relative_to(ctx.settings.workspace.resolve()))


async def read_file(ctx: ToolContext, args: dict) -> str:
    p = resolve(ctx, args["path"])
    if not p.is_file():
        raise ToolError(f"No file at {args['path']}")
    data = p.read_bytes()
    if b"\x00" in data[:4096]:
        return f"{args['path']} is a binary file ({len(data)} bytes)."
    text = data.decode("utf-8", errors="replace")
    start = max(int(args.get("offset", 1)), 1)
    lines = text.splitlines()
    chunk = lines[start - 1:start - 1 + int(args.get("limit", 2000))]
    numbered = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, start))
    return clip(numbered) + (f"\n\n({len(lines)} lines total)" if len(lines) > len(chunk) else "")


async def write_file(ctx: ToolContext, args: dict) -> str:
    p = resolve(ctx, args["path"])
    content = args["content"]
    if len(content) > TEXT_LIMIT * 10:
        raise ToolError("content too large")
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if args.get("append"):
        with p.open("a") as f:
            f.write(content)
    else:
        p.write_text(content)
    await ctx.emit("artifact", {"path": rel(ctx, p), "action": "updated" if existed else "created"})
    return f"{'Updated' if existed else 'Created'} {rel(ctx, p)} ({len(content)} chars)."


async def edit_file(ctx: ToolContext, args: dict) -> str:
    p = resolve(ctx, args["path"])
    if not p.is_file():
        raise ToolError(f"No file at {args['path']}")
    text = p.read_text()
    count = text.count(args["old"])
    if count == 0:
        raise ToolError("The 'old' text was not found. Read the file and copy it exactly.")
    if count > 1:
        raise ToolError(f"The 'old' text appears {count} times; include more context so it's unique.")
    p.write_text(text.replace(args["old"], args["new"], 1))
    await ctx.emit("artifact", {"path": rel(ctx, p), "action": "updated"})
    return f"Edited {rel(ctx, p)}."


async def list_files(ctx: ToolContext, args: dict) -> str:
    base = resolve(ctx, args.get("path", "."))
    if not base.is_dir():
        raise ToolError(f"No directory at {args.get('path')}")
    out = []
    for p in sorted(base.rglob("*")):
        if any(part.startswith(".") for part in p.relative_to(base).parts):
            continue
        if len(out) >= 300:
            out.append("... (truncated)")
            break
        out.append(rel(ctx, p) + ("/" if p.is_dir() else f"  ({p.stat().st_size} B)"))
    return "\n".join(out) or "(empty)"


def _shell_env() -> dict:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    for secret in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY"):
        env.pop(secret, None)  # don't hand keys to arbitrary scripts
    return env


def _log_dir(ctx: ToolContext) -> Path:
    d = ctx.settings.workspace / ".conductor" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _kill_group(pgid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.3)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _tail(path: Path, limit: int = 12_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


URL_RX = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::\])(?::\d+)?[^\s'\"<>]*")


async def run_shell(ctx: ToolContext, args: dict) -> str:
    cmd = args["command"]
    assessment = assess_shell(cmd, ctx.settings.workspace)
    if not await ctx.approve("run_shell", f"Run: {cmd}", args, assessment):
        raise ToolError("This command wasn't approved (the user declined, or it's above the risk level allowed "
                        "for this run). Try a safer approach, or ask the user.")
    if args.get("background"):
        return await _start_background(ctx, cmd, args.get("name") or cmd[:60])
    timeout = min(int(args.get("timeout", 120)), 900)
    log = _log_dir(ctx) / f"run-{uuid.uuid4().hex[:8]}.log"
    # Output goes to a file and we wait for bash itself to exit - not for the output to close - so a
    # server started with `&` can't hang the tool. Its own process group lets us stop everything on timeout.
    with log.open("wb") as f:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", cmd, cwd=str(ctx.settings.workspace), env=_shell_env(),
            stdout=f, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await asyncio.to_thread(_kill_group, proc.pid)
            out = _tail(log, 4000)
            log.unlink(missing_ok=True)
            raise ToolError(f"Command timed out after {timeout}s and was stopped. If it starts a server or a "
                            f"long-running job, run it with background: true instead.\nOutput so far:\n{out}")
    text = _tail(log, 60_000)
    log.unlink(missing_ok=True)
    note = ""
    if _group_alive(proc.pid):  # something it started with `&` is still running: track it
        ctx.conductor.processes[proc.pid] = {"pgid": proc.pid, "name": cmd[:60], "cmd": cmd, "log": None,
                                             "started": time.time(), "task_id": ctx.task_id}
        note = (f"\n\nNote: this command left background processes running (group {proc.pid}). They're tracked - "
                "use the processes tool to stop them. Next time, prefer background: true so you also get their logs.")
    result = f"exit code {proc.returncode}\n{text}" if text else f"exit code {proc.returncode} (no output)"
    return clip(result + note)


async def _start_background(ctx: ToolContext, cmd: str, name: str) -> str:
    log = _log_dir(ctx) / f"bg-{uuid.uuid4().hex[:8]}.log"
    with log.open("ab") as f:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", cmd, cwd=str(ctx.settings.workspace), env=_shell_env(),
            stdout=f, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    for _ in range(12):  # give it a moment to start (or crash) so the first result is useful
        await asyncio.sleep(0.25)
        if proc.returncode is not None or URL_RX.search(_tail(log, 4000)):
            break
    out = _tail(log, 4000)
    if proc.returncode is not None:
        return clip(f"It exited right away (code {proc.returncode}), so nothing is running in the background.\n{out}")
    entry = {"pgid": proc.pid, "name": name, "cmd": cmd, "log": str(log), "started": time.time(),
             "task_id": ctx.task_id, "proc": proc}
    ctx.conductor.processes[proc.pid] = entry
    urls = sorted(set(URL_RX.findall(out)))
    await ctx.emit("process", {"action": "started", "id": proc.pid, "name": name, "urls": urls})
    rel_log = str(log.relative_to(ctx.settings.workspace))
    return (f"Started in the background as process {proc.pid} ({name}). Log: {rel_log}"
            + (f"\nIt's serving at: {', '.join(urls)}" if urls else "")
            + f"\nOutput so far:\n{out or '(none yet)'}\nUse the processes tool to check its log or stop it.")


async def processes(ctx: ToolContext, args: dict) -> str:
    c = ctx.conductor
    action = args.get("action", "list")
    if action == "list":
        rows = []
        for pid, p in list(c.processes.items()):
            alive = _group_alive(p["pgid"])
            rows.append(f"- {pid}: {p['name']} - {'running' if alive else 'exited'}, started "
                        f"{int(time.time() - p['started'])}s ago" + (f", log {Path(p['log']).name}" if p.get("log") else ""))
            if not alive:
                c.processes.pop(pid, None)
        return "\n".join(rows) or "No background processes."
    pid = args.get("id")
    if pid is None or int(pid) not in c.processes:
        raise ToolError(f"No tracked process {pid}. Call processes with action 'list'.")
    p = c.processes[int(pid)]
    if action == "logs":
        if not p.get("log"):
            return "That process wasn't started with background: true, so there's no log to read."
        return clip(_tail(Path(p["log"]), 20_000) or "(no output yet)")
    if action == "stop":
        await asyncio.to_thread(_kill_group, p["pgid"])
        c.processes.pop(int(pid), None)
        await ctx.emit("process", {"action": "stopped", "id": int(pid), "name": p["name"]})
        return f"Stopped {pid} ({p['name']})."
    raise ToolError("action must be list, logs, or stop")


TOOLS = [
    Tool("read_file", "Read a text file from the workspace (line-numbered).",
         {"path": {"type": "string"}, "offset": {"type": "integer", "description": "1-based start line"},
          "limit": {"type": "integer"}},
         ["path"], read_file, summarize=lambda a: f"Read {a.get('path')}"),
    Tool("write_file",
         "Create or overwrite a file in the workspace. Use this for deliverables (reports, code, data) - "
         "the user can open everything in the workspace.",
         {"path": {"type": "string"}, "content": {"type": "string"},
          "append": {"type": "boolean", "description": "Append instead of overwrite"}},
         ["path", "content"], write_file, summarize=lambda a: f"Wrote {a.get('path')}"),
    Tool("edit_file", "Replace one exact, unique snippet of text in a workspace file.",
         {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
         ["path", "old", "new"], edit_file, summarize=lambda a: f"Edited {a.get('path')}"),
    Tool("list_files", "List files under a workspace directory (recursive).",
         {"path": {"type": "string", "description": "Directory, default '.'"}},
         [], list_files, summarize=lambda a: f"Listed {a.get('path', 'workspace')}"),
    Tool("run_shell",
         "Run a bash command in the workspace directory (python3, pip, curl, git, etc. as installed). "
         "Use it to run code, process data, or install packages. Returns combined stdout/stderr. For "
         "anything that keeps running - a dev server, a watcher, a local page for the user - set "
         "background: true: it returns right away with the process id, log path, and any URL it's "
         "serving, and keeps running until you stop it with the processes tool.",
         {"command": {"type": "string"}, "timeout": {"type": "integer", "description": "Seconds, default 120"},
          "background": {"type": "boolean", "description": "Keep it running and return immediately"},
          "name": {"type": "string", "description": "Short label for a background process"}},
         ["command"], run_shell, risky=True,
         summarize=lambda a: f"{'⟳ ' if a.get('background') else '$ '}{a.get('command', '')[:80]}"),
    Tool("processes",
         "Manage background processes started with run_shell(background: true): list them, read a "
         "process's log, or stop it. Stop servers you no longer need.",
         {"action": {"type": "string", "enum": ["list", "logs", "stop"]}, "id": {"type": "integer"}},
         ["action"], processes, summarize=lambda a: f"Processes: {a.get('action')} {a.get('id') or ''}".strip()),
]
