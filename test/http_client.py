"""Minimal requests-compatible wrapper built on httpx to remove deprecated urllib3 extras."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

Response = httpx.Response


def _build_client(timeout: Optional[float] = None) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def get(url: str, **kwargs: Any) -> Response:
    timeout = kwargs.pop("timeout", None)
    with _build_client(timeout=timeout) as client:
        return client.get(url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    timeout = kwargs.pop("timeout", None)
    with _build_client(timeout=timeout) as client:
        return client.post(url, **kwargs)


def put(url: str, **kwargs: Any) -> Response:
    timeout = kwargs.pop("timeout", None)
    with _build_client(timeout=timeout) as client:
        return client.put(url, **kwargs)


def delete(url: str, **kwargs: Any) -> Response:
    timeout = kwargs.pop("timeout", None)
    with _build_client(timeout=timeout) as client:
        return client.delete(url, **kwargs)


class exceptions:
    RequestException = httpx.HTTPError


__all__ = [
    "Response",
    "get",
    "post",
    "put",
    "delete",
    "exceptions",
]
