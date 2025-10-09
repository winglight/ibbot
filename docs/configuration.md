# 配置与环境变量总览

本文档汇总 refactor 后各服务需要的核心环境变量与 Redis 通道约定，帮助在部署/排障时快速定位配置入口。所有变量都支持通过 `.env`
与 `.env.local` 叠加加载：`src.config.load_project_env` 会优先读取 `.env.local`，再读取 `.env`，最后回落至仓库内置默认值。

## 服务专属 IB 连接配置

每个微服务都有独立的 IB 网关连接，需要在对应环境变量中声明主机、端口与客户端 ID。未显式配置时会退回 `BaseServiceIBClient`
中的默认值（`127.0.0.1:7497`，客户端 ID `0`）。建议在生产环境为每个服务保留唯一的客户端 ID，避免请求冲突。

| 服务 | 主机变量 | 端口变量 | 客户端 ID 变量 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Account | `ACCOUNT_IB_HOST` | `ACCOUNT_IB_PORT` | `ACCOUNT_IB_CLIENT_ID` | `127.0.0.1` / `7497` / `0` | 负责账户摘要、持仓、PnL 订阅与 Redis 分发。【F:services/account/ib_client.py†L20-L59】 |
| Orders | `ORDERS_IB_HOST` | `ORDERS_IB_PORT` | `ORDERS_IB_CLIENT_ID` | `127.0.0.1` / `7497` / `0` | 负责委托下单、撤单、成交与状态回调。【F:services/orders/ib_client.py†L16-L148】 |
| Market Data | `MARKET_DATA_IB_HOST` | `MARKET_DATA_IB_PORT` | `MARKET_DATA_IB_CLIENT_ID` | `127.0.0.1` / `7497` / `0` | 负责行情、盘口、逐笔及历史数据订阅。【F:services/market_data/ib_client.py†L34-L115】 |

> 💡 **客户端 ID 规划建议**：Account/Orders/Market Data 服务默认监听不同的 Redis 通道，可将客户端 ID 分别设置为 `101/102/103`
等固定值，以便在 IB Gateway/TWS 侧快速识别连接来源。

## Redis 连接与覆盖规则

全局默认 Redis 连接由 `IBBOT_REDIS_URL` 提供，其它服务在未设置专属 URL 时也会继承该地址。需要拆分依赖时，可通过下列变量覆
盖到独立的 Redis 实例：

| 变量 | 默认行为 | 典型用途 |
| --- | --- | --- |
| `IBBOT_REDIS_URL` | 所有模块共用的默认 Redis 连接串。 | 本地开发或小规模部署时的统一入口。【F:src/ibbot/redis/__init__.py†L44-L79】 |
| `ACCOUNT_CACHE_REDIS_URL` | 账户快照缓存专用 Redis；空值时继承 `IBBOT_REDIS_URL`。 | 减少行情/策略写入对账户缓存的影响。 |
| `ORDERS_REDIS_URL` | 订单服务订阅与发布使用的 Redis。 | 与策略或通知服务解耦订单事件。 |
| `STRATEGY_REDIS_URL` | 策略消费行情/订单/账户事件的 Redis。 | 在仿真环境中隔离策略实验数据。 |
| `NOTIFICATION_REDIS_URL` | 通知服务队列使用的 Redis。 | 允许在生产中使用托管 Redis 专注于可靠性。 |

`src/ibbot/redis.get_redis_client` 会在获取 Redis 客户端前调用 `_prime_shared_client`，确保 `REDIS_URL` 与 `IBBOT_REDIS_URL`
保持一致；若需要完全隔离，请同时设置 URL 和密码等认证参数。【F:src/ibbot/redis/__init__.py†L81-L141】

## Redis 通道命名规范

各服务通过枚举 `RedisChannel` 发布标准化消息，便于下游统一消费与观测。下表列出了当前的通道后缀及载荷说明：

