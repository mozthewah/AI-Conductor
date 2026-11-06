"""Files and shell, confined to the workspace directory."""

from __future__ import annotations

import asyncio
import os
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


async def run_shell(ctx: ToolContext, args: dict) -> str:
    cmd = args["command"]
    assessment = assess_shell(cmd, ctx.settings.workspace)
    if not await ctx.approve("run_shell", f"Run: {cmd}", args, assessment):
        raise ToolError("This command wasn't approved (the user declined, or it's above the risk level allowed "
                        "for this run). Try a safer approach, or ask the user.")
    timeout = min(int(args.get("timeout", 120)), 900)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    for secret in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY"):
        env.pop(secret, None)  # don't hand keys to arbitrary scripts
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", cmd, cwd=str(ctx.settings.workspace), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"Command timed out after {timeout}s.")
    text = out.decode("utf-8", errors="replace")
    return clip(f"exit code {proc.returncode}\n{text}" if text else f"exit code {proc.returncode} (no output)")


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
         "Use it to run code, process data, or install packages. Returns combined stdout/stderr.",
         {"command": {"type": "string"}, "timeout": {"type": "integer", "description": "Seconds, default 120"}},
         ["command"], run_shell, risky=True, summarize=lambda a: f"$ {a.get('command', '')[:80]}"),
]
