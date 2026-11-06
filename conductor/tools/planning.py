"""Planning + talking to the user mid-task."""

from __future__ import annotations

from . import Tool, ToolContext, ToolError

STATUSES = ["pending", "in_progress", "blocked", "done", "skipped"]


async def update_plan(ctx: ToolContext, args: dict) -> str:
    items = []
    for raw in args["steps"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("step"), str):
            raise ToolError("each step needs a 'step' string and a 'status'")
        status = raw.get("status", "pending")
        if status not in STATUSES:
            raise ToolError(f"status must be one of {STATUSES}")
        items.append({"step": raw["step"], "status": status})
    ctx.store.update_task(ctx.task_id, plan=items)
    await ctx.emit("plan", {"steps": items})
    done = sum(1 for i in items if i["status"] in ("done", "skipped"))
    return f"Plan updated ({done}/{len(items)} complete)."


async def message_user(ctx: ToolContext, args: dict) -> str:
    await ctx.emit("message", {"text": args["text"]})
    return "Shown to the user."


TOOLS = [
    Tool(
        name="update_plan",
        description=(
            "Show the user your step-by-step plan as a live checklist. Call it once you understand a "
            "multi-step task, then again whenever a step's status changes. Send the full list each time."
        ),
        properties={
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string"},
                        "status": {"type": "string", "enum": STATUSES},
                    },
                    "required": ["step", "status"],
                },
            }
        },
        required=["steps"],
        handler=update_plan,
        summarize=lambda a: f"Updated plan ({len(a.get('steps', []))} steps)",
    ),
    Tool(
        name="message_user",
        description=(
            "Send the user a message right now, verbatim, in the middle of a long task - e.g. an "
            "interim finding, an exact value, or a link they asked to see early. Reserve it for content "
            "that needs to reach them verbatim before you finish; your final reply is shown anyway."
        ),
        properties={"text": {"type": "string", "description": "Markdown message"}},
        required=["text"],
        handler=message_user,
        summarize=lambda a: "Messaged you",
    ),
]
