"""Fires scheduled tasks. Runs inside the web server process."""

from __future__ import annotations

import asyncio
import logging
import time

from .agent import Conductor

log = logging.getLogger("conductor.scheduler")


async def tick(conductor: Conductor, now: float | None = None) -> list[str]:
    now = now or time.time()
    started = []
    for s in conductor.store.due_schedules(now):
        task_id = conductor.start_task(f"[Scheduled] {s['prompt']}", origin="schedule")
        started.append(task_id)
        if s["every_minutes"]:
            nxt = s["next_run"]
            while nxt <= now:  # skip missed runs instead of firing a burst
                nxt += s["every_minutes"] * 60
            conductor.store.update_schedule(s["id"], next_run=nxt, last_task_id=task_id)
        else:
            conductor.store.update_schedule(s["id"], enabled=0, last_task_id=task_id)
        log.info("schedule %s started task %s", s["id"], task_id)
    return started


async def run_forever(conductor: Conductor, interval: float = 20.0) -> None:
    while True:
        try:
            await tick(conductor)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval)
