"""SQLite persistence: tasks, transcripts, event logs, long-term memory, schedules.

Everything the conductor knows lives in one file (data/conductor.db), so it
survives restarts and you can inspect it with any SQLite browser.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | running | waiting | done | error | cancelled
    system_prompt TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '[]',
    answer TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'user', -- user | schedule | subagent
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS events (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- preference | fact | person | project | episode | note
    content TEXT NOT NULL,
    source_task TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_used REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, kind, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, kind) VALUES (new.id, new.content, new.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, kind)
    VALUES ('delete', old.id, old.content, old.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, kind ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, kind)
    VALUES ('delete', old.id, old.content, old.kind);
    INSERT INTO memories_fts(rowid, content, kind) VALUES (new.id, new.content, new.kind);
END;
CREATE TABLE IF NOT EXISTS runs (           -- one row per task: what the router learns from
    task_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    category TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    routed_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    feedback INTEGER,                    -- +1 / -1 / NULL
    cost_usd REAL NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    every_minutes INTEGER,           -- NULL = one-shot
    next_run REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_task_id TEXT,
    created_at REAL NOT NULL
);
"""

# Kinds that describe *the user* - always injected into the system prompt.
PROFILE_KINDS = ("preference", "person")

_STOPWORDS = set(
    "a an the and or but if then of to in on for with at by from is are was were be been "
    "it this that these those i me my you your we our can could would should please do does "
    "did how what when where why who which make get let about into up out over just".split()
)


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        run_cols = {r[1] for r in self._db.execute("PRAGMA table_info(runs)")}
        for name, ddl in [("domain", "TEXT NOT NULL DEFAULT 'general'"), ("review", "REAL"),
                          ("parent_id", "TEXT"), ("mode", "TEXT NOT NULL DEFAULT 'direct'")]:
            if name not in run_cols:
                self._db.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(tasks)")}
        for name, ddl in [("model", "TEXT"), ("route_json", "TEXT NOT NULL DEFAULT '{}'"),
                          ("compare_of", "TEXT"), ("cost_usd", "REAL NOT NULL DEFAULT 0")]:
            if name not in cols:
                self._db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")

    # -- helpers ---------------------------------------------------------
    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self._lock:
            row = self._db.execute(sql, params).fetchone()
            return dict(row) if row else None

    # -- tasks -----------------------------------------------------------
    def create_task(self, title: str, *, parent_id: str | None = None, origin: str = "user",
                    system_prompt: str = "", model: str | None = None, compare_of: str | None = None) -> str:
        tid = new_id("t_")
        now = time.time()
        self._exec(
            "INSERT INTO tasks (id, parent_id, title, status, system_prompt, origin, model, compare_of,"
            " created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (tid, parent_id, title[:200], system_prompt, origin, model, compare_of, now, now),
        )
        return tid

    def get_task(self, task_id: str) -> dict | None:
        t = self._one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if t:
            t["plan"] = json.loads(t.pop("plan_json"))
            t["route"] = json.loads(t.pop("route_json") or "{}")
        return t

    def list_tasks(self, limit: int = 100, include_children: bool = False) -> list[dict]:
        where = "" if include_children else "WHERE parent_id IS NULL"
        rows = self._all(
            f"SELECT id, parent_id, title, status, origin, model, compare_of, created_at, updated_at FROM tasks {where}"
            " ORDER BY updated_at DESC LIMIT ?", (limit,))
        return rows

    def child_tasks(self, task_id: str) -> list[dict]:
        return self._all("SELECT id, title, status, answer FROM tasks WHERE parent_id = ? ORDER BY created_at",
                         (task_id,))

    def update_task(self, task_id: str, **fields: Any) -> None:
        if "plan" in fields:
            fields["plan_json"] = json.dumps(fields.pop("plan"))
        if "route" in fields:
            fields["route_json"] = json.dumps(fields.pop("route"))
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    def add_usage(self, task_id: str, input_tokens: int, output_tokens: int, cost: float = 0.0) -> None:
        self._exec("UPDATE tasks SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,"
                   " cost_usd = cost_usd + ? WHERE id = ?", (input_tokens, output_tokens, cost, task_id))

    def compares(self, task_id: str) -> list[dict]:
        """All tasks in the same comparison group (original + retries with other models)."""
        t = self.get_task(task_id)
        root = (t or {}).get("compare_of") or task_id
        return self._all("SELECT id, model, status, cost_usd FROM tasks WHERE id = ? OR compare_of = ?"
                         " ORDER BY created_at", (root, root))

    # -- router learning ---------------------------------------------------
    def start_run(self, task_id: str, model: str, category: str, difficulty: str, routed_by: str,
                  domain: str = "general", mode: str = "direct", parent_id: str | None = None) -> None:
        """category = the skill (coding, ui_design, research...)."""
        self._exec("INSERT OR REPLACE INTO runs (task_id, model, category, domain, difficulty, routed_by, mode,"
                   " parent_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (task_id, model, category, domain, difficulty, routed_by, mode, parent_id, time.time()))

    def set_review(self, task_id: str, score: float) -> bool:
        """The conductor's verdict on a specialist's work (0-1)."""
        return self._exec("UPDATE runs SET review = ? WHERE task_id = ?", (score, task_id)).rowcount > 0

    def finish_run(self, task_id: str, status: str, cost: float, seconds: float) -> None:
        self._exec("UPDATE runs SET status = ?, cost_usd = cost_usd + ?, seconds = seconds + ? WHERE task_id = ?",
                   (status, cost, seconds, task_id))

    def set_feedback(self, task_id: str, value: int | None) -> bool:
        return self._exec("UPDATE runs SET feedback = ? WHERE task_id = ?", (value, task_id)).rowcount > 0

    def get_run(self, task_id: str) -> dict | None:
        return self._one("SELECT * FROM runs WHERE task_id = ?", (task_id,))

    # Strongest signal first: your thumbs, then the conductor's review, then "it finished".
    _REWARD = ("CASE WHEN feedback > 0 THEN 1.0 WHEN feedback < 0 THEN 0.0 WHEN review IS NOT NULL THEN review"
               " WHEN status = 'done' THEN 0.7 WHEN status = 'cancelled' THEN 0.4 ELSE 0.0 END")

    def run_stats(self) -> list[dict]:
        return self._all(
            f"SELECT model, category, domain, COUNT(*) AS runs, SUM({self._REWARD}) AS reward FROM runs"
            " WHERE status != 'running' GROUP BY model, category, domain")

    def leaderboard(self) -> list[dict]:
        return self._all(
            f"SELECT model, category, domain, COUNT(*) AS runs, SUM({self._REWARD}) AS reward,"
            " SUM(feedback > 0) AS up, SUM(feedback < 0) AS down, SUM(status = 'error') AS errors,"
            " AVG(review) AS avg_review, AVG(cost_usd) AS avg_cost, AVG(seconds) AS avg_seconds"
            " FROM runs WHERE status != 'running' GROUP BY model, category, domain ORDER BY category, domain")

    def model_stats(self, category: str | None = None) -> list[dict]:
        where = "WHERE status != 'running'" + (" AND category = ?" if category else "")
        return self._all(
            f"SELECT model, {'category, ' if not category else ''}COUNT(*) AS runs, SUM({self._REWARD}) AS reward,"
            " SUM(feedback > 0) AS up, SUM(feedback < 0) AS down, SUM(status = 'error') AS errors,"
            " AVG(cost_usd) AS avg_cost, AVG(seconds) AS avg_seconds FROM runs "
            f"{where} GROUP BY model{', category' if not category else ''} ORDER BY runs DESC",
            (category,) if category else ())

    def delete_task(self, task_id: str) -> None:
        for child in self.child_tasks(task_id):
            self.delete_task(child["id"])
        for table in ("messages", "events", "runs"):
            self._exec(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
        self._exec("DELETE FROM tasks WHERE id = ?", (task_id,))

    # -- transcript (append-only: the API ties thinking to exact history) --
    def append_message(self, task_id: str, role: str, content: Any) -> None:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE task_id = ?",
                                   (task_id,)).fetchone()
            self._db.execute("INSERT INTO messages (task_id, seq, role, content_json) VALUES (?, ?, ?, ?)",
                             (task_id, row[0], role, json.dumps(content)))
            self._db.commit()

    def load_messages(self, task_id: str) -> list[dict]:
        rows = self._all("SELECT role, content_json FROM messages WHERE task_id = ? ORDER BY seq", (task_id,))
        return [{"role": r["role"], "content": json.loads(r["content_json"])} for r in rows]

    # -- events ----------------------------------------------------------
    def append_event(self, task_id: str, type_: str, data: dict) -> dict:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE task_id = ?",
                                   (task_id,)).fetchone()
            ev = {"task_id": task_id, "seq": row[0], "ts": time.time(), "type": type_, "data": data}
            self._db.execute("INSERT INTO events (task_id, seq, ts, type, data_json) VALUES (?, ?, ?, ?, ?)",
                             (task_id, ev["seq"], ev["ts"], type_, json.dumps(data)))
            self._db.commit()
            return ev

    def load_events(self, task_id: str, after: int = -1) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE task_id = ? AND seq > ? ORDER BY seq", (task_id, after))
        return [{"task_id": r["task_id"], "seq": r["seq"], "ts": r["ts"], "type": r["type"],
                 "data": json.loads(r["data_json"])} for r in rows]

    # -- memory ----------------------------------------------------------
    def add_memory(self, kind: str, content: str, source_task: str | None = None) -> int:
        content = content.strip()
        dup = self._one("SELECT id FROM memories WHERE lower(content) = lower(?)", (content,))
        if dup:
            return dup["id"]
        cur = self._exec("INSERT INTO memories (kind, content, source_task, created_at) VALUES (?, ?, ?, ?)",
                         (kind, content, source_task, time.time()))
        return int(cur.lastrowid)

    def update_memory(self, memory_id: int, content: str) -> bool:
        return self._exec("UPDATE memories SET content = ? WHERE id = ?", (content.strip(), memory_id)).rowcount > 0

    def delete_memory(self, memory_id: int) -> bool:
        return self._exec("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def list_memories(self, kind: str | None = None, limit: int = 500) -> list[dict]:
        if kind:
            return self._all("SELECT * FROM memories WHERE kind = ? ORDER BY created_at DESC LIMIT ?", (kind, limit))
        return self._all("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,))

    def search_memories(self, query: str, limit: int = 8, exclude_kinds: tuple[str, ...] = ()) -> list[dict]:
        terms = [w for w in re.findall(r"[A-Za-z0-9_]{3,}", query.lower()) if w not in _STOPWORDS]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"*' for t in dict.fromkeys(terms))
        excl = ""
        params: list[Any] = [fts_query]
        if exclude_kinds:
            excl = f" AND m.kind NOT IN ({','.join('?' * len(exclude_kinds))})"
            params += list(exclude_kinds)
        params.append(limit)
        rows = self._all(
            "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid"
            f" WHERE memories_fts MATCH ?{excl} ORDER BY bm25(memories_fts) LIMIT ?", params)
        if rows:
            now = time.time()
            with self._lock:
                self._db.executemany("UPDATE memories SET uses = uses + 1, last_used = ? WHERE id = ?",
                                     [(now, r["id"]) for r in rows])
                self._db.commit()
        return rows

    # -- schedules -------------------------------------------------------
    def add_schedule(self, prompt: str, next_run: float, every_minutes: int | None) -> str:
        sid = new_id("s_")
        self._exec("INSERT INTO schedules (id, prompt, every_minutes, next_run, created_at) VALUES (?, ?, ?, ?, ?)",
                   (sid, prompt, every_minutes, next_run, time.time()))
        return sid

    def list_schedules(self) -> list[dict]:
        return self._all("SELECT * FROM schedules ORDER BY next_run")

    def due_schedules(self, now: float) -> list[dict]:
        return self._all("SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?", (now,))

    def update_schedule(self, sid: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE schedules SET {cols} WHERE id = ?", (*fields.values(), sid))

    def delete_schedule(self, sid: str) -> bool:
        return self._exec("DELETE FROM schedules WHERE id = ?", (sid,)).rowcount > 0
