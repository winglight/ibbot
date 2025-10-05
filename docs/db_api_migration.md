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

若脚本提示缺少表结构，请参考 `scripts/sql/20240522_init_mariadb.sql` 初始化数据
库。该脚本同步了订单表的最新字段（自动递增 `id` 主键、命令/客户端/IB ID、
生命周期时间戳、成交价格与 JSON 原始载荷等），并创建了仓储依赖的唯一索引
与辅助索引。所有历史引用 `src.ibbot.db` 的模块都应替换为上述新接口，以避免在
运行时遇到 `ImportError`。

## 从 SQLite 迁移历史数据

- 先执行 `scripts/sql/20240522_init_mariadb.sql` 创建新的 MariaDB 表结构。
- 运行 `python scripts/sqlite_to_mariadb.py`（可选 `--sqlite` / `--tables` 参数）
  将旧版 `data/trading_bot.db` 中的订单、风控、配置等表迁移到 MariaDB。脚本会
  自动跳过 DOM 与 K 线相关的数据表。

## 生产环境订单表迁移指引

老版本的 `orders` 表通常以 `order_id` 作为主键，字段也缺少仓储目前写入的生命周期
数据。升级到新结构前请先备份数据库，然后按照以下步骤操作：

1. **添加自增主键**

   ```sql
   ALTER TABLE orders DROP PRIMARY KEY;
   ALTER TABLE orders
       ADD COLUMN id INT UNSIGNED NOT NULL AUTO_INCREMENT FIRST,
       MODIFY COLUMN order_id VARCHAR(191) NULL,
       ADD PRIMARY KEY (id);
   ```

2. **补齐字段与时间戳默认值**（已有字段按需 `MODIFY`，缺失字段使用 `ADD COLUMN`）：

   ```sql
   ALTER TABLE orders
       ADD COLUMN command_id VARCHAR(191) NULL AFTER id,
       ADD COLUMN client_order_id VARCHAR(191) NULL AFTER order_id,
       ADD COLUMN ib_order_id VARCHAR(191) NULL AFTER client_order_id,
       ADD COLUMN side VARCHAR(32) NULL AFTER action,
       ADD COLUMN limit_price DOUBLE NULL AFTER price,
       ADD COLUMN stop_price DOUBLE NULL AFTER limit_price,
       ADD COLUMN executed_at DATETIME NULL AFTER updated_at,
       ADD COLUMN order_timestamp DATETIME NULL AFTER executed_at,
       ADD COLUMN fill_price DOUBLE NULL AFTER order_timestamp,
       ADD COLUMN filled_quantity DOUBLE NULL AFTER fill_price,
       ADD COLUMN remaining_quantity DOUBLE NULL AFTER filled_quantity,
       ADD COLUMN avg_fill_price DOUBLE NULL AFTER remaining_quantity,
       ADD COLUMN order_source VARCHAR(191) NULL AFTER avg_fill_price,
       ADD COLUMN strategy VARCHAR(191) NULL AFTER order_source,
       ADD COLUMN strategy_name VARCHAR(191) NULL AFTER strategy,
       ADD COLUMN rule_id VARCHAR(191) NULL AFTER strategy_name,
       ADD COLUMN parent_order_id VARCHAR(191) NULL AFTER rule_id,
       ADD COLUMN commission DOUBLE NULL AFTER notes,
       ADD COLUMN pnl DOUBLE NULL AFTER commission,
       ADD COLUMN realized_pnl DOUBLE NULL AFTER pnl,
       ADD COLUMN unrealized_pnl DOUBLE NULL AFTER realized_pnl,
       ADD COLUMN trigger VARCHAR(191) NULL AFTER unrealized_pnl,
       ADD COLUMN trigger_type VARCHAR(191) NULL AFTER trigger,
       ADD COLUMN trigger_status VARCHAR(191) NULL AFTER trigger_type,
       ADD COLUMN trigger_price DOUBLE NULL AFTER trigger_status,
       ADD COLUMN account VARCHAR(191) NULL AFTER trigger_price,
       ADD COLUMN source VARCHAR(191) NULL AFTER account,
       ADD COLUMN raw_payload JSON NULL AFTER source,
       MODIFY COLUMN status VARCHAR(64) NOT NULL DEFAULT 'Submitted',
       MODIFY COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
       MODIFY COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
           ON UPDATE CURRENT_TIMESTAMP,
       MODIFY COLUMN exchange VARCHAR(64) NOT NULL DEFAULT 'SMART',
       MODIFY COLUMN sec_type VARCHAR(64) NOT NULL DEFAULT 'STK',
       MODIFY COLUMN is_deleted TINYINT(1) NOT NULL DEFAULT 0;
   ```

3. **补充索引**（确保命令/客户端/IB ID 唯一性以及常用查询）

   ```sql
   ALTER TABLE orders
       ADD UNIQUE KEY ux_orders_command_id (command_id),
       ADD UNIQUE KEY ux_orders_client_order_id (client_order_id),
       ADD UNIQUE KEY ux_orders_ib_order_id (ib_order_id),
       ADD KEY ix_orders_symbol (symbol),
       ADD KEY ix_orders_status_created (status, created_at),
       ADD KEY ix_orders_strategy (strategy),
       ADD KEY ix_orders_strategy_name (strategy_name),
       ADD KEY ix_orders_parent (parent_order_id),
       ADD KEY ix_orders_created (created_at);
   ```

若订单量较少，也可创建临时表使用 `scripts/sql/20240522_init_mariadb.sql` 的结构重建：

```sql
CREATE TABLE orders_new LIKE orders;
-- 调整新表结构或直接使用脚本创建
INSERT INTO orders_new SELECT * FROM orders;
RENAME TABLE orders TO orders_legacy, orders_new TO orders;
```

迁移完成后，运行 `python scripts/diagnose_foundation.py` 与相关单元测试确认仓储读写
正常。
