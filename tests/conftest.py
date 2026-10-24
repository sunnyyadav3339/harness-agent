import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated workspace so tests never touch the real skills/runs."""
    from harness import config

    monkeypatch.setenv("HARNESS_WORKSPACE", str(tmp_path / "ws"))
    config.get_settings.cache_clear()
    settings = config.get_settings()
    settings.ensure_dirs()
    yield settings
    config.get_settings.cache_clear()
