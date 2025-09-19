# DOM 参数优化模块使用指南

本指南概述 DOM Optimizer 模块在最新迭代中的功能、使用流程以及相关测试方法。内容涵盖后端信号跟踪、自动优化编排器、前端可视化页面，以及常用的配置与调试命令，便于在集成阶段（Phase 4）完成联调并为交付阶段（Phase 5）做准备。

## 1. 模块概览

- **信号追踪与结果标注**：`DOMService` 在 `_on_depth_update` 中调用 `SignalOutcomeTracker`，为新触发的多/空信号分配 `signal_id`，并在配置的延迟窗口后依据价格变动自动写入 `outcome`（成功=1/失败=0）。
- **历史记录与缓存**：`DomCacheManager` 将每次快照与指标写入磁盘，`GET /api/dom/history` 支持时间范围、分页、是否包含快照等参数，供前端与优化流程读取。
- **自动化优化编排器**：`DOMOptimizationOrchestrator` 封装历史拉取、阈值网格搜索、阈值持久化以及计划调度，提供以下接口：
  - `POST /api/dom/optimize`：立即执行优化，可选择 `apply` 是否写回阈值、`obi_candidates`/`ofi_candidates` 搜索空间与 `min_signals` 下限。
  - `GET /api/dom/automation/status`：查询当前状态、最近执行结果、下一次计划等信息。
  - `POST /api/dom/automation/schedule`：开启/关闭定时任务并调整默认参数（回溯分钟数、信号下限、是否自动写回等）。
- **前端 DOM Optimizer 页面**（`static/index.html` + `static/js/dom_optimizer.js`）：
  - 配置面板：选择交易标的、回溯窗口、候选阈值，并展示总信号、分方向计数、成功率、待评估数。
  - 性能概览：显示总体精度、平均 Outcome 写回延迟、最新信号概况。
  - 趋势与指标图表：基于 Chart.js 渲染趋势分数 vs 价格（双 Y 轴）以及 OBI/OFI + 阈值/信号标记，可按需刷新或开启 30s 轮询。
  - 信号列表：支持分页、Outcome 过滤、时间区间查询，可在优化前后对比结果。
  - 手动优化控制：一键调用 `POST /api/dom/optimize`，展示返回的精度、阈值与执行耗时。

## 2. 运行准备

1. **环境激活**：
   ```bash
   conda activate py311
   ```

2. **可选环境变量**：
   - `DOM_SIGNAL_OUTCOME_DELAY_SECONDS`：Outcome 判定延迟（秒）。
   - `DOM_SIGNAL_OUTCOME_THRESHOLD`：多/空信号成功所需的相对价格变动阈值。
   - `DOM_CACHE_RETENTION_DAYS`：历史缓存保留时间。
   - `DOM_AUTOMATION_DEFAULT_*`：编排器默认计划（间隔分钟数、回溯长度、最小信号、是否自动写回等）。

3. **后端进程**：确保主应用（FastAPI + 背景任务）可访问本地存储与数据库。若只在本地重放数据，可使用历史快照喂给 `DOMService._on_depth_update`。

## 3. 常见使用流程

1. **采集与验证**：
   - 启动 DOM 服务，回放或订阅真实行情，等待 Outcome 标注完成。
   - 前端（DOM Optimizer 页面）刷新统计，确认长/短信号计数与成功率正常。
   - 使用趋势/指标图表查看阈值与信号标记是否符合预期。

2. **历史分析**：
   - 在信号列表中设置时间区间/Outcome 过滤，导出需要观察的样本。
   - 检查性能概览中的平均延迟与最新信号信息，判断 Outcome 配置是否合理。

3. **手动优化**：
   - 输入自定义的 OBI/OFI 候选值、最小信号数等参数，点击 “立即优化”。
   - 观察返回的精度、正/负信号数和性能耗时，确认 `updated_thresholds` 是否符合预期。
   - 图表与信号列表会随最新历史记录刷新，方便对比优化前后的阈值位置。

4. **自动化计划**（可选）：
   - 通过 `POST /api/dom/automation/schedule` 开启周期性优化，并在状态卡片中查看下一次执行时间。
   - 状态接口会记录最近结果、耗时和是否已应用阈值，可在前端或后端监控系统中展示。

## 4. 测试与验证

### 4.1 后端集成测试

`test/test_dom_phase4_integration.py` 模拟深度快照 → 信号标注 → Outcome 标记 → 自动优化 → 阈值写回的整条链路。运行前建议关闭自动拉起服务的 fixture：

```bash
IBBOT_SKIP_SERVER=1 pytest test/test_dom_phase4_integration.py -k full_loop -vv
```

若依赖真实服务，可省略 `IBBOT_SKIP_SERVER=1`，但需确保 uvicorn 可绑定到 8000 端口且具备数据库权限。

### 4.2 前端手工验证

1. 打开 DOM Optimizer 页面，确保统计面板、性能概览、图表、信号分页均能加载历史数据。
2. 手动触发优化后，确认结果区域与图表阈值同步更新。
3. 开启 “直播” 切换（30s 轮询）以观察实时指标刷新情况，验证可视化性能在高频数据下是否稳定。

## 5. 变更摘要

- 新增 `test/test_dom_phase4_integration.py` 用于 Phase 4 全链路验证。
- 前端优化页面现已具备统计摘要、双图表与实时轮询能力，并串起手动优化流程。
- 相关文档统一放置于 `docs/`，便于后续交付与知识传递。

如需扩展自动化计划、集成 WebSocket 更新或添加更多图表，可在 Phase 4 调优阶段继续迭代。
