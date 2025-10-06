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
  `deploy.sh` 会按 Redis → MariaDB → IB Gateway → 主服务 → Optimizer → UI → Cloudflare Tunnel（如配置）依次启动，并在 180 秒内轮询健康状态。
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
| 阶段 2 | MariaDB `config` 表存在服务配置键（`<service>:` 前缀）；`services/<name>/main.py` 日志无异常；Redis 指标集合（`zset:service_metrics:*`）出现对应服务写入。 |
| 阶段 3 | 访问主站 `/rapidoc`、`/api/monitoring/metrics/write-rate` 成功；前端监控卡片可展示队列和写入速率指标。 |
| 阶段 4 | `pytest tests`、`pytest tests/e2e`、`pytest tests/perf`、`pytest tests/failover` 全部通过；`./scripts/deploy.sh up --env production` 能在目标环境跑通。 |

## 2. 监控指标与诊断入口

### 2.1 指标来源概览

| 指标 | 来源 | 说明 |
| --- | --- | --- |
| Redis 指标集合 | 所有服务通过 `src.ibbot.redis.record_service_metrics` 写入 | 键名形如 `zset:service_metrics:<service>:<metric>`，统一记录 `service`、`metric`、`value` 等字段，默认保留 24 小时。 |
| `hash:service:queue-monitor` | Queue Monitor 服务 (`services/queue_monitor/main.py`) | 保存最近一次队列/Stream 指标快照，可用 `redis-cli HGETALL` 查看。 |
| `stream:market-service` | Market Data 服务 | 累积行情快照，Risk/策略消费并统计延迟。 |
| `stream:ib:results` | IB Command/风险服务 | 存储指令执行结果、风控触发记录。 |
| `logs/trading_bot.log` | `src/ibbot/logging` 统一配置 | 主服务与子服务可指定 `log_dir`；检查 WARNING/ERROR。 |

### 2.2 常用监控命令

- **写入速率（Redis 指标集合）**：
  ```bash
  curl "http://localhost:8000/api/monitoring/metrics/write-rate?service=market-data-service&aggregate=sum"
  ```
- **队列堆积情况**：
  ```bash
  redis-cli HGETALL hash:service:queue-monitor
  ```
- **行情写入快照**（Parquet `data/market-data` 目录）：
  ```bash
  ls -ltr data/market-data | tail
  parquet-tools meta data/market-data/<symbol>/<interval>.parquet
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
- **风险快照持久化**：风险服务启动时会使用 `src.db` 提供的仓库工厂创建 MariaDB 写入器。
  请在 `.env.local` 中配置 `IBBOT_DB_URL`，否则仅会在 Redis 中缓存风险配置与规则快照。
- **实时止损/止盈权威数据**：风控判断依赖行情服务写入的
  `hash:market:snapshot:{symbol}` 与风险服务维护的 `hash:risk:rules`
  Hash。Redis 中的这两类快照是唯一可信的实时价格与规则来源；
  MariaDB `risk_rules` 表仅作备份，直接读取可能因延迟或缓存失效
  返回空值，导致止损/止盈误判。

### 2.3 风险 trailing 规则缓存与广播

- **Redis 缓存键**：
  - `hash:risk:rules`：存放每个标的的实时风控规则，Trailing 更新会直接覆盖 `stop_loss`、`take_profit`、`atr_params` 等字段。
  - `json:risk:rules`：Risk Runtime 写入的规则快照 JSON，字段包含 `rules`、`updated_at`、`count` 与 `source`，排查前端展示问题时可 `redis-cli GET json:risk:rules` 对照。
  - `json:risk:config`：与上方规则快照同批写入，携带 `rules_count`、`updated_at`，用于确认配置是否和规则同步刷新。
- **广播频道**：`pubsub:risk:rules:update`。Trailing 引擎每次写入缓存后会通过该频道推送 `{type:'risk_rules', rules:{...}}` 消息，前端订阅者据此刷新界面。若页面不更新，优先确认此频道是否持续产生消息。
- **行情订阅来源**：`pubsub:ib:price:{symbol}`。Trailing 引擎为每个跟踪中的标的维护独立订阅，可通过 `redis-cli PUBSUB NUMSUB pubsub:ib:price:ES` 验证是否仍在监听。持仓快照取自 `account:positions`，若方向识别异常需同步检查该键的 JSON。
- **节流逻辑**：价格移动必须超过止损/止盈偏移量的 20%（不足时使用最小跳动单位）且距离上次推送至少 `trail_min_interval` 秒；若规则未显式配置则回退到 ATR 参数里的 `update_throttle` 或默认 2 秒。排查“更新过于频繁/迟迟不动”时，请同时核对这两个阈值。

运营人员在排查 trailing 规则时，可按照上述顺序（缓存 → 广播 → 行情订阅 → 节流阈值）逐项定位问题来源。

## 3. 常见故障排查

### 3.1 基础依赖不可用

- **现象**：`deploy.sh` 阶段性失败或诊断脚本报错。
- **定位**：
  1. 运行 `python scripts/diagnose_foundation.py` 查看 Redis/MariaDB 连接详情。
  2. 若仅 Redis 异常，检查端口占用（`ss -ltnp | grep 6379`）与防火墙。
  3. 若 MariaDB 凭据错误，校对 `.env.local` 的 `IBBOT_DB_URL/USER/PASSWORD` 与容器初始化日志。
- **恢复**：修正配置后 `./scripts/deploy.sh restart redis mariadb ibbot`。

### 3.2 行情无更新

- **现象**：前端行情卡片不刷新、策略无触发。
- **定位**：
  1. 检查 `redis-cli PUBSUB CHANNELS "pubsub:ib:price*"` 是否存在频道；若无，确认 `services/ib_feed` 是否运行且订阅列表正确。
  2. 使用上文的 Parquet 检查命令确认行情文件在最近 5 分钟内有更新。
  3. 查看 `services/ib_feed` 日志（`docker logs` 或 `tail -f data/logs/ib-feed.log`）。
- **恢复**：更新配置（数据库 `config` 表中 `ib-feed:` 前缀的订阅列表），重启 `ib-feed` 与 `market-data`。

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
- **监控**：前端 “Service Health” 面板新增 `Notification Service` 卡片，对应 Redis 健康键
  `hash:service:health:notification`。若状态显示“未上报”，可通过 `/api/services/restart`
  触发对 `notification` 服务的重启（默认监听 `8060` 端口）。该操作会自动串联
  `connection_control`，确保连接断开后订阅与会话均重新建立。
  `/api/services/status` 响应同样会列出该卡片，`POST /api/services/restart {"service": "notification"}`
  会依次重启连接控制与通知分发，便于在排查 Redis 连接或通道堆积问题时“一键修复”。
- **恢复**：检查 `.env.local` 中的 `TELEGRAM_TOKEN/CHAT_ID`、浏览器通知配置；修复后重启通知服务。
  - 若在裸机或自定义网络中运行，请确保 `NOTIFICATION_SERVICE_URL` 指向实际可访问的地址（Docker 默认 `http://notification:8055`，独立部署可改为 `http://127.0.0.1:8055`），必要时同步更新 `NOTIFICATION_SERVICE_TIMEOUT`。

