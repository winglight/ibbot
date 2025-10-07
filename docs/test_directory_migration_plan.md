# 测试目录迁移任务清单

## 背景
目前仓库同时维护 `test/` 与 `tests/` 两套测试树。`test/` 目录承担真实依赖协作的冒烟回归，`tests/` 目录覆盖分层单元与集成场景。为统一维护，需要在不丢失覆盖的前提下，将 `test/` 中仍具独特价值的用例迁移至 `tests/`。

## 迁移目标
- 将 `test/` 中所有仍具独特覆盖的场景迁移或重写到 `tests/` 相应分层中。
- 在 CI 与开发指南中统一使用 `tests/` 目录执行测试。
- 确认迁移后可删除旧的 `test/` 目录而不损失测试保障。

## 任务分解

## 当前进度概览
- 差异盘点：**3 / 3** 项已完成。
- 场景迁移与重写：**11 / 11** 项已完成。
- 并行验证：**0 / 3** 项已启动，待全量迁移完成后展开。
- 覆盖验证与文档更新：**0 / 3** 项待执行。
- 完成标准：**0 / 3** 项待验证。

### 1. 差异盘点（Owner: QA/Core Dev）
- [x] 列出 `test/` 中各文件覆盖的路由、服务、依赖协作链路。（详见《`test/` 目录用例覆盖矩阵》）
- [x] 对照 `tests/` 目录确认已覆盖项与缺口，形成差异矩阵。
- [x] 标记需要迁移/重写的用例，并确定目标分层位置（如 `tests/integration`、`tests/data_layer` 等）。

### 2. 场景迁移与重写
- [x] `test/test_auth.py`：在 `tests/` 中新增无桩件的登录冒烟测试，验证 JWT、角色与权限返回。（已迁移至 `tests/integration/auth/test_login_smoke.py`）
- [x] `test/test_dom_config.py`：迁移成覆盖真实依赖的 DOM 配置读取与写入流程。（见 `tests/integration/dom/test_config_roundtrip.py`）
- [x] `test/test_connection.py`：补齐真实依赖下的连接配置回归，确保与桩件单测互补。（已迁移至 `tests/integration/connection/test_connection_endpoints.py`）
- [x] `test/test_market_quote_adapter.py`：在 `tests/` 中重写行情适配器→事件总线→数据管理的端到端链路测试。（见 `tests/data_layer/test_market_quote_adapter.py`）
- [x] `test/test_dom_depth_adapter.py`：迁移 DOM 深度适配器与数据源管理器/事件总线的集成测试，覆盖快照缓存、历史裁剪与 `force_restart` 强制重启路径。（见 `tests/data_layer/test_dom_depth_adapter.py`）
- [x] `test/test_dom_routes.py`：在 `tests/` 中重写 DOM HTTP 路由集成测试，覆盖启停、权限校验、数据查询与告警确认。（见 `tests/integration/dom/test_routes_endpoints.py`）
- [x] `test/test_data_layer_manager.py`：迁移数据源管理器的引用计数、快照/历史代理与 `force_restart` 去重路径。（见 `tests/data_layer/test_data_source_manager.py`）
- [x] `test/test_data_layer_event_bus.py`：迁移事件总线的同步/异步处理器分发、退订与 `publish_nowait` 行为验证。（见 `tests/data_layer/test_event_bus.py`）
- [x] `test/test_data_layer_config.py`：迁移数据层模式与 K 线配置的纯函数测试，保留枚举解析与历史上限校验。（见 `tests/config/test_data_layer_config.py`）
- [x] 其他差异矩阵标记的场景：按优先级逐项迁移，并记录完成状态。
  - [x] `test/test_market_data_fallback.py`：迁移 `/api/chart_data` 服务回退用例，覆盖缓存 key 与 404 处理。（见 `tests/integration/market_data/test_market_data_fallback.py`）
  - [x] `test/test_depth_subscriber.py`：迁移订阅线程超时与强制重启覆盖。（见 `tests/data_layer/test_depth_subscriber.py`）
  - [x] `test/test_optimizer_report_payload.py`：迁移 `_build_report_payload` 汇总逻辑的去重与统计字段校验。（见 `tests/services/optimizer/test_report_payload.py`）
  - [x] `test/test_api_docs.py`：迁移 `/api-docs.json` 与 `docs/api_doc.json` 的快照比对，保留分类断言与更新提示。（已迁移至 `tests/integration/docs/test_api_docs_snapshot.py`）

