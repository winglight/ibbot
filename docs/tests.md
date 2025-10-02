# 测试清单与执行命令

下表汇总不同层级的自动化/半自动化测试与推荐执行命令，覆盖阶段 1-4 的交付要求。所有命令默认在仓库根目录执行。

| 测试类型 | 覆盖阶段 | 说明 | 执行命令 |
| --- | --- | --- | --- |
| 单元测试 | 阶段 1-2 | 覆盖配置、Redis/Influx 封装、服务内部逻辑 | `pytest tests/config tests/foundation -q` |
| 集成测试 | 阶段 2-3 | FastAPI 接口、服务编排、队列/流交互 | `pytest tests/services tests/ui -q` |
| 端到端 (E2E) | 阶段 3-4 | 模拟下单/风控/策略链路，依赖 Redis/模拟 IB | `pytest tests/e2e -q` |
| 性能测试 | 阶段 4 | `tests/perf` 使用内置基准校验吞吐/延迟阈值 | `pytest tests/perf -k benchmark` |
| 故障注入 | 阶段 4 | `tests/failover` 注入 Redis/Influx/IB 断连并验证恢复 | `pytest tests/failover` |

以下章节保留原有的手工测试用例，建议在自动化测试通过后再执行，以覆盖真实 IBKR 数据场景。

# 手工测试用例（IB 实盘/纸交易数据下的模拟验证）

本测试集用于人工在真实 IBKR 数据环境下进行端到端验证，覆盖下单（不同来源、不同资产、买多/卖空、平仓、反转）、风险规则（不同触发条件）、策略（不同类型），并聚焦异常与边界条件处理。每个用例提供操作步骤、预期结果与多通道验证方法（界面、日志、数据库、环境变量）。

## 一、准备与通用说明
- 环境与账户
  - 建议使用 Paper 账户；确保 IB Gateway/TWS 已登录并启用 API（见 README）
  - .env 已配置 ADMIN_USERNAME/ADMIN_PASSWORD、IBKR_HOST/PORT、JWT_SECRET 等
- 启动与访问
  - 启动主服务：python main.py（或 docker compose up -d）
  - Web UI：默认 http://localhost:8000
  - 登录后再访问受保护面板（如日志、策略）
- 排错与验证通道
  - 界面：Web UI 各面板（交易/持仓/风险/策略/日志）
  - 日志：logs/trading_bot.log（前端“日志”面板支持按日期/模块/等级筛选；等级为包容式过滤）
  - 数据库：data/trading_bot.db（表：orders、risk_rules、strategies、strategy_executions）。可使用 sqlite3 或 DB Browser
  - 环境变量：核对 .env 与容器 env（docker compose config 或 docker inspect）

## 二、下单相关场景

说明：以下用例同时覆盖不同来源（Web UI、外部策略 API、内部策略触发）、不同资产（STK/FUT，若无期货权限可仅测 STK）、方向（BUY/SELL）、动作（开仓/平仓/反转）。

1) Web UI 下单-市价开多（STK）
- 前置：已登录 Web UI；已连接 IB；账户资金充足；选择如 AAPL
- 步骤：
  1. 在交易面板选择 symbol=AAPL、数量=10、方向=BUY、订单类型=MKT
  2. 点击 Place Order
- 预期：
  - 界面弹出成功提示并显示订单ID；订单状态从 Submitted -> Filled（或 Pending -> Filled）
- 验证：
  - 界面：订单列表出现该笔，状态与成交价格显示
  - 日志：订单微服务与 ibkr_client 记录下单与成交；等级≥info
  - DB：orders 表新增记录（symbol=AAPL, action=BUY, order_type=MKT, status=Filled 或 Submitted）

2) Web UI 下单-限价开空（STK）
- 前置：账户允许卖空且有可借券（如不支持卖空，可改为 BUY 限价）
- 步骤：symbol=MSFT, 数量=10, 方向=SELL, 订单类型=LMT, price=市价-0.5
- 预期：
  - 订单成功提交并在达到限价时成交；若未成交，状态保持 Submitted
- 验证：
  - 界面订单卡片状态更新；必要时取消订单验证取消流程
  - 日志包含限价参数与交易所路由
  - DB orders 行包含 price 字段与状态更新轨迹（created_at/updated_at/filled）

3) 平仓操作（从有持仓到零仓位）
- 前置：已有 AAPL 多头 10 股
- 步骤：下单 SELL 10 MKT
- 预期：
  - 成交后持仓归零
- 验证：
  - 界面持仓面板该 symbol 清零
  - 日志出现持仓变动与订单成交记录
  - DB orders 最新记录 action=SELL、对应 strategy_executions 若由策略触发

4) 反转操作（多转空或空转多）
- 前置：AAPL 多头 10 股
- 步骤：下单 SELL 20 MKT（先平10再开空10）
- 预期：
  - 成交后仓位=-10
- 验证：
  - 界面持仓为 -10；订单面板有两条成交（或一条总量）
  - 日志记录反转逻辑说明（由订单服务或 ibkr_client 拼接）
  - DB orders 有本次 SELL 20 记录

5) 批量取消与单笔取消
- 前置：挂多笔 LMT 订单
- 步骤：使用“取消全部”与对单笔点“取消”
- 预期：订单状态转为 Cancelled
- 验证：
  - 界面状态更新；取消按钮置灰
  - 日志含 cancel 请求与回报；WARNING 级别若遇状态回退保护
  - DB orders updated_at 变化，status=Cancelled

