# Redis 缓存化统计设计方案

## 背景
策略面板、策略详情页以及账户概览最初直接从 SQLite 或 IBKR API 拉取数据并实时聚合。迁移到微服务架构后，读取路径已经改成通过订单/风控/策略服务的 HTTP 客户端，但缓存策略仍沿用“实时聚合”的旧假设：

- `StrategyManager.get_strategy_performance`、`get_strategy_order_stats` 每次调用都会遍历大量订单并在内存中重新计算总盈亏、胜率、最大回撤和 Sharpe。
- `/api/strategies/{name}/pnl/daily`、WebSocket `strategy_performance` 依赖同一条计算链路，命中频率高时容易阻塞。
- `/api/account_info`、`/api/positions` 在每次轮询时都回源至 IBKR 客户端，只做了极短暂的进程级缓存，导致响应慢且消耗连接数。
- `/api/account/daily-summary` 会遍历全部订单按日聚合，随着订单数量增长成本迅速增大。

这些接口的访问模式十分固定，完全可以通过在订单成交事件上做增量更新、在接口上读取缓存的方式来优化。

## Redis 接入约定

1. **连接配置**：在现有 `.env` 模板中新增 Redis 连接参数，默认值与开发环境一致。

   ```dotenv
   # Redis Configuration
   REDIS_HOST=127.0.0.1
   REDIS_PORT=6379
   REDIS_DB=0
   REDIS_PASSWORD=
   REDIS_USE_TLS=False
   ```

   在 `settings.py` 或集中化配置模块中读取上述变量，并暴露统一的 `get_redis_client()` 辅助函数，供 Strategy、Order、Account 管理器复用。

2. **命名空间规则**：以功能划分 key 前缀，便于后续清理与监控。
   - 策略统计：`strategy:{name}:stats`、`strategy:{name}:fills`、`strategy:{name}:daily`
   - 账户快照：`account:info`、`account:positions`
   - 账户日度盈亏：`account:daily_pnl`

3. **容错**：在 Redis 不可用时保持现有逻辑。所有写入都包裹在 try/except 中，失败时记录告警但不影响订单处理链路；读取端若发现缓存缺失或标记为 stale，则回落到原有数据库 / IB 查询路径。

## 策略统计缓存

### 写入触发点

- 订单服务订单状态事件：当状态进入 `Filled` 后立即更新基础统计字段（成交数、最近触发时间等），并把成交快照写入 capped list（例如仅保留最近 200 条）。
- `IBKRClient._update_order_commission_pnl`：佣金和盈亏就绪后，再补充盈利总额、平方和、胜负单统计、最大回撤状态及日度盈亏增量等高阶指标。

两个触发点通过 pipeline/Lua 保证原子性，避免并发写入造成统计错乱。

### Redis 数据结构

| Key | 类型 | 说明 |
| --- | --- | --- |
| `strategy:{name}:stats` | Hash | 聚合字段：`filled_count`、`win_count`、`loss_count`、`gross_pnl`、`gross_pnl_sq`、`max_drawdown`、`peak_equity`、`last_triggered`、`avg_duration_sec` 等 |
| `strategy:{name}:fills` | List | 最近 N 条成交（JSON 序列化），用于前端 Trades 列表及 WebSocket 推送 |
| `strategy:{name}:daily` | Hash 或 Sorted Set | key 为 `YYYY-MM-DD`，value 为当日盈亏、成交数，用于日历视图 |

### 回填与清理

- 新增 `StrategyManager.rebuild_strategy_cache()`：通过 `OrdersServiceClient.list_orders` 按时间顺序重放订单至缓存。应用启动时执行一次；在 `StrategyManager.reset_strategy_data`、策略删除/重命名时同时清理相关 Redis key。
- 若检测到缓存缺失（例如 Redis 重启），接口调用时可触发延迟回填（后台任务），保障用户看到的数据始终可用。

### 读取路径改造

1. `/api/strategies`、`/api/strategies/{name}`：优先读取 `strategy:{name}:stats`，仅在缓存缺失时回落到原有数据库统计。
2. `/api/strategies/{name}/performance`、`/pnl/daily`：分别从 `stats`、`daily` 读取组合响应；若需要细粒度指标（Sharpe、Sortino），利用缓存中的累计值直接计算。
3. WebSocket `strategy_performance`：订阅 Redis 快照，避免每次推送都重新查询数据库。

## 账户信息与持仓缓存

### 写入策略

- 在订单状态变为 `Filled` 后，通过后台任务调用 `ibkr_client.get_account_info()` 与 `get_positions()`，把返回值写入 Redis（Key：`account:info`、`account:positions`）。
- 缓存结构：
  - `account:info` 使用 Hash 存储核心字段（可选 JSON 序列化），附加 `updated_at` 时间戳与 `source` 标识。
  - `account:positions` 可使用 JSON 字符串或 Hash+Sorted Set（按合约标识划分），同样附带快照时间。
- 若长时间（如 10 分钟）无成交，可复用当前的 `AsyncIOScheduler` heartbeat 触发一次刷新，防止数据过旧。

### 读取改造

- `/api/account_info`、`/api/positions`：优先返回 Redis 中的快照，同时附带缓存年龄。如果缓存不存在或年龄超过阈值（例如 120 秒），接口层触发后台刷新并在当前请求中回落到 IBKR 实时查询。
- 连接断开或 IBKR 拉取失败时，可在缓存中设置 `stale=True` 字段，前端据此展示“数据可能过期”的提示。

## 账户日度盈亏缓存

### 写入逻辑

- 在佣金和盈亏确定的回调中，根据成交时间转换为美东交易日（`ZoneInfo("America/New_York")`），将当日的已实现盈亏累加到 `account:daily_pnl` hash 中。
- 同时维护 `account:net_liquidation`（Hash）存储最近一次的净值快照与时间戳，为 `/api/account/daily-summary` 提供实时资金信息。

### 回填与读取

- 新增回填函数（如 `AccountManager.rebuild_daily_pnl_cache`），遍历历史订单构建日度盈亏。启动时或检测缓存缺失时执行。
- `/api/account/daily-summary` 首先读取 Redis 数据并构造响应；若缓存缺失则回落到现有数据库扫描逻辑，并在计算完成后写回缓存。

## 异常处理与监控

- 在写入失败时使用模块化 logger（`strategy_logger`、`order_logger` 等）记录错误，并在必要时触发通知（例如 Telegram）。
- 建议为关键 key 设置 TTL 监控或指标上报（例如通过 Prometheus Exporter）以便及时发现缓存漂移。

## 后续工作清单

1. 实现 `src/cache.py`（或 `src/utils/cache.py`）提供统一的 Redis 客户端与 pipeline 辅助方法。
2. 在应用初始化阶段（如 `core/__init__.py`）加载 Redis 配置并注入到 StrategyManager、IBKRClient 等组件；订单事件由 `services/orders` 负责写入。
3. 按上述触发点更新订单处理流程，并确保单元测试覆盖 Filled 事件的缓存写入。
4. 为回填与清理逻辑编写 CLI/脚本或管理接口，方便在 Redis 数据损坏时重建。
5. 更新部署文档与 Docker Compose，确保 Redis 服务在默认环境中可用，并在 `.env.example` 中说明相关变量。

