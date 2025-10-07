# DOM 数据落盘与 WebSocket 自检改造方案

## 背景现状
- 市场数据配置面板当前的回补接口仅面向蜡烛图：后端 `/market/backfill` 接口会根据用户选择的 barSize 调用 `_fetch_candles_from_ib`，并把结果写回 Redis 及 Parquet。【F:services/market_data/main.py†L1980-L2065】前端 `initMarketDataBackfillCard` 也只渲染 K 线区间和 Redis/Parquet 覆盖度信息。【F:static/js/app.js†L5320-L5409】
- DOM 实时快照由 IB feed 服务推送到 Redis，同时追加到本地 Parquet `data/dom/<symbol>/<date>.parquet`，确保磁盘留存已经存在。【F:services/ib_feed/main.py†L263-L299】此外，`DomCacheManager` 会把快照与指标写入 `data/dom_cache_*.pkl` 方便服务内回放，但它不负责长期保留或补数据。【F:src/dom_cache.py†L29-L119】
- WebSocket 自愈 Runbook 目前仅包含 Redis、快照刷新、策略、订单、日志、数据库等静态检查步骤，没有行情订阅的动态检测条目。【F:src/runbooks/websocket_remediation.py†L300-L344】

## 改造目标
1. **DOM 历史落盘与补拉能力对齐主干分支**
   - 允许配置面板查看 DOM 本地覆盖情况，并按需触发补拉任务，使优化器和主程序都能依赖一致的数据目录。
   - 如果 IB API 支持 DOM 历史查询，应通过独立任务把缺口补齐并存入 Parquet，同时更新内存/Redis 快照。
2. **WebSocket Runbook 增强行情链路自检**
   - 增加实时价格与 DOM 深度两条检测，帮助定位订阅缺失、无数据或落盘目录异常的问题，并给出修复建议。

## 实现方案
### 1. DOM 数据存储与补拉
- **后端 API 扩展**
  - 在 `services/market_data/main.py` 新增 DOM 存储概览接口（例如 `/dom/storage`），复用 `MarketDataParquetWriter.read_dom` 统计本地文件的日期覆盖、最新时间戳以及总快照数，并比对 Redis/缓存的最近更新。可共用现有的 `_list_parquet_segments`/`_summarise_redis_series` 模式。
  - 为回补操作设计新的请求模型（如 `DomBackfillRequest`），允许指定 symbol、时间跨度、深度层级等参数。端点 `/dom/backfill` 会：
    1. 校验 IB fallback 是否开启、Symbol 是否在订阅白名单。
    2. 根据请求调用新的 `_fetch_dom_history_from_ib` 协程：若 IB 不支持历史 DOM，则返回明确错误并在 UI 显示说明；若支持，则按时间窗口批量抓取 Level 2 快照。
    3. 对抓取到的快照调用 `MarketDataParquetWriter.append_dom` 落盘，并通过 `dom_service` 或 redis pubsub 刷新本地缓存/快照，保证之后的回放与优化器查询可见。
    4. 汇总落盘文件、最新时间戳、补写数量返回给前端。
  - 需要引入新的幂等控制（根据时间戳去重），并考虑与 `DomCacheManager` 的联动：在补写成功后主动注入缓存，以便 `/api/dom/history` 即刻可见。

- **IB 历史查询适配**
  - 在 `src/ibkr_client.py` 或独立适配模块封装 DOM 历史抓取逻辑（例如调用 `reqHistoricalTicks` 带 `whatToShow='MKT_DEPTH'` 或 `reqMktDepth` + 快照回放）。实现时需：
    * 支持按开始/结束时间分段抓取，避免单次请求超时。
    * 统一结构输出（bids/asks 数组、timestamp），与实时快照保持一致。
    * 提供能力检测：IB 如果抛出 `HMDS` 不支持等错误，捕获后返回 `warning` 状态，并提示用户手动补档。

