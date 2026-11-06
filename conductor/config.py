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
    subagent_budget: str = "frugal"
    explore: float = 0.1             # chance the router tries a less-proven model
    anthropic_key: bool = False
    openrouter_key: str = ""
    effort: str = "high"
    subagent_effort: str = "medium"
    reflection_effort: str = "low"
    max_tokens: int = 64000
    max_steps: int = 60
    approval: str = "ask"  # "ask" | "auto"
    data_dir: Path = field(default_factory=lambda: Path("data"))
    workspace: Path = field(default_factory=lambda: Path("workspace"))
    learning: bool = True
    web_tools: bool = True
    browser: bool = True
    fetch_catalog: bool = True

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
            explore=float(os.environ.get("CONDUCTOR_EXPLORE", cls.explore)),
            anthropic_key=bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            openrouter_key=os.environ.get("OPENROUTER_API_KEY", ""),
            effort=os.environ.get("CONDUCTOR_EFFORT", cls.effort),
            approval=os.environ.get("CONDUCTOR_APPROVAL", cls.approval).lower(),
            data_dir=Path(os.environ.get("CONDUCTOR_DATA_DIR", "data")),
            workspace=Path(os.environ.get("CONDUCTOR_WORKSPACE", "workspace")),
            learning=_flag("CONDUCTOR_LEARNING", True),
            web_tools=_flag("CONDUCTOR_WEB_TOOLS", True),
            browser=_flag("CONDUCTOR_BROWSER", True),
        )
        s.data_dir = s.data_dir.resolve()
        s.workspace = s.workspace.resolve()
        return s