6) 资金不足下单
- 前置：设置数量为极大值触发不足
- 步骤：BUY MKT 提交
- 预期：
  - 返回资金不足错误；不会产生有效委托
- 验证：
  - 界面 toast/错误提示
  - 日志 risk_manager 或 ibkr_client 记录错误，等级≥warning
  - DB 无新有效订单或记录为失败状态（若实现）

7) 订单被拒绝/网络异常/超时
- 步骤：
  - 暂时断开网络或关闭 IB Gateway 再下单
- 预期：
  - 接口报错或重试；UI 显示失败；队列可能进入 failed
- 验证：
  - 日志 error/critical 级别，包含异常堆栈或错误码
  - DB 若产生任务记录，状态=failed 且带 error_message（如有队列任务）

8) 部分成交与取消剩余
- 前置：提交较大 LMT，在部分成交后取消剩余
- 预期：
  - 成交数量<下单数量；取消后状态 Closed/Cancelled
- 验证：
  - 界面成交均价与数量反映部分填充
  - 日志包含多次回报
  - DB orders filled_quantity/avg_fill_price（如有字段）或多条更新

## 三、风险规则相关场景

1) 固定价格止损（多头）
- 前置：AAPL 多头 10 股；新增风险规则 stop_loss_price=入场价-1.0
- 步骤：等待价格跌破或在测试环境中构造行情触发
- 预期：
  - 系统自动发送 SELL 10 平仓
- 验证：
  - 界面：风险规则面板显示已触发/订单面板出现平仓
  - 日志：risk_manager 记录触发与下单信息（≥info）
  - DB：risk_rules 该 symbol 存在；orders 出现自动单，order_source=strategy/risk（实现而定）

2) 固定价格止盈（多头）
- 前置：take_profit_price=入场价+1.0
- 预期：
  - 价格触发后自动平仓
- 验证方法同上

3) 百分比止损（多头或空头）
- 前置：max_loss_percent=0.05
- 预期：
  - 浮动亏损≥5%触发
- 验证：
  - 日志包含当前盈亏/阈值对比
  - DB 无需新增 risk_rules 行变动，但 orders 生成平仓

4) 多级阈值（3%预警、8%强制平仓）
- 前置：配置 LOSS_THRESHOLD_1=0.03, LOSS_THRESHOLD_2=0.08
- 步骤：等待价格分别触发两个阈值
- 预期：
  - 达到3%：仅告警，不平仓；达到8%：自动平仓
- 验证：
  - UI 通知/Telegram（如配置）
  - 日志：3%为 WARNING，8%触发下单为 INFO/WARNING

5) 规则开关与修改
- 步骤：
  - 禁用规则 -> 价格触发不应下单
  - 修改规则参数 -> 新参数应生效
- 验证：
  - UI 规则状态与更新时间
  - 日志与 DB 记录一致

## 四、策略相关场景

1) 外部策略 API 下单成功
- 前置：创建策略并生成令牌；记录 token
- 步骤：
  - 调用 POST /api/strategy，携带 token 与 ExternalStrategyOrderRequest（symbol/action/quantity/order_type/price 可选）
- 预期：返回 task_id，任务进入队列并最终生成订单
- 验证：
  - UI 队列状态 pending->processing->completed
  - 日志 strategy_manager/queue_manager 记录状态迁移
  - DB strategy_executions 新增执行记录；orders 有对应订单，order_source=strategy

2) 外部策略认证失败
- 步骤：使用无效或过期 token 调用 /api/strategy
- 预期：返回 401 Unauthorized
- 验证：
  - 日志 security/auth 相关模块记录失败原因（若有）

3) 策略不存在或被禁用
- 步骤：使用存在但禁用的策略名称提交
- 预期：返回策略不可用错误
- 验证：
  - 日志与 UI 错误提示一致

4) 内部策略触发下单
- 前置：启用某内置策略（例如基于均线/突破的样例）
- 步骤：满足触发条件或在测试环境中构造行情
- 预期：自动生成订单
- 验证：同“外部策略 API 下单成功”

## 五、异常与边界条件

1) 网络异常（短暂断连）
- 步骤：断开网络 30-60 秒后恢复
- 预期：连接管理自动重连；中断期间下单失败会重试或失败落库
- 验证：
  - 日志 connection_manager 记录断连/重连；WARNING/ERROR 级别

2) IB 限流/拒单
- 步骤：短时间重复提交多笔订单
- 预期：部分被拒；系统记录错误并不中断
- 验证：日志含错误码说明；UI/DB 可见失败

3) 队列重试
- 前置：让 /api/strategy 任务在 IB 未连接时触发
- 预期：任务自动重试最多3次后标记永久失败
- 验证：
  - UI 队列页显示 retry_count 增加，最终 failed
  - 日志 queue_manager 记录重试次数与最终失败

4) 权限与认证
- 步骤：未登录访问日志面板或调用受保护 API
- 预期：返回 Authentication required / 401
- 验证：登录后重试即正常

## 六、清理与回滚
- 取消所有未成交订单并确认状态为 Cancelled
- 如在 Paper 环境产生持仓，手动平仓并确认持仓归零
- 可备份 data/trading_bot.db 与 logs 以存档

---
提示：前端“日志等级”筛选为包容式（warning=warning+error+critical），可用于验证异常阶段是否有期望的 WARNING/ERROR/CRITICAL 记录。