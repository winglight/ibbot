# `app.js` 模块化拆分前分析与迁移计划

为了把超过 12k 行的 `static/js/app.js` 按功能拆分为更易维护的模块，下文整理了现有代码的变量/方法分类及分步迁移计划。文档分为：

1. 全局常量与工具函数概览。
2. `TradingApp` 构造器中的核心状态字段。
3. `TradingApp` 方法按功能模块的清单（含行号）。
4. 通用工具/辅助方法列表。
5. 建议的迁移步骤与每一步的验证要点。

## 1. 全局常量与工具函数

| 名称 | 类型 | 作用 | 代码位置 |
| --- | --- | --- | --- |
| `INTERVAL_MINUTES_MAP` | 常量映射 | 统一监控区间与分钟数的映射 | `static/js/app.js` L2-L11 |
| `MONITOR_SOURCE_TZ` / `MONITOR_FALLBACK_TZ` | 常量 | 监控图表的优先与备用时区 | `static/js/app.js` L13-L15 |
| `MONITOR_CHART_MAX_MINUTES` / `MONITOR_CHART_MAX_POINTS` | 常量 | 控制监控图最大时间跨度与点数 | `static/js/app.js` L15-L16 |
| `MONITOR_DURATION_MINUTES_MAP` | 常量映射 | 支持字符串/数字时长到分钟数转换 | `static/js/app.js` L18-L109 |
| `normalizeIntervalKey` / `normalizeDurationKey` | 函数 | 清洗区间/时长输入为统一格式 | `static/js/app.js` L31-L49 |
| `getMonitorDurationMinutes` | 函数 | 将用户输入解析为分钟数 | `static/js/app.js` L51-L111 |
| `MONITOR_DURATION_OPTIONS` | 常量映射 | 监控时长下拉框选项配置 | `static/js/app.js` L113-L154 |
| `getMonitorDurationOptions` / `refreshMonitorDurationSelect` | 函数 | 根据区间刷新监控时长选择框 | `static/js/app.js` L156-L198 |
| `TREND_LEVEL_STYLES` / `MONITOR_EVENT_SIDE_COLORS` | 常量 | 趋势等级样式与买卖颜色配置 | `static/js/app.js` L200-L291 |
| `getTrendLevelStyle` / `getTrendSuggestionLabel` | 函数 | 趋势提示标签文字与样式获取 | `static/js/app.js` L293-L305 |
| `TrendSignalLabelPlugin` | 对象 | Chart.js 插件，用于在趋势图上绘制文字标记 | `static/js/app.js` L312-L356 |
| `slugifyStrategyName` | 函数 | 将策略名称转换为安全的 `slug` | `static/js/app.js` L358-L364 |
| `window.getTrendLevelStyle`/`window.getTrendSuggestionLabel` | 全局挂载 | 兼容旧的内联脚本调用 | `static/js/app.js` L307-L310 |
| `const app = new TradingApp()` / `window.app = app` | 实例化 | 页面入口，供内联按钮/外部挂钩使用 | `static/js/app.js` L12664-L12668 |

> 以上全局内容可优先抽离到独立工具模块（如 `static/js/monitoring/constants.js` 与 `static/js/monitoring/utils.js`），以便后续模块化复用。

## 2. `TradingApp` 构造器核心状态

`TradingApp` 在构造函数中初始化了大量状态，下表按功能聚类便于后续拆分：

| 功能域 | 关键字段示例 | 说明 | 代码位置 |
| --- | --- | --- | --- |
| WebSocket 与连接 | `this.ws`, `this.connected`, `this.wsHandlers`, `this.ibkrManager` | 跟踪连接状态并注册消息处理器 | `static/js/app.js` L368-L466 |
| 监控与图表 | `this.monitorTrendChart`, `this.monitorTrendData`, `this.monitorChartRenderTimeout`, `this.pendingMonitorChartRender` | 管理监控实时/历史图形及渲染节流 | `static/js/app.js` L398-L425 |
| 策略详情 | `this.strategyChartState`, `this.strategyDetailsState`, `this.strategyPerformanceCache` | 驱动策略详情页的图表、统计、缓存 | `static/js/app.js` L413-L448 |
| 订单与执行 | `this.orders`, `this.internalStrategyOrders`, `this.executionsPagination`, `this.cancellingOrders` | 追踪订单、执行与分页信息 | `static/js/app.js` L371-L383, L391-L395 |
| 风险与提醒 | `this.currentRiskSettings`, `this.riskSettingsSaving`, `this.startReminderTimeout`, `this.endReminderTimeout` | 风险规则编辑状态与交易提醒定时器 | `static/js/app.js` L379-L433 |
| UI 与配置 | `this.isReadOnly`, `this.authRole`, `this.summaryControlsInitialized`, `this.panelLoadingStates` | 控制界面交互、权限、加载指示器 | `static/js/app.js` L426-L458 |
| 管理器依赖 | `LoginManager`, `IBKRConnectionManager`, `OrdersServiceClient`, `NotificationsManager` 等 | 依赖外部模块提供的业务逻辑 | `static/js/app.js` L459-L474 |

