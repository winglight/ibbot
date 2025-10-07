"""Provide a helper namespace for service-related tests.

This package intentionally shadows the production ``services`` module so pytest
can import helper utilities via ``tests.services``.  When pytest prepends the
``tests`` directory to ``sys.path`` during collection, the shadow would normally
prevent imports such as ``from services.risk import RiskRule`` from resolving to
the real application code.  To avoid duplicate helper hierarchies, we proxy the
package to the actual implementation while keeping the helper namespace
available.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[2]
_REAL_PACKAGE = _ROOT / "services"

_spec = importlib.util.spec_from_file_location(
    "services",
    _REAL_PACKAGE / "__init__.py",
    submodule_search_locations=[str(_REAL_PACKAGE)],
)

if _spec is None or _spec.loader is None:
    raise ImportError("Unable to load ibbot services package for tests")

_real_services = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_services)

# Make sure ``import services`` returns the real implementation even when
# ``tests`` is earlier on ``sys.path``.
sys.modules.setdefault("services", _real_services)


class _ProxyModule(ModuleType):
    """Delegate missing attributes to the real services package."""

    def __getattr__(self, item: str):  # type: ignore[override]
        try:
            return super().__getattribute__(item)
        except AttributeError:
            return getattr(_real_services, item)


_proxy = _ProxyModule(__name__)
_proxy.__doc__ = __doc__
_proxy.__file__ = __file__
_proxy.__package__ = __name__
_proxy.__path__ = [str(Path(__file__).resolve().parent), str(_REAL_PACKAGE)]  # type: ignore[attr-defined]

sys.modules[__name__] = _proxy
