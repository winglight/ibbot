"""
Strategy Management Module
Handles strategy storage, execution, and performance tracking
"""

import os
import json
import csv
import hashlib
import hmac
import time
import importlib.util
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from loguru import logger

from .database import DatabaseManager, get_db_manager, Strategy as DatabaseStrategy, StrategyExecution as DatabaseStrategyExecution


@dataclass
class Strategy:
    """Strategy data class"""
    name: str
    strategy_type: str  # "external" or "internal"
    enabled: bool = True
    api_endpoint: Optional[str] = None
    file_path: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime = None
    trigger_count: int = 0
    last_triggered: Optional[datetime] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


@dataclass
class StrategyExecution:
    """Strategy execution record"""
    strategy_name: str
    execution_time: datetime
    order_id: Optional[str] = None
    symbol: Optional[str] = None
    action: Optional[str] = None
    quantity: Optional[int] = None
    price: Optional[float] = None
    status: str = "executed"
    pnl: Optional[float] = None
    
    def __post_init__(self):
        if self.execution_time is None:
            self.execution_time = datetime.now()


class StrategyManager:
    """Strategy Manager Class"""
    
    def __init__(self, data_dir: str = "data", strategy_dir: str = "strategy"):
        self.data_dir = Path(data_dir)
        self.strategy_dir = Path(strategy_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.strategy_dir.mkdir(exist_ok=True)
        
        # External and internal API clients
        self.external_clients = {}
        
        # Database manager
        self.db_manager = get_db_manager()
        
        # Dependencies - will be injected
        self.risk_manager = None
        
        # 内存缓存，从数据库加载
        self.strategies: Dict[str, Strategy] = {}
        self.executions: List[StrategyExecution] = []
        self.settings: Dict[str, Any] = {}
        
        # 认证token（用于外部策略访问）
        self.auth_token = None
        
        # 加载数据
        self._load_from_database()
        
    def set_dependencies(self, risk_manager=None):
        """Set dependency objects for strategy manager"""
        self.risk_manager = risk_manager
        logger.info(f"Dependencies set for StrategyManager: risk_manager={'initialized' if risk_manager else 'None'}")


    def _load_from_database(self):
        """从数据库加载策略设置、策略和执行记录"""
        try:
            # 加载策略设置
            self.settings = self.db_manager.load_strategy_settings()
            
            # 设置默认值
            if not self.settings:
                self.settings = {
                    "api_endpoint": "/api/strategy",
                    "api_method": "POST",
                    "api_payload_template": {
                        "strategy": "{{strategy_name}}",
                        "symbol": "{{symbol}}",
                        "action": "{{action}}",
                        "qty": "{{quantity}}",
                        "type": "{{order_type}}",
                        "price": "{{price}}"
                    },
                    "strategy_path": "strategy/",
                    "enable_auth": False,
                    "auth_token": None
                }
                # 保存默认设置到数据库
                self.db_manager.save_strategy_settings(self.settings)
            
            # 更新实例变量
            self.api_endpoint = self.settings["api_endpoint"]
            self.api_method = self.settings["api_method"]
            self.api_payload_template = self.settings["api_payload_template"]
            self.strategy_dir = Path(self.settings["strategy_path"]).resolve()
            self.enable_auth = self.settings.get("enable_auth", False)
            self.auth_token = self.settings.get("auth_token")
            
            # 确保策略目录存在
            self.strategy_dir.mkdir(parents=True, exist_ok=True)
            
            # 加载策略
            db_strategies = self.db_manager.load_strategies()
            for strategy_data in db_strategies:
                # 将数据库的策略数据类转换为本地策略数据类
                strategy = Strategy(
                    name=strategy_data.name,
                    strategy_type=strategy_data.strategy_type,
                    enabled=strategy_data.enabled,
                    api_endpoint=strategy_data.api_endpoint,
                    file_path=strategy_data.file_path,
                    description=strategy_data.description,
                    created_at=datetime.fromisoformat(strategy_data.created_at) if strategy_data.created_at else datetime.now(),
                    trigger_count=0,  # 数据库数据类中没有这个字段，设为默认值
                    last_triggered=None
                )
                self.strategies[strategy.name] = strategy
            
            # 加载策略执行记录
            db_executions = self.db_manager.load_strategy_executions()
            for execution_data in db_executions:
                execution = StrategyExecution(
                    strategy_name=execution_data.name,
                    execution_time=datetime.fromisoformat(execution_data.timestamp),
                    order_id=execution_data.order_id,
                    symbol=execution_data.symbol,
                    action=execution_data.action,
                    quantity=execution_data.quantity,
                    price=execution_data.price,
                    status=execution_data.status,
                    pnl=execution_data.profit
                )
                self.executions.append(execution)
            
            logger.info(f"Loaded {len(self.strategies)} strategies and {len(self.executions)} executions from database")
            
        except Exception as e:
            logger.error(f"Error loading from database: {e}")
            # 如果数据库加载失败，尝试从文件加载并迁移
            self._load_from_files_and_migrate()
    
    def _load_from_files_and_migrate(self):
        """从文件加载数据并迁移到数据库（向后兼容）"""
        logger.info("Loading from legacy files and migrating to database...")
        
        # 加载设置
        self._load_settings()
        
        # 加载策略
        self._load_strategies()
        
        # 加载执行记录
        self._load_executions()
        
        # 迁移到数据库
        try:
            # 保存设置到数据库
            self.db_manager.save_strategy_settings(self.settings)
            
            # 保存策略到数据库
            for strategy in self.strategies.values():
                # 保存到数据库
                db_strategy = DatabaseStrategy(
                    name=strategy.name,
                    strategy_type=strategy.strategy_type,
                    enabled=strategy.enabled,
                    api_endpoint=strategy.api_endpoint,
                    file_path=strategy.file_path,
                    description=strategy.description,
                    created_at=strategy.created_at.isoformat()
                )
                self.db_manager.save_strategy(db_strategy)
            
            # 保存执行记录到数据库
            for execution in self.executions:
                db_execution = DatabaseStrategyExecution(
                    name=execution.strategy_name,
                    symbol=execution.symbol,
                    action=execution.action,
                    quantity=execution.quantity,
                    price=execution.price,
                    timestamp=execution.execution_time.isoformat(),
                    order_id=execution.order_id,
                    profit=execution.pnl,
                    status=execution.status
                )
                self.db_manager.save_strategy_execution(db_execution)
            
            logger.info("Successfully migrated data from files to database")
            
        except Exception as e:
            logger.error(f"Error migrating data to database: {e}")
        
    def _load_settings(self):
        """Load strategy settings - 兼容性方法，现在从数据库加载"""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r') as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load settings: {e}")
                data = {}
        else:
            data = {}
            
        # Default settings
        defaults = {
            "api_endpoint": "/api/strategy",
            "api_method": "POST",
            "api_payload_template": {
                "strategy": "{{strategy_name}}",
                "symbol": "{{symbol}}",
                "action": "{{action}}",
                "qty": "{{quantity}}",
                "type": "{{order_type}}",
                "price": "{{price}}"
            },
            "strategy_path": "strategy/",
            "enable_auth": False,
            "auth_token": None
        }
        
        self.settings = {**defaults, **data}
        
        # Update instance variables
        self.api_endpoint = self.settings["api_endpoint"]
        self.api_method = self.settings["api_method"]
        self.api_payload_template = self.settings["api_payload_template"]
        self.strategy_dir = Path(self.settings["strategy_path"]).resolve()
        self.enable_auth = self.settings.get("enable_auth", False)
        self.auth_token = self.settings.get("auth_token")
        
        # Ensure strategy directory exists
        self.strategy_dir.mkdir(parents=True, exist_ok=True)
        
    def _save_settings(self):
        """Save strategy settings - 现在保存到数据库"""
        try:
            self.db_manager.save_strategy_settings(self.settings)
            logger.info("Strategy settings saved to database")
        except Exception as e:
            logger.error(f"Failed to save settings to database: {e}")
            
    def _load_strategies(self):
        """Load strategies from CSV file - 兼容性方法"""
        if self.strategies_file.exists():
            try:
                with open(self.strategies_file, 'r', newline='') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # Convert string values back to appropriate types
                        row['enabled'] = row['enabled'].lower() == 'true'
                        if row['created_at']:
                            row['created_at'] = datetime.fromisoformat(row['created_at'])
                        if row['last_triggered']:
                            row['last_triggered'] = datetime.fromisoformat(row['last_triggered'])
                        else:
                            row['last_triggered'] = None
                        if row['trigger_count']:
                            row['trigger_count'] = int(row['trigger_count'])
                        
                        # Handle empty/None values
                        for key in ['api_endpoint', 'file_path', 'description']:
                            if not row[key]:
                                row[key] = None
                        
                        self.strategies[row['name']] = Strategy(**row)
                        
                logger.info(f"Loaded {len(self.strategies)} strategies from CSV")
            except Exception as e:
                logger.error(f"Failed to load strategies: {e}")
                self.strategies = {}
                
    def _save_strategies(self):
        """Save strategies - 现在保存到数据库"""
        try:
            for strategy in self.strategies.values():
                db_strategy = DatabaseStrategy(
                    name=strategy.name,
                    strategy_type=strategy.strategy_type,
                    enabled=strategy.enabled,
                    api_endpoint=strategy.api_endpoint,
                    file_path=strategy.file_path,
                    description=strategy.description,
                    created_at=strategy.created_at.isoformat() if strategy.created_at else datetime.now().isoformat()
                )
                self.db_manager.save_strategy(db_strategy)
            logger.info("Strategies saved to database")
        except Exception as e:
            logger.error(f"Failed to save strategies to database: {e}")
            
    def _load_executions(self):
        """Load strategy executions from CSV - 兼容性方法"""
        if self.executions_file.exists():
            try:
                with open(self.executions_file, 'r', newline='') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # Convert string values back to appropriate types
                        row['execution_time'] = datetime.fromisoformat(row['execution_time'])
                        if row['quantity']:
                            row['quantity'] = int(row['quantity'])
                        if row['price']:
                            row['price'] = float(row['price'])
                        if row['pnl']:
                            row['pnl'] = float(row['pnl'])
                        
                        self.executions.append(StrategyExecution(**row))
                        
                logger.info(f"Loaded {len(self.executions)} strategy executions from CSV")
            except Exception as e:
                logger.error(f"Failed to load executions: {e}")
                self.executions = []
                
    def _save_executions(self):
        """Save strategy executions - 现在保存到数据库（仅保存新记录）"""
        try:
            # 获取数据库中已有的记录，避免重复保存
            existing_executions = self.db_manager.load_strategy_executions(limit=1000)
            existing_timestamps = {(e.name, e.timestamp, e.order_id or "") for e in existing_executions}
            
            saved_count = 0
            for execution in self.executions:
                # 构建唯一标识符来检查是否已存在
                execution_key = (execution.strategy_name, execution.execution_time.isoformat(), execution.order_id or "")
                
                if execution_key not in existing_timestamps:
                    db_execution = DatabaseStrategyExecution(
                        name=execution.strategy_name,
                        symbol=execution.symbol,
                        action=execution.action,
                        quantity=execution.quantity,
                        price=execution.price,
                        timestamp=execution.execution_time.isoformat(),
                        order_id=execution.order_id,
                        profit=execution.pnl,
                        status=execution.status
                    )
                    self.db_manager.save_strategy_execution(db_execution)
                    saved_count += 1
            
            if saved_count > 0:
                logger.info(f"Strategy executions saved to database: {saved_count} new records")
            else:
                logger.debug("No new strategy executions to save")
        except Exception as e:
            logger.error(f"Failed to save executions to database: {e}")

    def get_settings(self) -> Dict[str, Any]:
        """Get current settings"""
        return self.settings.copy()
        
    def update_settings(self, new_settings: Dict[str, Any]) -> bool:
        """Update settings"""
        try:
            # 过滤掉值为 None 的字段，避免覆盖已有设置
            cleaned = {k: v for k, v in new_settings.items() if v is not None}
            self.settings.update(cleaned)
            self._save_settings()
            self._load_from_database()  # 重新加载以更新实例变量
            return True
        except Exception as e:
            logger.error(f"Failed to update settings: {e}")
            return False
            
    def add_strategy(self, strategy_data: Dict[str, Any]) -> bool:
        """Add a new strategy"""
        try:
            name = strategy_data['name']
            if name in self.strategies:
                logger.warning(f"Strategy {name} already exists")
                return False
            
            # 处理内部策略的文件选择逻辑
            final_file_path = strategy_data.get('file_path')
            if strategy_data['strategy_type'] == "internal":
                selected_file = strategy_data.get('selected_file')
                use_template = strategy_data.get('use_template', False)
                
                if selected_file and selected_file != "new":
                    # 选择了现有文件
                    final_file_path = selected_file
                    # 如果名称为空或使用默认名称，自动从文件名生成
                    if not name or name == selected_file:
                        name = Path(selected_file).stem  # 去掉.py扩展名
                        # 检查生成的名称是否已存在
                        counter = 1
                        original_name = name
                        while name in self.strategies:
                            name = f"{original_name}_{counter}"
                            counter += 1
                elif use_template or selected_file == "new":
                    # 创建新文件或使用模板
                    final_file_path = f"{name}.py"
                
            strategy = Strategy(
                name=name,
                strategy_type=strategy_data['strategy_type'],
                enabled=strategy_data.get('enabled', True),
                api_endpoint=strategy_data.get('api_endpoint'),
                file_path=final_file_path,
                description=strategy_data.get('description'),
                created_at=datetime.now()
            )
            
            # 保存到数据库
            db_strategy = DatabaseStrategy(
                name=strategy.name,
                strategy_type=strategy.strategy_type,
                enabled=strategy.enabled,
                api_endpoint=strategy.api_endpoint,
                file_path=strategy.file_path,
                description=strategy.description,
                created_at=strategy.created_at.isoformat()
            )
            self.db_manager.save_strategy(db_strategy)
            
            # 更新内存缓存
            self.strategies[name] = strategy
            
            # 如果是内部策略，处理文件创建（仅在新建文件时创建模板）
            if strategy.strategy_type == "internal":
                selected_file = strategy_data.get('selected_file')
                use_template = strategy_data.get('use_template', False)
                
                # 仅当用户选择“新建文件”时才创建模板，选择现有文件一律不写入
                if selected_file == "new" and strategy.file_path:
                    self._create_strategy_template(name, strategy.file_path)
            
            # 保存SL/TP配置到策略设置
            sl_tp_settings = {}
            if strategy_data.get('sl_enabled'):
                sl_tp_settings[f"{name}_sl_enabled"] = True
                sl_tp_settings[f"{name}_sl_price"] = strategy_data.get('sl_price')
                sl_tp_settings[f"{name}_sl_type"] = strategy_data.get('sl_type', 'amount')
            if strategy_data.get('tp_enabled'):
                sl_tp_settings[f"{name}_tp_enabled"] = True
                sl_tp_settings[f"{name}_tp_price"] = strategy_data.get('tp_price')
                sl_tp_settings[f"{name}_tp_type"] = strategy_data.get('tp_type', 'amount')
                
            if sl_tp_settings:
                self.db_manager.save_strategy_settings(sl_tp_settings)
                # 同步到内存中的 settings，确保后续 get_strategy 能直接读取
                self.settings.update(sl_tp_settings)
                logger.info(f"Saved SL/TP settings for strategy {name}")
                
            logger.info(f"Strategy {name} added successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add strategy: {e}")
            return False

    def get_strategy(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a strategy by name"""
        if name in self.strategies:
            strategy = self.strategies[name]
            strategy_dict = asdict(strategy)
            # Compute trigger_count from executions to avoid stale persisted values
            computed_count = sum(1 for e in self.executions if e.strategy_name == strategy.name)
            strategy_dict['trigger_count'] = computed_count
            # Compute last_triggered from executions if available
            times = [e.execution_time for e in self.executions if e.strategy_name == strategy.name]
            if times:
                strategy_dict['last_triggered'] = max(times)
            # Convert datetime objects to strings for JSON serialization
            if strategy_dict['created_at'] and isinstance(strategy_dict['created_at'], datetime):
                strategy_dict['created_at'] = strategy_dict['created_at'].isoformat()
            if strategy_dict['last_triggered'] and isinstance(strategy_dict['last_triggered'], datetime):
                strategy_dict['last_triggered'] = strategy_dict['last_triggered'].isoformat()
            # 合并SL/TP设置
            try:
                settings = self.db_manager.load_strategy_settings()
            except Exception:
                settings = getattr(self, 'settings', {}) or {}
            sl_enabled = settings.get(f"{name}_sl_enabled", False)
            tp_enabled = settings.get(f"{name}_tp_enabled", False)
            strategy_dict['sl_enabled'] = sl_enabled
            strategy_dict['tp_enabled'] = tp_enabled
            strategy_dict['sl_price'] = settings.get(f"{name}_sl_price") if sl_enabled else None
            strategy_dict['tp_price'] = settings.get(f"{name}_tp_price") if tp_enabled else None
            strategy_dict['sl_type'] = settings.get(f"{name}_sl_type", 'amount')
            strategy_dict['tp_type'] = settings.get(f"{name}_tp_type", 'amount')
            return strategy_dict
        return None

    def update_strategy(self, name: str, updates: Dict[str, Any]) -> bool:
        """Update a strategy"""
        try:
            if name not in self.strategies:
                return False
                
            strategy = self.strategies[name]
            # 将SL/TP更新与常规字段分离
            sl_tp_keys = {"sl_enabled", "sl_price", "sl_type", "tp_enabled", "tp_price", "tp_type"}
            settings_to_save: Dict[str, Any] = {}
            for key, value in updates.items():
                if key in sl_tp_keys:
                    # 构造持久化键
                    if key.startswith('sl_'):
                        settings_key = f"{name}_{key}"
                        settings_to_save[settings_key] = value
                    elif key.startswith('tp_'):
                        settings_key = f"{name}_{key}"
                        settings_to_save[settings_key] = value
                elif hasattr(strategy, key):
                    setattr(strategy, key, value)
            
            # 先保存策略实体（常规字段）
            self._save_strategies()
            
            # 持久化SL/TP设置
            if settings_to_save:
                self.db_manager.save_strategy_settings(settings_to_save)
                # 更新内存
                self.settings.update(settings_to_save)
            
            logger.info(f"Updated strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to update strategy {name}: {e}")
            return False

    def _create_strategy_template(self, strategy_name: str, file_path: Optional[str] = None):
        """Create a Python strategy template file"""
        template_content = f'''"""
Strategy: {strategy_name}
Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

from datetime import datetime
from typing import Dict, Any, Optional


class {strategy_name.replace(' ', '').replace('-', '').replace('_', '')}Strategy:
    """
    {strategy_name} Strategy Implementation
    """
    
    def __init__(self):
        self.name = "{strategy_name}"
        self.description = "Strategy template for {strategy_name}"
        
    def initialize(self):
        """Initialize the strategy"""
        print(f"Initializing strategy: {{self.name}}")
        
    def on_data(self, symbol: str, data: Dict[str, Any]):
        """
        Handle market data updates
        
        Args:
            symbol: Stock symbol
            data: Market data dictionary
        """
        pass
        
    def generate_signal(self, symbol: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on market data
        Returns:
            A dict with signal details: { '{' }"action": "BUY"/"SELL", "quantity": int, "price": float, "order_type": "MARKET"/"LIMIT"{ '}' } or None if no signal
        """
        return None
        
    def on_order_filled(self, order_id: str, price: float, quantity: int):
        """Handle order filled events"""
        print(f"Order filled: id={{order_id}}, price={{price}}, qty={{quantity}}")
        
    def on_error(self, error: str):
        """Handle errors"""
        print(f"Strategy error: {{error}}")


# Strategy instance
strategy = {strategy_name.replace(' ', '').replace('-', '').replace('_', '')}Strategy()

# Entry point
if __name__ == "__main__":
    strategy.initialize()
    print(f"Strategy {{strategy.name}} is ready")
'''
        
        strategy_file = self.strategy_dir / (file_path if file_path else f"{strategy_name}.py")
        try:
            with open(strategy_file, 'w') as f:
                f.write(template_content)
            logger.info(f"Created strategy template: {strategy_file}")
        except Exception as e:
            logger.error(f"Failed to create strategy template: {e}")
            


    def get_strategies(self) -> List[Dict[str, Any]]:
        """Get all strategies"""
        result = []
        for strategy in self.strategies.values():
            strategy_dict = asdict(strategy)
            # Compute trigger_count from executions to avoid stale persisted values
            computed_count = sum(1 for e in self.executions if e.strategy_name == strategy.name)
            strategy_dict['trigger_count'] = computed_count
            # Compute last_triggered from executions if available
            times = [e.execution_time for e in self.executions if e.strategy_name == strategy.name]
            if times:
                strategy_dict['last_triggered'] = max(times)
            # Convert datetime objects to strings for JSON serialization
            if strategy_dict['created_at'] and isinstance(strategy_dict['created_at'], datetime):
                strategy_dict['created_at'] = strategy_dict['created_at'].isoformat()
            if strategy_dict['last_triggered'] and isinstance(strategy_dict['last_triggered'], datetime):
                strategy_dict['last_triggered'] = strategy_dict['last_triggered'].isoformat()
            result.append(strategy_dict)
        return result
        

            
    def delete_strategy(self, name: str) -> bool:
        """Delete a strategy"""
        try:
            if name not in self.strategies:
                return False
                
            # Delete from memory
            del self.strategies[name]
            
            # Delete from database directly
            self.db_manager.delete_strategy(name)
            
            # Also clean executions for this strategy from database
            self.db_manager.delete_strategy_executions(name)
            
            # Clean from memory executions
            self.executions = [e for e in self.executions if e.strategy_name != name]
            
            logger.info(f"Deleted strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete strategy {name}: {e}")
            return False

    def record_execution(self, execution: StrategyExecution):
        """Record a strategy execution"""
        try:
            # 添加到内存缓存
            self.executions.append(execution)
            
            # 直接保存单条新记录到数据库，不触发批量保存
            db_execution = DatabaseStrategyExecution(
                name=execution.strategy_name,
                symbol=execution.symbol,
                action=execution.action,
                quantity=execution.quantity,
                price=execution.price,
                timestamp=execution.execution_time.isoformat(),
                order_id=execution.order_id,
                profit=execution.pnl,
                status=execution.status
            )
            self.db_manager.save_strategy_execution(db_execution)

            # Update strategy last triggered time only (trigger_count is derived from executions)
            if execution.strategy_name in self.strategies:
                strategy = self.strategies[execution.strategy_name]
                strategy.last_triggered = execution.execution_time
                # Do not save strategies here just for derived fields

            logger.info(f"Recorded execution for strategy: {execution.strategy_name}")
        except Exception as e:
            logger.error(f"Failed to record execution: {e}")
            
    def update_execution_price_and_pnl(self, order_id: str, fill_price: float, ibkr_client=None):
        """Update strategy execution price and recalculate PnL after order fills"""
        try:
            # Find the execution record by order_id
            execution = None
            for exec_record in self.executions:
                if exec_record.order_id == order_id:
                    execution = exec_record
                    break
            
            # If not found in memory, try to load from database and add to memory
            if not execution:
                logger.info(f"Execution record not found in memory for order_id: {order_id}, checking database...")
                try:
                    # Load all executions from database to find the missing one
                    db_executions = self.db_manager.load_strategy_executions()
                    for db_exec in db_executions:
                        if db_exec.order_id == order_id:
                            # Create StrategyExecution from database record and add to memory
                            execution = StrategyExecution(
                                strategy_name=db_exec.name,
                                execution_time=datetime.fromisoformat(db_exec.timestamp),
                                order_id=db_exec.order_id,
                                symbol=db_exec.symbol,
                                action=db_exec.action,
                                quantity=db_exec.quantity,
                                price=db_exec.price,
                                status=db_exec.status,
                                pnl=db_exec.profit
                            )
                            self.executions.append(execution)
                            logger.info(f"Loaded execution record from database and added to memory: {order_id}")
                            break
                except Exception as db_err:
                    logger.error(f"Failed to load execution from database for order_id {order_id}: {db_err}")
            
            if not execution:
                logger.warning(f"No execution record found for order_id: {order_id} in memory or database")
                return
            
            # Update execution price
            execution.price = fill_price
            
            # Check if this is an opening position for an internal strategy with SL/TP enabled
            self._check_and_create_auto_risk_rules(execution, fill_price)
            
            # Recalculate PnL for all executions of the same strategy and symbol
            if execution.symbol and execution.action and execution.quantity:
                try:
                    strategy_executions = [e for e in self.executions 
                                         if e.strategy_name == execution.strategy_name 
                                         and e.symbol == execution.symbol 
                                         and e.price is not None]
                    
                    # Sort by execution time to ensure FIFO order
                    strategy_executions.sort(key=lambda x: x.execution_time)
                    
                    # Reset all PnL values for this strategy/symbol combination
                    for exec_rec in strategy_executions:
                        exec_rec.pnl = 0.0
                    
                    # Use simple FIFO matching: pair BUY and SELL orders in sequence
                    buy_queue = []  # Store (price, quantity, exec_record) for unmatched buys
                    sell_queue = []  # Store (price, quantity, exec_record) for unmatched sells
                    
                    futures_multipliers = {'MNQ': 2, 'NQ': 20, 'ES': 50, 'MES': 5}
                    multiplier = futures_multipliers.get(execution.symbol, 1)
                    
                    for exec_rec in strategy_executions:
                        action = (exec_rec.action or '').upper()
                        quantity = exec_rec.quantity or 0
                        price = exec_rec.price or 0.0
                        
                        if action == 'BUY':
                            remaining_qty = quantity
                            # Try to match with existing sells first
                            while remaining_qty > 0 and sell_queue:
                                sell_price, sell_qty, sell_exec = sell_queue[0]
                                
                                # Calculate matched quantity
                                matched_qty = min(remaining_qty, sell_qty)
                                
                                # Calculate PnL for this match (sell_price - buy_price) * qty * multiplier
                                pnl = (sell_price - price) * matched_qty * multiplier
                                
                                # Add PnL to the sell execution (since sell closes the position)
                                if sell_exec.pnl is None:
                                    sell_exec.pnl = 0.0
                                sell_exec.pnl += pnl
                                
                                # Update quantities
                                remaining_qty -= matched_qty
                                sell_queue[0] = (sell_price, sell_qty - matched_qty, sell_exec)
                                
                                # Remove fully matched sell orders
                                if sell_queue[0][1] <= 0:
                                    sell_queue.pop(0)
                            
                            # Add unmatched buy quantity to buy queue
                            if remaining_qty > 0:
                                buy_queue.append((price, remaining_qty, exec_rec))
                                
                        elif action == 'SELL':
                            remaining_qty = quantity
                            # Try to match with existing buys first
                            while remaining_qty > 0 and buy_queue:
                                buy_price, buy_qty, buy_exec = buy_queue[0]
                                
                                # Calculate matched quantity
                                matched_qty = min(remaining_qty, buy_qty)
                                
                                # Calculate PnL for this match (sell_price - buy_price) * qty * multiplier
                                pnl = (price - buy_price) * matched_qty * multiplier
                                
                                # Add PnL to the current sell execution
                                if exec_rec.pnl is None:
                                    exec_rec.pnl = 0.0
                                exec_rec.pnl += pnl
                                
                                # Update quantities
                                remaining_qty -= matched_qty
                                buy_queue[0] = (buy_price, buy_qty - matched_qty, buy_exec)
                                
                                # Remove fully matched buy orders
                                if buy_queue[0][1] <= 0:
                                    buy_queue.pop(0)
                            
                            # Add unmatched sell quantity to sell queue
                            if remaining_qty > 0:
                                sell_queue.append((price, remaining_qty, exec_rec))
                    
                    logger.info(f"Updated PnL for {execution.strategy_name}/{execution.symbol} executions using FIFO matching")
                    
                except Exception as calc_err:
                    logger.warning(f"Failed to calculate PnL using FIFO matching: {calc_err}")
                    execution.pnl = 0.0
            else:
                execution.pnl = 0.0
                logger.info(f"Updated execution {order_id}: price={fill_price}, PnL=0 (missing data)")
            
            # Save updated executions
            # 优先根据 order_id 更新数据库中的记录，避免重复插入
            try:
                total_profit = None
                # 如果只更新单条，可以直接更新该条记录
                if order_id:
                    # 选取与该order_id对应的exec记录的pnl作为更新值（如果有）
                    target_exec = next((e for e in strategy_executions if e.order_id == order_id), None)
                    if target_exec:
                        total_profit = target_exec.pnl
                self.db_manager.update_strategy_execution(order_id, price=fill_price, profit=total_profit)
                logger.info(f"Strategy execution updated in database for order {order_id}")
            except Exception as db_upd_err:
                logger.warning(f"Failed to update strategy execution in DB, fallback to batch save: {db_upd_err}")
                self._save_executions()
                logger.info(f"Strategy execution updated and saved for order {order_id}")
            
        except Exception as e:
            logger.error(f"Failed to update execution price and PnL: {e}")
            
    def _check_and_create_auto_risk_rules(self, execution: StrategyExecution, fill_price: float):
        """Check if this is an opening position and create auto risk rules for internal strategies"""
        try:
            if not self.risk_manager:
                logger.debug("RiskManager not available, skipping auto risk rule creation")
                return
            
            if not execution.strategy_name or not execution.symbol or not execution.action:
                logger.debug("Missing execution data for auto risk rule creation")
                return
            
            # Check if this strategy exists and is internal
            strategy = self.strategies.get(execution.strategy_name)
            if not strategy:
                logger.debug(f"Strategy {execution.strategy_name} not found in local strategies")
                return
            
            if strategy.strategy_type != "internal":
                logger.debug(f"Strategy {execution.strategy_name} is not internal, skipping auto risk rules")
                return
            
            # Get strategy SL/TP settings from settings cache
            settings = self.get_settings()
            sl_enabled = settings.get(f"{execution.strategy_name}_sl_enabled", False)
            tp_enabled = settings.get(f"{execution.strategy_name}_tp_enabled", False)
            
            if not sl_enabled and not tp_enabled:
                logger.debug(f"No SL/TP enabled for strategy {execution.strategy_name}")
                return
            
            # Check if this is an opening position by calculating net position
            strategy_executions = [e for e in self.executions 
                                 if e.strategy_name == execution.strategy_name 
                                 and e.symbol == execution.symbol 
                                 and e.price is not None]
            
            # Calculate net position after this execution
            net_position = 0
            for exec_rec in strategy_executions:
                if exec_rec.action and exec_rec.quantity:
                    if exec_rec.action.upper() == 'BUY':
                        net_position += exec_rec.quantity
                    elif exec_rec.action.upper() == 'SELL':
                        net_position -= exec_rec.quantity
            
            # Check if we just opened a position (went from 0 to non-zero)
            previous_executions = [e for e in strategy_executions if e.execution_time < execution.execution_time]
            previous_net_position = 0
            for exec_rec in previous_executions:
                if exec_rec.action and exec_rec.quantity:
                    if exec_rec.action.upper() == 'BUY':
                        previous_net_position += exec_rec.quantity
                    elif exec_rec.action.upper() == 'SELL':
                        previous_net_position -= exec_rec.quantity
            
            # Only create risk rules when opening a new position (previous position was 0)
            if previous_net_position != 0:
                logger.debug(f"Not an opening position for {execution.strategy_name}/{execution.symbol}, previous position: {previous_net_position}")
                return
            
            if net_position == 0:
                logger.debug(f"Net position is 0 after execution for {execution.strategy_name}/{execution.symbol}")
                return
            
            logger.info(f"Opening position detected for {execution.strategy_name}/{execution.symbol}, creating auto risk rules")
            
            # Calculate SL/TP prices
            sl_price = None
            tp_price = None
            
            if sl_enabled:
                sl_value = settings.get(f"{execution.strategy_name}_sl_price")
                sl_type = settings.get(f"{execution.strategy_name}_sl_type", "amount")
                
                if sl_value:
                    if sl_type == "percent":
                        # For long positions: SL = fill_price * (1 - sl_percent/100)
                        # For short positions: SL = fill_price * (1 + sl_percent/100)
                        sl_percent = float(sl_value) / 100.0
                        if net_position > 0:  # Long position
                            sl_price = fill_price * (1 - sl_percent)
                        else:  # Short position
                            sl_price = fill_price * (1 + sl_percent)
                    else:  # amount
                        # For long positions: SL = fill_price - sl_amount
                        # For short positions: SL = fill_price + sl_amount
                        sl_amount = float(sl_value)
                        if net_position > 0:  # Long position
                            sl_price = fill_price - sl_amount
                        else:  # Short position
                            sl_price = fill_price + sl_amount
            
            if tp_enabled:
                tp_value = settings.get(f"{execution.strategy_name}_tp_price")
                tp_type = settings.get(f"{execution.strategy_name}_tp_type", "amount")
                
                if tp_value:
                    if tp_type == "percent":
                        # For long positions: TP = fill_price * (1 + tp_percent/100)
                        # For short positions: TP = fill_price * (1 - tp_percent/100)
                        tp_percent = float(tp_value) / 100.0
                        if net_position > 0:  # Long position
                            tp_price = fill_price * (1 + tp_percent)
                        else:  # Short position
                            tp_price = fill_price * (1 - tp_percent)
                    else:  # amount
                        # For long positions: TP = fill_price + tp_amount
                        # For short positions: TP = fill_price - tp_amount
                        tp_amount = float(tp_value)
                        if net_position > 0:  # Long position
                            tp_price = fill_price + tp_amount
                        else:  # Short position
                            tp_price = fill_price - tp_amount
            
            # Create risk rule if we have at least one price
            if sl_price is not None or tp_price is not None:
                success = self.risk_manager.add_risk_rule(
                    symbol=execution.symbol,
                    stop_loss_price=sl_price,
                    take_profit_price=tp_price,
                    max_loss_percent=None
                )
                
                if success:
                    logger.info(f"Auto risk rule created for {execution.strategy_name}/{execution.symbol}: SL=${sl_price}, TP=${tp_price}")
                else:
                    logger.warning(f"Failed to create auto risk rule for {execution.strategy_name}/{execution.symbol}")
            else:
                logger.debug(f"No valid SL/TP prices calculated for {execution.strategy_name}/{execution.symbol}")
            
        except Exception as e:
            logger.error(f"Error creating auto risk rules: {e}")

            
    def get_executions(self, strategy_name: str, limit: int = 100) -> List[StrategyExecution]:
        """Get executions for a specific strategy"""
        try:
            # Load from database
            db_executions = self.db_manager.load_strategy_executions(strategy_name, limit)
            
            # Map database execution to StrategyExecution format expected by frontend
            result = []
            for db_exec in db_executions:
                # Map database fields to StrategyExecution fields
                execution = StrategyExecution(
                    strategy_name=db_exec.name,
                    execution_time=datetime.fromisoformat(db_exec.timestamp),
                    order_id=db_exec.order_id,
                    symbol=db_exec.symbol,
                    action=db_exec.action,
                    quantity=db_exec.quantity,
                    price=db_exec.price,
                    status=db_exec.status or "executed",
                    pnl=db_exec.profit
                )
                result.append(execution)
            
            return result
        except Exception as e:
            logger.error(f"Failed to get executions for {strategy_name}: {e}")
            return []
            
    def get_strategy_performance(self, strategy_name: str, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> Dict[str, Any]:
        """Get strategy performance metrics"""
        try:
            executions = [e for e in self.executions if e.strategy_name == strategy_name]
            
            if start_date:
                executions = [e for e in executions if e.execution_time >= start_date]
            if end_date:
                executions = [e for e in executions if e.execution_time <= end_date]
                
            total_executions = len(executions)
            
            if total_executions == 0:
                return {
                    'total_executions': 0,
                    'total_pnl': 0,
                    'avg_pnl': 0,
                    'winning_trades': 0,
                    'losing_trades': 0,
                    'win_rate': 0,
                    'max_drawdown': 0,
                    'sharpe_ratio': 0,
                    'avg_trade_duration': 0
                }
            
            # Basic metrics
            pnl_values = [e.pnl for e in executions if e.pnl is not None]
            total_pnl = sum(pnl_values)
            avg_pnl = total_pnl / len(pnl_values) if pnl_values else 0
            
            winning_trades = len([pnl for pnl in pnl_values if pnl > 0])
            losing_trades = len([pnl for pnl in pnl_values if pnl < 0])
            win_rate = (winning_trades / len(pnl_values) * 100) if pnl_values else 0
            
            # Max Drawdown calculation
            cumulative_pnl = []
            running_total = 0
            for pnl in pnl_values:
                running_total += pnl
                cumulative_pnl.append(running_total)
            
            max_drawdown = 0
            if cumulative_pnl:
                peak = cumulative_pnl[0]
                for value in cumulative_pnl:
                    if value > peak:
                        peak = value
                    drawdown = peak - value
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
            
            # Sharpe Ratio (simplified - assuming risk-free rate = 0)
            sharpe_ratio = 0
            if pnl_values and len(pnl_values) > 1:
                import statistics
                std_dev = statistics.stdev(pnl_values)
                if std_dev > 0:
                    sharpe_ratio = avg_pnl / std_dev
            
            # Average Trade Duration (placeholder - would need open/close pairs)
            avg_trade_duration = 0  # Hours, would need more sophisticated tracking
            
            return {
                'total_executions': total_executions,
                'total_pnl': round(total_pnl, 2),
                'avg_pnl': round(avg_pnl, 2),
                'winning_trades': winning_trades,
                'losing_trades': losing_trades,
                'win_rate': round(win_rate, 2),
                'max_drawdown': round(max_drawdown, 2),
                'sharpe_ratio': round(sharpe_ratio, 3),
                'avg_trade_duration': avg_trade_duration
            }
        except Exception as e:
            logger.error(f"Failed to get performance for {strategy_name}: {e}")
            return {}
            
    def generate_auth_token(self) -> str:
        """Generate a new authentication token"""
        timestamp = str(int(time.time()))
        secret = os.urandom(32).hex()
        token_data = f"{timestamp}:{secret}"
        token = hashlib.sha256(token_data.encode()).hexdigest()
        
        # Update settings with new token
        self.settings['auth_token'] = token
        self.auth_token = token
        self._save_settings()
        
        logger.info("Generated new auth token")
        return token
        
    def validate_auth_token(self, token: str) -> bool:
        """Validate an authentication token"""
        if not self.auth_token:
            return False
        return hmac.compare_digest(self.auth_token, token)