这些字段为模块切割提供边界：例如监控相关状态与方法可以与策略、风险模块分离，连接与通知可独立管理。

## 3. `TradingApp` 方法分类概览

下表统计了 `TradingApp` 中 230 个实例方法按功能域的分布（根据方法名关键字初步划分，详见后续清单）：

| 功能模块 | 方法数量 | 说明 |
| --- | --- | --- |
| WebSocket/Connection 管理 | 5 | 连接配置、事件注册、消息分发以及详情弹窗联动 |
| 监控与趋势图表 | 67 | 监控 DOM/趋势信号、图表同步、策略图表渲染等 |
| 策略管理与绩效 | 49 | 策略列表、详情、绩效及内部订单数据处理 |
| 风险控制与规则 | 19 | 风险规则增删改查与状态管理 |
| 下单与执行流程 | 20 | 下单表单、订单执行、历史执行数据处理 |
| 通知与提醒 | 3 | 交易提醒的排程、触发与下一次时间计算 |
| 界面状态与交互 | 22 | 页面初始化、事件绑定、只读模式、配置弹窗等 |
| 数据拉取与缓存 | 6 | 通用数据请求、脚本加载、PnL/配置获取 |
| 通用工具/辅助 | 39 | 各模块共用的工具方法（格式化、节流、第三方依赖加载等） |

### 3.1 方法明细

为避免主文过长，采用可折叠 `<details>` 展示各功能域对应的方法及行号，便于逐个迁移时查阅。

<details>
<summary><strong>WebSocket/Connection 管理 (5)</strong></summary>

- `ensureDefaultConnectionConfig` (L479)
- `registerWebSocketHandler` (L486)
- `unregisterWebSocketHandler` (L500)
- `handleWebSocketPayload` (L547)
- `showStrategyDetailsModal` (L10593)

</details>

<details>
<summary><strong>监控与趋势图表 (67)</strong></summary>

- `addDomSignal` (L938)
- `ensureMonitorAnnotations` (L944)
- `updateDomSignalAnnotations` (L967)
- `setMonitorTrendHistory` (L990)
- `resetMonitorTrendData` (L1008)
- `formatTrendPointForChart` (L1012)
- `getFormattedTrendChartData` (L1084)
- `updateMonitorSignalOverlayDatasets` (L1091)
- `renderMonitorSignalHistory` (L1206)
- `getTrendValues` (L1365)
- `setupTrendLegendAndAnnotations` (L1400)
- `refreshTrend` (L1416)
- `syncTrendReferenceLines` (L1421)
- `deriveTrendLevel` (L1425)
- `addMonitorTrendPoint` (L1435)
- `syncCharts` (L1641)
- `normalizeMonitorTime` (L1669)
- `getMonitorDisplayTimes` (L1686)
- `getMonitorDisplayLabels` (L1691)
- `extractRangeFromMonitorChart` (L1696)
- `guardChartScriptableDescriptors` (L2653)
- `createCandlestickChartConfig` (L2908)
- `shouldResumeMonitorRealtime` (L3557)
- `subscribeMonitorRealtime` (L3564)
- `unsubscribeMonitorRealtime` (L3609)
- `exitMonitorFullscreen` (L3634)
- `openMonitorModal` (L3725)
- `syncMonitorModalData` (L4326)
- `loadMonitorOrdersList` (L4436)
- `_loadMonitorOrdersList` (L4451)
- `getMonitorTimezone` (L4725)
- `parseMonitorChartTime` (L4747)
- `loadMonitorChart` (L4858)
- `updateMonitorStopLossLine` (L6404)
- `updateMonitorTakeProfitLine` (L6442)
- `updateMonitorRealtime` (L6480)
- `displayChart` (L7301)
- `createDefaultStrategyChartState` (L8998)
- `getStrategyChartState` (L9044)
- `getStrategyChartRefreshIntervalMs` (L9051)
- `getStrategyChartTimeUnit` (L9057)
- `updateStrategyChartSymbol` (L9105)
- `setupStrategyChartTab` (L9132)
- `refreshStrategyChartDuration` (L9264)
- `resetStrategyChartState` (L9284)
- `destroyStrategyChart` (L9327)
- `normalizeStrategyChartBars` (L9338)
- `applyStrategyChartOrders` (L9414)
- `setStrategyChartRiskLevels` (L9423)
- `renderStrategyChart` (L9468)
- `updateStrategyChartSummary` (L9806)
- `updateStrategyChartButtons` (L9817)
- `updateStrategyChartStatus` (L9837)
- `loadStrategyChart` (L9863)
- `startStrategyChartRealtime` (L10056)
- `stopStrategyChartRealtime` (L10080)
- `startStrategyChartPlayback` (L10094)
- `stopStrategyChartPlayback` (L10144)
- `toggleStrategyChartRealtime` (L10167)
- `toggleStrategyChartPlayback` (L10176)
- `setupStrategyChartState` (L10693)
- `loadStrategyChartsTab` (L11262)
- `loadStrategyStrategyChartTab` (L11281)
- `ensureChartDestroyed` (L11590)
- `renderLineChart` (L11597)
- `renderBarChart` (L11608)
- `renderPieChart` (L11619)

