#!/usr/bin/env python3
"""
IBKR连接诊断工具
用于检查和诊断IBKR连接问题
"""

import socket
import sys
import os
from pathlib import Path
from src.config import load_project_env
from loguru import logger
import asyncio

try:  # pragma: no cover - utility script, rarely exercised in tests
    from ib_insync import IB
except ImportError:  # pragma: no cover - allow diagnostics in stub mode
    from test.ib_insync_stub import IB

# 加载环境变量
load_project_env()

def check_port_connectivity(host: str, port: int) -> bool:
    """检查端口连通性"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as e:
        logger.error(f"端口连通性检查失败: {e}")
        return False

async def test_ibkr_connection(host: str, port: int, client_id: int) -> bool:
    """测试IBKR连接，自动尝试不同的客户端ID"""
    ib = IB()
    
    # 尝试多个客户端ID
    for attempt in range(5):
        try_client_id = client_id + attempt
        if try_client_id > 32:
            try_client_id = try_client_id - 32
        
        try:
            print(f"   尝试客户端ID: {try_client_id}")
            await ib.connectAsync(host=host, port=port, clientId=try_client_id, timeout=8)
            logger.info(f"IBKR连接测试成功，使用客户端ID: {try_client_id}")
            ib.disconnect()
            return True
        except Exception as e:
            error_msg = str(e)
            if "already connected" in error_msg.lower() or "duplicate" in error_msg.lower():
                print(f"   客户端ID {try_client_id} 已被使用")
                continue
            elif attempt == 4:  # 最后一次尝试
                logger.error(f"IBKR连接测试失败: {e}")
                return False
            else:
                print(f"   客户端ID {try_client_id} 连接失败: {error_msg}")
    
    return False

def main():
    """主诊断函数"""
    print("=" * 60)
    print("IBKR连接诊断工具")
    print("=" * 60)
    
    # 读取配置
    host = os.getenv('IBKR_HOST', '127.0.0.1')
    port = int(os.getenv('IBKR_PORT', '4002'))
    client_id = int(os.getenv('IBKR_CLIENT_ID', '1'))
    paper_trading = os.getenv('IBKR_PAPER_TRADING', 'True').lower() == 'true'
    
    print(f"当前配置:")
    print(f"  主机: {host}")
    print(f"  端口: {port}")
    print(f"  客户端ID: {client_id}")
    print(f"  模拟交易: {paper_trading}")
    print()
    
    # 检查1: 端口连通性
    print("1. 检查端口连通性...")
    if check_port_connectivity(host, port):
        print("   ✓ 端口连通性正常")
    else:
        print("   ✗ 端口连通性失败")
        print("   可能原因:")
        print("   - TWS或IB Gateway未启动")
        print("   - 端口配置错误")
        print("   - 防火墙阻止连接")
        print()
        print("解决方案:")
        if paper_trading:
            print("   1. 启动IB Gateway (Paper Trading)")
            print("   2. 确保端口设置为4002 (Paper) 或 4001 (Live)")
        else:
            print("   1. 启动TWS或IB Gateway (Live Trading)")
            print("   2. 确保端口设置为7497 (TWS) 或 4001 (Gateway Live)")
        print("   3. 在TWS/Gateway中启用API连接")
        print("   4. 检查防火墙设置")
        return
    
    # 检查2: IBKR API连接
    print("2. 测试IBKR API连接...")
    try:
        result = asyncio.run(test_ibkr_connection(host, port, client_id))
        if result:
            print("   ✓ IBKR API连接成功")
            print("\n诊断结果: 连接配置正常")
        else:
            print("   ✗ IBKR API连接失败")
            print("   可能原因:")
            print("   - 客户端ID冲突")
            print("   - API未在TWS/Gateway中启用")
            print("   - 连接超时")
            print()
            print("解决方案:")
            print("   1. 在TWS/Gateway中启用API连接:")
            print("      - 打开 Configure > API > Settings")
            print("      - 勾选 'Enable ActiveX and Socket Clients'")
            print("      - 设置正确的端口号")
            print("   2. 尝试不同的客户端ID (1-32)")
            print("   3. 重启TWS/Gateway")
    except Exception as e:
        print(f"   ✗ 连接测试异常: {e}")
    
    print()
    print("常见端口配置:")
    print("  TWS Live: 7497")
    print("  TWS Paper: 7497")
    print("  IB Gateway Live: 4001")
    print("  IB Gateway Paper: 4002")
    print()
    print("如果问题仍然存在，请检查:")
    print("  1. TWS/IB Gateway是否正在运行")
    print("  2. API设置是否正确配置")
    print("  3. 网络连接是否正常")
    print("  4. 防火墙是否允许连接")

if __name__ == "__main__":
    main()