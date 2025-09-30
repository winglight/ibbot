#!/usr/bin/env python3
"""Backward compatible entry point for the optimizer service."""

from services.optimizer.main import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    main()