- **前端改造**
  - 在 Configuration 面板 `Market Data Backfill` 卡片内增加 DOM 选项卡或折叠面板：
    * 展示 DOM 本地文件覆盖信息与最近更新状态。
    * 表单允许选择 symbol、日期区间、目标深度行数。提交调用 `/dom/backfill`，并在结果中展示成功/失败、更新数量、失败原因。
    * 若后端返回“不支持补拉”，在 UI 提示用户仅能依赖实时订阅。
  - 复用现有 loading/read-only 状态管理，确保访客模式无法触发补拉。

- **Optimizer 对接**
  - 在 `services/optimizer/main.py` 启动前增加校验：如果策略配置使用 DOM，检查 `MarketDataParquetWriter.read_dom` 是否能读取到指定 symbol/日期的数据；若缺失则抛出 `OptimizerDataError`，提示先运行 DOM backfill。
  - 提供 CLI 或 API 让 optimizer 查询可用 DOM 数据区间，用于自动化训练脚本决定补拉范围。

### 2. WebSocket 数据修复 Runbook 扩展
- **新增步骤**
  - `RunbookStep(id="realtime-price")`：
    * 使用 `DataSourceManager` 订阅 `market.tick`，symbol 从环境变量（如 `RUNBOOK_SYMBOL_TICK`）读取，默认回退至热门合约。
    * 先调用 `snapshot()` 检查是否有缓存。如果无快照则标记 `warning` 并给出“检查 IB Feed/Redis”建议。
    * 若有快照，再监听事件总线几秒钟等待推送；收到更新则 `success`，否则 `warning` 并提供排查路径（检查订阅、IB 连通性）。
  - `RunbookStep(id="dom-depth")`：
    * 借助 DOM 服务客户端（`get_dom_service` 或 HTTP `/dom/data`）检查最近 DOM 时间戳，并尝试向 `dom_service.request_stream_start` 订阅或通过 `DataSourceManager` 的 `dom.depth` 渠道监听更新。
    * 检查本地 Parquet 目录是否存在当日文件；若不存在且订阅失败，给出补拉或重启 IB feed 的操作建议。
    * 对于成功收到的 DOM 更新，返回最近一次更新时间与层级计数；若只存在落盘无实时，则置 `warning` 并提示检查 Redis/订阅；完全失败则 `error`。

- **序列化与前端展示**
  - `serialize_runbook_state` 已支持 details 字段，新增步骤会自动显示；需要在前端 Runbook 面板补充字段说明（例如成功时显示“最近 DOM 更新时间 / 本地文件路径”）。

### 3. 验证与回归
- 单元测试：
  - 为新 API 增加 FastAPI 路由测试，模拟成功/不支持/失败三种回补结果。
  - Mock IB 客户端，验证补拉逻辑的时间片循环与落盘调用。
  - Runbook 步骤测试，确保各类返回路径（success/warning/error）被覆盖。
- 集成测试：
  - 在 docker-compose 或本地环境中，通过假 Redis + 假 IB 客户端演练回补与 Runbook，确认 UI 能正确显示状态。
- 回滚策略：
  - 通过 feature flag（如 `DOM_BACKFILL_ENABLED`）控制新接口与 UI 按钮，必要时可快速关闭。

## 风险与兼容性
- IB 历史 DOM 支持存在不确定性，需要在实现前通过 paper 账号验证；若不支持，需确保 UI/Runbook 给出清晰提示并不会阻塞其他功能。
- 批量补拉可能产生大量 Parquet 文件，需考虑磁盘容量与写入锁竞争；应在 `MarketDataParquetWriter` 落盘前添加节流或批量写入优化。
- Runbook 订阅检查涉及真实行情订阅，可能消耗 IB 流量；需提供环境变量控制测试 Symbol 和最大等待时间。


## 实施进度
- [x] DOM 数据存储与补拉 API/IB 客户端落地，支持从配置面板发起补拉并汇总 Redis/缓存/Parquet 覆盖情况。
- [x] WebSocket Runbook 新增实时价格与 DOM 深度诊断，前端以结构化明细呈现排查建议。
- [x] Optimizer 服务与控制台提供 DOM 覆盖查询接口、CLI 以及 UI 面板，便于在落盘完成后校验数据可用性。