### 3. 并行验证
- [ ] 在迁移过程中持续运行 `pytest test` 与 `pytest tests`，对比确保旧目录仍全部通过。
  - 2025-10-07：`pytest -q test --disable-warnings` ✅（37 项通过，0 失败）。
  - 2025-10-07：`pytest -q tests --disable-warnings` ❌（31 个模块导入失败，缺少 `services.*` 运行时依赖；需补充打桩或在计划中标记为外部组件缺口）。
  - 2025-10-07：`pytest -q tests --disable-warnings` ❌（75 个用例在集合阶段后失败：`src.routes` 下缺失的 `get_*_service_client` 工厂导致大量路由用例无法猴子补丁，`/api/login` 亦返回 401；需补齐路由层依赖注入或调整测试替身）。
  - 2025-10-07：`pytest -q tests --disable-warnings` ⚠️（所有 96 项断言通过且 3 项跳过；终止阶段仍需手动 Ctrl+C 退出残留后台线程，后续需排查退出阻塞原因）。
  - 2025-10-08：`pytest -q tests --disable-warnings` ❌（运行可自然结束，无需手动中断；但 `tests/routes/*` 仍因缺失 `get_*_service_client` 工厂抛出 57 个错误，另有 `tests/ui/test_startup.py` 与 `tests/unit/db/test_repositories.py` 2 项断言失败，待补足路由层依赖与仓库桩件）。
  - 2025-10-08：`pytest -q tests --disable-warnings` ⚠️（99 项通过、3 项跳过；新增的 service client 工厂兼容层已解锁路由套件，但执行结束后仍需手动 Ctrl+C 退出残留线程）。
  - 2025-10-08：`pytest -q tests --disable-warnings` ⚠️（106 项通过、3 项跳过；全部断言通过但进程在退出阶段依旧阻塞，需手动 `Ctrl+C` 触发 `KeyboardInterrupt` 后才返回 shell）。
- [ ] 对迁移后的新用例执行 `pytest tests/<target>`，确认断言与行为与旧用例一致。
  - 2025-10-08：`pytest -q tests/ui/test_startup.py --disable-warnings` ✅（5 项通过；补充登录桩件与 SQLite 句柄检查后行为与旧冒烟一致）。
  - 2025-10-08：`pytest -q tests/unit/db/test_repositories.py::test_risk_rules_repository_uses_naive_timestamp_for_mariadb --disable-warnings` ✅（验证风险规则仓库在 MariaDB 模式下写入朴素时间戳，与旧套件断言一致）。
- [ ] 记录执行结果与覆盖率快照（如使用 `pytest --cov`）以便最终验收。

### 4. 覆盖验证与文档更新
- [ ] 确认差异矩阵中的每个旧场景在 `tests/` 中有对应断言。
- [ ] 更新 README/开发指南中的测试执行说明，统一推荐 `pytest tests ...`。
- [ ] 准备删除 `test/` 目录的变更，确保流水线脚本同步调整。

### 5. 完成标准
- [ ] `pytest tests` 全量通过，覆盖率不低于迁移前水平。
- [ ] CI/CD 仅依赖 `tests/` 目录即可完成回归。
- [ ] `test/` 目录清空或移除后无用例缺失、无脚本引用残留。

---
**备注**：执行过程中若发现 `tests/` 现有架构不便承载某些集成场景，可评估新增 `tests/integration` 子目录或调整 fixture 设计，确保迁移后的可维护性与执行效率。
