# Market Data Service

该文档说明市场数据服务（`services.market_data`）的组件、依赖、接口以及运维要点，便于在多容器部署或分布式场景下复用。

## 概述

- **职责**：订阅 Redis Pub/Sub 行情通道、整合账户快照、向 Redis Stream 与 WebSocket 广播统一结构的市场事件。
- **架构**：`MarketDataRuntime` 管理配置、Redis 连接、指标/健康任务；`PubSubListener` 负责监听频道并维护内存快照；`StreamPublisher` 负责写入 Redis Stream；`MarketDataWebSocketManager` 提供实时推送与心跳管理。
- **部署方式**：可通过 `uvicorn services.market_data.app:create_app --host 0.0.0.0 --port 8010` 启动，Docker Compose 示例见 `docker/docker-compose.yml` 中的 `market-data-service` 服务。

## 运行时依赖

| 组件 | 作用 | 备注 |
| --- | --- | --- |
| Redis | 读取市场数据配置（`config:market-data`）、订阅 `pubsub:market-data:*`、写入 Stream/健康状态 | 必须；通过 `REDIS_URL` 或 `REDIS_HOST/PORT/DB` 指定 |
| InfluxDB | 写入 `service_metrics` 或自定义 measurement 的运行指标 | 可选；未配置时自动跳过写入 |
| IB Account Snapshot Service | 提供账户/持仓增量快照并合并到行情数据中 | 可选；配置 `MARKET_DATA_ACCOUNT_SNAPSHOT_REQUIRED=true` 时变为必需 |

## 环境变量

所有变量都已有默认值，可在 `.env` 或容器环境中覆盖。

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `MARKET_DATA_SERVICE_URL` | 主程序访问 REST API 时使用的基础地址 | `http://localhost:8010` |
| `MARKET_DATA_SERVICE_WS_URL` | 主程序/前端使用的 WebSocket 地址 | `ws://localhost:8010/ws/market` |
| `MARKET_DATA_SYMBOLS` | 默认订阅的标的列表（逗号分隔） | `BTCUSDT` |
| `MARKET_DATA_STREAM_KEY` | Redis Stream 名称 | `stream:market-data` |
| `MARKET_DATA_STREAM_LENGTH` | Stream 最大长度（`XADD MAXLEN`） | `1000` |
| `MARKET_DATA_SERVICE_NAME` | 服务标识，用于健康/指标命名 | `market-data` |
| `MARKET_DATA_METRICS_PERIOD` | 指标汇总周期（秒） | `60.0` |
| `MARKET_DATA_ACCOUNT_SNAPSHOT_REFRESH_INTERVAL` | 合并账户快照的主动刷新间隔（秒，`0` 表示禁用定时器） | `5.0` |
| `MARKET_DATA_ACCOUNT_SNAPSHOT_RETRY_INTERVAL` | 合并账户快照失败后的重试间隔（秒） | `30.0` |
| `MARKET_DATA_ACCOUNT_SNAPSHOT_REQUIRED` | 是否在账户快照不可用时将服务标记为失败 | `False` |
| `MARKET_DATA_SERVICE_VERSION` | 自定义版本号写入健康信息 | `unknown` |
| `MARKET_DATA_METRICS_INFLUX_URL` | 写入指标的 Influx 地址 | 未设置 |
| `MARKET_DATA_METRICS_INFLUX_BUCKET` | 指标写入的 bucket 名称 | 未设置 |
| `MARKET_DATA_METRICS_INFLUX_ORG` | Influx 组织 | 未设置 |
| `MARKET_DATA_METRICS_INFLUX_TOKEN` | Influx token | 未设置 |
| `MARKET_DATA_METRICS_MEASUREMENT` | 指标 measurement 名称 | `service_metrics` |
| `MARKET_DATA_METRICS_INFLUX_TIMEOUT_MS` | 写入 Influx 的超时时间（毫秒） | 未设置 |

> 除上述变量外，服务也会复用通用的 `REDIS_URL` / `REDIS_HOST` 等设置。

