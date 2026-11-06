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


def _pick(ctx: ToolContext, requested: str | None) -> str:
    """A model the caller named (validated), else the next step up, else the best other model."""
    c = ctx.conductor
    task = c.store.get_task(ctx.task_id)
    if requested:
        ids = {m.id for m in c.catalog.roster()}
        if requested not in ids:
            raise ToolError(f"'{requested}' isn't in the roster. Use find_model to see who's available.")
        if requested == task.get("model"):
            raise ToolError("That's the model you're running on. Pick a different one from find_model.")
        return requested
    if c.settings.expert_model and c.settings.expert_model != "auto":
        return c.settings.expert_model
    step = c.stronger_model(task)
    if step:
        return step
    route = task.get("route") or {}
    return c.router.pick(skill=route.get("skill", "other"), domain=route.get("domain", "general"),
                         difficulty="hard", budget="best",
                         exclude={task.get("model")} if task.get("model") else None).model


def expert_for(ctx: ToolContext) -> str:
    return _pick(ctx, None)


async def find_model(ctx: ToolContext, args: dict) -> str:
    from ..router import heuristic_label
    c = ctx.conductor
    task = c.store.get_task(ctx.task_id)
    skill, domain = args.get("skill"), args.get("domain")
    if not skill and args.get("need"):
        guess = heuristic_label(args["need"])
        skill, domain = guess["skill"], domain or guess["domain"]
    opts = c.model_options(task, skill, domain)
    me = c.catalog.get(task.get("model") or "")
    if not opts:
        return (f"No model in the roster looks clearly better than you ({me.name or me.id}) at this. Keep going, "
                "and use consult_expert for a second opinion if you need one.")
    label = f"{(skill or (task.get('route') or {}).get('skill', 'this work')).replace('_', ' ')}"
    lines = [f"Models likely to handle {label} better than you ({me.name or me.id}), smallest step up first:"]
    for o in opts[:6]:
        basis = (f"{o['runs']} past task(s) for this user" if o["runs"] else
                 "public benchmarks" if o["benchmark"] is not None else "its tier")
        step = "same tier" if o["steps_up"] == 0 else f"{o['steps_up']} tier(s) up"
        lines.append(f"- {o['model']} ({o['name']}): {o['tier']}, {step}, est. quality {o['quality']:.0%} "
                     f"(+{o['gain']:.0%}) from {basis}; {o['price']}, ~${o['est_cost']:.3f} for a hard task")
    lines.append("Prefer the smallest step that's likely enough. Then call consult_expert(model=...) for advice, "
                 "or escalate(model=...) to hand the task over.")
    return "\n".join(lines)


async def consult_expert(ctx: ToolContext, args: dict) -> str:
    c = ctx.conductor
    expert = c.catalog.get(_pick(ctx, args.get("model")))
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
    target = _pick(ctx, args["model"]) if args.get("model") else c.stronger_model(task)
    if not target:
        return "You're already on the strongest available model for this work - keep going, and use consult_expert for a second opinion."
    c._escalate_next[ctx.task_id] = (f"The model asked for help: {args['reason']}", "model", target)
    name = c.catalog.get(target).name or target
    return f"{name} takes over from the next step. Leave a short note on what you've tried so far."


TOOLS = [
    Tool("find_model",
         "Find models that would likely handle something better than you, before asking or handing off. "
         "Describe what you're struggling with (e.g. 'Go concurrency bug', 'UI design critique', 'tax rules "
         "for freelancers'); you get the roster's better options with their track record, price, and how "
         "big a step up each is, smallest step first. Free - it doesn't call any model.",
         {"need": {"type": "string", "description": "What you need help with"},
          "skill": {"type": "string", "description": "Optional: coding, ui_design, architecture, research, writing, analysis, planning, browsing"},
          "domain": {"type": "string", "description": "Optional: travel, finance, health... or general"}},
         ["need"], find_model, summarize=lambda a: f"Looked for a better model for {a.get('need', '')[:50]}"),
    Tool("consult_expert",
         "Ask a stronger model (your 'big brother') one question and get its advice, then carry on "
         "yourself. Use it instead of guessing: when you're unsure, catch yourself hedging (hm, actually, "
         "wait), keep reconsidering the same point, need facts that may be newer than you know, or hit a "
         "tool, API, or domain you don't know well. It can't see this conversation, so include the goal, "
         "what you've tried, and the relevant snippets. Asking is cheap compared to a wrong answer.",
         {"question": {"type": "string"},
          "context": {"type": "string", "description": "What it needs to know: goal, what you tried, key snippets"},
          "model": {"type": "string", "description": "Optional model id from find_model; default is the next step up"}},
         ["question"], consult_expert, summarize=lambda a: "Asked a stronger model"),
    Tool("escalate",
         "Hand the rest of this task to a stronger model ('pass it to my big brother'). Use it when the "
         "work is clearly beyond you: it needs deep expertise or careful multi-step reasoning, you've "
         "tried twice and it isn't working, or the stakes are high. The stronger model sees everything "
         "so far; say in `reason` what's hard and what you've tried.",
         {"reason": {"type": "string"},
          "model": {"type": "string", "description": "Optional model id from find_model; default is the next step up"}},
         ["reason"], escalate, summarize=lambda a: "Passed it to a stronger model"),
]
