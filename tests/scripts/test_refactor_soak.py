from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson
import pytest

from scripts import refactor_soak


@pytest.mark.asyncio
async def test_run_soak_collects_statistics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    channels = ["ib:account:updates", "ib:orders:status"]
    payloads = [
        {
            "messages": [
                {"channel": "ib:account:updates", "data": {"value": "1"}},
                {"channel": "ib:orders:status", "data": {"status": "Filled"}},
            ],
            "endpoint_results": [{"url": "http://service/subscriptions"}],
        },
        {
            "messages": [
                {"channel": "ib:account:updates", "data": {"value": "2"}},
                {"channel": "ib:account:updates", "data": {"value": "3"}},
                {"channel": "ib:orders:status", "data": {"status": "Submitted"}},
            ],
            "endpoint_results": [{"url": "http://service/subscriptions"}],
        },
    ]

    async def _fake_run(duration: float, output: Path, capture_channels: list[str]) -> dict[str, Any]:
        payload = payloads[len(list((output.parent).glob("iteration_*.json")))]
        output.write_bytes(orjson.dumps(payload))
        return payload

    monkeypatch.setattr(refactor_soak.integration_check, "_run", _fake_run)

    summary, failures = await refactor_soak._run_soak(
        iterations=2,
        duration=0.1,
        channels=channels,
        output_dir=tmp_path,
        min_messages=2,
        allow_missing=False,
    )

    assert not failures
    assert summary["iterations"] == 2
    assert summary["statistics"]["overall"]["min"] == 2

    summary_path = tmp_path / "summary.json"
    assert summary_path.exists()

    stored = orjson.loads(summary_path.read_bytes())
    assert stored["results"][0]["message_count"] == 2
    assert stored["results"][1]["per_channel"]["ib:account:updates"] == 2


@pytest.mark.asyncio
async def test_run_soak_reports_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    channels = ["ib:market:ticks", "ib:orders:status"]

    async def _fake_run(duration: float, output: Path, capture_channels: list[str]) -> dict[str, Any]:
        payload = {
            "messages": [{"channel": "ib:market:ticks", "data": {"value": "1"}}],
            "endpoint_results": [],
        }
        output.write_bytes(orjson.dumps(payload))
        return payload

    monkeypatch.setattr(refactor_soak.integration_check, "_run", _fake_run)

    summary, failures = await refactor_soak._run_soak(
        iterations=1,
        duration=0.1,
        channels=channels,
        output_dir=tmp_path,
        min_messages=2,
        allow_missing=False,
    )

    assert failures
    assert any("missing messages" in failure for failure in failures)
    assert any("minimum expected" in failure for failure in failures)
    assert summary["failures"] == failures