</details>

<details>
<summary><strong>策略管理与绩效 (49)</strong></summary>

- `handleStrategyPerformanceMessage` (L565)
- `handleStrategiesSnapshot` (L601)
- `handleSizingHistorySnapshot` (L617)
- `renderSizingHistory` (L656)
- `applySizingRuleStatusesFromHistory` (L726)
- `handleStrategyStatusMessage` (L745)
- `applyStrategyStatusPayload` (L749)
- `loadInternalStrategyOrders` (L7492)
- `loadStrategySettings` (L7542)
- `saveStrategySettings` (L7561)
- `loadStrategyRiskSettings` (L7741)
- `setStrategyDataPush` (L7888)
- `loadStrategyStatus` (L7976)
- `loadStrategies` (L8353)
- `resolveStrategySymbol` (L8370)
- `formatStrategyDataReception` (L8399)
- `displayStrategies` (L8520)
- `toggleStrategy` (L8641)
- `toggleStrategyDataReception` (L8656)
- `editStrategy` (L8791)
- `createEmptyStrategyDetailsState` (L9022)
- `getStrategyFromCache` (L9037)
- `resolveStrategySymbol` (L9075)
- `normalizeStrategyOrders` (L9374)
- `openStrategyDetails` (L10185)
- `initializeStrategyDetailsState` (L10199)
- `prepareStrategyDetailsModal` (L10204)
- `ensureStrategyDetailsTabListener` (L10580)
- `initializeStrategyDetailsContent` (L10606)
- `updateStrategyDetailsHeader` (L10626)
- `configureStrategyDetailsTabs` (L10651)
- `activateStrategyDetailsDefaultTab` (L10708)
- `requestActiveStrategyDetailsTabLoad` (L10723)
- `triggerStrategyDetailsTabLoad` (L10732)
- `fetchStrategyBasicInfo` (L10759)
- `fetchStrategyInternalInfo` (L10783)
- `loadStrategySummaryTab` (L10815)
- `loadStrategyRiskTab` (L11239)
- `loadStrategyInternalOrdersTab` (L11255)
- `loadStrategyExecutionsTab` (L11294)
- `loadStrategyPnlCalendarTab` (L11301)
- `loadStrategyApiExamplesTab` (L11307)
- `getStrategyExecutionsCacheKey` (L11328)
- `clearStrategyExecutionsCache` (L11332)
- `fetchStrategyExecutions` (L11346)
- `loadStrategyPerformance` (L11376)
- `loadStrategyExecutions` (L11452)
- `deleteStrategy` (L11646)
- `loadSizingHistory` (L12054)

</details>

<details>
<summary><strong>风险控制与规则 (19)</strong></summary>

