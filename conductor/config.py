"""Runtime settings, read from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def _flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "on", "true", "yes"}


@dataclass
class Settings:
    # "auto" lets the router pick per task; or pin a model id
    # ("claude-opus-5-5" direct, or an OpenRouter id like "openai/gpt-6.1-sol").
    model: str = "auto"
    subagent_model: str = "auto"
    router_model: str = ""          # cheap classifier; default picked from available keys
    router_budget: str = "balanced"  # frugal | balanced | best
    subagent_budget: str = "frugal"   # supporting parts (reading, lookups)
    part_budget: str = "best"         # core parts of a conducted request: quality first
    conductor_budget: str = "best"    # the model that splits, reviews, and integrates
    explore: float = 0.1             # chance the router tries a less-proven model
    compose: str = "auto"            # auto = compose anything beyond a quick question; always; off = only multi-part
    composer_hands: bool = False     # False: the composer can't run the shell/edit/browse itself - it must delegate
    max_depth: int = 2               # specialists can hand off sub-parts, up to this many levels
    check_every: int = 4             # supervise specialists every N steps (drift check by a cheap model)
    auto_escalate: bool = True       # move a task to a stronger model when it outgrows/struggles
    max_escalations: int = 2         # automatic upgrades per task (you can always upgrade by hand)
    expert_model: str = "auto"       # who consult_expert asks; auto = best available for the work
    anthropic_key: bool = False
    openrouter_key: str = ""
    openrouter_management_key: str = ""  # optional: lets Conductor see the account balance
    key_name: str = ""                    # name of OPENROUTER_API_KEY on openrouter.ai, if auto-matching fails
    credit_low: float = 2.0               # USD; below this, warn and lean on cheaper models
    claude_cli: str = "fallback"          # Claude Code on your subscription: fallback | prefer | off
    headroom: str = "auto"                # compress bulky tool output with Headroom: auto (if installed) | on | off
    headroom_proxy: str = "auto"          # Headroom's proxy for Claude Code: auto (run it) | connect (yours) | off | URL
    claude_cli_path: str = ""             # where the `claude` CLI is, if it isn't on PATH
    byok: str = "auto"                    # auto: lean on your own provider keys when credits run low/out;
                                          # prefer: also favour them while credits are fine; off: ignore
    effort: str = "high"
    subagent_effort: str = "medium"
    reflection_effort: str = "low"
    max_tokens: int = 64000
    max_steps: int = 60
    # Auto-approve actions up to this severity: safe | low | medium | high.
    # Anything above asks you; critical always asks. ("ask" = low, "auto" = high, for old configs.)
    approval: str = "low"
    unattended_approval: str = "low"  # scheduled runs: above this is declined (nobody to ask)
    data_dir: Path = field(default_factory=lambda: Path("data"))
    workspace: Path = field(default_factory=lambda: Path("workspace"))
    learning: bool = True
    web_tools: bool = True
    browser: bool = True
    fetch_catalog: bool = True
    port: int = 8765  # Conductor's own web server (tools must not take this port)
    audit: bool = True  # write data/logs/audit-YYYY-MM-DD.jsonl

    @property
    def default_router_model(self) -> str:
        if self.router_model:
            return self.router_model
        return "anthropic/claude-haiku-5.5" if self.openrouter_key else "claude-haiku-5-5"

    @property
    def fallback_model(self) -> str:
        """Used when routing is off or fails."""
        return "anthropic/claude-opus-5.5" if self.openrouter_key and not self.anthropic_key else "claude-opus-5-5"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "conductor.db"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(Path(".env"))
        s = cls(
            model=os.environ.get("CONDUCTOR_MODEL", cls.model),
            subagent_model=os.environ.get("CONDUCTOR_SUBAGENT_MODEL", cls.subagent_model),
            router_model=os.environ.get("CONDUCTOR_ROUTER_MODEL", ""),
            router_budget=os.environ.get("CONDUCTOR_BUDGET", cls.router_budget).lower(),
            subagent_budget=os.environ.get("CONDUCTOR_SUBAGENT_BUDGET", cls.subagent_budget).lower(),
            part_budget=os.environ.get("CONDUCTOR_PART_BUDGET", cls.part_budget).lower(),
            conductor_budget=os.environ.get("CONDUCTOR_LEAD_BUDGET", cls.conductor_budget).lower(),
            explore=float(os.environ.get("CONDUCTOR_EXPLORE", cls.explore)),
            auto_escalate=_flag("CONDUCTOR_AUTO_ESCALATE", True),
            compose=os.environ.get("CONDUCTOR_COMPOSE", cls.compose).lower(),
            composer_hands=_flag("CONDUCTOR_COMPOSER_HANDS", False),
            max_depth=int(os.environ.get("CONDUCTOR_MAX_DEPTH", cls.max_depth)),
            expert_model=os.environ.get("CONDUCTOR_EXPERT_MODEL", cls.expert_model),
            anthropic_key=bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            openrouter_key=os.environ.get("OPENROUTER_API_KEY", ""),
            openrouter_management_key=os.environ.get("OPENROUTER_MANAGEMENT_KEY", ""),
            key_name=os.environ.get("CONDUCTOR_KEY_NAME", ""),
            credit_low=float(os.environ.get("CONDUCTOR_CREDIT_LOW", cls.credit_low)),
            byok=os.environ.get("CONDUCTOR_BYOK", cls.byok).lower(),
            claude_cli=os.environ.get("CONDUCTOR_CLAUDE_CLI", cls.claude_cli).lower(),
            headroom=os.environ.get("CONDUCTOR_HEADROOM", cls.headroom).lower(),
            headroom_proxy=os.environ.get("CONDUCTOR_HEADROOM_PROXY", cls.headroom_proxy),
            claude_cli_path=os.environ.get("CONDUCTOR_CLAUDE_PATH", ""),
            effort=os.environ.get("CONDUCTOR_EFFORT", cls.effort),
            approval={"ask": "low", "auto": "high"}.get(
                os.environ.get("CONDUCTOR_APPROVAL", cls.approval).lower(),
                os.environ.get("CONDUCTOR_APPROVAL", cls.approval).lower()),
            unattended_approval=os.environ.get("CONDUCTOR_UNATTENDED_APPROVAL", cls.unattended_approval).lower(),
            data_dir=Path(os.environ.get("CONDUCTOR_DATA_DIR", "data")),
            workspace=Path(os.environ.get("CONDUCTOR_WORKSPACE", "workspace")),
            learning=_flag("CONDUCTOR_LEARNING", True),
            web_tools=_flag("CONDUCTOR_WEB_TOOLS", True),
            browser=_flag("CONDUCTOR_BROWSER", True),
            port=int(os.environ.get("CONDUCTOR_PORT", "8765")),
            audit=_flag("CONDUCTOR_AUDIT", True),
        )
        s.data_dir = s.data_dir.resolve()
        s.workspace = s.workspace.resolve()
        return s
