# 5 分钟 ATR 跟踪止盈止损方案设计

## 1. 目标
- 在保留现有 DOM 策略/风控框架的前提下，实现基于「5 分钟 ATR(14) + 现价」的动态止损、止盈价位。
- 支持对同一标的的多笔持仓共享一套跟踪规则，自动将最新价差同步到 `risk_manager` 中的 `RiskRule` 并持久化。
- 通过订阅式行情流快速获取最新的价格、成交量序列，避免频繁全量拉取历史数据导致的延迟和配额消耗。

## 2. 总体架构
```
┌──────────────────────────────────────────────┐
│               ATRTrailingService             │
│                                              │
│  ┌───────────────┐   ┌────────────────────┐  │
│  │PriceVolumeFeed│ → │ATRCalculator (5m/14)│ → 更新止损/止盈差值
│  └───────────────┘   └────────────────────┘  │
│             │                     │           │
│             ▼                     ▼           │
│      BarCache(5m)         RiskRuleUpdater     │
└─────────────┬─────────────────────┬───────────┘
              │                     │
              ▼                     ▼
        IBKR 实时报价          RiskManager.update_risk_rule
```

- **PriceVolumeFeed**：通过 IBKR 的 `reqRealTimeBars` 接口按固定间隔（例如 5 秒）订阅某个合约的成交价与成交量。服务端维护订阅任务和回调，将实时条拼接成 5 分钟 K 线。
- **BarCache**：每个标的维持一个循环队列（容量 ≥ 14）用于缓存最近的 5 分钟 OHLCV 数据，同时保存最新一根尚未结束的 5 分钟 bar 的增量状态。
- **ATRCalculator**：每当 bar 完成或价格发生显著跳变时触发计算，使用 Welles Wilder 公式对最近 14 根 5 分钟 K 线计算 ATR。
- **RiskRuleUpdater**：把 `现价 ± ATR * 系数` 得到的止损/止盈价位写回 `risk_manager.update_risk_rule`，并持久化到数据库。更新节流确保最小间隔（例如 30 秒）以避免频繁写入。

## 3. 关键流程设计
### 3.1 行情订阅 Price/Volume
1. 新增 `src/market_data_stream.py`，封装:
   - `class PriceVolumeStream`：按符号管理 IBKR 订阅 ID，调用 `ib.reqRealTimeBars(contract, whatToShow='TRADES', useRTH=False)`。
   - 使用 `ib_insync` 的事件回调 `ib.pendingTickersEvent` 或 `ib.reqRealTimeBars` 返回的 async queue 获取 `RealTimeBar`。
   - 允许调用方指定下采样间隔 `interval_seconds`。若 IBKR 只允许 5 秒数据，则通过内部节流器（例如只保留每 `interval_seconds/5` 个 bar）。

2. 对于每个 `symbol`，维护:
   - `task`: 后台 `asyncio.Task` 负责监听 bar 数据。
   - `listeners`: 回调列表，ATR 服务作为订阅者。
   - 当无活跃订阅者时，自动取消任务并向 IBKR 发送 `ib.cancelRealTimeBars(req_id)`。

3. Price/Volume 序列生成:
   - 每个 `RealTimeBar` 结构含 `open, high, low, close, volume`，`barSize` 固定为 5 秒。
   - `PriceVolumeStream` 按时间戳对齐并推送给监听者，保证无重复、无乱序。

### 3.2 5 分钟 Bar 聚合
1. 在 ATR 服务内部为每个标的维护 `FiveMinuteBarBuilder`：
   ```python
   class FiveMinuteBarBuilder:
       def __init__(self, window=timedelta(minutes=5)):
           self.window = window
           self.active_bar = None  # {start, end, open, high, low, close, volume}
           self.completed = deque(maxlen=lookback)
   ```
2. 逻辑:
   - 第一个 5 秒 bar 到来时创建 `active_bar`，`start = timestamp.floor(5 minutes)`。
   - 连续合并 price/volume，更新 `high/low/close/volume`。
   - 当下一条 bar 的时间 >= `active_bar.start + 5 min`：
     - 将 `active_bar` 推入 `completed`。
     - 创建新 bar（处理跨窗口遗留）。
   - 对于缺失的 bar，回填为上一个 close，volume = 0（避免 ATR 断层）。

### 3.3 ATR 计算
1. 使用 Wilder 平滑：
   ```python
   def compute_atr(bars: Sequence[Bar], length: int = 14):
       true_ranges = []
       for i, bar in enumerate(bars):
           if i == 0:
               tr = bar.high - bar.low
           else:
               prev_close = bars[i-1].close
               tr = max(bar.high - bar.low,
                        abs(bar.high - prev_close),
                        abs(bar.low - prev_close))
           true_ranges.append(tr)
       atr = true_ranges[0]
       for tr in true_ranges[1:]:
           atr = (atr * (length - 1) + tr) / length
       return atr
   ```
