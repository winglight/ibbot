# Redis & REST 通道编排说明

该文档补充 UI Gateway 与后台服务（命令执行、账户快照、行情聚合）之间的契约，帮助运维在多容器或分布式部署下快速排查问题。

## 启动顺序

1. **Redis** – 所有命令、快照与健康信息的统一缓存层，建议提前创建独立的数据库或前缀。
2. **IB Command Worker** – 负责执行 `queue:ib:commands` 中的 IBKR 指令，输出到 `hash:ib:result:*` 与可选的 `stream:ib:results`。UI 若检测到 `hash:service:health:ib-command` 为空会直接返回 503。
3. **Account Snapshot Service** – `python -m services.account_snapshot.main --mode worker`，将最新账户/持仓写入 `account:info`、`account:positions` 及 `stream:account-snapshot`，并维护 `hash:service:health:account-snapshot`。
4. **Market Data Service** – `uvicorn services.market_data.app:create_app --host 0.0.0.0 --port 8010`，消费 Redis Pub/Sub 或外部行情源，输出 `hash:market:snapshot:{symbol}` 和 `stream:market-data`。
5. **UI Gateway** – 本仓库的 FastAPI 应用，通过 `/api/service_health` 聚合 Redis 健康信息并向前端广播。

> 若以 Docker/Compose 部署，建议在 `depends_on` 中显式声明上述顺序，并在健康检查通过后再启动下游服务。

## Redis 键与 Stream 契约

| 键 / 前缀 | 说明 | 生产者 | 备注 |
| --- | --- | --- | --- |
| `queue:ib:commands` | 待执行的 IBKR 指令（JSON 字符串） | UI Gateway / Account Snapshot Service | Worker 读取后需写回结果或错误 |
| `hash:ib:result:{command_id}` | 命令结果哈希，字段包含 `status`、`data`、`error` | IB Command Worker | TTL 由 `ACCOUNT_SNAPSHOT_RESULT_TTL_SECONDS` 控制 |
| `stream:ib:results` | 命令完成事件流 | IB Command Worker | UI 可选消费以更新通知面板 |
| `account:info` / `account:positions` | 最新账户/持仓快照，字段 `data`、`updated_at`、`source`、`stale` | Account Snapshot Service | UI `/api/account_*` 会附带 `cache_age_seconds`、`stale_reason` |
| `stream:account-snapshot` | 快照增量与失效标记 | Account Snapshot Service | 字段 `type`（refresh/stale）、`payload`、`timestamp` |
| `hash:market:snapshot:{symbol}` | 行情快照，包含 `price`、`bid`、`ask`、`volume`、`stale` | Market Data Service | UI `/api/realtime_data` 与 WebSocket 初始快照读取该键 |
| `stream:market-data` | 统一行情事件流 | Market Data Service | 字段 `symbol`、`type`、`data`（JSON）、`ts_ns`、`latency_ms` |
| `hash:service:health:*` | 服务健康数据 | 各后台服务 | `/api/service_health` 会聚合并附带 `age_seconds` 计算 |

## 健康检查与监控

- **Account Snapshot Service**：`GET /health` 返回 `last_refresh`、`queue_depth`、`last_error`。超过 `ACCOUNT_HEALTH_STALE_SECONDS` 会被 UI 视为离线。
- **Market Data Service**：`GET /health` 暴露最近推送时间、Stream 写入失败次数及订阅延迟。
- **IB Command Worker**：定期写入 `hash:service:health:ib-command`（字段 `status`、`last_success`、`queue_depth` 等）。若缺失或 `status=error`，下单/刷新接口会返回 503。
- **UI Gateway**：`GET /api/service_health`（需要认证）聚合所有 `hash:service:health:*` 键，前端 WebSocket 初始广播中也包含 `service_health` 事件，可直接复用。

## 契约测试

单元与集成测试覆盖了 Redis 契约的关键场景：

- `test/test_orders_api.py`：模拟命令服务与 Redis 响应，验证 `/api/place_order`、`/api/cancel_order` 在命令超时和成功场景下的返回结构与 WebSocket 广播保持一致。
- `test/test_account.py`：构造 `account:info`、`account:positions` 快照及 `hash:service:health:account-snapshot`，验证快照失效与命令超时的回退逻辑。
- `test/test_market_data_fallback.py`：模拟 `hash:market:snapshot:{symbol}` 与 REST 服务响应，确认行情接口正确处理 `stale` 标记。
- `test/test_connection.py`：构建断线重连流程，确保 `/api/connect` 在超时与成功时都能输出统一的状态事件。

运行 `pytest` 即可在本地验证 Redis/REST 通道契约是否被破坏。
