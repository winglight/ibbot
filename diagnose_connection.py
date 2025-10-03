#!/usr/bin/env python3
"""
IBKR连接诊断工具
用于检查和诊断IBKR与MariaDB的连接问题。
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from typing import Any, Set
from urllib.parse import urlparse, urlunparse

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_PATH = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from src.config import load_project_env  # noqa: E402
from src.db import DatabaseConfigError, get_database_url, run_sql_query  # noqa: E402

try:  # pragma: no cover - utility script, rarely exercised in tests
    from ib_insync import IB
except ImportError:  # pragma: no cover - allow diagnostics in stub mode
    from test.ib_insync_stub import IB

load_project_env()

REQUIRED_DB_TABLES: Set[str] = {
    "orders",
    "market_prices",
    "market_events",
    "service_metrics",
    "risk_config_snapshots",
    "risk_rule_snapshots",
}


def _redact_db_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:  # pragma: no cover - defensive parsing guard
        return url

    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        if ":" in userinfo:
            username = userinfo.split(":", 1)[0]
            userinfo = f"{username}:***"
        elif userinfo:
            userinfo = f"{userinfo}:***"
        netloc = f"{userinfo}@{host}" if userinfo else host

    return urlunparse(parsed._replace(netloc=netloc))


def _extract_scalar(rows: list[dict[str, Any]]) -> Any | None:
    if not rows:
        return None
    row = rows[0]
    if not row:
        return None
    return next(iter(row.values()), None)


async def _fetch_existing_tables() -> set[str]:
    queries: list[tuple[str, tuple[str, ...] | None]] = [
        ("SELECT name FROM sqlite_master WHERE type='table'", ("name", "NAME")),
        ("SHOW TABLES", None),
        (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE()",
            ("table_name", "TABLE_NAME"),
        ),
    ]

    last_error: Exception | None = None
    executed_successfully = False
    for statement, columns in queries:
        try:
            rows = await asyncio.to_thread(run_sql_query, statement)
        except Exception as exc:  # pragma: no cover - depends on environment
            last_error = exc
            continue

        table_names: set[str] = set()
        for row in rows:
            if not row:
                continue
            value: Any | None = None
            if columns is None:
                value = next(iter(row.values()), None)
            else:
                for key in columns:
                    value = row.get(key)
                    if value:
                        break
            if value:
                table_names.add(str(value))

        executed_successfully = True
        if table_names:
            return table_names

    if executed_successfully:
        return set()

    if last_error is not None:
        raise last_error
    return set()


def check_port_connectivity(host: str, port: int) -> bool:
    """检查端口连通性"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as exc:  # pragma: no cover - system specific
        logger.error(f"端口连通性检查失败: {exc}")
        return False


async def test_ibkr_connection(host: str, port: int, client_id: int) -> bool:
    """测试IBKR连接，自动尝试不同的客户端ID"""
    ib = IB()

    for attempt in range(6):
        try_client_id = client_id + attempt
        if try_client_id > 32:
            try_client_id -= 32

        try:
            print(f"   尝试客户端ID: {try_client_id}")
            await ib.connectAsync(host=host, port=port, clientId=try_client_id, timeout=8)
            logger.info(f"IBKR连接测试成功，使用客户端ID: {try_client_id}")
            ib.disconnect()
            return True
        except Exception as exc:
            error_msg = str(exc)
            if "already connected" in error_msg.lower() or "duplicate" in error_msg.lower():
                print(f"   客户端ID {try_client_id} 已被使用")
                continue
            if attempt == 5:
                logger.error(f"IBKR连接测试失败: {exc}")
                return False
            print(f"   客户端ID {try_client_id} 连接失败: {error_msg}")

    return False


async def test_mariadb_connection() -> bool:
    print("3. 检查MariaDB数据库...")
    try:
        db_url = get_database_url()
    except DatabaseConfigError as exc:
        print(f"   ✗ 数据库配置错误: {exc}")
        print("   解决方案:")
        print("     - 设置 IBBOT_DB_URL 或 DATA_LAYER_DB_URL 环境变量")
        print("     - 确保连接字符串使用 mysql、mariadb 或 mysql+pymysql 协议")
        return False

    print(f"   数据库URL: {_redact_db_url(db_url)}")

    try:
        rows = await asyncio.to_thread(run_sql_query, "SELECT 1 AS result")
        value = _extract_scalar(rows)
        print(f"   ✓ 连接成功 (SELECT 1 -> {value})")
    except DatabaseConfigError as exc:
        print(f"   ✗ 无法建立数据库连接: {exc}")
        print("   解决方案:")
        print("     - 安装依赖: pip install pymysql")
        print("     - 确认数据库URL配置正确并可访问")
        return False
    except Exception as exc:  # pragma: no cover - driver/environment specific
        print(f"   ✗ 数据库查询失败: {exc}")
        print("   解决方案:")
        print("     - 检查MariaDB服务是否正在运行")
        print("     - 确认网络与安全组允许数据库端口访问")
        print("     - 验证数据库用户权限")
        return False

    try:
        tables = await _fetch_existing_tables()
    except Exception as exc:  # pragma: no cover - depends on DB permissions
        print(f"   ✗ 无法获取表信息: {exc}")
        print("   解决方案:")
        print("     - 检查数据库用户是否具有 SHOW TABLES 权限")
        print("     - 确认数据库实例状态正常")
        return False

    missing = sorted(REQUIRED_DB_TABLES - tables)
    if missing:
        print("   ✗ 数据库缺少必要表:")
        for table in missing:
            print(f"     - {table}")
        print("   解决方案:")
        print("     - 运行 scripts/sql/20240522_init_mariadb.sql 初始化表结构")
        print("     - 或执行最新的数据库迁移脚本")
        return False

    print("   ✓ 所有必需表均已存在")

    return True


async def _run_async_checks(host: str, port: int, client_id: int) -> None:
    print("2. 测试IBKR API连接...")
    try:
        ib_result = await test_ibkr_connection(host, port, client_id)
    except Exception as exc:  # pragma: no cover - depends on external API
        print(f"   ✗ 连接测试异常: {exc}")
        ib_result = False

    if ib_result:
        print("   ✓ IBKR API连接成功")
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

    print()
    db_result = await test_mariadb_connection()
    print()

    if ib_result and db_result:
        print("诊断结果: IBKR 与 MariaDB 配置均正常")
    elif ib_result:
        print("诊断结果: IBKR 正常，但 MariaDB 存在问题")
    elif db_result:
        print("诊断结果: MariaDB 正常，但 IBKR 存在问题")
    else:
        print("诊断结果: IBKR 与 MariaDB 均需要进一步检查")


def main() -> None:
    print("=" * 60)
    print("IBKR连接诊断工具")
    print("=" * 60)

    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PORT", "4002"))
    client_id = int(os.getenv("IBKR_CLIENT_ID", "1"))
    paper_trading = os.getenv("IBKR_PAPER_TRADING", "True").lower() == "true"

    print("当前配置:")
    print(f"  主机: {host}")
    print(f"  端口: {port}")
    print(f"  客户端ID: {client_id}")
    print(f"  模拟交易: {paper_trading}")
    print()

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

    asyncio.run(_run_async_checks(host, port, client_id))

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
