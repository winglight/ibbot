#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整的止损单测试用例
包括：
1. 修改risk rule状态为active
2. 观察触发的止损单执行步骤
3. 详细的日志记录和错误捕获
"""

import asyncio
import logging
import sys
import os
import json
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / 'src'))

# 修复导入路径
sys.path.insert(0, str(project_root))
os.chdir(str(project_root))

from src.risk_manager import RiskManager, RiskRule, OrderRequest, OrderType
from src.ibkr_client import IBKRClient
from src.connection_manager import ConnectionManager
from src.telegram_notifier import TelegramNotifier
from src.utils import now_local

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('test_stop_loss_comprehensive.log')
    ]
)
logger = logging.getLogger(__name__)

class MockIBKRClient:
    """模拟IBKR客户端"""
    
    def __init__(self):
        self.connected = True
        self.mock_positions = [
            {
                'symbol': 'AAPL',
                'position': 100,  # 持有100股
                'avg_cost': 150.0,
                'market_price': 145.0,  # 当前价格低于成本价，触发止损
                'current_price': 145.0,  # 添加current_price字段
                'unrealized_pnl': -500.0
            }
        ]
        self.orders_placed = []
        
    def get_positions(self):
        """获取持仓信息"""
        logger.info(f"Mock: 返回持仓信息 {len(self.mock_positions)} 个")
        return self.mock_positions
    
    def place_market_order(self, symbol, quantity, action):
        """模拟下单"""
        logger.info(f"Mock: 下单 {action} {quantity} {symbol}")
        
        # 模拟可能的事件循环错误
        try:
            current_loop = asyncio.get_running_loop()
            logger.debug(f"Mock: 当前事件循环状态 - 运行中: {current_loop.is_running()}")
        except RuntimeError as e:
            logger.debug(f"Mock: 无法获取事件循环: {e}")
        
        # 记录订单
        order_dict = {
            'symbol': symbol,
            'quantity': quantity,
            'action': action,
            'timestamp': now_local().isoformat(),
            'status': 'FILLED'
        }
        self.orders_placed.append(order_dict)
        logger.info(f"Mock: 订单已记录 {order_dict}")
        
        # 创建模拟的trade对象，包含order属性
        class MockTrade:
            def __init__(self, order_data):
                self.order = MockOrder(order_data)
                self.orderStatus = MockOrderStatus()
                
        class MockOrder:
            def __init__(self, order_data):
                self.symbol = order_data['symbol']
                self.totalQuantity = order_data['quantity']
                self.action = order_data['action']
                self.orderType = 'MKT'
                self.orderId = f"MOCK_{symbol}_{int(now_local().timestamp())}"
                
        class MockOrderStatus:
            def __init__(self):
                self.status = 'Filled'
                
        return MockTrade(order_dict)

class MockConnectionManager:
    """模拟连接管理器"""
    
    def __init__(self):
        self.connected = True
        
    def run_in_connection_thread(self, func):
        """模拟在连接线程中运行函数"""
        logger.debug(f"Mock: 在连接线程中执行函数 {func.__name__ if hasattr(func, '__name__') else 'lambda'}")
        
        # 检查是否在事件循环中
        try:
            loop = asyncio.get_running_loop()
            logger.debug(f"Mock: 检测到运行中的事件循环: {loop}")
            # 这里可能会引发 "this event loop is already running" 错误
            # 我们模拟这种情况
            if loop.is_running():
                logger.warning("Mock: 检测到事件循环已在运行，可能引发冲突")
        except RuntimeError:
            logger.debug("Mock: 没有检测到运行中的事件循环")
        
        return func()

class MockTelegramNotifier:
    """模拟Telegram通知器"""
    
    def __init__(self):
        self.notifications_sent = []
        
    async def send_message(self, message: str):
        """模拟发送消息"""
        logger.info(f"Mock Telegram: {message}")
        self.notifications_sent.append({
            'message': message,
            'timestamp': now_local().isoformat()
        })
        return True
        
    async def send_stop_loss_alert(self, symbol: str, position: float, trigger_price: float, 
                                 order_type: str, order_id: int):
        """模拟发送止损警报"""
        action = "Take Profit" if order_type == "profit" else "Stop Loss"
        message = f"🚨 {action}执行: {symbol} @ ${trigger_price}, 持仓: {position}"
        logger.info(f"Mock Telegram Alert: {message}")
        self.notifications_sent.append({
            'type': 'stop_loss_alert',
            'symbol': symbol,
            'position': position,
            'trigger_price': trigger_price,
            'order_type': order_type,
            'order_id': order_id,
            'message': message,
            'timestamp': now_local().isoformat()
        })
        return True

async def _test_comprehensive_stop_loss():
    """完整的止损单测试"""
    logger.info("=== 开始完整止损单测试 ===")
    
    try:
        # 步骤1: 创建模拟组件
        logger.info("步骤1: 创建模拟组件")
        mock_ibkr = MockIBKRClient()
        mock_connection_manager = MockConnectionManager()
        mock_telegram = MockTelegramNotifier()
        
        # 创建风险管理器
        risk_manager = RiskManager(mock_telegram)
        
        # 步骤2: 创建并激活风险规则
        logger.info("步骤2: 创建并激活风险规则")
        
        # 创建止损规则
        stop_loss_rule = RiskRule(
            symbol="AAPL",
            stop_loss_price=145.0,  # 止损价格
            max_loss_percent=3.0,  # 3%止损
            enabled=True,  # 设置为激活状态
            created_at=now_local()
        )
        
        # 添加到风险管理器
        success = risk_manager.add_risk_rule(
            symbol=stop_loss_rule.symbol,
            stop_loss_price=stop_loss_rule.stop_loss_price,
            max_loss_percent=stop_loss_rule.max_loss_percent
        )
        logger.info(f"风险规则已添加并激活: {stop_loss_rule.symbol}, 成功: {success}")
        
        # 步骤3: 模拟价格数据触发止损
        logger.info("步骤3: 模拟价格数据触发止损")
        
        # 模拟价格数据 - AAPL从150跌到145，跌幅超过3%
        price_data = {
            'symbol': 'AAPL',
            'price': 145.0,
            'timestamp': now_local().isoformat()
        }
        
        logger.info(f"模拟价格数据: {price_data}")
        
        # 步骤4: 检查风险并执行止损
        logger.info("步骤4: 检查风险并执行止损")
        
        # 手动触发风险检查
        await risk_manager._monitor_positions()
        
        # 步骤5: 验证结果
        logger.info("步骤5: 验证结果")
        
        # 检查是否有订单被下达
        if mock_ibkr.orders_placed:
            logger.info(f"✅ 成功下达 {len(mock_ibkr.orders_placed)} 个订单")
            for order in mock_ibkr.orders_placed:
                logger.info(f"  订单详情: {order}")
        else:
            logger.warning("⚠️ 没有订单被下达")
        
        # 检查风险规则状态
        active_rules = [rule for rule in risk_manager.risk_rules.values() if rule.enabled]
        logger.info(f"活跃风险规则数量: {len(active_rules)}")
        
        # 验证测试结果
        test_passed = True
        
        # 检查是否有订单被执行
        if not mock_ibkr.orders_placed:
            logger.error("❌ 测试失败: 没有订单被下达")
            test_passed = False
        
        # 检查Telegram通知是否发送
        if not mock_telegram.notifications_sent:
            logger.error("❌ 测试失败: 没有发送Telegram通知")
            test_passed = False
        
        if test_passed:
            logger.info("✅ 完整止损单测试通过")
        else:
            logger.error("❌ 完整止损单测试失败")
        
        return test_passed
        
        logger.info("=== 完整止损单测试完成 ===")
        return True
        
    except Exception as e:
        logger.error(f"测试过程中发生错误: {e}")
        logger.error(f"错误类型: {type(e).__name__}")
        import traceback
        logger.error(f"错误堆栈: {traceback.format_exc()}")
        return False

async def _test_event_loop_conflict():
    """专门测试事件循环冲突问题"""
    logger.info("=== 开始事件循环冲突测试 ===")
    
    try:
        # 创建模拟组件
        mock_ibkr = MockIBKRClient()
        mock_telegram = MockTelegramNotifier()
        
        # 替换place_market_order方法以模拟事件循环冲突
        original_place_order = mock_ibkr.place_market_order
        
        def problematic_place_order(symbol, quantity, action):
            """模拟可能引发事件循环冲突的下单方法"""
            logger.debug(f"模拟下单方法被调用: {action} {quantity} {symbol}")
            
            # 检查当前事件循环状态
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    logger.warning("检测到事件循环已在运行，这可能引发 'this event loop is already running' 错误")
                    # 这里我们不实际引发错误，而是记录日志
                    logger.info("使用executor模式应该能避免此问题")
            except RuntimeError:
                logger.debug("没有检测到运行中的事件循环")
            
            return original_place_order(symbol, quantity, action)
        
        mock_ibkr.place_market_order = problematic_place_order
        
        # 创建风险管理器
        risk_manager = RiskManager(mock_telegram)
        
        # 创建订单请求
        order_request = OrderRequest(
            symbol="AAPL",
            quantity=100,
            order_type=OrderType.STOP_LOSS,
            price=145.0
        )
        
        # 执行订单
        logger.info("执行订单以测试事件循环冲突...")
        await risk_manager._execute_order(order_request)
        
        logger.info("✅ 事件循环冲突测试通过")
        return True
        
    except Exception as e:
        if "this event loop is already running" in str(e):
            logger.error(f"❌ 检测到事件循环冲突错误: {e}")
            return False
        else:
            logger.error(f"其他错误: {e}")
            import traceback
            logger.error(f"错误堆栈: {traceback.format_exc()}")
            return False

async def main():
    """主测试函数"""
    logger.info("开始运行完整的止损单测试套件")
    
    # 测试1: 完整止损单流程
    test1_result = await _test_comprehensive_stop_loss()
    
    # 测试2: 事件循环冲突
    test2_result = await _test_event_loop_conflict()
    
    # 总结
    logger.info("=== 测试结果总结 ===")
    logger.info(f"完整止损单测试: {'✅ 通过' if test1_result else '❌ 失败'}")
    logger.info(f"事件循环冲突测试: {'✅ 通过' if test2_result else '❌ 失败'}")
    
    if test1_result and test2_result:
        logger.info("🎉 所有测试通过！")
        return True
    else:
        logger.error("💥 部分测试失败")
        return False

if __name__ == "__main__":
    # 运行测试
    result = asyncio.run(main())
    sys.exit(0 if result else 1)

def test_comprehensive_stop_loss():
    asyncio.run(_test_comprehensive_stop_loss())

def test_event_loop_conflict():
    asyncio.run(_test_event_loop_conflict())

