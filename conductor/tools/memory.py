"""Long-term memory: things worth knowing about the user across tasks."""

from __future__ import annotations

from . import Tool, ToolContext, ToolError

KINDS = ["preference", "person", "fact", "project", "note"]


async def remember(ctx: ToolContext, args: dict) -> str:
    mid = ctx.store.add_memory(args["kind"], args["content"], ctx.task_id)
    await ctx.emit("memory", {"action": "saved", "id": mid, "kind": args["kind"], "content": args["content"]})
    return f"Saved memory #{mid}."


async def recall(ctx: ToolContext, args: dict) -> str:
    rows = ctx.store.search_memories(args["query"], limit=int(args.get("limit", 10)))
    if not rows:
        return "Nothing relevant in memory."
    return "\n".join(f"#{r['id']} [{r['kind']}] {r['content']}" for r in rows)


async def update_memory(ctx: ToolContext, args: dict) -> str:
    mid = int(args["id"])
    if args.get("content"):
        if not ctx.store.update_memory(mid, args["content"]):
            raise ToolError(f"No memory #{mid}")
        await ctx.emit("memory", {"action": "updated", "id": mid, "content": args["content"]})
        return f"Updated memory #{mid}."
    if not ctx.store.delete_memory(mid):
        raise ToolError(f"No memory #{mid}")
    await ctx.emit("memory", {"action": "forgot", "id": mid})
    return f"Forgot memory #{mid}."


TOOLS = [
    Tool("remember",
         "Save something durable about the user or their world to long-term memory: preferences, people, "
         "recurring projects, accounts, facts they told you. Save it as soon as you learn it, in a "
         "self-contained sentence. Don't store secrets (passwords, keys) or one-off task details.",
         {"kind": {"type": "string", "enum": KINDS}, "content": {"type": "string"}},
         ["kind", "content"], remember, summarize=lambda a: f"Remembered: {a.get('content', '')[:60]}"),
    Tool("recall",
         "Search long-term memory (past preferences, facts, and summaries of earlier tasks).",
         {"query": {"type": "string"}, "limit": {"type": "integer"}},
         ["query"], recall, summarize=lambda a: f"Recalled “{a.get('query', '')}”"),
    Tool("update_memory",
         "Correct a memory (pass new content) or forget it (omit content) when it's wrong or outdated.",
         {"id": {"type": "integer"}, "content": {"type": "string"}},
         ["id"], update_memory, summarize=lambda a: f"Updated memory #{a.get('id')}"),
]
