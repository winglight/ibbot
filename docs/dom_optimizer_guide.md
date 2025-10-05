# DOM 优化器退役说明

DOM 参数优化与自动化调度组件已从核心服务中移除。主应用仍然提供 DOM 订阅、历史数据与统计接口，但不再负责模型训练、阈值扫描或计划任务。

## 保留能力

- `GET /api/dom/data`：实时 DOM 快照。
- `GET /api/dom/history/{symbol}`：带分页的历史行情与信号记录。
- `GET /api/dom/stats/{symbol}`：聚合统计与指标查询。
- `POST /api/dom/start` / `POST /api/dom/stop`：控制 DOM 订阅生命周期。

以上接口可继续为策略、监控面板或外部分析脚本提供数据来源。

## 独立优化器的部署建议

若仍需基于 DOM 数据运行离线或批量优化，可将原优化器代码拆分为独立服务：

1. **运行方式**：单独部署在任务节点或数据科学环境，直接访问数据库/缓存或通过上述 REST 接口抓取样本。
2. **配置约定**：相关环境变量（如 `DOM_AUTOMATION_DEFAULT_*`）已从 `.env` 与 Compose 模板中移除，请在独立服务内自定义调度参数。
3. **阈值回写**：建议通过自定义脚本与配置文件管理阈值，再由人工或运维流程同步至生产环境。

## 文档链接更新

- DOM 数据面板使用方法：`static/index.html` 与 `src/dom_service.py` 内的代码注释。
- 策略如何消费 DOM 数据：`src/strategies/dom_strategy.py` 与 `src/strategies/readme.md`。

后续如需重新引入自动化调度，请在独立仓库维护计划任务与模型逻辑，再通过 API 或配置文件与主站点集成。