- `handleRiskRulesMessage` (L593)
- `normalizeRuleStatus` (L701)
- `updateRiskPrices` (L3250)
- `openRiskRuleModal` (L3359)
- `editPositionRiskRule` (L3470)
- `showActiveRiskRule` (L7261)
- `hideActiveRiskRule` (L7290)
- `addRiskRule` (L7354)
- `loadRiskRules` (L7538)
- `ensureRiskSettingsControls` (L7619)
- `updateRiskSettingsStatus` (L7693)
- `renderRiskSettingsForm` (L7715)
- `handleRiskSettingsSave` (L7758)
- `handleRiskSettingsReset` (L7839)
- `removeRiskRule` (L8038)
- `toggleRiskRule` (L8040)
- `showRiskRuleOrders` (L11938)
- `loadGSRulesStatus` (L12356)
- `updateRuleStatus` (L12376)

</details>

<details>
<summary><strong>下单与执行流程 (20)</strong></summary>

- `closePosition` (L3658)
- `loadMonitorOrdersList` (L4436)
- `_loadMonitorOrdersList` (L4451)
- `initOrdersDebounce` (L7406)
- `loadOrders` (L7413)
- `placeOrder` (L7417)
- `cancelOrder` (L7421)
- `cancelAllOrders` (L7425)
- `syncLimitOrder` (L7429)
- `renderInternalOrders` (L7438)
- `editOrder` (L8305)
- `renderExecutionsTable` (L11481)
- `renderExecutionsPagination` (L11525)
- `exportExecutionsCsv` (L11561)
- `refreshPositionSuggestion` (L12082)
- `updatePositionSuggestionUI` (L12161)
- `clearPositionSuggestion` (L12198)
- `updatePositionValueField` (L12522)
- `startOrdersAutoRefresh` (L12640)
- `stopOrdersAutoRefresh` (L12655)

</details>

<details>
<summary><strong>通知与提醒 (3)</strong></summary>

- `scheduleTradingReminders` (L2308)
- `fireTradingReminder` (L2367)
- `getNextReminderDate` (L2408)

</details>

<details>
<summary><strong>界面状态与交互 (22)</strong></summary>

- `updatePanelLoadingIndicator` (L509)
- `setPanelLoading` (L523)
- `resetPanelLoading` (L543)
- `init` (L1783)
- `setupEventListeners` (L1837)
- `updateReadOnlyMode` (L3194)
- `updatePnLPreview` (L3399)
- `addPnLUpdateListeners` (L3434)
- `removePnLUpdateListeners` (L3461)
- `initDebounce` (L4429)
- `renderOhlcPriceStrip` (L6277)
- `updateExpectedPnL` (L7094)
- `updateSummaryPrimaryStatus` (L7672)
- `handleSummaryPushToggle` (L7875)
- `setupSymbolInput` (L8171)
- `initSymbolCombobox` (L11730)
- `initSidePanels` (L12211)
- `showConfigModal` (L12402)
- `saveConfigFromModal` (L12455)
- `resetConfigFromModal` (L12500)
- `showNotesTooltip` (L12596)
- `hideNotesTooltip` (L12632)

</details>

<details>
<summary><strong>数据拉取与缓存 (6)</strong></summary>

- `fetchWithTimeout` (L2453)
- `loadExternalScript` (L2785)
- `loadPortfolio` (L7522)
- `loadPnLCalendar` (L11469)
- `fetchCurrentPrice` (L11872)
- `loadConfigData` (L12561)

</details>

## 4. 通用工具/辅助方法 (39)

这些方法跨多个模块复用，建议在拆分过程中抽到共享 `utils` 包：

- `constructor` (L367)
- `fmt` (L1374)
- `formatRuntimeDuration` (L1378)
- `detectSecurityType` (L1733)
- `getContractMultiplier` (L1750)
- `bindEventListeners` (L1846)
- `isWorkingDay` (L2419)
- `isNasdaqHoliday` (L2424)
- `debounce` (L2443)
- `apiCall` (L2476)
- `registerCandlestickControllers` (L2726)
- `ensureCandlestickPlugin` (L2858)
- `waitForCandlestickPlugin` (L2888)
- `isLuxonReady` (L2989)
- `ensureLuxon` (L2993)
- `isDateAdapterReady` (L3075)
- `ensureDateAdapter` (L3094)
- `getTradingPrice` (L3240)
- `ensureSummaryControls` (L7585)
- `handleSummaryPrimarySymbolChange` (L7635)
- `handleSummaryDataFeedChange` (L7850)
- `formatNumber` (L8044)
- `escapeHtml` (L8052)
- `formatDuration` (L8064)
- `correctPriceStep` (L8089)
- `showAlert` (L8125)
- `setActiveDropdownItem` (L8250)
- `showHistoricalSymbols` (L8256)
- `getLocalDayKey` (L9064)
- `makeHistogram` (L11630)
- `clearData` (L11663)
- `getStatusColor` (L11694)
- `isSameDay` (L11719)
- `setActiveDropdownItem` (L11736)
- `showHistoricalSymbols` (L11742)
- `searchSymbols` (L11789)
- `getHistoricalSymbols` (L11852)
- `addToHistoricalSymbols` (L11857)
- `applySuggestedQuantity` (L12317)

