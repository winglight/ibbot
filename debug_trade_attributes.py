#!/usr/bin/env python3
"""
调试脚本：查看ib-insync Trade对象的所有属性
用于确认是否有commission、realizedPNL等字段
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.ibkr_client import IBKRClient
from loguru import logger

def debug_trade_attributes():
    """调试Trade对象的属性"""
    client = IBKRClient()
    
    try:
        print("连接到IBKR...")
        if not client.connect():
            print("连接失败")
            return
            
        print("连接成功，获取trades...")
        ib = client.ib
        if not ib:
            print("无法获取IB实例")
            return
            
        # 获取所有trades（包括已完成的）
        all_trades = ib.trades()
        print(f"找到 {len(all_trades)} 个trades")
        
        if not all_trades:
            print("没有找到任何trades")
            return
            
        # 查看第一个trade的所有属性
        trade = all_trades[0]
        print(f"\n=== Trade对象属性 ===")
        print(f"Trade类型: {type(trade)}")
        
        # 打印所有属性
        for attr in dir(trade):
            if not attr.startswith('_'):
                try:
                    value = getattr(trade, attr)
                    if not callable(value):
                        print(f"{attr}: {value} (类型: {type(value)})")
                except Exception as e:
                    print(f"{attr}: 无法获取 - {e}")
        
        # 特别检查commission相关属性
        print(f"\n=== Commission相关属性 ===")
        commission_attrs = ['commission', 'commissionReport', 'commissions']
        for attr in commission_attrs:
            if hasattr(trade, attr):
                value = getattr(trade, attr)
                print(f"{attr}: {value}")
            else:
                print(f"{attr}: 不存在")
        
        # 检查PnL相关属性
        print(f"\n=== PnL相关属性 ===")
        pnl_attrs = ['pnl', 'realizedPNL', 'unrealizedPNL', 'realizedPnL', 'unrealizedPnL']
        for attr in pnl_attrs:
            if hasattr(trade, attr):
                value = getattr(trade, attr)
                print(f"{attr}: {value}")
            else:
                print(f"{attr}: 不存在")
                
        # 检查fills
        print(f"\n=== Fills信息 ===")
        if hasattr(trade, 'fills') and trade.fills:
            print(f"Fills数量: {len(trade.fills)}")
            for i, fill in enumerate(trade.fills):
                print(f"Fill {i}:")
                for attr in dir(fill):
                    if not attr.startswith('_'):
                        try:
                            value = getattr(fill, attr)
                            if not callable(value):
                                print(f"  {attr}: {value}")
                        except Exception as e:
                            print(f"  {attr}: 无法获取 - {e}")
        else:
            print("没有fills信息")
            
    except Exception as e:
        print(f"调试过程中出错: {e}")
        import traceback
        traceback.print_exc()
    finally:
        client.disconnect()
        print("\n调试完成")

if __name__ == "__main__":
    debug_trade_attributes()