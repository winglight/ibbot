"""IB gateway service exposing a shared Interactive Brokers client."""

from __future__ import annotations

__all__ = ["run_service", "main"]

from .main import run_service, main
