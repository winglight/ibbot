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

    env_local = PROJECT_ROOT / ".env.local"
    env_file = PROJECT_ROOT / ".env"
    template_files: Sequence[Path] = (
        PROJECT_ROOT / ".env.example",
        PROJECT_ROOT / "config" / "defaults.env",
    )

    env_files: list[tuple[Path, bool]] = []

    if env_local.exists():
        env_files.append((env_local, True))

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

    env_files.append((base_file, True))

    for file_path, override in env_files:
        if file_path:
            load_dotenv(dotenv_path=file_path, override=override)

    if extra_files:
        for file_path in extra_files:
            candidate = Path(file_path)
            if candidate.exists():
                load_dotenv(dotenv_path=candidate, override=True)
