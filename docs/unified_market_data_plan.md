# Unified Market Data Platform Design

_Last updated: 2024-07-30_

## Background
- DOM 当前由 `DepthSubscriber → DOMService → 策略/前端` 持续推送，具备缓存、指标与监听机制。
- K 线、分时行情使用 `IBKRClient.get_historical_data/get_real_time_data` 即时拉取，缺乏共享缓存与标准订阅接口。
- 告警、WebSocket、策略事件各自维护监听列表，难以复用，也不利于未来拆分为分布式服务。

## Goals
- 在现有 DOM 能力之上，构建统一的数据源管理层，抽象订阅、缓存、持久化与广播逻辑。
- 将实时行情、K 线等数据源纳入统一管理，并提供标准化的历史查询与实时推送服务。
- 设计轻量级 Pub/Sub 总线整合 DOM、行情、告警与策略事件，预留扩展为 ESB 的能力。
- 在不影响当前生产路径的前提下，实现可配置的双轨（legacy/unified）切换方案。

## Non-Goals
- 不在本阶段重构外部 UI/前端组件，只提供兼容接口。
- 不引入重量级消息系统（Kafka/NATS 等）作为首发版本，但保留未来替换空间。

## High-Level Architecture
```
IBKR (DOM/Tick/Historical)
        │
        ▼
+----------------------+         +--------------------------+
|  DataSourceManager   |◀───────▶| SubscriptionRegistry     |
|  (Facades)           |         | (dedupe lifecycle)       |
+----------------------+         +--------------------------+
        │
        ▼
+----------------------+         +--------------------------+
| Data Adapters        |         | IntervalTransformer      |
|  - IBDomSource       |         |  (tick→bar)              |
|  - IBQuoteSource     |         +--------------------------+
|  - IBHistoricalLoader|
+----------------------+
        │
        ▼
+----------------------+         +--------------------------+
| Cache Layer          |         | Persistence Layer        |
|  (Redis snapshots)   |         | (InfluxDB time-series)   |
+----------------------+         +--------------------------+
        │
        ▼
+-----------------------------------------------------------+
| EventBus (in-process, pluggable backend)                  |
+-----------------------------------------------------------+
        │
        ▼
Consumers: DOMService, StrategyManager, WebSocket, Alerts, Optimizer
```

## Module Responsibilities
- **DataSourceManager**: 对外统一入口。暴露 `subscribe`, `unsubscribe`, `snapshot`, `history`, `register_source`。负责参数校验、调度数据适配器、维护订阅上下文。
- **DataSourceAdapter**: 贴近 IBKR API 的薄适配层。实现统一 `start(params) -> SubscriptionHandle` 接口，接收订阅配置并将原始流规范化为标准事件。
- **SubscriptionRegistry**: 记录活跃订阅，处理同 symbol/interval 的复用、引用计数、自动重试与断线恢复。
- **IntervalTransformer**: 接收 tick/DOM，生成不同 interval 的 K 线或聚合指标，输出至缓存+持久化。
- **Cache Layer (Redis)**: 存储热点快照与短期窗口（如最近 N 根 K 线），提供快速 `snapshot` 服务，并支撑策略预热。
- **Persistence Layer (InfluxDB)**: 长期存储时序数据，支持历史查询、回放与回测；可选写入告警审计。
- **EventBus**: 统一发布订阅总线，支持 topic 通配、消费者隔离，默认基于 asyncio/队列实现，留出替换为 Redis Streams/NATS 的扩展点。
- **Compatibility Facade**: 供 `DOMService`、`StrategyManager`、`IBKRClient` 等现有模块调用，内部根据配置决定走旧逻辑或新数据层。

## Data Flow & Switching Strategy
1. **订阅流程**：消费者调用 `DataSourceManager.subscribe`，输入标准化参数（symbol、channel、interval、options）。管理层委派到对应适配器，接收数据后写入 Redis 快照 → 发布 EventBus → 异步写入 InfluxDB。
2. **历史查询**：先查 Redis 缓存（短期窗口），miss 时查询 InfluxDB，若 Influx 缺口则触发 `IBHistoricalLoader` 异步补数并回写缓存。
3. **切换机制**：配置项 `DATA_LAYER_MODE` (`legacy`/`unified`/`dual`) 控制逻辑。
   - `legacy`: 完全沿用 `IBKRClient` 现有实现。
   - `unified`: 路由到新数据层。
   - `dual`: 两套逻辑并行，结果对比并记录差异（用于观察期）。
4. **回退策略**：统一通过 Feature Flag（环境变量或数据库配置）实时切换；订阅/缓存模块需支持幂等初始化以避免切换时重复连接。

