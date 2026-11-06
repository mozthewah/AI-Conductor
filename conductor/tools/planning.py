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


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


async def show_user(ctx: ToolContext, args: dict) -> str:
    from pathlib import Path
    from urllib.parse import quote
    target = args["target"].strip()
    if target.startswith(("http://", "https://")):
        href, is_image, label = target, Path(target.split("?")[0]).suffix.lower() in IMAGE_EXT, target
    else:
        root = ctx.settings.workspace.resolve()
        p = (root / target.removeprefix("file://").lstrip("/")).resolve() if not Path(target).is_absolute() \
            else Path(target.removeprefix("file://")).resolve()
        if not p.is_relative_to(root) or not p.exists():
            raise ToolError(f"No file at {target} in the workspace.")
        rel = str(p.relative_to(root))
        href, is_image, label = f"/api/files/raw?path={quote(rel)}", p.suffix.lower() in IMAGE_EXT, rel
    payload = {"title": args.get("title") or label, "note": args.get("note", ""), "href": href,
               "image": is_image, "label": label}
    await ctx.emit("show", payload)
    root_task = ctx.conductor._root(ctx.task_id)
    if root_task and root_task["id"] != ctx.task_id:
        await ctx.conductor.emit(root_task["id"], "show", payload)
    return f"Shown to the user: {label}"


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
        name="show_user",
        description=(
            "Put something in front of the user: a link they can click (e.g. a local page a tool is serving, "
            "like http://127.0.0.1:41963/) or a workspace file (images and mockups get a preview). Your "
            "browser is private - the user never sees it - so anything they need to look at or click must "
            "go through here."
        ),
        properties={"target": {"type": "string", "description": "URL, or a path in the workspace"},
                    "title": {"type": "string"}, "note": {"type": "string", "description": "What to look at / do"}},
        required=["target"],
        handler=show_user,
        summarize=lambda a: f"Showed you {a.get('title') or a.get('target', '')[:60]}",
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
