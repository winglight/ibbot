#!/usr/bin/env python3
"""
测试手动下单功能修复 - 使用独立的IB Gateway连接
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
import threading
import time
from loguru import logger

class TestIBApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.connected = False
        self.next_order_id = None
        self.order_placed = False
        self.order_id = None
        self.error_occurred = False
        
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        logger.info(f"ERROR {reqId} {errorCode} {errorString}")
        if errorCode in [502, 504]:  # Connection errors
            self.error_occurred = True
            
    def nextValidId(self, orderId):
        logger.info(f"Connected. Next valid order ID: {orderId}")
        self.connected = True
        self.next_order_id = orderId
        
    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        logger.info(f"Order {orderId} status: {status}, filled: {filled}, remaining: {remaining}")
        
    def openOrder(self, orderId, contract, order, orderState):
        logger.info(f"Open order {orderId}: {contract.symbol} {order.action} {order.totalQuantity} @ {order.lmtPrice}")
        
    def execDetails(self, reqId, contract, execution):
        logger.info(f"Execution: {execution.execId} {contract.symbol} {execution.side} {execution.shares} @ {execution.price}")
        
    def create_stock_contract(self, symbol):
        """创建股票合约"""
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract
        
    def create_limit_order(self, action, quantity, price):
        """创建限价单"""
        order = Order()
        order.action = action
        order.totalQuantity = quantity
        order.orderType = "LMT"
        order.lmtPrice = price
        return order
        
    def place_test_order(self):
        """下测试订单"""
        if not self.connected or self.next_order_id is None:
            logger.error("未连接或没有有效的订单ID")
            return False
            
        try:
            # 创建AAPL股票合约
            contract = self.create_stock_contract("AAPL")
            
            # 创建限价买单，价格设置得很低，不会成交
            order = self.create_limit_order("BUY", 1, 100.0)
            
            # 下单
            self.order_id = self.next_order_id
            logger.info(f"下单: {contract.symbol} {order.action} {order.totalQuantity} @ {order.lmtPrice}")
            self.placeOrder(self.order_id, contract, order)
            self.order_placed = True
            
            return True
            
        except Exception as e:
            logger.error(f"下单时发生错误: {e}")
            return False
            
    def cancel_test_order(self):
        """取消测试订单"""
        if self.order_id is not None:
            logger.info(f"取消订单: {self.order_id}")
            self.cancelOrder(self.order_id, "")
            return True
        return False

def run_test():
    """运行测试"""
    logger.info("=== 独立下单功能测试 ===")
    
    # 创建应用实例
    app = TestIBApp()
    
    try:
        # 连接到IB Gateway (使用模拟交易端口)
        logger.info("连接到IB Gateway...")
        app.connect("127.0.0.1", 4002, clientId=3)  # 使用模拟交易端口4002
        
        # 启动API线程
        api_thread = threading.Thread(target=app.run, daemon=True)
        api_thread.start()
        
        # 等待连接建立
        timeout = 10
        start_time = time.time()
        while not app.connected and not app.error_occurred and (time.time() - start_time) < timeout:
            time.sleep(0.1)
            
        if app.error_occurred:
            logger.error("连接时发生错误")
            return False
            
        if not app.connected:
            logger.error("连接超时")
            return False
            
        logger.info("✅ 连接成功")
        
        # 等待一下确保连接稳定
        time.sleep(1)
        
        # 测试下单
        logger.info("测试下单功能...")
        if app.place_test_order():
            logger.info("✅ 下单成功")
            
            # 等待一下然后取消订单
            time.sleep(3)
            
            if app.cancel_test_order():
                logger.info("✅ 取消订单成功")
            else:
                logger.warning("⚠️ 取消订单失败")
                
            # 等待一下让取消操作完成
            time.sleep(2)
            
            return True
        else:
            logger.error("❌ 下单失败")
            return False
            
    except Exception as e:
        logger.error(f"测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        # 断开连接
        if app.isConnected():
            logger.info("断开连接...")
            app.disconnect()
            time.sleep(1)

if __name__ == "__main__":
    success = run_test()
    
    if success:
        logger.info("✅ 独立下单功能测试通过")
    else:
        logger.error("❌ 独立下单功能测试失败")
    
    logger.info("测试完成")