"""Helpers for loading project level environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load_project_env(extra_files: Iterable[Path | str] | None = None) -> None:
    """Load `.env` defaults and optional overrides once per process.

    Parameters
    ----------
    extra_files:
        Optional iterable of extra dotenv files. They are applied after the
        canonical files and therefore override previous values.
    """

    env_files: Sequence[tuple[Path, bool]] = (
        (PROJECT_ROOT / ".env", False),
        (PROJECT_ROOT / ".env.local", True),
    )

    for file_path, override in env_files:
        if file_path.exists():
            load_dotenv(dotenv_path=file_path, override=override)

    if extra_files:
        for file_path in extra_files:
            candidate = Path(file_path)
            if candidate.exists():
                load_dotenv(dotenv_path=candidate, override=True)
