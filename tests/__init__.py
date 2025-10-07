"""Test helpers package.

This module also ensures the repository root is available on ``sys.path``
before any test modules are imported.  Pytest imports ``tests`` as a package
early during collection, at which point per-directory ``conftest`` fixtures
have not yet run.  Several test modules import the production ``services``
package, which lives at the repository root.  Without eagerly inserting the
root path, Python instead resolves to ``tests/services`` (which exists solely
for organising test helpers) and subsequently fails with
``ModuleNotFoundError: No module named 'services.*'`` during collection.

Keeping the path adjustment here guarantees the real production package wins
module resolution while preserving pytest's helper structure.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Ensure the real application packages (``services``, ``src`` etc.) are
# discoverable before any test modules execute imports during collection.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
