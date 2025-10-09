"""ibbot package initialisation."""
from __future__ import annotations

from .utils.loguru_compat import enable_percent_style_logging

# Enable compatibility with legacy ``%``-style logging calls as soon as the
# package is imported. This ensures any module importing ``loguru.logger`` can
# continue using the traditional formatting style without emitting literal
# placeholders in log output.
enable_percent_style_logging()

__all__ = ["enable_percent_style_logging"]

