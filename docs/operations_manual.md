# IBBot 运维手册与故障排查

本文档面向值班与运维团队，覆盖 IBBot 在各阶段的操作步骤、常用监控指标以及故障排查流程。文中命令默认在仓库根目录执行，相关脚本与服务以仓库当前实现为准。

## 1. 日常操作流程

### 1.1 变更前检查

1. **同步环境变量**：确认 `.env`/`.env.local` 已更新到最新提交，并在需要时使用 `diff` 核对敏感变量。
2. **依赖自检**：运行诊断脚本（阶段 ≥1 必做）。
   ```bash
   python scripts/diagnose_foundation.py --dry-run
   ```
   如果需要真实连通性测试，移除 `--dry-run` 参数。
3. **镜像准备**：阶段 4 建议提前构建镜像。
   ```bash
   ./scripts/deploy.sh up --env local --build --no-diagnose
   ./scripts/deploy.sh down
   ```

### 1.2 启动/停止服务

- **顺序启动（推荐）**：
  ```bash
  ./scripts/deploy.sh up --env local
  ```
  `deploy.sh` 会按 Redis → InfluxDB → IB Gateway → 主服务 → Optimizer → UI → Cloudflare Tunnel（如配置）依次启动，并在 180 秒内轮询健康状态。
- **查看状态**：
  ```bash
  ./scripts/deploy.sh status
  ```
- **单服务重启**（例如仅重启行情聚合服务）：
  ```bash
  ./scripts/deploy.sh restart market-data
  ```
- **停机清理**：
  ```bash
  ./scripts/deploy.sh down
  ```

### 1.3 阶段特定核对项

| 阶段 | 核对项 |
| --- | --- |
| 阶段 1 | `python scripts/diagnose_foundation.py` 返回全绿；`data/logs/` 写入权限正常。 |
| 阶段 2 | Redis 中存在服务配置键（`hash:config:<service>`）；`services/<name>/main.py` 日志无异常；Influx `service_metrics` 出现对应服务写入。 |
| 阶段 3 | 访问主站 `/rapidoc`、`/api/monitoring/influx/write-rate` 成功；前端监控卡片可展示队列和 Influx 指标。 |
| 阶段 4 | `pytest tests`、`pytest tests/e2e`、`pytest tests/perf`、`pytest tests/failover` 全部通过；`./scripts/deploy.sh up --env production` 能在目标环境跑通。 |

## 2. 监控指标与诊断入口

### 2.1 指标来源概览

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| `service_metrics` measurement | 所有服务通过 `src/ibbot/influx.build_service_metric_point` 写入 | 统一包含 `service`、`metric`、`value` 字段，监控吞吐、延迟、队列深度等。 |
| `hash:service:queue-monitor` | Queue Monitor 服务 (`services/queue_monitor/main.py`) | 保存最近一次队列/Stream 指标快照，可用 `redis-cli HGETALL` 查看。 |
| `stream:market-service` | Market Data 服务 | 累积行情快照，Risk/策略消费并统计延迟。 |
| `stream:ib:results` | IB Command/风险服务 | 存储指令执行结果、风控触发记录。 |
| `logs/trading_bot.log` | `src/ibbot/logging` 统一配置 | 主服务与子服务可指定 `log_dir`；检查 WARNING/ERROR。 |

### 2.2 常用监控命令

- **Influx 写入速率**（`service_metrics` measurement）：
  ```bash
  curl "http://localhost:8000/api/monitoring/influx/write-rate?service=market-data-service&aggregate=sum"
  ```
- **队列堆积情况**：
  ```bash
  redis-cli HGETALL hash:service:queue-monitor
  ```
- **行情写入快照**（`market_price`/`market_dom` measurement，替换 `<bucket>` 为实际名称）：
  ```bash
  docker exec -it influxdb influx query 'from(bucket: "<bucket>") |> range(start: -5m) |> filter(fn: (r) => r._measurement == "market_price") |> limit(n:5)'
  ```
