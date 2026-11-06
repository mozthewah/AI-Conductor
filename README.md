# Conductor

A self-hosted AI conductor: you hand it a task, and it plans, searches the web, drives a real
browser, writes and runs code, splits work across parallel sub-agents, and reports back.
It **remembers** what it learns about you and **writes down skills** for itself, so it gets better
at your recurring work over time.

It's in the same family as Hermes Agent and OpenClaw, with a calm, Perplexity-Computer-style
interface: a live activity timeline on the left and a "computer" panel on the right (plan
checklist, browser view, files, sub-agents).

It runs on **any model on OpenRouter** (Claude, GPT, Gemini, Grok, DeepSeek, Qwen, Kimi, GLM…) and/or
Claude direct from Anthropic. A **learning router** picks the model for each task and gets better
at it from your results.

## What it can do

| Capability | How |
|---|---|
| **Picks the right model** | A cheap classifier labels each task, then a router chooses from your roster by track record and cost. You can also pin a model or compare two side by side |
| **Plans** | `update_plan` renders a live checklist you can watch tick off |
| **Researches** | Hosted web search + fetch (OpenRouter's for any model, Anthropic's for direct Claude), with cited sources |
| **Uses a computer** | A real Chromium tab per task (Playwright): click, type, read, and screenshots streamed to the UI |
| **Does real work** | Files and a shell inside `./workspace`, so it can write reports, build and test scripts, and crunch data |
| **Conducts** | Splits multi-part requests, hires the best model per part, reviews their work, and integrates it |
| **Remembers** | SQLite + full-text search. Facts about you are injected into every task; past-task summaries are recalled when relevant |
| **Learns skills** | Reusable procedures saved as Markdown in `data/skills/`, listed in every task's prompt and loaded on demand |
| **Reflects** | After each task, a short review decides what's worth remembering and whether a new skill was learned |
| **Runs on a schedule** | "Every weekday at 8am, brief me on…" becomes a recurring task |
| **Checks with you** | Asks before shell commands (configurable), asks questions when a decision is really yours, and confirms before irreversible actions |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[browser]"           # drop [browser] if you don't want the browser tool
playwright install chromium           # only needed for the browser tool

