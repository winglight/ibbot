"""
简化的IB连接管理器
基于ib_async的简单连接示例重新实现
"""
import os
import time
import threading
from typing import Optional, Any
from loguru import logger
from dataclasses import dataclass

try:
    from ib_async import IB
except ImportError:
    IB = None

# 创建专用logger
simple_logger = logger.bind(module="simple_connection")


@dataclass
class SimpleConnectionConfig:
    """简化的连接配置"""
    host: str = '127.0.0.1'
    port: int = 4002
    client_id: int = 1
    
    @classmethod
    def from_env(cls) -> 'SimpleConnectionConfig':
        """从环境变量加载配置"""
        return cls(
            host=os.getenv('IBKR_HOST', '127.0.0.1'),
            port=int(os.getenv('IBKR_PORT', '4002')),
            client_id=int(os.getenv('IBKR_CLIENT_ID', '1'))
        )


class SimpleConnectionManager:
    """简化的IB连接管理器"""
    
    def __init__(self):
        if IB is None:
            raise RuntimeError("ib_async not available")
        
        self.config = SimpleConnectionConfig.from_env()
        self.ib: Optional[IB] = None
        self.connected = False
        self._lock = threading.Lock()
        
        simple_logger.info("简化连接管理器初始化完成")
    
    def connect(self) -> bool:
        """连接到IB"""
        with self._lock:
            if self.connected and self.ib:
                simple_logger.info("已经连接到IB")
                return True
            
            try:
                # 创建新的IB实例
                self.ib = IB()
                
                # 简单连接，就像示例中那样
                simple_logger.info(f"连接到IB: {self.config.host}:{self.config.port}, 客户端ID: {self.config.client_id}")
                self.ib.connect(
                    host=self.config.host,
                    port=self.config.port,
                    clientId=self.config.client_id
                )
                
                self.connected = True
                simple_logger.info("IB连接成功")
                return True
                
            except Exception as e:
                simple_logger.error(f"IB连接失败: {e}")
                self.connected = False
                if self.ib:
                    try:
                        self.ib.disconnect()
                    except:
                        pass
                    self.ib = None
                return False
    
    def disconnect(self) -> None:
        """断开IB连接"""
        with self._lock:
            if self.ib:
                try:
                    self.ib.disconnect()
                    simple_logger.info("IB连接已断开")
                except Exception as e:
                    simple_logger.warning(f"断开连接时出错: {e}")
                finally:
                    self.ib = None
                    self.connected = False
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.connected and self.ib is not None and self.ib.isConnected()
    
    def get_ib_instance(self) -> Optional[IB]:
        """获取IB实例"""
        if self.is_connected():
            return self.ib
        return None
    
    def reconnect(self) -> bool:
        """重新连接"""
        simple_logger.info("尝试重新连接IB")
        self.disconnect()
        time.sleep(1)  # 短暂等待
        return self.connect()


# 全局实例
_simple_connection_manager: Optional[SimpleConnectionManager] = None


def get_simple_connection_manager() -> SimpleConnectionManager:
    """获取全局简化连接管理器实例"""
    global _simple_connection_manager
    if _simple_connection_manager is None:
        _simple_connection_manager = SimpleConnectionManager()
    return _simple_connection_manager


def get_ib_client() -> Optional[IB]:
    """获取IB客户端实例"""
    manager = get_simple_connection_manager()
    if not manager.is_connected():
        if manager.connect():
            return manager.get_ib_instance()
        return None
    return manager.get_ib_instance()