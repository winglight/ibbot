from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, validator
from typing import Optional, List, Dict
from datetime import datetime, timedelta
import random
import time
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="IB Gateway Mock", version="0.1.0")

# In-memory state
connected: bool = False
orders: List[Dict] = []
positions: Dict[str, Dict] = {}
prices: Dict[str, float] = {}
next_order_id: int = 1


class OrderRequest(BaseModel):
    symbol: str
    quantity: int
    action: str  # BUY / SELL
    order_type: str  # MARKET / LIMIT
    exchange: str = "SMART"
    sec_type: str = "STK"
    price: Optional[float] = None

    @validator('quantity')
    def quantity_must_be_positive(cls, v):
        if v is None or v <= 0:
            raise ValueError('quantity must be greater than 0')
        return v

    @validator('price')
    def price_if_provided_positive(cls, v, values):
        # Only validate if provided; for LIMIT orders it should be provided by caller
        if v is not None and v <= 0:
            raise ValueError('price must be greater than 0 when provided')
        return v


class CancelOrderRequest(BaseModel):
    order_id: int

class SetPriceRequest(BaseModel):
    symbol: str
    price: float

class HistoricalDataRequest(BaseModel):
    symbol: str
    duration: str = "1 D"
    bar_size: str = "5 mins"
    exchange: str = "SMART"

class ChartDataRequest(HistoricalDataRequest):
    pass

# 新增：按节奏推送生成的行情数据（基于OHLCV的close）
class FeedDataRequest(BaseModel):
    symbol: str
    duration: str = "1 D"
    bar_size: str = "5 mins"
    delay_ms: int = 500  # 每个数据点之间的延迟（毫秒）

account_info: Dict = {
    "account_id": "DU1234567",
    "account_type": "INDIVIDUAL",
    "total_cash": 100000.0,
    "available_funds": 100000.0,
    "buying_power": 400000.0,
    "net_liquidation": 100000.0,
    "unrealized_pnl": 0.0,
    "realized_pnl": 0.0,
}

# --------- Helpers ---------

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _fill_order(o: Dict, fill_price: float):
    global positions, account_info
    o["status"] = "Filled"
    o["fill_price"] = float(fill_price)
    o["filled"] = o["quantity"]
    o["remaining"] = 0
    o["filled_at"] = _now_iso()

    sym = o["symbol"]
    qty = o["quantity"] * (1 if o["action"].upper() == "BUY" else -1)

    # Update position
    pos = positions.get(sym, {
        "symbol": sym,
        "position": 0,
        "avg_cost": 0.0,
        "market_price": prices.get(sym, fill_price),
        "current_price": prices.get(sym, fill_price),
        "market_value": 0.0,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_percent": 0.0,
        "price_source": "mock",
    })

    prev_pos = pos["position"]
    prev_cost = pos["avg_cost"]

    new_pos = prev_pos + qty
    if new_pos == 0:
        # Closed position
        pos["position"] = 0
        pos["avg_cost"] = 0.0
    elif (prev_pos >= 0 and qty >= 0) or (prev_pos <= 0 and qty <= 0):
        # Adding to position (same direction)
        total_shares = abs(prev_pos) + abs(qty)
        pos["avg_cost"] = ((abs(prev_pos) * prev_cost) + (abs(qty) * fill_price)) / total_shares
        pos["position"] = new_pos
    else:
        # Reversing position
        if abs(qty) <= abs(prev_pos):
            pos["position"] = new_pos
            # avg_cost remains the same
        else:
            # Fully reverse and more
            pos["position"] = new_pos
            pos["avg_cost"] = fill_price

    # Update account cash
    cost = fill_price * abs(qty)
    if o["action"].upper() == "BUY":
        account_info["total_cash"] -= cost
        account_info["available_funds"] -= cost
    else:
        account_info["total_cash"] += cost
        account_info["available_funds"] += cost

    positions[sym] = pos
    _recalc_market(sym)
    _recalc_account()


