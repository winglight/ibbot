# `test/` 目录用例覆盖矩阵

> 目的：梳理旧目录中仍具独特价值的回归场景，对照 `tests/` 现有用例识别缺口，为迁移提供依据。

| 旧目录文件 | 主要验证场景 | `tests/` 中的相关用例 | 缺口与迁移备注 | 推荐落库位置 |
| --- | --- | --- | --- | --- |
| `test/test_auth.py` | 使用真实 FastAPI app、Redis 替身与环境变量完成 `/api/login` 登录冒烟，以及访客账号写权限限制。 | `tests/ui/test_startup.py` 在大量 monkeypatch 下走启动流程并调用 `/api/login`。 | ✅ 已迁移到 `tests/integration/auth/test_login_smoke.py`，保留无桩件的 HTTP 冒烟回归用于验证 JWT/角色。 | `tests/integration/auth` |
| `test/test_connection.py` | 真实应用下的 `/api/connection_config` 与 `/api/connection_status` 回归，验证序列化字段与账号类型。 | ✅ `tests/integration/connection/test_connection_endpoints.py` 在真实 app 上断言环境配置与 HTTP 回退 payload。 | 已迁移，保留了账号类型与回退响应的断言。 | `tests/integration/connection` |
| `test/test_dom_config.py` | 通过真实依赖读取/修改 `/api/config/dom` 配置并回滚，检验写入后返回体。 | ✅ `tests/integration/dom/test_config_roundtrip.py` 复用真实 app + Redis 替身完成读写回归。 | 已迁移，保留 round-trip 校验并在测试中回滚原配置。 | `tests/integration/dom` |
| `test/test_dom_routes.py` | 结合 `main.app` 与注入的 `DummyDomService` 覆盖 DOM 启停、权限、metrics、history、alerts 等完整流程。 | ✅ `tests/integration/dom/test_routes_endpoints.py` 在真实 app 上复用 stub DOM 服务，覆盖权限、启停、数据、历史、告警等路径。 | 已迁移，同时保留对读写权限、超时 pending、告警 ack 等断言。 | `tests/integration/dom` |
| `test/test_dom_depth_adapter.py` | 将 `DomDepthAdapter` 与 `DataSourceManager`、`EventBus` 串联，验证订阅→事件→快照/历史。 | ✅ `tests/data_layer/test_dom_depth_adapter.py` 使用内存订阅器发布行情并断言历史裁剪与 `force_restart`。 | 已迁移，保留事件发布、缓存快照与强制重启路径的断言。 | `tests/data_layer` |
| `test/test_market_quote_adapter.py` | 串联 `InMemoryQuoteFeed`、`MarketQuoteAdapter`、`DataSourceManager`、`EventBus`，验证 tick→candle 生命周期与缓存。 | ✅ `tests/data_layer/test_market_quote_adapter.py` 使用内存行情源驱动真实事件总线，断言快照、历史与退订行为。 | 已迁移，保留 tick→candle 生命周期与退订后的静默保障。 | `tests/data_layer` |
| `test/test_data_layer_manager.py` | 验证 `DataSourceManager` 的去重、订阅生命周期、快照/历史与 `force_restart` 行为。 | ✅ `tests/data_layer/test_data_source_manager.py` 复用内存适配器校验引用计数、快照/历史代理与 `force_restart` 去重。 | 已迁移，保留引用计数取消时机与选项去重的断言。 | `tests/data_layer` |
| `test/test_data_layer_event_bus.py` | 测试 `EventBus` 对同步/异步 handler 的派发与取消订阅行为。 | ✅ `tests/data_layer/test_event_bus.py` 直接 exercise `EventBus`，覆盖同步/异步处理器、退订与 `publish_nowait` 行为。 | 已迁移，保留轻量化单元覆盖验证基础分发语义。 | `tests/data_layer` |
| `test/test_data_layer_config.py` | 覆盖 `get_data_layer_mode` 与 helper 函数的枚举解析。 | ✅ `tests/config/test_data_layer_config.py` 验证模式解析、helper 断言与 K 线配置覆盖。 | 已迁移，新增覆盖环境 override、历史上限校验与遗留键兼容性。 | `tests/config` |
| `test/test_depth_subscriber.py` | 验证 `DepthSubscriber` 在连接线程失败时按 15 秒超时回退。 | ✅ `tests/data_layer/test_depth_subscriber.py` 断言订阅失败路径的 15 秒超时，以及强制重启时的取消超时和合同重建。 | 已迁移并扩展覆盖未连接错误处理与 `force_restart` 去重。 | `tests/data_layer` |
| `test/test_market_data_fallback.py` | 在真实路由上替换 Market Data service 客户端，验证 `/api/chart_data` 对服务响应、空结果、缓存隔离的处理。 | ✅ `tests/integration/market_data/test_market_data_fallback.py` 复用真实 FastAPI app，并通过 fake 服务断言缓存 key、服务调用顺序与 404 回退。 | 已迁移，保留缓存清理的 autouse fixture 并确保不会访问遗留数据层。 | `tests/integration/market_data` |
| `test/test_optimizer_report_payload.py` | 验证 `_build_report_payload` 对优化结果汇总、失败原因去重、统计字段。 | ✅ `tests/services/optimizer/test_report_payload.py` 断言结果截断、统计补全与失败原因去重。 | 已迁移，扩展校验已有 summary 字段保持不变。 | `tests/services/optimizer` |
| `test/test_api_docs.py` | 比对 `/api-docs.json` 输出与 `docs/api_doc.json` 的分类快照。 | `tests/integration/docs/test_api_docs_snapshot.py` 使用真实 FastAPI app 生成分类并校验快照。 | ✅ 已迁移：保留分类构建逻辑与差异提示，确保更新时先刷新 `docs/api_doc.json`。 | `tests/integration/docs` |

附录：
- `test/README.md`、`test/ib_insync_stub.py`（现已实现 `ib_async` 兼容 stub）等辅助文件暂未纳入矩阵，待迁移实作时根据需要复制/内联。
- 差异矩阵将随迁移进度更新状态列（如 `✅ 已迁移`、`🚧 迁移中`）。
