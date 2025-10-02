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
    "sqlite3": {
        "pattern": re.compile(r"(?m)^\s*import\s+sqlite3"),
        "allowed": {
            "scripts/migrate_risk_rule_type.py",
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

        if rel_path == "main.py" or rel_path.startswith("src/routes/"):
            for module in (
                "src.notification_manager",
                "src.queue_manager",
                "src.risk_manager",
                "src.strategy_manager",
                "src.strategy_risk",
                "src.telegram_notifier",
                "src.database",
            ):
                legacy_pattern = re.compile(
                    rf"(?m)^\s*(?:from\s+{module}\s+import|import\s+{module})"
                )
                for match in legacy_pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    snippet = match.group(0).strip()
                    violations.append(
                        f"{rel_path}:{line}: legacy-manager-import -> {snippet}"
                    )

    assert not violations, "Forbidden imports detected:\n" + "\n".join(violations)
