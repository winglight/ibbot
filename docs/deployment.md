# IBBot 部署与诊断指引

本指南总结了使用 Docker Compose 与脚本工具部署 IBBot 的推荐流程，涵盖本地开发、生产环境切换以及部署后健康检查步骤。文档同步了分阶段交付的能力矩阵，便于在不同阶段快速确认所需服务是否已经上线。

## 阶段交付功能概览

| 阶段 | 交付内容 | 关键目录/组件 |
| --- | --- | --- |
| 阶段 0：准备 | `.env` 模板、`data/logs` 结构、基础 README | 根目录 `.env.example`、`data/` |
| 阶段 1：基础能力库 | Redis/Influx/日志/配置封装、诊断脚本 | `src/ibbot/{config,redis,influx,logging}`、`scripts/diagnose_foundation.py` |
| 阶段 2：独立服务 | 指令执行、行情采集、行情聚合、风险、策略、通知、队列监控等后台服务 | `services/ib_command`、`services/ib_feed`、`services/market_data`、`services/risk`、`services/strategy`、`services/notification`、`services/queue_monitor` |
| 阶段 3：主程序改造 | FastAPI 主应用整合新服务接口与监控视图 | `main.py`、`src/routes/monitoring.py`、`static/` |
| 阶段 4：联调回归 | Docker Compose 全量联调、E2E/性能/故障注入测试 | `docker/*.compose.yml`、`tests/{e2e,perf,failover}` |

在部署前确认目标阶段所需目录或服务均存在，并参考下文的部署指引选择合适的启动组合。

## 1. 预备条件

- Docker Engine 20+ 与 docker compose 插件。
- 宿主机开放端口：`6379` (Redis)、`8086` (InfluxDB)、`8000/8001` (服务)、`4173` (Optimizer UI)、`5800/5900/4001/4002` (IB Gateway)。
- 根目录 `.env` 提供默认值，可通过 `.env.local` 覆盖敏感配置，例如账号、密钥或生产端口。

## 2. 快速启动

```bash
# 按依赖顺序启动 Redis → InfluxDB → IB Gateway → 主服务/优化器 → UI
./scripts/deploy.sh up --env local
```

脚本会自动：

1. 依次执行 `docker compose up -d <service>` 以确保依赖先行（顺序：Redis → InfluxDB → IB Gateway → 主服务 → 优化器 → UI → Cloudflare Tunnel）。
2. 输出 `docker compose ps --format json` 并在 180 秒内等待所有已启动服务进入 `running/healthy` 状态。
3. 调用 `python scripts/diagnose_foundation.py` 检查 Redis、InfluxDB 与日志写入（阶段 1 交付能力）。

如需重启单个服务：

```bash
./scripts/deploy.sh restart ibbot
```

## 支持的服务与重启方式

后台运行的服务会同时支持 Docker Compose 与本地脚本两种重启机制。控制台会优先调用 `/api/services/restart`
（Docker 模式），若宿主机未安装 docker compose 会自动切换至 `/api/services/restart/local` 并执行仓库内的
`scripts/run_service.py`。下表列出了当前监控的服务与对应的端口：

| 服务 | 默认端口 | Docker 重启 | 本地脚本 |
| --- | --- | --- | --- |
| Trading Core (`ibbot`) | 8000 | `docker compose restart ibbot` | `python scripts/run_service.py ibbot` |
| Command Dispatcher (`ib_command`) | — | `docker compose restart ib_command` | `python scripts/run_service.py ib_command` |
| Connection Control (`connection_control`, legacy) | 8056 | `docker compose restart connection_control` | `python scripts/run_service.py connection_control` |
| Account Service (`account`) | 8020 | `docker compose restart account` | `python scripts/run_service.py account` |
| Order Gateway (`orders`) | 8050 | `docker compose restart orders` | `python scripts/run_service.py orders` |
| Market Data Sync (`market_data`) | 8010 | `docker compose restart market_data` | `python scripts/run_service.py market_data` |
| Risk Manager (`risk`) | 8030 | `docker compose restart risk` | `python scripts/run_service.py risk` |
| Strategy Scheduler (`strategy`) | 8045 | `docker compose restart strategy` | `python scripts/run_service.py strategy` |
| Notification Dispatcher (`notification`) | 8055 | `docker compose restart notification` | `python scripts/run_service.py notification` |
| Market Data Feed (`market_data_feed`) | — | `docker compose restart market_data_feed` | `python scripts/run_service.py market_data_feed` |
| Queue Monitor (`queue_monitor`) | 8011 | `docker compose restart queue_monitor` | `python scripts/run_service.py queue_monitor` |

