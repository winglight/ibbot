from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Callable


_MODULE_PATH = Path(__file__).resolve().parent


def _load_native() -> (
    tuple[Callable[[Any], bytes], Callable[[Any], Any], Any] | None
):
    search_paths = [
        str(path)
        for path in map(Path, sys.path)
        if path.resolve() != _MODULE_PATH
    ]
    spec = importlib.machinery.PathFinder.find_spec("orjson", search_paths)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dumps_fn = getattr(module, "dumps", None)
    loads_fn = getattr(module, "loads", None)
    if callable(dumps_fn) and callable(loads_fn):
        return dumps_fn, loads_fn, module
    return None


_native = _load_native()

if _native:
    dumps, loads, _native_module = _native
    OPT_INDENT_2 = getattr(_native_module, "OPT_INDENT_2", 2)
else:

    def dumps(obj: Any, *, option: int | None = None) -> bytes:  # pragma: no cover - compatibility signature
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def loads(data: bytes | bytearray | memoryview | str) -> Any:  # pragma: no cover - compatibility signature
        if isinstance(data, (bytes, bytearray, memoryview)):
            return json.loads(bytes(data).decode("utf-8"))
        return json.loads(data)

    OPT_INDENT_2 = 2


__all__ = ["dumps", "loads", "OPT_INDENT_2"]