cp .env.example .env                  # add OPENROUTER_API_KEY (and/or ANTHROPIC_API_KEY)
conductor serve                       # → http://127.0.0.1:8765
```

Prefer the terminal?

```bash
conductor chat                        # interactive
conductor chat "summarize the files in my workspace"
conductor chat --auto                 # don't ask before shell commands
```

## The conductor: the best model for each part

Conductor doesn't use one model for everything. When a request needs several kinds of work
(say, *design and build an app*, or *find travel deals and budget the trip*), a **conductor** model
takes charge:

1. **Splits** the request into parts. Each part gets a skill (`coding`, `ui_design`,
   `architecture`, `research`, `writing`, `analysis`, `planning`, `browsing`) and a domain
   (`travel`, `finance`, `health`, … or `general`), plus what it depends on.
2. **Hires** the best model for each part from the router, so design might go to one model and the
   code to another. Independent parts run in parallel. A part that depends on another waits, then
   gets that part's output and files.
3. **Reviews** every part: **accept** it, **revise** it (the same model fixes it, in the same
   conversation), or **reassign** it (a different model takes over).
4. **Integrates** the result and tells you which model did what.

Simple, single-kind requests skip the conductor and go straight to the best model for that work.
The **Team** tab shows every part, its model, and its review status, and you can 👍/👎 each part.

## Models and the router

Every model talks through the same Anthropic Messages format: Anthropic natively, everything else
through [OpenRouter's Messages-compatible endpoint](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message.md).
So tools, streaming, the browser, memory, and skills work the same whichever model runs a task.

**How a model gets picked for a piece of work (skill + domain):**

1. **Classify.** A small model (Claude Haiku 5.5 by default, about a hundredth of a cent per request) labels
   the request: main skill, domain, difficulty, and the distinct kinds of work involved. More than
   one kind, or hard work, gets a conductor.
2. **Start from benchmarks.** OpenRouter's benchmark feed gives a day-one ranking per skill:
   Design Arena for `ui_design`, coding indexes for `coding`, search evals for `research`, agentic
   scores for `browsing` and conducting, and general intelligence for the rest. It's fetched with your
   key and cached daily. Models without benchmark data fall back to their price tier.
3. **Learn from you.** Results update each model's score: 👍 = 1, 👎 = 0, the conductor's review
   (accepted first try 0.9, after revisions less, reassigned 0.1), and otherwise finished OK = 0.7.
   Results count fully for the exact skill + domain, half for the same skill in other domains, and a
   quarter for the same domain with other skills. So if Kimi keeps nailing finance analysis, it gets
   the edge on new finance work too.
4. **Pick.** Expected quality minus a cost penalty set by the budget. Core parts and the conductor
   default to quality first (`best`), supporting parts lean cheap. About 10% of the time it samples
   instead of taking the top score (Thompson sampling), so it keeps learning about less-proven models.

**What you see:**
- A model picker on the composer (Auto, or pin a model)
- A "Routed to …" card on each task explaining the choice and the runners-up
- 👍/👎 plus cost and tokens under each answer; your feedback is the router's main signal
- **Try another model**, which re-runs the same request on a different model side by side so you can compare
- A **Models** page with a per-category scoreboard and a roster editor that searches OpenRouter's live catalog

Sub-agents are routed too, leaning cheap (`CONDUCTOR_SUBAGENT_BUDGET=frugal`): the lead model plans and
synthesizes while cheaper models do the reading. The catalog (prices, context, which models support
tools, images, or reasoning) refreshes from OpenRouter daily and is cached in `data/`.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | – | Enables every OpenRouter model |
| `ANTHROPIC_API_KEY` | – | Enables Claude direct from Anthropic (optional if you use OpenRouter) |
| `CONDUCTOR_MODEL` | `auto` | `auto` = router picks per task; or pin an id like `openai/gpt-6.1-sol` or `claude-opus-5-5` |
| `CONDUCTOR_BUDGET` | `balanced` | `frugal` / `balanced` / `best`: how much the router weighs cost |
| `CONDUCTOR_LEAD_BUDGET` | `best` | Budget for picking the conductor |
| `CONDUCTOR_PART_BUDGET` | `best` | Budget for core parts |
| `CONDUCTOR_SUBAGENT_BUDGET` | `frugal` | Budget for supporting parts |
| `CONDUCTOR_SUBAGENT_MODEL` | `auto` | Pin every specialist to one model instead |
| `CONDUCTOR_ROUTER_MODEL` | `anthropic/claude-haiku-5.5` | The classifier |
| `CONDUCTOR_EXPLORE` | `0.1` | Share of tasks where the router tries a less-proven model |
| `CONDUCTOR_AUTO_ESCALATE` | `on` | Move tasks to a stronger model when they outgrow or struggle on the current one |
| `CONDUCTOR_EXPERT_MODEL` | `auto` | Who `consult_expert` asks (auto = the best available for the work) |
| `CONDUCTOR_EFFORT` | `high` | Reasoning effort for models that support it (`low` … `max`). Sub-agents run at `medium` |
| `CONDUCTOR_APPROVAL` | `low` | Auto-approve actions up to `safe` / `low` / `medium` / `high` (critical always asks). Also editable on the Safety page |
| `CONDUCTOR_UNATTENDED_APPROVAL` | `low` | Same, for scheduled runs; anything above it is declined |
| `CONDUCTOR_LEARNING` | `on` | Reflect after tasks and save memories and skills |
| `CONDUCTOR_WEB_TOOLS` | `on` | Hosted web search and fetch |
| `CONDUCTOR_BROWSER` | `on` | Local Chromium browser tool |
| `CONDUCTOR_DATA_DIR` | `./data` | Database and skills |
| `CONDUCTOR_WORKSPACE` | `./workspace` | The only directory its file tools and shell work in |
| `CONDUCTOR_CHROMIUM` | – | Path to a Chromium binary, if Playwright can't find one |

## How it works

```
you ──► Conductor.start_task
          │  system prompt = persona + what it knows about you + related memories + skill index
          ▼  (frozen for the life of the task)
        ┌─────────────────── agent loop ───────────────────┐
        │ stream a Claude turn  (adaptive thinking,         │
        │   progress updates, server-side refusal fallback) │
        │ run requested tools in parallel ──► events ──► UI │
        │ append results (transcript is append-only)        │
        └──────────────── until no more tool calls ─────────┘
          ▼
        final answer ──► reflection ──► memories / episode summary / new skill
```

- `conductor/agent.py` – the loop, sub-agents, approvals and questions, reflection
- `conductor/llm.py` – streaming client for Anthropic + OpenRouter (the only file that talks to model APIs)
- `conductor/models.py` – live OpenRouter catalog, prices, capabilities, and the roster
- `conductor/router.py` – classification (skill, domain, parts), scoring, and the learning model picker
- `conductor/benchmarks.py` – OpenRouter benchmark feed, turned into per-skill starting scores
- `conductor/tools/agents.py` – the conductor's `assign` / `review` tools (parts, dependencies, revisions)
- `conductor/tools/` – one module per capability; add your own by appending a `Tool`
- `conductor/store.py` – SQLite: tasks, transcripts, event log, memory (FTS5), schedules, router run log
- `conductor/skills.py` – the Markdown skill library
- `conductor/server.py` + `conductor/web/` – FastAPI + SSE + a framework-free UI

### Adding a tool

```python
# conductor/tools/weather.py
from . import Tool

async def get_weather(ctx, args):
    return f"Sunny in {args['city']}"