> ℹ️ **依赖顺序提示**：控制台会自动按依赖顺序重启服务，例如 `market_data` 会先重启 `market_data_feed`，
> `ibbot` 会在 `account` 与 `orders` 成功后再启动。`queue_monitor` 独立运行，可单独重启。历史组件
> `connection_control` 仍可手动触发，但默认计划已不再包含。

`services/notification/main.py` 是通知管道的常驻进程，负责将 Redis 队列中的事件转发至 Telegram/邮件等渠道。
无论采用 Docker 还是 `scripts/run_service.py notification` 方式，都需要确保该脚本与主服务同机运行，
并在主程序环境变量中配置 `NOTIFICATION_SERVICE_URL`（Docker 模式默认指向 `http://notification:8055`）。
如果在本地单机调试时仅启动主程序与通知脚本，可在 `.env.local` 中设置
`NOTIFICATION_SERVICE_URL=http://127.0.0.1:8055` 与 `NOTIFICATION_SERVICE_TIMEOUT`，以便跳过容器网络解析。

关闭所有容器：

```bash
./scripts/deploy.sh down
```

## 3. 环境切换（local ↔ production）

`scripts/deploy.sh` 提供 `--env` 参数用于切换 Compose 文件：

- `--env local`（默认）：使用 `docker/local.compose.yml`，适合本地开发，数据默认写入仓库内 `data/` 目录。
- `--env production`：在上述基础上叠加 `docker/prod.compose.yml`，会将数据映射至 `/var/lib/ibbot/*`，并允许通过环境变量调整端口。

示例：

```bash
# 生产部署（需提前准备 .env.local）
./scripts/deploy.sh up --env production
```

脚本会自动加载 `.env` 与 `.env.local` 中的变量；若配置了 `CF_TUNNEL_TOKEN`，Cloudflare Tunnel 服务也会加入启动序列。

## 4. 部署后健康检查

除自动执行的诊断命令外，可手动重复以下步骤：

```bash
# 查看容器状态
./scripts/deploy.sh status

# 再次执行基础依赖检查
./scripts/deploy.sh diagnose
```

`scripts/diagnose_foundation.py` 会基于 `ConfigProvider` 读取 Redis/Influx/日志配置，并给出详细的成功或失败原因（若使用 `--no-diagnose` 可跳过）。

阶段 2 及以后可进一步核对：

- `services/queue_monitor` 写入的 Redis 快照 `hash:service:queue-monitor`（确认队列长度/滞留时间指标）。
- `services/queue_monitor` 同步的健康哈希 `hash:service:health:queue`（供 `/api/services/status` 及外部仪表板读取聚合指标）。
- 主服务 `/api/monitoring/influx/write-rate` 接口返回的 `service_metrics` 写入速率，用于验证 `services/ib_feed`、`services/market_data` 等服务上报是否正常。

若 InfluxDB 使用全新凭据，请确保 `.env.local` 中的 `INFLUX_TOKEN`、`INFLUX_ORG`、`INFLUX_BUCKET` 与初始化参数一致。

## 5. CI 基本校验

GitHub Actions `ci.yml` 增加了 Docker Compose 冒烟测试步骤，会在拉取代码后运行 `./scripts/deploy.sh up --env local --build`，
随后自动执行健康检查与诊断脚本，并在流水线结束时调用 `./scripts/deploy.sh down` 完成清理，可提前捕获镜像更新或依赖初始化异常。

## 6. 常见问题

| 现象 | 排查建议 |
| --- | --- |
| `deploy.sh` 提示缺少 compose 文件 | 确认 `docker/local.compose.yml`、`docker/prod.compose.yml` 位于仓库中，或检查 `--env` 参数是否拼写正确。 |
| 诊断脚本 Redis 连接失败 | 核实宿主机 `6379` 端口是否被占用，或 `.env.local` 中的 `IBBOT_REDIS_URL`/`REDIS_URL` 是否覆盖为其他地址。 |
| InfluxDB 健康检查失败 | 检查 `INFLUX_*` 初始化变量是否匹配容器首次启动时设置的值；必要时删除 `data/influxdb*` 重新初始化。 |
| Optimizer UI 无法访问 | 确认 `optimizer_ui/` 目录存在构建产物，并检查宿主机 `4173` 端口是否冲突。 |

如需更深入的依赖说明，请参考 `docs/foundation_libs.md`。

## 更多运维资料

- 《服务阶段交付与运维概览》：`docs/service_delivery_overview.md`（阶段功能列表、各服务依赖矩阵）。
- 《IBBot 运维手册与故障排查》：`docs/operations_manual.md`（操作步骤、指标、诊断流程）。
