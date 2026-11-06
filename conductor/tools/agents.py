"""Sub-agents and human-in-the-loop."""

from __future__ import annotations

import asyncio

from . import Tool, ToolContext, ToolError, clip

MAX_PARALLEL = 6


async def delegate(ctx: ToolContext, args: dict) -> str:
    jobs = args["tasks"]
    if not jobs or not all(isinstance(j, dict) and isinstance(j.get("instructions"), str) for j in jobs):
        raise ToolError("tasks must be a list of {title, instructions}")
    if len(jobs) > MAX_PARALLEL:
        raise ToolError(f"At most {MAX_PARALLEL} sub-agents at once; batch the rest.")
    results = await asyncio.gather(*[
        ctx.conductor.run_subagent(ctx.task_id, j.get("title") or j["instructions"][:60], j["instructions"])
        for j in jobs
    ])
    parts = [f"## Sub-agent {i + 1}: {jobs[i].get('title', '')}\n{r}" for i, r in enumerate(results)]
    return clip("\n\n".join(parts), 60_000)


async def ask_user(ctx: ToolContext, args: dict) -> str:
    reply = await ctx.conductor.ask_user(ctx.task_id, args["question"], args.get("options") or [])
    if reply is None:
        return "The user didn't answer (unattended run). Make a sensible choice, say what you assumed, and continue."
    return f"User replied: {reply}"


TOOLS = [
    Tool(
        name="delegate",
        description=(
            "Run several sub-agents in parallel, each on an independent piece of the job (e.g. research "
            "one company each, or check several sources). Each gets your tools minus delegate/schedule, "
            "works on its own, and returns a written report. Give each fully self-contained instructions - "
            "they can't see this conversation. Use it when the pieces are independent and each needs "
            "several steps; do quick lookups yourself."
        ),
        properties={
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "instructions": {"type": "string"}},
                    "required": ["title", "instructions"],
                },
            }
        },
        required=["tasks"],
        handler=delegate,
        top_level_only=True,
        summarize=lambda a: f"Delegated {len(a.get('tasks', []))} sub-tasks",
    ),
    Tool(
        name="ask_user",
        description=(
            "Pause and ask the user a question - for a real decision only they can make, missing info "
            "you can't find, or confirmation before something irreversible (purchases, sending, deleting). "
            "Don't ask about things you can reasonably decide yourself."
        ),
        properties={"question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"},
                                "description": "Optional quick-reply choices"}},
        required=["question"],
        handler=ask_user,
        top_level_only=True,
        summarize=lambda a: "Asked you a question",
    ),
]
