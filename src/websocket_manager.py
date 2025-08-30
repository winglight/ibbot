"""
WebSocket连接管理器
用于管理WebSocket连接和广播消息
"""

import asyncio
import json
from typing import Set, Dict, Any
from fastapi import WebSocket
from loguru import logger

# 为websocket模块创建专用logger
websocket_logger = logger.bind(module="websocket", request_id="-")


class WebSocketManager:
    """WebSocket连接管理器"""
    
    def __init__(self):
        self.connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket):
        """接受WebSocket连接"""
        await websocket.accept()
        self.connections.add(websocket)
        websocket_logger.info(f"WebSocket连接已建立，当前连接数: {len(self.connections)}")
    
    def disconnect(self, websocket: WebSocket):
        """断开WebSocket连接"""
        self.connections.discard(websocket)
        websocket_logger.info(f"WebSocket连接已断开，当前连接数: {len(self.connections)}")
    
    async def broadcast(self, message: Dict[str, Any]):
        """向所有连接的客户端广播消息"""
        if not self.connections:
            return
        
        # 将消息转换为JSON字符串
        try:
            message_str = json.dumps(message)
        except (TypeError, ValueError) as e:
            websocket_logger.error(f"无法序列化广播消息: {e}")
            return
        
        # 收集断开的连接
        disconnected = set()
        
        # 向所有连接发送消息
        for connection in self.connections.copy():
            try:
                await connection.send_text(message_str)
            except Exception as e:
                websocket_logger.warning(f"向WebSocket连接发送消息失败: {e}")
                disconnected.add(connection)
        
        # 清理断开的连接
        for connection in disconnected:
            self.connections.discard(connection)
        
        if disconnected:
            websocket_logger.info(f"清理了 {len(disconnected)} 个断开的WebSocket连接")
        
        websocket_logger.debug(f"广播消息到 {len(self.connections)} 个连接: {message}")


# 全局WebSocket管理器实例
websocket_manager = WebSocketManager()