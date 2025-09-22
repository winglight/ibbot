#!/usr/bin/env python3
"""Optimizer Service entry point.

Provides a dedicated FastAPI application for the optimizer module so that
core trading routes remain untouched when running in optimizer-only mode.
"""

import argparse
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

from src.optimizer_service import router as optimizer_router

OPTIMIZER_APP_TITLE = "IBBot Optimizer"
OPTIMIZER_APP_VERSION = "0.1.0"
OPTIMIZER_UI_DIR = Path(__file__).parent / "optimizer_ui"
DEFAULT_HOST = os.getenv("OPTIMIZER_HOST", "0.0.0.0")
DEFAULT_PORT = 8001  # Fixed default port


def ensure_optimizer_ui_dir() -> None:
    """Create optimizer UI directory if it doesn't exist yet."""
    try:
        OPTIMIZER_UI_DIR.mkdir(parents=True, exist_ok=True)
        (OPTIMIZER_UI_DIR / "templates").mkdir(parents=True, exist_ok=True)
        (OPTIMIZER_UI_DIR / "static").mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover - defensive, should not fail
        logger.warning(f"Unable to create optimizer UI directory: {exc}")


def build_app() -> FastAPI:
    """Instantiate the FastAPI app dedicated to the optimizer service."""
    load_dotenv()
    ensure_optimizer_ui_dir()

    app = FastAPI(title=OPTIMIZER_APP_TITLE, version=OPTIMIZER_APP_VERSION)
    templates_dir = OPTIMIZER_UI_DIR / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    static_dir = OPTIMIZER_UI_DIR / "static"
    if static_dir.exists():
        app.mount("/optimizer-static", StaticFiles(directory=str(static_dir)), name="optimizer_static")

    app.include_router(optimizer_router)

    @app.get("/", response_class=HTMLResponse)
    async def optimizer_index(request: Request):
        template_path = templates_dir / "index.html"
        if not template_path.exists():
            return HTMLResponse("<h3>Optimizer UI is under construction.</h3>", status_code=200)
        return templates.TemplateResponse("index.html", {"request": request})

    return app


app = build_app()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run optimizer-only FastAPI service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Service host (default from OPTIMIZER_HOST or 0.0.0.0)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Service port (default: 8001)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (intended for development only)",
    )
    return parser.parse_args(argv)


def configure_logging() -> None:
    try:
        logger.remove()
    except ValueError:
        pass
    logger.add(lambda msg: print(msg, end=""), level="INFO")


def main() -> None:
    args = parse_args()
    configure_logging()
    logger.info(
        f"Starting optimizer service on http://{args.host}:{args.port} "
        f"(reload={'on' if args.reload else 'off'})"
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
