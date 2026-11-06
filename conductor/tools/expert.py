"""Calling up: ask a stronger model for advice, or hand it the task."""

from __future__ import annotations

from ..llm import text_of
from . import Tool, ToolContext, ToolError, clip

EXPERT_SYSTEM = """\
You are a senior expert advising another AI assistant that is partway through a task for a user. \
It's asking you because the question is hard or it's unsure. Give a direct, correct, practical \
answer: the key facts, the approach you'd take, and pitfalls to avoid. Use web search if the \
answer depends on current information. If the question rests on a wrong assumption, say so. \
Be concise - it will act on your advice, not show it to the user verbatim."""


def expert_for(ctx: ToolContext) -> str:
    c = ctx.conductor
    if c.settings.expert_model and c.settings.expert_model != "auto":
        return c.settings.expert_model
    task = c.store.get_task(ctx.task_id)
    route = task.get("route") or {}
    pick = c.router.pick(skill=route.get("skill", "other"), domain=route.get("domain", "general"),
                         difficulty="hard", budget="best", exclude={task.get("model")} if task.get("model") else None)
    return pick.model


async def consult_expert(ctx: ToolContext, args: dict) -> str:
    c = ctx.conductor
    expert = c.catalog.get(expert_for(ctx))
    prompt = args["question"]
    if args.get("context"):
        prompt += f"\n\n<context>\n{clip(args['context'], 40_000)}\n</context>"
    await ctx.emit("consulted", {"action": "asking", "model": expert.id, "name": expert.name or expert.id,
                                 "question": clip(args["question"], 400)})

    async def quiet(kind: str, data: dict) -> None:
        return None

    resp = await c.llm.turn(model=expert, system=EXPERT_SYSTEM, messages=[{"role": "user", "content": prompt}],
                            tools=[], on_event=quiet, effort="high", web=c.settings.web_tools)
    usage = resp.get("usage") or {}
    c.store.add_usage(ctx.task_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("cost", 0.0))
    answer = text_of(resp["content"])
    if not answer:
        raise ToolError(f"{expert.name or expert.id} didn't return advice (stop reason: {resp.get('stop_reason')}).")
    await ctx.emit("consulted", {"action": "answered", "model": expert.id, "name": expert.name or expert.id,
                                 "answer": clip(answer, 1500), "cost": usage.get("cost", 0.0)})
    return f"Advice from {expert.name or expert.id}:\n\n{answer}"


async def escalate(ctx: ToolContext, args: dict) -> str:
    c = ctx.conductor
    task = c.store.get_task(ctx.task_id)
    if not c.stronger_model(task):
        return "You're already on the strongest available model for this work - keep going, and use consult_expert for a second opinion."
    c._escalate_next[ctx.task_id] = (f"The model asked for help: {args['reason']}", "model", None)
    return "A stronger model takes over from the next step. Leave a short note on what you've tried so far."


TOOLS = [
    Tool("consult_expert",
         "Ask a top-tier model for advice when you're unsure, stuck, or the stakes are high: a tricky "
         "technical decision, unfamiliar tool, conflicting information, or a plan you want checked. It "
         "can't see this conversation, so include the relevant context. Costs more than a normal step, "
         "so use it for questions that matter, not routine ones.",
         {"question": {"type": "string"},
          "context": {"type": "string", "description": "What it needs to know: goal, what you tried, key snippets"}},
         ["question"], consult_expert, summarize=lambda a: "Asked an expert model"),
    Tool("escalate",
         "Hand the rest of this task to a stronger model, when it's clearly beyond you: you keep "
         "going in circles, the work needs deeper expertise, or quality matters more than you can "
         "deliver. The stronger model sees the whole conversation so far.",
         {"reason": {"type": "string"}}, ["reason"], escalate, summarize=lambda a: "Asked for a stronger model"),
]
