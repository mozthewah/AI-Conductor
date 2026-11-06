"""Recurring and future tasks."""

from __future__ import annotations

import time
from datetime import datetime

from . import Tool, ToolContext, ToolError


async def schedule_task(ctx: ToolContext, args: dict) -> str:
    every = args.get("every_minutes")
    at = args.get("start_at")
    if every is not None and int(every) < 5:
        raise ToolError("every_minutes must be at least 5")
    if at:
        try:
            dt = datetime.fromisoformat(at)
            next_run = dt.timestamp() if dt.tzinfo else time.mktime(dt.timetuple())
        except ValueError:
            raise ToolError("start_at must be ISO 8601, e.g. 2026-11-06T08:30")
    elif every:
        next_run = time.time() + int(every) * 60
    else:
        raise ToolError("Give every_minutes, start_at, or both.")
    sid = ctx.store.add_schedule(args["prompt"], next_run, int(every) if every else None)
    when = datetime.fromtimestamp(next_run).strftime("%a %b %d %H:%M")
    await ctx.emit("schedule", {"id": sid, "prompt": args["prompt"], "next_run": next_run, "every_minutes": every})
    return f"Scheduled {sid}: first run {when}" + (f", then every {every} min." if every else ".")


async def list_schedules(ctx: ToolContext, args: dict) -> str:
    rows = ctx.store.list_schedules()
    if not rows:
        return "No scheduled tasks."
    return "\n".join(
        f"{r['id']}: next {datetime.fromtimestamp(r['next_run']):%Y-%m-%d %H:%M}"
        f"{' every ' + str(r['every_minutes']) + ' min' if r['every_minutes'] else ''}"
        f"{'' if r['enabled'] else ' (paused)'} - {r['prompt'][:100]}" for r in rows)


async def cancel_schedule(ctx: ToolContext, args: dict) -> str:
    if not ctx.store.delete_schedule(args["id"]):
        raise ToolError(f"No schedule {args['id']}")
    return f"Cancelled {args['id']}."


TOOLS = [
    Tool("schedule_task",
         "Run a task later or on repeat (e.g. a morning briefing, a weekly report, a price watch). The "
         "prompt must be self-contained - it runs as a fresh task with your memory and skills but not "
         "this conversation. Times are the server's local time.",
         {"prompt": {"type": "string"},
          "every_minutes": {"type": "integer", "description": "Repeat interval; 1440 = daily, 10080 = weekly"},
          "start_at": {"type": "string", "description": "ISO 8601 local time of first run"}},
         ["prompt"], schedule_task, 
         summarize=lambda a: "Scheduled a task"),
    Tool("list_schedules", "List scheduled tasks.", {}, [], list_schedules, 
         summarize=lambda a: "Checked schedules"),
    Tool("cancel_schedule", "Cancel a scheduled task by id.", {"id": {"type": "string"}}, ["id"],
         cancel_schedule,  summarize=lambda a: f"Cancelled {a.get('id')}"),
]
