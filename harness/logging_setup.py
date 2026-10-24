from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

_LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "agenticps" / "logs"


def setup_logging(level: str = "INFO") -> Path:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / "harness.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    root.addHandler(rotating)

    for noisy in ("httpx", "httpcore", "apscheduler", "PIL", "asyncio", "comtypes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
