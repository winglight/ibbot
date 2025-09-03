#!/usr/bin/env python3

import asyncio
from src.connection_manager import ConnectionManager

def test_connection():
    cm = ConnectionManager()
    try:
        print("开始连接测试...")
        result = cm.connect()
        print(f"连接结果: {result}")
        print(f"连接状态: {cm.is_connected()}")
        
        if cm.is_connected():
            print("✓ 连接成功!")
        else:
            print("✗ 连接失败")
            
    except Exception as e:
        print(f"连接异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if hasattr(cm, 'ib') and cm.ib:
            cm.disconnect()
            print("连接已断开")

if __name__ == "__main__":
    test_connection()