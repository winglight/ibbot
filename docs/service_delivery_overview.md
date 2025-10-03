# 服务阶段交付与运维概览

本文档整理 IBBot 在分阶段实施过程中的交付物，便于研发、运维与测试团队快速确认当前版本的能力范围，并与部署/监控手册联动。内容依据最新实现（`src/ibbot/*` 与 `services/*` 目录）整理。

## 阶段能力清单

| 阶段 | 完成时间点 | 交付内容 | 验收与佐证 |
| --- | --- | --- | --- |
| 阶段 0：准备 | 初始化仓库后 | `.env.example` 模板、`data/logs` 目录、README 基础结构 | `README.md` 的“快速开始”、`data/.gitkeep` 保留目录结构 |
| 阶段 1：基础能力库 | 基础依赖可被脚本消费 | 统一配置加载、Redis/Influx 封装、日志管理、诊断脚本 | `src/ibbot/{config,redis,influx,logging}`、`scripts/diagnose_foundation.py` 可独立运行 |
| 阶段 2：独立服务 | 微服务容器可启动并协同 | 指令执行（`ib_command`）、行情采集（`ib_feed`）、行情聚合（`market_data`）、风险引擎、策略调度、通知下发、队列监控、优化器 | 对应 `services/*` 目录均包含 `main.py` 与运行时逻辑；`docker/local.compose.yml` 暴露各容器 |
| 阶段 3：主程序改造 | 主应用对接新增服务 | FastAPI 路由整合、监控仪表盘、Influx 查询接口、UI 更新 | `main.py`、`src/routes/monitoring.py`、`static/index.html` 中的监控面板 |
| 阶段 4：联调与回归 | 交付前验收 | Docker Compose 全量联调、端到端/性能/故障注入测试覆盖、CI 冒烟 | `tests/{e2e,perf,failover}`、`scripts/perf/run_benchmark.py`、`.github/workflows/ci.yml`（如存在） |

## 服务矩阵（阶段 ≥2）

| 服务 | 入口 | 核心功能 | 依赖 | 可观测性/指标 |
| --- | --- | --- | --- | --- |
| IB Command (`ib_command`) | `services/ib_command/main.py` | 消费 Redis 队列、调用 `IBKRClient` 执行指令、写入结果流 | Redis 队列（`DEFAULT_COMMAND_QUEUE`）、可选 IBKR 客户端 | Redis 健康键 `hash:service:health:ib-command`、结果流 `stream:ib:results` |
| IB Feed (`ib_feed`) | `services/ib_feed/main.py` | 订阅 IB 行情、发布价格/DOM 快照、写入 Influx | IBKR 客户端、Redis、Influx（`market_price`/`market_dom` measurement） | Pub/Sub 频道 `pubsub:ib:price:*`、`pubsub:ib:dom:*`，Influx `market_price`/`market_dom` 查询 |
| Market Data (`market_data`) | `services/market_data/main.py` | 汇总行情快照、提供 REST/WebSocket、写入服务指标 | Redis Pub/Sub、Influx | WebSocket `/ws/market`、Redis Stream `stream:market-service`、Influx `service_metrics`（metric=`price-throughput`、`price-latency` 等） |
| Queue Monitor (`queue_monitor`) | `services/queue_monitor/main.py` | 统计队列/Stream 长度、滞留时间，广播指标 | Redis、Influx | Redis 哈希 `hash:service:queue-monitor`、Influx `service_metrics`（metric=`queue-*`） |
| Risk Service (`risk`) | `services/risk/main.py` & `runtime.py` | 消费行情/执行结果、运行 `RiskEngine`、触发平仓、写入事件 | Redis Streams（`stream:market-service`、`stream:ib:results`）、命令队列、Influx | Influx `logs` measurement（tag `service=risk-service`）、可选本地日志目录（`log_dir`） |
| Strategy Service (`strategy`) | `services/strategy/main.py` | 读取策略任务、生成 IB 指令、回写事件流 | Redis 列表/流、命令队列 | Redis 队列 `list:queue:strategy`、事件流 `stream:strategy:events` |
| Notification Service (`notification`) | `services/notification/main.py` | 消费事件、调用浏览器/Telegram 客户端发送通知 | Redis Stream、外部 webhook/Telegram | Influx `logs` measurement（tag `service=notification`）、Redis `stream:notification:events` |
| Optimizer Service (`optimizer`) | `services/optimizer/main.py` | 调度 DOM 优化任务、写入进度、发布结果 | Redis、Influx、DOM 数据源 | Redis 流 `stream:optimizer:events`、结果哈希 `hash:optimizer:result:<task_id>`、Influx 查询任务输入数据 |

> 注：所有服务均通过 `ConfigProvider`（`src/ibbot/config/provider.py`）读取远程配置，若配置项未提供则回退到默认值。

### DOM 服务部署说明

- FastAPI 主进程不再在启动阶段尝试自动拉起 DOM Worker。若需要在单体部署中继续复用内置 `DOMService` 的自动启动逻辑，可显式设置环境变量 `ENABLE_DOM_AUTO_START=true`。
- 微服务形态下，DOM 行情与指标由 `ib_feed`/`market_data` 服务负责采集与聚合。主应用通过 Redis 获取健康快照，所使用的键可通过 `DOM_SERVICE_HEALTH_KEY` 指定（默认为空，即表示此部署未提供内置 DOM 服务）。
- 当未配置外部健康键时，`/api/services/status` 会返回 `dom-service`（可通过 `DOM_SERVICE_NAME` 覆盖）条目，状态为 `offline` 且 detail 为“external/not provisioned”，提示 UI 不再尝试自动启动。

## 阶段切换注意事项

1. **阶段 1 → 阶段 2**：部署前确保 Redis/Influx 已通过 `scripts/diagnose_foundation.py` 验证；各服务的 `SERVICE_NAME` 需在配置中心写入订阅标的、队列键等参数。
2. **阶段 2 → 阶段 3**：主服务需加载新增路由（如 `/api/monitoring/metrics/write-rate`）并开放给前端；记得同步更新 `static/index.html` 的监控组件。
3. **阶段 3 → 阶段 4**：在 `docker/local.compose.yml` 中加入所有服务，执行 `./scripts/deploy.sh up --env local --build` 进行联调，再按照测试清单运行单元/集成/E2E/性能/故障注入。
4. **回滚策略**：若新阶段出现异常，可通过 `./scripts/deploy.sh restart <service>` 单独重启；必要时在配置中心将新服务标记为禁用（例如从 Redis `hash:config:<service>` 删除订阅）。

## 与其他文档的关系

- 部署脚本细节：见《IBBot 部署与诊断指引》（`docs/deployment.md`）。
- 运维日常操作、值班流程与故障排查：见《IBBot 运维手册与故障排查》（`docs/operations_manual.md`）。
- 测试分类与执行命令：见更新后的 `docs/tests.md`。
