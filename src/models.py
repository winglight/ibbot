"""
Pydantic 请求模型定义

基于 main.py 中的 API 端点分析，定义所有必需的请求模型
"""

from typing import Optional, Dict, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    use_cache: Optional[bool] = Field(True, alias="useCache")
    force_refresh: Optional[bool] = Field(False, alias="forceRefresh")

    model_config = ConfigDict(populate_by_name=True)

class OrderRiskRuleRequest(BaseModel):
    """风险规则请求模型"""
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    @field_validator('stop_loss_price', 'take_profit_price')
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
    risk_rule: Optional[OrderRiskRuleRequest] = None  # 新增风险规则字段

    # 校验：数量必须大于0
    @field_validator('quantity')
    def quantity_must_be_positive(cls, v):
        if v is None or v <= 0:
            raise ValueError('quantity must be greater than 0')
        return v

    # 校验：如提供 price/stop_loss/take_profit，则必须大于0
    @field_validator('price', 'stop_loss', 'take_profit')
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
    rule_type: str = "fixed"
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_loss_percent: Optional[float] = None
    max_time_span: Optional[int] = 30  # 以分钟为单位的最大持仓时间，默认30分钟
    atr_params: Optional[Dict[str, Any]] = None

    @field_validator('rule_type')
    def normalize_rule_type(cls, value: Optional[str]) -> str:
        text = (value or 'fixed').strip().lower()
        allowed = {'fixed', 'trailing'}
        if text not in allowed:
            raise ValueError(f'rule_type must be one of {sorted(allowed)}')
        return text

    @field_validator('stop_loss_price', 'take_profit_price')
    def prices_must_be_positive(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and value <= 0:
            raise ValueError('stop_loss_price/take_profit_price must be greater than 0 when provided')
        return value

    @field_validator('atr_params')
    def normalize_atr_params(cls, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ValueError('atr_params must be a mapping when provided')
        cleaned: Dict[str, Any] = {}
        for key, val in payload.items():
            if val is None:
                continue
            try:
                cleaned[key] = float(val)
            except (TypeError, ValueError):
                raise ValueError(f'atr_params values must be numeric, got {key}={val!r}')
        return cleaned or None

    # 至少提供一个止盈/止损价格，且提供的价格必须 > 0（固定规则适用）
    @model_validator(mode='after')
    def validate_sl_tp_prices(cls, values: 'RiskRuleRequest'):
        if values.rule_type != 'trailing':
            sl = values.stop_loss_price
            tp = values.take_profit_price
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
    primary_symbol: Optional[str] = None
    sl_enabled: bool = False
    tp_enabled: bool = False
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_type: str = "amount"  # "amount", "percent" 或 "trailing"
    tp_type: str = "amount"  # "amount", "percent" 或 "trailing"
    data_subscription: Optional[str] = None
    position_add_cooldown_minutes: Optional[int] = None
    close_before_close_minutes: Optional[int] = None
    sl_atr_params: Optional[Dict[str, Any]] = None

    @field_validator('data_subscription')
    def validate_data_subscription(cls, value: Optional[str]):
        if value is None:
            return value
        allowed = {"market_data", "dom_trend"}
        if value not in allowed:
            raise ValueError(f'data_subscription must be one of {sorted(allowed)}')
        return value

    @field_validator('primary_symbol')
    def validate_primary_symbol(cls, value: Optional[str]):
        if value is None:
            return value
        value = value.strip().upper()
        return value or None

    @field_validator('position_add_cooldown_minutes', 'close_before_close_minutes')
    def validate_positive_minutes(cls, value: Optional[int]):
        if value is not None and value < 0:
            raise ValueError('minute values must be >= 0 when provided')
        return value

    @field_validator('sl_type', 'tp_type')
    def validate_sl_tp_type(cls, value: str):
        allowed = {'amount', 'percent', 'trailing'}
        text = (value or '').strip().lower()
        if text not in allowed:
            raise ValueError(f'sl/tp type must be one of {sorted(allowed)}')
        return text

    @field_validator('sl_atr_params')
    def validate_sl_atr_params(cls, value: Optional[Dict[str, Any]]):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError('sl_atr_params must be a mapping when provided')
        cleaned: Dict[str, Any] = {}
        for key, raw in value.items():
            if raw is None:
                continue
            try:
                cleaned[key] = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f'sl_atr_params values must be numeric, got {key}={raw!r}')
        return cleaned or None

class StrategyRenameRequest(BaseModel):
    new_name: str
    rename_file: Optional[bool] = False

class StrategyDataPushRequest(BaseModel):
    enabled: bool
    subscription: Optional[str] = None
    symbol: Optional[str] = None
    force_restart: Optional[bool] = False

    @field_validator('subscription')
    def validate_subscription(cls, value: Optional[str]):
        if value is None:
            return value
        allowed = {"market_data", "dom_trend"}
        if value not in allowed:
            raise ValueError(f'subscription must be one of {sorted(allowed)}')
        return value

    @field_validator('symbol')
    def normalize_symbol(cls, value: Optional[str]):
        if value is None:
            return value
        value = value.strip().upper()
        return value or None

class StrategySettingsRequest(BaseModel):
    """策略设置请求模型"""

    api_endpoint: Optional[str] = None
    api_method: Optional[str] = None
    api_payload_template: Optional[Dict[str, Any]] = None
    strategy_path: Optional[str] = None
    enable_auth: Optional[bool] = None
    auth_token: Optional[str] = None
    pause_data_push_on_holidays: Optional[bool] = None


class StrategyRiskSettings(BaseModel):
    """策略级风控设置"""

    max_positions: Optional[int] = None
    forbid_add_on_loss: bool = True
    loss_time_threshold: Optional[int] = None  # seconds
    loss_pnl_threshold: Optional[float] = None
    notify_on_trigger: bool = True

    @field_validator('max_positions')
    def validate_max_positions(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if value < 0:
            raise ValueError('max_positions must be >= 0 when provided')
        return value

    @field_validator('loss_time_threshold')
    def validate_loss_time_threshold(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if value < 0:
            raise ValueError('loss_time_threshold must be >= 0 when provided')
        return value

    @field_validator('loss_pnl_threshold')
    def validate_loss_pnl_threshold(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return float(value)


class StrategyParameterUpdateRequest(BaseModel):
    """Update request for a single strategy parameter."""

    parameter_name: str = Field(..., alias="name")
    value: Any = None

    model_config = ConfigDict(populate_by_name=True)

# 新增：风险配置更新请求模型
class RiskConfigUpdateRequest(BaseModel):
    risk_per_trade_r: Optional[float] = None
    rv_ref_value: Optional[float] = None
    rv_ref_method: Optional[str] = None  # static/current/rolling
    vol_window: Optional[int] = None
    signal_tiers: Optional[Dict[str, float]] = None

    exec_min_volume: Optional[float] = None
    exec_max_spread: Optional[float] = None
    exec_max_slippage: Optional[float] = None

    dd_step: Optional[float] = None
    dd_max_steps: Optional[int] = None
    dd_scale: Optional[float] = None

    contract_rounding: Optional[str] = None  # none/down/up/nearest
    price_threshold: Optional[float] = None
    leverage_mnq: Optional[int] = None
    leverage_map: Optional[Dict[str, float]] = None

    cb_daily_r: Optional[float] = None
    cb_streak: Optional[int] = None

    point_value_map: Optional[Dict[str, float]] = None

    @field_validator(
        'risk_per_trade_r', 'rv_ref_value', 'exec_min_volume', 'exec_max_spread', 'exec_max_slippage',
        'dd_step', 'dd_scale', 'price_threshold', 'cb_daily_r'
    )
    def positive_or_zero(cls, v):
        if v is not None and v < 0:
            raise ValueError('numeric values must be >= 0 when provided')
        return v

    @field_validator('vol_window', 'dd_max_steps', 'leverage_mnq', 'cb_streak')
    def positive_int(cls, v):
        if v is not None and v < 0:
            raise ValueError('integer values must be >= 0 when provided')
        return v

    @field_validator('leverage_map', 'point_value_map')
    def normalize_numeric_map(cls, value):
        if value is None:
            return value
        cleaned = {}
        for key, val in value.items():
            if val is None:
                continue
            num = float(val)
            if num < 0:
                raise ValueError('map values must be >= 0 when provided')
            cleaned[str(key).upper()] = num
        return cleaned

# 新增：仓位建议请求模型
class PositionSizingRequest(BaseModel):
    symbol: str
    stop_distance: float
    rv_current: Optional[float] = None
    rv_ref: Optional[float] = None
    signal_tier: Optional[str] = 'A'
    execution_metrics: Optional[Dict[str, float]] = None

    @field_validator('symbol')
    def symbol_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('symbol cannot be empty')
        return v.strip().upper()

    @field_validator('stop_distance')
    def stop_distance_positive(cls, v):
        if v <= 0:
            raise ValueError('stop_distance must be greater than 0')
        return v

class CurrentPriceRequest(BaseModel):
    """获取当前价格请求模型"""
    symbol: str
    exchange: str = "SMART"
    
    @field_validator('symbol')
    def symbol_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError('symbol cannot be empty')
        return v.strip().upper()