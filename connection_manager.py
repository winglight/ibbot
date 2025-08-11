from typing import Optional, Dict, Any
from dataclasses import dataclass
from ib_insync import IB, util
from loguru import logger
import os
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv


load_dotenv()

@dataclass
class ConnectionConfig:
    """连接配置类"""
    host: str = '127.0.0.1'
    port: int = 7497
    client_id: int = 1
    paper_trading: bool = True
    
    @classmethod
    def from_env(cls) -> 'ConnectionConfig':
        """从环境变量加载配置"""
        return cls(
            host=os.getenv('IBKR_HOST', '127.0.0.1'),
            port=int(os.getenv('IBKR_PORT', '7497')),
            client_id=int(os.getenv('IBKR_CLIENT_ID', '1')),
            paper_trading=os.getenv('IBKR_PAPER_TRADING', 'True').lower() == 'true'
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'host': self.host,
            'port': self.port,
            'client_id': self.client_id,
            'paper_trading': self.paper_trading
        }

class ConnectionManager:
    """统一的IBKR连接管理器"""
    
    def __init__(self):
        self.ib: Optional[IB] = None
        self.config: ConnectionConfig = ConnectionConfig.from_env()
        self.connected: bool = False
        
    def update_config(self, config: ConnectionConfig) -> None:
        """更新连接配置"""
        if self.connected:
            logger.warning("Connection is active. Disconnect first before updating config.")
            return
        
        self.config = config
        logger.info(f"Connection config updated: {config.to_dict()}")
    
    def _connect_in_thread(self) -> bool:
         """在独立线程中连接到IBKR服务器"""
         def connect_worker():
             try:
                 # 在新线程中创建新的事件循环
                 loop = asyncio.new_event_loop()
                 asyncio.set_event_loop(loop)
                 
                 # 断开现有连接
                 if self.ib:
                     try:
                         self.ib.disconnect()
                     except:
                         pass
                 
                 # 创建新的IB实例
                 self.ib = IB()
                 
                 # 在独立线程的事件循环中连接
                 self.ib.connect(
                     host=self.config.host,
                     port=self.config.port,
                     clientId=self.config.client_id
                 )
                 
                 self.connected = True
                 account_type = "Paper" if self.config.paper_trading else "Live"
                 logger.info(f"Connected to IBKR {account_type} account at {self.config.host}:{self.config.port}")
                 
                 # 保持事件循环运行，不要关闭
                 # 将事件循环保存到实例中
                 self._loop = loop
                 return True
                 
             except Exception as e:
                 logger.error(f"Failed to connect to IBKR: {e}")
                 self.connected = False
                 return False
         
         # 使用线程池执行连接操作
         with ThreadPoolExecutor(max_workers=1) as executor:
             future = executor.submit(connect_worker)
             return future.result(timeout=30)  # 30秒超时
    
    def connect(self) -> bool:
        """连接到IBKR服务器"""
        if self.connected and self.ib and self.ib.isConnected():
            logger.info("Already connected to IBKR")
            return True
        
        logger.info("Connecting to IBKR...")
        return self._connect_in_thread()
    

    
    def disconnect(self) -> None:
        """断开IBKR连接"""
        if self.ib and self.connected:
            try:
                # 检查保存的事件循环是否存在且未关闭
                if hasattr(self, '_loop') and self._loop and not self._loop.is_closed():
                    self.ib.disconnect()
                    logger.info("Disconnected from IBKR")
                    # 关闭事件循环
                    self._loop.close()
                else:
                    # 如果事件循环已关闭，直接标记为断开
                    logger.info("IBKR connection already closed")
            except Exception as e:
                logger.error(f"Error during disconnect: {e}")
            finally:
                self.connected = False
                self.ib = None
                if hasattr(self, '_loop'):
                    delattr(self, '_loop')
    
    def is_connected(self) -> bool:
        """检查连接状态"""
        if not self.ib:
            self.connected = False
            return False
            
        try:
            is_connected = self.ib.isConnected()
            self.connected = is_connected
            return is_connected
        except:
            self.connected = False
            return False
    
    def get_ib_instance(self) -> Optional[IB]:
        """获取IB实例"""
        if self.is_connected():
            return self.ib
        return None
    
    def get_account_info(self) -> Dict[str, Any]:
        """获取账户信息"""
        if not self.is_connected():
            raise Exception("Not connected to IBKR")
            
        try:
            account_values = self.ib.accountValues()
            account_summary = self.ib.accountSummary()
            
            # 提取关键信息
            info = {
                'account_id': '',
                'account_type': 'Paper' if self.config.paper_trading else 'Live',
                'total_cash': 0.0,
                'net_liquidation': 0.0,
                'buying_power': 0.0
            }
            
            # 从account_values中提取信息
            for av in account_values:
                if av.tag == 'AccountCode':
                    info['account_id'] = av.value
                elif av.tag == 'TotalCashValue' and av.currency == 'USD':
                    info['total_cash'] = float(av.value)
                elif av.tag == 'NetLiquidation' and av.currency == 'USD':
                    info['net_liquidation'] = float(av.value)
                elif av.tag == 'BuyingPower' and av.currency == 'USD':
                    info['buying_power'] = float(av.value)
            
            return info
            
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            raise
    
    def get_config(self) -> ConnectionConfig:
        """获取当前配置"""
        return self.config

# 全局连接管理器实例
connection_manager = ConnectionManager()