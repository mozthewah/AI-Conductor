"""GitHub Copilot CLI (`copilot -p`) as a worker that runs on your Copilot subscription.

Like the Claude Code worker: when a task can't continue on OpenRouter (or if you prefer it), Conductor
hands the work to Copilot CLI in the workspace. Copilot serves GPT, Claude and Gemini models.

Safety: Copilot runs WITHOUT --allow-all-tools. Every action that needs permission goes through a
`PermissionRequest` hook that asks Conductor (same safety levels; it can ask you in the app). The hook
file lives in Copilot's user hooks folder and does nothing unless Conductor started that run (it only
acts when CONDUCTOR_HOOK_URL is set), so your own Copilot sessions are unaffected. If the hook isn't
loaded for some reason, Copilot's own rules apply, which in prompt mode deny anything not pre-approved.

Headroom: if Headroom has its own Copilot sign-in (`headroom copilot-auth login`), the run goes through
`headroom wrap copilot --native`, which compresses Copilot's traffic. Headroom prints its own status
lines, so Conductor gives it a stand-in `copilot` that sends the real one's answer to a file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .claude_cli import CLIResult, OnEvent

QUOTA_RX = re.compile(r"(quota|premium request|ai credits?|usage limit|rate limit|exceeded|limit reached)", re.I)
AUTH_RX = re.compile(r"(not (logged|signed) in|log ?in|authenticat|unauthori[sz]ed|no copilot access)", re.I)
MODEL_RX = re.compile(r"(model .*(not (supported|available|found))|unknown model|invalid model)", re.I)
HOOK_FILE = "conductor-approvals.json"


def find(path: str = "") -> str | None:
    import glob
    if path:
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else shutil.which(path)
    hit = shutil.which("copilot")
    if hit:
        return hit
    home = os.path.expanduser("~")
    candidates = [f"{home}/.local/bin/copilot", f"{home}/.npm-global/bin/copilot", "/usr/local/bin/copilot",
                  "/opt/homebrew/bin/copilot"]
    candidates += sorted(glob.glob(f"{home}/.nvm/versions/node/*/bin/copilot"), reverse=True)
    candidates += sorted(glob.glob("/opt/node*/bin/copilot"), reverse=True)
    return next((p for p in candidates if os.path.isfile(p) and os.access(p, os.X_OK)), None)


def model_name(model_id: str, override: str = "") -> str:
    """Copilot's name for the model the task was on (it falls back to 'auto' if Copilot doesn't have it)."""
    if override:
        return override
    m = (model_id or "").lower().split("/")[-1].split(":")[0]
    return m if m.startswith(("gpt", "claude", "gemini", "o3", "o4", "grok")) else "auto"


def serves(model_id: str) -> bool:
    """Model families Copilot carries."""
    m = (model_id or "").lower()
    if m.startswith("local/"):  # your own server's gpt-oss / gemma: never Copilot
        return False
    return any(k in m for k in ("anthropic/", "claude", "openai/", "gpt", "google/", "gemini"))


def hooks_dir() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "hooks"


def hook_config() -> dict:
    """PascalCase PermissionRequest: Claude-style tool names (Bash, Write, Read...) in the payload."""
    return {"version": 1, "hooks": {"PermissionRequest": [
        {"type": "command", "exec": sys.executable, "args": ["-m", "conductor.hook_relay"], "timeoutSec": 3600}]}}


def install_hook() -> Path | None:
    """Write (or refresh) Conductor's approval hook in Copilot's user hooks folder. Inert unless Conductor
    started the run."""
    try:
        d = hooks_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / HOOK_FILE
        body = json.dumps(hook_config(), indent=2)
        if not p.exists() or p.read_text() != body:
            p.write_text(body)
        return p
    except OSError:
        return None


def headroom_copilot_ready() -> bool:
    """Headroom can route Copilot (it has the proxy installed and its own Copilot sign-in)."""
    from .compression import HeadroomProxy
    exe = HeadroomProxy.executable()
    if not exe or not HeadroomProxy.installed():
        return False
    try:
        out = subprocess.run([exe, "copilot-auth", "status"], capture_output=True, text=True, timeout=20,
                             env={**os.environ, "HEADROOM_BEACON": os.environ.get("HEADROOM_BEACON", "off")})
        text = (out.stdout + out.stderr).lower()
        return out.returncode == 0 and "logged in" in text and "not logged in" not in text
    except Exception:
        return False


class CopilotCLI:
    def __init__(self, path: str = "", mode: str = "fallback", model: str = "", headroom: str = "auto"):
        self.mode = mode if mode in ("fallback", "prefer", "off") else "fallback"
        self.exe = None if self.mode == "off" else find(path)
        self.model = model
        self.headroom = headroom if headroom in ("auto", "off") else "auto"
        self._headroom_ok: tuple[float, bool] | None = None
        self._procs: dict[str, subprocess.Popen] = {}

    @property
    def available(self) -> bool:
        return self.mode != "off" and bool(self.exe)

    def use_headroom(self) -> bool:
        if self.headroom == "off":
            return False
        now = time.time()
        if not self._headroom_ok or now - self._headroom_ok[0] > 600:
            self._headroom_ok = (now, headroom_copilot_ready())
        return self._headroom_ok[1]

    def command(self, *, prompt: str, model: str) -> list[str]:
        return ["-p", prompt[:120_000], "-s", "--no-color", "--no-ask-user", "--model", model]

    async def run(self, key: str, *, prompt: str, model: str, cwd: str, on_event: OnEvent,
                  hook_url: str | None, via_headroom: bool) -> CLIResult:
        """Run once. Returns the answer, or an error/limit flag."""
        env = {**os.environ}
        env["CONDUCTOR_HOOK_URL"] = hook_url or ""
        env.setdefault("HEADROOM_BEACON", "off")
        out = CLIResult(model=model)
        started = time.time()
        args = self.command(prompt=prompt, model=model)
        answer_file = None
        if via_headroom:
            from .compression import HeadroomProxy
            shim_dir = Path(tempfile.mkdtemp(prefix="conductor-copilot-"))
            answer_file = shim_dir / "answer.txt"
            shim = shim_dir / "copilot"
            shim.write_text(f'#!/bin/sh\nexec "{self.exe}" "$@" > "{answer_file}"\n')
            shim.chmod(0o755)
            env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
            cmd = [HeadroomProxy.executable(), "wrap", "copilot", "--native", "--", *args]
        else:
            cmd = [self.exe, *args]
        proc = await asyncio.to_thread(subprocess.Popen, cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self._procs[key] = proc
        try:
            stdout, stderr = await asyncio.to_thread(proc.communicate)
        except asyncio.CancelledError:
            self.stop(key)
            raise
        finally:
            self._procs.pop(key, None)
        text = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace").strip()
        if answer_file is not None:
            try:
                text = answer_file.read_text(errors="replace")
            except OSError:
                text = ""
            shutil.rmtree(answer_file.parent, ignore_errors=True)
        out.text = text.strip()
        out.seconds = time.time() - started
        if proc.returncode != 0 or not out.text:
            out.is_error = True
            tail = (err or text).strip().splitlines()
            out.error = (tail[-1] if tail else f"copilot exited with code {proc.returncode}")[:400]
            blob = f"{err}\n{text}"
            out.limited = bool(QUOTA_RX.search(blob)) and not AUTH_RX.search(blob)
            if AUTH_RX.search(blob):
                out.error = "Copilot CLI isn't signed in (run `copilot` once and use /login). " + out.error
            elif MODEL_RX.search(blob):
                out.error = "model:" + out.error
        return out

    def stop(self, key: str) -> None:
        proc = self._procs.get(key)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def stop_all(self) -> None:
        for key in list(self._procs):
            self.stop(key)


# Copilot's own tool names -> the Claude Code names Conductor's safety rules know.
COPILOT_TOOLS = {"bash": "Bash", "shell": "Bash", "powershell": "Bash", "run_in_terminal": "Bash",
                 "view": "Read", "read": "Read", "read_file": "Read", "create": "Write", "write": "Write",
                 "write_file": "Write", "edit": "Edit", "str_replace": "Edit", "str_replace_editor": "Edit",
                 "apply_patch": "Edit", "glob": "Glob", "grep": "Grep", "rg": "Grep", "ls": "LS",
                 "web_fetch": "WebFetch", "fetch": "WebFetch", "web_search": "WebSearch", "task": "Task"}


def hook_request(body: dict) -> tuple[str, dict]:
    """(tool, input) from a permission hook payload, whichever shape sent it: Claude Code / Claude-format
    (tool_name, tool_input) or Copilot's own (toolName, toolArgs as a JSON string, or toolInput)."""
    body = body or {}
    tool = str(body.get("tool_name") or body.get("toolName") or body.get("tool") or "")
    raw = next((body[k] for k in ("tool_input", "toolInput", "toolArgs", "input", "arguments") if body.get(k)), {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {"command": raw} if tool.lower() in ("bash", "shell", "powershell") else {"value": raw}
    inp = dict(raw) if isinstance(raw, dict) else {"value": raw}
    tool = COPILOT_TOOLS.get(tool.lower(), tool) if tool and not tool[0].isupper() else tool
    if tool in ("Read", "Write", "Edit") and "file_path" not in inp:
        p = inp.get("path") or inp.get("filePath") or inp.get("file")
        if p:
            inp["file_path"] = p
    if tool == "Bash" and "command" not in inp:
        inp["command"] = inp.get("cmd") or inp.get("script") or ""
    return tool, inp

