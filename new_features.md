### 总体技术方案概述

基于现有IBKR Trading Bot项目（使用Python 3.8+、ib_insync、FastAPI、pandas等），我们将逐步集成高级风控、人工与自动交易融合，以及通知扩展。方案优先使用内置或环境可用库（如numpy、scipy、pandas、torch for ML），避免新依赖安装（假设无互联网访问）。对于VaR/蒙特卡洛，使用numpy/scipy模拟；ATR手动计算以pandas实现；ML异常检测用PyTorch autoencoder；WebSocket用FastAPI内置；通知用smtplib（email）和urllib（Slack webhook）。

方案拆分成独立小步骤，按功能分组，每个步骤聚焦单一修改（如添加函数、更新API），便于逐步实现、测试和回滚。每个步骤包括：
- **目标**：修改内容。
- **文件/模块**：涉及位置。
- **技术细节**：实现要点。
- **测试建议**：验证方式。

总步骤约15-20个，预计分阶段实施：先风控基础（步骤1-8），再融合（9-13），最后通知（14-16）。使用异步队列（queue_manager.py）集成新任务，避免阻塞。

---

### 高级风控实现步骤

#### 步骤1: 集成实时VaR计算基础
- **目标**：添加VaR计算函数，使用历史模拟法（初始简单版，后扩展蒙特卡洛）。
- **文件/模块**：src/risk_manager.py（新增`calculate_var`函数）。
- **技术细节**：使用pandas加载历史回报率（从ib_insync获取数据），计算百分位数VaR（e.g., 95%置信）。输入：持仓数据、历史回报序列；输出：VaR值。参考numpy.percentile。
- **测试建议**：在test.py运行模拟数据，验证VaR输出（如对随机回报，VaR应负值表示损失）。

#### 步骤2: 添加蒙特卡洛模拟 for VaR和压力测试
- **目标**：扩展VaR到蒙特卡洛，模拟极端场景（如市场崩盘）。
- **文件/模块**：src/risk_manager.py（新增`monte_carlo_var`函数）。
- **技术细节**：用numpy生成随机路径（基于历史均值/方差，模拟1,000-10,000次），计算模拟回报排序后的VaR/ES。压力测试：添加参数如波动率倍增（e.g., x2 for crash）。集成到异步队列作为定时任务（apscheduler每5min运行）。
- **测试建议**：用code_execution工具模拟：生成随机回报，检查VaR是否接近理论值。

#### 步骤3: 实时监控VaR并触发警报
- **目标**：将VaR集成到风险监控循环。
- **文件/模块**：src/risk_manager.py（更新`monitor_risks`函数）；main.py（添加API `/api/var_status`）。
- **技术细节**：在现有检查间隔（.env CHECK_INTERVAL）调用monte_carlo_var，若VaR超阈值（e.g., 5%），触发通知/平仓。使用pandas处理持仓数据。
- **测试建议**：模拟高VaR场景，检查日志是否记录警报。

#### 步骤4: 实现动态阈值调整基于ATR
- **目标**：计算ATR并自动更新止损止盈。
- **文件/模块**：src/risk_manager.py（新增`calculate_atr`函数）。
- **技术细节**：手动计算True Range（max(high-low, high-prev_close, prev_close-low)），用pandas.rolling(mean=14)取平均。更新止损=当前价 - ATR*倍数（e.g., 2x）。从ib_insync获取OHLC数据。
- **测试建议**：输入样例OHLC数据，验证ATR值（e.g., 对波动数据ATR应较高）。

#### 步骤5: 集成动态阈值到风险规则
- **目标**：自动调整现有阈值（如LOSS_THRESHOLD_1）。
- **文件/模块**：src/risk_manager.py（更新`apply_risk_rules`）；database.py（存储动态阈值）。
- **技术细节**：每检查周期调用calculate_atr，更新SQLite风险表。API `/api/risk_rules` 返回动态值。
- **测试建议**：改变模拟数据波动，检查阈值是否自动变紧。

#### 步骤6: 添加组合风险管理（相关性、Beta、最大回撤）
- **目标**：计算持仓间相关性和Beta。
- **文件/模块**：src/risk_manager.py（新增`portfolio_risk_metrics`函数）。
- **技术细节**：用pandas.corr计算相关性；Beta=协方差(资产,市场)/市场方差（市场用SPY数据）。最大回撤：cumulative returns min。全局上限：若总风险>阈值，暂停新单。
- **测试建议**：用多资产模拟数据，验证相关性矩阵（diag=1）。

