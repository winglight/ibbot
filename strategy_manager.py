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
    
    def __init__(self, data_dir: str = "data", strategy_dir: str = "project/strategy"):
        self.data_dir = Path(data_dir)
        self.strategy_dir = Path(strategy_dir)
        self.settings_file = self.data_dir / "settings.json"
        self.strategies_file = self.data_dir / "strategies.csv"
        self.executions_file = self.data_dir / "strategy_executions.csv"
        
        self.strategies: Dict[str, Strategy] = {}
        self.settings = self._load_settings()
        self._load_strategies()
        
    def _load_settings(self) -> Dict[str, Any]:
        """Load strategy settings"""
        try:
            if self.settings_file.exists():
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            else:
                # Default settings
                default_settings = {
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
                    "strategy_path": "project/strategy/",
                    "enable_auth": False,
                    "auth_token": None
                }
                self._save_settings(default_settings)
                return default_settings
        except Exception as e:
            logger.error(f"Error loading settings: {e}")
            return {}
    
    def _save_settings(self, settings: Dict[str, Any]):
        """Save strategy settings"""
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            self.settings = settings
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
    
    def _load_strategies(self):
        """Load strategies from CSV file"""
        try:
            if self.strategies_file.exists():
                with open(self.strategies_file, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        strategy = Strategy(
                            name=row['name'],
                            strategy_type=row['strategy_type'],
                            enabled=row['enabled'].lower() == 'true',
                            api_endpoint=row.get('api_endpoint') or None,
                            file_path=row.get('file_path') or None,
                            description=row.get('description') or None,
                            created_at=datetime.fromisoformat(row['created_at']) if row.get('created_at') else datetime.now(),
                            trigger_count=int(row.get('trigger_count', 0)),
                            last_triggered=datetime.fromisoformat(row['last_triggered']) if row.get('last_triggered') else None
                        )
                        self.strategies[strategy.name] = strategy
        except Exception as e:
            logger.error(f"Error loading strategies: {e}")
    
    def _save_strategies(self):
        """Save strategies to CSV file"""
        try:
            with open(self.strategies_file, 'w', newline='', encoding='utf-8') as f:
                fieldnames = ['name', 'strategy_type', 'enabled', 'api_endpoint', 'file_path', 
                             'description', 'created_at', 'trigger_count', 'last_triggered']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                
                for strategy in self.strategies.values():
                    row = asdict(strategy)
                    row['created_at'] = strategy.created_at.isoformat() if strategy.created_at else ''
                    row['last_triggered'] = strategy.last_triggered.isoformat() if strategy.last_triggered else ''
                    writer.writerow(row)
        except Exception as e:
            logger.error(f"Error saving strategies: {e}")
    
    def get_settings(self) -> Dict[str, Any]:
        """Get strategy settings"""
        return self.settings
    
    def update_settings(self, new_settings: Dict[str, Any]):
        """Update strategy settings"""
        self.settings.update(new_settings)
        self._save_settings(self.settings)
    
    def add_strategy(self, strategy_data: Dict[str, Any]) -> bool:
        """Add a new strategy"""
        try:
            if strategy_data['name'] in self.strategies:
                raise ValueError(f"Strategy '{strategy_data['name']}' already exists")
            
            strategy = Strategy(
                name=strategy_data['name'],
                strategy_type=strategy_data['strategy_type'],
                enabled=strategy_data.get('enabled', True),
                api_endpoint=strategy_data.get('api_endpoint'),
                file_path=strategy_data.get('file_path'),
                description=strategy_data.get('description')
            )
            
            self.strategies[strategy.name] = strategy
            self._save_strategies()
            
            # Create strategy template if internal strategy
            if strategy.strategy_type == 'internal' and strategy_data.get('use_template', False):
                self._create_strategy_template(strategy.name)
            
            return True
        except Exception as e:
            logger.error(f"Error adding strategy: {e}")
            return False
    
    def get_strategies(self) -> List[Dict[str, Any]]:
        """Get all strategies"""
        result = []
        for strategy in self.strategies.values():
            strategy_dict = asdict(strategy)
            strategy_dict['created_at'] = strategy.created_at.isoformat() if strategy.created_at else ''
            strategy_dict['last_triggered'] = strategy.last_triggered.isoformat() if strategy.last_triggered else ''
            result.append(strategy_dict)
        return result
    
    def get_strategy(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific strategy"""
        if name in self.strategies:
            strategy = self.strategies[name]
            strategy_dict = asdict(strategy)
            strategy_dict['created_at'] = strategy.created_at.isoformat() if strategy.created_at else ''
            strategy_dict['last_triggered'] = strategy.last_triggered.isoformat() if strategy.last_triggered else ''
            return strategy_dict
        return None
    
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
            return True
        except Exception as e:
            logger.error(f"Error updating strategy: {e}")
            return False
    
    def delete_strategy(self, name: str) -> bool:
        """Delete a strategy"""
        try:
            if name in self.strategies:
                del self.strategies[name]
                self._save_strategies()
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting strategy: {e}")
            return False
    
    def record_execution(self, execution: StrategyExecution):
        """Record strategy execution"""
        try:
            # Update strategy trigger count and last triggered time
            if execution.strategy_name in self.strategies:
                strategy = self.strategies[execution.strategy_name]
                strategy.trigger_count += 1
                strategy.last_triggered = execution.execution_time
                self._save_strategies()
            
            # Save execution record
            file_exists = self.executions_file.exists()
            with open(self.executions_file, 'a', newline='', encoding='utf-8') as f:
                fieldnames = ['strategy_name', 'execution_time', 'order_id', 'symbol', 'action', 
                             'quantity', 'price', 'status', 'pnl']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                
                if not file_exists:
                    writer.writeheader()
                
                row = asdict(execution)
                row['execution_time'] = execution.execution_time.isoformat()
                writer.writerow(row)
                
        except Exception as e:
            logger.error(f"Error recording execution: {e}")
    
    def get_strategy_performance(self, strategy_name: str, start_date: datetime = None, end_date: datetime = None) -> Dict[str, Any]:
        """Get strategy performance metrics"""
        try:
            if not self.executions_file.exists():
                return {}
            
            executions = []
            with open(self.executions_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row['strategy_name'] == strategy_name:
                        execution_time = datetime.fromisoformat(row['execution_time'])
                        if start_date and execution_time < start_date:
                            continue
                        if end_date and execution_time > end_date:
                            continue
                        executions.append(row)
            
            if not executions:
                return {"trade_count": 0}
            
            # Calculate performance metrics
            total_pnl = sum(float(ex.get('pnl', 0)) for ex in executions if ex.get('pnl'))
            trade_count = len(executions)
            winning_trades = len([ex for ex in executions if float(ex.get('pnl', 0)) > 0])
            win_rate = (winning_trades / trade_count) * 100 if trade_count > 0 else 0
            
            # Calculate average trade duration (placeholder)
            avg_trade_duration = "N/A"  # Would need more data to calculate
            
            # Calculate max drawdown (simplified)
            pnls = [float(ex.get('pnl', 0)) for ex in executions if ex.get('pnl')]
            cumulative_pnl = []
            running_sum = 0
            for pnl in pnls:
                running_sum += pnl
                cumulative_pnl.append(running_sum)
            
            max_drawdown = 0
            if cumulative_pnl:
                peak = cumulative_pnl[0]
                for pnl in cumulative_pnl:
                    if pnl > peak:
                        peak = pnl
                    drawdown = peak - pnl
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
            
            # Calculate Sharpe ratio (simplified)
            if len(pnls) > 1:
                import statistics
                avg_return = statistics.mean(pnls)
                std_return = statistics.stdev(pnls)
                sharpe_ratio = avg_return / std_return if std_return != 0 else 0
            else:
                sharpe_ratio = 0
            
            return {
                "pnl": total_pnl,
                "trade_count": trade_count,
                "win_rate": win_rate,
                "max_drawdown": max_drawdown,
                "sharpe_ratio": sharpe_ratio,
                "avg_trade_duration": avg_trade_duration
            }
            
        except Exception as e:
            logger.error(f"Error calculating performance: {e}")
            return {}
    
    def _create_strategy_template(self, strategy_name: str):
        """Create a Python strategy template"""
        template_content = f'''"""
Strategy: {strategy_name}
Auto-generated strategy template
"""

from datetime import datetime
from loguru import logger


class {strategy_name.replace(' ', '').replace('-', '_')}Strategy:
    def __init__(self, ibkr_client):
        self.ibkr_client = ibkr_client
        self.name = "{strategy_name}"
        self.enabled = True
        
    def initialize(self):
        """Initialize the strategy"""
        logger.info(f"Initializing strategy: {{self.name}}")
        
    def generate_signals(self, market_data):
        """Generate trading signals based on market data"""
        # Implement your signal generation logic here
        # Return format: [{{"symbol": "AAPL", "action": "BUY", "quantity": 100, "price": 150.0}}]
        return []
        
    def execute_order(self, signal):
        """Execute trading order"""
        try:
            if not self.enabled:
                return None
                
            # Place order using IBKR client
            if signal["action"] == "BUY":
                trade = self.ibkr_client.place_market_order(
                    symbol=signal["symbol"],
                    quantity=signal["quantity"],
                    action="BUY"
                )
            elif signal["action"] == "SELL":
                trade = self.ibkr_client.place_market_order(
                    symbol=signal["symbol"],
                    quantity=signal["quantity"],
                    action="SELL"
                )
            
            if trade:
                logger.info(f"Order placed: {{signal}}")
                return trade.order.orderId
            else:
                logger.error(f"Failed to place order: {{signal}}")
                return None
                
        except Exception as e:
            logger.error(f"Error executing order: {{e}}")
            return None
            
    def on_market_data(self, symbol, data):
        """Handle incoming market data"""
        signals = self.generate_signals(data)
        for signal in signals:
            self.execute_order(signal)
            
    def log_message(self, message):
        """Log strategy message"""
        logger.info(f"[{{self.name}}] {{message}}")


# Strategy instance
def create_strategy(ibkr_client):
    return {strategy_name.replace(' ', '').replace('-', '_')}Strategy(ibkr_client)
'''
        
        try:
            strategy_file = self.strategy_dir / f"{strategy_name.replace(' ', '_').lower()}.py"
            with open(strategy_file, 'w', encoding='utf-8') as f:
                f.write(template_content)
            logger.info(f"Strategy template created: {strategy_file}")
        except Exception as e:
            logger.error(f"Error creating strategy template: {e}")
    
    def validate_auth_token(self, token: str) -> bool:
        """Validate API authentication token"""
        if not self.settings.get('enable_auth', False):
            return True
        
        expected_token = self.settings.get('auth_token')
        if not expected_token:
            return True
        
        return hmac.compare_digest(token, expected_token)
    
    def generate_auth_token(self) -> str:
        """Generate a new authentication token"""
        import secrets
        token = secrets.token_urlsafe(32)
        self.settings['auth_token'] = token
        self._save_settings(self.settings)
        return token