def _try_fill_limit_orders_for_symbol(sym: str):
    price = prices.get(sym)
    if price is None:
        return
    for o in orders:
        if o["symbol"] != sym or o["status"] != "Submitted" or o["order_type"] != "LMT":
            continue
        lp = o.get("limit_price")
        if lp is None:
            continue
        if o["action"].upper() == "BUY" and lp >= price:
            _fill_order(o, price)
        elif o["action"].upper() == "SELL" and lp <= price:
            _fill_order(o, price)


def _recalc_market(sym: Optional[str] = None):
    """Recalculate market value for positions"""
    syms = [sym] if sym else list(positions.keys())
    for s in syms:
        pos = positions.get(s)
        if pos is None:
            # Create default position
            default_price = prices.get(s, 0.0)
            pos = {
                "symbol": s,
                "position": 0,
                "avg_cost": 0.0,
                "market_price": default_price,
                "current_price": default_price,
                "market_value": 0.0,
                "unrealized_pnl": 0.0,
                "unrealized_pnl_percent": 0.0,
                "price_source": "mock",
            }
            positions[s] = pos
        p = prices.get(s, pos.get("current_price", 0.0))
        pos["market_price"] = p
        pos["current_price"] = p
        pos["market_value"] = p * pos["position"]
        basis = pos["avg_cost"] * pos["position"]
        pos["unrealized_pnl"] = pos["market_value"] - basis
        pos["unrealized_pnl_percent"] = (pos["unrealized_pnl"] / basis * 100.0) if basis != 0 else 0.0
        pos["price_source"] = "mock"


def _recalc_account():
    """Recalculate account totals"""
    mv = sum(v["market_value"] for v in positions.values())
    account_info["net_liquidation"] = account_info["total_cash"] + mv
    # Simple buying power calculation
    account_info["buying_power"] = max(0.0, account_info["available_funds"] * 4)


def _parse_duration_to_minutes(duration: str) -> int:
    """Parse duration like '1 D', '5 H', '30 min' to minutes"""
    try:
        parts = duration.strip().split()
        if len(parts) != 2:
            return 24 * 60  # default 1 day
        qty = float(parts[0])
        unit = parts[1].lower()
        if unit.startswith('d'):
            return int(qty * 24 * 60)
        if unit.startswith('h'):
            return int(qty * 60)
        if unit.startswith('min'):
            return int(qty)
        return 24 * 60
    except Exception:
        return 24 * 60


def _bar_minutes(bar_size: str) -> int:
    """Parse bar size like '5 mins', '1 hour' to minutes"""
    try:
        parts = bar_size.strip().split()
        if len(parts) >= 2 and parts[1].lower().startswith('min'):
            return int(float(parts[0]))
        if parts[1].lower().startswith('hour'):
            return int(float(parts[0]) * 60)
        return 5  # default
    except Exception:
        return 5