## REST API

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/market` | GET | 返回所有订阅标的的最新快照；可通过查询参数 `stale=true/false` 过滤失效标志 |
| `/market/{symbol}` | GET | 返回单个标的的快照，找不到时返回 `404` |
| `/api/realtime_data/{symbol}` | GET | 与 `/market/{symbol}` 等价的兼容接口 |
| `/health` | GET | 返回运行状态：Redis/Influx 写入错误、账户快照最新时间、订阅延迟等 |

响应中的快照字段示例：

```json
{
  "symbol": "MNQ",
  "type": "dom.metrics",
  "data": {"bid": 17985.75, "ask": 17986.0},
  "latency_ms": 12.4,
  "account_info": {...},
  "positions": [...],
  "stale": false
}
```

## WebSocket

- 端点：`/ws/market`。
- 查询参数：
  - `symbols`（可选）：逗号分隔的标的过滤，例如 `symbols=MNQ,ES`。
  - `types` 或 `event_types`（可选）：按事件类型过滤，例如 `types=dom.metrics,market.tick`。
- 连接建立后服务会立即发送 `{"type": "snapshot", "data": [...]}`，随后推送实时事件 `{"symbol": "MNQ", "type": "dom.metrics", "data": {...}}`。
- 心跳：服务每 `ping_interval` 秒发送 `{"type": "ping", "ts": ...}`，客户端需回复 `{"type": "pong"}`。可通过发送消息 `{ "type": "subscribe", "symbols": ["CL"] }` 实时调整过滤条件。

## Redis Stream

- 事件写入 `MARKET_DATA_STREAM_KEY`，使用 `XADD MAXLEN` 控制长度。
- 条目字段：
  - `symbol`：标的代码。
  - `type`：事件类型（如 `dom.metrics`、`market.tick`）。
  - `data`：JSON 字符串化的事件数据。
  - `ts_ns`：纳秒时间戳。
  - `latency_ms`：源事件延迟（字符串，可能为空）。
- Stream 写入失败会触发指数退避重试，并在 `/health` 输出 `last_stream_error` 与 `write_failures` 计数。

## 指标与健康

- 当配置 Influx 参数后，`MarketDataRuntime` 会将 `StreamPublisher.collect_metrics()` 的结果写入 `service.<MARKET_DATA_SERVICE_NAME>` topic，包含事件数、平均/最大延迟、最后写入时间等字段。
- `/health` 返回 `heartbeat_ts`、`last_success`、`write_failures`、订阅失败统计、以及账户快照合并状态。
- 可以通过 `MARKET_DATA_METRICS_PERIOD` 调整指标写入频率；若未配置 Influx，相关任务会自动停用。

## 日志

- 默认使用 Loguru 输出到 stdout；Docker Compose 示例将日志目录挂载到 `logs/`，建议在容器中使用 `LOGURU_LEVEL` 控制级别。
- 推荐将 `uvicorn` 命令追加 `--access-log` 控制台输出，必要时可在主程序日志面板中引用 `logs/market-data-service.log`。

## 与主程序集成

- 在主应用容器中设置 `MARKET_DATA_SERVICE_URL=http://market-data-service:8010` 与 `MARKET_DATA_SERVICE_WS_URL=ws://market-data-service:8010/ws/market`，即可让现有 API/前端面板通过内部网络访问该服务。
- 主程序的队列/监控面板可读取 `MARKET_DATA_STREAM_KEY` Stream 展示实时写入速率；Influx 中的 `service_metrics` measurement 则用于绘制运行指标。

## 运维与故障排查

1. **健康检查**：`curl http://market-data-service:8010/health`，确认 `last_error` 为空、`heartbeat_ts` 更新。
2. **指标查询**：若启用 Influx，可查询 `from(bucket: "<bucket>") |> range(start: -15m) |> filter(fn: (r) => r._measurement == "service_metrics" and r.symbol == "market-data-service")`。
3. **日志位置**：查看容器 stdout 或挂载目录 `logs/market-data-service.log`，关注 `Stream publish attempt failed`、`PubSub listener error` 等关键字。
4. **Redis Stream 滞留**：通过 `XINFO STREAM <key>` 检查长度与消费情况，必要时调高 `MARKET_DATA_STREAM_LENGTH` 或优化消费者处理逻辑。
