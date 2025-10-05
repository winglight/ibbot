# 数据访问层迁移指南（`src.ibbot.db` → `src.db`）

自 2024 年 6 月起，数据库仓储与配置接口统一收敛到 `src.db` 命名空间。旧的
`src.ibbot.db` 包已被移除，并在导入时立即抛出 `ImportError` 引导开发者迁移。

## 关键变化概览

- **统一入口**：仓储工厂、写入器以及同步/异步辅助函数均由 `src.db` 顶层模块
  暴露，例如 `from src.db import order_repository, run_in_thread`。
- **配置解析**：数据库连接解析逻辑迁移到 `src.db.config`。原先的
  `src.ibbot.db.config.resolve_database_url` / `get_database_url` 如今分别位于
  `src.db.config.resolve_database_url` 与 `src.db.config.get_database_url`。
- **兼容层清理**：`src/ibbot/db/__init__.py` 现在只负责提示迁移，任何对该模块的
  直接导入都会抛出异常。
- **辅助常量**：需要检查 MariaDB 表是否创建完成的脚本可从
  `src.db.schema.REQUIRED_TABLES` 获取当前要求的表名集合。

## 代码迁移示例

```diff
- from src.ibbot.db import order_repository
+ from src.db import order_repository

- from src.ibbot.db.config import resolve_database_url
+ from src.db.config import resolve_database_url

- from ibbot.db.schemas import metadata
+ from src.db.schema import REQUIRED_TABLES
```

更新完成后，可执行以下自检脚本验证环境配置：

```bash
python scripts/diagnose_foundation.py --dry-run
python diagnose_connection.py
```

若脚本提示缺少表结构，可启动 `services/orders`、`services/strategy` 或数据层服
务触发自动建表流程（内部使用 `scripts/sql/db.sql`）。也可以运行 `python -m
src.db.bootstrap` 手动兜底。所有历史引用 `src.ibbot.db` 的模块都应替换为上述新接
口，以避免在运行时遇到 `ImportError`。

## 从 SQLite 迁移历史数据

- 如需手动初始化，可执行 `python -m src.db.bootstrap`（底层读取 `scripts/sql/db.sql`）。
- 运行 `python scripts/sqlite_to_mariadb.py`（可选 `--sqlite` / `--tables` 参数）
  将旧版 `data/trading_bot.db` 中的订单、风控、配置等表迁移到 MariaDB。脚本会
  自动跳过 DOM 与 K 线相关的数据表。
