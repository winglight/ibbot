"""Command-line entrypoint for the risk FastAPI gateway."""

from __future__ import annotations

import argparse
import os
from typing import Sequence

from services.risk.http import create_app

DEFAULT_HOST = os.getenv("RISK_SERVICE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("RISK_SERVICE_PORT", "8030"))

app = create_app()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the risk service HTTP gateway")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Service bind host (default: %(default)s)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Service port (default: %(default)s)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload. Only use in development.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
