#!/usr/bin/env python3
"""
测试简化的IBKRClient
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.simple_ibkr_client import SimpleIBKRClient, get_simple_ibkr_client, get_ib_client
from loguru import logger

def test_simple_ibkr_client():
    """测试SimpleIBKRClient的基本功能"""
    logger.info("=== 测试SimpleIBKRClient ===")
    
    # 创建客户端实例
    client = SimpleIBKRClient()
    logger.info(f"客户端创建成功，连接参数: {client.host}:{client.port}, client_id={client.client_id}")
    
    # 测试连接
    logger.info("尝试连接到IBKR...")
    success = client.connect()
    logger.info(f"连接结果: {'成功' if success else '失败'}")
    
    if success:
        logger.info(f"连接状态: {client.is_connected()}")
        
        # 测试获取账户信息
        logger.info("获取账户信息...")
        account_info = client.get_account_info()
        logger.info(f"账户信息条目数: {len(account_info.get('account_values', []))}")
        
        # 测试获取持仓
        logger.info("获取持仓信息...")
        positions = client.get_positions()
        logger.info(f"持仓数量: {len(positions)}")
        
        # 断开连接
        logger.info("断开连接...")
        client.disconnect()
        logger.info(f"断开后连接状态: {client.is_connected()}")
    
    return success

def test_global_functions():
    """测试全局函数"""
    logger.info("=== 测试全局函数 ===")
    
    # 测试get_simple_ibkr_client
    client = get_simple_ibkr_client()
    logger.info(f"获取全局客户端: {type(client).__name__}")
    
    # 测试get_ib_client
    logger.info("尝试通过get_ib_client获取IB实例...")
    ib = get_ib_client()
    
    if ib:
        logger.info(f"成功获取IB实例: {type(ib).__name__}")
        logger.info(f"IB连接状态: {ib.isConnected()}")
        
        # 断开连接
        client.disconnect()
    else:
        logger.warning("未能获取IB实例")

def test_historical_data():
    """测试历史数据获取"""
    logger.info("=== 测试历史数据获取 ===")
    
    client = get_simple_ibkr_client()
    
    if client.connect():
        logger.info("获取EURUSD历史数据...")
        data = client.get_historical_data("EURUSD", "1 D", "1 hour")
        logger.info(f"获取到 {len(data)} 条历史数据")
        
        if data:
            logger.info(f"第一条数据: {data[0]}")
            logger.info(f"最后一条数据: {data[-1]}")
        
        client.disconnect()

if __name__ == "__main__":
    logger.info("开始测试简化的IBKRClient...")
    
    # 测试基本功能
    test_simple_ibkr_client()
    
    # 测试全局函数
    test_global_functions()
    
    # 测试历史数据（如果连接成功）
    # test_historical_data()
    
    logger.info("测试完成")