## 5. 重排迁移计划（基于 2024 年现状）

结合现有 `monitoring/manager.js`、`strategies/manager.js`、`orders/manager.js` 等文件的内容，以下步骤按照“先迁走仍保留在 `app.js` 中的实现，再收缩入口类”的思路重新排序。每一步都列出了需要迁移的方法与目标模块，迁移完成后请删除 `app.js` 中的回退实现，仅保留必要的委托调用。

### 步骤 1：监控状态与时间选择逻辑
- **目标模块**：`static/js/monitoring/manager.js`
- **待迁移方法**：`initializeMonitoringStateFallback`、`createDefaultStrategyChartState`、`getStrategyChartState`、`getStrategyChartRefreshIntervalMs`、`getStrategyChartTimeUnit`、`refreshStrategyChartDuration`、以及 `bindMonitoringStateProxies` 中对状态字段的定义（改为在 `MonitoringManager` 内维护状态并只在构造函数中注册代理）。
- **验证要点**：监控弹窗与策略图表的区间/时长下拉框仍能正确渲染和联动，刷新页面后状态不会丢失。

### 步骤 2：监控趋势数据处理与渲染
- **目标模块**：`static/js/monitoring/manager.js`（必要时可拆出 `monitoring/trend.js`）
- **待迁移方法**：`setMonitorTrendHistory`、`resetMonitorTrendData`、`formatTrendPointForChart`、`getFormattedTrendChartData`、`updateMonitorSignalOverlayDatasets`、`renderMonitorSignalHistory`、`getTrendValues`、`setupTrendLegendAndAnnotations`、`refreshTrend`、`syncTrendReferenceLines`、`deriveTrendLevel`、`addMonitorTrendPoint`。
- **验证要点**：历史趋势图加载、信号标记、趋势等级提示等功能保持一致；`TrendSignalLabelPlugin` 在新模块中仍能工作。

### 步骤 3：监控实时订阅与模态交互
- **目标模块**：`static/js/monitoring/manager.js`
- **待迁移方法**：`syncCharts`、`normalizeMonitorTime`、`getMonitorDisplayTimes`、`getMonitorDisplayLabels`、`extractRangeFromMonitorChart`、`shouldResumeMonitorRealtime`、`subscribeMonitorRealtime`、`unsubscribeMonitorRealtime`、`updateMonitorRealtime`、`loadMonitorChart`、`displayChart`、`updateMonitorStopLossLine`、`updateMonitorTakeProfitLine`、`openMonitorModal`、`syncMonitorModalData`、`loadMonitorOrdersList`、`_loadMonitorOrdersList`、`exitMonitorFullscreen`。
- **验证要点**：实时订阅切换、全屏模式、订单列表刷新、止盈止损线拖拽/输入仍可正常使用；重新打开弹窗可恢复实时流。

### 步骤 4：策略详情面板与内部订单
- **目标模块**：新建 `static/js/strategies/details.js`（负责详情 UI）并扩展 `strategies/manager.js`（负责数据请求）
- **待迁移方法**：`openStrategyDetails`、`initializeStrategyDetailsState`、`prepareStrategyDetailsModal`、`ensureStrategyDetailsTabListener`、`initializeStrategyDetailsContent`、`activateStrategyDetailsDefaultTab`、`requestActiveStrategyDetailsTabLoad`、`triggerStrategyDetailsTabLoad`、`loadStrategySummaryTab`、`loadStrategyExecutionsTab`、`loadStrategyChartsTab`、`loadStrategyStrategyChartTab`、`loadStrategyInternalOrdersTab`、`loadStrategyPnLCalendarTab`、`loadStrategyApiExamplesTab`、`updateStrategyDetailsHeader`、`updateStrategySummaryHeader`、`renderStrategyExecutions`、`fetchStrategyExecutions`、`clearStrategyExecutionsCache`、`loadInternalStrategyOrders`。
- **验证要点**：策略详情弹窗可打开并在各标签间切换；执行记录、内部订单、PnL 日历和 API 示例加载逻辑保持一致；导出/重置等按钮仍可调用。

