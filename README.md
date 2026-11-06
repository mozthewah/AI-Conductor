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

## The composer: the best model for each part

Conductor doesn't use one model for everything. For anything beyond a quick question, the lead
model is a **composer**. It doesn't do the work itself: it plans, hands each piece to the specialist
model best at it, keeps them on task, and puts the result together.

1. **Splits** the request into parts. Each part gets a skill (`coding`, `ui_design`,
   `architecture`, `research`, `writing`, `analysis`, `planning`, `browsing`) and a domain
   (`travel`, `finance`, `health`, … or `general`), plus what it depends on.
2. **Hires** the best model for each part from the router, so design might go to one model and the
   code to another. Independent parts run in parallel. A part that depends on another waits, then
   gets that part's output and files.
3. **Reviews** every part: **accept** it, **revise** it (the same model fixes it, in the same
   conversation), or **reassign** it (a different model takes over).
4. **Integrates** the result and tells you which model did what.

Quick questions (chat or easy one-step work) skip the composer and go straight to the best model.
`CONDUCTOR_COMPOSE=always` composes everything; `off` composes only obviously multi-part requests.

**The composer has no hands.** In a composed task, the composer can plan, assign, review, read files,
ask you and message you, but it can't run commands, edit files, browse, or search the web. Every
piece of actual work goes to a specialist. (`CONDUCTOR_COMPOSER_HANDS=on` gives it hands back.)

**Specialists get the same toolbox as the composer,** including handing off sub-parts of their own
(up to `CONDUCTOR_MAX_DEPTH`, default 2 levels) and asking you questions.

**Keeping specialists on task.** Every few steps (`check_every`, default 4) a cheap model compares
a specialist's recent actions to its brief. If it's drifting, the specialist gets a steer note
("you're off-brief: focus on X"). The steer shows in the composer's feed and the Team panel.

