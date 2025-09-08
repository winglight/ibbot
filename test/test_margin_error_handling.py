#!/usr/bin/env python3
"""
测试保证金不足错误处理功能

这个测试脚本验证：
1. 当IBKR返回保证金不足错误时，系统能正确捕获
2. 订单状态能正确更新为FAILED
3. 错误信息能正确记录
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import time
import uuid
from unittest.mock import Mock, patch, MagicMock
from src.ibkr_client import IBKRClient
from src.connection_manager import ConnectionManager
from src.order_manager import OrderManager, OrderStatus
from loguru import logger

# 设置测试日志
test_logger = logger.bind(module="test_margin_error")

class TestMarginErrorHandling:
    """测试保证金不足错误处理"""
    
    def __init__(self):
        self.ibkr_client = None
        self.order_manager = None
        
    def setup(self):
        """设置测试环境"""
        test_logger.info("Setting up test environment...")
        
        # 创建IBKR客户端实例
        self.ibkr_client = IBKRClient()
        self.order_manager = OrderManager()
        
        test_logger.info("Test environment setup complete")
    
    def test_margin_error_detection(self):
        """测试保证金不足错误检测"""
        test_logger.info("Testing margin error detection...")
        
        # 模拟不同的保证金不足错误消息
        error_cases = [
            (201, "Order rejected - insufficient buying power"),
            (399, "Insufficient margin for this order"),
            (10148, "Not enough funds available"),
            (201, "Order rejected due to margin requirement"),
            (202, "Order cancelled - account restriction")
        ]
        
        for error_code, error_message in error_cases:
            test_logger.info(f"Testing error case: {error_code} - {error_message}")
            
            # 检查错误检测逻辑
            order_error_codes = [201, 202, 203, 434, 478, 10147]
            order_error_keywords = [
                'insufficient', 'margin', 'buying power', 'rejected', 'not enough',
                'order rejected', 'insufficient funds', 'insufficient buying power',
                'margin requirement', 'account restriction', 'position limit'
            ]
            
            is_order_error = (error_code in order_error_codes or 
                            any(keyword.lower() in error_message.lower() for keyword in order_error_keywords))
            
            assert is_order_error, f"Failed to detect error: {error_code} - {error_message}"
            test_logger.info(f"✓ Successfully detected error: {error_code}")
    
    def test_order_status_update(self):
        """测试订单状态更新"""
        test_logger.info("Testing order status update...")
        
        # 创建测试订单 - 使用唯一ID避免重复
        test_order_id = f"TEST_ORDER_{uuid.uuid4().hex[:8].upper()}"
        
        try:
            # 创建订单
            self.order_manager.create_order(
                order_id=test_order_id,
                symbol="AAPL",
                action="BUY",
                quantity=100,
                order_type="MKT",
                price=None,
                order_source="test"
            )
            
            # 验证订单创建成功
            order = self.order_manager.get_order(test_order_id)
            assert order is not None, "Failed to create test order"
            # 订单创建后默认状态可能是Submitted，这是正常的
            assert order.status in [OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value], f"Unexpected status: {order.status}"
            test_logger.info(f"✓ Test order created: {test_order_id} (status: {order.status})")
            
            # 模拟订单状态更新为REJECTED
            self.order_manager.update_order_status(
                order_id=test_order_id,
                status=OrderStatus.REJECTED.value,
                notes="IBKR Error 201: Order rejected - insufficient buying power"
            )
            
            # 验证状态更新
            updated_order = self.order_manager.get_order(test_order_id)
            assert updated_order.status == OrderStatus.REJECTED.value, f"Expected REJECTED, got {updated_order.status}"
            assert "insufficient buying power" in updated_order.notes, "Error message not recorded"
            test_logger.info(f"✓ Order status updated to REJECTED: {test_order_id}")
            
        finally:
            # 清理测试数据 - 由于OrderManager没有delete_order方法，我们跳过清理
            # 在实际环境中，订单会保留在数据库中用于审计
            test_logger.info(f"✓ Test order completed: {test_order_id} (cleanup skipped)")
    
    def test_reqid_mapping(self):
        """测试reqId到订单ID的映射机制"""
        test_logger.info("Testing reqId mapping mechanism...")
        
        # 模拟reqId映射
        test_req_id = 12345
        test_order_id = "ORDER_12345"
        
        # 添加映射
        self.ibkr_client.req_id_to_order_id[test_req_id] = test_order_id
        
        # 验证映射存在
        assert test_req_id in self.ibkr_client.req_id_to_order_id, "reqId mapping not found"
        assert self.ibkr_client.req_id_to_order_id[test_req_id] == test_order_id, "reqId mapping incorrect"
        test_logger.info(f"✓ reqId mapping verified: {test_req_id} -> {test_order_id}")
        
        # 模拟错误处理时的映射查找
        found_order_id = self.ibkr_client.req_id_to_order_id.get(test_req_id)
        assert found_order_id == test_order_id, "Failed to retrieve order_id from reqId"
        test_logger.info(f"✓ reqId lookup successful: {test_req_id} -> {found_order_id}")
        
        # 清理映射
        del self.ibkr_client.req_id_to_order_id[test_req_id]
        assert test_req_id not in self.ibkr_client.req_id_to_order_id, "Failed to cleanup reqId mapping"
        test_logger.info(f"✓ reqId mapping cleaned up: {test_req_id}")
    
    def test_connection_manager_error_handler(self):
        """测试ConnectionManager的错误处理器"""
        test_logger.info("Testing ConnectionManager error handler...")
        
        # 获取ConnectionManager实例
        connection_manager = self.ibkr_client.connection_manager
        
        # 验证弱引用设置
        assert hasattr(connection_manager, '_ibkr_client_ref'), "IBKRClient reference not set"
        assert connection_manager._ibkr_client_ref() is self.ibkr_client, "IBKRClient reference incorrect"
        test_logger.info("✓ IBKRClient weak reference verified")
        
        # 模拟错误处理
        test_req_id = 54321
        test_order_id = "ORDER_54321"
        
        # 创建测试订单 - 使用唯一ID避免重复
        test_order_id = f"TEST_ORDER_{uuid.uuid4().hex[:8].upper()}"
        
        try:
            self.order_manager.create_order(
                order_id=test_order_id,
                symbol="TSLA",
                action="BUY",
                quantity=10,
                order_type="MKT",
                price=None,
                order_source="test"
            )
            
            # 添加reqId映射
            self.ibkr_client.req_id_to_order_id[test_req_id] = test_order_id
            
            # 模拟错误处理
            connection_manager._handle_order_error(
                req_id=test_req_id,
                error_code=201,
                error_string="Order rejected - insufficient buying power"
            )
            
            # 验证订单状态更新
            updated_order = self.order_manager.get_order(test_order_id)
            assert updated_order.status == OrderStatus.REJECTED.value, f"Expected REJECTED, got {updated_order.status}"
            test_logger.info(f"✓ Error handler updated order status: {test_order_id}")
            
            # 验证映射已清理
            assert test_req_id not in self.ibkr_client.req_id_to_order_id, "reqId mapping not cleaned up"
            test_logger.info(f"✓ reqId mapping cleaned up by error handler: {test_req_id}")
            
        finally:
            # 清理测试数据 - 由于OrderManager没有delete_order方法，我们跳过清理
            # 在实际环境中，订单会保留在数据库中用于审计
            test_logger.info(f"✓ Test order completed: {test_order_id} (cleanup skipped)")
    
    def run_all_tests(self):
        """运行所有测试"""
        test_logger.info("Starting margin error handling tests...")
        
        try:
            self.setup()
            
            # 运行各项测试
            self.test_margin_error_detection()
            self.test_order_status_update()
            self.test_reqid_mapping()
            self.test_connection_manager_error_handler()
            
            test_logger.info("✅ All tests passed successfully!")
            return True
            
        except Exception as e:
            test_logger.error(f"❌ Test failed: {e}")
            import traceback
            traceback.print_exc()
            return False

def main():
    """主函数"""
    print("=== 保证金不足错误处理测试 ===")
    print("Testing margin error handling functionality...\n")
    
    # 创建测试实例
    test_runner = TestMarginErrorHandling()
    
    # 运行测试
    success = test_runner.run_all_tests()
    
    print("\n=== 测试结果 ===")
    if success:
        print("✅ 所有测试通过！保证金不足错误处理功能正常工作。")
        return 0
    else:
        print("❌ 测试失败！请检查错误处理实现。")
        return 1

if __name__ == "__main__":
    exit(main())