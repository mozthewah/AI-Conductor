# Conductor

A self-hosted AI conductor: you hand it a task, and it plans, searches the web, drives a real
browser, writes and runs code, splits work across parallel sub-agents, and reports back.
It **remembers** what it learns about you and **writes down skills** for itself, so it gets better
at your recurring work over time.

It's in the same family as Hermes Agent and OpenClaw, with a calm, Perplexity-Computer-style
interface: a live activity timeline on the left and a "computer" panel on the right (plan
checklist, browser view, files, sub-agents).

Powered by Claude (`claude-opus-5-5` by default) through the official Anthropic Python SDK.

## What it can do

| Capability | How |
|---|---|
| **Plans** | `update_plan` renders a live checklist you can watch tick off |
| **Researches** | Anthropic-hosted `web_search` + `web_fetch`, with cited sources |
| **Uses a computer** | A real Chromium tab per task (Playwright): click, type, read, and screenshots streamed to the UI |
| **Does real work** | Files and a shell inside `./workspace`, so it can write reports, build and test scripts, and crunch data |
| **Delegates** | `delegate` runs up to 6 sub-agents in parallel, each with its own tools and transcript |
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

cp .env.example .env                  # add ANTHROPIC_API_KEY (or run `ant auth login`)
conductor serve                       # → http://127.0.0.1:8765
```

Prefer the terminal?

```bash
conductor chat                        # interactive
conductor chat "summarize the files in my workspace"
conductor chat --auto                 # don't ask before shell commands
```

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CONDUCTOR_MODEL` | `claude-opus-5-5` | Model for the conductor, sub-agents, and reflection |
| `CONDUCTOR_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max`. Sub-agents run at `medium` and reflection at `low` |
| `CONDUCTOR_APPROVAL` | `ask` | `ask` = approve each shell command; `auto` = run without asking |
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
- `conductor/llm.py` – streaming Claude wrapper (the only file that talks to the API)
- `conductor/tools/` – one module per capability; add your own by appending a `Tool`
- `conductor/store.py` – SQLite: tasks, transcripts, event log, memory (FTS5), schedules
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

## Safety notes

- File tools are confined to the workspace. **The shell is not a sandbox**: a command can reach
  anything your user account can. Keep `CONDUCTOR_APPROVAL=ask` unless you run it in a container or VM.
- The API key is removed from the environment of shell commands it runs.
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

The suite runs offline. It uses a scripted fake model for the agent loop, a mocked HTTP endpoint
to check the real Claude request and stream parsing, and a live Chromium smoke test (skipped if
no browser is installed).

## Ideas for next steps

- Connect MCP servers (Gmail, Calendar, Drive, …) as additional tools
- Push or email notifications when scheduled tasks finish
- Docker image with the shell sandboxed
- Auth plus multi-user memory for running it on a home server
