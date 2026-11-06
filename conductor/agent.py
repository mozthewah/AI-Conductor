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
import math
import re
import secrets
import time
from pathlib import Path
from typing import Any

from . import prompts
from .config import Settings
from .llm import CURRENT_TASK, INTERRUPTED_SENTINEL, LLM, MessagesLLM, describe_api_error, text_of
from .audit import EVENT_KINDS, AuditLog
from .benchmarks import Benchmarks
from .account import Account
from .claude_cli import ClaudeCLI, model_alias
from .compression import Compressor, HeadroomProxy, proxy_url
from .copilot_cli import CopilotCLI, install_hook as install_copilot_hook
from .copilot_cli import model_name as copilot_model_name, serves as copilot_serves
from .credits import CreditError, CreditMonitor
from .models import Catalog
from .router import SKILLS, Router

SKILL_SET = set(SKILLS) | {"orchestration"}
# What a hands-off composer can't do itself: the actual work goes to specialists.
HANDS_ON_TOOLS = {"run_shell", "write_file", "edit_file", "browser", "processes"}
HEDGES = re.compile(r"\b(hm+|hmm+|actually|wait|on second thought|let me reconsider|i'?m not (?:sure|certain)|"
                    r"not sure (?:if|whether)|i (?:might|may) be wrong|or maybe|hold on|i don'?t (?:know|recall))\b", re.I)
from .safety import JUDGE_PROMPT, JUDGE_SCHEMA, JUDGE_SYSTEM, LEVELS, Assessment, Policy
from .skills import SkillLibrary
from .store import PROFILE_KINDS, Store, new_id
from .tools import Tool, ToolContext, ToolError, build_registry, clip

log = logging.getLogger("conductor")

READ_TOOLS = {"view_image", "read_file", "list_files"}
CHANGING_TOOLS = {"write_file", "edit_file", "run_shell", "processes", "browser"}

# Events that stream token-by-token are broadcast live but not persisted;
# the finished block is persisted as its own event instead.
EPHEMERAL = {"text_delta", "progress_delta", "tool_start", "text_start"}


