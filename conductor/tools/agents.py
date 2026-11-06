"""The conductor's tools: assign parts to specialist models, review their work,
and ask the user when a decision is really theirs."""

from __future__ import annotations

import asyncio

from ..router import DIFFICULTIES, SKILLS
from . import Tool, ToolContext, ToolError, clip

MAX_PARALLEL = 6
MAX_PARTS = 12


def _validate(parts: list) -> list[dict]:
    if not isinstance(parts, list) or not parts:
        raise ToolError("parts must be a non-empty list")
    if len(parts) > MAX_PARTS:
        raise ToolError(f"At most {MAX_PARTS} parts per assign call.")
    seen: dict[str, dict] = {}
    for raw in parts:
        if not isinstance(raw, dict) or not isinstance(raw.get("instructions"), str):
            raise ToolError("each part needs id, title, skill, domain, and instructions")
        pid = str(raw.get("id") or f"p{len(seen) + 1}")
        if pid in seen:
            raise ToolError(f"duplicate part id '{pid}'")
        seen[pid] = {
            "id": pid,
            "title": str(raw.get("title") or raw["instructions"][:60]),
            "skill": raw.get("skill") if raw.get("skill") in SKILLS else "other",
            "domain": str(raw.get("domain") or "general"),
            "difficulty": raw.get("difficulty") if raw.get("difficulty") in DIFFICULTIES else "medium",
            "priority": "supporting" if raw.get("priority") == "supporting" else "core",
            "instructions": raw["instructions"],
            "depends_on": [str(d) for d in raw.get("depends_on") or []],
        }
    for p in seen.values():
        for dep in p["depends_on"]:
            if dep not in seen:
                raise ToolError(f"part '{p['id']}' depends on unknown part '{dep}' (it must be in the same call)")
    # cycle check
    state: dict[str, int] = {}

    def visit(pid: str) -> None:
        if state.get(pid) == 1:
            raise ToolError(f"circular depends_on involving '{pid}'")
        if state.get(pid) == 2:
            return
        state[pid] = 1
        for d in seen[pid]["depends_on"]:
            visit(d)
        state[pid] = 2

    for pid in seen:
        visit(pid)
    return list(seen.values())


def _brief(part: dict, upstream: list[tuple[dict, dict]]) -> str:
    text = f"# Your part: {part['title']}\n\n{part['instructions']}"
    if upstream:
        text += "\n\n# Work you build on (from other specialists, accepted so far)\n"
        for dep, res in upstream:
            text += f"\n## {dep['title']}\n{clip(res['answer'], 12_000)}\n"
    return text


def _model_name(ctx: ToolContext, model_id: str | None) -> str:
    if not model_id:
        return "?"
    return ctx.conductor.catalog.get(model_id).name or model_id


async def assign(ctx: ToolContext, args: dict) -> str:
    parts = _validate(args["parts"])
    await ctx.emit("plan_parts", {"parts": [{k: p[k] for k in ("id", "title", "skill", "domain", "priority",
                                                                "depends_on")} for p in parts]})
    results: dict[str, dict] = {}
    pending = {p["id"]: p for p in parts}
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def go(p: dict) -> None:
        upstream = [(next(x for x in parts if x["id"] == d), results[d]) for d in p["depends_on"]]
        failed = [dep["title"] for dep, res in upstream if res["status"] != "done"]
        if failed:
            results[p["id"]] = {"task_id": None, "model": None, "status": "skipped",
                                "answer": f"Skipped: it depends on {', '.join(failed)}, which didn't finish."}
            return
        async with sem:
            results[p["id"]] = await ctx.conductor.run_part(ctx.task_id, p, _brief(p, upstream))

    while pending:
        ready = [p for p in pending.values() if all(d in results for d in p["depends_on"])]
        for p in ready:
            pending.pop(p["id"])
        await asyncio.gather(*(go(p) for p in ready))

    out = ["Results from your specialists (review each with `review` before relying on it):"]
    for p in parts:
        r = results[p["id"]]
        out.append(f"\n## [{p['id']}] {p['title']} - {p['skill']}/{p['domain']} - "
                   f"{_model_name(ctx, r['model'])} ({r['status']})\n{clip(r['answer'], 15_000)}")
    return clip("\n".join(out), 80_000)


REVIEW_SCORES = {0: 0.9, 1: 0.65, 2: 0.5}


