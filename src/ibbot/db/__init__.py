"""Compatibility shim that exposes the DB-API repositories from :mod:`src.db`."""

from __future__ import annotations

from src.db import *  # noqa: F401,F403 - re-export public API
from src.db import __all__ as _public

__all__ = list(_public)

del _public
