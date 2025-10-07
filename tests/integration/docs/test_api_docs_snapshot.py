"""Integration tests that ensure the published API docs snapshot stays in sync."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

SNAPSHOT_PATH = Path("docs/api_doc.json")


def _build_categories(openapi: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Normalise the OpenAPI schema into the documented categories structure."""

    categories: dict[str, list[dict[str, Any]]] = {}
    for path, methods in openapi.get("paths", {}).items():
        for method, meta in methods.items():
            if method == "parameters":
                continue

            if path.startswith("/api/"):
                category = path.split("/")[2] or "root"
            elif path.startswith("/ws"):
                category = "websocket"
            elif path.startswith("/static"):
                category = "static"
            else:
                category = "root"

            query_params: list[dict[str, Any]] = []
            headers: list[dict[str, Any]] = []
            for param in meta.get("parameters", []):
                entry = {
                    "name": param.get("name"),
                    "required": param.get("required", False),
                }
                if "description" in param:
                    entry["description"] = param["description"]
                if param.get("in") == "query":
                    query_params.append(entry)
                elif param.get("in") == "header":
                    headers.append(entry)

            request_body: str | dict[str, Any] | None = None
            body_schema = (
                meta.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if body_schema:
                if isinstance(body_schema, Mapping) and "$ref" in body_schema:
                    request_body = body_schema["$ref"].split("/")[-1]
                elif isinstance(body_schema, Mapping) and "title" in body_schema:
                    request_body = body_schema["title"]
                else:
                    request_body = body_schema

            entry = {
                "method": method.upper(),
                "path": path,
                "summary": meta.get("summary"),
                "requires_auth": bool(meta.get("security")),
                "query_params": query_params or None,
                "headers": headers or None,
                "request_body": request_body,
                "response_codes": sorted(meta.get("responses", {}).keys()) or None,
            }
            categories.setdefault(category, []).append(entry)

    for items in categories.values():
        items.sort(key=lambda item: (item["path"], item["method"]))
    return categories


def test_api_docs_snapshot_matches_openapi(client: TestClient) -> None:
    """Ensure the live OpenAPI schema still matches the committed snapshot."""

    response = client.get("/api-docs.json")
    response.raise_for_status()
    categories = _build_categories(response.json())

    snapshot_raw = SNAPSHOT_PATH.read_text(encoding="utf-8")
    snapshot = json.loads(snapshot_raw)
    assert "categories" in snapshot, "docs/api_doc.json must include a categories mapping"
    assert categories == snapshot["categories"], (
        "OpenAPI schema has changed. Update docs/api_doc.json to match the new schema."
    )
