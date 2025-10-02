from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _python_files() -> list[Path]:
    skip_dirs = {"__pycache__", "node_modules", "build", "dist"}
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        files.append(path)
    return files


FORBIDDEN_RULES = {
    "core.connection_manager": {
        "pattern": re.compile(r"(?m)^\s*(?:from\s+core\s+import\s+connection_manager|import\s+core\.connection_manager)"),
        "allowed": set(),
    },
    "get_db_manager": {
        "pattern": re.compile(r"(?m)^\s*from\s+src\.database\s+import\s+get_db_manager"),
        "allowed": {
            "main.py",
            "services/risk/runtime.py",
            "src/dom_service.py",
            "src/ibkr_client.py",
            "src/order_manager.py",
            "src/optimizer_service/routes.py",
            "src/risk_manager.py",
            "src/routes/notifications.py",
            "src/strategy_manager.py",
        },
    },
    "sqlite3": {
        "pattern": re.compile(r"(?m)^\s*import\s+sqlite3"),
        "allowed": {
            "scripts/migrate_risk_rule_type.py",
            "src/database.py",
            "src/order_manager.py",
            "src/optimizer_service/db.py",
        },
    },
}


def test_forbidden_imports() -> None:
    violations: list[str] = []
    for path in _python_files():
        rel_path = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for rule_name, rule in FORBIDDEN_RULES.items():
            pattern = rule["pattern"]
            allowed = rule["allowed"]
            for match in pattern.finditer(text):
                if rel_path in allowed:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                snippet = match.group(0).strip()
                violations.append(f"{rel_path}:{line}: {rule_name} -> {snippet}")

    assert not violations, "Forbidden imports detected:\n" + "\n".join(violations)