async def review(ctx: ToolContext, args: dict) -> str:
    c = ctx.conductor
    runs = c.part_tasks(ctx.task_id, args["part"])
    if not runs:
        raise ToolError(f"No part '{args['part']}' has run yet under this task.")
    current = runs[-1]
    route = current.get("route") or {}
    verdict, notes = args["verdict"], (args.get("notes") or "").strip()
    revisions = int(route.get("revisions", 0))

    if verdict == "accept":
        score = REVIEW_SCORES.get(revisions, 0.4)
        c.store.set_review(current["id"], score)
        await ctx.emit("part", {"action": "accepted", "part_id": args["part"], "task_id": current["id"],
                                "model": current["model"], "revisions": revisions, "notes": clip(notes, 300)})
        return f"Accepted part '{args['part']}' ({_model_name(ctx, current['model'])})."

    if not notes:
        raise ToolError("Give specific notes so the specialist knows what to fix.")

    if verdict == "revise":
        if revisions >= 3:
            raise ToolError("This part has been revised 3 times; reassign it or fix the rest yourself.")
        c.store.update_task(current["id"], route={**route, "revisions": revisions + 1})
        res = await c.revise_part(ctx.task_id, current["id"], notes)
        return (f"Revised part '{args['part']}' ({_model_name(ctx, res['model'])}, {res['status']}). Review it again:\n\n"
                f"{clip(res['answer'], 15_000)}")

    # reassign: the current model fell short; hand the part to a different one
    c.store.set_review(current["id"], 0.1)
    tried = {r["model"] for r in runs if r.get("model")}
    part = {"id": args["part"], "title": current["title"], "skill": route.get("skill", "other"),
            "domain": route.get("domain", "general"), "difficulty": route.get("difficulty", "medium"),
            "priority": "core", "exclude": sorted(tried)}
    first_brief = next((e["data"]["text"] for e in c.store.load_events(runs[0]["id"]) if e["type"] == "user"), "")
    brief = (f"{first_brief}\n\n# Note from the conductor\nA previous attempt at this part fell short:\n{notes}\n"
             "Do it properly; you can read any files that attempt left in the workspace.")
    await ctx.emit("part", {"action": "reassigned", "part_id": args["part"], "task_id": current["id"],
                            "model": current["model"], "notes": clip(notes, 300)})
    res = await c.run_part(ctx.task_id, part, brief)
    return (f"Reassigned part '{args['part']}' to {_model_name(ctx, res['model'])} ({res['status']}). "
            f"Review it again:\n\n{clip(res['answer'], 15_000)}")


async def report_blocked(ctx: ToolContext, args: dict) -> str:
    ctx.conductor._blocked[ctx.task_id] = {"what": args["what"], "needs": args["needs"]}
    return "Reported to the composer. Stop here; you'll be resumed once it's resolved."


async def ask_user(ctx: ToolContext, args: dict) -> str:
    reply = await ctx.conductor.ask_user(ctx.task_id, args["question"], args.get("options") or [])
    if reply is None:
        return "The user didn't answer (unattended run). Make a sensible choice, say what you assumed, and continue."
    return f"User replied: {reply}"


TOOLS = [
    Tool(
        name="assign",
        description=(
            "Hand parts of the work to specialist models. For each part the router picks the model with the "
            "best track record (and benchmarks) for that skill and domain - e.g. one model for UI design, "
            "another for code, another for travel research. Parts without dependencies run in parallel; a "
            "part with depends_on starts after those parts finish and receives their output. Each part's "
            "instructions must stand alone (the specialist can't see this conversation) and should name the "
            "workspace files it should produce. Returns every specialist's report."
        ),
        properties={
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Short id, e.g. 'design'"},
                        "title": {"type": "string"},
                        "skill": {"type": "string", "enum": SKILLS},
                        "domain": {"type": "string", "description": "Subject area: travel, finance, health... or general"},
                        "difficulty": {"type": "string", "enum": DIFFICULTIES},
                        "priority": {"type": "string", "enum": ["core", "supporting"],
                                     "description": "core = central to the result (quality first); supporting = lookups/reading (cheaper)"},
                        "instructions": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "title", "skill", "domain", "instructions"],
                },
            }
        },
        required=["parts"],
        handler=assign,
        delegates=True,
        
        summarize=lambda a: f"Assigned {len(a.get('parts', []))} part(s) to specialists",
    ),
    Tool(
        name="review",
        description=(
            "Judge a specialist's part after checking it (read the files it wrote). verdict: accept (good "
            "enough to build on), revise (same model fixes it - give specific notes), or reassign (hand it "
            "to a different model - give notes on what was wrong). Your verdicts also teach the router "
            "which models are good at what."
        ),
        properties={
            "part": {"type": "string", "description": "The part id used in assign"},
            "verdict": {"type": "string", "enum": ["accept", "revise", "reassign"]},
            "notes": {"type": "string"},
        },
        required=["part", "verdict"],
        handler=review,
        delegates=True,
        
        summarize=lambda a: f"Reviewed {a.get('part')}: {a.get('verdict')}",
    ),
    Tool(
        name="report_blocked",
        description=(
            "Tell the composer you can't continue without something only it or the user can give: a "
            "decision, access, credentials, or missing information. Your run pauses and the composer "
            "resumes you with an answer. First do everything you can that isn't blocked."
        ),
        properties={"what": {"type": "string", "description": "What's blocked and why"},
                    "needs": {"type": "string", "description": "Exactly what would unblock you"}},
        required=["what", "needs"],
        handler=report_blocked,
        summarize=lambda a: f"Blocked: {a.get('what', '')[:60]}",
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
        
        summarize=lambda a: "Asked you a question",
    ),
]