#### 步骤7: 集成全局风控到订单流程
- **目标**：在下单前检查组合风险。
- **文件/模块**：src/ibkr_client.py（更新`place_order`）；queue_manager.py（添加风险检查任务）。
- **技术细节**：队列前调用portfolio_risk_metrics，若超限拒绝订单。添加.env GLOBAL_RISK_LIMIT=0.1。
- **测试建议**：模拟高相关持仓，检查是否阻塞新单。

#### 步骤8: 实现ML异常检测（滑点过大）
- **目标**：用PyTorch训练简单autoencoder检测订单异常。
- **文件/模块**：src/risk_manager.py（新增`anomaly_detector`类）；data/下存储模型。
- **技术细节**：用torch构建autoencoder（输入：执行价-预期价差等特征），训练历史订单数据。推理：重建误差>阈值=异常，暂停交易。初始训练用pandas数据，保存torch模型。
- **测试建议**：生成正常/异常滑点数据，检查检测准确率>80%。

---

### 人工与自动交易融合实现步骤

#### 步骤9: 添加"待审批"状态到订单队列
- **目标**：允许自动订单进入待审状态。
- **文件/模块**：src/queue_manager.py（更新队列状态枚举：PENDING, PROCESSING, APPROVAL, COMPLETED, FAILED）；strategy_manager.py（标记自动订单）。
- **技术细节**：策略订单入队时设为APPROVAL。API `/api/order_queue_status` 显示状态。
- **测试建议**：提交自动订单，检查队列是否停在APPROVAL。

#### 步骤10: 实现Web界面审核/修改接口
- **目标**：人工通过Web审核订单。
- **文件/模块**：main.py（新增API `/api/approve_order/{task_id}` POST，参数：修改字段如价格）；static/app.js（添加审核按钮）。
- **技术细节**：FastAPI处理审批：更新队列任务，移到PROCESSING。拒绝则FAILED。
- **测试建议**：浏览器测试审核流程，检查订单执行。

#### 步骤11: 集成WebSocket for 实时干预
- **目标**：人工暂停/恢复策略或插入优先订单。
- **文件/模块**：main.py（扩展`/ws` WebSocket，处理消息如{"action": "pause_strategy", "name": "strat1"}）；strategy_manager.py（添加pause_flag）。
- **技术细节**：用FastAPI WebSocketManager广播/接收。插入订单：WebSocket发送高优先任务到队列头（queue.insert(0)）。
- **测试建议**：用WebSocket客户端测试暂停，检查策略是否停止。

#### 步骤12: 添加协同日志统一记录
- **目标**：记录人工/自动操作。
- **文件/模块**：src/database.py（扩展executions表添加`source`字段：AUTO/MANUAL）；loguru配置统一日志。
- **技术细节**：所有操作（下单、审批）插入日志记录。API `/api/logs` 过滤source。
- **测试建议**：执行混合操作，检查日志是否标记来源。

#### 步骤13: 实现影子模式
- **目标**：自动策略模拟执行，人工确认后真实下单。
- **文件/模块**：src/strategy_manager.py（新增`shadow_mode`旗标）；queue_manager.py（模拟任务不调用ib_insync）。
- **技术细节**：.env添加SHADOW_MODE=True时，策略生成订单但不提交，记录模拟P&L。WebSocket推送确认，确认后真实执行。
- **测试建议**：启用影子，检查模拟日志无真实订单。

---

### 通知扩展实现步骤

#### 步骤14: 添加Email通知支持
- **目标**：集成Email渠道。
- **文件/模块**：src/telegram_notifier.py（重命为notifier.py，新增`send_email`函数）。
- **技术细节**：用内置smtplib发送（.env添加EMAIL_HOST/USER/PASS）。分级通知：info/warn/error。
- **测试建议**：触发警报，检查邮箱接收。

#### 步骤15: 添加Slack通知支持
- **目标**：集成Slack webhook。
- **文件/模块**：notifier.py（新增`send_slack`函数）。
- **技术细节**：用urllib.request.post发送JSON到.env SLACK_WEBHOOK_URL。格式：{"text": "警报: VaR超限"}。
- **测试建议**：配置webhook，测试发送。

#### 步骤16: 实现自定义警报规则
- **目标**：用户定义通知规则。
- **文件/模块**：main.py（新增API `/api/notification_rules` POST/GET）；database.py（新表rules: event_type, channel, threshold）。
- **技术细节**：风险/订单事件匹配规则，调用相应send_函数。多渠道：规则指定email/slack/telegram。
- **测试建议**：添加规则（如VaR>5%发Slack），触发验证。