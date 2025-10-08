# IB API 迁移分解任务

在替换 `ib_insync` 为官方 `ibapi` 的重构中，建议按照以下阶段推进，每个阶段完成后进行代码评审与回归验证。

## 阶段一：基础设施与依赖准备
1. **依赖与环境**：
   - 在 `requirements.txt` 中移除 `ib_insync`，添加官方 `ibapi` 依赖。
   - 更新 Docker、`start_with_env.py` 等脚本以使用新依赖。
2. **控制器原型**：
   - 新建 `IBAPIController`，继承 `EWrapper`、组合 `EClient`。
   - 实现连接、重连、线程管理、事件循环桥接（使用 `asyncio.Queue`/`Future`）。
   - 提供基础请求封装（`connect`, `disconnect`, `is_connected`）。
3. **单元测试准备**：
   - 为 `ibapi` 创建 stub 或 mock，覆盖现有 `ib_insync` stub 的功能。

## 阶段二：合约与市场数据
1. **合约构建与资格校验**：
   - 替换所有 `ib_insync.contract` 引用为 `ibapi.contract.Contract`。
   - 重写 `_qualify_contract` 等逻辑，通过 `reqContractDetails` 获取结果并缓存。
2. **行情订阅重构**：
   - 使用 `reqMktData`、`reqMktDepth` 实现实时行情与深度。
   - 构建 `MarketDataRouter`，维护 ticker 状态并推送到原有监听队列。
   - 更新 `PriceVolumeStream` 与实时 bar 的对接。
3. **历史与逐笔数据**：
   - 替换 `reqHistoricalDataAsync`、`reqHistoricalTicksAsync` 等调用，建立 Future 异步等待机制。

## 阶段三：订单与执行
1. **下单接口**：
   - 将 `placeOrder`、`cancelOrder` 等封装改写为 `ibapi` 请求。
   - 引入 `TradeBook` 或订单缓存，取代 `ib.openTrades()`、`ib.trades()`。
2. **回报处理**：
   - 在 `orderStatus`、`openOrder`、`execDetails` 等回调中更新内部状态。
   - 调整风控、策略模块对订单状态的读取方式。
3. **测试与校验**：
   - 为订单流新建集成测试或模拟回调测试。

## 阶段四：账户、持仓与资金
1. **账户同步**：
   - 使用 `reqAccountSummary`、`reqAccountUpdates` 获取账户数值，维护缓存并暴露查询接口。
2. **持仓管理**：
   - 重写 `portfolio`、`positions` 等查询，适配 `reqPositions` 回调。
3. **服务层适配**：
   - 确保 FastAPI 服务、数据层调用新的异步接口仍然兼容。

## 阶段五：最终整合与验证
1. **全面测试**：
   - 运行完整 `pytest` 套件，补充针对新控制器的单元测试。
   - 在仿真或测试环境连接 TWS/Gateway，验证行情、下单、账户同步。
2. **文档更新**：
   - 修改 README、开发文档，说明官方 API 的使用方式、启动流程。
3. **部署准备**：
   - 检查监控与日志，确保重连、超时、错误处理符合生产要求。

按照上述阶段逐步推进，可控制风险并确保迁移期间业务连续性。