## Event Schema & Data Contracts
所有事件遵循统一 envelope：
```
{
  "event_type": "dom.metrics" | "market.tick" | ...,
  "topic": "dom.metrics" | "market.candle.1m" | ...,
  "symbol": "ES",
  "source": "ibkr",
  "ts_ns": 1722338123456789012,
  "sequence": 123456,                # 每 source/topic 内自增
  "correlation_id": "optional",
  "payload": { ... },
  "meta": {
    "interval": "1m",              # 仅对 candle/tick 适用
    "schema_version": 1,
    "flags": ["is_cached"],        # 可选
  }
}
```

具体 `payload` 约定：
- **DOM Depth (`dom.depth`)**: `levels`（买卖深度列表）、`imbalance`, `ts_exchange`, `latency_ms`。
- **DOM Metrics (`dom.metrics`)**: 指标名称 → 数值、`signal_long`, `signal_short`、`window_id`。
- **Market Tick (`market.tick`)**: `last`, `bid`, `ask`, `volume`, `trade_id`, `market_state`。
- **Market Candle (`market.candle.{interval}`)**: `open`, `high`, `low`, `close`, `volume`, `num_trades`, `interval_start`, `interval_end`。
- **Alerts (`alert.event`)**: `alert_id`, `severity`, `message`, `details`, `origin`。
- **Strategy Events (`strategy.event`)**: `strategy_id`, `status`, `message`, `orders` (列表)、`metrics`。

事件均需通过 `schema_version` 管理兼容性，并在文档中维护 JSON Schema（后续可放置于 `schemas/market_data`）。

## Storage & Caching Layout
- **Redis**
  - Key: `MarketSnapshot:{symbol}` → 最新 tick/行情；TTL 5s，定期刷新。
  - Key: `DOMSnapshot:{symbol}` → 最新 DOM/指标；TTL 2s。
  - Key: `Candles:{symbol}:{interval}` → 最近 N 根（默认 300 条）JSON 列表。
- **InfluxDB**
  - Measurement: `dom_depth`, tags=`symbol`,`exchange`,`source`; fields：价格深度、盘口量。
  - Measurement: `dom_metrics`, tags=`symbol`,`metric`, fields=`value`,`signal_long`,`signal_short`。
  - Measurement: `tick_quotes`, tags=`symbol`,`exchange`,`source`; fields=`last`,`bid`,`ask`,`volume`。
  - Measurement: `candles_{interval}`（或统一 measurement + interval tag）。

### 实际部署配置

统一数据层在运行时会根据环境变量自动切换后端：

- `DATA_LAYER_REDIS_URL`：Redis 连接串（例如 `redis://:REDIS_PASSWORD@redis:6379/0`）。未设置时回退为进程内缓存。
- `DATA_LAYER_REDIS_PREFIX`（可选）：键名前缀，默认 `market-data`。
- `DATA_LAYER_REDIS_SNAPSHOT_TTL`（可选）：快照 TTL 秒数，未设置则常驻。
- `DATA_LAYER_INFLUX_URL`：InfluxDB 访问地址（例如 `http://influxdb:8086`）。
- `DATA_LAYER_INFLUX_BUCKET`：写入 bucket 名称。
- `DATA_LAYER_INFLUX_ORG`：组织名称。
- `DATA_LAYER_INFLUX_TOKEN`（可选）：访问 token，如未配置则使用匿名方式。
- `DATA_LAYER_INFLUX_MEASUREMENT`（可选）：事件 measurement，默认 `market_events`。
- `DATA_LAYER_INFLUX_TIMEOUT_MS`（可选）：InfluxDB 客户端超时时间（毫秒），未设置则使用默认值。

配置步骤：
1. 在 `.env` 或部署环境中设置上述变量，Redis/Influx 任意一项缺失即保持内存/Noop 模式。
2. 确保容器或主机已部署对应服务，可参考 `docker/local.compose.yml` 中 `redis` 与 `influxdb` 的端口映射。
3. 重启应用后，日志会输出 `Using Redis cache backend...` 或 `Using InfluxDB persistence backend...`，确认接入成功。

## Compatibility Layer
- `IBKRClient.get_real_time_data/get_historical_data` 调整为根据 mode 调用：
  - Legacy 模式：沿用现有逻辑（请求 IBKR API）。
  - Unified 模式：调用 `DataSourceManager.snapshot/history`，必要时触发订阅或缓存补齐。
  - Dual 模式：同时调用新旧路径并比对（仅用于 QA），异常写入监控。
