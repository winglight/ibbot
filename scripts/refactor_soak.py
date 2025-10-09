from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import statistics
import sys
from pathlib import Path
from typing import Iterable

import orjson

from scripts import refactor_integration_check as integration_check


DEFAULT_ITERATIONS = 5
DEFAULT_MIN_MESSAGES = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the refactor acceptance harness in a soak loop and collect per-channel metrics."
        )
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"Number of capture iterations to run (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=integration_check.DEFAULT_DURATION,
        help=(
            "Duration in seconds for each capture iteration. "
            f"Defaults to {integration_check.DEFAULT_DURATION}."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/regression/soak"),
        help="Directory where per-iteration captures and summary.json will be written.",
    )
    parser.add_argument(
        "--channels",
        nargs="*",
        default=list(integration_check.DEFAULT_CHANNELS),
        help=(
            "Optional list of Redis channels to subscribe to. "
            "Defaults to the standard refactor channel set."
        ),
    )
    parser.add_argument(
        "--min-messages",
        type=int,
        default=DEFAULT_MIN_MESSAGES,
        help=(
            "Minimum number of messages required per iteration. "
            "Failures are reported if this threshold is not met."
        ),
    )
    parser.add_argument(
        "--allow-missing-channels",
        action="store_true",
        help=(
            "If set, the soak runner will not fail iterations that miss messages "
            "for one or more subscribed channels."
        ),
    )
    return parser


async def _run_iteration(
    iteration: int,
    duration: float,
    output_dir: Path,
    channels: Iterable[str],
) -> dict[str, object]:
    capture_path = output_dir / f"iteration_{iteration:03d}.json"
    payload = await integration_check._run(duration, capture_path, channels)

    counts: dict[str, int] = {channel: 0 for channel in channels}
    for message in payload.get("messages", []):
        channel = message.get("channel")
        if channel in counts:
            counts[channel] += 1

    missing_channels = [channel for channel, count in counts.items() if count == 0]

    return {
        "iteration": iteration,
        "capture_path": str(capture_path),
        "message_count": len(payload.get("messages", [])),
        "per_channel": counts,
        "missing_channels": missing_channels,
        "endpoint_results": payload.get("endpoint_results", []),
    }


def _summarise_counts(channel_counts: dict[str, list[int]]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for channel, values in channel_counts.items():
        if values:
            summary[channel] = {
                "min": min(values),
                "max": max(values),
                "avg": statistics.mean(values),
            }
        else:
            summary[channel] = {"min": 0.0, "max": 0.0, "avg": 0.0}
    return summary


async def _run_soak(
    iterations: int,
    duration: float,
    channels: Iterable[str],
    output_dir: Path,
    min_messages: int,
    allow_missing: bool,
) -> tuple[dict[str, object], list[str]]:
    channel_list = list(channels)
    output_dir.mkdir(parents=True, exist_ok=True)

    per_iteration: list[dict[str, object]] = []
    failures: list[str] = []
    overall_counts: list[int] = []
    per_channel_values: dict[str, list[int]] = {channel: [] for channel in channel_list}

    for iteration in range(iterations):
        result = await _run_iteration(iteration, duration, output_dir, channel_list)
        per_iteration.append(result)
        overall_counts.append(result["message_count"])

        for channel, count in result["per_channel"].items():
            per_channel_values.setdefault(channel, []).append(count)

        if result["message_count"] < min_messages:
            failures.append(
                "Iteration {} produced {} messages (minimum expected: {}).".format(
                    iteration, result["message_count"], min_messages
                )
            )

        if not allow_missing and result["missing_channels"]:
            failures.append(
                "Iteration {} missing messages for channels: {}".format(
                    iteration, ", ".join(result["missing_channels"])
                )
            )

    overall_stats = {
        "min": min(overall_counts) if overall_counts else 0,
        "max": max(overall_counts) if overall_counts else 0,
        "avg": statistics.mean(overall_counts) if overall_counts else 0.0,
    }

    summary_payload: dict[str, object] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "iterations": iterations,
        "duration": duration,
        "min_messages": min_messages,
        "allow_missing_channels": allow_missing,
        "channels": channel_list,
        "results": per_iteration,
        "statistics": {
            "overall": overall_stats,
            "per_channel": _summarise_counts(per_channel_values),
        },
        "failures": failures,
    }

    summary_path = output_dir / "summary.json"
    summary_path.write_bytes(orjson.dumps(summary_payload, option=orjson.OPT_INDENT_2))

    return summary_payload, failures


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    channels = args.channels or list(integration_check.DEFAULT_CHANNELS)

    summary, failures = asyncio.run(
        _run_soak(
            iterations=args.iterations,
            duration=args.duration,
            channels=channels,
            output_dir=args.output_dir,
            min_messages=args.min_messages,
            allow_missing=args.allow_missing_channels,
        )
    )

    print(
        "Completed %s iterations (duration %.2fs each)." % (summary["iterations"], summary["duration"])
    )
    for result in summary["results"]:
        missing = result["missing_channels"]
        missing_repr = ", ".join(missing) if missing else "none"
        print(
            " - Iteration %(iteration)s captured %(message_count)s messages (missing: %(missing)s)"%
            {
                "iteration": result["iteration"],
                "message_count": result["message_count"],
                "missing": missing_repr,
            }
        )

    if failures:
        print("Soak validation reported issues:", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)
        return 1

    print("All soak iterations satisfied the configured thresholds.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