def _generate_ohlcv(symbol: str, duration: str, bar_size: str) -> List[Dict]:
    total_minutes = _parse_duration_to_minutes(duration)
    step_min = _bar_minutes(bar_size)
    bars = max(1, total_minutes // step_min)
    last_price = prices.get(symbol, 100.0)
    now = datetime.utcnow()
    data: List[Dict] = []
    for i in range(bars):
        # Go back in time
        ts = now - timedelta(minutes=(bars - i) * step_min)
        # Random price movement
        change = random.uniform(-0.005, 0.005)
        open_p = last_price
        close_p = max(0.01, open_p * (1 + change))
        high_p = max(open_p, close_p) * (1 + random.uniform(0.0, 0.002))
        low_p = min(open_p, close_p) * (1 - random.uniform(0.0, 0.002))
        vol = int(random.uniform(1000, 10000))
        data.append({
            "date": ts,
            "open": float(open_p),
            "high": float(high_p),
            "low": float(low_p),
            "close": float(close_p),
            "volume": vol,
        })
        last_price = close_p
    # Update global price
    prices[symbol] = float(last_price)
    _recalc_market(symbol)
    return data

# --------- API Routes ---------

@app.post("/api/connect")
def connect():
    global connected
    connected = True
    return {"success": True, "connected": True}

@app.post("/api/disconnect")
def disconnect():
    global connected
    connected = False
    return {"success": True, "connected": False}

@app.get("/api/connection_status")
def connection_status():
    return {"success": True, "connected": connected}

# Order management
@app.post("/api/place_order")
def place_order(order: OrderRequest):
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    global next_order_id

    otype = order.order_type.upper()
    action = order.action.upper()

    od = {
        "order_id": next_order_id,
        "symbol": order.symbol,
        "action": action,
        "quantity": int(order.quantity),
        "order_type": "MKT" if otype == "MARKET" else "LMT",
        "limit_price": float(order.price) if order.price is not None else None,
        "avg_fill_price": 0.0,
        "status": "Submitted",
        "filled": 0,
        "remaining": int(order.quantity),
        "submit_time": _now_iso(),
        "exchange": order.exchange,
        "sec_type": order.sec_type,
    }
    orders.append(od)
    oid = next_order_id
    next_order_id += 1

    # Simulate order execution
    ref_price = prices.get(order.symbol, order.price if order.price is not None else 100.0)
    if otype == "MARKET":
        _fill_order(od, ref_price)
    else:
        # LIMIT order - check if can fill immediately
        _try_fill_limit_orders_for_symbol(order.symbol)

    return {"success": True, "order_id": oid, "message": "Order placed"}

@app.delete("/api/cancel_order/{order_id}")
def cancel_order(order_id: int):
    for o in orders:
        if o["order_id"] == order_id:
            if o["status"] == "Filled":
                return {"success": False, "message": "Already filled"}
            o["status"] = "Cancelled"
            o["remaining"] = 0
            return {"success": True, "message": "Cancelled"}
    raise HTTPException(status_code=404, detail="Order not found")

@app.post("/api/cancel_order")
def cancel_order_post(req: CancelOrderRequest):
    return cancel_order(req.order_id)

@app.delete("/api/cancel_all_orders")
def cancel_all_orders():
    cnt = 0
    for o in orders:
        if o["status"] == "Submitted":
            o["status"] = "Cancelled"
            o["remaining"] = 0
            cnt += 1
    return {"success": True, "message": f"Cancelled {cnt} orders"}

@app.get("/api/orders")
def get_orders():
    # Convert to the format expected by frontend
    converted = []
    for o in sorted(orders, key=lambda x: x["order_id"], reverse=True):
        converted.append({
            "order_id": o["order_id"],
            "symbol": o["symbol"],
            "action": o["action"],
            "quantity": o["quantity"],
            "order_type": o["order_type"],
            "limit_price": o.get("limit_price"),
            "avg_fill_price": o.get("fill_price", 0.0),
            "status": o["status"],
            "filled": o["filled"],
            "remaining": o["remaining"],
            "submit_time": o.get("submit_time"),
            "order_source": "mock",
            "trigger_type": None,
        })
    return {"success": True, "orders": converted}

# Market data
@app.get("/api/realtime_data/{symbol}")
def realtime_data(symbol: str, exchange: str = "SMART"):
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    prev = prices.get(symbol)
    base = prev if prev is not None else 100.0
    # Small random movement
    new_price = max(0.01, base * (1 + random.uniform(-0.002, 0.002)))
    prices[symbol] = float(new_price)
    _recalc_market(symbol)
    _try_fill_limit_orders_for_symbol(symbol)
    data = {
        "symbol": symbol,
        "last": float(new_price),
        "bid": float(new_price) - 0.01,
        "ask": float(new_price) + 0.01,
        "volume": int(random.uniform(1000, 10000)),
        "timestamp": _now_iso(),
    }
    return {"success": True, "data": data}

@app.post("/api/historical_data")
def historical_data(req: HistoricalDataRequest):
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    data = _generate_ohlcv(req.symbol, req.duration, req.bar_size)
    return {
        "success": True,
        "data": [{
            "date": (d["date"].isoformat() if hasattr(d["date"], 'isoformat') else str(d["date"])) ,
            "open": d["open"],
            "high": d["high"],
            "low": d["low"],
            "close": d["close"],
            "volume": d["volume"],
        } for d in data],
        "symbol": req.symbol,
        "duration": req.duration,
        "bar_size": req.bar_size,
        "rows": len(data),
    }

@app.post("/api/chart_data")
def chart_data(req: ChartDataRequest):
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    data = _generate_ohlcv(req.symbol, req.duration, req.bar_size)
    chart = [{
        "time": (d["date"].isoformat() if hasattr(d["date"], 'isoformat') else str(d["date"])),
        "open": d["open"],
        "high": d["high"],
        "low": d["low"],
        "close": d["close"],
        "volume": d["volume"],
    } for d in data]
    return {
        "success": True,
        "data": chart,
        "symbol": req.symbol,
        "duration": req.duration,
        "bar_size": req.bar_size,
    }

# Account & positions
@app.get("/api/account_info")
def get_account_info():
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    return {"success": True, "account_info": account_info}

@app.get("/api/positions")
def get_positions(fast: bool = True):
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    if not fast:
        _recalc_market()  # ensure up-to-date
    return {"success": True, "positions": list(positions.values())}

# Mock utilities
@app.post("/api/mock/set_price")
def set_price(req: SetPriceRequest):
    """Set a specific price for a symbol (for testing)"""
    prices[req.symbol] = float(req.price)
    _recalc_market(req.symbol)
    _try_fill_limit_orders_for_symbol(req.symbol)
    return {
        "success": True,
        "message": f"Price for {req.symbol} set to {req.price}",
        "symbol": req.symbol,
        "price": float(req.price),
    }

@app.post("/api/mock/feed_data")
def feed_data(req: FeedDataRequest):
    """Stream historical data with delays"""
    data = _generate_ohlcv(req.symbol, req.duration, req.bar_size)
    
    async def generate():
        for i, d in enumerate(data):
            # Update price and trigger limit orders
            close_price = d["close"]
            prices[req.symbol] = close_price
            _recalc_market(req.symbol)
            _try_fill_limit_orders_for_symbol(req.symbol)
            
            yield {
                "success": True,
                "data": {
                    "time": (d["date"].isoformat() if hasattr(d["date"], 'isoformat') else str(d["date"])),
                    "open": d["open"],
                    "high": d["high"],
                    "low": d["low"],
                    "close": d["close"],
                    "volume": d["volume"],
                },
                "symbol": req.symbol,
                "index": i,
                "total": len(data),
                "is_last": i == len(data) - 1,
            }
            if i < len(data) - 1:  # Don't delay after last item
                time.sleep(req.delay_ms / 1000.0)
    
    return {"success": True, "message": "Data feed started", "bars": len(data)}

@app.get("/api/mock/reset")
def reset_mock():
    """Reset all mock data"""
    global connected, orders, positions, prices, next_order_id, account_info
    connected = False
    orders.clear()
    positions.clear()
    prices.clear()
    next_order_id = 1
    account_info.update({
        "total_cash": 100000.0,
        "available_funds": 100000.0,
        "buying_power": 400000.0,
        "net_liquidation": 100000.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
    })
    return {"success": True, "message": "Mock state reset"}

@app.get("/api/mock/status")
def mock_status():
    """Get current mock state"""
    return {
        "success": True,
        "status": {
            "connected": connected,
            "orders_count": len(orders),
            "positions_count": len(positions),
            "prices_count": len(prices),
            "next_order_id": next_order_id,
            "account_info": account_info,
        }
    }

# Documentation
@app.get("/", response_class=HTMLResponse)
def docs():
    return """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Mock Gateway API - RapiDoc</title>
        <script type="module" src="https://unpkg.com/rapidoc/dist/rapidoc-min.js"></script>
      </head>
      <body>
        <rapi-doc
          spec-url="/api-docs.json"
          theme="dark"
          render-style="read"
          nav-bg-color="#1e3a8a"
          primary-color="#3b82f6"
          allow-try="true"
          show-header="true"
          heading-text="IB Gateway Mock API">
        </rapi-doc>
      </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mock_ib_gateway:app", host="0.0.0.0", port=18080)