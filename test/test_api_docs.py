import json
from pathlib import Path


def _build_categories(openapi: dict) -> dict:
    categories: dict[str, list[dict]] = {}
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

            query_params = []
            headers = []
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

            request_body = None
            body_schema = (
                meta.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if body_schema:
                if "$ref" in body_schema:
                    request_body = body_schema["$ref"].split("/")[-1]
                elif "title" in body_schema:
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


def test_api_doc_json_matches_openapi(client):
    openapi = client.get("/api-docs.json").json()
    actual = _build_categories(openapi)

    recorded = json.loads(Path("docs/api_doc.json").read_text(encoding="utf-8"))
    assert "categories" in recorded
    assert actual == recorded["categories"]
