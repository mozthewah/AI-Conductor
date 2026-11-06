"""Tools for using and growing the skill library."""

from __future__ import annotations

from . import Tool, ToolContext, ToolError


async def use_skill(ctx: ToolContext, args: dict) -> str:
    skill = ctx.skills.mark_used(args["name"])
    if not skill:
        names = ", ".join(s.name for s in ctx.skills.list()) or "none"
        raise ToolError(f"No skill named '{args['name']}'. Available: {names}")
    await ctx.emit("skill", {"action": "used", "name": skill.name})
    return f"# Skill: {skill.name}\n{skill.description}\n\n{skill.body}"


async def save_skill(ctx: ToolContext, args: dict) -> str:
    skill = ctx.skills.save(args["name"], args["description"], args["instructions"])
    await ctx.emit("skill", {"action": "saved", "name": skill.name, "description": skill.description})
    return f"Saved skill '{skill.name}'. It will be offered on future tasks."


TOOLS = [
    Tool("use_skill",
         "Load one of your saved skills (listed in the system prompt) to follow its proven procedure.",
         {"name": {"type": "string"}}, ["name"], use_skill,
         summarize=lambda a: f"Using skill {a.get('name')}"),
    Tool("save_skill",
         "Write down a reusable procedure you just worked out, so next time you can do it faster and "
         "more reliably. Good skills are specific: the steps that worked, the sites/commands/files "
         "involved, gotchas you hit, and what a good result looks like. Saving with an existing name "
         "improves that skill.",
         {"name": {"type": "string", "description": "short-kebab-case name"},
          "description": {"type": "string", "description": "One line: when to use this skill"},
          "instructions": {"type": "string", "description": "Markdown steps, tips, and pitfalls"}},
         ["name", "description", "instructions"], save_skill,
         summarize=lambda a: f"Learned skill {a.get('name')}"),
]
