# IB API 迁移任务审查

本报告对照 `docs/ibapi_migration_tasks.md` 中定义的阶段性目标，审查当前代码库的实际实现状态，并解释仍然出现的 `ib_insync` 相关错误原因。

## 阶段一复盘（步骤二启动前）
- ✅ **依赖与脚本检查**：`requirements.txt`、`start_with_env.py` 与 `start_service.sh` 均未再引用 `ib_insync`，确认第一阶段的环境改造已经到位。【F:requirements.txt†L1-L26】【F:start_with_env.py†L1-L200】【F:start_service.sh†L1-L120】
- ✅ **连接入口**：`ConnectionManager` 会自动识别测试 stub 并强制回退到内置的 `IBAPIController`，确保缺失真实 `ib_insync` 包时不会再触发“未安装 ib_insync”错误；真实依赖存在时仍保留旧路径供后续阶段逐步收敛。【F:src/connection_manager.py†L1-L1900】

## 阶段二复盘（步骤三推进后）
- ✅ **实时行情路径**：`subscribe_market_data` 与 `_get_real_time_data_legacy` 现在强制通过 `MarketDataRouter` 调用 `IBAPIController`，不再回退到 `ib.reqMktData`，确保在缺失 `ib_insync` 时仍可获取最新报价。【F:src/ibkr_client.py†L3392-L3440】【F:src/ibkr_client.py†L2764-L2885】
- ✅ **历史数据**：`get_historical_data` 已完全依赖 `IBAPIController.request_historical_data`，移除了对 `ib.reqHistoricalDataAsync` 的兜底调用。【F:src/ibkr_client.py†L2443-L2538】
- ✅ **数据模型依赖**：行情与合约对象改为直接构建 `ibapi.contract.Contract`，仅在与旧 `ib_insync` 接口交互时动态转换，主路径已不再维护双栈模型。【F:src/ibkr_client.py†L1486-L1660】【F:src/ibkr_client.py†L1772-L1905】

## 阶段一：基础设施与依赖准备
- ✅ **依赖声明**：`requirements.txt` 只列出了官方 `ibapi` 而没有 `ib_insync`，符合迁移计划的第一步。【F:requirements.txt†L1-L26】
- ✅ **控制器原型**：`ConnectionManager` 默认通过 `IBAPIController` 建立连接，仅在显式设置 `IBKR_ENABLE_IB_INSYNC=1` 且真实依赖存在时才会回退到旧实现，确保官方 API 成为主路径。【F:src/connection_manager.py†L145-L228】【F:src/connection_manager.py†L909-L933】
- ✅ **测试桩**：生产代码与单元测试均改为直接构造 `ibapi` 合约对象，不再导入 `test.ib_insync_stub`，确保缺失 `ib_insync` 时仍可运行。【F:src/ibkr_client.py†L20-L122】【F:tests/unit/test_ibkr_contract_qualification.py†L1-L86】

## 阶段二：合约与市场数据
- ✅ **合约构建**：`IBKRClient` 默认创建 `ibapi.contract.Contract` 对象并使用控制器查询详情，对旧接口的兼容通过按需转换完成，避免了长期维护 `ib_insync` 合约容器的桥接复杂度。【F:src/ibkr_client.py†L1550-L1905】
- ✅ **行情路由**：市场数据订阅与实时拉取路径默认依赖 `MarketDataRouter`，所有行情更新均通过 `IBAPIController` 触发的事件分发，无需再构造 `ib_insync` ticker 对象。【F:src/market_data_router.py†L1-L260】【F:src/ibkr_client.py†L2784-L2934】
- ✅ **历史/逐笔数据**：历史 bar 与 DOM 逐笔抓取均通过 `IBAPIController.request_historical_data` / `request_historical_ticks` 完成，彻底移除了 `ib_insync` 回退分支。【F:src/ibkr_client.py†L2218-L2263】【F:src/ibkr_client.py†L2295-L2551】

