"""Logging utilities for ibbot.

Provides factory functions to create loggers with both console and file
handlers. The output format can be configured to either human-readable text or
JSON. Logs are written to :mod:`data/logs` with file names derived from the
logger's module name.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Tuple

__all__ = [
    "get_logger",
    "get_log_file",
    "read_log_segment",
]


# Determine the repository root by traversing up from this file. The root is
# three levels above: ``src/ibbot/logging/__init__.py`` -> ``src`` -> project
# root.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_LOG_DIR = _PROJECT_ROOT / "data" / "logs"


class JsonFormatter(logging.Formatter):
    """Format log records as JSON strings."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        # Build a structured representation of the log record.
        payload = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False)


def _safe_module_name(module_name: str) -> str:
    """Return a filesystem-safe module name."""

    return module_name.replace(os.sep, ".").replace("\\", ".")


def _ensure_log_dir(log_dir: Path | None = None) -> Path:
    directory = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _get_formatter(format_type: str | None) -> logging.Formatter:
    if format_type is None or format_type.lower() == "text":
        return logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    if format_type.lower() == "json":
        return JsonFormatter()

    raise ValueError(f"Unsupported log format: {format_type}")


def get_log_file(module_name: str, log_dir: Path | None = None) -> Path:
    """Return the expected log file path for ``module_name``."""

    directory = _ensure_log_dir(log_dir)
    safe_name = _safe_module_name(module_name)
    return directory / f"{safe_name}.log"


def get_logger(
    module_name: str,
    *,
    level: int = logging.INFO,
    format_type: str | None = "text",
    log_dir: Path | None = None,
) -> logging.Logger:
    """Return a logger configured with file and console handlers.

    Parameters
    ----------
    module_name:
        Name of the module requesting the logger. This also determines the
        target log file.
    level:
        Logging level to apply to the logger.
    format_type:
        Either ``"text"`` or ``"json"``. Defaults to human-readable text.
    log_dir:
        Optional override for the log directory, primarily intended for
        testing.
    """

    logger = logging.getLogger(module_name)
    logger.setLevel(level)
    logger.propagate = False

    formatter = _get_formatter(format_type)
    log_file = get_log_file(module_name, log_dir=log_dir)

    if not getattr(logger, "_ibbot_handlers_configured", False):
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        logger._ibbot_handlers_configured = True  # type: ignore[attr-defined]
        logger._ibbot_handlers = {file_handler, console_handler}  # type: ignore[attr-defined]
        logger._ibbot_log_dir = log_file.parent  # type: ignore[attr-defined]
    else:
        # Refresh existing handler formatters and, if the log directory changed,
        # recreate the file handler to point to the new location.
        handlers = getattr(logger, "_ibbot_handlers", set())
        for handler in list(handlers):
            if isinstance(handler, logging.FileHandler):
                if Path(handler.baseFilename) != log_file:
                    logger.removeHandler(handler)
                    handler.close()
                    new_handler = logging.FileHandler(log_file, encoding="utf-8")
                    new_handler.setFormatter(formatter)
                    logger.addHandler(new_handler)
                    handlers.remove(handler)
                    handlers.add(new_handler)
                else:
                    handler.setFormatter(formatter)
            else:
                handler.setFormatter(formatter)

        logger._ibbot_handlers = handlers  # type: ignore[attr-defined]

    return logger


def read_log_segment(
    module_name: str,
    *,
    offset: int = 0,
    size: int = 4096,
    log_dir: Path | None = None,
) -> Tuple[str, int]:
    """Read ``size`` characters starting at ``offset`` from the log file.

    Returns a tuple of the read text and the new offset.
    """

    log_file = get_log_file(module_name, log_dir=log_dir)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.touch(exist_ok=True)

    with log_file.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        data = handle.read(size)
        new_offset = handle.tell()

    return data, new_offset
