# DOM Data Flow Review

## IBKR 到内部策略的数据路径

1. **连接与订阅建立**：`ConnectionManager` 负责管理与 IBKR 的连接、重试与线程隔离；深度订阅模块通过它获取 `IB` 实例并在连接线程里执行订阅与取消操作，保证 API 调用不会阻塞主循环。【F:src/connection_manager.py†L1-L200】【F:src/depth_subscriber.py†L32-L213】
2. **共享依赖装配**：`src/dom_dependencies.py` 在加载时创建了单例的 `DOMProcessor` 与 `DepthSubscriber`，供核心模块与 DOM 服务复用，避免循环依赖。【F:src/dom_dependencies.py†L1-L58】
3. **数据处理与事件派发**：`DOMService` 将 `DepthSubscriber` 推送的 DOM 更新写入缓存、调用 `DOMProcessor` 生成指标，并通过内部监听表 `_listeners` 广播最新指标与事件，供上层策略与界面消费。【F:src/dom_service.py†L55-L192】【F:src/dom_service.py†L262-L295】
4. **策略订阅与执行**：`DOMStrategy` 在启动时确保目标合约的深度流已开启，并把自身注册为 DOM 服务监听者；收到指标后更新内部状态并触发下单逻辑，同时对长时间无数据的情况做断路器保护。【F:src/strategies/dom_strategy.py†L149-L205】【F:src/strategies/dom_strategy.py†L243-L340】【F:src/strategies/dom_strategy.py†L959-L979】
5. **策略管理与数据推送状态**：策略服务运行时通过 `request_data_push_state` 协调订阅生命周期——校验策略配置、调用 `DOMService` 启动或停止深度流、在失败时回滚状态，并在状态轮询时检测长时间无数据以触发重启。【F:services/strategy/http.py†L362-L410】【F:services/strategy/http.py†L998-L1045】

以上链路实现了“IBKR → 深度订阅 → DOM 服务 → 策略”的闭环，一旦连接或订阅失败，对应状态会从 `DOMService` 与 `StrategyManager` 回传到界面与监控逻辑。

## 问题 1：是否需要统一的数据订阅 Hub？

当前架构对 DOM 数据已经有较完整的单元：`DepthSubscriber` + `DOMService` 提供了统一的流式订阅、缓存与监听机制。但 K 线/实时价格数据仍然通过 `IBKRClient.get_real_time_data` 等方法按需调用，没有统一的推送入口或缓存策略，策略或前端若想复用只能各自调用 IBKR 接口。【F:src/ibkr_client.py†L525-L700】

若系统未来需要让多个模块同时订阅不同类型的数据（DOM、分时、K 线等），构建一个抽象的订阅 Hub 可以带来：

- 统一的连接与权限控制，避免在多个地方直接操作 IBKR 接口。
- 共享的缓存与节流策略，减少重复请求。
- 标准化的事件格式与生命周期管理，便于策略、告警、界面共享同一数据源。

然而，现有 DOM 流程已经耦合在 `DOMService`，贸然替换需要重构现有监听接口与状态管理。更务实的路径是先设计一个可扩展的“数据源管理”层，将 DOM 订阅作为第一种实现，再逐步把 K 线、实时行情纳入，从而平滑迁移。

## 问题 2：为何有的模块能收到 DOM，有的停止？

- **订阅配置缺失或失败回滚**：当策略未配置 `data_subscription`/`primary_symbol`，或 `DOMService.start_stream` 因连接未就绪而失败时，策略服务运行时会将状态重置为未启用并记录错误，导致该策略表现为“停止接收”。【F:services/strategy/http.py†L362-L410】【F:src/dom_service.py†L520-L578】
- **仅支持单活跃合约**：`DOMService` 使用 `_active_symbol` 标记当前活跃流，并在 `is_stream_active` 中直接与该字段比较；虽然底层 `DepthSubscriber` 能维护多流，但服务层的单一活跃符号逻辑会让后续请求覆盖前者，其他模块因此观察到数据中断。【F:src/dom_service.py†L756-L795】
- **运行时健康检测触发停用/重启**：`DOMStrategy` 的运行状态依赖 `_last_data_timestamp`，超过 5 秒无更新即视为未接收；策略服务运行时的状态生成逻辑也会在长时间无数据时写入“stalled”并调度强制重启。这些机制能自动恢复，但在重启窗口内模块会显示为“停止”。【F:src/strategies/dom_strategy.py†L959-L979】【F:services/strategy/http.py†L980-L1045】

因此，“能/不能接收 DOM”往往由配置、连接状态和上述健康检查决定。排查流程应优先确认策略的订阅配置、IBKR 连接、`DOMService` 是否已持有目标符号的活跃流，以及是否正在执行自动重启。

## 问题 3：是否需要 Pub/Sub 模块？

目前 DOM 数据通过 `DOMService` 的监听表广播，通知与 WebSocket 则分别由 `NotificationManager`、`WebSocketManager` 处理；它们都是进程内、松耦合的实现，缺乏跨模块统一的消息总线。【F:src/dom_service.py†L262-L295】【F:src/notification_manager.py†L1-L200】【F:src/websocket_manager.py†L1-L60】

引入专门的 Pub/Sub 模块可以：

- 统一 DOM、行情、告警、策略事件的投递接口，避免每个服务维护各自的监听列表。
- 支持多消费者、历史重放、持久化等能力，提升监控与回放场景的可扩展性。
- 为未来拆分服务或部署分布式组件（如优化器、UI、告警引擎）提供基础设施。

实施时需考虑性能与一致性：在进程内可优先构建轻量级事件总线（如基于 asyncio/队列），待需求增长再引入外部消息系统（Redis、NATS 等）。与此同时，要为现有依赖 `DOMService` 的代码提供兼容层，确保迁移平滑。

## 建议总结

1. 在现有 DOM 管线之上抽象“数据源管理”接口，并逐步将 K 线/实时行情纳入统一订阅体系。
2. 优化 `DOMService` 以支持多活跃符号或显式限制单流，并在界面/日志中暴露详细状态，帮助排查订阅被覆盖的情况。
3. 设计轻量级 Pub/Sub 总线整合 DOM、行情、告警与策略事件，为未来扩展提供统一的消息通路。

这些改动能够减少模块间的状态分裂，提升数据流的可观测性与可扩展性。
