#!/usr/bin/env python3
"""
测试简化的IB连接管理器
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from src.simple_connection_manager import get_simple_connection_manager, get_ib_client
from loguru import logger

def test_simple_connection():
    """测试简化连接"""
    logger.info("开始测试简化的IB连接")
    
    # 获取连接管理器
    manager = get_simple_connection_manager()
    
    # 测试连接
    if manager.connect():
        logger.info("连接成功！")
        
        # 获取IB客户端
        ib = manager.get_ib_instance()
        if ib:
            logger.info(f"IB客户端状态: 已连接={ib.isConnected()}")
            
            # 简单测试：获取账户信息
            try:
                accounts = ib.managedAccounts()
                logger.info(f"管理的账户: {accounts}")
            except Exception as e:
                logger.warning(f"获取账户信息失败: {e}")
        
        # 断开连接
        manager.disconnect()
        logger.info("连接已断开")
    else:
        logger.error("连接失败")

def test_convenience_function():
    """测试便利函数"""
    logger.info("测试便利函数 get_ib_client()")
    
    ib = get_ib_client()
    if ib:
        logger.info("通过便利函数获取到IB客户端")
        logger.info(f"连接状态: {ib.isConnected()}")
        
        # 清理
        manager = get_simple_connection_manager()
        manager.disconnect()
    else:
        logger.error("便利函数返回None")

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("简化IB连接管理器测试")
    logger.info("=" * 50)
    
    test_simple_connection()
    
    logger.info("-" * 30)
    
    test_convenience_function()
    
    logger.info("测试完成")