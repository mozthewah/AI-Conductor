"""Command line: `conductor serve` for the web app, `conductor chat` for the terminal."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .config import Settings

DIM, BOLD, CYAN, YELLOW, RED, GREEN, RESET = (
    ("\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[0m")
    if sys.stdout.isatty() else ("",) * 7)


def _stay_awake():
    """Hold off sleep/suspend while serving (systemd-inhibit). Returns the holder process, or None."""
    import shutil
    import subprocess
    exe = shutil.which("systemd-inhibit")
    if not exe:
        print(f"{YELLOW}  Can't keep the computer awake: systemd-inhibit isn't available.{RESET}")
        return None
    try:
        proc = subprocess.Popen([exe, "--what=sleep:idle", "--who=Conductor", "--mode=block",
                                 "--why=Serving Conductor to your other devices", "sleep", "infinity"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            proc.wait(timeout=0.5)  # still running = the inhibitor took hold
            err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
            print(f"{YELLOW}  Couldn't keep the computer awake: {err or 'systemd-inhibit exited'}{RESET}")
            return None
        except subprocess.TimeoutExpired:
            return proc
    except OSError as e:
        print(f"{YELLOW}  Couldn't keep the computer awake: {e}{RESET}")
        return None


def serve(args) -> None:
    import uvicorn

    from . import access
    from .server import create_app

    host = "0.0.0.0" if getattr(args, "lan", False) else args.host
    os.environ["CONDUCTOR_PORT"] = str(args.port)  # so tools know not to take this port
    os.environ["CONDUCTOR_HOST"] = host
    print(f"{BOLD}AI Conductor{RESET} -> http://127.0.0.1:{args.port}")
    if not access.is_local(host):
        s = Settings.from_env()
        s.ensure_dirs()
        token = access.load_token(s.data_dir)
        addrs = access.lan_addresses()
        print(f"  On your other devices (same Wi-Fi): " + ", ".join(f"http://{a}:{args.port}" for a in addrs))
        print(f"  Passcode for them: {BOLD}{token}{RESET}  (also on the Safety page; this computer never needs it)")
        link = access.device_link(token, args.port)
        art = access.qr_terminal(link) if link else None
        if art:
            print("  Or scan this with your phone's camera to open it already signed in:\n" + art)
        print(f"{DIM}  If your phone can't connect, the firewall may be blocking port {args.port} "
              f"(e.g. `sudo ufw allow {args.port}/tcp`).{RESET}")
    awake = _stay_awake() if (getattr(args, "stay_awake", False) or os.environ.get("CONDUCTOR_STAY_AWAKE", "").lower()
                              in ("1", "on", "true", "yes")) else None
    if awake:
        from . import updater
        updater.on_restart(awake.terminate)  # the update button restarts the process; don't leave it behind
        print(f"{DIM}  Keeping this computer awake while Conductor runs (closing a laptop lid may still sleep it).{RESET}")
    # A short graceful window closes open live-update streams instead of dumping tracebacks on Ctrl+C.
    try:
        uvicorn.run(create_app(), host=host, port=args.port, log_level="warning", timeout_graceful_shutdown=2)
    finally:
        if awake:
            awake.terminate()


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
    cat = Catalog(s.data_dir, has_openrouter=bool(s.openrouter_key), has_anthropic=s.anthropic_key,
                  **s.catalog_kwargs()).load()
    roster = cat.roster()
    (ok if roster else bad)(f"Model catalog: {len(cat.models)} models, {len(roster)} in your roster")
    if s.local_url:
        for st in cat.local_status:
            if st["models"]:
                names = ", ".join(st["models"][:8]) + (f" (+{len(st['models']) - 8} more)" if len(st["models"]) > 8 else "")
                ok(f"Model server {st['url']} ({st['api']}): {names}")
                no_tools = [m.id for m in cat.models.values() if m.provider == "local" and m.base_url == st["url"]
                            and not m.tools]
                if no_tools:
                    print(f"    can't call tools (left out of the roster): {', '.join(no_tools)}")
            else:
                bad(f"Model server {st['url']} ({st['api']}): {st['error'] or 'no chat models found'}")
        if not (s.openrouter_key or s.anthropic_key):
            print("    No cloud keys: your own models are the whole roster.")
        else:
            print("    Add local models to the roster on the Models page (they're free; OpenRouter-out fallback either way).")
    else:
        from . import local as local_models
        for st in local_models.find_running():
            print(f"  A model server is running at {st['url']} ({st['api']}, {len(st['models'])} models). "
                  f"Set CONDUCTOR_LOCAL_URL={st['url']} to use it.")
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
    if s.openrouter_management_key:
        from .account import Account
        acct = Account(s.data_dir, key=s.openrouter_key, management_key=s.openrouter_management_key, mode=s.byok,
                       key_name=s.key_name)
        acct.note_catalog(cat)
        acct.refresh_sync([m.id for m in roster if m.provider == "openrouter"], force=True)
        summ = acct.summary(cat)
        for what, why in summ["errors"].items():
            bad(f"Management key: couldn't read {what}: {why}")
        k = summ["key"]
        (ok if k else bad)(f"Management key sees {summ.get('key_count') or 0} API key(s)"
                           + (f"; Conductor runs on '{k.get('name') or k.get('label')}'" if k else
                              "; couldn't match OPENROUTER_API_KEY to one of them"))
        if not k:
            names = ", ".join(repr(n) for n in acct.data.get("key_names") or [] if n)
            print(f"      Set CONDUCTOR_KEY_NAME in .env to its name on openrouter.ai/settings/keys"
                  f"{' (one of: ' + names + ')' if names else ''}. Only needed for per-key spend and BYOK key filters.")
        if summ["byok"]:
            ok(f"BYOK keys: {', '.join(b['provider'] for b in summ['byok'])}"
               + (f" (mode {s.byok})" if s.byok != "auto" else ""))
            covered = summ["byok_covered"]
            (ok if covered else print)(
                f"  {'BYOK covers ' + ', '.join(c['name'] for c in covered) if covered else '  no roster model is served by those providers'}")
            for sug in summ["byok_suggestions"]:
                print(f"      could add {sug['model']} (your {', '.join(sug['providers'])} key)")
        else:
            print(f"  No BYOK provider keys (add them at {summ['byok_settings_url']})")
        if summ["guardrail"]:
            g = summ["guardrail"]
            ok(f"Guardrail on Conductor's key: {g.get('name')}"
               + (f", ${g['limit_usd']}/{g.get('reset_interval') or 'period'}" if g.get("limit_usd") else ""))
            for b in summ["guardrail_blocked"]:
                if b["model"] in {m.id for m in roster}:
                    bad(f"Roster model {b['model']} is blocked by it ({b['why']}); the router will skip it")
    else:
        print("  OPENROUTER_MANAGEMENT_KEY not set (optional: balance, BYOK, guardrails, 30-day spend)")
    import shutil
    from .compression import Compressor, proxy_url
    hr = Compressor(s.headroom, s.workspace)
    hr_cli = shutil.which("headroom")
    if s.headroom == "off":
        print("  Headroom compression is off (CONDUCTOR_HEADROOM=off)")
    elif hr.available:
        ok(f"Headroom {hr.version or ''} is active: bulky tool output is compressed before models see it".replace("  ", " "))
    elif hr_cli:
        bad(f"The `headroom` command is installed ({hr_cli}) but Conductor's Python can't use it. "
            f"Run: {sys.executable} -m pip install headroom-ai")
    else:
        print("  Headroom not installed (optional, saves tokens): pip install -e \".[headroom]\"")
    if s.headroom_proxy != "off":
        from .compression import HeadroomProxy
        hp = proxy_url(s.headroom_proxy)
        if hp:
            ok(f"Headroom proxy running at {hp}; the Claude Code fallback goes through it")
        elif s.headroom_proxy == "auto" and HeadroomProxy.installed():
            ok("Headroom proxy installed; Conductor starts it with `conductor serve` and stops it on exit")
        elif s.headroom_proxy == "auto" and hr.available:
            print(f'  Headroom proxy not installed (optional, for the Claude Code fallback): '
                  f'{sys.executable} -m pip install "headroom-ai[proxy]"')
        else:
            print("  Headroom proxy: not running")
    from .claude_cli import find as find_claude
    if s.claude_cli == "off":
        print("  Claude Code fallback is off (CONDUCTOR_CLAUDE_CLI=off)")
    elif (exe := find_claude(s.claude_cli_path)):
        import subprocess
        try:
            ver = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception as e:
            ver = f"version check failed: {e}"
        ok(f"Claude Code found ({ver}); mode {s.claude_cli}. It runs on your Claude subscription login "
           "(run `claude` once and sign in with your Claude account if you haven't)")
    else:
        print("  Claude Code CLI not found (optional: install it to fall back to your Claude subscription)")
    from . import copilot_cli
    if s.copilot == "off":
        print("  GitHub Copilot fallback is off (CONDUCTOR_COPILOT=off)")
    elif (cp := copilot_cli.find(s.copilot_path)):
        import subprocess
        try:
            ver = subprocess.run([cp, "--version"], capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
        except Exception as e:
            ver = f"version check failed: {e}"
        ok(f"GitHub Copilot CLI found ({ver}); mode {s.copilot}. It runs on your Copilot subscription "
           "(run `copilot` once and /login if you haven't)")
        if s.copilot_headroom != "off":
            print("  Copilot through Headroom: " + ("on (Headroom is signed in to Copilot)" if copilot_cli.headroom_copilot_ready()
                  else "off until you run `headroom copilot-auth login` (optional; needs headroom-ai[proxy])"))
    else:
        print("  GitHub Copilot CLI not found (optional: npm install -g @github/copilot, to fall back to your Copilot subscription)")
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


def audit(args) -> None:
    """Print recent audit-log entries: what happened, who did it, when."""
    from .audit import AuditLog

    s = Settings.from_env()
    log = AuditLog(s.data_dir / "logs", enabled=False)
    if not log.dir.is_dir():
        print(f"No audit log yet ({log.dir}).")
        return
    rows = log.read(days=args.days)
    if args.task:
        rows = [r for r in rows if str(r.get("task", "")).startswith(args.task)]
    if args.kind:
        rows = [r for r in rows if str(r.get("kind", "")).startswith(args.kind)]
    if args.json:
        for r in rows[-args.lines:]:
            print(json.dumps(r, ensure_ascii=False))
        return
    skip = {"ts", "kind", "actor", "task"}
    for r in rows[-args.lines:]:
        ts = str(r.get("ts", ""))
        when = ts[:10] + " " + ts[11:23] if len(ts) >= 23 else ts
        detail = r.get("summary") or r.get("label") or r.get("text") or r.get("path") or ""
        extra = " ".join(", ".join(f"{k}={v}" for k, v in r.items() if k not in skip and v != detail
                                   and not isinstance(v, (dict, list))).split())[:160]
        detail = " ".join(str(detail).split())
        colour = RED if r.get("ok") is False or "error" in str(r.get("kind")) else (CYAN if r.get("actor") == "user" else "")
        print(f"{DIM}{when}{RESET} {colour}{r.get('actor', '?'):6} {r.get('kind', '?'):24}{RESET} "
              f"{DIM}{r.get('task', '') or '':10}{RESET} {str(detail)[:90]}  {DIM}{extra}{RESET}")
    print(f"{DIM}({min(len(rows), args.lines)} of {len(rows)} entries; files in {log.dir}){RESET}")


def main() -> None:
    from pathlib import Path

    from .config import _load_dotenv
    _load_dotenv(Path(".env"))  # so CONDUCTOR_HOST / CONDUCTOR_PORT in .env apply to the defaults below
    p = argparse.ArgumentParser(prog="conductor", description="Your self-hosted AI conductor.")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default=os.environ.get("CONDUCTOR_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("CONDUCTOR_PORT", "8765")))
    s.add_argument("--lan", action="store_true", help="reachable from other devices on your network (passcode-protected)")
    s.add_argument("--stay-awake", action="store_true", help="keep the computer from sleeping while serving")
    sub.add_parser("doctor", help="check keys, models, benchmarks, and the browser")
    au = sub.add_parser("audit", help="show the local audit log")
    au.add_argument("-n", "--lines", type=int, default=50, help="how many entries (default 50)")
    au.add_argument("--task", help="only this task id (prefix ok)")
    au.add_argument("--kind", help="only kinds starting with this, e.g. tool, ui, http, model")
    au.add_argument("--days", type=int, default=7, help="how many daily files to read (default 7)")
    au.add_argument("--json", action="store_true", help="raw JSON lines")
    ch = sub.add_parser("chat", help="chat in the terminal")
    ch.add_argument("--auto", action="store_true", help="auto-approve everything up to high risk (critical still asks)")
    ch.add_argument("prompt", nargs="*")
    args = p.parse_args()
    if args.cmd == "chat":
        chat(args)
    elif args.cmd == "audit":
        audit(args)
    elif args.cmd == "doctor":
        doctor(args)
    else:
        if args.cmd is None:
            args.host, args.port = os.environ.get("CONDUCTOR_HOST", "127.0.0.1"), int(os.environ.get("CONDUCTOR_PORT", "8765"))
            args.lan = args.stay_awake = False
        serve(args)


if __name__ == "__main__":
    main()
