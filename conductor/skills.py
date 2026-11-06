"""Learned skills: reusable procedures the conductor writes down for itself.

Each skill is a plain Markdown file in data/skills/ with a small header, so you
can read, edit, or hand-write them too:

    ---
    name: weekly-competitor-digest
    description: Research 3-5 competitors and write a dated digest to the workspace
    uses: 4
    ---
    1. ...
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "skill"


@dataclass
class Skill:
    name: str
    description: str
    body: str
    uses: int = 0
    updated: float = 0.0

    def render(self) -> str:
        return (f"---\nname: {self.name}\ndescription: {self.description}\nuses: {self.uses}\n"
                f"updated: {int(self.updated or time.time())}\n---\n{self.body.strip()}\n")


def _parse(text: str, fallback_name: str) -> Skill:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        header, sep, body = rest.partition("\n---")
        if sep:
            for line in header.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
            body = body.lstrip("\n")
        else:
            body = text
    return Skill(
        name=meta.get("name", fallback_name),
        description=meta.get("description", ""),
        body=body,
        uses=int(meta.get("uses", "0") or 0),
        updated=float(meta.get("updated", "0") or 0),
    )


class SkillLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / f"{slugify(name)}.md"

    def list(self) -> list[Skill]:
        skills = [_parse(p.read_text(), p.stem) for p in sorted(self.root.glob("*.md"))]
        return sorted(skills, key=lambda s: (-s.uses, s.name))

    def get(self, name: str) -> Skill | None:
        p = self._path(name)
        return _parse(p.read_text(), p.stem) if p.exists() else None

    def save(self, name: str, description: str, body: str) -> Skill:
        existing = self.get(name)
        skill = Skill(name=slugify(name), description=description.strip().replace("\n", " "),
                      body=body, uses=existing.uses if existing else 0, updated=time.time())
        self._path(name).write_text(skill.render())
        return skill

    def mark_used(self, name: str) -> Skill | None:
        skill = self.get(name)
        if skill:
            skill.uses += 1
            self._path(name).write_text(skill.render())
        return skill

    def delete(self, name: str) -> bool:
        p = self._path(name)
        if p.exists():
            p.unlink()
            return True
        return False

    def index(self) -> str:
        skills = self.list()
        if not skills:
            return "(none yet - you will build this library as you work)"
        return "\n".join(f"- {s.name}: {s.description}" for s in skills)