TOOLS = [Tool("get_weather", "Current weather for a city.",
              {"city": {"type": "string"}}, ["city"], get_weather)]
```

Then add `tools += weather.TOOLS` in `build_registry` (`conductor/tools/__init__.py`).
Set `risky=True` and call `await ctx.approve(...)` in the handler for anything that should ask first.

## Calling up to a stronger model

A task starts on the model the router picks for the first message. But a conversation can grow:
"do you have access to my repos?" can turn into "clone it, install Go, and get the design reviewed".
Conductor moves up a tier when that happens:

- **Follow-ups are re-triaged.** If a new message is harder (or a heavier kind of work) than the
  task was routed for, the task moves to the best model for the new work.
- **It notices when a model struggles.** Repeating the same call three times, or three failing steps
  in a row, hands the task to a stronger model with a note to step back and change approach.
- **Models know where they stand.** Each model is told which model it's running as and who is on
  call above it, with concrete cues for calling up: hedging ("hm… actually… wait"), guessing at
  facts, reconsidering the same point, or unfamiliar territory.
- **Models can call up themselves:**
  - `find_model` lists roster models that would likely handle the problem better, with their track
    record, price, and how big a step up each is (smallest step first). It's free and calls no model.
  - `consult_expert` sends one question (with context) to a stronger model and returns its advice,
    while the task stays put.
  - `escalate` hands the whole task over.

  The last two take an optional model choice from `find_model`. By default they go to the *next step
  up*, the cheapest model that's clearly better at this kind of work, not straight to the most
  expensive one.
- **Hedging gets noticed.** A turn full of "hm / actually / wait / not sure" gets a note to ask
  instead of guessing. If the model keeps hedging after that, the task moves up a step automatically.
- **You can do it too.** The **⬆ Stronger model** menu on every task offers the next step up, the
  best available model, or any model in the roster.

The new model sees the whole conversation. The earlier model's private reasoning notes are left
out (only the model that wrote them can read them), and its web-tool records become plain-text
summaries. Being escalated away from counts against a model for that kind of work, so the router
learns not to start there next time. Upgrades climb one step at a time (e.g. a fast model → a mid-tier one → a top-tier one), and the incoming model gets a note
on who it's replacing and why. Automatic upgrades are capped at two per task
(`CONDUCTOR_AUTO_ESCALATE=off` disables them).

Reasoning notes in the activity feed are collapsed to a one-line preview. Click one to expand it,
or use the 💭 button on a task to show them all.

## Approvals by risk level

Every shell command and browser action is rated before it runs:

| Level | What it means | Examples |
|---|---|---|
| **safe** | Read-only | `ls`, `cat`, `git status`, opening a page |
| **low** | Small, reversible changes in the workspace | `mkdir`, `cp`, writing a file, `git add`, clicking a link |
| **medium** | Installs, runs code, deletes workspace files | `pip install`, `python app.py`, `rm build/`, typing into forms |
| **high** | Outside the workspace or outward-facing | `sudo`, `git push`, sending data, reading SSH keys, clicking Buy/Send/Delete |
| **critical** | Destructive or system-level | `rm -rf ~`, formatting disks, `curl … \| sh`. Always asks. |

Actions up to your auto-approve level just run (default: **low**). Riskier ones show an approval
card with the level and the reason. **Always allow** remembers the command's first two words
*together with its level*, so allowing `npm test` never covers `npm test && git push`.
Commands the built-in rules don't recognize are rated by the router's small model, with its
reasoning shown. Scheduled runs have their own limit, and anything above it is declined because
nobody's there to ask. Every automatic decision is logged in the task's activity feed. Change the
levels and manage remembered rules on the **Safety** page.

## Safety notes

- File tools are confined to the workspace. **The shell is not a sandbox**: a command can reach
  anything your user account can. Keep `CONDUCTOR_APPROVAL=ask` unless you run it in a container or VM.
- API keys are removed from the environment of shell commands it runs.
- The browser shares nothing with your personal browser profile. To let it use logged-in sites,
  you'd have to wire that up deliberately.
- Scheduled tasks run unattended: they can't ask questions, and shell commands are declined
  unless approval is `auto`.
- Bind to `127.0.0.1` (the default). There's no login, so don't expose the server to a network as is.

## Tests

```bash
pip install -e ".[dev,browser]"
pytest
```

The suite runs offline. It uses a scripted fake model for the agent loop and router, mocked HTTP
endpoints to check the exact requests sent to Anthropic and OpenRouter (and how their streams are
parsed), and a live Chromium smoke test (skipped if no browser is installed).

## Ideas for next steps

- Telegram bot, so you can hand it tasks from your phone anywhere
- Connect MCP servers (Gmail, Calendar, Drive, …) as additional tools
- Push or email notifications when scheduled tasks finish
- Docker image with the shell sandboxed
- Auth plus multi-user memory for running it on a home server