**Moving backwards.** The first time a model seems stuck (repeating itself, failing step after
step), it's told to step back: re-read the brief, question its assumptions, try a different route,
or set the piece aside and work on something unblocked. Only if it's stuck again does it move up a
model. A specialist that can't continue without a decision, access, or information calls
`report_blocked`. The composer then gets what's needed (from you via `ask_user`, from other parts,
or the workspace) and resumes that specialist where it stopped, while independent parts keep
going. Plan steps can be marked *blocked* too.
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
| `OPENROUTER_MANAGEMENT_KEY` | – | Optional, read-only use: balance, BYOK keys, guardrails, 30-day spend (see [The management key](#the-management-key-byok-guardrails-spend)) |
| `CONDUCTOR_BYOK` | `auto` | `auto`: lean on your own provider keys when credits run low or out; `prefer`: always; `off` |
| `CONDUCTOR_HOST` | `127.0.0.1` | `0.0.0.0` to reach it from other devices (same as `serve --lan`) |
| `CONDUCTOR_PORT` | `8765` | Web server port |
| `CONDUCTOR_ACCESS_TOKEN` | auto | Passcode for other devices (default: generated into `data/access_token`) |
| `CONDUCTOR_STAY_AWAKE` | `off` | Keep the computer from sleeping while serving (same as `serve --stay-awake`) |
| `CONDUCTOR_KEY_NAME` | – | Name of your OpenRouter API key, if `doctor` can't match it automatically |
| `CONDUCTOR_CLAUDE_CLI` | `fallback` | Hand work to Claude Code on your subscription: `fallback`, `prefer`, `off` |
| `CONDUCTOR_CLAUDE_PATH` | – | Path to the `claude` CLI if it isn't on PATH |
| `CONDUCTOR_HEADROOM` | `auto` | Compress bulky tool output with Headroom when it's installed (`auto`, `on`, `off`) |
| `CONDUCTOR_HEADROOM_PROXY` | `auto` | Headroom's proxy for the Claude Code fallback: `auto` (Conductor runs it), `connect` (you run it), `off`, or a URL |
| `CONDUCTOR_AUDIT` | `on` | Local audit log in `data/logs/` (see [Audit log](#audit-log)) |
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
- **Models hand down, too.** The reverse of escalating: `hand_down` passes the rest of a task to a
  cheaper model when what's left is routine (formatting, simple edits, mechanical changes). It picks
  the cheapest model that's still good enough for that work, judged at the work's real difficulty,
  and never one the task was already moved up from. `find_model(direction="cheaper")` shows the
  options. Top-tier models are told not to spend themselves on routine work. Handing down isn't held
  against the bigger model; the result is credited to whichever model finishes it. The task menu
  also offers "⬇ Cheaper model".
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

## Long-running commands and showing you things

- `run_shell` with `background: true` starts servers and watchers without blocking. It returns the
  process id, log path, and any local URL it's serving. The `processes` tool lists them, reads
  their logs, and stops them. Background processes are stopped when Conductor shuts down. A
  foreground command that leaves something running (`cmd &`) no longer hangs the tool; the
  leftover is tracked so it can be stopped.
- Models know Conductor's own port and won't start anything on it.
- The agent's browser is private, so anything you should see or click (a local page, a mockup, a
  report) comes to you through `show_user`: a card in the feed with a link, or a preview for images.
- Web fetches show ✓ with their size, or the error, in the feed.

## Credits

Conductor watches your OpenRouter credits and handles running low or out without losing work:

- **Balance:** shown in the sidebar. Your normal key shows its own limit and any budgets on it.
  To see the whole account balance, add a management key as `OPENROUTER_MANAGEMENT_KEY`.
- **How often it checks:** about once a minute while something is running. When idle, at most every
  15 minutes, and not at all while no page is open on screen. A task paused for credits re-checks less
  and less often (up to every 15 minutes); **Resume** always works instantly.
- **Running low** (below `CONDUCTOR_CREDIT_LOW`, default $2): replies are capped at 16k tokens (OpenRouter
  reserves credit for the largest possible reply before each call, so big caps stall small balances), and a one-time warning on the task, and the
  router leans toward cheaper models. Models can call `check_credits` to plan around the budget.
- **OpenRouter's three kinds of "can't spend" (HTTP 402) are each handled:**
  - *In-flight budget full* (lots of parallel requests on a small balance): waits for `Retry-After`
    and retries.
  - *Request too big for what's left*: retries with a smaller output budget.
  - *Actually out* (account, key limit, or a spending budget): the task **pauses at that exact step**.
    Paused specialists show on the main task. Conductor re-checks every minute and resumes on its
    own once credits are back, or right away when you press **Resume**. If it can't see your
    balance, it retries every couple of minutes instead; that's free, since OpenRouter rejects
    unfunded requests before any model runs. Nothing is redone or lost.
    If you have your own provider keys on OpenRouter (BYOK, below), the task moves to a model they
    cover and keeps going instead of pausing.

## The management key (BYOK, guardrails, spend)

`OPENROUTER_MANAGEMENT_KEY` is optional. Create one at openrouter.ai/settings/management-keys.
Conductor only *reads* with it (it never creates, changes, or deletes anything), and uses what it sees:

- **Your own provider keys (BYOK).** Conductor lists the provider keys on your account (OpenAI,
  Anthropic, Google, ...) and checks which roster models those providers serve. Work on those models
  costs only OpenRouter's 5% BYOK fee in credits (waived up to a monthly allowance), and the rest
  is billed to your provider account. So:
  - `CONDUCTOR_BYOK=auto` (default): routing is normal while credits are fine. When they run **low**,
    BYOK models count at their credit cost, so the router leans on them. When they run **out**, a
    task moves to the best BYOK-covered model and keeps working instead of pausing. Those requests
    are pinned to your providers, and OpenRouter's hosted web search is switched off until credits
    are back, since it would spend credits.
  - `prefer`: favour BYOK models all the time. `off`: ignore them.
  - Disabled keys, and keys restricted to *other* OpenRouter API keys or to other models, are
    respected.
  - BYOK-covered models you haven't added to the roster are suggested on the Models page.
- **Conductor's own key.** It finds which of your API keys it's running on, and shows that key's
  spend this month (credits and BYOK) and its limit.
- **Guardrails.** If a guardrail applies to that key, models it would reject (blocked models, or only
  served by blocked providers) are dropped from routing, escalation, and hand-downs, so nothing is
  picked only to fail.
- **30-day activity.** Spend, BYOK spend, requests, and tokens per model, on the Models page.
- **Account balance** in the sidebar, as before.

All of this shows on the Models page under **Your OpenRouter account** (with a Refresh button), in
`conductor doctor`, and to the models through `check_credits`. It's cached in `data/account.json` and
refreshed every 30 minutes. Which providers serve each model is read with your normal key, and
re-checked every 6 hours.

## Claude Code fallback (your Claude subscription)

If the [Claude Code](https://code.claude.com) CLI is installed and signed in with your Claude account,
Conductor can hand work to it, and that runs on your subscription instead of OpenRouter credits.

- **When** (`CONDUCTOR_CLAUDE_CLI=fallback`, the default): a Claude specialist moves to Claude Code as soon
  as credits are **low** (before spending more), and any Claude task moves when OpenRouter can't run it
  (credits out, a step too big for the balance, "too many requests in flight", the provider failing),
  instead of pausing. Other models try your BYOK keys or a cheaper model first, and use Claude Code as the last
  step before pausing. A composer that runs out of credits hands the rest of the request to Claude Code,
  which finishes it alone, without hiring other models.
- **`prefer`**: Claude specialists run on Claude Code from the start, to save credits. **`off`**: never.
- **Continuity**: Claude Code gets the brief plus the work done so far, and follow-up messages resume the
  same Claude Code session.
- **Safety**: every tool call it makes (shell, edits, reads) goes through Conductor's own safety levels
  via a hook, and asks you in the app when it's above your auto-approve level, just like Conductor's
  own tools. (In `conductor chat`, where there's no web server to ask, it gets file tools only and no shell.)
- **Billing**: Conductor removes `ANTHROPIC_API_KEY` from Claude Code's environment and doesn't use
  `--bare`, so it runs on your subscription login, not an API account. Your plan's usage limits apply:
  if you hit one, Conductor stops using Claude Code for 30 minutes and falls back to pausing.
- If it can't take over (the CLI isn't found, a usage limit, an error), the task says why before pausing.
  Conductor looks for `claude` on PATH and in the usual install spots (npm, nvm, `~/.local/bin`), so it
  works from a background service too; `CONDUCTOR_CLAUDE_PATH` overrides.
- `conductor doctor` shows whether it found the CLI. It's the CLI's documented headless mode
  (`claude -p`) running on your own machine for your own work.

## Saving tokens with Headroom

If [Headroom](https://docs.headroomlabs.ai) (`headroom-ai`) is installed in Conductor's environment
(`pip install -e ".[headroom]"`), Conductor uses it to shrink bulky tool output before any model sees it:

- **What:** shell output, process logs, page text from the browser, and file listings over about 2,000
  characters. Logs keep their errors and stack traces; JSON keeps its structure. In testing, an 800-line
  log went from ~14k tokens to under 100 with the error line intact.
- **Not:** file contents read for editing (the model needs those exact), errors, or small outputs.
- **Nothing is lost:** the full original is saved under `.conductor/full/` in the workspace, and the
  model is told where, so it can read it if it needs everything.
- **Cache-safe:** each result is compressed once, when it arrives, and the compressed version is what's
  stored and re-sent. Earlier turns never change, so the provider's prompt cache keeps hitting.
  (Re-compressing history on every call is what breaks caches; Conductor never does that.)
- **Claude Code fallback:** Claude Code is pointed at Headroom's proxy (port 8787). With
  `CONDUCTOR_HEADROOM_PROXY=auto` (default), `conductor serve` starts the proxy in the background (if
  `headroom-ai[proxy]` is installed and none is running), restarts it if it dies, and stops it on exit and
  before an update restart. It never stops a proxy you started yourself. `connect` uses one you run,
  `off` never uses it. If a run fails through the proxy before doing anything, Conductor goes direct.
  The proxy logs to `data/logs/headroom-proxy.log`; its first start downloads a small compression model.
- **Privacy:** Headroom's anonymous usage beacon is switched off unless you set `HEADROOM_BEACON`.
- Each compression shows in the audit log as `tool.compressed` with the tokens saved.
  `CONDUCTOR_HEADROOM=off` turns it off; `conductor doctor` shows whether it's active.

## Use it from your phone (or any device on your Wi-Fi)

```bash
conductor serve --lan --stay-awake
```

- `--lan` opens it to other devices on your network (same as `CONDUCTOR_HOST=0.0.0.0` in `.env`). The
  terminal prints the address, a passcode, and a **QR code**: scan it with your phone's camera and
  it opens Conductor already signed in. The same QR code and passcode are at the top of the
  **Safety** page on the computer.
- **Passcode:** other devices enter it once, then stay signed in for a year. The computer
  itself never needs it. It's kept in `data/access_token` (or set `CONDUCTOR_ACCESS_TOKEN`).
  **New passcode** on the Safety page signs every device out. Ten wrong tries lock that device out
  for 10 minutes, and requests sent from other websites are refused.
- **Add to home screen** in your phone's browser to open it like an app.
- `--stay-awake` (or `CONDUCTOR_STAY_AWAKE=on`) stops the computer from sleeping while Conductor runs
  (via `systemd-inhibit`). Closing a laptop lid can still suspend it, depending on your settings.
- If your phone can't connect, your firewall is probably blocking the port:
  `sudo ufw allow 8765/tcp` (or your distro's equivalent).
- It's plain HTTP, meant for your home network only. Don't forward the port to the internet.
  For access away from home, use a VPN such as Tailscale or WireGuard.
- Sign-ins, wrong passcodes, and blocked requests all show in the audit log, with the device's
  address.

To have it start on its own at login, a systemd user service works:

```ini
# ~/.config/systemd/user/conductor.service
[Unit]
Description=AI Conductor
[Service]
WorkingDirectory=%h/Documents/AI-Conductor
ExecStart=%h/Documents/AI-Conductor/.venv/bin/conductor serve --lan --stay-awake
Restart=on-failure
[Install]
WantedBy=default.target
```

Then run `systemctl --user enable --now conductor`. Use `journalctl --user -u conductor` to see its
output, including the passcode.

## Updating

The sidebar has an **update** button (on your phone too). The first tap checks git for new commits on the
branch you're on. The second tap runs `git pull --ff-only`, reinstalls if `pyproject.toml` changed, and
restarts Conductor with the same command line. The page reloads when it's back. If tasks are running, it
warns you first; they stop, and you can send them a message afterwards to continue. If you've edited
files locally in a way that conflicts, the pull stops and nothing changes. Updates show in the audit log.
From the terminal it's just `git pull` (plus `pip install -e .` if dependencies changed), then restart.

## Audit log

Everything Conductor does is written locally, one JSON object per line, to
`data/logs/audit-YYYY-MM-DD.jsonl` (a new file each day; nothing leaves your machine):

- **You:** messages you send, buttons/links you click, pages you open, dropdowns and toggles you change
  (`ui.click`, `ui.view`, `ui.select`, `ui.toggle`), and every API request the page makes (`http.request`
  with method, path, status, and time taken).
- **The models:** which model was picked and why (`route.decided`), every model call with tokens, cost,
  and duration (`model.call`), escalations and hand-downs, replies, plans, sub-agent parts.
- **Actions:** every tool call and its result with timing (`tool.call` / `tool.result`), files changed,
  browser screenshots, approvals (auto or yours), credit pauses/resumes, errors, server start/stop.

Each entry has a millisecond timestamp, who did it (`user` / `model` / `system`), and the task id. Content
is kept to a short preview: what you typed into a prompt is logged as its length (`"<412 chars>"`), long
text is trimmed to 300 characters, and anything that looks like an API key or token is redacted. The
page's routine background polling isn't logged, so the file stays readable.

It's also on the web, under **Audit log** in the sidebar (works on your phone too). You can search,
filter by kind (clicks, requests, model calls, tools, approvals, sign-ins...) and time range, and tap a
task id to see just that task. Tap an entry to see all its fields, and download each day's full file.
It updates live. **Copy** copies the entries shown as raw JSON lines, and **Raw** shows them as text you
can select. Both work from a phone over plain HTTP.

```bash
conductor audit                  # last 50 entries
conductor audit -n 200 --kind tool        # just tool calls/results
conductor audit --task t_7f9d --kind ui   # what you clicked in one task
conductor audit --json | jq .    # raw lines
```

Turn it off with `CONDUCTOR_AUDIT=off`. Old files aren't deleted automatically; remove them whenever.

## Troubleshooting

`conductor doctor` checks your keys (and whether OpenRouter accepts them), the model catalog, the
benchmark feed, and the browser, and prints the actual errors. The Models page also says
whether benchmark data loaded. Without it, every model starts out rated the same.

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
