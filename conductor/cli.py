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

    os.environ["CONDUCTOR_PORT"] = str(args.port)  # so tools know not to take this port
    print(f"{BOLD}AI Conductor{RESET} -> http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _chat(settings: Settings, first: str | None) -> None:
    from .agent import Conductor

    c = Conductor(settings)
    task_id: str | None = None
    print(f"{BOLD}AI Conductor{RESET} {DIM}({'auto-routing, ' + settings.router_budget + ' budget' if settings.model == 'auto' else settings.model}, effort {settings.effort}, "
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
                        always = f", a = always allow '{d['remember_key']}'" if d.get("remember_key") else ""
                        print(f"{YELLOW}  ! {d.get('level', '?').upper()}: {d.get('reason', '')}{RESET}")
                        ans = (await _ainput(f"{YELLOW}  ? Allow {d['summary']} [y/N{always}] {RESET}")).strip().lower()
                        if ans in ("a", "always") and d.get("remember_key"):
                            c.resolve_request(d["id"], True, "always")
                        else:
                            c.resolve_request(d["id"], ans in ("y", "yes"))
                    else:
                        opts = f" ({' / '.join(d['options'])})" if d.get("options") else ""
                        ans = (await _ainput(f"{YELLOW}  ? {d['question']}{opts}\n  > {RESET}")).strip()
                        c.resolve_request(d["id"], True, ans)
                elif t == "approval":
                    mark = "auto-allowed" if d["allowed"] else "declined"
                    print(f"{DIM}  \u26e8 {mark} ({d['level']}): {d['summary'][:80]}{RESET}")
                elif t == "routed":
                    print(f"{DIM}  \u21c4 {d.get('name') or d['model']} ({d['category']}, {d['difficulty']}"
                          f"{', ~$%.3f' % d['est_cost'] if d.get('est_cost') else ''}){RESET}")
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
        settings.approval = "high"
    try:
        asyncio.run(_chat(settings, " ".join(args.prompt) if args.prompt else None))
    except (KeyboardInterrupt, EOFError):
        print()


def doctor(args) -> None:
    """Check keys, the model catalog, the benchmark feed, and the browser - with the real errors."""
    import json
    import urllib.request

    from .benchmarks import Benchmarks
    from .models import Catalog

    s = Settings.from_env()
    ok = lambda m: print(f"{GREEN}  ✓ {m}{RESET}")
    bad = lambda m: print(f"{RED}  ✗ {m}{RESET}")
    print(f"{BOLD}Conductor doctor{RESET}  (data: {s.data_dir}, workspace: {s.workspace})")
    (ok if s.openrouter_key else bad)(f"OPENROUTER_API_KEY {'set' if s.openrouter_key else 'missing'}")
    (ok if s.anthropic_key else print)(f"  ANTHROPIC_API_KEY {'set' if s.anthropic_key else 'not set (optional)'}")
    if s.openrouter_key:
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                         headers={"Authorization": f"Bearer {s.openrouter_key}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                info = json.loads(r.read()).get("data", {})
            ok(f"OpenRouter accepts the key (label: {info.get('label', '?')}, "
               f"usage ${info.get('usage', 0):.2f}{', limit $' + str(info['limit']) if info.get('limit') else ''})")
        except Exception as e:
            bad(f"OpenRouter rejected or couldn't check the key: {e}")
    s.ensure_dirs()
    cat = Catalog(s.data_dir, has_openrouter=bool(s.openrouter_key), has_anthropic=s.anthropic_key).load()
    roster = cat.roster()
    (ok if roster else bad)(f"Model catalog: {len(cat.models)} models, {len(roster)} in your roster")
    cache = s.data_dir / "benchmarks.json"
    if cache.exists():
        cache.unlink()  # force a fresh fetch so we see the real result
    b = Benchmarks(s.data_dir, key=s.openrouter_key).load()
    b.remap(cat.models)
    st = b.status()
    if st["models"]:
        covered = [m.id for m in roster if b.summary(m.id)]
        missing = [m.id for m in roster if not b.summary(m.id)]
        (ok if len(covered) >= len(roster) * 0.6 else bad)(
            f"Benchmarks: {st['models']} models across {', '.join(st['signals'])}; "
            f"{len(covered)}/{len(roster)} roster models covered")
        for mid in missing:
            print(f"      no benchmark data for {mid} (it'll be judged by its price tier until it has results)")
    else:
        bad(f"Benchmarks unavailable: {st['error']}")
    if s.browser:
        try:
            from .tools.browser import BrowserPool

            async def check():
                pool = BrowserPool(s.workspace / ".screens")
                await pool._ensure()
                await pool.shutdown()
            asyncio.run(check())
            ok("Browser: Chromium launches")
        except Exception as e:
            bad(f"Browser: {str(e).splitlines()[0]} (run `playwright install chromium`, or set CONDUCTOR_BROWSER=off)")


def main() -> None:
    p = argparse.ArgumentParser(prog="conductor", description="Your self-hosted AI conductor.")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default=os.environ.get("CONDUCTOR_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("CONDUCTOR_PORT", "8765")))
    sub.add_parser("doctor", help="check keys, models, benchmarks, and the browser")
    ch = sub.add_parser("chat", help="chat in the terminal")
    ch.add_argument("--auto", action="store_true", help="auto-approve everything up to high risk (critical still asks)")
    ch.add_argument("prompt", nargs="*")
    args = p.parse_args()
    if args.cmd == "chat":
        chat(args)
    elif args.cmd == "doctor":
        doctor(args)
    else:
        if args.cmd is None:
            args.host, args.port = os.environ.get("CONDUCTOR_HOST", "127.0.0.1"), 8765
        serve(args)


if __name__ == "__main__":
    main()