## 阶段三：订单与执行
- ✅ **下单接口**：`IBKRClient.place_order` 与 `cancel_order` 等方法现已默认通过 `IBAPIController` 构造官方 `Order` 并提交，`_TradeBook` 在确认回报后会记录订单状态供后续查询和批量取消使用，完全移除了对 `ib_insync` 下单路径的依赖。【F:src/ibkr_client.py†L1486-L1888】【F:src/ibkr_client.py†L3600-L3717】
- ✅ **回报处理**：内置 `_TradeBook` 通过控制器的事件分发队列消费 `openOrder`/`orderStatus`/`execDetails` 等回调，维护内存快照并向风险/策略模块提供实时数据，确保在纯 `ibapi` 模式下亦能跟踪订单生命周期。【F:src/ibapi_controller.py†L175-L205】【F:src/ibkr_client.py†L305-L520】
- ✅ **测试**：新增的 `tests/unit/test_ibkr_order_routing.py` 使用 `IBAPIController` stub 驱动下单、取消与批量操作流程，验证控制器与 `_TradeBook` 的协同行为，替换了旧的 `ib_insync` 订单用例。【F:tests/unit/test_ibkr_order_routing.py†L1-L160】

## 阶段四：账户、持仓与资金
- ✅ **账户同步/持仓管理**：账户信息、账户概要与持仓查询现已强制通过 `IBAPIController` 的 `reqAccountUpdates`、`reqAccountSummary` 与 `reqPositions`，移除了对 `ib.accountValues()`、`ib.accountSummary()`、`ib.portfolio()` 的运行时依赖，缓存与实时估值全部由官方 API 提供的数据维护。【F:src/ibkr_client.py†L3887-L3986】【F:src/ibkr_client.py†L3990-L4096】
- ✅ **服务层适配**：账户快照服务与 FastAPI 端点继续调用 `IBKRClient` 异步接口即可获得最新数据，不再需要 `ib_insync` 客户端；新的单元测试覆盖纯控制器路径以确保在无 `ib_insync` 环境下仍可刷新账户与持仓。【F:src/account_snapshot_service.py†L300-L368】【F:tests/unit/test_ibkr_account_data.py†L1-L136】

## 阶段五：最终整合与验证
- ✅ **全面测试**：完整的 `pytest` 套件覆盖行情、订单、账户与连接路径，并新增 `tests/unit/test_connection_manager_ibapi.py` 验证默认走 `IBAPIController`，测试环境无需 `ib_insync` 即可通过。【F:tests/unit/test_connection_manager_ibapi.py†L1-L86】
- ✅ **文档/部署**：README 调整为以官方 `ibapi` 控制器为核心的架构描述，并新增控制器自检步骤，指导如何在部署前验证连接与诊断脚本。【F:README.md†L31-L120】【F:README.md†L210-L236】

## 错误原因说明
早先日志中的报错源于 `ConnectionManager` 在导入 `ib_insync` 失败时自动回退到测试 stub `test/ib_insync_stub.IB`。该 stub 会在 `connectAsync` 中直接抛出 “`ib_insync is not installed; cannot establish IBKR connection`” 的运行时异常，导致连接循环不断重试并记录相同的警告。【F:test/ib_insync_stub.py†L1-L120】

现在连接管理器会检测这类 stub，并改为直接启用 `IBAPIController`。因此，在缺少真实 `ib_insync` 依赖的环境中将不会再触发上述错误；如需兼容旧实现，可显式设置 `IBKR_ENABLE_IB_INSYNC=1` 并确保依赖安装完成。【F:src/connection_manager.py†L1-L1900】

## 结论
`docs/ibapi_migration_tasks.md` 中列出的迁移任务现已全部完成。项目默认通过 `IBAPIController` 驱动连接、行情、订单、账户与持仓流程，迁移文档与测试套件覆盖纯官方 API 路径，`ib_insync` 仅作为显式启用的兼容选项保留。

## 下一步目标
- ✅ 完成行情与历史数据路径的 `IBAPIController` 迁移，移除了对 `ib_insync` 会话的实时数据依赖。【F:src/ibkr_client.py†L2130-L2198】【F:src/ibkr_client.py†L2784-L2934】
- ✅ Stage Five 自检路径就绪：README 中的控制器验证步骤与诊断脚本确保在部署前即可复现官方 API 流程。【F:README.md†L210-L236】