- **策略事件流积压**：
  ```bash
  redis-cli XINFO GROUPS stream:strategy:events
  redis-cli XLEN stream:strategy:events
  ```
- **风险服务日志**（如配置独立目录）：
  ```bash
  tail -f data/logs/risk-service.log
  ```

## 3. 常见故障排查

### 3.1 基础依赖不可用

- **现象**：`deploy.sh` 阶段性失败或诊断脚本报错。
- **定位**：
  1. 运行 `python scripts/diagnose_foundation.py` 查看 Redis/Influx 连接详情。
  2. 若仅 Redis 异常，检查端口占用（`ss -ltnp | grep 6379`）与防火墙。
  3. 若 Influx Token 错误，校对 `.env.local` 的 `INFLUX_TOKEN/ORG/BUCKET` 与容器初始化日志。
- **恢复**：修正配置后 `./scripts/deploy.sh restart redis influxdb ibbot`。

### 3.2 行情无更新

- **现象**：前端行情卡片不刷新、策略无触发。
- **定位**：
  1. 检查 `redis-cli PUBSUB CHANNELS "pubsub:ib:price*"` 是否存在频道；若无，确认 `services/ib_feed` 是否运行且订阅列表正确。
  2. 使用上文的 Influx 查询命令确认 `market_price`/`market_dom` measurement 在最近 5 分钟内有写入。
  3. 查看 `services/ib_feed` 日志（`docker logs` 或 `tail -f data/logs/ib-feed.log`）。
- **恢复**：更新配置（Redis `hash:config:ib-feed` 中的订阅列表），重启 `ib-feed` 与 `market-data`。

### 3.3 队列/流堆积

- **现象**：Queue Monitor 报警或前端监控显示 `stale_seconds` 升高。
- **定位**：
  1. `redis-cli HGET hash:service:queue-monitor queue:ib-command` 查看长度与滞留时间。
  2. 通过 `redis-cli XINFO GROUPS stream:market-service`/`stream:ib:results` 查看各消费组的 `pending` 数量与最后交付时间。
  3. 查看相关服务日志（如 `tail -f data/logs/risk-service.log`、`docker logs market-data`）确认是否抛出异常。
- **恢复**：
  - 若策略处理慢，可暂时扩容策略服务实例（增加消费者 ID）。
  - 若异常事件导致消费者崩溃，先 `./scripts/deploy.sh restart <service>`，必要时使用 `XCLAIM` 转移 pending 记录。

### 3.4 通知未送达

- **现象**：风控触发但 Telegram/浏览器无提示。
- **定位**：
  1. `redis-cli XLEN stream:notification:events` 判断事件是否进入通知流。
  2. 查看 `services/notification` 日志确认 `send_*` 调用是否报错。
  3. 在主服务调用 `/api/notifications/test`（需管理员身份）发送测试消息。
- **恢复**：检查 `.env.local` 中的 `TELEGRAM_TOKEN/CHAT_ID`、浏览器通知配置；修复后重启通知服务。

### 3.5 性能阈值不达标

- **现象**：性能测试失败或 `service_metrics` 中 throughput/latency 超限。
- **定位**：
  1. 执行基准：`pytest tests/perf -k benchmark`。
  2. 查看 `tests/perf/benchmark.py` 设置的阈值，确认瓶颈类别。
  3. 检查 `service_metrics` 中 `metric=throughput`、`metric=latency` 的实际值。
- **恢复**：考虑增大 Redis 连接池、调整 `services/market_data` 的 `metrics_interval` 或减少订阅标的；必要时扩容容器资源。

## 4. 交付前检查清单

1. `./scripts/deploy.sh up --env local --build` 成功并通过诊断。
2. 监控接口 `/api/monitoring/influx/write-rate` 与 Queue Monitor 快照均有数据。
3. 测试命令按《测试清单》执行并通过。
4. 更新 `docs/deployment.md`、`docs/service_delivery_overview.md`、`docs/tests.md` 后，与实现差异再次校对。

完成上述步骤后即可提交审阅或进入下一阶段发布流程。
