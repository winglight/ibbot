import asyncio
import os
import csv
import json
import math
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from queue import Queue

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

# 为risk_manager模块创建专用logger
risk_logger = logger.bind(module="risk_manager", request_id="-")
from dotenv import load_dotenv

from .notification_manager import NotificationManager
from .database import DatabaseManager, get_db_manager
from .database import AutoOrder as DatabaseAutoOrder
from .database import RiskRule as DatabaseRiskRule

load_dotenv()

@dataclass
class AutoOrder:
    order_id: str
    symbol: str
    action: str
    quantity: int
    order_type: str
    price: Optional[float]
    status: str
    created_at: datetime
    executed_at: Optional[datetime] = None
    rule_id: Optional[str] = None
    order_source: str = "auto"  # "auto" or "manual"

class OrderType(Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    RISK_CLOSE = "risk_close"

@dataclass
class RiskRule:
    symbol: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_loss_percent: Optional[float] = None
    enabled: bool = True
    max_time_span: Optional[int] = None  # 以分钟为单位的最大持仓时间
    created_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()

@dataclass
class OrderRequest:
    order_type: OrderType
    symbol: str
    quantity: int
    price: Optional[float] = None
    reason: str = ""
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

@dataclass
class PositionSizingResult:
    contracts: int
    scale_vol: float
    scale_dd: float
    scale_sig: float
    scale_exec: float
    scale_total: float
    R: float  # base risk budget in currency
    R_star: float  # risk after vol scaling
    stop_distance: float
    point_value: float
    explanation: str = ""

class RiskManager:
    def __init__(self, notification_manager: NotificationManager):
        self.notification_manager = notification_manager
        self.db_manager = get_db_manager()
        
        # 内存中的缓存，从数据库加载
        self.risk_rules: Dict[str, RiskRule] = {}
        self.auto_orders: Dict[str, AutoOrder] = {}
        
        # 订单队列
        self.order_queue = Queue()
        
        # 调度器
        self.scheduler: Optional[AsyncIOScheduler] = None

        # 组件依赖
        self.order_manager = None

        # 监控状态
        self.monitoring_active = False
        
        # 记录已警告的订单状态倒退，避免重复警告
        self.warned_order_downgrades = set()
        
        # 数据目录
        self.data_dir = "data"
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 风险阈值配置
        self.loss_threshold_1 = 0.03  # 3% 损失阈值，触发第一级警告
        self.loss_threshold_2 = 0.08  # 8% 损失阈值，触发自动平仓
        
        # 价格阈值与杠杆配置
        self.price_threshold = float(os.getenv('PRICE_THRESHOLD', 0.02))  # 从环境变量读取，默认2%金额阈值
        self.leverage_mnq = int(os.getenv('LEVERAGE_MNQ', 20))  # MNQ杠杆倍数，从环境变量读取
        
        # 动态仓位管理与断路器配置
        self.risk_per_trade_r = float(os.getenv('RISK_PER_TRADE_R', 0.005))
        self.rv_ref_value = float(os.getenv('RV_REF_VALUE', 1.0))
        self.signal_tiers: Dict[str, float] = {
            'A': 1.0, 'B': 0.75, 'C': 0.5, 'D': 0.25
        }
        self.exec_min_volume: Optional[float] = None
        self.exec_max_spread: Optional[float] = None
        self.exec_max_slippage: Optional[float] = None

        # 新增：波动率与点值运行时配置
        self.vol_window: int = int(os.getenv('VOL_WINDOW', 20))
        self.rv_ref_method: str = os.getenv('RV_REF_METHOD', 'static').lower()  # static|rolling|current
        self.point_value_map: Dict[str, float] = {
            'MNQ': 2.0,
            'MES': 5.0,
            'NQ': 20.0,
            'ES': 50.0,
        }

        # 回撤分级缩放
        self.dd_step: float = float(os.getenv('DD_STEP', 0.05))
        self.dd_max_steps: int = int(os.getenv('DD_MAX_STEPS', 4))
        self.dd_scale: float = float(os.getenv('DD_SCALE', 0.5))
        self.dd_steps_applied: int = 0

        # 合约数量舍入
        self.contract_rounding: str = os.getenv('CONTRACT_ROUNDING', 'floor').lower()

        # 权益与风控状态
        self.equity_last: float = 0.0
        self.equity_peak: float = 0.0
        self.sod_equity: float = 0.0
        self.today_date = datetime.now().date()
        self.daily_loss_currency: float = 0.0
        self.daily_loss_R: float = 0.0
        self.loss_streak: int = 0

        # 断路器状态
        self.circuit_active: bool = False
        self.circuit_reason: str = ''
        self.cb_daily_r: float = float(os.getenv('CB_DAILY_R', 3.0))
        self.cb_streak: int = int(os.getenv('CB_STREAK', 3))
        
        # 待处理的限价订单状态
        self.pending_orders: Dict[str, Dict[str, bool]] = {}
        
        # 警告历史记录
        self.alert_history: Dict[str, Dict[str, datetime]] = {}
        
        # 从数据库加载现有数据
        self._load_from_database()

    def _load_from_database(self):
        """从数据库加载风险规则和订单记录到内存缓存"""
        try:
            # 加载风险规则
            db_rules = self.db_manager.load_risk_rules()
            for rule_db in db_rules:
                rule = RiskRule(
                    symbol=rule_db.symbol,
                    stop_loss_price=rule_db.stop_loss_price,
                    take_profit_price=rule_db.take_profit_price,
                    max_loss_percent=rule_db.max_loss_percent,
                    enabled=getattr(rule_db, 'enabled', True),  # 从数据库读取enabled
                    max_time_span=getattr(rule_db, 'max_time_span', None),
                    created_at=datetime.fromisoformat(rule_db.timestamp) if rule_db.timestamp else datetime.now()
                )
                self.risk_rules[rule.symbol] = rule
            
        except Exception as e:
            risk_logger.error(f"Error loading data from database: {e}")

    # 新增：从数据库加载风控运行时配置
    def _load_risk_config_from_db(self) -> None:
        try:
            settings = self.db_manager.load_strategy_config() or {}
            cfg = settings.get('risk_config') or {}

            # 读取及回退默认
            self.vol_window = int(cfg.get('vol_window', self.vol_window))
            self.rv_ref_method = str(cfg.get('rv_ref_method', self.rv_ref_method)).lower()
            self.risk_per_trade_r = float(cfg.get('risk_per_trade_r', self.risk_per_trade_r))
            self.rv_ref_value = float(cfg.get('rv_ref_value', self.rv_ref_value))

            st = cfg.get('signal_tiers')
            if isinstance(st, dict) and st:
                new_st: Dict[str, float] = {}
                for k, v in st.items():
                    try:
                        new_st[str(k).upper()] = float(v)
                    except Exception:
                        continue
                if new_st:
                    self.signal_tiers.update(new_st)

            # 执行约束
            mv = cfg.get('exec_min_volume', self.exec_min_volume)
            self.exec_min_volume = float(mv) if mv is not None else None
            ms = cfg.get('exec_max_spread', self.exec_max_spread)
            self.exec_max_spread = float(ms) if ms is not None else None
            sl = cfg.get('exec_max_slippage', self.exec_max_slippage)
            self.exec_max_slippage = float(sl) if sl is not None else None

            # 回撤分级缩放与舍入
            self.dd_step = float(cfg.get('dd_step', self.dd_step))
            self.dd_max_steps = int(cfg.get('dd_max_steps', self.dd_max_steps))
            self.dd_scale = float(cfg.get('dd_scale', self.dd_scale))
            self.contract_rounding = str(cfg.get('contract_rounding', self.contract_rounding)).lower()

            # 断路器与其他
            self.cb_daily_r = float(cfg.get('cb_daily_r', self.cb_daily_r))
            self.cb_streak = int(cfg.get('cb_streak', self.cb_streak))
            self.price_threshold = float(cfg.get('price_threshold', self.price_threshold))
            self.leverage_mnq = int(cfg.get('leverage_mnq', self.leverage_mnq))

            # 点值映射
            pv_map = cfg.get('point_value_map')
            if isinstance(pv_map, dict):
                cleaned: Dict[str, float] = {}
                for k, v in pv_map.items():
                    try:
                        cleaned[(k or '').upper()] = float(v)
                    except Exception:
                        continue
                if cleaned:
                    self.point_value_map.update(cleaned)

            risk_logger.info("Loaded risk runtime config from database")
        except Exception as e:
            risk_logger.warning(f"Failed to load risk_config: {e}")

    # 新增：对外获取风控配置
    def get_config(self) -> Dict[str, Any]:
        try:
            return {
                'risk_per_trade_r': self.risk_per_trade_r,
                'rv_ref_value': self.rv_ref_value,
                'rv_ref_method': self.rv_ref_method,
                'vol_window': self.vol_window,
                'signal_tiers': self.signal_tiers,
                'exec_min_volume': self.exec_min_volume,
                'exec_max_spread': self.exec_max_spread,
                'exec_max_slippage': self.exec_max_slippage,
                'dd_step': self.dd_step,
                'dd_max_steps': self.dd_max_steps,
                'dd_scale': self.dd_scale,
                'contract_rounding': self.contract_rounding,
                'price_threshold': self.price_threshold,
                'leverage_mnq': self.leverage_mnq,
                'cb_daily_r': self.cb_daily_r,
                'cb_streak': self.cb_streak,
                'point_value_map': self.point_value_map,
            }
        except Exception as e:
            risk_logger.error(f"get_config error: {e}")
            return {}

    # 新增：更新风控配置并持久化
    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        try:
            # 简单校验/转换
            def as_float(name: str, default: Optional[float]):
                if name in updates and updates[name] is not None:
                    return float(updates[name])
                return default
            def as_int(name: str, default: Optional[int]):
                if name in updates and updates[name] is not None:
                    return int(updates[name])
                return default
            def as_str(name: str, default: Optional[str]):
                if name in updates and updates[name] is not None:
                    return str(updates[name])
                return default

            self.risk_per_trade_r = as_float('risk_per_trade_r', self.risk_per_trade_r)
            self.rv_ref_value = as_float('rv_ref_value', self.rv_ref_value)
            self.rv_ref_method = as_str('rv_ref_method', self.rv_ref_method).lower()
            self.vol_window = as_int('vol_window', self.vol_window)

            # tiers 合并（大写key）
            if isinstance(updates.get('signal_tiers'), dict):
                st_new: Dict[str, float] = {}
                for k, v in updates['signal_tiers'].items():
                    try:
                        st_new[str(k).upper()] = float(v)
                    except Exception:
                        continue
                if st_new:
                    self.signal_tiers.update(st_new)

            # 执行约束
            self.exec_min_volume = as_float('exec_min_volume', self.exec_min_volume)
            self.exec_max_spread = as_float('exec_max_spread', self.exec_max_spread)
            self.exec_max_slippage = as_float('exec_max_slippage', self.exec_max_slippage)

            # 回撤与舍入
            self.dd_step = as_float('dd_step', self.dd_step)
            self.dd_max_steps = as_int('dd_max_steps', self.dd_max_steps)
            self.dd_scale = as_float('dd_scale', self.dd_scale)
            self.contract_rounding = as_str('contract_rounding', self.contract_rounding).lower()

            # 断路器与其他
            self.cb_daily_r = as_float('cb_daily_r', self.cb_daily_r)
            self.cb_streak = as_int('cb_streak', self.cb_streak)
            self.price_threshold = as_float('price_threshold', self.price_threshold)
            self.leverage_mnq = as_int('leverage_mnq', self.leverage_mnq)

            # 点值映射合并
            if isinstance(updates.get('point_value_map'), dict):
                pv_new: Dict[str, float] = {}
                for k, v in updates['point_value_map'].items():
                    try:
                        pv_new[(k or '').upper()] = float(v)
                    except Exception:
                        continue
                if pv_new:
                    self.point_value_map.update(pv_new)

            # 持久化到数据库（使用 config 表，strategy_settings 键下的 risk_config 命名空间）
            settings = self.db_manager.load_strategy_config() or {}
            settings['risk_config'] = self.get_config()
            self.db_manager.save_strategy_config(settings)

            return self.get_config()
        except Exception as e:
            risk_logger.error(f"update_config error: {e}")
            return self.get_config()

    def start(self):
        """启动风险管理器"""
        try:
            # 必须在已有运行事件循环中启动
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                risk_logger.error("RiskManager.start() 需要在正在运行的事件循环中调用，未启动调度器")
                return

            # 确保调度器与当前事件循环绑定
            if self.scheduler is None or not getattr(self.scheduler, 'running', False):
                self.scheduler = AsyncIOScheduler(event_loop=loop)
                risk_logger.debug(f"AsyncIOScheduler 绑定事件循环: {loop}")
                self._start_scheduler()
            else:
                risk_logger.info("Risk manager scheduler 已经在运行")

            self.monitoring_active = True
            risk_logger.info("Risk monitoring started successfully")
        except Exception as e:
            risk_logger.error(f"Error starting risk manager: {e}")
    
    def _start_scheduler(self):
        """启动调度器和添加任务"""
        # 若尚未创建，则绑定到当前事件循环
        if self.scheduler is None:
            loop = asyncio.get_running_loop()
            self.scheduler = AsyncIOScheduler(event_loop=loop)
            risk_logger.debug(f"为_start_scheduler创建AsyncIOScheduler并绑定事件循环: {loop}")
        # 启动调度器
        if not self.scheduler.running:
            self.scheduler.start()
            risk_logger.info("Risk manager scheduler started")
        
        # 添加监控任务
        self.scheduler.add_job(
            self._monitor_positions,
            'interval',
            seconds=5,  # 从15秒改为5秒，大幅提高检查频率
            id='position_monitor',
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=10,
            max_instances=1
        )
        
        # 添加处理订单队列任务
        self.scheduler.add_job(
            self.process_pending_orders,
            'interval',
            seconds=5,
            id='order_processor',
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=10,
            max_instances=1
        )
        
        # 添加每日汇总任务（每天早上9点）
        self.scheduler.add_job(
            self._send_daily_summary,
            'cron',
            hour=9,
            minute=0,
            id='daily_summary',
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=60,
            max_instances=1
        )
        
        # 添加市价单自动同步任务（每1秒）
        self.scheduler.add_job(
            self._auto_sync_market_orders,
            'interval',
            seconds=1,
            id='market_order_sync',
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=5,
            max_instances=1
        )

    def stop(self):
        """停止风险管理器"""
        try:
            self.monitoring_active = False
            if self.scheduler:
                try:
                    if self.scheduler.running:
                        self.scheduler.remove_all_jobs()
                        self.scheduler.shutdown(wait=False)
                finally:
                    self.scheduler = None
            risk_logger.info("Risk manager stopped")
        except Exception as e:
            risk_logger.error(f"Error stopping risk manager: {e}")

    def add_risk_rule(self, symbol: str, stop_loss_price: Optional[float] = None,
                      take_profit_price: Optional[float] = None, max_loss_percent: Optional[float] = None,
                      max_time_span: Optional[int] = None):
        """添加风险规则"""
        try:
            risk_logger.info(f"add_risk_rule called: symbol={symbol}, SL={stop_loss_price}, TP={take_profit_price}, max_loss%={max_loss_percent}, max_time_span={max_time_span}")
            rule = RiskRule(
                symbol=symbol,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                max_loss_percent=max_loss_percent,
                enabled=True,
                max_time_span=max_time_span,
                created_at=datetime.now()
            )
            
            # 转换为数据库层的 RiskRule 并保存到数据库
            db_rule = DatabaseRiskRule(
                symbol=rule.symbol,
                stop_loss_price=rule.stop_loss_price,
                take_profit_price=rule.take_profit_price,
                max_loss_percent=rule.max_loss_percent,
                enabled=rule.enabled,
                max_time_span=rule.max_time_span,
                timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
            )
            self.db_manager.save_risk_rule(db_rule)
            risk_logger.debug(f"Risk rule persisted: {db_rule}")
            
            # 更新内存缓存
            self.risk_rules[symbol] = rule
            
            risk_logger.info(f"Risk rule added for {symbol}")
            return True
        except Exception as e:
            risk_logger.error(f"Error adding risk rule: {e}")
            return False

    def remove_risk_rule(self, symbol: str):
        """删除风险规则"""
        try:
            risk_logger.info(f"remove_risk_rule called: symbol={symbol}")
            # 从数据库删除
            self.db_manager.delete_risk_rule(symbol)
            
            # 从内存删除
            if symbol in self.risk_rules:
                del self.risk_rules[symbol]
                risk_logger.info(f"Risk rule removed for {symbol}")
                return True
            else:
                risk_logger.warning(f"Risk rule for {symbol} not found")
                return False
        except Exception as e:
            risk_logger.error(f"Error removing risk rule: {e}")
            return False

    def update_risk_rule(self, symbol: str, stop_loss_price: float = None,
                         take_profit_price: float = None, enabled: bool = True,
                         max_time_span: Optional[int] = None) -> bool:
        """更新风险规则"""
        try:
            risk_logger.info(f"update_risk_rule called: symbol={symbol}, SL={stop_loss_price}, TP={take_profit_price}, enabled={enabled}, max_time_span={max_time_span}")
            if symbol in self.risk_rules:
                rule = self.risk_rules[symbol]
                if stop_loss_price is not None:
                    risk_logger.debug(f"Updating SL for {symbol}: {rule.stop_loss_price} -> {stop_loss_price}")
                    rule.stop_loss_price = stop_loss_price
                if take_profit_price is not None:
                    risk_logger.debug(f"Updating TP for {symbol}: {rule.take_profit_price} -> {take_profit_price}")
                    rule.take_profit_price = take_profit_price
                if rule.enabled != enabled:
                    risk_logger.debug(f"Updating enabled for {symbol}: {rule.enabled} -> {enabled}")
                rule.enabled = enabled
                if max_time_span is not None:
                    rule.max_time_span = max_time_span
                
                # 转换为数据库层的 RiskRule 并保存到数据库
                db_rule = DatabaseRiskRule(
                    symbol=rule.symbol,
                    stop_loss_price=rule.stop_loss_price,
                    take_profit_price=rule.take_profit_price,
                    max_loss_percent=rule.max_loss_percent,
                    enabled=rule.enabled,
                    max_time_span=rule.max_time_span,
                    timestamp=rule.created_at.isoformat()
                )
                self.db_manager.save_risk_rule(db_rule)
                risk_logger.debug(f"Risk rule persisted after update: {db_rule}")
                
                risk_logger.info(f"Risk rule updated for {symbol}")
                return True
            else:
                risk_logger.warning(f"Risk rule for {symbol} not found")
                return False
        except Exception as e:
            risk_logger.error(f"Error updating risk rule for {symbol}: {e}")
            return False

    def get_risk_rules(self) -> Dict[str, RiskRule]:
        """获取所有风险规则"""
        return self.risk_rules.copy()

    async def _monitor_positions(self):
        """监控持仓风险"""
        try:
            import core  # 延迟导入避免循环导入
            if not core.ibkr_client or not getattr(core.ibkr_client, 'connected', False):
                risk_logger.warning("IBKR client not ready, skipping position monitoring")
                return
            
            risk_logger.info("=== Starting position monitoring cycle ===")
            
            # 设置fast=False确保获取最新实时价格，而不是使用缓存
            # get_positions 是异步方法，直接await调用
            # get_account_info 是同步阻塞方法，使用 asyncio.to_thread 在后台线程执行
            positions = await core.ibkr_client.get_positions(fast=False)
            try:
                account_info = await asyncio.to_thread(core.ibkr_client.get_account_info)
            except Exception as e:
                risk_logger.warning(f"Failed to get account info: {e}")
                account_info = {}
            
            risk_logger.info(f"Found {len(positions)} positions to monitor")
            risk_logger.info(f"Active risk rules: {len([r for r in self.risk_rules.values() if r.enabled])}")
            
            for position in positions:
                symbol = position['symbol']
                current_price = position['current_price']
                risk_logger.info(f"Checking position: {symbol} @ ${current_price:.2f}, qty: {position['position']}")
                
                await self._check_position_risk(position, account_info)
                await self._check_stop_loss_take_profit(position)
            
            # 检查是否有持仓为0但风险规则仍激活的资产，如果有则反激活风险规则
            position_symbols = {pos['symbol'] for pos in positions if pos['position'] != 0}
            for symbol, rule in list(self.risk_rules.items()):
                if rule.enabled and symbol not in position_symbols:
                    risk_logger.info(f"Position for {symbol} is 0 but risk rule is still active, deactivating rule")
                    rule.enabled = False
                    # 更新数据库中的风险规则状态
                    db_rule = DatabaseRiskRule(
                        symbol=rule.symbol,
                        stop_loss_price=rule.stop_loss_price,
                        take_profit_price=rule.take_profit_price,
                        max_loss_percent=rule.max_loss_percent,
                        enabled=rule.enabled,
                        max_time_span=rule.max_time_span,
                        timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                    )
                    self.db_manager.save_risk_rule(db_rule)
                    risk_logger.info(f"Risk rule for {symbol} has been deactivated due to zero position")
            
            risk_logger.info("=== Position monitoring cycle completed ===")
            
        except Exception as e:
            risk_logger.error(f"Error monitoring positions: {e}")
    
    async def _check_position_risk(self, position: Dict, account_info: Dict):
        """检查持仓风险"""
        symbol = position['symbol']
        loss_percent = abs(position['unrealized_pnl_percent']) / 100
        
        # Check if position is in loss
        if position['unrealized_pnl'] >= 0:
            return
        
        current_time = datetime.now()
        
        # Check first threshold (notification)
        if loss_percent >= self.loss_threshold_1:
            alert_key = f"{symbol}_threshold1"
            last_alert = self.alert_history.get(symbol, {}).get('threshold1')
            
            # Send alert only once per hour to avoid spam
            if not last_alert or (current_time - last_alert).seconds > 3600:
                msg = (
                    f"价格触发亏损阈值提醒\n"
                    f"当前持仓: {position['position']}\n"
                    f"当前价格: ${position['current_price']:.2f}\n"
                    f"平均成本: ${position['avg_cost']:.2f}\n"
                    f"亏损比例: {loss_percent*100:.2f}% (阈值: {self.loss_threshold_1*100:.2f}%)"
                )
                await self.notification_manager.send_risk_notification(
                    symbol=symbol,
                    message=msg,
                    risk_type="loss_threshold1"
                )
                
                if symbol not in self.alert_history:
                    self.alert_history[symbol] = {}
                self.alert_history[symbol]['threshold1'] = current_time
        
        # Check second threshold (auto close)
        if loss_percent >= self.loss_threshold_2:
            alert_key = f"{symbol}_threshold2"
            last_alert = self.alert_history.get(symbol, {}).get('threshold2')
            
            # Auto close only once
            if not last_alert:
                # Check available funds before closing
                if self._check_sufficient_funds(account_info, position):
                    order_request = OrderRequest(
                        order_type=OrderType.RISK_CLOSE,
                        symbol=symbol,
                        quantity=abs(int(position['position'])),
                        reason=f"Auto close due to {loss_percent*100:.2f}% loss"
                    )
                    self.order_queue.put(order_request)
                    
                    if symbol not in self.alert_history:
                        self.alert_history[symbol] = {}
                    self.alert_history[symbol]['threshold2'] = current_time
                    
                    risk_logger.warning(f"Auto close triggered for {symbol} due to {loss_percent*100:.2f}% loss")
    
    async def _check_stop_loss_take_profit(self, position: Dict):
        """检查止损止盈"""
        symbol = position['symbol']
        
        # 获取最新实时价格，而不是使用position中的价格
        try:
            # 尝试在后台线程获取最新实时价格，避免阻塞事件循环
            real_time_data = await asyncio.to_thread(core.ibkr_client.get_real_time_data, symbol)
            price = None
            if real_time_data:
                # get_real_time_data 返回的键为 'last'
                price = real_time_data.get('price') or real_time_data.get('last')
            if price:
                current_price = price
                risk_logger.info(f"Using real-time price for {symbol}: ${current_price:.2f}")
            else:
                current_price = position['current_price']
                risk_logger.info(f"Using position price for {symbol}: ${current_price:.2f}")
        except Exception as e:
            # 如果获取实时价格失败，则使用position中的价格
            current_price = position['current_price']
            risk_logger.error(f"Error getting real-time price for {symbol}: {e}, using position price: ${current_price:.2f}")
        
        if symbol not in self.risk_rules:
            risk_logger.debug(f"No risk rule found for {symbol}")
            return
        
        rule = self.risk_rules[symbol]
        if not rule.enabled:
            risk_logger.debug(f"Risk rule for {symbol} is disabled")
            return
        
        position_qty = int(position['position'])
        if position_qty == 0:
            risk_logger.debug(f"No position for {symbol}, disabling rule")
            rule.enabled = False
            # 更新数据库中的风险规则状态
            db_rule = DatabaseRiskRule(
                symbol=rule.symbol,
                stop_loss_price=rule.stop_loss_price,
                take_profit_price=rule.take_profit_price,
                max_loss_percent=rule.max_loss_percent,
                enabled=rule.enabled,
                max_time_span=rule.max_time_span,
                timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
            )
            self.db_manager.save_risk_rule(db_rule)
            return
        
        risk_logger.info(f"Checking risk rule for {symbol}: SL=${rule.stop_loss_price}, TP=${rule.take_profit_price}")
        
        # Initialize pending orders tracking for this symbol
        if symbol not in self.pending_orders:
            self.pending_orders[symbol] = {"stop_loss": False, "take_profit": False}
        
        # 以金额阈值为基础，按杠杆换算成价格差阈值
        amount_threshold = self.price_threshold
        threshold_price = amount_threshold * self.leverage_mnq if symbol == 'MNQ' else amount_threshold
        
        # 检查是否已有任何limit order pending，如果有则跳过
        if self.pending_orders[symbol]["stop_loss"] or self.pending_orders[symbol]["take_profit"]:
            risk_logger.debug(f"Already have pending limit order for {symbol}, skipping new order placement")
            return
        
        # 标记是否已下单，确保只下一个单
        order_placed = False
        
        # Prefer: Take Profit has higher priority than Stop Loss when both trigger simultaneously
        # Check take profit first
        if rule.take_profit_price and not order_placed:
            # 对于止盈：多头持仓时价格应该上涨到目标价格，空头持仓时价格应该下跌到目标价格
            if position_qty > 0:  # 多头持仓
                price_diff = current_price - rule.take_profit_price  # 当前价格 - 止盈价格
                # 修复：多头持仓当前价格>=止盈价格时立即触发
                should_trigger = current_price >= rule.take_profit_price
            else:  # 空头持仓
                price_diff = rule.take_profit_price - current_price  # 止盈价格 - 当前价格
                # 修复：空头持仓当前价格<=止盈价格时立即触发
                should_trigger = current_price <= rule.take_profit_price
            
            risk_logger.info(f"Take profit check for {symbol}: current=${current_price:.2f}, target=${rule.take_profit_price:.2f}, price_diff=${price_diff:.4f}, should_trigger={should_trigger}, position_qty={position_qty}")
            
            # If price reaches take profit level, place market order
            if should_trigger:
                risk_logger.info(f"Price reaches take profit for {symbol}, placing market order")
                try:
                    action = 'SELL' if position_qty > 0 else 'BUY'
                    # 直接调用下单方法，避免嵌套的事件循环调用
                    trade = core.ibkr_client.place_market_order(
                        symbol=symbol,
                        quantity=abs(position_qty),
                        action=action
                    )
                    
                    if trade:
                        self.pending_orders[symbol]["take_profit"] = True
                        order_placed = True
                        
                        # 保存订单记录到auto_orders内存和数据库
                        auto_order = AutoOrder(
                            order_id=str(trade.order.orderId),
                            symbol=symbol,
                            action=action,
                            quantity=abs(position_qty),
                            order_type="MKT",
                            price=current_price,  # 使用当前市价
                            status="Submitted",
                            created_at=datetime.now(),
                            executed_at=None,
                            rule_id=symbol,
                            order_source="tp"
                        )
                        self.auto_orders[str(trade.order.orderId)] = auto_order
                        # 保存到数据库，保证持久化
                        self.save_auto_order_to_db(auto_order)
                        
                        risk_logger.info(f"✅ Market order placed for take profit: {symbol} at market price ${current_price:.2f}")
                        risk_logger.info(f"Take profit market order successfully placed for {symbol}, rule remains active")
                        
                        # Send notification via NotificationManager
                        await self.notification_manager.send_stop_loss_notification(
                            symbol=symbol,
                            position=position_qty,
                            trigger_price=current_price,
                            order_type="profit",
                            order_id=trade.order.orderId
                        )
                        
                        # Disable rule and persist, then stop further checks
                        rule.enabled = False
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            max_time_span=rule.max_time_span,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                        )
                        self.db_manager.save_risk_rule(db_rule)
                        risk_logger.info(f"Take profit rule disabled after market order for {symbol}")
                        return
                    else:
                        risk_logger.warning(f"Failed to place take profit market order for {symbol}")
                        # 下单失败时禁用规则
                        rule.enabled = False
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            max_time_span=rule.max_time_span,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                        )
                        self.db_manager.save_risk_rule(db_rule)
                        risk_logger.info(f"Take profit rule disabled due to order failure for {symbol}")
                except Exception as e:
                    risk_logger.error(f"Failed to place take profit market order for {symbol}: {e}")
                    # 下单异常时禁用规则
                    rule.enabled = False
                    db_rule = DatabaseRiskRule(
                        symbol=rule.symbol,
                        stop_loss_price=rule.stop_loss_price,
                        take_profit_price=rule.take_profit_price,
                        max_loss_percent=rule.max_loss_percent,
                        enabled=rule.enabled,
                        max_time_span=rule.max_time_span,
                        timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                    )
                    self.db_manager.save_risk_rule(db_rule)
                    risk_logger.info(f"Take profit rule disabled due to exception for {symbol}")
            
            # If price has reached take profit, execute market order
            # 修复：区分多头和空头持仓的止盈触发条件
            take_profit_triggered = False
            if position_qty > 0:  # 多头持仓：当前价格 >= 止盈价格
                take_profit_triggered = current_price >= rule.take_profit_price
                trigger_msg = f"${current_price:.2f} >= ${rule.take_profit_price:.2f}"
            else:  # 空头持仓：当前价格 <= 止盈价格
                take_profit_triggered = current_price <= rule.take_profit_price
                trigger_msg = f"${current_price:.2f} <= ${rule.take_profit_price:.2f}"
            
            if take_profit_triggered:
                risk_logger.warning(f"🎯 Take profit triggered for {symbol}: {trigger_msg}")
                
                # Check position direction before placing order
                action = 'SELL' if position_qty > 0 else 'BUY'
                
                order_request = OrderRequest(
                    order_type=OrderType.TAKE_PROFIT,
                    symbol=symbol,
                    quantity=abs(position_qty),
                    reason=f"Take profit triggered at ${current_price:.2f}"
                )
                self.order_queue.put(order_request)
                
                # Disable rule after trigger and update database
                rule.enabled = False
                db_rule = DatabaseRiskRule(
                    symbol=rule.symbol,
                    stop_loss_price=rule.stop_loss_price,
                    take_profit_price=rule.take_profit_price,
                    max_loss_percent=rule.max_loss_percent,
                    enabled=rule.enabled,
                    max_time_span=rule.max_time_span,
                    timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                )
                self.db_manager.save_risk_rule(db_rule)
                risk_logger.info(f"Take profit order queued for {symbol} at ${current_price:.2f}, rule disabled")
                return  # 当触发止盈市价单时立即返回，不再检查止损

        # Check stop loss
        if rule.stop_loss_price and not order_placed:
            # 对于止损：多头持仓时价格应该下跌到目标价格，空头持仓时价格应该上涨到目标价格
            if position_qty > 0:  # 多头持仓
                price_diff = rule.stop_loss_price - current_price  # 止损价格 - 当前价格
                # 修复：多头持仓当前价格<=止损价格时立即触发，或者价格差在1以内
                should_trigger = current_price <= rule.stop_loss_price or (price_diff > 0 and price_diff <= 1)
            else:  # 空头持仓
                price_diff = current_price - rule.stop_loss_price  # 当前价格 - 止损价格
                # 修复：空头持仓当前价格>=止损价格时立即触发，或者价格差在1以内
                should_trigger = current_price >= rule.stop_loss_price or (price_diff > 0 and price_diff <= 1)
            
            risk_logger.warning(f"⚠️ Stop loss check for {symbol}: current=${current_price:.2f}, target=${rule.stop_loss_price:.2f}, price_diff=${price_diff:.4f}, should_trigger={should_trigger}, position_qty={position_qty}")
            
            # If price reaches stop loss level, place market order immediately
            if should_trigger:
                risk_logger.warning(f"🚨 STOP LOSS TRIGGERED for {symbol} at ${current_price:.2f}, placing market order immediately")
                try:
                    action = 'SELL' if position_qty > 0 else 'BUY'
                    # 直接调用下单方法，避免嵌套的事件循环调用
                    trade = core.ibkr_client.place_market_order(
                        symbol=symbol,
                        quantity=abs(position_qty),
                        action=action
                    )
                    
                    if trade:
                        self.pending_orders[symbol]["stop_loss"] = True
                        
                        # 发送统一通知
                        try:
                            await self.notification_manager.send_stop_loss_notification(
                                symbol=symbol,
                                position=position_qty,
                                trigger_price=current_price,
                                order_type="loss",
                                order_id=trade.order.orderId
                            )
                        except Exception as e:
                            risk_logger.error(f"Failed to send stop-loss notification: {e}")
                        order_placed = True
                        
                        # 保存订单记录到auto_orders内存和数据库
                        auto_order = AutoOrder(
                            order_id=str(trade.order.orderId),
                            symbol=symbol,
                            action=action,
                            quantity=abs(position_qty),
                            order_type="MKT",
                            price=current_price,  # 使用当前市价
                            status="Submitted",
                            created_at=datetime.now(),
                            executed_at=None,
                            rule_id=symbol,
                            order_source="sl"
                        )
                        self.auto_orders[str(trade.order.orderId)] = auto_order
                        self.save_auto_order_to_db(auto_order)
                        
                        risk_logger.info(f"✅ Market order placed for stop loss: {symbol} at market price ${current_price:.2f}")
                        
                        # 禁用规则并更新数据库
                        rule.enabled = False
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            max_time_span=rule.max_time_span,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                        )
                        self.db_manager.save_risk_rule(db_rule)
                        risk_logger.warning(f"🔒 Stop loss rule disabled for {symbol} after triggering at ${current_price:.2f}")
                        
                        # Send unified notification and return to avoid duplicate processing
                        try:
                            await self.notification_manager.send_stop_loss_notification(
                                symbol=symbol,
                                position=position_qty,
                                trigger_price=current_price,
                                order_type="loss",
                                order_id=trade.order.orderId
                            )
                        except Exception as e:
                            risk_logger.error(f"Failed to send stop-loss notification: {e}")
                        return
                    else:
                        risk_logger.warning(f"Failed to place stop loss market order for {symbol}")
                        # 下单失败时禁用规则
                        rule.enabled = False
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            max_time_span=rule.max_time_span,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                        )
                        self.db_manager.save_risk_rule(db_rule)
                        risk_logger.info(f"Stop loss rule disabled due to order failure for {symbol}")
                except Exception as e:
                    risk_logger.error(f"Failed to place stop loss market order for {symbol}: {e}")
                    # 下单异常时禁用规则
                    rule.enabled = False
                    db_rule = DatabaseRiskRule(
                        symbol=rule.symbol,
                        stop_loss_price=rule.stop_loss_price,
                        take_profit_price=rule.take_profit_price,
                        max_loss_percent=rule.max_loss_percent,
                        enabled=rule.enabled,
                        max_time_span=rule.max_time_span,
                        timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                    )
                    self.db_manager.save_risk_rule(db_rule)
                    risk_logger.info(f"Stop loss rule disabled due to exception for {symbol}")
            
            # If price has reached stop loss, execute market order
            # 修复：区分多头和空头持仓的止损触发条件
            stop_loss_triggered = False
            if position_qty > 0:  # 多头持仓：当前价格 <= 止损价格
                stop_loss_triggered = current_price <= rule.stop_loss_price
                trigger_msg = f"${current_price:.2f} <= ${rule.stop_loss_price:.2f}"
            else:  # 空头持仓：当前价格 >= 止损价格
                stop_loss_triggered = current_price >= rule.stop_loss_price
                trigger_msg = f"${current_price:.2f} >= ${rule.stop_loss_price:.2f}"
            
            if stop_loss_triggered:
                risk_logger.warning(f"🚨 Stop loss triggered for {symbol}: {trigger_msg}")
                
                # Check position direction before placing order
                action = 'SELL' if position_qty > 0 else 'BUY'
                
                order_request = OrderRequest(
                    order_type=OrderType.STOP_LOSS,
                    symbol=symbol,
                    quantity=abs(position_qty),
                    reason=f"Stop loss triggered at ${current_price:.2f}"
                )
                self.order_queue.put(order_request)
                
                # Disable rule after trigger and update database
                rule.enabled = False
                db_rule = DatabaseRiskRule(
                    symbol=rule.symbol,
                    stop_loss_price=rule.stop_loss_price,
                    take_profit_price=rule.take_profit_price,
                    max_loss_percent=rule.max_loss_percent,
                    enabled=rule.enabled,
                    max_time_span=rule.max_time_span,
                    timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                )
                self.db_manager.save_risk_rule(db_rule)
                risk_logger.info(f"Stop loss order queued for {symbol} at ${current_price:.2f}, rule disabled")
                return  # 触发市价单后立即返回，不再检查止盈
    
    def _auto_sync_market_orders(self):
        """自动同步所有活跃订单状态和成交价格（包括市价单和限价单）"""
        try:
            # 定期清理警告记录，避免内存泄漏（每100次调用清理一次）
            if len(self.warned_order_downgrades) > 100:
                self.warned_order_downgrades.clear()
                risk_logger.debug("已清理订单状态倒退警告记录")
            
            import core  # 延迟导入避免循环导入
            if not core.ibkr_client or not core.ibkr_client.is_connected():
                return
            
            # 获取订单管理器
            order_manager = getattr(self, 'order_manager', None)
            if not order_manager:
                return
            
            # 获取所有活跃状态的订单（包括Submitted、PreSubmitted、PendingSubmit等）
            active_orders = []
            active_statuses = ["Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel"]
            for order_id, order in order_manager.orders.items():
                if order.status in active_statuses:
                    active_orders.append(order)
            
            if not active_orders:
                return
            
            # 检查活跃订单状态（避免全量同步）
            try:
                synced_count = 0
                for order in active_orders:
                    # 检查订单是否在IBKR的trades中
                    order_id_int = int(order.order_id)
                    trade = None
                    
                    # 查找对应的trade
                    for t in core.ibkr_client.ib.trades():
                        if t.order.orderId == order_id_int:
                            trade = t
                            break
                    
                    if trade:
                        current_status = trade.orderStatus.status
                        # 将IBKR的Inactive状态映射为Rejected状态
                        if current_status == "Inactive":
                            current_status = "Rejected"
                        
                        # 检查状态是否需要更新，避免重复更新和状态倒退
                        if current_status != order.status:
                            # 定义状态转换的优先级（数字越大优先级越高）
                            status_priority = {
                                "PendingSubmit": 1,
                                "PreSubmitted": 2,
                                "Submitted": 3,
                                "Filled": 4,
                                "Cancelled": 4,
                                "Rejected": 4
                            }
                            
                            current_priority = status_priority.get(current_status, 0)
                            order_priority = status_priority.get(order.status, 0)
                            
                            # 只允许状态向前推进，不允许倒退
                            if current_priority >= order_priority:
                                # 更新订单状态
                                fill_price = None
                                if current_status == "Filled" and trade.orderStatus.avgFillPrice:
                                    fill_price = float(trade.orderStatus.avgFillPrice)
                                
                                order_manager.update_order_status(
                                    order.order_id,
                                    current_status,
                                    fill_price=fill_price,
                                    executed_at=datetime.now() if current_status == "Filled" else None
                                )
                                synced_count += 1
                                risk_logger.debug(f"Order {order.order_id} status updated: {order.status} -> {current_status}")
                            else:
                                # 避免重复警告同一个订单的状态倒退
                                warning_key = f"{order.order_id}_{order.status}_{current_status}"
                                if warning_key not in self.warned_order_downgrades:
                                    self.warned_order_downgrades.add(warning_key)
                                    risk_logger.warning(f"拒绝订单状态倒退: Order {order.order_id} 当前状态={order.status} (优先级{order_priority}), IBKR状态={current_status} (优先级{current_priority})")
                                    
                                    # 可选：直接从订单管理器中删除这个有问题的订单
                                    # order_manager.remove_order(order.order_id)
                                    # risk_logger.info(f"已删除状态异常的订单: {order.order_id}")
                
                if synced_count > 0:
                    risk_logger.debug(f"Auto-synced {synced_count} active orders")
                    
            except Exception as sync_err:
                risk_logger.warning(f"Failed to auto-sync market orders: {sync_err}")
                
        except Exception as e:
            risk_logger.error(f"Error in auto sync market orders: {e}")
    
    def _check_sufficient_funds(self, account_info: Dict, position: Dict) -> bool:
        """检查资金是否充足"""
        available_funds = account_info.get('available_funds', 0)
        required_funds = abs(position['position']) * position['current_price'] * 0.1  # 10% margin
        
        return available_funds >= required_funds
    
    async def process_pending_orders(self):
        """处理队列中的待处理订单"""
        processed_count = 0
        while not self.order_queue.empty():
            try:
                order_request = self.order_queue.get_nowait()
                await self._execute_order(order_request)
                processed_count += 1
            except Exception as e:
                risk_logger.error(f"Error processing order: {e}")
        
        if processed_count > 0:
            risk_logger.info(f"Processed {processed_count} pending orders")
    
    async def _execute_order(self, order_request: OrderRequest):
        """执行订单"""
        risk_logger.info(f"开始执行订单: {order_request.order_type.value} for {order_request.symbol}, quantity: {order_request.quantity}")
        
        try:
            # 检查连接状态
            if not core.ibkr_client or not getattr(core.ibkr_client, 'connected', False):
                risk_logger.error(f"Cannot execute order for {order_request.symbol}: IBKR client not ready")
                return
            
            risk_logger.debug(f"IBKR连接状态正常，开始获取持仓信息 for {order_request.symbol}")
            
            # Determine order action
            # get_positions 为同步阻塞方法，放到后台线程执行
            positions = await asyncio.to_thread(core.ibkr_client.get_positions)
            risk_logger.debug(f"获取到 {len(positions)} 个持仓")
            
            position = next((p for p in positions if p['symbol'] == order_request.symbol), None)
            
            if not position or position['position'] == 0:
                risk_logger.warning(f"No position found for {order_request.symbol}, skipping order. Available positions: {[p['symbol'] for p in positions]}")
                return
            
            risk_logger.info(f"找到持仓: {order_request.symbol}, position: {position['position']}, avg_cost: {position.get('avg_cost', 'N/A')}")
            
            # Determine action based on current position
            action = 'SELL' if position['position'] > 0 else 'BUY'
            risk_logger.info(f"确定订单动作: {action} {order_request.quantity} {order_request.symbol}")
            
            # 检查当前事件循环状态
            try:
                current_loop = asyncio.get_running_loop()
                risk_logger.debug(f"当前事件循环: {current_loop}, 是否运行中: {current_loop.is_running()}")
            except RuntimeError as e:
                risk_logger.debug(f"获取事件循环状态失败: {e}")
            
            # Place market order for immediate execution using executor to avoid event loop conflicts
            risk_logger.info(f"使用executor执行下单操作以避免事件循环冲突")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            try:
                import core  # 延迟导入避免循环导入
                trade = await loop.run_in_executor(
                    None,
                    lambda: core.ibkr_client.place_market_order(
                        symbol=order_request.symbol,
                        quantity=order_request.quantity,
                        action=action
                    )
                )
                risk_logger.info(f"下单操作完成，trade结果: {trade is not None}")
            except Exception as executor_error:
                risk_logger.error(f"Executor执行下单失败 for {order_request.symbol}: {executor_error}")
                risk_logger.error(f"错误类型: {type(executor_error).__name__}")
                import traceback
                risk_logger.error(f"错误堆栈: {traceback.format_exc()}")
                raise
            
            if trade:
                order_id = trade.order.orderId
                
                # Record auto order
                # 根据订单类型设置order_source
                if order_request.order_type == OrderType.STOP_LOSS:
                    order_source = "sl"
                elif order_request.order_type == OrderType.TAKE_PROFIT:
                    order_source = "tp"
                else:
                    order_source = "strategy"
                
                auto_order = AutoOrder(
                    order_id=str(order_id),
                    symbol=order_request.symbol,
                    action=action,
                    quantity=order_request.quantity,
                    order_type="MKT",
                    price=position['current_price'],
                    status="Submitted",
                    created_at=datetime.now(),
                    executed_at=None,
                    rule_id=order_request.symbol,  # Use symbol as rule identifier
                    order_source=order_source
                )
                
                self.auto_orders[str(order_id)] = auto_order
                self.save_auto_order_to_db(auto_order)
                
                # 同步到订单管理器
                if self.order_manager:
                    from .order_manager import Order, OrderStatus, OrderSource
                    order = Order(
                        order_id=str(order_id),
                        symbol=order_request.symbol,
                        action=action,
                        quantity=order_request.quantity,
                        order_type="MARKET",
                        price=position['current_price'],
                        status=OrderStatus.SUBMITTED.value,
                        created_at=datetime.now(),
                        order_source=order_source,
                        notes=order_request.reason
                    )
                    self.order_manager.add_order(order)
                    risk_logger.info(f"订单已同步到订单管理器: {order_id}")
                    
                    # 对于市价单，立即更新状态为已成交
                    # 市价单通常会立即成交，所以我们可以直接更新状态
                    if order.order_type == "MARKET":
                        from datetime import datetime
                        executed_at = datetime.now()
                        self.order_manager.update_order_status(
                            order_id=str(order_id),
                            status=OrderStatus.FILLED.value,
                            fill_price=position['current_price'],
                            executed_at=executed_at
                        )
                        risk_logger.info(f"市价单状态已更新为已成交: {order_id}")
                        
                        # 同时更新内存中的自动订单状态
                        auto_order.status = "Filled"
                        auto_order.executed_at = executed_at
                        self.update_order_status(str(order_id), "Filled")

                        # 触发一次佣金/PNL回填（使用executor避免阻塞事件循环）
                        try:
                            cm = getattr(core.ibkr_client, "connection_manager", None)
                            if cm is not None:
                                risk_logger.info("触发commission/PNL回填以避免市价单本地成交后缺失")
                                updated = await loop.run_in_executor(
                                    None,
                                    lambda: cm.backfill_commissions()
                                )
                                risk_logger.info(f"commission/PNL回填完成，尝试更新次数: {updated}")
                            else:
                                risk_logger.warning("未找到connection_manager，跳过commission/PNL回填")
                        except Exception as bf_err:
                            risk_logger.warning(f"commission/PNL回填失败: {bf_err}")
                else:
                    risk_logger.warning(f"订单管理器未初始化，无法同步订单: {order_id}")
                
                # Send notification based on order type (unified via NotificationManager)
                if order_request.order_type == OrderType.STOP_LOSS:
                    await self.notification_manager.send_stop_loss_notification(
                        symbol=order_request.symbol,
                        position=position['position'],
                        trigger_price=position['current_price'],
                        order_type="loss",
                        order_id=str(order_id)
                    )
                elif order_request.order_type == OrderType.TAKE_PROFIT:
                    await self.notification_manager.send_stop_loss_notification(
                        symbol=order_request.symbol,
                        position=position['position'],
                        trigger_price=position['current_price'],
                        order_type="profit",
                        order_id=str(order_id)
                    )
                elif order_request.order_type == OrderType.RISK_CLOSE:
                    await self.notification_manager.send_auto_close_notification(
                        symbol=order_request.symbol,
                        position=position['position'],
                        order_id=str(order_id)
                    )
                
                risk_logger.info(f"Order executed: {order_request.order_type.value} for {order_request.symbol}, Order ID: {order_id}")
            else:
                risk_logger.error(f"Failed to execute order for {order_request.symbol}")
                
        except Exception as e:
            risk_logger.error(f"Error executing order for {order_request.symbol}: {e}")
    
    async def _send_daily_summary(self):
        """发送每日摘要"""
        try:
            if not core.ibkr_client or not getattr(core.ibkr_client, 'connected', False):
                return
            
            account_info = await core.ibkr_client.get_account_info()
            positions = await core.ibkr_client.get_positions()
            
            await self.notification_manager.send_daily_summary_notification(account_info, positions)
            
        except Exception as e:
            risk_logger.error(f"Error sending daily summary: {e}")
    
    def get_order_queue_size(self) -> int:
        """获取订单队列大小"""
        return self.order_queue.qsize()
    
    def clear_order_queue(self):
        """清空订单队列"""
        while not self.order_queue.empty():
            try:
                self.order_queue.get_nowait()
            except:
                break
        risk_logger.info("Order queue cleared")
    
    def save_auto_order_to_db(self, order: AutoOrder):
        """保存单个订单到数据库并更新内存缓存"""
        try:
            # 创建数据库的AutoOrder实例，映射字段
            db_order = DatabaseAutoOrder(
                order_id=order.order_id,
                symbol=order.symbol,
                quantity=order.quantity,
                action=order.action,
                order_type=order.order_type,
                price=order.price,
                timestamp=order.created_at.isoformat() if order.created_at else datetime.now().isoformat(),
                exchange=getattr(order, 'exchange', 'SMART'),
                sec_type=getattr(order, 'sec_type', 'STK'),
                status=order.status if hasattr(order, 'status') else 'pending',
                executed_at=order.executed_at.isoformat() if getattr(order, 'executed_at', None) else None,
                rule_id=getattr(order, 'rule_id', None),
                order_source=getattr(order, 'order_source', 'auto')
            )
            
            # 保存到数据库
            self.db_manager.save_order(db_order)
            
            # 更新内存缓存
            self.auto_orders[order.order_id] = order
            
            risk_logger.info(f"Order {order.order_id} saved to database")
        except Exception as e:
            risk_logger.error(f"Error saving order to database: {e}")
    
    def get_auto_orders(self) -> List[Dict]:
        """获取所有订单记录列表（包含自动和手动订单）"""
        orders = []
        for order in self.auto_orders.values():
            orders.append({
                'order_id': order.order_id,
                'symbol': order.symbol,
                'action': order.action,
                'quantity': order.quantity,
                'order_type': order.order_type,
                'price': order.price,
                'status': order.status,
                'created_at': order.created_at.isoformat() if order.created_at else None,
                'executed_at': order.executed_at.isoformat() if order.executed_at else None,
                'rule_id': order.rule_id,
                'order_source': order.order_source
            })
        return orders

    def set_dependencies(self, order_manager=None):
        """注入外部依赖"""
        self.order_manager = order_manager

    def remove_order_record(self, order_id: str):
        """从记录中删除订单"""
        try:
            soft_deleted = False
            # 优先尝试通过 OrderManager 执行软删除，避免被 IBKR 重新同步
            if getattr(self, 'order_manager', None) and hasattr(self.order_manager, 'mark_order_deleted'):
                try:
                    soft_deleted = bool(self.order_manager.mark_order_deleted(order_id))
                    if soft_deleted:
                        risk_logger.info(f"Order {order_id} soft-deleted via OrderManager")
                except Exception as e:
                    risk_logger.warning(f"Order {order_id} soft-delete failed: {e}")

            had_memory_record = order_id in self.auto_orders
            if had_memory_record:
                del self.auto_orders[order_id]

            if soft_deleted or had_memory_record:
                risk_logger.info(f"Order {order_id} removed from records")
                return True
            else:
                risk_logger.warning(f"Order {order_id} not found in records")
                return soft_deleted
        except Exception as e:
            risk_logger.error(f"Error removing order {order_id}: {e}")
            return False

    def update_order_status(self, order_id: str, new_status: str):
        """更新订单状态（内存与数据库）"""
        try:
            if order_id in self.auto_orders:
                order = self.auto_orders[order_id]
                order.status = new_status
                if new_status in ['Filled', 'Cancelled']:
                    order.executed_at = datetime.now()
                
                # 如果订单是由风险规则自动生成且已成交，则禁用对应的风险规则并持久化
                if new_status == 'Filled' and getattr(order, 'order_source', None) == 'auto' and getattr(order, 'rule_id', None):
                    symbol = order.rule_id
                    rule = self.risk_rules.get(symbol)
                    if rule and rule.enabled:
                        rule.enabled = False
                        try:
                            db_rule = DatabaseRiskRule(
                                symbol=rule.symbol,
                                stop_loss_price=rule.stop_loss_price,
                                take_profit_price=rule.take_profit_price,
                                max_loss_percent=rule.max_loss_percent,
                                enabled=rule.enabled,
                                max_time_span=rule.max_time_span,
                                timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
                            )
                            self.db_manager.save_risk_rule(db_rule)
                            risk_logger.info(f"Risk rule for {symbol} disabled due to filled order {order_id}")
                        except Exception as db_rule_err:
                            risk_logger.warning(f"Failed to persist disabled rule for {symbol}: {db_rule_err}")
                
                # 同步到数据库：同时持久化状态、均价（若有）、执行时间
                try:
                    executed_str = order.executed_at.isoformat() if order.executed_at else None
                    self.db_manager.update_order(order_id, status=new_status, price=order.price, executed_at=executed_str)
                    risk_logger.info(f"Order {order_id} status persisted to DB: {new_status}, price={order.price}, executed_at={executed_str}")
                except Exception as db_err:
                    risk_logger.warning(f"Failed to persist order {order_id} update to DB: {db_err}")
                
                return True
            else:
                risk_logger.warning(f"Order {order_id} not found in records for status update")
                return False
        except Exception as e:
            risk_logger.error(f"Error updating order {order_id} status: {e}")
            return False

    # 保留原有的CSV方法作为备份和迁移用途，但标记为弃用
    def save_risk_rules_to_csv(self, filename: str = "risk_rules.csv"):
        """保存风险规则到CSV文件 - 已弃用，请使用数据库"""
        risk_logger.warning("save_risk_rules_to_csv is deprecated, data is now stored in database")
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['symbol', 'stop_loss_price', 'take_profit_price', 'max_loss_percent', 'enabled', 'created_at']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                
                writer.writeheader()
                for symbol, rule in self.risk_rules.items():
                    writer.writerow({
                        'symbol': rule.symbol,
                        'stop_loss_price': rule.stop_loss_price,
                        'take_profit_price': rule.take_profit_price,
                        'max_loss_percent': rule.max_loss_percent,
                        'enabled': rule.enabled,
                        'created_at': rule.created_at.isoformat() if rule.created_at else None
                    })
            risk_logger.info(f"Risk rules saved to {filename}")
        except Exception as e:
            risk_logger.error(f"Error saving risk rules to CSV: {e}")
    
    def load_risk_rules_from_csv(self, filename: str = "risk_rules.csv"):
        """从CSV文件加载风险规则 - 已弃用，请使用数据库"""
        risk_logger.warning("load_risk_rules_from_csv is deprecated, data is now loaded from database")
        try:
            if not os.path.exists(filename):
                risk_logger.warning(f"CSV file {filename} not found")
                return
            
            with open(filename, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                
                for row in reader:
                    symbol = row['symbol']
                    stop_loss_price = float(row['stop_loss_price']) if row['stop_loss_price'] else None
                    take_profit_price = float(row['take_profit_price']) if row['take_profit_price'] else None
                    max_loss_percent = float(row['max_loss_percent']) if row['max_loss_percent'] else None
                    enabled = row['enabled'].lower() == 'true' if row['enabled'] else True
                    created_at = datetime.fromisoformat(row['created_at']) if row['created_at'] else datetime.now()
                    
                    self.risk_rules[symbol] = RiskRule(
                        symbol=symbol,
                        stop_loss_price=stop_loss_price,
                        take_profit_price=take_profit_price,
                        max_loss_percent=max_loss_percent,
                        enabled=enabled,
                        created_at=created_at
                    )
            
            risk_logger.info(f"Risk rules loaded from {filename}, total: {len(self.risk_rules)}")
        except Exception as e:
            risk_logger.error(f"Error loading risk rules from CSV: {e}")
    
    def save_auto_orders_to_csv(self, filename: str = "orders.csv"):
        """保存订单记录到CSV文件 - 已弃用，请使用数据库"""
        risk_logger.warning("save_auto_orders_to_csv is deprecated, data is now stored in database")
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['order_id', 'symbol', 'action', 'quantity', 'order_type', 'price', 'status', 'created_at', 'executed_at', 'rule_id', 'order_source']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                
                writer.writeheader()
                for order_id, order in self.auto_orders.items():
                    writer.writerow({
                        'order_id': order.order_id,
                        'symbol': order.symbol,
                        'action': order.action,
                        'quantity': order.quantity,
                        'order_type': order.order_type,
                        'price': order.price,
                        'status': order.status,
                        'created_at': order.created_at.isoformat() if order.created_at else None,
                        'executed_at': order.executed_at.isoformat() if order.executed_at else None,
                        'rule_id': order.rule_id,
                        'order_source': order.order_source
                    })
            risk_logger.info(f"All orders saved to {filename}")
        except Exception as e:
            risk_logger.error(f"Error saving orders to CSV: {e}")
    
    def load_auto_orders_from_csv(self, filename: str = "orders.csv"):
        """从CSV文件加载订单记录 - 已弃用，请使用数据库"""
        risk_logger.warning("load_auto_orders_from_csv is deprecated, data is now loaded from database")
        try:
            if not os.path.exists(filename):
                risk_logger.warning(f"CSV file {filename} not found")
                return
            
            with open(filename, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                
                for row in reader:
                    created_at = datetime.fromisoformat(row['created_at']) if row['created_at'] else None
                    executed_at = datetime.fromisoformat(row['executed_at']) if row['executed_at'] else None
                    
                    auto_order = AutoOrder(
                        order_id=row['order_id'],
                        symbol=row['symbol'],
                        action=row['action'],
                        quantity=int(row['quantity']) if row['quantity'] else 0,
                        order_type=row['order_type'],
                        price=float(row['price']) if row['price'] else 0.0,
                        status=row['status'],
                        created_at=created_at,
                        executed_at=executed_at,
                        rule_id=row['rule_id'],
                        order_source=row['order_source']
                    )
                    
                    self.auto_orders[row['order_id']] = auto_order
            
            risk_logger.info(f"All orders loaded from {filename}, total orders: {len(self.auto_orders)}")
        except Exception as e:
            risk_logger.error(f"Error loading orders from CSV: {e}")

    def update_equity_state(self, account_info: Dict) -> None:
        """更新权益状态"""
        try:
            today = datetime.now().date()
            net_liq = float(account_info.get('net_liquidation', 0.0) or 0.0)
            realized_pnl = float(account_info.get('realized_pnl', 0.0) or 0.0)


            # 日切换处理：重置当日损益，记录开盘权益
            if getattr(self, 'today_date', None) != today:
                self.today_date = today
                self.sod_equity = net_liq if net_liq > 0 else (self.sod_equity or self.equity_last or 0.0)
                self.daily_loss_currency = 0.0
                self.daily_loss_R = 0.0
                # 不重置loss_streak，在record_trade_result中维护

            # 更新权益与峰值
            self.equity_last = net_liq

            if net_liq > 0:
                self.equity_peak = max(self.equity_peak, net_liq)

            # 计算回撤分级
            dd_pct = 0.0
            if self.equity_peak > 0:
                dd_pct = max(0.0, (self.equity_peak - net_liq) / self.equity_peak)
            steps = int(dd_pct // self.dd_step) if self.dd_step > 0 else 0
            self.dd_steps_applied = min(max(steps, 0), self.dd_max_steps)

            # 基于IBKR当日realized_pnl估算当日亏损（只统计亏损部分）
            self.daily_loss_currency = max(0.0, -realized_pnl)

            denom_R = max(1e-9, (self.sod_equity or net_liq) * max(1e-9, self.risk_per_trade_r))
            self.daily_loss_R = float(self.daily_loss_currency) / denom_R
        except Exception as e:
            risk_logger.error(f"[PS] update_equity_state error: {e}")

    def _compute_scale_dd(self) -> float:
        if self.dd_steps_applied <= 0:
            return 1.0
        try:
            scale = self.dd_scale ** self.dd_steps_applied
            return max(0.0, min(1.0, scale))
        except Exception:
            return 1.0

    def _compute_scale_vol(self, rv_current: Optional[float], rv_ref: Optional[float]) -> float:
        try:
            if rv_current is None or rv_current <= 0:
                return 1.0
            ref = rv_ref if (rv_ref is not None and rv_ref > 0) else (self.rv_ref_value if self.rv_ref_value > 0 else rv_current)
            scale = ref / rv_current
            return max(0.25, min(2.0, float(scale)))
        except Exception:
            return 1.0

    def _compute_scale_sig(self, tier: str) -> float:
        t = (tier or 'A').upper()
        return float(self.signal_tiers.get(t, 0.0))

    def _compute_scale_exec(self, execution_metrics: Optional[Dict[str, float]]) -> float:
        if not execution_metrics:
            return 1.0
        violations = 0
        try:
            vol = execution_metrics.get('volume')
            if vol is not None and self.exec_min_volume is not None and vol < self.exec_min_volume:
                violations += 1
            spread = execution_metrics.get('spread')
            if spread is not None and self.exec_max_spread is not None and spread > self.exec_max_spread:
                violations += 1
            slippage = execution_metrics.get('slippage')
            if slippage is not None and self.exec_max_slippage is not None and slippage > self.exec_max_slippage:
                violations += 1
            if violations <= 0:
                return 1.0
            if violations == 1:
                return 0.5
            return 0.0
        except Exception:
            return 1.0

    def _get_point_value(self, symbol: str) -> float:
        """根据合约符号返回每点价值，优先使用可配置映射。未命中返回1.0。"""
        try:
            sym = (symbol or "").upper()
            if hasattr(self, 'point_value_map') and isinstance(self.point_value_map, dict):
                pv = self.point_value_map.get(sym)
                if pv is not None:
                    return float(pv)
            return 1.0
        except Exception:
            return 1.0

    def _estimate_rv(self, symbol: str) -> Tuple[Optional[float], Optional[float], str]:
        """基于历史日线自动估算当前RV与参考RV。
        返回: (rv_current, rv_ref_by_method, info)
        - rv_current: 最近 vol_window 的收益率滚动标准差
        - rv_ref_by_method: 当 rv_ref_method=rolling/current 时返回对应参考值；static 时返回 None 以回退至 rv_ref_value
        - info: 解释字符串，便于拼接到 explanation
        """
        import signal
        import time
        
        def timeout_handler(signum, frame):
            raise TimeoutError("Historical data request timed out")
        
        try:
            # 检查是否有缓存的历史数据，避免阻塞调用
            if hasattr(core.ibkr_client, '_cached_historical_data') and symbol in core.ibkr_client._cached_historical_data:
                df = core.ibkr_client._cached_historical_data[symbol]
                elapsed = 0.0
            else:
                # 如果没有缓存，跳过RV估算避免阻塞
                risk_logger.warning(f"[RV] No cached historical data for {symbol}, skipping RV estimation")
                return None, None, f"rv:auto=no_cache({symbol})"
            
            if df is None or getattr(df, 'empty', True) or 'close' not in getattr(df, 'columns', {}):
                return None, None, f"rv:auto=no_data({symbol})"

            closes = df['close']
            rets = closes.pct_change().dropna()
            w = int(max(1, getattr(self, 'vol_window', 20)))
            if rets.shape[0] < max(5, w):
                return None, None, f"rv:auto=insufficient_data(n={rets.shape[0]})"

            rv_series = rets.rolling(w).std().dropna()
            rv_cur = float(rv_series.iloc[-1]) if not rv_series.empty else float(rets.std())

            method = (getattr(self, 'rv_ref_method', 'static') or 'static').lower()
            rv_ref_est: Optional[float] = None
            if method == 'current':
                rv_ref_est = rv_cur
            elif method == 'rolling':
                tail_n = max(w, min(len(rv_series), w * 5))
                rv_ref_est = float(rv_series.tail(tail_n).mean()) if not rv_series.empty else float(rets.std())

            info = f"rv:auto method={method}, window={w}, current={rv_cur:.6f}, elapsed={elapsed:.2f}s"
            if rv_ref_est is not None:
                info += f", ref={rv_ref_est:.6f}"
            return rv_cur, rv_ref_est, info
        except TimeoutError:
            # 超时时取消信号并返回默认值
            signal.alarm(0)
            risk_logger.warning(f"_estimate_rv timeout for {symbol}, using fallback")
            return None, None, f"rv:auto=timeout({symbol})"
        except Exception as e:
            # 确保清理信号处理器
            try:
                signal.alarm(0)
            except:
                pass
            risk_logger.error(f"_estimate_rv error: {e}")
            return None, None, "rv:auto=error"

    def position_sizing(self,
                        symbol: str,
                        stop_distance: float,
                        rv_current: Optional[float] = None,
                        signal_tier: str = 'A',
                        execution_metrics: Optional[Dict[str, float]] = None,
                        rv_ref: Optional[float] = None) -> PositionSizingResult:
        account_info = {}
        try:
            # 使用缓存的账户信息避免阻塞调用
            if hasattr(core.ibkr_client, '_cached_account_info'):
                account_info = core.ibkr_client._cached_account_info or {}
            # 如果没有可用缓存，则使用默认净值
            if not account_info or not account_info.get('net_liquidation'):
                account_info = {'net_liquidation': 100000.0}
                risk_logger.warning(f"[PS] No cached account info, using default net_liquidation")
        except Exception as e:
            risk_logger.error(f"[PS] get_account_info error: {e}")
            account_info = {'net_liquidation': 100000.0}
        self.update_equity_state(account_info)
        risk_logger.info(f"[PS] After update_equity_state: equity_last={self.equity_last}, account_info={account_info}")

        equity = self.equity_last
        point_value = self._get_point_value(symbol)
        explanation_parts: List[str] = []

        R = max(0.0, equity * self.risk_per_trade_r)
        explanation_parts.append(f"Equity={equity:.2f}, r={self.risk_per_trade_r:.4f} -> R={R:.2f}")

        # 自动估算RV（当未提供时），并按 rv_ref_method 处理参考值
        try:
            method_lower = (self.rv_ref_method or 'static').lower()
        except Exception:
            method_lower = 'static'
        need_auto_rv_cur = rv_current is None or rv_current <= 0
        need_auto_rv_ref = (rv_ref is None) and (method_lower in ('rolling', 'current'))
        if need_auto_rv_cur or need_auto_rv_ref:
            est_cur, est_ref, rv_info = self._estimate_rv(symbol)
            if need_auto_rv_cur and est_cur is not None and est_cur > 0:
                rv_current = est_cur
            if need_auto_rv_ref and est_ref is not None and est_ref > 0:
                rv_ref = est_ref
            if rv_info:
                explanation_parts.append(rv_info)

        scale_vol = self._compute_scale_vol(rv_current, rv_ref)
        scale_dd = self._compute_scale_dd()
        scale_sig = self._compute_scale_sig(signal_tier)
        scale_exec = self._compute_scale_exec(execution_metrics)
        scale_total = max(0.0, min(1.0, scale_vol * scale_dd * scale_sig * scale_exec))
        explanation_parts.append(
            f"scale_vol={scale_vol:.3f}, scale_dd={scale_dd:.3f}, scale_sig={scale_sig:.3f}, scale_exec={scale_exec:.3f} -> total={scale_total:.3f}")

        R_star = R * scale_vol * scale_dd * scale_sig * scale_exec
        denom = max(1e-9, point_value * max(1e-9, float(stop_distance or 0)))
        contracts_float = R_star / denom

        round_mode = self.contract_rounding
        if round_mode in ('round', 'bankers'):
            contracts = int(max(0, round(contracts_float)))
        else:
            contracts = int(math.floor(max(0.0, contracts_float)))

        explanation_parts.append(
            f"R*={R_star:.2f}, stop={stop_distance}, point_value={point_value} -> raw={contracts_float:.3f}, rounding={round_mode} -> {contracts}")

        return PositionSizingResult(
            contracts=contracts,
            scale_vol=scale_vol,
            scale_dd=scale_dd,
            scale_sig=scale_sig,
            scale_exec=scale_exec,
            scale_total=scale_total,
            R=R,
            R_star=R_star,
            stop_distance=float(stop_distance or 0.0),
            point_value=point_value,
            explanation=' | '.join(explanation_parts)
        )

    def check_circuit_breaker_before_new_order(self) -> Tuple[bool, str]:
        try:
            import core  # 延迟导入避免循环导入
            info = {}
            if hasattr(core.ibkr_client, '_cached_account_info'):
                info = core.ibkr_client._cached_account_info or {}
            self.update_equity_state(info)
        except Exception as e:
            risk_logger.error(f"[CB] get_account_info error: {e}")

        if self.circuit_active:
            return False, f"circuit_active: {self.circuit_reason}"

        reasons = []
        if self.cb_daily_r > 0 and self.daily_loss_R >= self.cb_daily_r:
            reasons.append(f"daily_loss_R={self.daily_loss_R:.2f} >= {self.cb_daily_r}")
        if self.cb_streak > 0 and self.loss_streak >= self.cb_streak:
            reasons.append(f"loss_streak={self.loss_streak} >= {self.cb_streak}")

        if reasons:
            self.circuit_active = True
            self.circuit_reason = ', '.join(reasons)
            try:
                self.clear_order_queue()
            except Exception:
                pass
            risk_logger.warning(f"[CB] Circuit breaker TRIGGERED: {self.circuit_reason}")
            try:
                if self.notification_manager:
                    asyncio.create_task(self.notification_manager.send_risk_notification(
                        symbol="ALL",
                        message=f"断路器触发: {self.circuit_reason}",
                        risk_type="circuit_breaker"
                    ))
            except Exception:
                pass
            return False, self.circuit_reason

        return True, "ok"

    def reset_circuit_breaker(self):
        self.circuit_active = False
        self.circuit_reason = ''
        risk_logger.info("[CB] Circuit breaker manually reset")

    def record_trade_result(self, pnl_currency: float):
        try:
            if pnl_currency is None:
                return
            if pnl_currency < 0:
                self.loss_streak += 1
                self.daily_loss_currency += -pnl_currency
            else:
                self.loss_streak = 0
            denom_R = (self.sod_equity or self.equity_last) * (self.risk_per_trade_r or 1e-9)
            self.daily_loss_R = (self.daily_loss_currency / denom_R) if denom_R > 0 else 0.0
        except Exception as e:
            risk_logger.error(f"[CB] record_trade_result error: {e}")

    # ... existing code ...