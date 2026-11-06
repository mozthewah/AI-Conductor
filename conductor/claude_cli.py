"""Claude Code (the `claude` CLI) as a worker that runs on your Claude subscription.

When a Claude task can't go on through OpenRouter (credits out, a step too big for the balance,
the provider failing), or always if you prefer it, Conductor hands the work to `claude -p`
running in the workspace. It runs with its own tools (Bash, Read, Edit, ...), and every tool
call goes through Conductor's safety levels via a PreToolUse HTTP hook, so the same
"auto-approve up to X, ask me above that" rules apply, including asking you in the app.

It deliberately does NOT use `--bare` (that mode ignores your subscription login), and it removes
ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL from the CLI's environment so the
run bills your subscription, not an API account.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

OnEvent = Callable[[str, dict], Awaitable[None]]

# Claude Code tool -> the Conductor tool it most resembles (so the app shows familiar icons/labels)
TOOL_NAMES = {"Bash": "run_shell", "Read": "read_file", "Write": "write_file", "Edit": "edit_file",
              "MultiEdit": "edit_file", "NotebookEdit": "edit_file", "Glob": "list_files", "Grep": "list_files",
              "LS": "list_files", "WebFetch": "web_fetch", "WebSearch": "web_search"}
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX")
LIMIT_RX = re.compile(r"(usage limit|limit reached|rate limit|out of (extra )?usage|5-hour limit|weekly limit)", re.I)


def find(path: str = "") -> str | None:
    return shutil.which(path or "claude") or next(
        (p for p in (os.path.expanduser("~/.claude/local/claude"), os.path.expanduser("~/.local/bin/claude"))
         if os.path.isfile(p) and os.access(p, os.X_OK)), None)


def model_alias(model_id: str, difficulty: str = "medium") -> str:
    """The CLI takes aliases; keep the family of the model the task was on, else pick by difficulty."""
    m = (model_id or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return "opus" if difficulty == "hard" else "sonnet"


def describe(tool: str, inp: dict) -> str:
    if tool == "Bash":
        return "$ " + (inp.get("command") or "")[:80]
    if tool in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        verb = {"Read": "Read", "Write": "Wrote"}.get(tool, "Edited")
        return f"{verb} {inp.get('file_path') or inp.get('notebook_path') or ''}"
    if tool in ("Glob", "Grep"):
        return f"Searched for {inp.get('pattern', '')}"
    if tool == "WebFetch":
        return f"Fetched {inp.get('url', '')}"
    if tool == "WebSearch":
        return f"Searched the web for {inp.get('query', '')}"
    if tool in ("Agent", "Task"):
        return f"Started a helper: {inp.get('description') or inp.get('subagent_type') or ''}"
    return f"{tool}"


@dataclass
class CLIResult:
    text: str = ""
    session_id: str | None = None
    is_error: bool = False
    limited: bool = False      # hit the subscription's usage limit
    error: str = ""
    turns: int = 0
    seconds: float = 0.0
    model: str = ""
    denials: list = field(default_factory=list)


class ClaudeCLI:
    def __init__(self, path: str = "", mode: str = "fallback"):
        self.mode = mode if mode in ("fallback", "prefer", "off") else "fallback"
        self.exe = None if self.mode == "off" else find(path)
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    @property
    def available(self) -> bool:
        return self.mode != "off" and bool(self.exe)

    def command(self, *, model: str, system: str, cwd: str, hook_url: str | None, resume: str | None,
                max_turns: int) -> list[str]:
        cmd = [self.exe, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
               "--max-turns", str(max_turns), "--append-system-prompt", system]
        if hook_url:
            # Every tool call asks Conductor first: same safety levels, and it can ask you in the app.
            settings = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "http", "url": hook_url, "timeout": 3600, "onFailure": "block"}]}]}}
            cmd += ["--settings", json.dumps(settings), "--permission-mode", "default"]
        else:
            # No Conductor web server to ask (terminal chat): files only, no shell.
            cmd += ["--allowedTools", "Read,Glob,Grep,Edit,Write,WebSearch,WebFetch",
                    "--disallowedTools", "Bash"]
        if resume:
            cmd += ["--resume", resume]
        return cmd

    async def run(self, key: str, *, prompt: str, model: str, system: str, cwd: str, on_event: OnEvent,
                  hook_url: str | None = None, resume: str | None = None, max_turns: int = 60,
                  base_url: str | None = None) -> CLIResult:
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        if base_url:  # e.g. Headroom's local proxy, which compresses what Claude Code sends
            env["ANTHROPIC_BASE_URL"] = base_url
        started = time.time()
        out = CLIResult(model=model)
        proc = await asyncio.create_subprocess_exec(
            *self.command(model=model, system=system, cwd=cwd, hook_url=hook_url, resume=resume, max_turns=max_turns),
            cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16 * 1024 * 1024)
        self._procs[key] = proc
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            names: dict[str, str] = {}
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                await self._handle(ev, out, names, on_event)
            err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
            code = await proc.wait()
            if not out.text and not out.is_error and code != 0:
                out.is_error, out.error = True, (err.splitlines() or [f"claude exited with code {code}"])[-1][:400]
            if out.is_error and LIMIT_RX.search(f"{out.error} {out.text} {err}"):
                out.limited = True
        except asyncio.CancelledError:
            self.stop(key)
            raise
        finally:
            self._procs.pop(key, None)
        out.seconds = time.time() - started
        return out

    async def _handle(self, ev: dict, out: CLIResult, names: dict, on_event: OnEvent) -> None:
        t, sub = ev.get("type"), ev.get("subtype")
        if ev.get("session_id"):
            out.session_id = ev["session_id"]
        if t == "system" and sub == "init":
            out.model = ev.get("model") or out.model
        elif t == "system" and sub == "api_retry":
            note = {"rate_limit": "hit a rate limit", "overloaded": "is overloaded"}.get(ev.get("error"), "had a hiccup")
            await on_event("status", {"text": f"Claude Code {note}; retrying (attempt {ev.get('attempt')})."})
        elif t == "assistant":
            nested = ev.get("parent_tool_use_id") is not None
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text") and not nested:
                    await on_event("text", {"text": b["text"]})
                elif b.get("type") == "tool_use":
                    names[b.get("id")] = b.get("name", "")
                    inp = b.get("input") or {}
                    await on_event("tool_call", {"id": b.get("id"), "name": TOOL_NAMES.get(b.get("name"), b.get("name")),
                                                 "summary": describe(b.get("name", ""), inp), "risky": False,
                                                 "input": {k: (v[:2000] if isinstance(v, str) else v)
                                                           for k, v in inp.items()}, "via": "claude_cli"})
        elif t == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    out.turns += 1
                    await on_event("tool_result", {"id": b.get("tool_use_id"),
                                                   "name": TOOL_NAMES.get(names.get(b.get("tool_use_id"), ""), ""),
                                                   "ok": not b.get("is_error"), "preview": str(content or "")[:1500]})
        elif t == "result":
            out.text = ev.get("result") or out.text
            out.is_error = bool(ev.get("is_error")) or (sub not in (None, "success"))
            if out.is_error:
                out.error = (ev.get("result") or sub or "Claude Code stopped with an error.")[:400]
            out.denials = ev.get("permission_denials") or []

    def stop(self, key: str) -> None:
        proc = self._procs.get(key)
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def stop_all(self) -> None:
        for key in list(self._procs):
            self.stop(key)
