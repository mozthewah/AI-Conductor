"""Command line: `conductor serve` for the web app, `conductor chat` for the terminal."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .config import Settings

DIM, BOLD, CYAN, YELLOW, RED, GREEN, RESET = (
    ("\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[0m")
    if sys.stdout.isatty() else ("",) * 7)


def serve(args) -> None:
    import uvicorn

    from .server import create_app

    print(f"{BOLD}AI Conductor{RESET} -> http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _chat(settings: Settings, first: str | None) -> None:
    from .agent import Conductor

    c = Conductor(settings)
    task_id: str | None = None
    print(f"{BOLD}AI Conductor{RESET} {DIM}({settings.model}, effort {settings.effort}, "
          f"approval {settings.approval}) - type /new for a fresh task, /quit to exit{RESET}\n")
    pending = first
    while True:
        prompt = pending if pending is not None else (await _ainput(f"{BOLD}you>{RESET} ")).strip()
        pending = None
        if not prompt:
            continue
        if prompt in ("/quit", "/exit"):
            break
        if prompt == "/new":
            task_id = None
            print(f"{DIM}(new task){RESET}")
            continue
        if task_id is None:
            task_id = c.start_task(prompt)
        else:
            c.send_message(task_id, prompt)
        q = c.subscribe(task_id)
        streaming = False
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    if not c.is_running(task_id):
                        break
                    continue
                t, d = ev["type"], ev["data"]
                if t == "text_delta":
                    if not streaming:
                        print(f"\n{CYAN}conductor>{RESET} ", end="")
                        streaming = True
                    print(d["text"], end="", flush=True)
                    continue
                if streaming and t not in ("assistant_text",):
                    print()
                    streaming = False
                if t == "progress":
                    print(f"{DIM}  • {d['text'].strip()}{RESET}")
                elif t == "tool_call":
                    print(f"{YELLOW}  ↳ {d['summary']}{RESET}")
                elif t == "tool_result" and not d["ok"]:
                    print(f"{RED}    ✗ {d['preview'][:200]}{RESET}")
                elif t == "plan":
                    marks = {"done": "☑", "in_progress": "▸", "skipped": "–", "pending": "☐"}
                    for s in d["steps"]:
                        print(f"{DIM}    {marks.get(s['status'], '?')} {s['step']}{RESET}")
                elif t == "message":
                    print(f"\n{CYAN}conductor (update)>{RESET} {d['text']}\n")
                elif t == "subagent":
                    print(f"{DIM}  ⚇ sub-agent {d['action']}: {d['title']}{RESET}")
                elif t == "request":
                    if d["kind"] == "approval":
                        ans = (await _ainput(f"{YELLOW}  ? Allow {d['summary']} [y/N] {RESET}")).strip().lower()
                        c.resolve_request(d["id"], ans in ("y", "yes"))
                    else:
                        opts = f" ({' / '.join(d['options'])})" if d.get("options") else ""
                        ans = (await _ainput(f"{YELLOW}  ? {d['question']}{opts}\n  > {RESET}")).strip()
                        c.resolve_request(d["id"], True, ans)
                elif t == "status":
                    print(f"{DIM}  {d['text']}{RESET}")
                elif t == "learned":
                    for m in d["memories"]:
                        print(f"{GREEN}  + remembered: {m['content']}{RESET}")
                    if d.get("skill"):
                        print(f"{GREEN}  + learned skill: {d['skill']['name']}{RESET}")
                elif t == "error":
                    print(f"{RED}  error: {d['text']}{RESET}")
        finally:
            c.unsubscribe(task_id, q)
        await c.wait(task_id)
        print()
    await c.shutdown()


def chat(args) -> None:
    settings = Settings.from_env()
    if args.auto:
        settings.approval = "auto"
    try:
        asyncio.run(_chat(settings, " ".join(args.prompt) if args.prompt else None))
    except (KeyboardInterrupt, EOFError):
        print()


def main() -> None:
    p = argparse.ArgumentParser(prog="conductor", description="Your self-hosted AI conductor.")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default=os.environ.get("CONDUCTOR_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("CONDUCTOR_PORT", "8765")))
    ch = sub.add_parser("chat", help="chat in the terminal")
    ch.add_argument("--auto", action="store_true", help="don't ask before running shell commands")
    ch.add_argument("prompt", nargs="*")
    args = p.parse_args()
    if args.cmd == "chat":
        chat(args)
    else:
        if args.cmd is None:
            args.host, args.port = os.environ.get("CONDUCTOR_HOST", "127.0.0.1"), 8765
        serve(args)


if __name__ == "__main__":
    main()
