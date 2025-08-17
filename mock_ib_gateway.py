from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime

app = FastAPI(title="IB Gateway Mock", version="0.1.0")

# --------- Models ---------
class OrderRequest(BaseModel):
    symbol: str
    quantity: int
    action: str  # BUY / SELL
    order_type: str  # MARKET / LIMIT
    exchange: str = "SMART"
    sec_type: str = "STK"
    price: Optional[float] = None

class CancelOrderRequest(BaseModel):
    order_id: int

class SetPriceRequest(BaseModel):
    symbol: str
    price: float

# --------- In-memory state ---------
connected: bool = False
orders: List[Dict] = []
next_order_id: int = 1
positions: Dict[str, Dict] = {}
prices: Dict[str, float] = {}

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
        # fully closed
        pos["position"] = 0
        pos["avg_cost"] = 0.0
    elif (prev_pos >= 0 and qty >= 0) or (prev_pos <= 0 and qty <= 0):
        # adding to same side -> weighted average
        total_shares = abs(prev_pos) + abs(qty)
        pos["avg_cost"] = ((abs(prev_pos) * prev_cost) + (abs(qty) * fill_price)) / total_shares
        pos["position"] = new_pos
    else:
        # reducing or flipping side
        if abs(qty) <= abs(prev_pos):
            pos["position"] = new_pos
            # avg_cost unchanged when partially closing
        else:
            # flipped side, set new avg cost to fill price for remaining
            pos["position"] = new_pos
            pos["avg_cost"] = fill_price

    # Update account cash and realized PnL for filled trade
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
    # Recalculate position market values and PnL
    syms = [sym] if sym else list(positions.keys())
    for s in syms:
        pos = positions[s]
        p = prices.get(s, pos.get("current_price", 0.0))
        pos["market_price"] = p
        pos["current_price"] = p
        pos["market_value"] = p * pos["position"]
        basis = pos["avg_cost"] * pos["position"]
        pos["unrealized_pnl"] = pos["market_value"] - basis
        pos["unrealized_pnl_percent"] = (pos["unrealized_pnl"] / basis * 100.0) if basis != 0 else 0.0
        pos["price_source"] = "mock"

def _recalc_account():
    # Simple net liquidation approximation
    mv = sum(v["market_value"] for v in positions.values())
    account_info["net_liquidation"] = account_info["total_cash"] + mv
    # Simplified buying power: 4x of cash
    account_info["buying_power"] = max(0.0, account_info["available_funds"] * 4)

# --------- Connection endpoints ---------
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

# --------- Trading endpoints ---------
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

    # Fill logic
    ref_price = prices.get(order.symbol, order.price if order.price is not None else 100.0)
    if otype == "MARKET":
        _fill_order(od, ref_price)
    else:
        # LMT -> may fill immediately depending on current price
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
    # Align response shape with main app
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

# --------- Account / Positions ---------
@app.get("/api/account_info")
def get_account_info():
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    return {"success": True, "account_info": account_info}

@app.get("/api/positions")
def get_positions():
    if not connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR (mock)")
    _recalc_market()  # ensure up-to-date
    return {"success": True, "positions": list(positions.values())}

# --------- Mock utilities ---------
@app.post("/api/mock/set_price")
def set_price(req: SetPriceRequest):
    prices[req.symbol] = float(req.price)
    _recalc_market(req.symbol)
    _try_fill_limit_orders_for_symbol(req.symbol)
    return {"success": True, "symbol": req.symbol, "price": prices[req.symbol]}

@app.post("/api/mock/reset")
def reset_state():
    global orders, next_order_id, positions, prices, account_info, connected
    orders = []
    next_order_id = 1
    positions = {}
    prices = {}
    connected = False
    account_info = {
        "account_id": "DU1234567",
        "account_type": "INDIVIDUAL",
        "total_cash": 100000.0,
        "available_funds": 100000.0,
        "buying_power": 400000.0,
        "net_liquidation": 100000.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
    }
    return {"success": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mock_ib_gateway:app", host="0.0.0.0", port=18080)