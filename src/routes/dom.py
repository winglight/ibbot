from typing import Any, Dict, List, Mapping, Optional

import urllib.parse

import httpx

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from .auth import get_current_user
from src.dom_dependencies import dom_config
from src.routes.service_clients import (
    MarketDataServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)
from loguru import logger

router = APIRouter()


def _ensure_not_read_only(request: Request) -> None:
    if getattr(request.state, "is_read_only", False):
        raise HTTPException(status_code=403, detail="Read-only access")


class StartRequest(BaseModel):
    symbol: str
    num_rows: int = Field(default=5, ge=1, le=30)
    force_restart: bool = False


@router.post("/api/dom/start")
async def dom_start(
    req: StartRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    symbol = (req.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    client = get_dom_service_client()
    try:
        return await client.start_dom(
            {
                "symbol": symbol,
                "num_rows": req.num_rows,
                "force_restart": bool(req.force_restart),
            }
        )
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


class StopRequest(BaseModel):
    symbol: str


@router.post("/api/dom/stop")
async def dom_stop(
    req: StopRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    symbol = (req.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    client = get_dom_service_client()
    try:
        return await client.stop_dom({"symbol": symbol})
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


class DOMConfigModel(BaseModel):
    window: int = dom_config.window
    max_levels: int = dom_config.max_levels
    trend_thresholds: List[float] = Field(
        default_factory=lambda: list(getattr(dom_config, "trend_thresholds", []))
    )


class DomServiceGateway:
    """Thin wrapper that exposes DOM operations through the market-data service client."""

    def __init__(self, client: MarketDataServiceClient) -> None:
        self._client = client

    @property
    def client(self) -> MarketDataServiceClient:
        return self._client

    async def start_dom(self, payload: Mapping[str, Any]) -> Any:
        return await self._client.dom_start(payload)

    async def stop_dom(self, payload: Mapping[str, Any]) -> Any:
        return await self._client.dom_stop(payload)

    async def get_config(self) -> Any:
        return await self._client.dom_get_config()

    async def update_config(self, payload: Mapping[str, Any]) -> Any:
        return await self._client.dom_update_config(payload)

    async def get_data(self, symbol: Optional[str] = None) -> Any:
        return await self._client.dom_get_data(symbol)

    async def get_history(
        self,
        symbol: str,
        *,
        limit: int | None = None,
        start: str | None = None,
        end: str | None = None,
        include_snapshots: bool | None = None,
    ) -> Any:
        return await self._client.dom_get_history(
            symbol,
            limit=limit,
            start=start,
            end=end,
            include_snapshots=include_snapshots,
        )

    async def get_stats(
        self,
        symbol: str,
        *,
        lookback_seconds: int | None = None,
        start: str | None = None,
        end: str | None = None,
        max_points: int | None = None,
    ) -> Any:
        return await self._client.dom_get_stats(
            symbol,
            lookback_seconds=lookback_seconds,
            start=start,
            end=end,
            max_points=max_points,
        )

    async def get_alerts(self) -> Any:
        return await self._client.dom_get_alerts()

    async def acknowledge_alert(self, alert_id: int) -> Any:
        return await self._client.dom_ack_alert(alert_id)

    def websocket_url(self, path: str) -> str:
        base_url = self._client.base_url
        parsed = urllib.parse.urlparse(base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        ws_path = urllib.parse.urljoin(base_path + "/", path.lstrip("/"))
        return urllib.parse.urlunparse((scheme, parsed.netloc, ws_path, "", "", ""))


_DOM_SERVICE_CLIENT = DomServiceGateway(MarketDataServiceClient())


def get_dom_service_client() -> DomServiceGateway:
    return _DOM_SERVICE_CLIENT


def _translate_service_error(exc: ServiceClientError) -> HTTPException:
    if isinstance(exc, ServiceUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ServiceResponseError):
        detail = exc.detail or "DOM service error"
        status = exc.status_code or 502
        return HTTPException(status_code=status, detail=detail)
    return HTTPException(status_code=502, detail=str(exc))


@router.get("/api/config/dom")
async def get_dom_config(user: Optional[str] = Depends(get_current_user)):
    client = get_dom_service_client()
    try:
        return await client.get_config()
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


@router.put("/api/config/dom")
async def update_dom_config(
    cfg: DOMConfigModel,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    client = get_dom_service_client()
    try:
        return await client.update_config(
            {
                "window": cfg.window,
                "max_levels": cfg.max_levels,
                "trend_thresholds": cfg.trend_thresholds,
            }
        )
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


@router.get("/api/dom/data")
async def get_dom_data(
    symbol: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
):
    client = get_dom_service_client()
    try:
        return await client.get_data(symbol)
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


@router.get("/api/alerts")
async def get_alerts(user: Optional[str] = Depends(get_current_user)):
    client = get_dom_service_client()
    try:
        return await client.get_alerts()
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


@router.post("/api/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: int,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    client = get_dom_service_client()
    try:
        return await client.acknowledge_alert(alert_id)
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


@router.websocket("/ws/dom/{symbol}")
async def dom_ws(websocket: WebSocket, symbol: str):
    await websocket.accept()

    client = get_dom_service_client()
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    remote_path = f"/ws/dom/{encoded_symbol}"
    ws_url = client.websocket_url(remote_path)

    try:
        async with httpx.AsyncClient(timeout=None) as session:
            async with session.ws_connect(ws_url) as remote_ws:
                async for message in remote_ws.iter_text():
                    await websocket.send_text(message)
    except WebSocketDisconnect:
        return
    except httpx.HTTPError as exc:
        logger.warning("DOM websocket bridge failed", symbol=symbol, error=str(exc))
        await websocket.send_json(
            {
                "type": "dom_error",
                "symbol": symbol,
                "success": False,
                "error": "DOM service connection failed",
                "details": str(exc),
            }
        )
        await websocket.close(code=1011)


@router.get("/api/dom/history/{symbol}")
async def get_dom_history(
    symbol: str,
    limit: int = Query(500, ge=0),
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_snapshots: bool = Query(True),
    user: Optional[str] = Depends(get_current_user),
):
    client = get_dom_service_client()
    try:
        return await client.get_history(
            symbol,
            limit=None if limit == 0 else limit,
            start=start,
            end=end,
            include_snapshots=include_snapshots,
        )
    except ServiceClientError as exc:
        raise _translate_service_error(exc)


@router.get("/api/dom/stats/{symbol}")
async def get_dom_stats(
    symbol: str,
    lookback_seconds: Optional[int] = Query(None, ge=60),
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_points: Optional[int] = Query(None, ge=10, le=5000),
    user: Optional[str] = Depends(get_current_user),
):
    client = get_dom_service_client()
    try:
        return await client.get_stats(
            symbol,
            lookback_seconds=lookback_seconds,
            start=start,
            end=end,
            max_points=max_points,
        )
    except ServiceClientError as exc:
        raise _translate_service_error(exc)
