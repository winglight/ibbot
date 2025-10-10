"""
简化的IBKR客户端实现
基于test.py的简单连接方式，移除复杂的事件循环管理
"""
import os
import asyncio
from typing import Optional, Dict, Any, List, Callable, Mapping, Sequence
from loguru import logger
from ib_async import IB, Stock, Contract, util

class SimpleIBKRClient:
    """简化的IBKR客户端，直接使用ib_async进行连接"""
    
    def __init__(self):
        self.ib: Optional[IB] = None
        self.host = os.getenv('IBKR_HOST', '127.0.0.1')
        self.port = int(os.getenv('IBKR_PORT', '4002'))
        self.client_id = int(os.getenv('IBKR_CLIENT_ID', '10'))
        self._price_listeners: List[Callable[[str, Mapping[str, Any]], None]] = []
        self._disconnect_listeners: List[Callable[[], None]] = []
        
        logger.info(f"SimpleIBKRClient initialized with {self.host}:{self.port}, client_id={self.client_id}")
    
    def connect(self) -> bool:
        """连接到IBKR"""
        try:
            if self.ib is None:
                self.ib = IB()
            
            if self.ib.isConnected():
                logger.info("Already connected to IBKR")
                return True
            
            logger.info(f"Connecting to IBKR at {self.host}:{self.port} with client_id={self.client_id}")
            self.ib.connect(self.host, self.port, clientId=self.client_id)
            
            if self.ib.isConnected():
                logger.info("Successfully connected to IBKR")
                return True
            else:
                logger.error("Failed to connect to IBKR")
                return False
                
        except Exception as e:
            logger.error(f"Error connecting to IBKR: {e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        try:
            if self.ib and self.ib.isConnected():
                logger.info("Disconnecting from IBKR")
                self.ib.disconnect()
                
                # 通知断开连接监听器
                for listener in self._disconnect_listeners:
                    try:
                        listener()
                    except Exception as e:
                        logger.error(f"Error in disconnect listener: {e}")
                        
        except Exception as e:
            logger.error(f"Error disconnecting from IBKR: {e}")
    
    def is_connected(self) -> bool:
        """检查连接状态"""
        return self.ib is not None and self.ib.isConnected()
    
    @property
    def connected(self) -> bool:
        """连接状态属性"""
        return self.is_connected()
    
    def get_ib_instance(self) -> Optional[IB]:
        """获取IB实例"""
        return self.ib if self.is_connected() else None
    
    def add_price_listener(self, callback: Callable[[str, Mapping[str, Any]], None]) -> None:
        """添加价格监听器"""
        self._price_listeners.append(callback)
    
    def add_disconnect_listener(self, callback: Callable[[], None]) -> None:
        """添加断开连接监听器"""
        self._disconnect_listeners.append(callback)
    
    def _resolve_managed_account(self) -> Optional[str]:
        if not self.ib:
            return None

        try:
            accounts = self.ib.managedAccounts()
        except Exception as exc:
            logger.error(f"Error resolving managed accounts: {exc}")
            return None

        if not accounts:
            return None

        if isinstance(accounts, str):
            parts = [part.strip() for part in accounts.split(',') if part.strip()]
            return parts[0] if parts else None

        try:
            for item in accounts:
                text = str(item).strip()
                if text:
                    return text
        except TypeError:
            pass
        return None

    def _call_account_method(self, name: str, account: Optional[str]) -> Sequence[Any]:
        if not self.ib:
            return ()

        method = getattr(self.ib, name, None)
        if method is None:
            return ()

        if account:
            try:
                result = method(account)
                if result:
                    return result
            except TypeError:
                pass
            except Exception as exc:
                logger.error(f"Error calling {name} with account {account}: {exc}")

        try:
            result = method()
        except TypeError:
            return ()
        except Exception as exc:
            logger.error(f"Error calling {name}: {exc}")
            return ()

        return result if result else ()

    def get_account_info(self) -> Dict[str, Any]:
        """获取账户信息"""
        if not self.is_connected():
            logger.warning("Not connected to IBKR")
            return {}

        account_id = self._resolve_managed_account()

        summary_items = self._call_account_method("accountSummary", account_id)
        value_items = self._call_account_method("accountValues", account_id)

        account_info = {
            'account_values': [
                {
                    'tag': getattr(av, 'tag', None),
                    'value': getattr(av, 'value', None),
                    'currency': getattr(av, 'currency', None),
                    'account': getattr(av, 'account', None)
                }
                for av in value_items
            ],
            'account_summary': [
                {
                    'tag': getattr(summary, 'tag', None),
                    'value': getattr(summary, 'value', None),
                    'currency': getattr(summary, 'currency', None),
                    'account': getattr(summary, 'account', None)
                }
                for summary in summary_items
            ]
        }

        return account_info
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """获取持仓信息"""
        if not self.is_connected():
            logger.warning("Not connected to IBKR")
            return []
        
        try:
            positions = self.ib.positions()
            return [
                {
                    'contract': {
                        'symbol': pos.contract.symbol,
                        'secType': pos.contract.secType,
                        'exchange': pos.contract.exchange,
                        'currency': pos.contract.currency
                    },
                    'position': pos.position,
                    'avgCost': pos.avgCost,
                    'account': pos.account
                }
                for pos in positions
            ]
            
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []
    
    def get_historical_data(self, symbol: str, duration: str = "1 D", bar_size: str = "1 min") -> List[Dict[str, Any]]:
        """获取历史数据"""
        if not self.is_connected():
            logger.warning("Not connected to IBKR")
            return []
        
        try:
            contract = Stock(symbol, 'SMART', 'USD')
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow='MIDPOINT',
                useRTH=True
            )
            
            return [
                {
                    'date': bar.date,
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume
                }
                for bar in bars
            ]
            
        except Exception as e:
            logger.error(f"Error getting historical data for {symbol}: {e}")
            return []


# 全局实例
_simple_ibkr_client: Optional[SimpleIBKRClient] = None

def get_simple_ibkr_client() -> SimpleIBKRClient:
    """获取全局SimpleIBKRClient实例"""
    global _simple_ibkr_client
    if _simple_ibkr_client is None:
        _simple_ibkr_client = SimpleIBKRClient()
    return _simple_ibkr_client

def get_ib_client() -> Optional[IB]:
    """获取连接的IB客户端实例"""
    client = get_simple_ibkr_client()
    if not client.is_connected():
        if client.connect():
            return client.get_ib_instance()
        else:
            return None
    return client.get_ib_instance()