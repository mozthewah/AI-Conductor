"""Tool framework: every capability the conductor has is a Tool in a registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..llm import tool_spec

if TYPE_CHECKING:
    from ..agent import Conductor

ToolResult = str | list[dict]
Handler = Callable[["ToolContext", dict], Awaitable[ToolResult]]

MAX_RESULT_CHARS = 30_000

_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict,
}


class ToolError(Exception):
    """Raise from a handler to send an is_error tool_result back to the model."""


@dataclass
class ToolContext:
    conductor: "Conductor"
    task_id: str
    depth: int = 0  # 0 = top-level task, 1 = sub-agent

    @property
    def settings(self):
        return self.conductor.settings

    @property
    def store(self):
        return self.conductor.store

    @property
    def skills(self):
        return self.conductor.skills

    async def emit(self, type_: str, data: dict) -> None:
        await self.conductor.emit(self.task_id, type_, data)

    async def approve(self, tool: str, summary: str, args: dict) -> bool:
        return await self.conductor.request_approval(self.task_id, tool, summary, args)


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: list[str]
    handler: Handler
    risky: bool = False          # needs approval when CONDUCTOR_APPROVAL=ask
    top_level_only: bool = False  # hidden from sub-agents (delegate, schedule...)
    summarize: Callable[[dict], str] | None = None
    extra: dict = field(default_factory=dict)

    def spec(self) -> dict:
        return tool_spec(self.name, self.description, self.properties, self.required)

    def validate(self, args: Any) -> str | None:
        """Eager input streaming means the server no longer validates inputs - we do."""
        if not isinstance(args, dict):
            return "tool input must be a JSON object"
        for key in self.required:
            if key not in args:
                return f"missing required field '{key}'"
        for key, val in args.items():
            prop = self.properties.get(key)
            if prop is None:
                continue
            expected = _JSON_TYPES.get(prop.get("type", ""))
            if expected and not isinstance(val, expected):
                return f"field '{key}' should be {prop['type']}"
            if expected is int and isinstance(val, bool):
                return f"field '{key}' should be integer"
            if "enum" in prop and val not in prop["enum"]:
                return f"field '{key}' must be one of {prop['enum']}"
        return None

    def describe(self, args: dict) -> str:
        if self.summarize:
            try:
                return self.summarize(args)
            except Exception:
                pass
        return self.name


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} characters omitted] ...\n\n{text[-half:]}"


def build_registry(settings) -> list[Tool]:
    from . import agents, browser, memory, planning, schedule, skills, workspace

    tools: list[Tool] = []
    tools += planning.TOOLS
    tools += workspace.TOOLS
    if settings.browser and browser.available():
        tools += browser.TOOLS
    tools += memory.TOOLS
    tools += skills.TOOLS
    tools += agents.TOOLS
    tools += schedule.TOOLS
    return tools
