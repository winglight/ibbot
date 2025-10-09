# TWS API 官方 Testbed 示例中的关键封装

本文整理自 `IBJts/samples/Python/Testbed/Program.py`，汇总官方 Testbed 对 TWS API 的标准封装方式，特别关注账户/资产订阅、市场数据订阅（包含逐笔与深度）以及股票/期货下单的调用入口。

## 账户与资产数据订阅

官方 Demo 在 `TestApp.accountOperations_req` 中集中封装了所有账户层面的订阅请求：

- **托管账户列表**：`reqManagedAccts()`
- **家族账户代码**：`reqFamilyCodes()`
- **账户汇总信息**：`reqAccountSummary(9001, "All", AccountSummaryTags.AllTags)` 以及针对不同分类的 `$LEDGER` 变体。
- **实时账户更新**：`reqAccountUpdates(True, self.account)`，一次只能订阅一个账户；取消时调用 `reqAccountUpdates(False, self.account)`。
- **多账户/多模型更新**：`reqAccountUpdatesMulti(9005, self.account, "", True)` 与 `cancelAccountUpdatesMulti(9005)`。
- **账户持仓**：`reqPositions()`、`reqPositionsMulti(9006, self.account, "")` 以及对应的 `cancelPositions*`。
- **用户信息**：`reqUserInfo(0)`。

这些请求对应的回调封装在同一个类中，例如 `accountSummary`、`updateAccountValue`、`updateAccountTime`、`accountUpdateMulti`、`position`/`positionMulti` 等方法用于接收数据并打印示例输出。【F:IBJts/samples/Python/Testbed/Program.py†L409-L485】【F:IBJts/samples/Python/Testbed/Program.py†L539-L643】

## 市场数据订阅

### Tick 级实时数据

`TestApp.tickDataOperations_req` 封装了多种 `reqMktData` 示例：

- 普通股票/组合报价订阅：`reqMktData(1000, ContractSamples.USStockAtSmart(), "", False, False, [])`。
- 快照式请求：`reqMktData(..., True, ...)`。
- 监管快照、RTVolume、新闻、期货持仓、ETF 专属等，通过 `genericTickList` 参数指定。
- 配合 `reqMarketDataType(MarketDataTypeEnum.DELAYED_FROZEN)` 切换数据类型。

对应的 tick 回调包括 `tickPrice`、`tickSize`、`tickString`、`tickGeneric` 等，以及 `tickByTickOperations_req`/`cancel` 中的 `reqTickByTickData` 与回调 `tickByTickAllLast`、`tickByTickBidAsk`、`tickByTickMidPoint`。【F:IBJts/samples/Python/Testbed/Program.py†L676-L863】【F:IBJts/samples/Python/Testbed/Program.py†L700-L870】

### 市场深度（DOM）

`TestApp.marketDepthOperations_req` 展示了 L1/L2 深度订阅：

- `reqMktDepth(2001, ContractSamples.EurGbpFx(), 5, False, [])` 请求传统深度。
- `reqMktDepth(2002, ContractSamples.EuropeanStock(), 5, True, [])` 请求 SMART 深度。
- `reqMktDepthExchanges()` 获取可用深度路由。

回调 `updateMktDepth` 与 `updateMktDepthL2` 分别处理逐层/逐做市商的 DOM 更新；取消订阅由 `marketDepthOperations_cancel` 中的 `cancelMktDepth` 完成。【F:IBJts/samples/Python/Testbed/Program.py†L918-L990】

## 下单示例（股票与期货）

官方 Testbed 在 `placeOrder` 调用处提供多种模板。最基础的股票、期货示例如下：

- **股票市价单**：`self.placeOrder(self.nextOrderId(), ContractSamples.USStockAtSmart(), OrderSamples.MarketOrder("BUY", 100))`。【F:IBJts/samples/Python/Testbed/Program.py†L1483-L1513】
- **股票限价单**：`self.placeOrder(self.nextOrderId(), ContractSamples.EuropeanStock(), OrderSamples.LimitOrder("BUY", 100, 111.11))`。
- **期货限价单**：`self.placeOrder(self.nextOrderId(), ContractSamples.SimpleFuture(), OrderSamples.LimitOrder("BUY", Decimal("1"), 5333))`，同时展示 CME Tagging 字段等扩展。【F:IBJts/samples/Python/Testbed/Program.py†L1951-L1962】

此外还涵盖组合、对冲、FA 账户、IBKR ATS 等高级案例，可按需取用或简化为生产代码的模板。

## 实现要点

1. **统一封装**：官方示例通过 `TestApp`（继承 `EClient` 与 `EWrapper`）集中定义请求与回调，便于在 GUI 或 CLI 中调用不同的操作方法。
2. **请求/取消配对**：每个订阅都提供 `*_req` 与 `*_cancel` 成对方法，使用固定的 `reqId` 便于追踪。
3. **回调打印**：示例默认打印到 stdout，实际项目中可改写为入库或事件驱动处理。
4. **合约与订单模板**：`ContractSamples` 和 `OrderSamples` 提供了标准化的股票、期货、期权、组合、算法订单等示例配置。

按照上述结构即可在自定义项目中复制官方 Demo 的做法，快速搭建账户、行情、交易的基础框架。