| 通道 | 发布方 | 载荷 | 说明 |
| --- | --- | --- | --- |
| `ib:account:updates` | Account | `AccountUpdateEvent` | 账户摘要（余额、净值等）增量更新。【F:src/ib/events.py†L257-L272】 |
| `ib:account:portfolio` | Account | `PortfolioPositionEvent` | 持仓/成本/实时盈亏快照。【F:src/ib/events.py†L257-L272】 |
| `ib:orders:status` | Orders | `OrderStatusEvent` | 订单状态与剩余数量更新。【F:src/ib/events.py†L273-L278】 |
| `ib:orders:executions` | Orders | `ExecutionEvent` | 成交明细与佣金事件。【F:src/ib/events.py†L273-L278】 |
| `ib:market:ticks` | Market Data | `MarketTickEvent` | 逐笔行情价格/数量变动。【F:src/ib/events.py†L279-L284】 |
| `ib:market:dom` | Market Data | `DOMLevelEvent` | 深度盘口增量更新。【F:src/ib/events.py†L279-L284】 |
| `ib:market:tick-by-tick` | Market Data | `TickByTickEvent` | 官方 tick-by-tick 回调聚合。【F:src/ib/events.py†L285-L289】 |
| `ib:market:real-time-bars` | Market Data | `RealTimeBarEvent` | 实时 bar（OHLCV）数据。【F:src/ib/events.py†L290-L296】 |
| `ib:errors` | 所有服务 | `ErrorEvent` | IB API 错误/断线回调广播。【F:src/ib/events.py†L297-L305】 |

## 市场数据服务运行时开关

市场数据服务支持通过环境变量调整订阅上限、延迟数据兜底等行为：

- `MARKET_DATA_TYPE`：传入 `live`/`delayed`/数值 ID（1~4）以切换 `reqMarketDataType`；默认保留 IB 帐号的实时类型。
- `MARKET_DATA_DELAYED_FALLBACK`：布尔值，控制实时不可用时是否自动切换至延迟数据。【F:services/market_data/main.py†L346-L374】
- `MARKET_DATA_MAX_SUBSCRIPTIONS`：限制同时活跃的订阅数量，超限后会拒绝新请求。【F:services/market_data/main.py†L358-L370】

## 策略服务默认订阅

策略服务会在加载配置中心后，使用以下环境变量作为缺省值补足策略元数据：

- `STRATEGY_DEFAULT_REQUIRED_CHANNELS`：逗号分隔的必须订阅通道列表。
- `STRATEGY_DEFAULT_CHANNEL_MAP`：JSON 或 `channel=alias` 对，映射 Redis 通道到策略内部名称。
- `STRATEGY_DEFAULT_LATENCY_BUDGET_MS`：毫秒级延迟预算，超出后触发健康警报。
- `STRATEGY_USE_DELAYED_DATA`：布尔值，指示默认是否允许订阅延迟行情。

以上变量会在 `services.strategy.main` 启动时解析，并传入 `StrategyAdapter` 与 `StrategyTaskConsumer`，确保在 Redis 连接完
成之前就建立好订阅上下文。【F:services/strategy/main.py†L170-L210】


## 观测与指标

所有基于 FastAPI 的服务都会挂载统一的 `/metrics` 端点，返回 Prometheus 兼容的观测数据：

- `ibbot_redis_publish_failures_total{service="<name>"}`：Redis 发布失败的次数，来自 `RedisPublisher` 的重试逻辑。【F:src/ib/redis_streaming.py†L90-L140】
- `ibbot_ib_client_reconnects_total{service="<name>"}`：IB 客户端成功连接/重连的次数，用于监控连通性。【F:services/orders/main.py†L208-L213】
- `ibbot_active_subscriptions{service="<name>",channel="..."}`：当前活跃订阅数量（账户、订单、行情分通道统计）。【F:services/market_data/main.py†L108-L158】

在本地或 CI 中，可运行 `python scripts/refactor_integration_check.py` 触发三个核心服务的订阅端点并捕获 Redis 消息快照。
脚本会将采集结果写入 `data/regression/ib_stream_baseline.json` 供后续回归比对使用。【F:scripts/refactor_integration_check.py†L1-L165】
