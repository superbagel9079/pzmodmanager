"""Log file setup.

Everything the tool does is written to a log file: the paths it probed, the mods
it found, the rules it ran, and every error it swallowed to keep going. When a
scan returns something surprising, the log is where the answer is.

Nothing is written to the console by the logging system: progress on screen goes
through the pipeline's progress callback instead, so the two never fight over the
terminal.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOGGER_NAME = "pzmodmanager"
DEFAULT_LOG_NAME = "pzmodmanager.log"

_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)-22s  %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def default_log_path() -> Path:
    """Where the log goes when the user does not say.

    Next to the user's other data rather than in the current directory, so
    running the tool from a read-only or unexpected working directory still
    works, and so the log follows the data folder when that is moved.
    """
    try:
        from .store import state_dir

        return state_dir() / DEFAULT_LOG_NAME
    except Exception:
        pass
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "pzmodmanager" / DEFAULT_LOG_NAME
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "pzmodmanager" / DEFAULT_LOG_NAME
    else:
        base = os.environ.get("XDG_STATE_HOME")
        if base:
            return Path(base) / "pzmodmanager" / DEFAULT_LOG_NAME
        return Path.home() / ".local" / "state" / "pzmodmanager" / DEFAULT_LOG_NAME
    return Path.cwd() / DEFAULT_LOG_NAME


def setup_logging(path: Path | None = None, level: str = "info") -> Path | None:
    """Attach a file handler to the pzmodmanager logger. Returns the log path.

    Returns None when the log file could not be opened; that is never fatal, the
    scan just runs without a log.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    target = Path(path) if path else default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, mode="w", encoding="utf-8")
    except OSError:
        # A log we cannot write is not a reason to refuse to run.
        logger.addHandler(logging.NullHandler())
        return None

    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    logger.info("Log started at %s", target)
    return target


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{module}")
