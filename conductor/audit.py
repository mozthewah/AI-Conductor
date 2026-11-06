"""A local, append-only audit log of everything Conductor does.

One JSON object per line in data/logs/audit-YYYY-MM-DD.jsonl:

    {"ts": "2026-11-06T09:41:07.123-05:00", "kind": "tool.call", "actor": "model",
     "task": "t_ab12", "tool": "run_shell", "summary": "$ go test ./...", "ok": true, "ms": 4210}

Long text is trimmed to a preview (content matters less than what happened), and anything that
looks like an API key or token is scrubbed before it's written. Set CONDUCTOR_AUDIT=off to disable.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

PREVIEW = 300
SECRET = re.compile(
    r"(sk-(?:or|ant|proj)?-?[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9\-]{10,}"
    r"|AKIA[0-9A-Z]{16}|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}|(?i:(?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S{6,})")


def scrub(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        v = SECRET.sub(lambda m: m.group(0)[:6] + "…[redacted]", value)
        return v if len(v) <= PREVIEW else f"{v[:PREVIEW]}… ({len(v)} chars)"
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {str(k): scrub(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        items = [scrub(v, depth + 1) for v in list(value)[:20]]
        return items + [f"… ({len(value) - 20} more)"] if len(value) > 20 else items
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub(str(value), depth)


class AuditLog:
    def __init__(self, directory: Path, enabled: bool = True):
        self.dir = Path(directory)
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, day: datetime | None = None) -> Path:
        return self.dir / f"audit-{(day or datetime.now()).strftime('%Y-%m-%d')}.jsonl"

    def write(self, kind: str, actor: str = "system", task: str | None = None, **fields: Any) -> None:
        if not self.enabled:
            return
        now = datetime.now().astimezone()
        record = {"ts": now.isoformat(timespec="milliseconds"), "kind": kind, "actor": actor}
        if task:
            record["task"] = task
        record.update(scrub({k: v for k, v in fields.items() if v is not None}))
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._lock, self.path_for(now).open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # never let logging break the work

    def read(self, days: int = 7) -> list[dict]:
        files = sorted(self.dir.glob("audit-*.jsonl"))[-days:]
        out = []
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out


# How each conductor event shows up in the audit log: (kind, actor). Events not listed are logged
# as "event.<type>" with actor "system"; streaming deltas are never logged.
EVENT_KINDS = {
    "user": ("task.message", "user"), "user_delivered": ("task.message_delivered", "system"),
    "routed": ("route.decided", "system"), "escalated": ("route.model_changed", "system"),
    "assistant_text": ("model.reply", "model"), "progress": ("model.note", "model"),
    "tool_call": ("tool.call", "model"), "tool_result": ("tool.result", "system"),
    "server_tool_input": ("tool.web", "model"), "server_tool_result": ("tool.web_result", "system"),
    "artifact": ("file.changed", "model"), "screenshot": ("browser.screenshot", "model"),
    "plan": ("plan.updated", "model"), "plan_parts": ("compose.split", "model"), "part": ("compose.part", "system"),
    "steer": ("compose.steer", "system"), "blocked": ("compose.blocked", "model"),
    "request": ("ask.waiting_for_user", "system"), "request_resolved": ("ask.answered", "user"),
    "approval": ("safety.auto_decision", "system"), "consulted": ("model.consult", "model"),
    "memory": ("memory.changed", "model"), "skill": ("skill.changed", "model"), "learned": ("learning.saved", "system"),
    "schedule": ("schedule.created", "model"), "process": ("process", "model"), "show": ("ui.shown_to_user", "model"),
    "message": ("model.message_user", "model"), "paused": ("credits.paused", "system"),
    "resumed": ("credits.resumed", "system"), "credits_low": ("credits.low", "system"),
    "done": ("task.done", "system"), "error": ("task.error", "system"), "cancelled": ("task.cancelled", "user"),
    "feedback": ("feedback", "user"), "status": ("status", "system"), "subagent": ("compose.subagent", "system"),
}
