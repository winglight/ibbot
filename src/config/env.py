"""Helpers for loading project level environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from dotenv import load_dotenv, dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _apply_overrides(
    file_path: Path,
    original_env: Mapping[str, str],
) -> None:
    """Apply overrides from ``file_path`` while respecting existing env values.

    Environment variables that were present before :func:`load_project_env` was
    called must not be clobbered by values from dotenv files.  This helper
    updates :mod:`os.environ` with the contents of the provided dotenv file but
    skips keys that already existed in ``original_env``.
    """

    values = dotenv_values(file_path)
    for key, value in values.items():
        if value is None:
            continue
        if key in original_env:
            continue
        os.environ[key] = value


def load_project_env(extra_files: Iterable[Path | str] | None = None) -> None:
    """Public wrapper that normalises *extra_files* for caching."""

    normalized: tuple[Path | str, ...] | None = None
    if extra_files is not None:
        normalized = tuple(extra_files)

    _load_project_env(normalized)


@lru_cache(maxsize=1)
def _load_project_env(extra_files: tuple[Path | str, ...] | None = None) -> None:
    """Load `.env` defaults and optional overrides once per process.

    Parameters
    ----------
    extra_files:
        Optional iterable of extra dotenv files. They are applied after the
        canonical files and therefore override previous values.
    """

    env_local = PROJECT_ROOT / ".env.local"
    env_file = PROJECT_ROOT / ".env"
    template_files: Sequence[Path] = (
        PROJECT_ROOT / ".env.example",
        PROJECT_ROOT / "config" / "defaults.env",
    )

    base_file: Path | None = None
    if env_file.exists():
        base_file = env_file
    else:
        for template in template_files:
            if template.exists():
                base_file = template
                break

        if base_file is None:
            template_hint = " or ".join(f"`{path.name}`" for path in template_files)
            raise FileNotFoundError(
                "Missing `.env` file. Copy "
                f"{template_hint or 'the provided template'} to `.env` or `.env.local` "
                "and retry."
            )

    original_env = dict(os.environ)

    if base_file:
        load_dotenv(dotenv_path=base_file, override=False)

    if env_local.exists():
        _apply_overrides(env_local, original_env)

    if extra_files:
        for file_path in extra_files:
            candidate = Path(file_path)
            if candidate.exists():
                _apply_overrides(candidate, original_env)
