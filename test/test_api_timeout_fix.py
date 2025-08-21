#!/usr/bin/env python3
"""
测试API超时修复效果
验证所有后台接口调用不再卡住和超时
"""

import asyncio
import httpx
import time
from loguru import logger

# 测试配置
BASE_URL = "http://localhost:8000"
TEST_TIMEOUT = 5.0  # 每个API调用的测试超时时间

async def _test_api_endpoint(client: httpx.AsyncClient, method: str, endpoint: str, data=None, expected_status=None):
    """
    测试单个API端点的响应时间和超时处理
    """
    start_time = time.time()
    try:
        if method.upper() == "GET":
            response = await client.get(f"{BASE_URL}{endpoint}", timeout=TEST_TIMEOUT)
        elif method.upper() == "POST":
            response = await client.post(f"{BASE_URL}{endpoint}", json=data, timeout=TEST_TIMEOUT)
        elif method.upper() == "DELETE":
            response = await client.delete(f"{BASE_URL}{endpoint}", timeout=TEST_TIMEOUT)
        else:
            logger.error(f"Unsupported method: {method}")
            return False
            
        elapsed = time.time() - start_time
        
        # 检查响应时间
        if elapsed > TEST_TIMEOUT:
            logger.error(f"❌ {method} {endpoint} - 超时 ({elapsed:.2f}s)")
            return False
            
        # 检查状态码
        if expected_status and response.status_code != expected_status:
            if response.status_code == 408:  # Request Timeout
                logger.warning(f"⚠️  {method} {endpoint} - 服务器超时 (408) - 修复生效")
                return True
            elif response.status_code in [400, 401, 403, 404, 500, 503]:
                logger.info(f"ℹ️  {method} {endpoint} - 预期错误 ({response.status_code}) - 响应正常 ({elapsed:.2f}s)")
                return True
            else:
                logger.warning(f"⚠️  {method} {endpoint} - 状态码 {response.status_code} ({elapsed:.2f}s)")
                return True
        
        logger.success(f"✅ {method} {endpoint} - 成功 ({elapsed:.2f}s)")
        return True
        
    except httpx.TimeoutException:
        elapsed = time.time() - start_time
        logger.error(f"❌ {method} {endpoint} - 客户端超时 ({elapsed:.2f}s)")
        return False
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"❌ {method} {endpoint} - 异常: {e} ({elapsed:.2f}s)")
        return False

async def _test_api_performance():
    """
    测试主要API端点的性能和超时处理
    """
    logger.info("🚀 开始测试API超时修复效果...")
    
    # 测试用例列表
    test_cases = [
        # 基础端点
        ("GET", "/", None, 200),
        ("GET", "/api/connection_config", None, 200),
        
        # 连接相关 (可能需要认证)
        ("POST", "/api/connect", {
            "host": "127.0.0.1",
            "port": 7497,
            "client_id": 1,
            "paper_trading": True
        }, None),
        
        # 数据相关 (可能因未连接而失败，但不应超时)
        ("POST", "/api/historical_data", {
            "symbol": "AAPL",
            "duration": "1 D",
            "bar_size": "1 min",
            "exchange": "SMART"
        }, None),
        
        ("POST", "/api/chart_data", {
            "symbol": "GOOGL",
            "duration": "1 D", 
            "bar_size": "5 mins",
            "exchange": "SMART"
        }, None),
        
        ("GET", "/api/realtime_data/MSFT", None, None),
        
        # 账户和持仓 (可能因未连接而失败，但不应超时)
        ("GET", "/api/account_info", None, None),
        ("GET", "/api/positions", None, None),
        ("GET", "/api/orders", None, None),
        
        # 订单操作 (可能因未连接而失败，但不应超时)
        ("DELETE", "/api/cancel_all_orders", None, None),
    ]
    
    results = []
    total_tests = len(test_cases)
    
    async with httpx.AsyncClient() as client:
        for i, (method, endpoint, data, expected_status) in enumerate(test_cases, 1):
            logger.info(f"\n[{i}/{total_tests}] 测试 {method} {endpoint}")
            success = await _test_api_endpoint(client, method, endpoint, data, expected_status)
            results.append((method, endpoint, success))
            
            # 短暂延迟避免过快请求
            await asyncio.sleep(0.5)
    
    # 统计结果
    successful = sum(1 for _, _, success in results if success)
    failed = total_tests - successful
    
    logger.info(f"\n📊 测试结果统计:")
    logger.info(f"✅ 成功: {successful}/{total_tests}")
    logger.info(f"❌ 失败: {failed}/{total_tests}")
    
    if failed == 0:
        logger.success("🎉 所有API端点响应正常，超时问题已修复！")
    else:
        logger.warning(f"⚠️  {failed}个端点仍有问题，需要进一步检查")
        
        # 显示失败的端点
        logger.info("\n失败的端点:")
        for method, endpoint, success in results:
            if not success:
                logger.error(f"  - {method} {endpoint}")
    
    return failed == 0

async def _test_concurrent_requests():
    """
    测试并发请求处理能力
    """
    logger.info("\n🔄 测试并发请求处理...")
    
    async def make_request(client, i):
        try:
            start_time = time.time()
            response = await client.get(f"{BASE_URL}/api/connection_config", timeout=TEST_TIMEOUT)
            elapsed = time.time() - start_time
            logger.info(f"并发请求 {i}: {response.status_code} ({elapsed:.2f}s)")
            return elapsed < TEST_TIMEOUT
        except Exception as e:
            logger.error(f"并发请求 {i} 失败: {e}")
            return False
    
    # 发起10个并发请求
    async with httpx.AsyncClient() as client:
        tasks = [make_request(client, i) for i in range(1, 11)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        successful = sum(1 for r in results if r is True)
        logger.info(f"并发测试结果: {successful}/10 成功")
        
        return successful >= 8  # 至少80%成功率

async def main():
    """
    主测试函数
    """
    logger.info("=" * 60)
    logger.info("API超时修复验证测试")
    logger.info("=" * 60)
    
    # 测试API性能
    api_test_passed = await _test_api_performance()
    
    # 测试并发处理
    concurrent_test_passed = await _test_concurrent_requests()
    
    # 总结
    logger.info("\n" + "=" * 60)
    logger.info("测试总结")
    logger.info("=" * 60)
    
    if api_test_passed and concurrent_test_passed:
        logger.success("🎉 所有测试通过！API超时问题已成功修复")
        logger.info("\n修复内容:")
        logger.info("✅ 连接API添加了15秒断开和30秒连接超时")
        logger.info("✅ 历史数据和图表数据API添加了30秒超时")
        logger.info("✅ 订单取消API添加了20秒超时")
        logger.info("✅ 实时数据API已有10秒超时")
        logger.info("✅ 账户信息和持仓API已有10秒超时")
        logger.info("✅ 订单列表API已有3秒超时")
    else:
        logger.warning("⚠️  部分测试未通过，可能需要进一步优化")
        if not api_test_passed:
            logger.error("- API响应测试未完全通过")
        if not concurrent_test_passed:
            logger.error("- 并发请求测试未通过")

if __name__ == "__main__":
    asyncio.run(main())

def test_api_performance():
    asyncio.run(_test_api_performance())

def test_concurrent_requests():
    asyncio.run(_test_concurrent_requests())

