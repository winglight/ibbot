# 迁移指南：从本地管理器到 Redis/REST 通道

本指南帮助将旧版依赖本地内存管理器的部署迁移到基于 Redis + REST/Stream 的新架构。迁移过程中请先在测试环境完成演练，再逐步切换生产流量。

## 1. 前提检查

- 确保当前代码已更新至最新版本并通过 `pytest`。
- 准备可用的 Redis 实例（建议独立数据库或命名空间）。
- 确认能够部署以下后台服务：
  - **IB Command Worker**：连接 IB Gateway，消费 `queue:ib:commands` 并回写 `hash:ib:result:*`。
  - **Account Snapshot Service**：`python -m services.account_snapshot.main --mode worker`。
  - **Market Data Service**：`uvicorn services.market_data.app:create_app --host 0.0.0.0 --port 8010`。
- 若之前通过 `USE_LEGACY=1` 启动 `python main.py`，迁移后建议取消该变量（默认将以 Gunicorn 多进程模式启动）。

## 2. 部署后台服务

1. **Redis**：先行启动，并根据需要创建数据库或开启持久化。
2. **IB Command Worker**：
   - 读取环境变量 `IB_COMMAND_QUEUE_KEY`（默认 `queue:ib:commands`）、`IB_COMMAND_RESULT_PREFIX`（默认 `hash:ib:result:`）与 `IB_COMMAND_RESULT_STREAM_KEY`（默认 `stream:ib:results`）。
   - 定期写入健康信息至 `hash:service:health:ib-command`，字段建议包含 `status`、`last_success`、`queue_depth`。
3. **Account Snapshot Service**：
   - 建议以 systemd 或容器形式运行 `python -m services.account_snapshot.main --mode worker`。
   - 关键环境变量：`ACCOUNT_SNAPSHOT_COMMAND_QUEUE_KEY`、`ACCOUNT_SNAPSHOT_COMMAND_RESULT_PREFIX`、`ACCOUNT_SNAPSHOT_STREAM_KEY`、`ACCOUNT_SNAPSHOT_HEALTH_KEY`。
   - API 模式可通过 `python -m services.account_snapshot.main --mode api --port 8082` 暴露 `/health`、`/refresh/*`。
4. **Market Data Service**：
   - 依赖 Redis 的 `MARKET_DATA_STREAM_KEY`（默认 `stream:market-data`）与 `MARKET_DATA_SNAPSHOT_PREFIX`（默认 `hash:market:snapshot`）。
   - 通过环境变量 `MARKET_DATA_SERVICE_URL`、`MARKET_DATA_SERVICE_WS_URL` 配置对外访问地址。

建议按 “Redis → 命令 Worker → Snapshot Service → Market Data Service → UI Gateway” 的顺序启动；每个服务就绪后再启动下游组件。

## 3. 更新 UI Gateway 配置

在 `.env` 或部署环境中新增/确认以下变量：

| 变量 | 说明 |
| --- | --- |
| `REDIS_URL` / `REDIS_HOST` | 指向上述共享 Redis 实例 |
| `IB_COMMAND_QUEUE_KEY`、`IB_COMMAND_RESULT_PREFIX`、`IB_COMMAND_HEALTH_KEY` | 与命令 Worker 保持一致 |
| `ACCOUNT_SNAPSHOT_API_URL` | 指向账户快照 API（如 `http://account-snapshot-service:8082`），UI 会在命令失败时回退到该接口 |
| `ACCOUNT_HEALTH_STALE_SECONDS` | 快照健康超时时间，超时将视为离线 |
| `MARKET_DATA_SERVICE_URL`、`MARKET_DATA_SERVICE_WS_URL` | 指向市场数据 REST/WebSocket 服务 |
| `MARKET_DATA_STREAM_KEY`、`MARKET_DATA_SNAPSHOT_PREFIX` | 供回退逻辑读取 Redis 快照 |
| `SERVICE_HEALTH_PATTERN` *(可选)* | UI 聚合健康信息时的 Redis 匹配模式，默认 `hash:service:health:*` |

迁移后，前端/自动化任务应改用：

- `/api/account_info`、`/api/positions` 获取 Redis 缓存并在必要时自动刷新。
- `/api/place_order`、`/api/cancel_order` 通过命令队列执行交易，返回的 `service_health` 字段可用于监控。
- `/api/realtime_data/{symbol}`、`/ws/market` 访问市场数据服务，若 Redis 快照标记 `stale=true` 会自动回退到 REST 拉取。

## 4. 验证与回滚策略

1. 在测试环境执行 `pytest test/test_account.py test/test_orders_api.py test/test_connection.py test/test_market_data_fallback.py`，确认契约测试全部通过。
2. 启动所有后台服务后，访问 `/api/service_health` 确认 `account-snapshot`、`ib-command`、`market-data` 均显示 `status=ok`。
3. 进行一次端到端演练（登录 → 连接 IBKR → 刷新账户 → 下单 → 行情推送），观察 Redis 中的 Stream/Hash 是否按预期更新。
4. 如需回滚，可将 `USE_LEGACY=1`、`AUTO_CONNECT=true` 等旧变量恢复原状，并停止外部服务；UI 会回退到内存管理器逻辑（不再依赖 Redis）。

## 5. 常见问题

- **命令接口返回 503**：检查 `hash:service:health:ib-command` 是否存在，或确认命令 Worker 是否具备 Redis 访问权限。
- **账户接口长时间返回 `stale`**：查看 `stream:account-snapshot` 是否有新事件，如无则可能是 Snapshot Service 与命令 Worker 之间的队列堵塞。
- **行情接口 `source=history`**：说明 Redis 快照或 REST 服务不可用，应排查 `hash:market:snapshot:{symbol}` 是否过期以及 `/market/{symbol}` REST 是否可访问。

完成上述步骤后，即可在不依赖旧版本地管理器的情况下运行 UI Gateway，并享受统一的 Redis/REST 通道带来的弹性扩展能力。
