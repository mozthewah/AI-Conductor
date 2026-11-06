"""The Conductor: runs tasks as an agent loop and broadcasts everything it does.

Flow for one task:
  build a frozen system prompt (memories + skill index)  ->
  loop { stream a model turn -> run requested tools in parallel -> append results } ->
  final answer -> reflect and learn (memories, episode summary, skills).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import prompts
from .config import Settings
from .llm import INTERRUPTED_SENTINEL, LLM, MessagesLLM, describe_api_error, text_of
from .benchmarks import Benchmarks
from .models import Catalog
from .router import Router
from .safety import JUDGE_PROMPT, JUDGE_SCHEMA, JUDGE_SYSTEM, LEVELS, Assessment, Policy
from .skills import SkillLibrary
from .store import PROFILE_KINDS, Store, new_id
from .tools import Tool, ToolContext, ToolError, build_registry, clip

log = logging.getLogger("conductor")

# Events that stream token-by-token are broadcast live but not persisted;
# the finished block is persisted as its own event instead.
EPHEMERAL = {"text_delta", "progress_delta", "tool_start", "text_start"}


class Conductor:
    def __init__(self, settings: Settings, llm: LLM | None = None, store: Store | None = None):
        settings.ensure_dirs()
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.skills = SkillLibrary(settings.skills_dir)
        self.catalog = Catalog(settings.data_dir, has_openrouter=bool(settings.openrouter_key),
                               has_anthropic=settings.anthropic_key, fetch=settings.fetch_catalog).load()
        self.llm: LLM = llm or MessagesLLM(anthropic_enabled=settings.anthropic_key,
                                           openrouter_key=settings.openrouter_key, max_tokens=settings.max_tokens)
        self.benchmarks = Benchmarks(settings.data_dir, key=settings.openrouter_key,
                                     fetch=settings.fetch_catalog).load()
        self.router = Router(self)
        self.policy = Policy(settings.data_dir, settings.approval, settings.unattended_approval)
        self._judged: dict[str, Assessment] = {}
        self.tools: dict[str, Tool] = {t.name: t for t in build_registry(settings)}
        self.browser_pool = None
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._pending: dict[str, tuple[str, asyncio.Future, dict]] = {}

    # ------------------------------------------------------------------ events
    async def emit(self, task_id: str, type_: str, data: dict) -> None:
        if type_ in EPHEMERAL:
            ev = {"task_id": task_id, "seq": None, "type": type_, "data": data}
        else:
            ev = self.store.append_event(task_id, type_, data)
        for q in list(self._subscribers.get(task_id, ())):
            q.put_nowait(ev)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        self._subscribers.get(task_id, set()).discard(q)

    # ------------------------------------------------------------------ prompts
    def _profile(self) -> list[dict]:
        rows = [m for m in self.store.list_memories(limit=200) if m["kind"] in PROFILE_KINDS]
        return rows[:40]

    def build_system_prompt(self, prompt: str, depth: int = 0, role: str = "") -> str:
        profile = prompts.bullet(self._profile(), "(nothing yet)")
        if depth > 0:
            return prompts.SUBAGENT.format(today=prompts.today(), profile=profile, role=role or "a focused helper",
                                           skills=self.skills.index(), workspace=self.settings.workspace)
        related = self.store.search_memories(prompt, limit=8, exclude_kinds=PROFILE_KINDS)
        shell_note = (f"and browser actions are rated by risk. Up to '{self.policy.threshold}' runs "
                      "immediately; riskier ones wait for the user's OK, so batch those together and prefer "
                      "safer equivalents when they exist.")
        return prompts.CONDUCTOR.format(
            today=prompts.today(), profile=profile,
            related=prompts.bullet(related, "(none)"), skills=self.skills.index(),
            workspace=self.settings.workspace, shell_note=shell_note,
        )

    def tool_list(self, depth: int) -> list[Tool]:
        return [t for t in self.tools.values() if depth == 0 or not t.top_level_only]

    def tool_specs(self, depth: int) -> list[dict]:
        return [t.spec() for t in self.tool_list(depth)]

    # ------------------------------------------------------------------ public API
    def start_task(self, prompt: str, origin: str = "user", model: str | None = None,
                   compare_of: str | None = None) -> str:
        """model: None/"auto" = let the router choose; otherwise a model id to pin."""
        title = prompt.strip().splitlines()[0][:90] if prompt.strip() else "Untitled task"
        task_id = self.store.create_task(title, origin=origin, system_prompt=self.build_system_prompt(prompt),
                                         compare_of=compare_of)
        self._launch(task_id, prompt, model or self.settings.model, "compare" if compare_of else None)
        return task_id

    def compare(self, task_id: str, model: str) -> str:
        """Re-run a task's original request on another model, side by side."""
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        first = next((e["data"]["text"] for e in self.store.load_events(task_id) if e["type"] == "user"), None)
        if first is None:
            raise ValueError("That task has no request to re-run.")
        root = task.get("compare_of") or task_id
        return self.start_task(first, origin=task["origin"] if task["origin"] != "subagent" else "user",
                               model=model, compare_of=root)

    def feedback(self, task_id: str, value: int | None) -> bool:
        ok = self.store.set_feedback(task_id, value)
        if ok:
            self.store.append_event(task_id, "feedback", {"value": value})
        return ok

    def send_message(self, task_id: str, prompt: str) -> None:
        if task_id in self._running:
            raise RuntimeError("That task is still running - wait for it or cancel it first.")
        if not self.store.get_task(task_id):
            raise KeyError(task_id)
        self._launch(task_id, prompt)

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    def cancel(self, task_id: str) -> bool:
        t = self._running.get(task_id)
        if t:
            t.cancel()
            return True
        return False

    async def wait(self, task_id: str) -> None:
        t = self._running.get(task_id)
        if t:
            await asyncio.gather(t, return_exceptions=True)

    def _launch(self, task_id: str, prompt: str, model: str | None = None, by: str | None = None) -> None:
        self.store.update_task(task_id, status="running")
        t = asyncio.create_task(self._run_top(task_id, prompt, model, by))
        self._running[task_id] = t
        t.add_done_callback(lambda _: self._running.pop(task_id, None))

    async def _route(self, task_id: str, prompt: str, *, pinned: str | None = None, budget: str = "balanced",
                     by: str | None = None, part: dict | None = None) -> None:
        """Pick this task's model once; it sticks for the life of the task."""
        task = self.store.get_task(task_id)
        if task.get("model"):
            return
        try:
            if part is not None:
                decision = self.router.route_part(
                    skill=part.get("skill", "other"), domain=part.get("domain", "general"),
                    difficulty=part.get("difficulty", "medium"), priority=part.get("priority", "core"),
                    exclude=set(part.get("exclude") or []), pinned=self.settings.subagent_model)
            else:
                await self.emit(task_id, "status", {"text": "Choosing a model..."})
                decision = await self.router.route(prompt, pinned=pinned, budget=budget)
        except Exception as e:
            log.warning("routing failed: %s", e)
            from .router import Decision
            decision = Decision(self.settings.fallback_model, "other", "general", "medium",
                                "Router unavailable.", "fallback")
        if by:
            decision.by = by
            decision.reason = "Side-by-side comparison run."
        info = self.catalog.get(decision.model)
        route = {**decision.to_dict(), "name": info.name or info.id, **({"part_id": part["id"]} if part else {})}
        fields: dict[str, Any] = {"model": decision.model, "route": route}
        if decision.mode == "conduct" and task.get("parent_id") is None:
            # Settled before the first model turn, so the system prompt is still unsent.
            fields["system_prompt"] = task["system_prompt"] + prompts.CONDUCT_MODE
        self.store.update_task(task_id, **fields)
        self.store.start_run(task_id, decision.model, decision.skill, decision.difficulty, decision.by,
                             domain=decision.domain, mode=decision.mode, parent_id=task.get("parent_id"))
        if part is None:
            await self.emit(task_id, "routed", route)

    async def _run_top(self, task_id: str, prompt: str, model: str | None = None, by: str | None = None) -> None:
        started = time.time()
        cost_before = self.store.get_task(task_id)["cost_usd"]
        try:
            answer = await self.run(task_id, prompt, depth=0,
                                    route={"pinned": model, "budget": self.settings.router_budget, "by": by})
        finally:
            t = self.store.get_task(task_id)
            if t:
                self.store.finish_run(task_id, t["status"], t["cost_usd"] - cost_before, time.time() - started)
        task = self.store.get_task(task_id)
        if task and task["status"] == "done" and self.settings.learning:
            try:
                await self.reflect(task_id)
            except Exception as e:  # learning is best-effort
                log.warning("reflection failed: %s", e)
                await self.emit(task_id, "status", {"text": f"Skipped learning step ({describe_api_error(e)})"})
        return answer

    # ------------------------------------------------------------------ the loop
    async def run(self, task_id: str, prompt: str, depth: int = 0, route: dict | None = None,
                  effort: str | None = None) -> str:
        effort = effort or (self.settings.effort if depth == 0 else self.settings.subagent_effort)
        messages = self.store.load_messages(task_id)

        # If a previous run was cancelled mid-tool, answer the dangling tool calls first
        # (the transcript is append-only - we never rewrite earlier turns).
        content: list[dict] = [
            {"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": "Cancelled by the user."}
            for tid in self._dangling_tool_ids(messages)
        ]
        content.append({"type": "text", "text": prompt})
        self._append(task_id, messages, "user", content)
        self.store.update_task(task_id, status="running")
        await self.emit(task_id, "user", {"text": prompt})
        if route is not None:
            await self._route(task_id, prompt, **route)
        task = self.store.get_task(task_id)
        system = task["system_prompt"]
        model = self.catalog.get(task.get("model") or self.settings.fallback_model)

        ctx = ToolContext(conductor=self, task_id=task_id, depth=depth)
        specs = self.tool_specs(depth)
        answer = ""
        try:
            for _ in range(self.settings.max_steps):
                turn = len(messages)

                async def on_event(kind: str, data: dict, _turn=turn) -> None:
                    await self.emit(task_id, kind, {**data, "turn": _turn})

                resp = await self.llm.turn(model=model, system=system, messages=messages, tools=specs,
                                           on_event=on_event, effort=effort, web=self.settings.web_tools,
                                           session_id=task_id)
                blocks = resp["content"]
                self._append(task_id, messages, "assistant", blocks)
                usage = resp.get("usage") or {}
                self.store.add_usage(task_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                                     usage.get("cost", 0.0))
                await self._emit_blocks(task_id, turn, blocks)

                stop = resp.get("stop_reason")
                if stop == "pause_turn":
                    continue
                if stop == "refusal":
                    answer = text_of(blocks) or "I can't help with that request."
                    await self.emit(task_id, "status", {"text": "The model declined this request."})
                    break

                tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
                if not tool_uses:
                    answer = text_of(blocks)
                    if stop == "max_tokens":
                        answer += "\n\n_(Reply was cut off at the output limit - say \"continue\" to resume.)_"
                    elif stop == "model_context_window_exceeded":
                        answer += "\n\n_(This conversation is full - start a new task to keep going.)_"
                    break

                truncated = stop == "max_tokens"
                results = await asyncio.gather(*[self._run_tool(ctx, b, truncated) for b in tool_uses])
                self._append(task_id, messages, "user", list(results))
            else:
                answer = (text_of(messages[-1]["content"]) if messages[-1]["role"] == "assistant" else "")
                answer += f"\n\n_(Stopped after {self.settings.max_steps} steps.)_"

            self.store.update_task(task_id, status="done", answer=answer)
            await self.emit(task_id, "done", {"answer": answer})
            return answer
        except asyncio.CancelledError:
            self.store.update_task(task_id, status="cancelled")
            await self.emit(task_id, "cancelled", {})
            raise
        except Exception as e:
            log.exception("task %s failed", task_id)
            msg = describe_api_error(e)
            self.store.update_task(task_id, status="error", answer=msg)
            await self.emit(task_id, "error", {"text": msg})
            return f"Error: {msg}"

    def _append(self, task_id: str, messages: list[dict], role: str, content: Any) -> None:
        messages.append({"role": role, "content": content})
        self.store.append_message(task_id, role, content)

    @staticmethod
    def _dangling_tool_ids(messages: list[dict]) -> list[str]:
        if not messages or messages[-1]["role"] != "assistant":
            return []
        content = messages[-1]["content"]
        return [b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]

    async def _emit_blocks(self, task_id: str, turn: int, blocks: list[dict]) -> None:
        for i, b in enumerate(blocks):
            t = b.get("type")
            if t == "thinking" and b.get("thinking") and b["thinking"].strip() != INTERRUPTED_SENTINEL:
                await self.emit(task_id, "progress", {"turn": turn, "index": i, "text": b["thinking"]})
            elif t == "text" and b.get("text"):
                await self.emit(task_id, "assistant_text", {"turn": turn, "index": i, "text": b["text"]})

    async def _run_tool(self, ctx: ToolContext, block: dict, truncated: bool) -> dict:
        tid, name, args = block["id"], block["name"], block.get("input")
        tool = self.tools.get(name)

        def result(content: Any, error: bool = False) -> dict:
            r = {"type": "tool_result", "tool_use_id": tid, "content": content}
            if error:
                r["is_error"] = True
            return r

        if truncated:
            return result("Your tool call was cut off at the output limit. Retry with smaller input "
                          "(e.g. write a large file in parts with append).", True)
        if tool is None or (ctx.depth > 0 and tool.top_level_only):
            return result(f"Unknown tool '{name}'.", True)
        problem = tool.validate(args)
        if problem:
            return result(json.dumps({"INVALID_INPUT": problem}), True)

        await self.emit(ctx.task_id, "tool_call", {
            "id": tid, "name": name, "summary": tool.describe(args), "risky": tool.risky,
            "input": {k: (clip(v, 2000) if isinstance(v, str) else v) for k, v in args.items()},
        })
        try:
            out = await tool.handler(ctx, args)
            ok = True
        except ToolError as e:
            out, ok = str(e), False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("tool %s crashed", name)
            out, ok = f"{type(e).__name__}: {e}", False

        preview = out if isinstance(out, str) else next(
            (b.get("text", "") for b in out if b.get("type") == "text"), "")
        await self.emit(ctx.task_id, "tool_result", {"id": tid, "name": name, "ok": ok,
                                                      "preview": clip(preview, 1500)})
        return result(out, not ok)

    # ------------------------------------------------------------------ specialists
    def part_tasks(self, parent_id: str, part_id: str | None = None) -> list[dict]:
        """Specialist runs under a conducted task (latest last), optionally for one part id."""
        out = []
        for child in self.store.child_tasks(parent_id):
            t = self.store.get_task(child["id"])
            pid = (t.get("route") or {}).get("part_id")
            if pid and (part_id is None or pid == part_id):
                out.append(t)
        return out

    async def run_part(self, parent_id: str, part: dict, brief: str) -> dict:
        """Hire the best model for one part, run it, and report back to the conductor."""
        skill, domain = part.get("skill", "other"), part.get("domain", "general")
        role = (f"the {skill.replace('_', ' ')} specialist"
                f"{'' if domain in ('', 'general') else ' (' + domain + ')'}")
        child = self.store.create_task(part["title"], parent_id=parent_id, origin="subagent",
                                       system_prompt=self.build_system_prompt(brief, depth=1, role=role))
        started = time.time()
        effort = self.settings.effort if part.get("priority", "core") == "core" else self.settings.subagent_effort
        try:
            await self._route(child, brief, part=part)
            t = self.store.get_task(child)
            await self.emit(parent_id, "part", {"action": "started", "part_id": part["id"], "task_id": child,
                                                "title": part["title"], "skill": skill, "domain": domain,
                                                "model": t["model"], "name": t["route"].get("name"),
                                                "reason": t["route"].get("reason")})
            answer = await self.run(child, brief, depth=1, effort=effort)
        finally:
            t = self.store.get_task(child)
            self.store.finish_run(child, t["status"], t["cost_usd"], time.time() - started)
        await self.emit(parent_id, "part", {"action": "finished", "part_id": part["id"], "task_id": child,
                                            "title": part["title"], "model": t.get("model"), "status": t["status"],
                                            "preview": clip(answer, 400)})
        return {"task_id": child, "model": t.get("model"), "status": t["status"], "answer": answer}

    async def revise_part(self, parent_id: str, task_id: str, notes: str) -> dict:
        """Send a specialist's work back to the same model, in the same conversation."""
        started = time.time()
        cost_before = self.store.get_task(task_id)["cost_usd"]
        t = self.store.get_task(task_id)
        await self.emit(parent_id, "part", {"action": "revising", "part_id": t["route"].get("part_id"),
                                            "task_id": task_id, "model": t["model"], "notes": clip(notes, 400)})
        answer = await self.run(task_id, f"The conductor reviewed your work and wants changes:\n\n{notes}",
                                depth=1, effort=self.settings.effort)
        t = self.store.get_task(task_id)
        self.store.finish_run(task_id, t["status"], t["cost_usd"] - cost_before, time.time() - started)
        await self.emit(parent_id, "part", {"action": "finished", "part_id": t["route"].get("part_id"),
                                            "task_id": task_id, "model": t["model"], "status": t["status"],
                                            "preview": clip(answer, 400)})
        return {"task_id": task_id, "model": t["model"], "status": t["status"], "answer": answer}

    # ------------------------------------------------------------------ human in the loop
    def _root(self, task_id: str) -> dict:
        task = self.store.get_task(task_id)
        while task and task.get("parent_id"):
            task = self.store.get_task(task["parent_id"])
        return task

    def interactive(self, task_id: str) -> bool:
        root = self._root(task_id)
        return bool(root) and root["origin"] == "user"

    async def _wait_for_user(self, task_id: str, kind: str, payload: dict) -> dict | None:
        if not self.interactive(task_id):
            return None
        root_id = self._root(task_id)["id"]
        rid = new_id("r_")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = (root_id, fut, {"kind": kind, **payload})
        self.store.update_task(root_id, status="waiting")
        await self.emit(root_id, "request", {"id": rid, "kind": kind, "from_task": task_id, **payload})
        try:
            res = await fut
        finally:
            self._pending.pop(rid, None)
            self.store.update_task(root_id, status="running")
        await self.emit(root_id, "request_resolved", {"id": rid, **res})
        return res

    def pending_requests(self, task_id: str | None = None) -> list[dict]:
        return [{"id": rid, "task_id": tid, **p} for rid, (tid, _, p) in self._pending.items()
                if task_id is None or tid == task_id]

    def resolve_request(self, request_id: str, approved: bool = True, text: str = "") -> bool:
        entry = self._pending.get(request_id)
        if not entry or entry[1].done():
            return False
        entry[1].set_result({"approved": approved, "text": text})
        return True

    async def judge(self, command: str) -> Assessment | None:
        """Ask the cheap router model to rate a command the rules didn't recognize."""
        if command in self._judged:
            return self._judged[command]
        try:
            out = await self.llm.structured(
                model=self.catalog.get(self.settings.default_router_model), system=JUDGE_SYSTEM,
                prompt=JUDGE_PROMPT.format(workspace=self.settings.workspace, command=command[:4000]),
                schema=JUDGE_SCHEMA, effort="low")
        except Exception as e:
            log.warning("safety judge failed: %s", e)
            return None
        if out.get("level") not in LEVELS:
            return None
        a = Assessment(out["level"], out.get("reason") or "Rated by the safety model.")
        self._judged[command] = a
        return a

    async def request_approval(self, task_id: str, tool: str, summary: str, args: dict,
                               assessment: Assessment | None = None) -> bool:
        a = assessment or Assessment("medium", "Unrated action.", sure=False)
        if not a.sure and tool == "run_shell":
            judged = await self.judge(args.get("command", ""))
            if judged:  # the rules' floor still applies if they saw something worse
                from .safety import rank
                a = judged if rank(judged.level) >= rank(a.level) or a.level == "medium" else a
                a = Assessment(a.level, a.reason + " (rated by the safety model)")
        interactive = self.interactive(task_id)
        decision, why = self.policy.decide(tool, args, a.level, interactive)
        target = self._root(task_id)["id"]
        if decision != "ask":
            await self.emit(target, "approval", {"tool": tool, "summary": summary, "level": a.level,
                                                 "reason": a.reason, "allowed": decision == "allow", "why": why,
                                                 "from_task": task_id})
            return decision == "allow"
        from .safety import rule_key
        res = await self._wait_for_user(task_id, "approval", {
            "tool": tool, "summary": summary, "level": a.level, "reason": a.reason, "why": why,
            "remember_key": rule_key(tool, args) if a.level != "critical" else ""})
        if res and res["approved"] and res.get("text") == "always":
            self.policy.remember(tool, args, a.level)
        return bool(res and res["approved"])

    async def ask_user(self, task_id: str, question: str, options: list[str]) -> str | None:
        res = await self._wait_for_user(task_id, "question", {"question": question, "options": options})
        if not res or not res["approved"]:
            return None
        return res["text"] or "(no answer)"

    # ------------------------------------------------------------------ learning
    def _trace(self, task_id: str) -> str:
        """A compact transcript for reflection: requests, actions, outcomes."""
        lines: list[str] = []
        for ev in self.store.load_events(task_id):
            d, t = ev["data"], ev["type"]
            if t == "user":
                lines.append(f"USER: {d['text']}")
            elif t == "progress":
                lines.append(f"  (note) {d['text']}")
            elif t == "tool_call":
                lines.append(f"  -> {d['name']}: {clip(json.dumps(d['input']), 300)}")
            elif t == "tool_result" and not d["ok"]:
                lines.append(f"     ! error: {clip(d['preview'], 200)}")
            elif t == "skill":
                lines.append(f"  [skill {d['action']}: {d['name']}]")
            elif t == "memory":
                lines.append(f"  [memory {d['action']}: {d.get('content', '')}]")
            elif t == "done":
                lines.append(f"ASSISTANT FINAL: {clip(d['answer'], 3000)}")
        return clip("\n".join(lines), 40_000)

    async def reflect(self, task_id: str) -> dict:
        await self.emit(task_id, "status", {"text": "Reflecting on what to remember..."})
        existing = self.store.list_memories(limit=100)
        task = self.store.get_task(task_id)
        out = await self.llm.structured(
            model=self.catalog.get(task.get("model") or self.settings.fallback_model),
            system=prompts.REFLECT_SYSTEM,
            prompt=prompts.REFLECT_PROMPT.format(
                trace=self._trace(task_id),
                memories="\n".join(f"- [{m['kind']}] {m['content']}" for m in existing if m["kind"] != "episode")
                or "(none)",
                skills=self.skills.index()),
            schema=prompts.REFLECT_SCHEMA,
            effort=self.settings.reflection_effort,
        )
        learned: dict[str, Any] = {"memories": [], "skill": None}
        if out.get("summary"):
            self.store.add_memory("episode", out["summary"], task_id)
        for m in out.get("memories", [])[:5]:
            mid = self.store.add_memory(m["kind"], m["content"], task_id)
            learned["memories"].append({"id": mid, **m})
        if out.get("save_skill") and out.get("skill_name") and out.get("skill_instructions"):
            skill = self.skills.save(out["skill_name"], out.get("skill_description", ""), out["skill_instructions"])
            learned["skill"] = {"name": skill.name, "description": skill.description}
        await self.emit(task_id, "learned", learned)
        return learned

    async def shutdown(self) -> None:
        for t in list(self._running.values()):
            t.cancel()
        if self.browser_pool:
            await self.browser_pool.shutdown()
