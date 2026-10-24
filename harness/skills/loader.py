"""SKILL.md loader and router.

A skill is ``<workspace>/skills/<name>/SKILL.md``: YAML frontmatter + Markdown
SOP. Files are read fresh on every run (never cached at boot), so edits in the
Drive folder after the code freeze take effect immediately.

Frontmatter keys (all optional except name/description):
  name, description, triggers: [phrases], tools: [tool prefixes],
  vision: auto|always|never, data: [workspace-relative yaml files],
  busy_selectors: [css], max_steps: int, draft: bool
Fast-path macros live in a fenced ```yaml macros block in the body or in a
sibling macros.yaml.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
_MACRO_BLOCK_RE = re.compile(r"```ya?ml\s+macros\s*\n(.*?)```", re.S)
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "from", "with", "my", "me", "it", "is", "this",
         "that", "at", "by", "be", "do", "please", "can", "you", "i", "your", "then", "into", "as"}


class SkillError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    triggers: list[str] = field(default_factory=list)
    tools: list[str] | None = None
    vision: str = "auto"
    data: list[str] = field(default_factory=list)
    busy_selectors: list[str] = field(default_factory=list)
    max_steps: int | None = None
    draft: bool = False
    macros: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return self.path.parent


def parse_skill_text(text: str, path: Path) -> Skill:
    m = _FM_RE.match(text.lstrip("﻿"))
    if not m:
        raise SkillError(f"{path}: missing YAML frontmatter")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}: bad frontmatter: {exc}") from exc
    if not isinstance(meta, dict) or not meta.get("name") or not meta.get("description"):
        raise SkillError(f"{path}: frontmatter needs name and description")
    body = m.group(2)
    macros: dict = {}
    for block in _MACRO_BLOCK_RE.findall(body):
        try:
            macros.update(yaml.safe_load(block) or {})
        except yaml.YAMLError as exc:
            raise SkillError(f"{path}: bad macros block: {exc}") from exc
    sibling = path.parent / "macros.yaml"
    if sibling.exists():
        macros.update(yaml.safe_load(sibling.read_text(encoding="utf-8")) or {})
    return Skill(
        name=str(meta["name"]),
        description=str(meta["description"]).strip(),
        path=path,
        body=body.strip(),
        triggers=[str(t).lower() for t in meta.get("triggers", []) or []],
        tools=meta.get("tools"),
        vision=str(meta.get("vision", "auto")),
        data=list(meta.get("data", []) or []),
        busy_selectors=list(meta.get("busy_selectors", []) or []),
        max_steps=meta.get("max_steps"),
        draft=bool(meta.get("draft", False)),
        macros=macros,
        meta=meta,
    )


def load_skill(path: Path) -> Skill:
    return parse_skill_text(path.read_text(encoding="utf-8"), path)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 1}


class SkillLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root

    def all(self) -> list[Skill]:
        skills = []
        if not self.root.exists():
            return skills
        for path in sorted(self.root.glob("*/SKILL.md")):
            try:
                skills.append(load_skill(path))
            except Exception as exc:  # one broken skill must not take down the others
                log.error("skipping skill %s: %s", path, exc)
        return skills

    def get(self, name: str) -> Skill | None:
        for s in self.all():
            if s.name == name or s.dir.name == name:
                return s
        return None

    def match(self, task: str) -> tuple[Skill | None, list[tuple[float, str]]]:
        """Deterministic scoring: explicit name > trigger phrases > description overlap."""
        text = task.lower()
        words = _tokens(task)
        scored: list[tuple[float, Skill]] = []
        for s in self.all():
            score = 0.0
            if re.search(rf"\b{re.escape(s.name.lower())}\b", text) or f"skill:{s.dir.name}" in text:
                score += 100
            score += sum(3.0 for t in s.triggers if not t.startswith("cron:") and t in text)
            desc = _tokens(s.description + " " + s.name.replace("_", " "))
            if desc:
                score += 4.0 * len(words & desc) / len(desc | words) if words else 0
            if s.draft:
                score *= 0.8
            scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        ranking = [(round(sc, 2), s.name) for sc, s in scored]
        if scored and scored[0][0] >= 3.0:
            return scored[0][1], ranking
        return None, ranking


_DATE_EXPR_RE = re.compile(r"\$\{date([+-]\d+)?(?::([^}]+))?\}")


def _expand_dynamic(value):
    """``${date+1:%d/%m/%Y}`` -> tomorrow (IST, NTP-corrected) in that format."""
    if isinstance(value, str) and "${date" in value:
        from datetime import timedelta

        from ..timeutil import CLOCK

        def _sub(m: re.Match) -> str:
            day = CLOCK.now() + timedelta(days=int(m.group(1) or 0))
            return day.strftime(m.group(2) or "%Y-%m-%d")

        return _DATE_EXPR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_dynamic(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_dynamic(v) for v in value]
    return value


def load_vault(workspace: Path, skill: Skill | None) -> dict:
    """Non-secret data the skill declared (e.g. data/passenger.yaml -> vault.passenger)."""
    vault: dict = {}
    files = list(skill.data) if skill else []
    common = workspace / "data" / "common.yaml"
    if common.exists():
        files.insert(0, "data/common.yaml")
    for rel in files:
        p = (workspace / rel).resolve()
        if not p.exists():
            log.warning("vault file %s not found", p)
            continue
        with p.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        vault[p.stem] = _expand_dynamic(data)
    return vault
