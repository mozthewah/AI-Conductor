"""Update Conductor in place: git pull, reinstall if dependencies changed, restart the process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_restart_hooks: list = []  # cleanups to run right before the process replaces itself (e.g. stay-awake holder)


def on_restart(fn) -> None:
    _restart_hooks.append(fn)


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)


def status(fetch: bool = True) -> dict:
    """Where this copy is, and whether the branch it tracks has anything new."""
    if not (REPO / ".git").exists():
        return {"can_update": False, "error": f"{REPO} isn't a git checkout, so it can't update itself."}
    out: dict = {"repo": str(REPO), "can_update": True, "error": ""}
    out["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    out["commit"] = _git("rev-parse", "--short", "HEAD").stdout.strip()
    out["subject"] = _git("log", "-1", "--format=%s").stdout.strip()
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        return {**out, "can_update": False, "error": f"Branch '{out['branch']}' doesn't track a remote branch."}
    out["upstream"] = upstream.stdout.strip()
    if fetch:
        f = _git("fetch", "--quiet", timeout=90)
        if f.returncode != 0:
            out["error"] = "Couldn't reach the remote: " + (f.stderr.strip().splitlines() or ["unknown error"])[-1]
    out["behind"] = int(_git("rev-list", "--count", "HEAD..@{u}").stdout.strip() or 0)
    out["ahead"] = int(_git("rev-list", "--count", "@{u}..HEAD").stdout.strip() or 0)
    out["changes"] = [l for l in _git("log", "--format=%h %s", "HEAD..@{u}").stdout.splitlines()][:20]
    out["dirty"] = bool(_git("status", "--porcelain", "--untracked-files=no").stdout.strip())
    return out


def update() -> dict:
    """git pull --ff-only, then reinstall if the dependencies changed. Doesn't restart."""
    before = _git("rev-parse", "HEAD").stdout.strip()
    pull = _git("pull", "--ff-only", timeout=180)
    log = (pull.stdout + pull.stderr).strip()
    if pull.returncode != 0:
        return {"ok": False, "log": log, "error": "git pull failed (see log). Nothing was changed."}
    after = _git("rev-parse", "HEAD").stdout.strip()
    if after == before:
        return {"ok": True, "updated": False, "log": log or "Already up to date."}
    changed = _git("diff", "--name-only", before, after).stdout.split()
    if "pyproject.toml" in changed:  # new or changed dependencies
        have = [name for name, mod in (("browser", "playwright"), ("headroom", "headroom")) if _importable(mod)]
        extras = f"[{','.join(have)}]" if have else ""  # keep the optional extras you already have
        pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", f"{REPO}{extras}"],
                             capture_output=True, text=True, timeout=600)
        log += "\n\n$ pip install -e .\n" + (pip.stdout + pip.stderr).strip()
        if pip.returncode != 0:
            return {"ok": False, "updated": True, "log": log,
                    "error": "Pulled the update, but installing its dependencies failed (see log). Not restarting."}
    return {"ok": True, "updated": True, "from": before[:7], "to": after[:7], "log": log, "files": changed[:40]}


def _importable(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


def restart() -> None:
    """Replace this process with a fresh copy of itself (same command line, same PID)."""
    for fn in _restart_hooks:
        try:
            fn()
        except Exception:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    argv = list(sys.argv)
    if argv and argv[0].endswith(("cli.py", "__main__.py")):  # started as `python -m conductor.cli`
        argv = ["-m", "conductor.cli", *argv[1:]]
    os.execv(sys.executable, [sys.executable, *argv])
