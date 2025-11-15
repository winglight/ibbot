"""Optimizer service package exposed for router imports."""

from .routes import router
from .plan_manager import plan_manager

__all__ = ["router", "plan_manager"]
