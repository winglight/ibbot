# 实时行情与DOM推流排查指引

本文基于主程序现有的数据流实现，整理了实时价格按钮与 DOM 启动按钮出现“无数据”或“无法推流”时的逐步排查流程。每一步都对应代码中的具体组件，按照顺序执行可以确定是哪个环节阻断了数据。

## 1. 组件链路总览

### 1.1 实时价格链路

1. 前端监控弹窗点击“实时”后，`static/js/monitoring/manager.js` 会向主站 WebSocket 发送 `subscribe_realtime` 消息。 【F:static/js/monitoring/manager.js†L1847-L1890】
2. 主站 `/ws` 端点在 `src/routes/websocket.py` 中接收消息，调用 `_REALTIME_PRICE_HUB.subscribe` 建立与统一数据层的推送；如果失败则退回 `_realtime_price_sender` 轮询模式。 【F:src/routes/websocket.py†L2201-L2222】【F:src/routes/websocket.py†L1524-L1556】
3. `RealtimePriceHub` 通过统一数据层事件总线订阅 `market.tick`，缓存最近一次行情并推送给所有 WebSocket 客户端。 【F:src/routes/websocket.py†L1325-L1498】【F:src/routes/websocket.py†L3477-L3489】
4. 统一数据层在 `src/data_layer/bootstrap.py` 中注册了 `MarketQuoteAdapter`，它从 Redis/行情服务中取到实时价并投递到事件总线。 【F:src/data_layer/bootstrap.py†L38-L71】
5. 行情服务 (`services/market_data/main.py`) 监听 `pubsub:ib:price:{symbol}` 频道，将 Redis 中的数据合成快照并广播。 【F:services/market_data/main.py†L1321-L1370】
6. IB 行情采集 (`services/ib_feed/main.py`) 将行情更新发布到同一频道，实现自 IB 到 Redis 的上游闭环。 【F:services/ib_feed/main.py†L263-L299】

### 1.2 DOM 链路

1. “启动 DOM” 按钮调用 `DOMManager.start()`，先请求 `/api/dom/start` 再建立 `/ws/dom/{symbol}` WebSocket。 【F:static/js/dom.js†L880-L915】【F:static/js/dom.js†L1177-L1203】
2. 主站 `src/routes/dom.py` 将 DOM WebSocket 请求桥接到 DOM 服务，实现数据透传。 【F:src/routes/dom.py†L257-L285】
3. DOM 服务调用 `DomService.request_stream_start`/`start_stream`，通过统一数据层订阅 `dom.depth` 并监听事件总线。 【F:src/dom_service.py†L650-L740】【F:src/dom_service.py†L780-L847】
4. 统一数据层在 `bootstrap.py` 中注册 `DomDepthAdapter`，负责把 Redis Pub/Sub 的 DOM 深度数据写入事件总线。 【F:src/data_layer/bootstrap.py†L38-L58】
5. 行情服务监听 `pubsub:ib:dom:{symbol}` 频道，缓存 DOM 快照并推送给主站。 【F:services/market_data/main.py†L1321-L1370】
6. IB Feed 同样负责把深度数据写入对应频道。 【F:services/ib_feed/main.py†L263-L299】

## 2. 实时价格排查步骤

> 建议在测试环境执行，确保 `.env` 或 `.env.local` 中的 Redis、数据层、服务地址与线上一致。主站可通过 `python -m dotenv -f .env.local run uvicorn main:app --host 0.0.0.0 --port 8005` 启动。 【F:start_commands.md†L37-L67】

1. **前端是否真正发起订阅**  
   - 打开浏览器开发者工具 → Console，点击“实时”按钮，应看到 `[monitor] subscribe realtime` 日志，且 Network 面板出现 `ws://.../ws` 帧。  
   - 若没有日志，检查当前监控上下文是否有符号，或是否被其他脚本覆盖 `subscribeMonitorRealtime`。 【F:static/js/monitoring/manager.js†L1847-L1890】

2. **确认 WebSocket 收到订阅指令**  
   - 在主站控制台启用调试日志：启动前设置 `export LOGURU_LEVEL=DEBUG`。  
   - 观察 `RealtimePriceHub subscribe requested` 或 “Falling back to polling realtime feed” 日志，说明消息已到达。 【F:src/routes/websocket.py†L1362-L1410】【F:src/routes/websocket.py†L2207-L2216】
   - 若看不到日志，可使用 `python scripts/ws_debug.py`（可临时编写）或 `wscat` 手动连 `/ws`，发送 `{"type": "subscribe_realtime", "symbol": "M2K"}` 验证。

3. **校验统一数据层状态**  
   - `RealtimePriceHub` 仅在 `DATA_LAYER_MODE` 不是 `legacy` 时工作。运行 `python - <<'PY' ...` 打印 `get_data_layer_mode()`；如返回 `legacy`，需要检查环境变量。 【F:src/routes/websocket.py†L1334-L1360】【F:src/data_layer/config.py†L55-L74】
   - 若数据层不可用，会自动退回 `_realtime_price_sender` 轮询；此路径使用市场数据服务的 REST 接口而非 Redis。 【F:src/routes/websocket.py†L1524-L1549】