2. 触发条件：
   - 新的 5 分钟 bar 完成。
   - 或当前价距上次 ATR 更新超过 `delta_threshold`（例如 0.25 ATR）。
   - 通过节流器确保距上次 `risk_rule` 写入≥30 秒。

### 3.4 止损/止盈同步
1. 获取当前持仓方向、均价：
   - 通过 `risk_manager._monitor_positions` 已查询的 `positions` 缓存。
   - ATR 服务暴露 `refresh_for_symbol(symbol, position)` 接口，由 `risk_manager` 在每次监控循环中调用。
2. 计算目标价：
   - 若多头：`stop_loss = last_price - atr_multiplier * atr`，`take_profit = last_price + tp_multiplier * atr`。
   - 若空头相反。
   - `atr_multiplier`、`tp_multiplier` 可以在 `.env` 或策略参数中配置（默认 1.0/2.0）。
3. 调用 `risk_manager.update_risk_rule(symbol, stop_loss_price=..., take_profit_price=...)`。
4. `RiskRuleUpdater` 记录更新时间、触发原因，写入日志，并通过 `notification_manager` 推送变动。

### 3.5 数据存储与恢复
- 每次 bar 完成后将 `completed` 队列快照写入 `data/atr_cache/{symbol}.json`，内容包括最后更新时间戳、ATR 值。
- 服务启动时尝试读取缓存，若缺失则回落到一次性调用 `get_historical_data(symbol, duration="2 D", bar_size="5 mins")` 以填充初始 14 根 bar。

## 4. 配置项
| 配置键 | 说明 | 默认值 |
| --- | --- | --- |
| `ATR_LOOKBACK` | ATR 计算长度 | 14 |
| `ATR_BAR_INTERVAL` | 聚合时间窗口（分钟） | 5 |
| `ATR_STREAM_INTERVAL` | 实时订阅下采样秒数 | 5 |
| `ATR_UPDATE_THROTTLE` | 止损/止盈写入最小间隔（秒） | 30 |
| `ATR_MULTIPLIER_SL` | 止损 ATR 倍数 | 1.0 |
| `ATR_MULTIPLIER_TP` | 止盈 ATR 倍数 | 2.0 |
| `ATR_DELTA_THRESHOLD` | 距离上次价差的触发阈值（ATR 倍数） | 0.25 |

配置通过 `.env` 或数据库策略表加载，允许按 symbol 覆写。

## 5. 与现有系统的集成
1. **依赖注入**：在 `src/dom_dependencies.py` 中创建 `atr_trailing_service` 单例，将 `core.connection_manager`、`risk_manager`、`notification_manager` 注入。
2. **生命周期管理**：
   - 在 `dom_service.start_strategy` 时根据策略配置启用或禁用 ATR 跟踪。
   - 风控循环中，如果 `RiskRule` 标记 `auto_trailing=True`，则调用 `atr_service.ensure_subscription(symbol)`。
3. **API 支持**：
   - 在 `routes/risk.py` 中增加端点允许前端配置 ATR 参数、查看当前 ATR 值和最近一次更新时间。

## 6. 实施步骤
1. **数据流实现**
   - 新增 `PriceVolumeStream`，编写单元测试模拟 `RealTimeBar` 推送。
   - 验证 5 分钟聚合准确性（处理缺口、夜盘）。
2. **ATR 服务与风控衔接**
   - 开发 `ATRTrailingService`，包含订阅管理、ATR 计算、更新节流。
   - 在 `risk_manager` 中挂钩，添加对 `auto_trailing` 标记的检查。
3. **配置与存储**
   - 扩展 `DatabaseRiskRule` 增加 `auto_trailing`, `atr_params` 字段。
   - 更新数据库迁移/初始化脚本。
4. **监控与告警**
   - 通过 `notification_manager` 记录 ATR 更新和异常。
   - 编写日志结构，便于排查。
5. **回测/仿真验证**
   - 利用历史数据回放验证 ATR 更新节奏。
   - 在 paper 账户进行端到端测试，确认止损/止盈更新正确落地。

## 7. 风险与缓解
- **IBKR 订阅限制**：实时条需要市场数据订阅权限，需提前校验 `market_data_type`；若失败，回退到每 1 分钟调用一次 `get_historical_data`。
- **网络或 API 中断**：若实时订阅断开，触发重连逻辑并发送告警，同时保持最近 ATR 值不变，避免重复下调止损。
- **计算延迟**：ATR 更新与风险监控放在同一事件循环，需确保协程使用 `asyncio.create_task`，避免阻塞 DOM 策略主线程。
- **多仓位冲突**：若同一 symbol 有不同策略，可在 `RiskRule` 中记录 `source_strategy` 并由 ATR 服务按策略维度分别维护价位。

## 8. 后续扩展
- 支持动态调整 ATR lookback，根据波动率状态切换窗口。
- 引入 `trading_hours` 过滤，避免在休市时 ATR 被静态价格稀释。
- 将 ATR、止损轨迹写入 Prometheus/InfluxDB 以便图表化监控。

