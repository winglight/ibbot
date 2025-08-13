from typing import Optional, Dict, Any
from dataclasses import dataclass
from ib_insync import IB, util
from loguru import logger
import os
import threading
import asyncio
import time
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
        self._connection_thread_alive: bool = False
        self._connection_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
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
                
                # 保存事件循环引用
                self._loop = loop
                self._connection_thread_alive = True
                
                # 保持事件循环运行，直到明确要求断开连接
                try:
                    while self._connection_thread_alive and self.ib.isConnected():
                        try:
                            loop.run_until_complete(asyncio.sleep(0.1))
                        except RuntimeError as e:
                            if "Event loop stopped before Future completed" in str(e):
                                logger.debug("Event loop stopped signal received")
                                break
                            raise
                except Exception as e:
                    logger.error(f"Connection loop error: {e}")
                finally:
                    self.connected = False
                    # 安全关闭事件循环
                    try:
                        if not loop.is_closed():
                            # 取消所有挂起的任务
                            pending_tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
                            for task in pending_tasks:
                                task.cancel()
                            # 等待任务完成或取消
                            if pending_tasks:
                                loop.run_until_complete(asyncio.gather(*pending_tasks, return_exceptions=True))
                            loop.close()
                    except Exception as e:
                        logger.debug(f"Error closing loop in thread: {e}")
                    logger.info("IBKR connection loop ended")
                    
            except Exception as e:
                logger.error(f"Failed to connect to IBKR: {e}")
                self.connected = False
        
        # 使用守护线程而不是线程池，确保线程能够持续运行
        self._connection_thread = threading.Thread(target=connect_worker, daemon=True)
        self._connection_thread.start()
        
        # 等待连接建立
        import time
        max_wait = 30  # 最大等待30秒
        wait_time = 0
        while wait_time < max_wait:
            time.sleep(0.5)
            wait_time += 0.5
            if self.connected:
                logger.info("IBKR connection established successfully")
                return True
        
        logger.error("IBKR connection timeout")
        return False
    
    def connect(self) -> bool:
        """连接到IBKR服务器"""
        if self.connected and self.ib and self.ib.isConnected():
            logger.info("Already connected to IBKR")
            return True
        
        logger.info("Connecting to IBKR...")
        return self._connect_in_thread()
    

    
    def disconnect(self) -> None:
        """断开与IBKR服务器的连接，并停止事件循环"""
        try:
            # 标记线程应当停止
            self._connection_thread_alive = False
            
            # 安全断开 IB
            if self.ib and self.ib.isConnected():
                try:
                    self.ib.disconnect()
                    logger.info("Disconnected from IBKR")
                except Exception as e:
                    logger.warning(f"Error while disconnecting IB: {e}")
            
            # 关闭事件循环
            if self._loop and not self._loop.is_closed():
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                    time.sleep(0.2)
                    self._loop.close()
                except Exception as e:
                    logger.debug(f"Error closing loop: {e}")
            
            # 等待线程结束
            if self._connection_thread and self._connection_thread.is_alive():
                try:
                    self._connection_thread.join(timeout=1.0)
                except Exception:
                    pass
            
            # 清理状态
            self.connected = False
            self.ib = None
            self._loop = None
            self._connection_thread = None
        except Exception as e:
            logger.error(f"Disconnect error: {e}")
        
        # 清理遗留代码已删除

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