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
        self.settings_file = self.data_dir / "strategy_settings.json"
        self.strategies_file = self.data_dir / "strategies.csv"
        self.executions_file = self.data_dir / "strategy_executions.csv"
        
        # Ensure directories exist
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.strategy_dir.mkdir(parents=True, exist_ok=True)
        
        self.strategies: Dict[str, Strategy] = {}
        self.executions: List[StrategyExecution] = []
        
        self._load_settings()
        self._load_strategies()
        self._load_executions()
        
    def _load_settings(self):
        """Load strategy settings"""
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
        """Save strategy settings"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
            logger.info("Strategy settings saved")
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            
    def _load_strategies(self):
        """Load strategies from CSV file"""
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
                        
                logger.info(f"Loaded {len(self.strategies)} strategies")
            except Exception as e:
                logger.error(f"Failed to load strategies: {e}")
                self.strategies = {}
                
    def _save_strategies(self):
        """Save strategies to CSV file"""
        try:
            with open(self.strategies_file, 'w', newline='') as f:
                if self.strategies:
                    fieldnames = list(asdict(list(self.strategies.values())[0]).keys())
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    
                    for strategy in self.strategies.values():
                        strategy_dict = asdict(strategy)
                        # Convert datetime objects to strings
                        if strategy_dict['created_at']:
                            strategy_dict['created_at'] = strategy_dict['created_at'].isoformat()
                        if strategy_dict['last_triggered']:
                            strategy_dict['last_triggered'] = strategy_dict['last_triggered'].isoformat()
                        else:
                            strategy_dict['last_triggered'] = ''
                        
                        # Handle None values for CSV
                        for key in ['api_endpoint', 'file_path', 'description']:
                            if strategy_dict[key] is None:
                                strategy_dict[key] = ''
                                
                        writer.writerow(strategy_dict)
                        
            logger.info("Strategies saved to CSV")
        except Exception as e:
            logger.error(f"Failed to save strategies: {e}")
            
    def _load_executions(self):
        """Load strategy executions from CSV"""
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
                        
                logger.info(f"Loaded {len(self.executions)} strategy executions")
            except Exception as e:
                logger.error(f"Failed to load executions: {e}")
                self.executions = []
                
    def _save_executions(self):
        """Save strategy executions to CSV"""
        try:
            with open(self.executions_file, 'w', newline='') as f:
                if self.executions:
                    fieldnames = list(asdict(self.executions[0]).keys())
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    
                    for execution in self.executions:
                        execution_dict = asdict(execution)
                        # Convert datetime to string
                        execution_dict['execution_time'] = execution_dict['execution_time'].isoformat()
                        writer.writerow(execution_dict)
                        
            logger.info("Strategy executions saved")
        except Exception as e:
            logger.error(f"Failed to save executions: {e}")
            
    def get_settings(self) -> Dict[str, Any]:
        """Get current settings"""
        return self.settings.copy()
        
    def update_settings(self, new_settings: Dict[str, Any]) -> bool:
        """Update settings"""
        try:
            self.settings.update(new_settings)
            self._save_settings()
            self._load_settings()  # Reload to update instance variables
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
                
            # Check for use_template parameter
            use_template = strategy_data.pop('use_template', False)
            
            strategy = Strategy(**strategy_data)
            
            # Create strategy template if requested
            if strategy.strategy_type == 'internal' and use_template:
                self._create_strategy_template(name)
                strategy.file_path = str(self.strategy_dir / f"{name}.py")
                
            self.strategies[name] = strategy
            self._save_strategies()
            
            logger.info(f"Added strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to add strategy: {e}")
            return False
            
    def _create_strategy_template(self, strategy_name: str):
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
        Generate trading signal
        
        Args:
            symbol: Stock symbol
            data: Market data
            
        Returns:
            Trading signal dictionary or None
            Example: {{'action': 'BUY', 'quantity': 100, 'price': 150.0}}
        """
        # TODO: Implement your strategy logic here
        return None
        
    def on_order_filled(self, order_id: str, symbol: str, quantity: int, price: float):
        """Handle order filled events"""
        print(f"Order filled: {{order_id}} - {{symbol}} {{quantity}}@{{price}}")
        
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
        
        strategy_file = self.strategy_dir / f"{strategy_name}.py"
        try:
            with open(strategy_file, 'w') as f:
                f.write(template_content)
            logger.info(f"Created strategy template: {strategy_file}")
        except Exception as e:
            logger.error(f"Failed to create strategy template: {e}")
            
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
            return strategy_dict
        return None

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
        
    def update_strategy(self, name: str, updates: Dict[str, Any]) -> bool:
        """Update a strategy"""
        try:
            if name not in self.strategies:
                return False
                
            strategy = self.strategies[name]
            for key, value in updates.items():
                if hasattr(strategy, key):
                    setattr(strategy, key, value)
                    
            self._save_strategies()
            logger.info(f"Updated strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to update strategy {name}: {e}")
            return False
            
    def delete_strategy(self, name: str) -> bool:
        """Delete a strategy"""
        try:
            if name not in self.strategies:
                return False
                
            del self.strategies[name]
            self._save_strategies()
            logger.info(f"Deleted strategy: {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete strategy {name}: {e}")
            return False

    def record_execution(self, execution: StrategyExecution):
        """Record a strategy execution"""
        try:
            self.executions.append(execution)
            self._save_executions()

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
            
            if not execution:
                logger.warning(f"No execution record found for order_id: {order_id}")
                return
            
            # Update execution price
            execution.price = fill_price
            
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
            self._save_executions()
            logger.info(f"Strategy execution updated and saved for order {order_id}")
            
        except Exception as e:
            logger.error(f"Failed to update execution price and PnL: {e}")
            
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