此外，`/api/services/status` 的 `startup_checks` 字段会列出关键启动自检结果。主应用在 FastAPI 生命周期钩子中会扫描当前进程的文件描述符（必要时调用 `lsof`）并记录 `sqlite_handles` 检测结果；一旦存在遗留的 SQLite 句柄，启动将失败。CI 仅需断言该字段为健康即可自动监控数据库连接清理回归。

### 3.5 性能阈值不达标

- **现象**：性能测试失败或 Redis 指标集合中的 throughput/latency 超限。
- **定位**：
  1. 执行基准：`pytest tests/perf -k benchmark`。
  2. 查看 `tests/perf/benchmark.py` 设置的阈值，确认瓶颈类别。
  3. 检查 Redis 指标集合中 `metric=throughput`、`metric=latency` 的实际值。
- **恢复**：考虑增大 Redis 连接池、调整 `services/market_data` 的 `metrics_interval` 或减少订阅标的；必要时扩容容器资源。

## 4. 交付前检查清单

1. `./scripts/deploy.sh up --env local --build` 成功并通过诊断。
2. 监控接口 `/api/monitoring/metrics/write-rate` 与 Queue Monitor 快照均有数据。
3. 测试命令按《测试清单》执行并通过。
4. 更新 `docs/deployment.md`、`docs/service_delivery_overview.md`、`docs/tests.md` 后，与实现差异再次校对。

完成上述步骤后即可提交审阅或进入下一阶段发布流程。

## 5. 市场数据落地与保留策略

- **目录结构**：实时 DOM 与 K 线数据统一写入 `data/market_data`。其中 DOM 快照位于
  `data/market_data/dom/{SYMBOL}/YYYYMMDD.parquet`，K 线数据位于
  `data/market_data/candles/{interval}/{SYMBOL}/YYYYMMDD.parquet`。文件按 UTC 日期划分，字段包含
  `timestamp/timestamp_ns` 与原始 DOM 层级或 OHLCV 指标，便于下游按同一 Schema 消费。
- **写入机制**：`services/ib_feed` 在 DOM 事件落地时直接调用 Parquet Writer，`data_layer`
  在生成 K 线事件时同样追加写入；若缓存命中失败，会从对应 Parquet 文件回填历史，确保服务重启后仍可快速恢复。
- **压缩与轮换**：提供 `scripts/market_data_rotate.py` 脚本，默认 7 天后将日度 Parquet 文件压缩为
  `.parquet.gz`，30 天后删除归档。可通过 `--base-dir`、`--compress-after-days`、`--delete-after-days`
  自定义策略，并支持 `--dry-run` 预览操作。部署时建议将该脚本加入定时任务，避免磁盘持续增长。
- **存储路径**：通过 `MARKET_DATA_STORAGE_PATH`（或 `DATA_LAYER_PARQUET_PATH`）调整根目录，`MARKET_DATA_PARQUET_COMPRESSION`
  控制写入压缩算法（默认 `snappy`）。确保目录具备写权限并纳入备份策略。
