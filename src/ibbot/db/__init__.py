"""Deprecated compatibility stub for :mod:`src.ibbot.db`."""

from __future__ import annotations

import warnings

warnings.warn(
    "src.ibbot.db has been removed. Import from src.db instead.",
    DeprecationWarning,
    stacklevel=2,
)

raise ImportError("src.ibbot.db is no longer available; use 'src.db' or 'src.db.config'.")
