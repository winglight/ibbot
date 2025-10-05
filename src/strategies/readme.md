# 策略开发与运行指南

本目录提供策略基类、运行器以及示例策略，帮助开发者在 IBBot 上快速构建量化策略。核心文件：

- `strategy_template.py`：定义 `BaseStrategy` 抽象基类与示例策略。封装绩效统计、日志、异常处理等通用能力。
- `strategy_runner.py`：命令行运行器，从主程序拉取行情、执行策略、将信号转换为 `/api/strategy` 外部订单。
- `dom_strategy.py`：平台内置 DOM 策略的实现示例，可参考其依赖注入方式。

## 1. 运行依赖

在使用 Runner 之前，请确保主服务已启动：

```bash
uvicorn main:app --reload
```

默认监听 `http://localhost:8000`，并可通过 `/api/login` 获取 JWT。若开启了策略访问令牌，可在“Strategy Settings”面板生成。

## 2. Runner 快速开始

```bash
# 推荐的模块运行方式
python -m src.strategies.strategy_runner \
  --file "Buy down and Sell open.py" \
  --symbols MNQ \
  --api http://localhost:8000 \
  --interval 2.0
```

常用参数：

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--file` / `RUNNER_STRATEGY_FILE` | 策略脚本，位于 `src/strategies` 目录 | 必填 |
| `--symbols` / `RUNNER_SYMBOLS` | 逗号分隔的交易标的 | 必填 |
| `--api` / `RUNNER_API_BASE` | 主服务地址 | `http://localhost:8000` |
| `--interval` / `RUNNER_INTERVAL` | 行情轮询间隔（秒） | `2.0` |
| `--strategy-name` | 在主服务中登记的策略名称 | 自动取脚本内 `strategy.name` |
| `--strategy-token` | 外部策略鉴权 token | 可选 |
| `--jwt` | 若主服务开启 JWT，传入 Bearer Token | 可选 |

## 3. 自定义策略流程

1. **复制模板**：以 `strategy_template.py` 为蓝本创建新文件，例如 `my_strategy.py`。
2. **继承 `BaseStrategy`** 并实现以下接口：
   - `initialize(self)`：初始化参数或资源。
   - `generate_signal(self, symbol, data)`：返回订单指令（如 `{"action": "BUY", "quantity": 1}`）。
   - `on_order_filled` / `on_error`：可选回调用于记录成交或异常。
3. **导出策略实例**：在脚本末尾提供 `strategy = MyStrategy()` 或 `StrategyClass`。
4. **运行 Runner** 指向该脚本，验证信号是否正确入队并在 Web UI 的“External Strategy”面板查看结果。

## 4. 与主服务的接口契约

Runner 调用的核心接口：

- `GET /api/realtime_data/{symbol}`：拉取最新行情，支持 `exchange`、`sec_type` 等参数。
- `POST /api/strategy`：提交外部策略订单。请求体遵循 `ExternalStrategyOrderRequest` 模型，可包含 `jwt`/`X-Strategy-Token` 头部。
- `GET /api/strategies/{name}/status`：可在调试时检查策略执行状态与队列情况。

所有外部策略请求都会进入 `queue_manager`，并由风险控制、订单路由器统一处理，失败会自动重试（最多 3 次）。

## 5. 联调建议

- 开发阶段可使用 TWS/IB Gateway 模拟账户或自建脚本回放 DOM 数据（可从 `/api/dom/history/{symbol}` 导出样本）。
- 使用 Web UI 的日志与队列面板观察策略执行情况，必要时可以在 `data/queue_tasks.json` 中查看持久化任务。
- 若策略频繁触发风控，可在 `/api/risk_rules` 或 `/api/strategy/{name}/risk-settings` 调整阈值。

## 6. Docker / 远程环境

在容器或远程 VPS 中运行 Runner 时，请确保：

- 目标主服务监听 `0.0.0.0` 并允许来自 Runner 的访问。
- `docker/local.compose.yml` 已挂载 `./strategy` 目录，可直接在宿主机编辑策略脚本。
- 若 Runner 位于容器外部，API 地址需指向宿主机可达的域名/IP。

## 7. 常见问题

| 问题 | 解决方案 |
| --- | --- |
| `attempted relative import with no known parent package` | 使用 `python -m src.strategies.strategy_runner` 运行，或在脚本内加入 `sys.path` 兜底。 |
| Runner 请求 401 | 确认 `/api/login` 返回的 JWT 已放入 `--jwt`，或在头部添加 `Authorization: Bearer ...`。 |
| 外部策略排队不执行 | 检查 `/api/queue/status` 是否存在 backlog，必要时调用 `/api/queue/tasks` 查看详情。 |
| 需要回测/回放 | 可利用 `src/dom_service.py` 的缓存能力或自定义数据源，当前版本未内置全量回测框架。 |

更多策略风险配置可参考 `src/strategy_risk.py` 与 `/api/strategies/{name}/risk-settings` 接口。