- `DOMService`：通过 `EventBus` 订阅 `dom.depth/dom.metrics`，暂留 `_listeners` 以兼容旧调用；在统一模式下 `_listeners` 迁移为对 EventBus 的订阅代理。
- `StrategyManager`：读取 `DataSourceManager` 提供的快照或通过 `EventBus` 接收实时事件；策略配置中新增 `data_layer_mode` 覆盖全局默认。
- `/api/historical_data` 与 `/api/current_price` API 优先读取统一数据层快照/历史，失败时再降级到 IBKR 实时/历史查询。

## Development Plan
### Phase 0 – Preparation & Baseline
- 定义数据契约与 envelope（本文件所述），创建 JSON Schema。
- 为现有 DOM 流、行情查询建立单元与集成基线测试，确保可回归。
- 搭建 InfluxDB / Redis 本地 docker compose 配置（可复用现有服务，建议直接复用 `docker/local.compose.yml` 中的定义）。

### Phase 1 – Core Infrastructure
- 实现 `EventBus`（in-process variant）及测试用假实现。
- 搭建 `DataSourceManager` 骨架、`SubscriptionRegistry`、事件 envelope。
- 将 `DepthSubscriber` 输出接入 EventBus，同时保持 DOMService 现状（双写）。

### Phase 2 – Unified Market Data Layer
- 实现 `IBQuoteSource`、`IBHistoricalLoader` 适配器，处理 tick、补数、异常恢复。
- 增加 `IntervalTransformer`，支持 1s/5s/1m/5m 可配置聚合；写入 Redis 缓存。
- 建立 Redis 快照与 InfluxDB 写入流水线，实现幂等与批量写机制。
- 扩展 `DataSourceManager.snapshot/history`，在统一模式下供 `IBKRClient` 调用。

### Phase 3 – Consumer Integration & Toggle
- 改造 `IBKRClient`、`StrategyManager`、`DOMService` 通过 Feature Flag 调用新层。
- 在配置模块添加 `DATA_LAYER_MODE`（含热更新能力，优先从 env，再 fallback DB）。
- `dual` 模式运行集成测试，记录差异到日志或 Prometheus 指标。
- 更新 REST/WebSocket 端点，支持从 Redis 快照读取数据并透传事件。

### Phase 4 – Event Ecosystem & Observability
- 将告警、策略事件接入 EventBus（发布/订阅），替换零散监听。
- 构建监控：订阅延迟、缓存命中率、Influx 写入失败率、重试次数。
- 编写回放工具（从 Influx/Redis 读取并重放到 EventBus）以支持调试。

### Phase 5 – Cleanup & Migration
- 在生产环境逐步启用 unified 模式，监控指标，确认稳定后淘汰 legacy path。
- 移除 `IBKRClient` 中旧的直接请求逻辑（或封存为 fallback），精简 DOMService listeners。
- 完成最终文档与运行手册更新。

## Testing Strategy
- 单元测试覆盖：EventBus（并发、退订）、SubscriptionRegistry（引用计数、断线恢复）、IntervalTransformer（聚合准确性）。
- 集成测试：基于 `ib_insync` stub 模拟 tick/DOM 流，验证 Redis 快照、Influx 写入、订阅去重。
- 回归测试：沿用现有 DOM pipeline tests，新增 `dual` 模式比较脚本。
- 性能测试：模拟高频 tick（>500 msg/s）与多 topic 订阅，评估延迟与资源占用。

## Monitoring & Observability
- 指标：`eventbus.publish_latency_ms`, `cache.hit_rate`, `subscription.reconnect_count`, `datastore.write_errors`。
- 日志：统一 `event_type` + `symbol` + `mode` 结构化日志，便于差异排查。
- 告警：当 unified 模式指标异常时，自动切换回 legacy 并发送 `alert.event`。

## Data Exchange Roadmap for Future ESB
- 事件 envelope 与 topic 命名与 ESB 对齐，未来仅需替换 EventBus 实现为 Redis Streams/NATS/Kafka。
- Schema 注册与版本管理预留接口，后续可接入 Schema Registry。
- 通过配置选择持久化后端（InfluxDB/pandas Parquet），满足不同环境部署需求。

## Open Questions
- InfluxDB 集群部署与权限管理是否有既有规范？
- Redis 的高可用方案是否需要与现有缓存共享？
- 是否有第三方行情源需要并行接入（如 CQG、Polygon）？

## Update Log
- 2024-07-30: 初始方案与分阶段计划。
- 2024-07-31: 新增 Redis/Influx 可选后端及配置说明。
