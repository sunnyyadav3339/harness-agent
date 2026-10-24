"""Per-run live objects that cannot (and should not) go into the checkpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from PIL import Image

if TYPE_CHECKING:
    from ..channels.base import Channel
    from ..config import Settings
    from ..credentials import SecretStore
    from ..llm.router import Router
    from ..perception.browser_view import BrowserSnapshot
    from ..perception.desktop_view import DesktopSnapshot
    from ..policy import Policy
    from ..runlog.logger import RunLogger
    from ..skills.loader import Skill
    from ..tools.browser import BrowserManager


class AgentState(TypedDict, total=False):
    """Checkpointed after every node. Kept small: no images, no live handles."""

    run_id: str
    task: str
    skill_name: str | None
    trigger: str
    step: int
    max_steps: int
    phase: str
    surface: str  # "browser" | "desktop"
    plan: dict | None
    llm: dict | None
    history: list[dict]
    notes: list[str]
    status: str  # running | done | failed | cancelled
    summary: str
    pending_handoff: dict | None
    approvals: list[str]
    failures: int
    error_ledger: list[str]
    need_vision: bool
    handoffs: int
    recoveries: int  # self-recovery rounds used (LangGraph drops keys not declared here)


@dataclass
class RunContext:
    settings: "Settings"
    run: "RunLogger"
    browser: "BrowserManager"
    router: "Router"
    policy: "Policy"
    secrets: "SecretStore"
    skill: "Skill | None" = None
    vault: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    extra_skills: list = field(default_factory=list)
    other_skills: list[tuple[str, str]] = field(default_factory=list)
    channel: "Channel | None" = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    # latest observation (runtime only)
    browser_snapshot: "BrowserSnapshot | None" = None
    desktop_snapshot: "DesktopSnapshot | None" = None
    desktop_scale: float = 1.0
    browser_scale: float = 1.0  # model-image px per CSS px
    observation_text: str = ""
    model_image: Image.Image | None = None
    screen_image: Image.Image | None = None
    observe_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


# run_id -> RunContext; graph nodes look their context up here.
CONTEXTS: dict[str, RunContext] = {}
