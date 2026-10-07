"""Logging configuration."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import Settings

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(settings: Settings, *, console_level: str | None = None) -> logging.Logger:
    """Configure console + rotating file logging and return the root logger."""

    level_name = (settings.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # A windowed (console=False) build has no stderr at all; adding a stream
    # handler for it would only produce silent handler errors.
    if sys.stderr is not None:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(getattr(logging, (console_level or level_name).upper(), level))
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(console)

    log_dir: Path = settings.resolve_path(settings.log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "downloader.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - filesystem dependent
        root.warning("无法创建日志目录 %s，仅输出到控制台", log_dir)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return root
