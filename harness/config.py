"""Process-level settings.

Two kinds of configuration exist on purpose:

* Settings here (env / .env): secrets and machine facts. Part of the frozen code.
* YAML under ``<workspace>/config``: models, triggers, policy. These live in the
  workspace (the Drive folder) so they stay editable after the code freeze,
  e.g. to swap a model that starts failing at 10:59.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local"))


def split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    harness_workspace: Path = REPO_ROOT / "workspace"
    # Machine-local state (checkpoints, IMAP position, heartbeat). Override for tests.
    harness_state_dir: Path | None = None

    gemini_api_keys: str = ""
    groq_api_keys: str = ""

    telegram_bot_token: str = ""
    telegram_chat_ids: str = ""

    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_oauth_token: str = ""

    chrome_path: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    chrome_profile_dir: Path = _LOCAL / "agenticps" / "chrome-profile"
    cdp_port: int = 9222
    # True for real runs (the recording must show the agent's browser). `run --background`
    # turns it off for development so the agent's Chrome never jumps over your windows.
    browser_foreground: bool = True

    # Local web console served by the daemon (127.0.0.1 only); 0 turns it off.
    web_port: int = 8765

    ntp_server: str = "time.google.com"
    log_level: str = "INFO"

    @property
    def workspace(self) -> Path:
        return self.harness_workspace.expanduser().resolve()

    @property
    def skills_dir(self) -> Path:
        return self.workspace / "skills"

    @property
    def data_dir(self) -> Path:
        return self.workspace / "data"

    @property
    def runs_dir(self) -> Path:
        return self.workspace / "runs"

    @property
    def config_dir(self) -> Path:
        return self.workspace / "config"

    @property
    def state_dir(self) -> Path:
        # Checkpoints are machine-local: SQLite must not sit in a synced folder.
        return self.harness_state_dir or (_LOCAL / "agenticps" / "state")

    def checkpoint_db(self, run_id: str) -> Path:
        # One SQLite file per run: concurrent processes (daemon, CLI runs, tests) and a
        # force-killed run can never corrupt another run's checkpoints.
        return self.state_dir / "checkpoints" / f"{run_id}.sqlite"

    def provider_keys(self, env_name: str) -> list[str]:
        value = os.environ.get(env_name) or getattr(self, env_name.lower(), "") or ""
        return split_csv(value)

    def allowed_chat_ids(self) -> set[str]:
        return set(split_csv(self.telegram_chat_ids))

    def ensure_dirs(self) -> None:
        for d in (self.skills_dir, self.data_dir, self.runs_dir, self.config_dir, self.state_dir,
                  self.state_dir / "checkpoints"):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_workspace_yaml(name: str, default: Any = None) -> Any:
    """Read ``<workspace>/config/<name>`` fresh every call (never cached)."""
    path = get_settings().config_dir / name
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return default if data is None else data
