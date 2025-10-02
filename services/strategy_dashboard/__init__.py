"""Strategy dashboard service package."""

from .app import create_app, runtime_dependency

__all__ = ["create_app", "runtime_dependency"]
