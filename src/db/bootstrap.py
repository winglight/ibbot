"""Database bootstrap helpers for ensuring required tables exist."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Mapping, Sequence

from .mariadb import MariaDBConfigurationError, connect
from .schema import REQUIRED_TABLES

__all__ = ["SchemaBootstrapError", "BootstrapResult", "ensure_schema"]


class SchemaBootstrapError(RuntimeError):
    """Raised when the database schema could not be created."""


@dataclass(frozen=True)
class BootstrapResult:
    """Result payload returned by :func:`ensure_schema`."""

    backend: str
    created_tables: tuple[str, ...]


_DEFAULT_SQL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sql" / "db.sql"

_TABLE_PATTERN = re.compile(
    r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?([A-Za-z0-9_]+)[`\"]?",
    re.IGNORECASE,
)
_INDEX_PATTERN = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?[A-Za-z0-9_]+[`\"]?\s+ON\s+[`\"]?([A-Za-z0-9_]+)[`\"]?",
    re.IGNORECASE,
)


def _get_logger(logger: logging.Logger | None) -> logging.Logger:
    if logger is not None:
        return logger
    return logging.getLogger("src.db.bootstrap")


def _load_statements(sql_path: Path) -> list[str]:
    try:
        text = sql_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem errors are environment specific
        raise SchemaBootstrapError(f"Unable to read SQL bootstrap script: {sql_path}") from exc

    statements: list[str] = []
    buffer: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
    if buffer:
        statement = "\n".join(buffer).strip()
        if statement:
            statements.append(statement)
    return statements


def _detect_backend(connection: object) -> str:
    module = getattr(connection.__class__, "__module__", "")
    if module.startswith("sqlite3"):
        return "sqlite"
    return "mysql"


def _introspect_tables(connection: object, backend: str) -> set[str]:
    cursor = connection.cursor()
    try:
        if backend == "sqlite":
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        else:
            cursor.execute("SHOW TABLES")
        rows = cursor.fetchall() or []
    finally:
        try:
            cursor.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass

    tables: set[str] = set()
    for row in rows:
        if isinstance(row, Mapping):
            for value in row.values():
                if value:
                    tables.add(str(value))
                    break
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
            if row:
                tables.add(str(row[0]))
        elif row:
            tables.add(str(row))
    return tables


def _statement_target(statement: str) -> str | None:
    text = statement.lstrip()
    match = _TABLE_PATTERN.match(text)
    if match:
        return match.group(1)
    match = _INDEX_PATTERN.match(text)
    if match:
        return match.group(1)
    return None


def _is_create_table(statement: str) -> bool:
    return bool(_TABLE_PATTERN.match(statement.lstrip()))


def _prepare_statement(statement: str, backend: str) -> str:
    if backend == "mysql":
        return statement.replace("AUTOINCREMENT", "AUTO_INCREMENT")
    return statement


def _execute_statement(connection: object, statement: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(statement)
        commit = getattr(connection, "commit", None)
        if callable(commit):
            commit()
    except Exception as exc:
        rollback = getattr(connection, "rollback", None)
        if callable(rollback):
            try:
                rollback()
            except Exception:  # pragma: no cover - defensive cleanup
                pass
        raise SchemaBootstrapError(f"Failed to execute SQL statement: {statement}") from exc
    finally:
        try:
            cursor.close()
        except Exception:  # pragma: no cover - defensive cleanup
            pass


def ensure_schema(
    *, sql_path: Path | str | None = None, logger: logging.Logger | None = None
) -> BootstrapResult:
    """Ensure that all required tables exist in the configured database."""

    sql_file = Path(sql_path) if sql_path is not None else _DEFAULT_SQL_PATH
    effective_logger = _get_logger(logger)

    statements = _load_statements(sql_file)
    if not statements:
        raise SchemaBootstrapError(f"No SQL statements found in {sql_file}")

    with connect() as connection:
        backend = _detect_backend(connection)
        existing_tables = _introspect_tables(connection, backend)
        missing_tables = sorted(REQUIRED_TABLES - existing_tables)
        if not missing_tables:
            effective_logger.debug("All required tables already exist; skipping bootstrap")
            return BootstrapResult(backend=backend, created_tables=tuple())

        missing_display = ", ".join(missing_tables)
        effective_logger.info(
            f"Bootstrapping database schema; missing tables: {missing_display}"
        )
        targets = set(missing_tables)
        created: list[str] = []

        for statement in statements:
            target = _statement_target(statement)
            if target is None or target not in targets:
                continue
            prepared = _prepare_statement(statement, backend)
            effective_logger.debug(f"Executing bootstrap statement for {target}")
            _execute_statement(connection, prepared)
            if _is_create_table(statement) and target not in created:
                created.append(target)

        refreshed_tables = _introspect_tables(connection, backend)

    remaining = sorted(REQUIRED_TABLES - refreshed_tables)
    if remaining:
        remaining_display = ", ".join(remaining)
        raise SchemaBootstrapError(
            "Database bootstrap incomplete; still missing tables: " + remaining_display
        )

    if created:
        effective_logger.info(f"Created missing tables: {', '.join(created)}")
    else:
        effective_logger.info("Bootstrap completed; no new tables were created")

    return BootstrapResult(backend=backend, created_tables=tuple(created))


def main() -> int:  # pragma: no cover - CLI utility
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("src.db.bootstrap")
    try:
        result = ensure_schema(logger=logger)
    except MariaDBConfigurationError as exc:
        logger.error(f"Database configuration error: {exc}")
        return 2
    except SchemaBootstrapError as exc:
        logger.error(f"Schema bootstrap failed: {exc}")
        return 1
    except Exception as exc:
        logger.exception(f"Unexpected error during schema bootstrap: {exc}")
        return 3

    if result.created_tables:
        logger.info(
            f"Schema bootstrap created tables: {', '.join(result.created_tables)}"
        )
    else:
        logger.info(
            f"Schema bootstrap confirmed existing schema for backend '{result.backend}'"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual execution path
    raise SystemExit(main())
