"""Repositories for persisting risk configuration snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ibbot.logging import get_logger

from ..schemas import RiskConfigSnapshot, RiskRuleSnapshot

__all__ = ["RiskSnapshotRepository"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _normalise_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalised: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, datetime):
            normalised[str(key)] = value.isoformat()
        elif isinstance(value, Mapping):
            normalised[str(key)] = _normalise_payload(value)
        elif isinstance(value, (list, tuple, set)):
            normalised[str(key)] = [
                item.isoformat() if isinstance(item, datetime) else item for item in value
            ]
        else:
            normalised[str(key)] = value
    return normalised


class RiskSnapshotRepository:
    """Persist risk configuration and rule snapshots."""

    def __init__(self, session: AsyncSession, *, logger_name: str = "db.risk") -> None:
        self._session = session
        self._logger = get_logger(logger_name)

    async def save_config_snapshot(
        self,
        *,
        service: str,
        payload: Mapping[str, Any],
        rules_count: int,
        updated_at: str | datetime | None = None,
        captured_at: datetime | str | None = None,
    ) -> None:
        row = {
            "service": service,
            "payload": _normalise_payload(payload),
            "rules_count": int(rules_count),
            "updated_at_text": str(updated_at) if updated_at is not None else None,
            "captured_at": _parse_datetime(captured_at) or _utc_now(),
        }
        try:
            await self._session.execute(insert(RiskConfigSnapshot).values(**row))
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception("Failed to persist risk config snapshot")
            raise

    async def save_rule_snapshots(
        self,
        *,
        service: str,
        rules: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        updated_at: str | datetime | None = None,
        captured_at: datetime | str | None = None,
    ) -> None:
        timestamp = _parse_datetime(captured_at) or _utc_now()
        rows: list[dict[str, Any]] = []
        if isinstance(rules, Mapping):
            for symbol, payload in rules.items():
                rows.append(
                    {
                        "service": service,
                        "symbol": str(symbol),
                        "payload": _normalise_payload(payload),
                        "updated_at_text": str(updated_at) if updated_at is not None else None,
                        "captured_at": timestamp,
                    }
                )
        else:
            for entry in rules:
                symbol = entry.get("symbol")
                payload = entry.get("payload")
                if symbol is None or not isinstance(payload, Mapping):
                    raise ValueError("Rule entries must include 'symbol' and mapping payload")
                rows.append(
                    {
                        "service": service,
                        "symbol": str(symbol),
                        "payload": _normalise_payload(payload),
                        "updated_at_text": str(entry.get("updated_at", updated_at))
                        if entry.get("updated_at", updated_at) is not None
                        else None,
                        "captured_at": _parse_datetime(entry.get("captured_at")) or timestamp,
                    }
                )
        if not rows:
            return
        try:
            await self._session.execute(insert(RiskRuleSnapshot), rows)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception("Failed to persist risk rule snapshots")
            raise

    async def bulk_upsert_rule_snapshots(
        self,
        *,
        service: str,
        rules: Mapping[str, Mapping[str, Any]],
        updated_at: str | datetime | None = None,
        captured_at: datetime | str | None = None,
    ) -> None:
        if not rules:
            return

        timestamp = _parse_datetime(captured_at) or _utc_now()
        rows: list[dict[str, Any]] = []
        symbols: list[str] = []

        for symbol, payload in rules.items():
            symbol_str = str(symbol)
            if not symbol_str:
                continue
            entry_payload = dict(payload)
            entry_payload.pop("symbol", None)
            entry_updated = entry_payload.get("updated_at", updated_at)
            entry_captured = entry_payload.get("captured_at")
            rows.append(
                {
                    "service": service,
                    "symbol": symbol_str,
                    "payload": _normalise_payload(entry_payload),
                    "updated_at_text": str(entry_updated) if entry_updated is not None else None,
                    "captured_at": _parse_datetime(entry_captured) or timestamp,
                }
            )
            symbols.append(symbol_str)

        if not rows:
            return

        delete_stmt = delete(RiskRuleSnapshot).where(
            RiskRuleSnapshot.service == service,
            RiskRuleSnapshot.symbol.in_(symbols),
        )
        insert_stmt = insert(RiskRuleSnapshot).values(rows)

        try:
            await self._session.execute(delete_stmt)
            await self._session.execute(insert_stmt)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception("Failed to upsert risk rule snapshots")
            raise
