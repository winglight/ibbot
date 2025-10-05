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
from typing import Set
from urllib.parse import urlparse, urlunparse

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_project_env  # noqa: E402
from src.db import (  # noqa: E402
    DatabaseConfigError,
    MariaDBConfigurationError,
    connect,
    get_database_url,
    run_sql_query,
)
from src.db.schema import REQUIRED_TABLES  # noqa: E402

try:  # pragma: no cover - utility script, rarely exercised in tests
    from ib_insync import IB
except ImportError:  # pragma: no cover - allow diagnostics in stub mode
    from test.ib_insync_stub import IB

load_project_env()

REQUIRED_DB_TABLES: Set[str] = set(REQUIRED_TABLES)


def _redact_db_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
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


def _detect_backend(url: str) -> str:
    scheme = urlparse(url).scheme.lower()
    if scheme.startswith("sqlite"):
        return "sqlite"
    return "mysql"


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
    except (DatabaseConfigError, MariaDBConfigurationError) as exc:
        print(f"   ✗ 数据库配置错误: {exc}")
        print("   解决方案:")
        print("     - 设置 IBBOT_DB_URL 或 DATA_LAYER_DB_URL 环境变量")
        print("     - 确保连接字符串使用 mysql+asyncmy 驱动")
        return False

    print(f"   数据库URL: {_redact_db_url(db_url)}")
    backend = _detect_backend(db_url)

    try:
        with connect():
            pass
    except MariaDBConfigurationError as exc:
        print(f"   ✗ 无法建立数据库连接: {exc}")
        print("   解决方案:")
        if "pymysql" in str(exc).lower():
            print("     - 安装依赖: pip install pymysql")
        else:
            print("     - 检查数据库URL与凭据配置")
        return False
    except Exception as exc:  # pragma: no cover - depends on driver environment
        print(f"   ✗ 数据库连接异常: {exc}")
        print("   解决方案:")
        print("     - 检查数据库主机与端口配置")
        print("     - 确认凭据是否正确")
        print("     - 查看数据库日志获取更多信息")
        return False

    try:
        result_rows = run_sql_query("SELECT 1 AS result")
        value = result_rows[0]["result"] if result_rows else None
        print(f"   ✓ 连接成功 (SELECT 1 -> {value})")
    except Exception as exc:
        print(f"   ✗ 数据库查询失败: {exc}")
        print("   解决方案:")
        print("     - 检查MariaDB服务是否正在运行")
        print("     - 确认网络与安全组允许数据库端口访问")
        print("     - 验证数据库用户权限")
        return False

    version_query = "SELECT VERSION() AS version"
    if backend == "sqlite":
        version_query = "SELECT sqlite_version() AS version"

    try:
        version_rows = run_sql_query(version_query)
        if version_rows:
            version = version_rows[0].get("version") or "<未知>"
            print(f"   数据库版本: {version}")
    except Exception:  # pragma: no cover - optional metadata
        pass

    if backend == "sqlite":
        table_query = "SELECT name AS table_name FROM sqlite_master WHERE type='table'"
    else:
        table_query = (
            "SELECT table_name AS table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        )

    try:
        table_rows = run_sql_query(table_query)
    except Exception as exc:
        print(f"   ✗ 无法获取数据库表列表: {exc}")
        print("   解决方案:")
        print("     - 检查数据库账户是否具备元数据查询权限")
        return False

    tables = {row.get("table_name") for row in table_rows if row.get("table_name")}
    missing = sorted(REQUIRED_DB_TABLES - tables)
    if missing:
        print("   ! 数据库缺少必要表:")
        for table in missing:
            print(f"     - {table}")
        print("   -> 尝试自动创建缺失的表 (scripts/sql/db.sql)")
        try:
            from src.db.bootstrap import SchemaBootstrapError, ensure_schema  # noqa: WPS433
        except ImportError as exc:
            print(f"   ✗ 自动建表模块不可用: {exc}")
        else:
            try:
                result = ensure_schema(logger=logger)
            except SchemaBootstrapError as exc:
                print(f"   ✗ 自动建表失败: {exc}")
            except Exception as exc:  # pragma: no cover - environment specific
                print(f"   ✗ 自动建表失败: {exc}")
            else:
                created_tables = list(result.created_tables)
                if created_tables:
                    print(f"   ✓ 已创建数据表: {', '.join(created_tables)}")
                else:
                    print("   ✓ 自动建表程序确认所有必需表均已存在")
                table_rows = run_sql_query(table_query)
                tables = {row.get("table_name") for row in table_rows if row.get("table_name")}
                missing = sorted(REQUIRED_DB_TABLES - tables)

        if missing:
            print("   ✗ 数据库仍缺少必要表:")
            for table in missing:
                print(f"     - {table}")
            print("   解决方案:")
            print("     - 启动 orders/strategy/data-layer 服务以触发自动建表")
            print(
                "     - 或运行 'python -m src.db.bootstrap' （使用 scripts/sql/db.sql 兜底初始化）"
            )
            return False

        print("   ✓ 所有必需表均已准备就绪")
        return True

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