class Conductor:
    def __init__(self, settings: Settings, llm: LLM | None = None, store: Store | None = None):
        settings.ensure_dirs()
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.audit = AuditLog(settings.data_dir / "logs", enabled=settings.audit)
        self.skills = SkillLibrary(settings.skills_dir)
        self.catalog = Catalog(settings.data_dir, has_openrouter=bool(settings.openrouter_key),
                               has_anthropic=settings.anthropic_key, fetch=settings.fetch_catalog).load()
        self.llm: LLM = llm or MessagesLLM(anthropic_enabled=settings.anthropic_key,
                                           openrouter_key=settings.openrouter_key, max_tokens=settings.max_tokens)
        self.benchmarks = Benchmarks(settings.data_dir, key=settings.openrouter_key,
                                     fetch=settings.fetch_catalog).load()
        self.benchmarks.remap(self.catalog.models)
        if hasattr(self.llm, "audit"):
            self.llm.audit = self.audit
        if hasattr(self.llm, "credit_low"):
            self.llm.credit_low = lambda: self.credits.low
        self.router = Router(self)
        self.credits = CreditMonitor(key=settings.openrouter_key, management_key=settings.openrouter_management_key,
                                     low=settings.credit_low, fetch=settings.fetch_catalog)
        self.account = Account(settings.data_dir, key=settings.openrouter_key,
                               management_key=settings.openrouter_management_key, fetch=settings.fetch_catalog,
                               mode=settings.byok, key_name=settings.key_name)
        self.account.note_catalog(self.catalog)
        self._account_refresh: asyncio.Task | None = None
        self._byok_note_pending: set[str] = set()
        self.cli = ClaudeCLI(settings.claude_cli_path, settings.claude_cli)
        self.cli_runs: dict[str, str] = {}   # hook token -> task id, while Claude Code runs a task
        self._cli_limited_until = 0.0         # subscription usage limit reached: don't retry until then
        self.server_url: str | None = None    # set by the web server; Claude Code's safety hook calls it
        self.compressor = Compressor(settings.headroom, settings.workspace)
        self._hr_proxy_failed = False         # Claude Code through Headroom's proxy didn't work: go direct
        self._cli_failed: set[str] = set()   # tasks Claude Code couldn't take this run (reason shown once)
        self.copilot = CopilotCLI(settings.copilot_path, settings.copilot, settings.copilot_model,
                                  settings.copilot_headroom)
        self._copilot_failed: set[str] = set()
        self._copilot_limited_until = 0.0
        self._copilot_hr_failed = False
        self._copilot_tokens: set[str] = set()  # hook tokens of Copilot runs (their steps are shown via the hook)
        self._explained: set[str] = set()
        self.hr_proxy = HeadroomProxy(settings.headroom_proxy, settings.data_dir / "logs")
        self._warned_low: set[str] = set()
        self.policy = Policy(settings.data_dir, settings.approval, settings.unattended_approval)
        self._judged: dict[str, Assessment] = {}
        self._escalate_next: dict[str, tuple[str, str, str | None]] = {}  # task -> (reason, by, target)
        self._blocked: dict[str, dict] = {}  # task -> {"what", "needs"} reported by a specialist
        self._inbox: dict[str, list[str]] = {}  # task -> messages the user sent while it was running
        self.processes: dict[int, dict] = {}  # background processes started by tools
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
            kind, actor = EVENT_KINDS.get(type_, (f"event.{type_}", "system"))
            self.audit.write(kind, actor, task_id, **{(f"{k}_" if k in ("kind", "actor", "task", "ts") else k): v
                                                     for k, v in data.items() if k != "turn"})
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
                                           skills=self.skills.index(), workspace=self.settings.workspace,
                                           port=self.settings.port)
        related = self.store.search_memories(prompt, limit=8, exclude_kinds=PROFILE_KINDS)
        shell_note = (f"and browser actions are rated by risk. Up to '{self.policy.threshold}' runs "
                      "immediately; riskier ones wait for the user's OK, so batch those together and prefer "
                      "safer equivalents when they exist.")
        return prompts.CONDUCTOR.format(
            today=prompts.today(), profile=profile,
            related=prompts.bullet(related, "(none)"), skills=self.skills.index(),
            workspace=self.settings.workspace, shell_note=shell_note, port=self.settings.port,
        )

    def hands_off(self, task_id: str) -> bool:
        return bool((self.store.get_task(task_id).get("route") or {}).get("hands_off"))

    def tool_list(self, depth: int, hands_off: bool = False) -> list[Tool]:
        return [t for t in self.tools.values()
                if not (t.top_level_only and depth > 0) and not (t.delegates and depth >= self.settings.max_depth)
                and not (hands_off and t.name in HANDS_ON_TOOLS)]

    def tool_specs(self, depth: int, hands_off: bool = False) -> list[dict]:
        return [t.spec() for t in self.tool_list(depth, hands_off)]

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

    def send_message(self, task_id: str, prompt: str) -> str:
        """Start a follow-up, or - if the task is mid-run - queue it for the model's next step."""
        if not self.store.get_task(task_id):
            raise KeyError(task_id)
        if task_id in self._running:
            waiting = self.pending_requests(task_id)
            question = next((r for r in waiting if r["kind"] == "question"), None)
            if question:  # it asked you something; typing in the main box answers it
                self.resolve_request(question["id"], True, prompt)
                return "answered"
            for r in waiting:  # typing instead of approving = "no, do this instead"
                if r["kind"] == "approval":
                    self.resolve_request(r["id"], False)
            self._inbox.setdefault(task_id, []).append(prompt)
            asyncio.get_running_loop().create_task(self.emit(task_id, "user", {"text": prompt, "queued": True}))
            return "queued"
        self._launch(task_id, prompt)
        return "started"

    def _drain_inbox(self, task_id: str) -> list[str]:
        return self._inbox.pop(task_id, [])

    async def _deliver(self, task_id: str, msgs: list[str]) -> dict:
        """Mid-task messages become a text block the model sees at its next step."""
        await self.emit(task_id, "user_delivered", {"count": len(msgs)})
        body = "\n\n".join(msgs)
        return {"type": "text", "text": (
            f"[The user sent this while you were working - take it into account now, and adjust your plan "
            f"if it changes anything:]\n{body}")}

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
        if decision.mode == "conduct" and task.get("parent_id") is None and not self.settings.composer_hands:
            route["hands_off"] = True  # decided before the first turn, so the tool list is fixed from the start
        fields: dict[str, Any] = {"model": decision.model, "route": route}
        # Settled before the first model turn, so the system prompt is still unsent.
        system = task["system_prompt"]
        if decision.mode == "conduct" and task.get("parent_id") is None:
            system += prompts.CONDUCT_MODE
            if route.get("hands_off"):
                system += prompts.HANDS_OFF
        fields["system_prompt"] = system + self.seat_text(decision.model, decision.skill, decision.domain)
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
            # Messages that arrived as the run was wrapping up: pick them up now.
            while task_id in self._inbox and self.store.get_task(task_id)["status"] == "done":
                answer = await self.run(task_id, "\n\n".join(self._drain_inbox(task_id)), depth=0, announce=False)
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
        while task_id in self._inbox:  # sent during reflection
            answer = await self.run(task_id, "\n\n".join(self._drain_inbox(task_id)), depth=0, announce=False)
        return answer

    # ------------------------------------------------------------------ the loop
    async def run(self, task_id: str, prompt: str, depth: int = 0, route: dict | None = None,
                  effort: str | None = None, announce: bool = True) -> str:
        self._cli_failed.discard(task_id)  # each run gets a fresh chance at the subscription fallbacks
        self._copilot_failed.discard(task_id)
        self._explained.discard(task_id)
        token = CURRENT_TASK.set(task_id)  # so side calls (supervisor, safety judge) are logged against it
        try:
            return await self._run(task_id, prompt, depth, route, effort, announce)
        finally:
            CURRENT_TASK.reset(token)

    async def _run(self, task_id: str, prompt: str, depth: int, route: dict | None,
                   effort: str | None, announce: bool) -> str:
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
        if announce:
            await self.emit(task_id, "user", {"text": prompt})
        else:
            await self.emit(task_id, "user_delivered", {"count": 1})
        if not self.store.get_task(task_id).get("model"):
            if route is not None:
                await self._route(task_id, prompt, **route)
        elif depth == 0:  # a follow-up: check it hasn't outgrown the current model
            if await self._reroute_followup(task_id, prompt):
                self._handoff_note(task_id, messages)
        task = self.store.get_task(task_id)
        system = task["system_prompt"]

        ctx = ToolContext(conductor=self, task_id=task_id, depth=depth)
        hands_off = depth == 0 and self.hands_off(task_id)
        specs = self.tool_specs(depth, hands_off)
        answer = ""
        seen_calls: dict[str, int] = {}
        changes = 0  # turns that changed something (files, processes, the browser)
        provider_switches = 0  # moves to another model because the provider kept failing
        failing_turns = 0
        hedgy_turns = 0
        nudged_at: int | None = None
        stuck_count = 0
        tool_turns = 0
        try:
            for _ in range(self.settings.max_steps):
                turn = len(messages)
                if task_id in self._escalate_next:
                    reason, by, target = self._escalate_next.pop(task_id)
                    if await self.escalate(task_id, reason, by=by, target=target):
                        self._handoff_note(task_id, messages)
                        hedgy_turns, nudged_at = 0, None
                model = self.catalog.get(self.store.get_task(task_id).get("model") or self.settings.fallback_model)
                via = self._continue_via(task_id, model, hands_off)
                if via:
                    ans = await (self._run_via_cli(task_id, messages, hands_off, "") if via == "claude_cli"
                                 else self._run_via_copilot(task_id, messages, hands_off, ""))
                    if ans is not None:
                        answer = ans
                        break

                async def on_event(kind: str, data: dict, _turn=turn) -> None:
                    await self.emit(task_id, kind, {**data, "turn": _turn})

                byok_only: list[str] = []
                if model.provider == "openrouter":
                    model, cli_why = await self._credit_check(task_id, model, hands_off)
                    if cli_why:  # credits are short and a subscription (Claude Code / Copilot) can take this
                        ans = await self._run_subscription(task_id, model, messages, hands_off, cli_why)
                        if ans is not None:
                            answer = ans
                            break
                        continue  # it couldn't (the reason is shown); the next check falls back as before
                    byok_only = self._byok_only(model)
                    if task_id in self._byok_note_pending:
                        self._byok_note_pending.discard(task_id)
                        self._handoff_note(task_id, messages)
                started_call = time.time()
                try:
                    resp = await self.llm.turn(model=model, system=system, messages=messages, tools=specs,
                                               on_event=on_event, effort=effort,
                                               # OpenRouter's web tools spend credits, so they're off on BYOK-only steps
                                               web=self.settings.web_tools and not hands_off and not byok_only,
                                               session_id=task_id, **({"providers": byok_only} if byok_only else {}))
                    u = resp.get("usage") or {}
                    self.audit.write("model.call", "system", task_id, model=model.id, effort=effort,
                                     stop=resp.get("stop_reason"), tokens_in=u.get("input_tokens"),
                                     cached=u.get("cache_read"), byok=byok_only or (
                                         self.account.byok_for(model.id, model.canonical) if model.provider == "openrouter" else None) or None,
                                     tokens_out=u.get("output_tokens"), cost_usd=round(u.get("cost", 0.0), 6),
                                     ms=int((time.time() - started_call) * 1000), depth=depth)
                except CreditError as ce:
                    self.audit.write("model.credit_limit", "system", task_id, model=model.id, credit_kind=ce.kind,
                                     error=str(ce), ms=int((time.time() - started_call) * 1000))
                    if ce.kind == "out":
                        self.credits.mark_out()
                        why = "OpenRouter credits are used up."
                        if self._sub_first(model) and (ans := await self._run_subscription(task_id, model, messages, hands_off, why)) is not None:
                            answer = ans
                            break
                        if byok_only or not await self._switch_to_byok(task_id, str(ce)):
                            if (ans := await self._run_subscription(task_id, model, messages, hands_off, why)) is not None:
                                answer = ans
                                break
                            await self._pause_for_credits(task_id, ce)
                    elif ce.kind == "too_big":  # this call costs more than the balance, even with a small reply
                        need = self._call_cost(model, system, messages, specs)
                        why = f"The next step needs about ${need:.2f} of OpenRouter credit on hand."
                        if self._sub_first(model) and (ans := await self._run_subscription(task_id, model, messages, hands_off, why)) is not None:
                            answer = ans
                            break
                        if not await self._switch_for_budget(task_id, model, need, str(ce)):
                            if (ans := await self._run_subscription(task_id, model, messages, hands_off, why)) is not None:
                                answer = ans
                                break
                            await self._pause_for_credits(task_id, ce, need=need)
                    elif self._sub_first(model) and self._sub_ready(task_id, model) and (ans := await self._run_subscription(
                            task_id, model, messages, hands_off, "OpenRouter keeps saying your balance can't cover more "
                            "requests right now.")) is not None:
                        answer = ans
                        break
                    else:  # in-flight budget still full after retries: a short breather, not a pause
                        await self.emit(task_id, "status", {"text": "Waiting for in-flight requests to settle..."})
                        await asyncio.sleep(30)
                    continue  # nothing was appended, so the same step simply runs again
                except Exception as call_err:
                    self.audit.write("model.error", "system", task_id, model=model.id,
                                     error=f"{type(call_err).__name__}: {call_err}",
                                     ms=int((time.time() - started_call) * 1000), byok_only=byok_only or None)
                    from .llm import is_transient
                    if not byok_only and is_transient(call_err) and model.provider == "openrouter":
                        why = f"{model.name or model.id} kept failing at the provider."
                        if self._sub_first(model) and (ans := await self._run_subscription(task_id, model, messages, hands_off, why)) is not None:
                            answer = ans
                            break
                        if provider_switches < 2 and await self._switch_after_errors(task_id, model, call_err):
                            provider_switches += 1
                            self._handoff_note(task_id, messages)
                            continue
                    if byok_only:  # e.g. the model isn't enabled on your provider account: try another, or pause
                        self.account.failed[model.id] = str(call_err)[:200]
                        await self.emit(self._root(task_id)["id"], "status", {
                            "text": f"{model.name or model.id} didn't work on your own {', '.join(byok_only)} key "
                                    f"({str(call_err)[:120]}); trying another."})
                        if not await self._switch_to_byok(task_id, "Credits are used up."):
                            if (ans := await self._run_subscription(task_id, model, messages, hands_off,
                                                                    "Credits are used up and your own keys didn't work.")) is not None:
                                answer = ans
                                break
                            await self._pause_for_credits(task_id, CreditError("out", "Credits are used up, and "
                                                                               "none of your own provider keys worked for this."))
                        continue
                    raise
                blocks = resp["content"]
                self._append(task_id, messages, "assistant", blocks, model=model.id)
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
                if not tool_uses and depth == 0 and task_id in self._inbox and stop in ("end_turn", "stop_sequence"):
                    # It was about to finish, but the user just said something: keep going.
                    self._append(task_id, messages, "user", [await self._deliver(task_id, self._drain_inbox(task_id))])
                    continue
                if not tool_uses:
                    answer = text_of(blocks)
                    if stop == "max_tokens":
                        answer += "\n\n_(Reply was cut off at the output limit - say \"continue\" to resume.)_"
                    elif stop == "model_context_window_exceeded":
                        answer += "\n\n_(This conversation is full - start a new task to keep going.)_"
                    break

                truncated = stop == "max_tokens"
                results = list(await asyncio.gather(*[self._run_tool(ctx, b, truncated) for b in tool_uses]))
                if self.compressor.available:  # shrink bulky output once, before it joins the transcript
                    first = messages[0]["content"] if messages else ""
                    results, shrunk = await asyncio.to_thread(
                        self.compressor.compress_results, task_id,
                        first if isinstance(first, str) else text_of(first), blocks, results, model.id)
                    for st in shrunk:
                        self.audit.write("tool.compressed", "system", task_id, **st)

                # Signs the model is struggling: the same call over and over, or turn after turn of errors.
                stuck = None
                for b in blocks:
                    if b.get("type") in ("tool_use", "server_tool_use") and b.get("name") not in ("update_plan",):
                        sig = b.get("name", "") + json.dumps(b.get("input", {}), sort_keys=True)
                        if b.get("name") in READ_TOOLS:  # re-reading after a change is checking, not looping
                            sig += f"@{changes}"
                        seen_calls[sig] = seen_calls.get(sig, 0) + 1
                        if seen_calls[sig] == 3:
                            stuck = f"it repeated the same {b.get('name')} call three times"
                if any(b.get("type") == "tool_use" and b.get("name") in CHANGING_TOOLS for b in blocks):
                    changes += 1
                failing_turns = failing_turns + 1 if results and all(r.get("is_error") for r in results) else 0
                if failing_turns >= 3:
                    stuck = "three steps in a row failed"
                if stuck:
                    stuck_count += 1
                    seen_calls.clear()
                    failing_turns = 0
                    if stuck_count == 1:  # first, move backwards: rethink, reroute, or park it
                        results.append({"type": "text", "text": prompts.STEP_BACK.format(why=stuck)})
                        await self.emit(task_id, "status", {"text": f"Stuck ({stuck}); told it to step back and rethink."})
                    elif self.settings.auto_escalate and task_id not in self._escalate_next:
                        self._escalate_next[task_id] = (f"The model seemed stuck: {stuck}, even after stepping back.",
                                                        "auto", None)

                # Hedging ("hm... actually... wait") means it's out of its depth: nudge it to ask its
                # big brother; if it keeps hedging after the nudge, hand the task up automatically.
                hedges = [m.group(0).lower() for b in blocks if b.get("type") in ("thinking", "text")
                          for m in HEDGES.finditer(b.get("thinking") or b.get("text") or "")]
                hedgy_turns = hedgy_turns + 1 if len(hedges) >= 2 else 0
                if len(hedges) >= 4 or hedgy_turns >= 2:
                    if nudged_at is None:
                        nudged_at = turn
                        hedgy_turns = 0
                        signals = ", ".join(f"“{h}”" for h in list(dict.fromkeys(hedges))[:4])
                        results.append({"type": "text", "text": prompts.HESITATION_NUDGE.format(signals=signals)})
                        await self.emit(task_id, "status", {"text": "The model sounds unsure; nudged it to ask a stronger model."})
                    elif self.settings.auto_escalate and task_id not in self._escalate_next \
                            and self.stronger_model(self.store.get_task(task_id)):
                        self._escalate_next[task_id] = ("It kept hedging after being nudged to ask for help.", "auto", None)
                if depth == 0 and task_id in self._inbox:
                    results.append(await self._deliver(task_id, self._drain_inbox(task_id)))
                tool_turns += 1
                if depth > 0 and self.settings.check_every and tool_turns % self.settings.check_every == 0 \
                        and task_id not in self._blocked:
                    steer = await self._supervise(task_id, messages, blocks)
                    if steer:
                        results.append({"type": "text", "text": steer})
                self._append(task_id, messages, "user", results)
                if task_id in self._blocked:
                    b = self._blocked.pop(task_id)
                    answer = (f"BLOCKED: {b['what']}\nNeeds: {b['needs']}\n\nProgress so far:\n"
                              f"{text_of(blocks) or '(see the workspace and transcript)'}")
                    self.store.update_task(task_id, status="blocked", answer=answer)
                    await self.emit(task_id, "blocked", b)
                    return answer
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

    def _append(self, task_id: str, messages: list[dict], role: str, content: Any, model: str | None = None) -> None:
        messages.append({"role": role, "content": content, **({"model": model} if model else {})})
        self.store.append_message(task_id, role, content, model=model)

    # ------------------------------------------------------------------ escalation
    def request_escalation(self, task_id: str, reason: str, by: str = "user", target: str | None = None) -> bool:
        """Upgrade a task's model: before its next step if running, else for the next message."""
        if task_id in self._running or self.store.get_task(task_id).get("parent_id"):
            self._escalate_next[task_id] = (reason, by, target)
            return True
        return False

    def seat_text(self, model_id: str, skill: str, domain: str) -> str:
        """Tell a model who it is and who it can call up to."""
        me = self.catalog.get(model_id)
        roster = [m for m in self.router.usable_roster() if m.id != model_id]
        if not roster:
            return ""
        roster_ids = [m.id for m in self.catalog.roster()]
        table = self.router._stats()
        est = lambda m: self.router.estimate(m, skill if skill in SKILL_SET else "other", domain or "general",
                                             "hard", roster_ids, table)[0]
        mine = est(me)
        ranked = sorted(roster, key=est, reverse=True)
        above = [m for m in ranked if est(m) > mine + 0.02][:3]
        names = ", ".join(m.name or m.id for m in (above or ranked[:2]))
        if not above:
            return prompts.SEAT_TOP.format(name=me.name or me.id, above=names)
        tier_words = {"fast": "fast, low-cost", "strong": "capable mid-tier", "frontier": "top-tier"}.get(me.tier, "capable")
        return prompts.SEAT_CALL_UP.format(name=me.name or me.id, tier_words=tier_words, above=names)

    def model_options(self, task: dict, skill: str | None = None, domain: str | None = None) -> list[dict]:
        """Models that would likely do this kind of work better than the task's current model,
        cheapest step up first. Each entry explains itself so a model (or you) can choose."""
        from .models import TIERS
        from .router import COST_WEIGHT, TOKEN_ESTIMATE
        route = task.get("route") or {}
        skill = skill or route.get("skill", "other")
        domain = domain or route.get("domain", "general")
        if route.get("mode") == "conduct" and skill == route.get("skill"):
            skill, domain = "orchestration", "general"
        if skill not in SKILL_SET:
            skill = "other"
        roster = self.router.usable_roster()
        ids = [m.id for m in roster]
        table = self.router._stats()
        current = task.get("model") or ""
        cur = self.catalog.get(current)
        cur_q = self.router.estimate(cur, skill, domain, "hard", ids, table)[0] if current else 0.0
        cur_t = TIERS.index(cur.tier) if cur.tier in TIERS else 1
        tin, tout = TOKEN_ESTIMATE["hard"]
        out = []
        for m in roster:
            if m.id == current:
                continue
            q, _, _, runs, bench = self.router.estimate(m, skill, domain, "hard", ids, table)
            if q <= cur_q + 0.03:
                continue
            t = TIERS.index(m.tier) if m.tier in TIERS else 1
            cost = m.cost(tin, tout, int(tin * 0.6))
            out.append({"model": m.id, "name": m.name or m.id, "tier": m.tier, "quality": round(q, 2),
                        "gain": round(q - cur_q, 2), "price": f"${m.price_in:g}/${m.price_out:g} per M tokens",
                        "est_cost": round(cost, 3), "runs": runs, "benchmark": bench,
                        "steps_up": max(t - cur_t, 0),
                        "value": q - COST_WEIGHT["balanced"] * math.log10(1 + cost / 0.005) / 3})
        # Smallest step up that clearly helps comes first; within a step, best value.
        out.sort(key=lambda o: (o["steps_up"], -o["value"]))
        return out

    def cheaper_options(self, task: dict, skill: str | None = None, domain: str | None = None,
                        difficulty: str | None = None) -> list[dict]:
        """Cheaper models that still look good enough for this work (judged at its real difficulty,
        not as if it were hard), cheapest first. Never one the task was already moved up from."""
        from .router import DIFFICULTIES, TOKEN_ESTIMATE
        route = task.get("route") or {}
        skill = skill or route.get("skill", "other")
        domain = domain or route.get("domain", "general")
        if skill not in SKILL_SET:
            skill = "other"
        difficulty = difficulty if difficulty in DIFFICULTIES else route.get("difficulty", "medium")
        if difficulty not in DIFFICULTIES:
            difficulty = "medium"
        current = self.catalog.get(task.get("model") or "")
        escaped = {h.get("from") for h in route.get("escalations") or [] if h.get("direction", "up") == "up"}
        roster = self.router.usable_roster()
        ids = [m.id for m in roster]
        table = self.router._stats()
        tin, tout = TOKEN_ESTIMATE[difficulty]
        cur_cost = current.cost(tin, tout, int(tin * 0.6))
        out = []
        for m in roster:
            if m.id == current.id or m.id in escaped:
                continue
            cost = m.cost(tin, tout, int(tin * 0.6))
            if cur_cost and cost > cur_cost * 0.7:  # must be meaningfully cheaper
                continue
            q, _, _, runs, bench = self.router.estimate(m, skill, domain, difficulty, ids, table)
            if q < 0.68:
                continue
            out.append({"model": m.id, "name": m.name or m.id, "tier": m.tier, "quality": round(q, 2),
                        "price": f"${m.price_in:g}/${m.price_out:g} per M tokens", "est_cost": round(cost, 4),
                        "saving": round(1 - cost / cur_cost, 2) if cur_cost else 0, "runs": runs, "benchmark": bench})
        out.sort(key=lambda o: (o["est_cost"], -o["quality"]))
        return out

    def cheaper_model(self, task: dict) -> str | None:
        opts = self.cheaper_options(task)
        return opts[0]["model"] if opts else None

    def stronger_model(self, task: dict, best: bool = False) -> str | None:
        """Next model up the ladder for this work (or the very best, if best=True)."""
        opts = self.model_options(task)
        if not opts:
            return None
        if best:
            return max(opts, key=lambda o: o["quality"])["model"]
        return opts[0]["model"]

    async def escalate(self, task_id: str, reason: str, by: str = "auto", target: str | None = None) -> str | None:
        task = self.store.get_task(task_id)
        route = dict(task.get("route") or {})
        history = list(route.get("escalations") or [])
        current = task.get("model")
        down = by == "down"
        ups = [h for h in history if h.get("direction", "up") == "up"]
        if by in ("auto", "model") and len(ups) >= self.settings.max_escalations:
            return None
        if len(history) >= self.settings.max_escalations + 3:  # no endless ping-pong
            return None
        if down:
            new = target if target and target != "auto" else self.cheaper_model(task)
            if not new:
                await self.emit(self._root(task_id)["id"], "status", {
                    "text": f"Wanted to hand this down ({reason.rstrip('.')}), but no cheaper model looks good enough for it."})
                return None
        elif target == "best":
            new = self.stronger_model(task, best=True)
        else:
            new = target if target and target != "auto" else self.stronger_model(task)
        if not down and (not new or new == current):
            await self.emit(self._root(task_id)["id"], "status", {
                "text": f"Wanted a stronger model ({reason.rstrip('.')}), but {self.catalog.get(current or '').name or current} "
                        "is already the best available for this."})
            return None
        info = self.catalog.get(new)
        history.append({"from": current, "to": new, "reason": reason, "by": by, "at": time.time(),
                        "direction": "down" if down else "up"})
        route.update({"escalations": history, "model": new, "name": info.name or new})
        self.store.update_task(task_id, model=new, route=route)
        # Teach the router: the earlier model wasn't enough for this kind of work. (Handing down
        # isn't a failure - the result simply gets credited to the cheaper model that finishes it.)
        if current and not down:
            ghost = f"{task_id}#esc{len(history)}"
            self.store.start_run(ghost, current, route.get("skill", "other"), route.get("difficulty", "medium"),
                                 "escalated", domain=route.get("domain", "general"), mode=route.get("mode", "direct"),
                                 parent_id=task.get("parent_id"))
            self.store.finish_run(ghost, "done", 0.0, 0.0)
            self.store.set_review(ghost, 0.3 if by != "user" else 0.2)
        self.store.set_run_model(task_id, new)
        payload = {"task_id": task_id, "from": current, "from_name": self.catalog.get(current or "").name or current,
                   "to": new, "name": info.name or new, "reason": reason, "by": by,
                   "direction": "down" if down else "up"}
        await self.emit(task_id, "escalated", payload)
        if task.get("parent_id"):
            await self.emit(self._root(task_id)["id"], "escalated", payload)
        return new

    async def _supervise(self, task_id: str, messages: list[dict], last_blocks: list[dict]) -> str | None:
        """The composer's eye on a specialist: a cheap model checks recent work against the brief."""
        first = messages[0]["content"] if messages else ""
        brief = first if isinstance(first, str) else " ".join(b.get("text", "") for b in first if b.get("type") == "text")
        actions = []
        for m in messages[-16:]:
            if m["role"] != "assistant" or not isinstance(m["content"], list):
                continue
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    actions.append(f"- {b['name']}: {json.dumps(b.get('input', {}))[:220]}")
                elif b.get("type") == "text" and b.get("text"):
                    actions.append(f"- said: {b['text'][:220]}")
        started, touched = [], []
        for e in self.store.load_events(task_id):
            if e["type"] == "process" and e["data"].get("action") == "started":
                started.append(f"{e['data'].get('id')} ({e['data'].get('name')})")
            elif e["type"] == "artifact" and e["data"].get("path"):
                touched.append(e["data"]["path"])
        own = ("Processes it started: " + (", ".join(started[-10:]) or "none") + "\nFiles it created or changed: "
               + (", ".join(list(dict.fromkeys(touched))[-15:]) or "none"))
        try:
            out = await self.llm.structured(
                model=self.catalog.get(self.settings.default_router_model), system=prompts.SUPERVISE_SYSTEM,
                prompt=prompts.SUPERVISE_PROMPT.format(brief=clip(brief, 6000), own=own,
                                                       actions="\n".join(actions[-14:])),
                schema=prompts.SUPERVISE_SCHEMA, effort="low")
        except Exception as e:
            log.warning("supervision check failed: %s", e)
            return None
        if out.get("on_track", True) is not False:
            return None
        task = self.store.get_task(task_id)
        payload = {"task_id": task_id, "title": task["title"], "part_id": (task.get("route") or {}).get("part_id"),
                   "issue": out.get("issue", ""), "steer": out.get("steer", "")}
        await self.emit(task_id, "steer", payload)
        if task.get("parent_id"):
            await self.emit(task["parent_id"], "steer", payload)
        return prompts.STEER_NOTE.format(issue=out.get("issue", "drifting from the brief"),
                                         steer=out.get("steer", "Return to your brief."))

    # ------------------------------------------------------------------ credits
    def refresh_account(self, force: bool = False) -> None:
        """Re-read BYOK keys, guardrails, and activity in the background (cached for 30 minutes)."""
        if self._account_refresh and not self._account_refresh.done():
            return
        if not force and time.time() - self.account.data.get("checked", 0) < 30 * 60:
            return
        roster = [m.id for m in self.catalog.roster() if m.provider == "openrouter"]
        self._account_refresh = asyncio.create_task(self.account.refresh(roster, force=True))

    async def _credit_check(self, task_id: str, model, hands_off: bool = False):
        """Cheap (cached) look at the balance before a step. Warns once when low. When credits are short
        and Claude Code can take the task, says so (a Claude specialist moves when credits are low; a
        composer, or any other model, only once they're out). Otherwise, when out: move to a model your
        own keys cover, or pause. Returns (model to run this step on, reason to use Claude Code or None)."""
        self.refresh_account()
        st = await self.credits.check()
        root = self._root(task_id)["id"]
        if st["level"] == "low" and root not in self._warned_low:
            self._warned_low.add(root)
            await self.emit(root, "credits_low", {"available": st["available"], "source": st["source"],
                                                  "threshold": st["low_threshold"], "top_up_url": st["top_up_url"],
                                                  "byok": [b.get("provider") for b in self.account.byok_keys()]
                                                  if self.account.has_byok else []})
        level = st["level"]
        cli = self._any_sub_ready(task_id)
        if self._sub_first(model) and self._sub_ready(task_id, model) and (level == "out" or (level == "low" and not hands_off)):
            have = f" (${st['available']:.2f} left)" if st.get("available") is not None else ""
            return model, ("OpenRouter credits are used up." if level == "out"
                           else f"OpenRouter credits are low{have}, so this saves them.")
        if level == "out":
            reason = f"Credits are used up ({st['source']})."
            if not await self._switch_to_byok(task_id, reason):
                if cli:
                    return model, "OpenRouter credits are used up."
                await self._explain_no_cli(task_id)
                await self._pause_for_credits(task_id, CreditError("out", reason))
        return self.catalog.get(self.store.get_task(task_id).get("model") or model.id), None

    async def _explain_no_cli(self, task_id: str) -> None:
        """Say (once per task run) why neither subscription fallback (Claude Code, Copilot) is taking over."""
        if task_id in self._explained:
            return
        self._explained.add(task_id)
        claude = ("turned off (CONDUCTOR_CLAUDE_CLI=off)" if self.cli.mode == "off"
                  else "the `claude` command isn't visible to Conductor (start Conductor from a shell where it "
                       "works, or set CONDUCTOR_CLAUDE_PATH)" if not self.cli.exe
                  else "its usage limit was reached recently" if time.time() < self._cli_limited_until
                  else "it couldn't take this one")
        copilot = ("turned off (CONDUCTOR_COPILOT=off)" if self.copilot.mode == "off"
                   else "not installed (or set CONDUCTOR_COPILOT_PATH)" if not self.copilot.exe
                   else "its quota was reached recently" if time.time() < self._copilot_limited_until
                   else "it couldn't take this one")
        await self.emit(self._root(task_id)["id"], "status", {
            "text": f"Couldn't hand this to a subscription. Claude Code: {claude}. GitHub Copilot: {copilot}."})

    def _copilot_ready(self, task_id: str) -> bool:
        return (self.copilot.available and time.time() >= self._copilot_limited_until
                and task_id not in self._copilot_failed)

    def _sub_first(self, model) -> bool:
        """Models a subscription carries go there before BYOK / cheaper-model switches."""
        return model.is_claude or copilot_serves(model.id)

    def _sub_ready(self, task_id: str, model) -> bool:
        return ((model.is_claude and self._cli_ready(task_id))
                or (copilot_serves(model.id) and self._copilot_ready(task_id)))

    def _any_sub_ready(self, task_id: str) -> bool:
        return self._cli_ready(task_id) or self._copilot_ready(task_id)

    async def _run_subscription(self, task_id: str, model, messages: list[dict], hands_off: bool,
                                reason: str) -> str | None:
        """Hand the task to a subscription worker: Claude tasks try Claude Code, then Copilot; others try
        Copilot, then Claude Code. Returns the answer, or None (with the reason shown) if neither can."""
        order = ("claude", "copilot") if model.is_claude else ("copilot", "claude")
        for worker in order:
            if worker == "claude" and self._cli_ready(task_id):
                ans = await self._run_via_cli(task_id, messages, hands_off, reason)
            elif worker == "copilot" and self._copilot_ready(task_id):
                ans = await self._run_via_copilot(task_id, messages, hands_off, reason)
            else:
                continue
            if ans is not None:
                return ans
        await self._explain_no_cli(task_id)
        return None

    def _cli_ready(self, task_id: str) -> bool:
        return (self.cli.available and time.time() >= self._cli_limited_until
                and task_id not in self._cli_failed)

    async def _switch_after_errors(self, task_id: str, model, err: Exception) -> bool:
        """The provider kept failing even after retries: carry on with the next-best model instead of dying."""
        task = self.store.get_task(task_id)
        route = dict(task.get("route") or {})
        history = list(route.get("escalations") or [])
        tried = {h.get("from") for h in history if h.get("direction") == "sideways"} | {model.id}
        d = self.router.pick(skill=route.get("skill", "other"), domain=route.get("domain", "general"),
                             difficulty=route.get("difficulty", "medium"), budget=self.settings.part_budget,
                             exclude=tried)
        if not d.model or d.model in tried or d.by == "fallback" and d.model == model.id:
            return False
        info = self.catalog.get(d.model)
        why = (f"{model.name or model.id} kept failing at the provider ({str(err)[:140]}), even after retries, "
               f"so {info.name or info.id} is taking over.")
        history.append({"from": model.id, "to": info.id, "reason": why, "by": "provider_error", "at": time.time(),
                        "direction": "sideways"})
        route.update({"escalations": history, "model": info.id, "name": info.name or info.id})
        self.store.update_task(task_id, model=info.id, route=route)
        self.store.set_run_model(task_id, info.id)
        payload = {"task_id": task_id, "from": model.id, "from_name": model.name or model.id, "to": info.id,
                   "name": info.name or info.id, "reason": why, "by": "provider_error", "direction": "sideways"}
        await self.emit(task_id, "escalated", payload)
        root = self._root(task_id)["id"]
        if root != task_id:
            await self.emit(root, "escalated", payload)
        return True

    def _byok_only(self, model) -> list[str]:
        """While credits are out, pin a BYOK-covered model to your own providers (no shared capacity)."""
        if model.provider != "openrouter" or self.credits.status()["level"] != "out":
            return []
        return self.account.byok_for(model.id, model.canonical)

    async def _switch_to_byok(self, task_id: str, reason: str) -> bool:
        """Credits are out: keep working on a model your own provider keys cover. True if the task can go on."""
        if not self.account.has_byok:
            return False
        task = self.store.get_task(task_id)
        current = self.catalog.get(task.get("model") or self.settings.fallback_model)
        if current.provider == "openrouter" and self.account.byok_for(current.id, current.canonical):
            return True  # already covered: the step reruns pinned to your providers
        covered = {m.id for m in self.router.usable_roster()
                   if m.provider == "openrouter" and self.account.byok_for(m.id, m.canonical)}
        if not covered:
            return False
        route = dict(task.get("route") or {})
        d = self.router.pick(skill=route.get("skill", "other"), domain=route.get("domain", "general"),
                             difficulty=route.get("difficulty", "medium"), budget=self.settings.part_budget,
                             only=covered)
        if d.model not in covered:
            return False
        info = self.catalog.get(d.model)
        providers = self.account.byok_for(info.id, info.canonical)
        history = list(route.get("escalations") or [])
        why = (f"{reason.rstrip('.')} - continuing on your own {', '.join(providers)} key (BYOK) "
               "instead of pausing. Hosted web search/fetch is off until credits are back; use the browser "
               "tool for lookups.")
        history.append({"from": current.id, "to": info.id, "reason": why, "by": "credits", "at": time.time(),
                        "direction": "byok"})
        route.update({"escalations": history, "model": info.id, "name": info.name or info.id})
        self.store.update_task(task_id, model=info.id, route=route)
        self.store.set_run_model(task_id, info.id)
        payload = {"task_id": task_id, "from": current.id, "from_name": current.name or current.id, "to": info.id,
                   "name": info.name or info.id, "reason": why, "by": "credits", "direction": "byok",
                   "providers": providers}
        await self.emit(task_id, "escalated", payload)
        root = self._root(task_id)["id"]
        if root != task_id:
            await self.emit(root, "escalated", payload)
        self._byok_note_pending.add(task_id)
        return True

    # ------------------------------------------------------------------ Claude Code (your subscription)
    def _continue_via(self, task_id: str, model, hands_off: bool) -> str | None:
        """Which subscription worker this task should keep running on (or start on, if preferred)."""
        route = self.store.get_task(task_id).get("route") or {}
        via = route.get("via")
        if hands_off and not self.credits.low:  # a composer goes back to composing once credits are fine
            return None
        if via == "claude_cli" and self._cli_ready(task_id):
            return "claude_cli"
        if via == "copilot" and self._copilot_ready(task_id):
            return "copilot"
        if hands_off:
            return None
        if self.cli.mode == "prefer" and model.is_claude and self._cli_ready(task_id):
            return "claude_cli"
        if self.copilot.mode == "prefer" and copilot_serves(model.id) and self._copilot_ready(task_id):
            return "copilot"
        return None

    def _wants_cli(self, task_id: str, model, hands_off: bool) -> bool:  # (kept for older callers)
        return self._continue_via(task_id, model, hands_off) == "claude_cli"

    def _proxy_for_cli(self) -> str | None:
        """Headroom's proxy URL if it's up; if Conductor manages it and it isn't running, (re)start it
        for next time (it takes a few seconds to come up, so this run goes direct)."""
        url = proxy_url(self.settings.headroom_proxy)
        if not url and self.hr_proxy.manages and not self.hr_proxy.running_ours():
            self.hr_proxy.start()
        return url

    def _cli_prompt(self, task: dict, messages: list[dict], resuming: bool) -> str:
        """What Claude Code gets: on a resumed session just what's new, else the brief plus progress so far."""
        if resuming:
            new = []
            for m in reversed(messages):
                if m["role"] == "assistant":
                    break
                new.append(text_of(m["content"]) if isinstance(m["content"], list) else str(m["content"]))
            return "\n\n".join(reversed([t for t in new if t.strip()])) or "Continue where you left off."
        first = messages[0]["content"] if messages else task.get("title", "")
        brief = first if isinstance(first, str) else text_of(first)
        steps = []
        for m in messages[1:]:
            if not isinstance(m["content"], list):
                steps.append(f"[{m['role']}] {str(m['content'])[:600]}")
                continue
            for b in m["content"]:
                kind = b.get("type")
                if kind == "text" and b.get("text", "").strip():
                    steps.append(f"[{m['role']}] {b['text'][:600]}")
                elif kind == "tool_use":
                    steps.append(f"[did] {b.get('name')}: {json.dumps(b.get('input', {}), default=str)[:300]}")
                elif kind == "tool_result":
                    content = b.get("content")
                    content = content if isinstance(content, str) else text_of(content) if isinstance(content, list) else ""
                    steps.append(f"[result{' (error)' if b.get('is_error') else ''}] {str(content)[:300]}")
        progress = clip("\n".join(steps)[-16000:], 16000) if steps else ""
        return (f"{brief}\n\n" + (f"---\nWork already done on this, most recent last:\n{progress}\n---\n"
                                    "Continue from where it stopped; don't redo finished steps. " if progress else "")
                + "When you're done, reply with a short report: what you did, files changed, how you checked it, "
                  "and anything left open.")

    async def _run_via_cli(self, task_id: str, messages: list[dict], hands_off: bool, reason: str) -> str | None:
        """Hand this task to Claude Code on the user's subscription. Returns its answer, or None if it can't."""
        if not self.cli.available or time.time() < self._cli_limited_until or task_id in self._cli_failed:
            if reason:
                await self._explain_no_cli(task_id)
            return None
        task = self.store.get_task(task_id)
        route = dict(task.get("route") or {})
        if hands_off and route.get("via") != "claude_cli":
            # The composer can't hire other models from inside Claude Code, so it finishes the rest itself.
            reason = (reason + " " if reason else "") + "Claude Code will finish the rest of the request itself."
        alias = route.get("cli_model") or model_alias(task.get("model") or "", route.get("difficulty", "medium"))
        resume = route.get("cli_session")
        if route.get("via") != "claude_cli":
            current = self.catalog.get(task.get("model") or self.settings.fallback_model)
            route.update({"via": "claude_cli", "cli_model": alias})
            self.store.update_task(task_id, route=route)
            payload = {"task_id": task_id, "from": current.id, "from_name": current.name or current.id,
                       "to": f"claude-code/{alias}", "name": f"Claude Code ({alias.title()})", "by": "claude_cli",
                       "direction": "cli", "reason": (reason + " " if reason else "")
                       + "Continuing on Claude Code with your Claude subscription."}
            await self.emit(task_id, "escalated", payload)
            if self._root(task_id)["id"] != task_id:
                await self.emit(self._root(task_id)["id"], "escalated", payload)
        token = secrets.token_urlsafe(18)
        self.cli_runs[token] = task_id
        turn = len(messages)
        counter = {"i": 0}

        async def on_event(kind: str, data: dict) -> None:
            if kind == "text":
                counter["i"] += 1
                await self.emit(task_id, "assistant_text", {"turn": turn, "index": counter["i"], "text": data["text"]})
            else:
                await self.emit(task_id, kind, data)

        system = (f"You're a specialist working for Conductor, the user's AI assistant, in its workspace "
                  f"({self.settings.workspace}). The user follows along in Conductor's app, not your terminal, "
                  f"so your final reply is your report to them. Don't use port {self.settings.port} (Conductor "
                  "runs there), and stop any background servers you start once you're done. Some actions may "
                  "be declined by the user's safety settings; if so, find another way or say what you need.")
        started = time.time()
        base = None if self._hr_proxy_failed else await asyncio.to_thread(self._proxy_for_cli)
        prompt = self._cli_prompt(task, messages, bool(resume))
        hook = f"{self.server_url}/api/cli/hook/{token}" if self.server_url else None
        try:
            res = await self.cli.run(task_id, prompt=prompt, model=alias, system=system,
                                     cwd=str(self.settings.workspace), on_event=on_event, hook_url=hook,
                                     resume=resume, max_turns=self.settings.max_steps, base_url=base)
            if base and res.is_error and not res.limited and not res.turns:
                # Through Headroom's proxy it failed before doing anything: go direct from now on.
                self._hr_proxy_failed = True
                await self.emit(task_id, "status", {"text": "Claude Code didn't work through Headroom's proxy; "
                                                           "running it directly instead."})
                res = await self.cli.run(task_id, prompt=prompt, model=alias, system=system,
                                         cwd=str(self.settings.workspace), on_event=on_event, hook_url=hook,
                                         resume=resume, max_turns=self.settings.max_steps)
        except FileNotFoundError:
            return None
        finally:
            self.cli_runs.pop(token, None)
        self.audit.write("model.call", "system", task_id, model=f"claude-code/{res.model or alias}", via="claude_cli",
                         headroom_proxy=bool(base and not self._hr_proxy_failed),
                         ok=not res.is_error, turns=res.turns, ms=int((time.time() - started) * 1000),
                         error=res.error or None, cost_usd=0.0)
        if res.limited or (res.is_error and not res.text):
            self._cli_failed.add(task_id)  # don't keep retrying it this run; fall back as before
        if res.limited:
            self._cli_limited_until = time.time() + 30 * 60
            await self.emit(self._root(task_id)["id"], "status", {
                "text": "Your Claude subscription's usage limit is reached, so Claude Code can't take this right now."})
            return None
        if res.is_error and not res.text:
            await self.emit(task_id, "status", {"text": f"Claude Code couldn't continue: {res.error}"})
            return None
        route = dict(self.store.get_task(task_id).get("route") or {})
        route["cli_session"] = res.session_id or resume
        self.store.update_task(task_id, route=route)
        answer = res.text or "(Claude Code finished without a report.)"
        self._append(task_id, messages, "assistant", [{"type": "text", "text": answer}], model=f"claude-code/{alias}")
        return answer

    async def _run_via_copilot(self, task_id: str, messages: list[dict], hands_off: bool, reason: str) -> str | None:
        """Hand this task to GitHub Copilot CLI on the user's Copilot subscription."""
        if not self._copilot_ready(task_id):
            return None
        task = self.store.get_task(task_id)
        route = dict(task.get("route") or {})
        model = route.get("copilot_model") or copilot_model_name(task.get("model") or "", self.settings.copilot_model)
        if route.get("via") != "copilot":
            if hands_off:
                reason = (reason + " " if reason else "") + "Copilot will finish the rest of the request itself."
            current = self.catalog.get(task.get("model") or self.settings.fallback_model)
            route.update({"via": "copilot", "copilot_model": model})
            self.store.update_task(task_id, route=route)
            payload = {"task_id": task_id, "from": current.id, "from_name": current.name or current.id,
                       "to": f"copilot/{model}", "name": f"GitHub Copilot ({model})", "by": "copilot",
                       "direction": "cli", "reason": (reason + " " if reason else "")
                       + "Continuing on GitHub Copilot with your Copilot subscription."}
            await self.emit(task_id, "escalated", payload)
            if self._root(task_id)["id"] != task_id:
                await self.emit(self._root(task_id)["id"], "escalated", payload)
        hook_file = await asyncio.to_thread(install_copilot_hook)
        token = secrets.token_urlsafe(18)
        self.cli_runs[token] = task_id
        self._copilot_tokens.add(token)
        hook = f"{self.server_url}/api/cli/hook/{token}" if self.server_url and hook_file else None
        preface = (f"You're a specialist working for Conductor, the user's AI assistant, in its workspace "
                   f"({self.settings.workspace}). The user follows along in Conductor's app, so your final reply is "
                   f"your report to them. Don't use port {self.settings.port} (Conductor runs there); stop any "
                   "background servers you start. Some actions may be declined by the user's safety settings; if "
                   "so, find another way or say what you need.\n\n")
        prompt = preface + self._cli_prompt(task, messages, False)

        async def on_event(kind: str, data: dict) -> None:
            await self.emit(task_id, kind, data)
        started = time.time()
        via_hr = False
        try:
            via_hr = not self._copilot_hr_failed and await asyncio.to_thread(self.copilot.use_headroom)
            run = lambda m, hr: self.copilot.run(task_id, prompt=prompt, model=m, cwd=str(self.settings.workspace),
                                                 on_event=on_event, hook_url=hook, via_headroom=hr)
            res = await run(model, via_hr)
            if via_hr and res.is_error and not res.limited:
                self._copilot_hr_failed = True
                via_hr = False
                await self.emit(task_id, "status", {"text": "Copilot didn't work through Headroom; running it directly."})
                res = await run(model, False)
            if res.is_error and res.error.startswith("model:") and model != "auto":
                model = "auto"  # Copilot doesn't have that model: let it pick
                route = dict(self.store.get_task(task_id).get("route") or {})
                route["copilot_model"] = model
                self.store.update_task(task_id, route=route)
                res = await run(model, via_hr)
        except FileNotFoundError:
            return None
        finally:
            self.cli_runs.pop(token, None)
            self._copilot_tokens.discard(token)
        self.audit.write("model.call", "system", task_id, model=f"copilot/{model}", via="copilot", headroom=via_hr,
                         ok=not res.is_error, ms=int((time.time() - started) * 1000), error=res.error or None,
                         cost_usd=0.0)
        if res.is_error:
            self._copilot_failed.add(task_id)
            if res.limited:
                self._copilot_limited_until = time.time() + 30 * 60
            await self.emit(task_id, "status", {"text": "GitHub Copilot couldn't continue: "
                                                        + res.error.removeprefix("model:")})
            return None
        answer = res.text
        self._append(task_id, messages, "assistant", [{"type": "text", "text": answer}], model=f"copilot/{model}")
        return answer

    async def cli_permission(self, task_id: str, tool: str, inp: dict, show: bool = False) -> tuple[bool, str]:
        """A subscription worker asks before a tool call. `show` also puts the step in the task (Copilot,
        whose own steps aren't streamed)."""
        allowed, why = await self._cli_permission(task_id, tool, inp)
        if show:
            from .claude_cli import TOOL_NAMES, describe
            await self.emit(task_id, "tool_call", {
                "id": f"copilot-{secrets.token_hex(4)}", "name": TOOL_NAMES.get(tool, tool), "risky": False,
                "summary": describe(tool, inp) + ("" if allowed else " (declined)"), "via": "copilot",
                "input": {k: (v[:2000] if isinstance(v, str) else v) for k, v in (inp or {}).items()}})
        return allowed, why

    async def _cli_permission(self, task_id: str, tool: str, inp: dict) -> tuple[bool, str]:
        """Claude Code asks before each tool call: rate it with Conductor's rules and ask the user if needed."""
        from .safety import SECRET_PATHS, assess_shell
        ws = self.settings.workspace.resolve()

        def outside(path: str) -> bool:
            try:
                return not Path(path).expanduser().resolve().is_relative_to(ws)
            except (OSError, ValueError):
                return True
        if tool == "Bash":
            cmd = inp.get("command", "")
            ok = await self.request_approval(task_id, "run_shell", f"Run: {cmd}", {"command": cmd},
                                             assess_shell(cmd, self.settings.workspace))
            return ok, "" if ok else "Declined by the user's safety settings."
        path = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            a = (Assessment("high", "It writes outside the workspace.") if outside(path)
                 else Assessment("low", "It writes a file in the workspace."))
            ok = await self.request_approval(task_id, "write_file", f"Edit {path}", {"path": path}, a)
            return ok, "" if ok else "Declined by the user's safety settings."
        if tool in ("Read", "Glob", "Grep", "LS"):
            if path and SECRET_PATHS.search(path):
                ok = await self.request_approval(task_id, "read_file", f"Read {path}", {"path": path},
                                                 Assessment("high", "It reads credentials."))
                return ok, "" if ok else "Declined: that's a credentials file."
            return True, ""
        if tool in ("WebFetch", "WebSearch", "TodoWrite", "Agent", "Task", "Skill", "ToolSearch"):
            return True, ""  # reading the web / planning / helpers (whose own tool calls come back here)
        ok = await self.request_approval(task_id, "claude_cli", f"Claude Code: {tool}", {"tool": tool},
                                         Assessment("medium", f"Claude Code wants to use {tool}."))
        return ok, "" if ok else "Declined by the user's safety settings."

    def _call_cost(self, model, system: str, messages: list[dict], tools: list[dict], reply: int = 4096) -> float:
        """Roughly what OpenRouter will want on hand for one call: the whole prompt plus a small reply."""
        chars = len(system) + len(json.dumps(messages, default=str)) + len(json.dumps(tools, default=str))
        return model.cost(int(chars / 3.5), reply)

    async def _switch_for_budget(self, task_id: str, model, need: float, reason: str) -> bool:
        """The current model's call doesn't fit the balance: move to one whose call does (your own keys
        first), instead of pausing. True if the task can go on."""
        st = self.credits.status()
        available = st.get("available")
        if available is None:
            return False
        tokens_in = need / max(model.price_in, 1e-9) * 1e6 if model.price_in else 0
        task = self.store.get_task(task_id)
        route = dict(task.get("route") or {})
        fits = set()
        for m in self.router.usable_roster():
            if m.id == model.id or m.provider != "openrouter":
                continue
            cost = m.cost(int(tokens_in), 4096)
            credit_cost = self.account.credit_cost(m.id, cost, m.canonical)
            if credit_cost < available * 0.8 and m.context >= tokens_in * 1.1:
                fits.add(m.id)
        if not fits:
            return False
        d = self.router.pick(skill=route.get("skill", "other"), domain=route.get("domain", "general"),
                             difficulty=route.get("difficulty", "medium"), budget="frugal", only=fits)
        if d.model not in fits:
            return False
        info = self.catalog.get(d.model)
        why = (f"{model.name or model.id}'s next step needs about ${need:.2f} on hand and only ${available:.2f} is "
               f"left, so {info.name or info.id} (fits the balance) is taking over instead of pausing.")
        history = list(route.get("escalations") or [])
        history.append({"from": model.id, "to": info.id, "reason": why, "by": "credits", "at": time.time(),
                        "direction": "sideways"})
        route.update({"escalations": history, "model": info.id, "name": info.name or info.id})
        self.store.update_task(task_id, model=info.id, route=route)
        self.store.set_run_model(task_id, info.id)
        payload = {"task_id": task_id, "from": model.id, "from_name": model.name or model.id, "to": info.id,
                   "name": info.name or info.id, "reason": why, "by": "credits", "direction": "sideways"}
        await self.emit(task_id, "escalated", payload)
        root = self._root(task_id)["id"]
        if root != task_id:
            await self.emit(root, "escalated", payload)
        self._byok_note_pending.add(task_id)  # the incoming model gets a hand-off note before its first step
        return True

    async def _pause_for_credits(self, task_id: str, err: CreditError, need: float | None = None) -> None:
        """Out of credits: park this task (and show it on the main task) until credits are back."""
        self.credits.mark_out()
        task = self.store.get_task(task_id)
        root = self._root(task_id)
        st = self.credits.status()
        if need is None and st["available"] is not None:
            need = st["available"] + 0.25  # OpenRouter says no at this balance: wait for a real top-up
        payload = {"task_id": task_id, "title": task["title"], "reason": str(err), "available": st["available"],
                   "source": st["source"], "top_up_url": st["top_up_url"], "visible": st["available"] is not None,
                   "need": round(need, 2) if need else None}
        self.store.update_task(task_id, status="paused")
        await self.emit(task_id, "paused", payload)
        if root["id"] != task_id:
            self.store.update_task(root["id"], status="paused")
            await self.emit(root["id"], "paused", payload)
        how = await self.credits.wait_for_credits(poll=None if st["available"] is not None else 120, need=need)
        self.credits.state["out"] = False
        self.store.update_task(task_id, status="running")
        if root["id"] != task_id:
            self.store.update_task(root["id"], status="running")
        resumed = {"task_id": task_id, "how": how}
        await self.emit(task_id, "resumed", resumed)
        if root["id"] != task_id:
            await self.emit(root["id"], "resumed", resumed)

    def resume(self, task_id: str) -> str:
        """User says credits are back: wake paused work, or restart a task paused before a restart."""
        if task_id in self._running:
            self.credits.resume_now()
            return "resumed"
        self.credits.state["out"] = False
        self.send_message(task_id, "Credits are topped up - continue where you left off.")
        return "restarted"

    def _handoff_note(self, task_id: str, messages: list[dict]) -> None:
        """Tell the incoming model who it is and why it's here (as a user-side note; history is append-only)."""
        route = self.store.get_task(task_id).get("route") or {}
        last = (route.get("escalations") or [{}])[-1]
        if not last:
            return
        template = prompts.HANDDOWN_NOTE if last.get("direction") == "down" else prompts.HANDOFF_NOTE
        note = template.format(new=self.catalog.get(last["to"]).name or last["to"],
                               old=self.catalog.get(last.get("from") or "").name or last.get("from"),
                               reason=last.get("reason", ""))
        self._append(task_id, messages, "user", [{"type": "text", "text": note}])

    async def _reroute_followup(self, task_id: str, prompt: str) -> str | None:
        """A follow-up can be much bigger than the first message; move up if it outgrew the model."""
        task = self.store.get_task(task_id)
        route = task.get("route") or {}
        if not self.settings.auto_escalate or route.get("by") == "pinned" or route.get("mode") == "conduct":
            return None
        try:
            label = await self.router.classify(prompt)
        except Exception:
            return None
        from .router import DIFFICULTIES
        old_d = route.get("difficulty", "medium")
        old_rank = DIFFICULTIES.index(old_d) if old_d in DIFFICULTIES else 1
        new_rank = DIFFICULTIES.index(label["difficulty"])
        if new_rank <= old_rank and label["skill"] == route.get("skill"):
            return None
        route = {**route, "skill": label["skill"], "domain": label["domain"],
                 "difficulty": DIFFICULTIES[max(old_rank, new_rank)]}
        self.store.update_task(task_id, route=route)
        if new_rank > old_rank or old_d == "easy":
            return await self.escalate(task_id, f"The work grew into {label['skill'].replace('_', ' ')} "
                                         f"({label['difficulty']}) beyond the first question.", by="auto")

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
        if tool is None or tool not in self.tool_list(ctx.depth, ctx.depth == 0 and self.hands_off(ctx.task_id)):
            return result(f"Unknown tool '{name}'.", True)
        problem = tool.validate(args)
        if problem:
            return result(json.dumps({"INVALID_INPUT": problem}), True)

        await self.emit(ctx.task_id, "tool_call", {
            "id": tid, "name": name, "summary": tool.describe(args), "risky": tool.risky,
            "input": {k: (clip(v, 2000) if isinstance(v, str) else v) for k, v in args.items()},
        })
        tool_started = time.time()
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
                                                      "ms": int((time.time() - tool_started) * 1000),
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

    def depth_of(self, task_id: str) -> int:
        depth, task = 0, self.store.get_task(task_id)
        while task and task.get("parent_id"):
            depth += 1
            task = self.store.get_task(task["parent_id"])
        return depth

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
            answer = await self.run(child, brief, depth=self.depth_of(child), effort=effort)
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
                                depth=self.depth_of(task_id), effort=self.settings.effort)
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
        import os, signal
        self.cli.stop_all()
        self.copilot.stop_all()
        self.hr_proxy.stop()
        for p in list(self.processes.values()):
            try:
                os.killpg(p["pgid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        self.processes.clear()
        for t in list(self._running.values()):
            t.cancel()
        if self.browser_pool:
            await self.browser_pool.shutdown()
