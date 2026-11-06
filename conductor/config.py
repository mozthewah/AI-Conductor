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
    model: str = "claude-opus-5-5"
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
