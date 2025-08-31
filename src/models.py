"""
Pydantic 请求模型定义

基于 main.py 中的 API 端点分析，定义所有必需的请求模型
"""

from pydantic import BaseModel, validator, root_validator
from typing import Optional, Dict, Any

class HistoricalDataRequest(BaseModel):
    """历史数据请求模型"""
    symbol: str
    duration: str
    bar_size: str
    exchange: str = "SMART"

class ChartDataRequest(BaseModel):
    """图表数据请求模型"""
    symbol: str
    duration: str
    bar_size: str
    exchange: str = "SMART"
    data_type: str = "TRADES"  # 添加data_type支持

class RiskRuleRequest(BaseModel):
    """风险规则请求模型"""
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    
    @validator('stop_loss_price', 'take_profit_price')
    def prices_must_be_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError('risk rule prices must be greater than 0 when provided')
        return v

class OrderRequest(BaseModel):
    """订单请求模型 - 与 API 端点保持一致"""
    symbol: str
    quantity: int
    action: str  # BUY, SELL
    order_type: str  # MARKET, LIMIT
    exchange: str = "SMART"
    sec_type: str = "STK"
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_rule: Optional[RiskRuleRequest] = None  # 新增风险规则字段

    # 校验：数量必须大于0
    @validator('quantity')
    def quantity_must_be_positive(cls, v):
        if v is None or v <= 0:
            raise ValueError('quantity must be greater than 0')
        return v

    # 校验：如提供 price/stop_loss/take_profit，则必须大于0
    @validator('price', 'stop_loss', 'take_profit')
    def optional_prices_must_be_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError('price values must be greater than 0 when provided')
        return v

class ExternalStrategyOrderRequest(BaseModel):
    """外部策略订单请求模型"""
    strategy: str
    symbol: str
    action: str  # BUY, SELL
    qty: int
    type: str  # MARKET, LIMIT
    price: Optional[float] = None
    exchange: str = "SMART"
    sec_type: str = "STK"
    token: Optional[str] = None

class RiskRuleRequest(BaseModel):
    """风险规则请求模型"""
    symbol: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_loss_percent: Optional[float] = None

    # 至少提供一个止盈/止损价格，且提供的价格必须 > 0
    @root_validator(skip_on_failure=True)
    def validate_sl_tp_prices(cls, values):
        sl = values.get('stop_loss_price')
        tp = values.get('take_profit_price')
        if (sl is None or sl <= 0) and (tp is None or tp <= 0):
            raise ValueError('At least one of stop_loss_price or take_profit_price must be provided and > 0')
        return values

class StrategyRequest(BaseModel):
    name: str
    strategy_type: str
    enabled: bool = True
    api_endpoint: Optional[str] = None
    file_path: Optional[str] = None
    description: Optional[str] = None
    use_template: bool = False
    selected_file: Optional[str] = None
    sl_enabled: bool = False
    tp_enabled: bool = False
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_type: str = "amount"  # "amount" or "percent"
    tp_type: str = "amount"  # "amount" or "percent"

class StrategySettingsRequest(BaseModel):
    """策略设置请求模型"""
    api_endpoint: Optional[str] = None
    api_method: Optional[str] = None
    api_payload_template: Optional[str] = None
    strategy_path: Optional[str] = None
    enable_auth: Optional[bool] = None
    auth_token: Optional[str] = None