4. **确认数据层已初始化 Redis 连接**  
   - `get_data_source_manager()` 会注册 Redis 缓存/持久化；若 Redis 地址配置错误，会回退到内存缓存导致无法接收跨进程数据。检查日志中 “Using Redis cache backend at ...” 与环境变量 `DATA_LAYER_REDIS_URL`、`DATA_LAYER_REDIS_PREFIX`。 【F:src/data_layer/bootstrap.py†L38-L104】

5. **检查 Redis Pub/Sub 是否有数据**  
   - 使用 `redis-cli -u $DATA_LAYER_REDIS_URL SUBSCRIBE pubsub:ib:price:M2K`（注意前缀配置）观察是否有消息。频道名称由 `PRICE_CHANNEL_TEMPLATE` 生成。 【F:services/market_data/main.py†L1321-L1342】
   - 若无消息，到 `services/ib_feed` 服务日志中确认 `_publish_price_snapshot` 是否被调用，或直接在 Redis 查看 `XADD` 流是否写入。 【F:services/ib_feed/main.py†L263-L299】

6. **验证行情服务快照是否可用**  
   - 调用 `curl http://<market-service>/market/M2K` 或 `/price/M2K`，应返回带 `timestamp` 的快照。该快照由 `_update_snapshot` 维护。 【F:services/market_data/main.py†L1356-L1370】
   - 如果市场服务收到 Redis 消息但 `runtime.get_snapshot` 返回空，检查是否订阅列表包含目标符号（`self.subscriptions`），以及是否曾执行 `/market/subscribe` 之类的初始化接口。

7. **确认主站转发**  
   - 成功的订阅会触发 `_forward_realtime_subscription` 循环向 WebSocket 推送。可在 `/ws` 的抓包中验证收到 `type: realtime_data`、`data.symbol: M2K` 的 JSON。 【F:src/routes/websocket.py†L3477-L3489】
   - 若 `success: false`，说明退回轮询仍未拿到数据，重点检查第 5、6 步。

## 3. DOM 推流排查步骤

1. **按钮是否真正触发 DOMManager**  
   - Console 中点击“启动 DOM”后应看到 `Failed to start DOM` 等日志；Network 面板出现 `POST /api/dom/start`。  
   - 若按钮不可点，检查 `DOMManager.canStart()` 是否因当前监控没有 symbol 而返回 `false`。 【F:static/js/dom.js†L640-L702】【F:static/js/dom.js†L880-L915】

2. **确认主站 DOM API 正常**  
   - 主站收到请求后，会通过 DOM 服务客户端调用真正的启动逻辑。若 DOM 服务地址配置错误，会直接抛出 HTTP 500。可在日志中搜索 “DOM websocket bridge failed” 或 `Failed to start depth stream`。 【F:src/routes/dom.py†L257-L285】

3. **检查 DOM 服务是否拿到数据层管理器**  
   - DOM 服务的 `get_dom_service()` 在第一次调用时需要成功初始化数据层；日志若提示 “Failed to initialise data layer manager”，说明环境变量、Redis 或数据层模式异常。 【F:src/dom_service.py†L650-L740】
   - 确认 `DATA_LAYER_MODE` 不是 `legacy`，否则 `_start_stream_unified` 会失败。

4. **验证 DOM 频道的 Redis 消息**  
   - 使用 `redis-cli` 订阅 `pubsub:ib:dom:M2K`（带上实际前缀）。如果没有消息，说明 IB Feed 或行情服务没有产出 DOM 数据。 【F:services/market_data/main.py†L1325-L1354】【F:services/ib_feed/main.py†L269-L299】
   - 检查 IB Feed 日志是否有 `process_dom_update` 调用以及 `_publish_dom_snapshot` 成功执行。

5. **确认统一数据层事件**  
   - DOM 服务会通过 `manager.event_bus.subscribe("dom.depth", ...)` 获取事件。可以在 DOM 服务中临时开启 DEBUG，确认 `_handle_dom_depth_event` 是否持续被调用。 【F:src/dom_service.py†L661-L682】
   - 若事件到达但 WebSocket 仍无数据，检查 `_on_depth_update` 后续是否成功写入缓存/广播。

6. **检查 DOM WebSocket 桥接**  
   - 当 DOM 服务成功推流后，主站 `/ws/dom/{symbol}` 会把消息原封不动地转发给前端。可以在浏览器的 DOM WebSocket 流里确认收到 `type: dom_stream` 或 `trend_score` 字段。 【F:src/routes/dom.py†L257-L285】【F:static/js/dom.js†L1177-L1203】
   - 如果前端断线频繁，确认 DOM 服务是否返回 `dom_error`，并根据错误提示排查。

## 4. 常见排查结论速查

| 现象 | 优先排查环节 |
| --- | --- |
| WebSocket 返回 `success: false` 且 `error: No market data available` | Redis 行情频道无数据 / 行情服务未缓存快照（步骤 5、6） |
| 点击“实时”无任何反应 | 前端未发送 `subscribe_realtime`（步骤 1） |
| DOM 启动提示成功但图表无更新 | DOM WebSocket 未连上或 Redis DOM 频道无数据（步骤 4、6） |
| DOM 启动立即报错 | DOM 服务未能初始化数据层或订阅失败（步骤 3） |

按照上述顺序逐项验证，可以定位到从 IB Feed → Redis → 数据层 → 主站 → 前端的任一断点，避免盲目改代码。