### 步骤 5：策略列表状态与绩效补强
- **目标模块**：`static/js/strategies/manager.js`
- **待迁移方法**：`handleStrategiesSnapshot`、`handleSizingHistorySnapshot`、`renderSizingHistory`、`applySizingRuleStatusesFromHistory`、`handleStrategyStatusMessage`、`applyStrategyStatusPayload`、`loadGSRulesStatus`、`updateRuleStatus`、以及与策略数据推送、重置、重命名相关的剩余 DOM 操作（如对列表开关、提示文本的直接操作）。
- **验证要点**：策略列表刷新、G/S 规则状态展示、策略推送开关、重命名/重置等交互均由 `StrategiesManager` 驱动，`TradingApp` 仅保留事件注册。

### 步骤 6：风险配置与通用工具收尾
- **目标模块**：`static/js/risk/manager.js` 与 `static/js/utils/format.js`（或现有 `utils/` 子模块）
- **待迁移方法**：`normalizeRuleStatus`、`showConfigModal`、`loadConfigData`、`saveConfigRules`、`resetConfigRules`、`fetchRiskRuleOrders` 以及 `formatNumber`、`isSameDay` 等通用工具函数；确保风险弹窗、配置保存与历史订单查看均通过 `RiskRulesManager` 和共享工具完成。
- **验证要点**：风险规则配置对话框、G/S 规则统计、风险订单查看、数字格式化等功能保持稳定；`app.js` 中不再保留通用工具的冗余实现。

每完成一个步骤，应手动回归对应模块的关键路径（监控图表、策略详情、风险配置等），并在浏览器控制台确认未出现新的未定义引用或事件绑定错误。

## 现状评估（2024-xx）

结合当前代码库，可以看到拆分工作已经打开战场，但大量回退实现仍让 `app.js` 维持在 5k+ 行体量，需要继续按步骤推进：

- **步骤 1 仍未完全收尾**：`MonitoringManager` 已负责策略图表默认状态、区间/时长计算等逻辑，但 `app.js` 仍初始化整套监控状态（`initializeMonitoringStateFallback`）并维护同名字段，导致状态分散在两个类中。【F:static/js/app.js†L503-L555】【F:static/js/monitoring/manager.js†L1560-L1638】
- **步骤 2 / 3 的核心逻辑尚未迁出**：监控实时渲染、订阅与模态交互在 `monitoring/manager.js` 中仍以内联 `legacy*` 方法形式保留了完整旧实现，新的类只是简单代理，未真正解耦。需要将这些函数拆分成实例方法并从 `app.js` 中删除重复代码。【F:static/js/monitoring/manager.js†L902-L1109】【F:static/js/monitoring/manager.js†L2578-L2610】
- **步骤 4 / 5 已取得明显进展**：策略详情和列表的大部分逻辑已经落地在 `StrategiesManager` 与 `StrategyDetailsController`，`app.js` 只负责把 WebSocket 消息与 UI 事件转给子模块，后续可在确认无遗留后删除回退分支。【F:static/js/strategies/manager.js†L20-L200】【F:static/js/strategies/details.js†L1-L200】【F:static/js/app.js†L329-L342】
- **步骤 6 仍需系统推进**：风险管理模块虽然有单独的 `RiskRulesManager`，但其渲染和交互仍频繁回调 `app` 上的工具方法（如 `formatNumber`），而这些通用工具依旧定义在 `app.js` 内，需要迁移到共享 `utils` 并由各模块引用，顺便完成风险弹窗相关逻辑的剥离。【F:static/js/risk/manager.js†L62-L160】【F:static/js/app.js†L3252-L3283】

若要把 `app.js` 压缩到 2000 行以下，必须按照计划逐步删除这些回退实现：优先让监控模块完全自洽（完成步骤 1~3），随后聚焦风险与通用工具（步骤 6），并在确认无外部依赖后移除 `app.js` 中的旧函数。每次迁移后都要同步清理 `legacy*` 代理与重复字段，才能真正显著降低文件体量。
