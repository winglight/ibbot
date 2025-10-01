#!/usr/bin/env python3
"""Convenience wrapper to launch the account snapshot service."""

from services.account_snapshot.main import main


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
