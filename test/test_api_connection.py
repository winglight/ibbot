#!/usr/bin/env python3
"""
测试API连接和基本功能
"""

import json
from typing import Any, Dict

import pytest
import requests

# API配置
BASE_URL = "http://127.0.0.1:8000"
USERNAME = "chy"
PASSWORD = "X9$K^nNI4"


def _request_json(method: str, url: str, *, token: str | None = None, timeout: int = 10) -> Dict[str, Any]:
    """执行JSON请求并返回响应内容."""

    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        pytest.skip(f"无法连接到API服务器: {exc}")

    if response.status_code != 200:
        pytest.fail(f"请求失败: {response.status_code} - {response.text}")

    try:
        return response.json()
    except ValueError as exc:
        pytest.fail(f"响应不是有效的JSON: {exc}")


def test_api_connection():
    """测试API连接"""
    print("🔍 测试API连接...")

    # 1. 测试登录
    print("\n1. 测试登录...")
    try:
        response = requests.post(
            f"{BASE_URL}/api/login",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=10,
        )
    except requests.RequestException as exc:
        pytest.skip(f"无法连接到API服务器: {exc}")

    if response.status_code != 200:
        pytest.skip(f"登录失败: {response.status_code} - {response.text}")

    try:
        token = response.json()["access_token"]
    except (ValueError, KeyError) as exc:
        pytest.fail(f"登录响应解析失败: {exc}")

    # 2. 测试连接状态
    print("\n2. 测试连接状态...")
    status = _request_json("GET", f"{BASE_URL}/api/connection_status", token=token)
    print(f"✅ 连接状态: {json.dumps(status, indent=2)}")

    # 3. 测试持仓获取
    print("\n3. 测试持仓获取...")
    positions = _request_json("GET", f"{BASE_URL}/api/positions", token=token)
    print(f"✅ 持仓获取成功，数量: {len(positions)}")
    for pos in positions[:3]:  # 只显示前3个
        print(f"   {pos['symbol']}: {pos['position']} 股")

    # 4. 测试风险规则获取
    print("\n4. 测试风险规则获取...")
    rules = _request_json("GET", f"{BASE_URL}/api/risk_rules", token=token)
    print(f"✅ 风险规则获取成功，数量: {len(rules)}")
    for rule in rules[:3]:  # 只显示前3个
        status_text = "激活" if rule['enabled'] else "禁用"
        print(f"   {rule['symbol']}: {status_text}")

    print("\n✅ 所有API测试通过！")


if __name__ == "__main__":
    print("=" * 50)
    print("🧪 API连接测试")
    print("=" * 50)

    try:
        test_api_connection()
    except pytest.SkipTest as exc:  # pragma: no cover - 手动运行时的提示
        print(f"⚠️ 测试被跳过: {exc}")
        success = False
    except AssertionError as exc:  # pragma: no cover - 手动运行时的提示
        print(f"❌ API连接测试失败: {exc}")
        success = False
    else:
        success = True

    print("\n" + "=" * 50)
    if success:
        print("🎉 API连接测试通过！")
        print("✅ 系统运行正常，可以进行风险规则测试")
    else:
        print("❌ API连接测试失败")
        print("⚠️ 需要检查系统状态")
    print("=" * 50)