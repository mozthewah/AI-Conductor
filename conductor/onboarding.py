"""First-time setup and importing from other assistants.

Import sources (read-only; nothing is changed in them):
  - Local agents: OpenClaw (~/.openclaw), Hermes (~/.hermes), Claude Code (~/.claude), Codex (~/.codex),
    Gemini CLI (~/.gemini), Cursor (~/.cursor). Their memory/instruction Markdown files become memory
    candidates; their SKILL.md skills can be copied into Conductor's skill library.
  - Pasted text: ask ChatGPT / Claude / Gemini to list what they remember about you, paste it here.
Everything is previewed as a checklist first; only what you tick is imported.

Keys: OPENROUTER_API_KEY / OPENROUTER_MANAGEMENT_KEY / ANTHROPIC_API_KEY found in those agents' .env
files can be offered (masked) for Conductor's own .env, from the computer itself only.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

HOME = Path.home()
MAX_ITEMS = 300
KEY_NAMES = ("OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "ANTHROPIC_API_KEY")
# What the Setup page may write to .env: the keys above plus your own model server (local.py).
SETUP_FIELDS = KEY_NAMES + ("CONDUCTOR_LOCAL_URL", "CONDUCTOR_LOCAL_KEY")

# name -> (folder, memory/instruction files (globs, relative), skill roots (relative))
AGENTS = {
    "openclaw": ("OpenClaw", ".openclaw",
                 ["workspace/USER.md", "workspace/MEMORY.md", "workspace/SOUL.md", "workspace/memory/*.md",
                  "USER.md", "MEMORY.md", "SOUL.md"], ["skills", "workspace/skills"]),
    "hermes": ("Hermes", ".hermes", ["memories/USER.md", "memories/MEMORY.md", "USER.md", "MEMORY.md", "SOUL.md"],
               ["skills"]),
    "claude": ("Claude Code", ".claude", ["CLAUDE.md"], ["skills"]),
    "codex": ("Codex", ".codex", ["AGENTS.md"], ["skills"]),
    "gemini": ("Gemini CLI", ".gemini", ["GEMINI.md"], []),
    "cursor": ("Cursor", ".cursor", ["rules/*.md", "rules/*.mdc"], []),
}

PASTE_PROMPT = ("Please list everything you remember or know about me from our past conversations: my name, "
                "work and projects, preferences (how I like answers, tools and languages I use), people and "
                "places I've mentioned, and anything else worth remembering. One fact per line, as a plain "
                "bulleted list, no commentary.")

PREF_RX = re.compile(r"\b(prefer|prefers|like|likes|love|hate|dislike|want|wants|don't|do not|never|always|"
                     r"rather|style|tone|concise|verbose|please)\b", re.I)
PERSON_RX = re.compile(r"\b(i am|i'm|my name|name is|lives? in|works? (at|as)|job|role|age|pronouns|partner|"
                       r"wife|husband|kids?|dog|cat|birthday|timezone|based in)\b", re.I)
PROJECT_RX = re.compile(r"\b(project|repo|building|app|website|startup|working on)\b", re.I)


# ------------------------------------------------------------------ memories from text
def extract_items(text: str, source: str = "") -> list[dict]:
    """Bullets and short lines -> memory candidates (with a guessed kind you can change)."""
    out, seen, in_code, in_front = [], set(), False, False
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if i == 0 and line == "---":
            in_front = True
            continue
        if in_front:
            in_front = line != "---"
            continue
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line or line.startswith(("#", "|", ">", "<!--")) or set(line) <= set("-=*_ "):
            continue
        item = re.sub(r"^(?:[-*•+]|\d+[.)]|\[[ xX]\])\s*", "", line).strip()
        item = re.sub(r"\*\*(.+?)\*\*", r"\1", item).strip()
        if len(item) < 8 or len(item) > 400 or item.endswith(":"):
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": guess_kind(item, source), "content": item, "source": source})
        if len(out) >= MAX_ITEMS:
            break
    return out


def guess_kind(item: str, source: str = "") -> str:
    if PREF_RX.search(item):
        return "preference"
    if PERSON_RX.search(item) or source.upper().endswith("USER.MD"):
        return "person"
    if PROJECT_RX.search(item):
        return "project"
    return "fact"


# ------------------------------------------------------------------ scanning local agents
def _skill_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found = []
    for p in root.rglob("SKILL.md"):
        if len(p.relative_to(root).parts) <= 5:
            found.append(p)
        if len(found) >= 200:
            break
    return sorted(found)


def _skill_meta(path: Path) -> dict:
    from .skills import _parse
    try:
        sk = _parse(path.read_text(errors="replace"), path.parent.name)
    except OSError:
        return {}
    return {"name": sk.name, "description": sk.description, "path": str(path), "chars": len(sk.body)}


def _env_keys(path: Path) -> dict[str, str]:
    keys = {}
    try:
        for raw in path.read_text(errors="replace").splitlines():
            k, sep, v = raw.strip().partition("=")
            k, v = k.strip().removeprefix("export ").strip(), v.split(" #", 1)[0].strip().strip('"').strip("'")
            if sep and k in KEY_NAMES and len(v) > 12:
                keys[k] = v
    except OSError:
        pass
    return keys


def mask(value: str) -> str:
    return value[:10] + "…" + value[-4:] if len(value) > 16 else "…"


def scan(home: Path | None = None) -> list[dict]:
    """What other assistants on this computer have that Conductor could import."""
    home = Path(home or HOME)
    sources = []
    for sid, (label, folder, mem_globs, skill_roots) in AGENTS.items():
        base = home / folder
        if not base.is_dir():
            continue
        files = []
        for g in mem_globs:
            for p in sorted(base.glob(g))[-40:]:  # e.g. the latest daily memory notes
                if p.is_file() and p not in files and p.stat().st_size < 2_000_000:
                    files.append(p)
        skills = [m for r in skill_roots for m in map(_skill_meta, _skill_files(base / r)) if m]
        keys = {}
        for env in (base / ".env", base / "workspace" / ".env"):
            keys.update(_env_keys(env))
        if files or skills or keys:
            sources.append({"id": sid, "label": label, "path": str(base),
                            "files": [str(p) for p in files], "skills": skills,
                            "keys": {k: mask(v) for k, v in keys.items()}})
    return sources


def preview(source: dict) -> list[dict]:
    items, seen = [], set()
    for f in source.get("files", []):
        try:
            text = Path(f).read_text(errors="replace")
        except OSError:
            continue
        for it in extract_items(text, Path(f).name):
            if it["content"].lower() not in seen:
                seen.add(it["content"].lower())
                items.append({**it, "source": f"{source['label']}: {Path(f).name}"})
    return items[:MAX_ITEMS]


def source_keys(source_id: str, home: Path | None = None) -> dict[str, str]:
    """The real key values (server side only) for a scanned source."""
    home = Path(home or HOME)
    if source_id not in AGENTS:
        return {}
    base = home / AGENTS[source_id][1]
    keys = {}
    for env in (base / ".env", base / "workspace" / ".env"):
        keys.update(_env_keys(env))
    return keys


# ------------------------------------------------------------------ Conductor's own .env and setup state
def write_env(updates: dict[str, str], path: Path | None = None) -> Path:
    """Set KEY=value lines in Conductor's .env (replacing existing ones, keeping everything else)."""
    path = Path(path or ".env")
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    for i, raw in enumerate(lines):
        k = raw.split("=", 1)[0].strip()
        if k in updates and not raw.lstrip().startswith("#"):
            lines[i] = f"{k}={updates[k]}"
            done.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    path.write_text("\n".join(lines) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "setup.json"


def load_state(data_dir: Path) -> dict:
    try:
        return json.loads(state_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, **fields) -> dict:
    st = {**load_state(data_dir), **fields, "updated": time.time()}
    state_path(data_dir).write_text(json.dumps(st, indent=1))
    return st
