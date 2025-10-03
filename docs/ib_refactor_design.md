# IB Trading Platform Refactor Design (v3)

## 1. 目标与约束
- **核心数据库**：除日志与 DOM/K 线行情文件之外，所有业务数据统一存储到 MariaDB，包含账户、订单、策略、配置、服务监控、优化结果等。
- **日志落盘**：各服务继续使用模块化日志组件将日志写入 `data/logs/<module>.log`，无需导入数据库。
- **行情归档**：原始 DOM 与 K 线数据仍保存在 `data/market/` 目录下，以 Parquet 文件格式按交易日/合约分区。
- **移除 InfluxDB**：系统不再依赖 InfluxDB；历史查询、监控统计均由 MariaDB、Redis 或预聚合文件承担。
- **解耦架构**：延续“多服务 + 消息队列”的思路，各服务通过 Redis 队列/发布订阅与 HTTP/WebSocket API 协同。

## 2. 当前痛点
1. **同步阻塞严重**：主进程串行处理 IB 调用、数据库访问、行情刷新等重任务，阻塞 UI。
2. **状态耦合**：模块彼此持有对象引用，拆分服务困难，缺乏清晰接口。
3. **数据杂乱**：SQLite + 文件 + 临时缓存混用，缺乏统一 schema；历史查询、统计不一致。
4. **监控缺失**：日志分散、无集中指标；服务健康情况难以掌握。
5. **测试薄弱**：新架构缺少阶段性验证方案。

## 3. 新架构总览

### 3.1 核心组件
- **基础能力库 (foundation-libs)**
  - `ibbot.config`: 加载 `.env` 与 MariaDB/Redis 参数。
  - `ibbot.redis`: 提供队列、Stream、Pub/Sub 封装。
  - `ibbot.db`: MariaDB 连接池、ORM/查询助手、迁移管理（基于 SQLAlchemy + Alembic）。
  - `ibbot.logging`: 统一日志格式/切分。
  - `ibbot.storage`: 行情 Parquet 写入/读取工具。
- **IB 指令执行服务 (ib-command-service)**：串行处理 Redis 中的 IB 指令并写回结果。
- **IB 行情采集服务 (ib-feed-service)**：维护 IB 行情订阅，推送实时数据并落地 DOM/K 线。
- **行情聚合服务 (market-data-service)**：统一对外提供 REST/WebSocket 行情/账户接口，缓存快照。
- **风险控制服务 (risk-service)**：消费行情与订单结果，执行风险规则。
- **策略服务 (strategy-service)**：调度策略任务，生成下单请求。
- **优化器服务 (optimizer-service)**：使用 MariaDB/Parquet 数据执行参数优化。
- **通知服务 (notification-service)**：订阅事件流并推送外部渠道。
- **运维与 UI 网关 (ui-gateway)**：管理配置、监控、日志查看与服务编排。

### 3.2 数据与通信流
- **Redis**
  - `queue:ib:commands`：指令入队。
  - `stream:ib:results`：指令回执/订单状态。
  - `pubsub:ib:price:<symbol>`、`pubsub:ib:dom:<symbol>`：实时行情推送。
  - `hash:market:snapshot:<symbol>`：市场数据服务维护的快照。
  - `stream:strategy:events`、`stream:risk:events`、`stream:notification:events`：业务事件总线。
  - `hash:service:health:<module>`：心跳与状态。
  - `hash:config:<module>`：可热更新配置。
- **MariaDB**
  - `accounts`：账户与仓位快照。
  - `orders` / `order_executions`：订单生命周期及成交。
  - `strategies` / `strategy_runs` / `strategy_logs`：策略配置、执行记录。
  - `risk_rules` / `risk_events`：风险规则与触发记录。
  - `optimizer_tasks` / `optimizer_results`：优化任务与结果。
  - `service_metrics`：服务采集指标（消费速率、延迟等）。
  - `audit_logs`：配置修改、敏感操作审计。
  - `configurations`：持久化 UI 配置、服务参数。
- **文件系统**
  - `data/logs/`：按模块滚动日志。
  - `data/market/<symbol>/<date>/dom.parquet`、`kline.parquet`：DOM/K 线数据分区存储。
  - `data/tmp/`：临时中间文件（例如批量导出）。

### 3.3 行情处理工作流
1. `ib-feed-service` 订阅 IB 行情，解析 Tick 与 DOM。
2. 实时数据发布至 `pubsub:ib:*` 并刷新 `hash:market:snapshot`。
3. 每隔固定窗口（如 1 秒/1 分钟）追加 DOM/K 线数据到 Parquet 文件；写入写入索引元数据到 MariaDB 的 `market_partitions` 表（记录文件路径、时间范围、行数）。
4. `market-data-service` 订阅实时通道，维护内存快照；对历史查询请求时，根据 `market_partitions` 元数据读取 Parquet。

### 3.4 订单与状态流
1. 策略/风控/手动 UI 调用 `enqueue_ib_command`，将订单请求写入 `queue:ib:commands`。
2. `ib-command-service` 串行执行 IB API，日志落盘，并将结果写入 `stream:ib:results`。
3. 订单执行成功后，同时写入 MariaDB `orders`、`order_executions`，并更新 `accounts` / `positions`。
4. `risk-service` 与 `strategy-service` 监听结果流与数据库变更，实现状态同步。

### 3.5 配置与监控
- 配置统一写入 MariaDB `configurations` 表，并镜像到 Redis `hash:config:<module>` 供服务热加载；UI 中的修改会同时更新两处。
- 每个服务定期上报健康信息到 Redis `hash:service:health` 与 MariaDB `service_metrics`。
- UI 仪表板从 MariaDB 聚合指标，结合 Redis 实时状态实现监控。

## 4. 详细模块设计

### 4.1 基础能力库
- **配置模块**：加载 `.env`，从 MariaDB/Redis 拉取配置，支持热更新回调。
- **数据库模块**：
  - 提供连接池、事务管理、数据访问层；使用迁移工具维护 schema。
  - 提供读写封装：`db.orders.create_order()`、`db.metrics.upsert_service_metric()` 等。
  - 支持读写分离（可配置），未来可扩展只读副本。
- **Redis 模块**：统一处理队列/Stream/PubSub，支持断线重连、消费确认。
- **日志模块**：按日期/大小切分，支持 JSON 文本两种格式。
- **存储模块**：
  - 行情写入：批量写入 Parquet（使用 PyArrow / Pandas），自动创建目录。
  - 行情读取：按时间范围扫描 `market_partitions`，合并 Parquet。

### 4.2 IB 指令执行服务
- 组件：`CommandConsumer`、`IBCommandExecutor`、`ResultPublisher`。
- 数据写入：
  - 订单请求 -> MariaDB `orders`（pending 状态）。
  - 执行结果 -> 更新 `orders` 状态、写入 `order_executions`。
- 异常处理：重试策略、熔断保护；异常写入 `service_metrics` 与日志。
- 测试：Mock IB 测试执行顺序、失败重试。

### 4.3 IB 行情采集服务
- DOM/Tick 聚合策略：
  - Tick 数据直接推送 Redis；
  - DOM 数据按 10 层聚合，写入 Parquet。
- 元数据管理：更新 `market_partitions`，记录 `symbol`, `date`, `type`, `file_path`, `start_ts`, `end_ts`, `row_count`, `checksum`。
- 健康检查：记录最近拉取时间、订阅数量到 `service_metrics`。
- 测试：模拟行情数据流，校验 Parquet 文件与元数据一致性。

### 4.4 行情聚合服务
- 实时接口：
  - REST `/market/<symbol>` 返回最新价格、DOM、账户信息（来自 Redis 快照 + MariaDB）。
  - WebSocket `/ws/market` 推送订阅列表更新。
- 历史接口：从 `market_partitions` 计算查询范围，读取 Parquet 并返回。
- 缓存策略：热数据缓存到 Redis，减少磁盘读取。
- 指标：将接口延迟、订阅数写入 `service_metrics`。

### 4.5 风险控制服务
- 数据源：实时行情（Redis）、订单结果（Redis/MariaDB）、仓位信息（MariaDB）。
- 规则管理：规则与阈值保存在 `risk_rules`，通过 UI 修改后广播。
- 执行动作：写入风险事件到 `risk_events`，并可触发自动下单。
- 日志：关键事件写入日志文件与 `risk_events`。
- 测试：单元测试规则判定；集成测试覆盖风险触发 -> 下单链路。

### 4.6 策略服务
- 任务入口：Redis 列表 `queue:strategy` 或 MariaDB `strategy_runs` 任务表。
- 执行流程：
  1. 拉取策略配置（MariaDB）与实时行情（market-data-service）。
  2. 执行策略逻辑，生成指令。
  3. 写入订单请求并记录执行日志到 `strategy_logs`。
- 调度：支持并发 worker，利用 Redis 分布式锁控制同一策略互斥。
- 测试：端到端流程、策略适配层单元测试。

### 4.7 优化器服务
- 数据来源：
  - 历史行情：从 Parquet 读取。
  - 策略绩效：来自 MariaDB `strategy_runs`、`optimizer_results`。
- 结果写入：优化完成后更新 MariaDB，并通知 UI。
- 支持批量任务、暂停/恢复。

### 4.8 通知服务
- 订阅 Redis 事件流，依据 `config:notification` 配置决定渠道。
- 发送结果写入 `notification_logs` 表与日志文件。
- 提供 webhook/FastAPI 接口供外部触发。

### 4.9 UI 网关
- 职责：
  - 提供登录/权限、配置管理、策略与风险监控。
  - 展示队列状态（Redis 指标）与服务指标（MariaDB 查询 + Redis 实时数据）。
  - 日志浏览：直接读取 `data/logs`，支持实时 tail。
  - 行情可视化：调用 market-data-service REST/WS。
  - 服务控制：调用 Docker/K8s API，实现重启、滚动更新。
- 数据访问：通过基础库访问 MariaDB 与 Redis，不直接接触 IB。

## 5. 数据库设计与迁移
- 使用 Alembic 管理 MariaDB schema，迁移脚本存放于 `migrations/`。
- 关键表与字段：
  - `orders(id, strategy_id, status, submitted_at, filled_qty, avg_price, error_code, metadata)`。
  - `order_executions(id, order_id, execution_ts, qty, price, commission, exchange)`。
  - `accounts(id, balance, equity, buying_power, updated_at)`。
  - `positions(id, account_id, symbol, qty, avg_cost, unrealized_pnl, updated_at)`。
  - `risk_rules(id, name, params, enabled)`。
  - `risk_events(id, rule_id, severity, message, created_at)`。
  - `service_metrics(id, module, metric, value, collected_at)`。
  - `configurations(id, module, key, value, updated_at, updated_by)`。
  - `market_partitions(id, symbol, date, type, file_path, start_ts, end_ts, row_count, checksum)`。
  - `notification_logs(id, channel, payload, status, created_at)`。
  - `audit_logs(id, actor, action, detail, created_at)`。
- 数据访问层提供 CRUD、聚合查询、分页工具；对高频写入场景使用批量插入。

## 6. 主程序改造计划
1. **初始化流程重构**：移除核心模块的全局单例，FastAPI 启动时初始化配置、Redis、MariaDB 客户端。
2. **API 重写**：
   - 下单 API -> Redis 队列 + MariaDB 记录。
   - 策略与风险 API -> 对应表增删改查，并触发 Redis 更新。
   - 行情展示 -> 调用 market-data-service。
3. **监控仪表板**：
   - 展示 Redis 队列深度、消费速率。
   - 展示 MariaDB 中的 `service_metrics` 趋势。
   - 日志查看、Parquet 文件健康检查（基于 `market_partitions`）。
4. **配置中心**：界面写入 `configurations` 表，并同步刷新 Redis。
5. **权限与审计**：所有敏感操作写入 `audit_logs`；结合 JWT/角色控制访问。
6. **容器管理**：调用 Docker/K8s API，实现服务重启、日志抓取。

## 7. 开发阶段与验证
- **阶段 0：准备**
  - 整理 `.env`（MariaDB/Redis 凭据），搭建 MariaDB 实例。
  - 编写基础迁移脚本，初始化核心表。
  - 测试：`tests/config/test_env_loading.py` 验证配置加载。
- **阶段 1：基础能力库**
  - 实现 `ibbot.db`、`ibbot.redis`、`ibbot.storage`。
  - 测试：
    - 数据库连接/事务 (`tests/foundation/test_db.py`)。
    - Redis 队列/Stream (`tests/foundation/test_redis.py`)。
    - Parquet 写入读取 (`tests/foundation/test_storage.py`)。
- **阶段 2：服务拆分**
  - 依次实现指令执行、行情采集、行情聚合、风险、策略、通知、优化器服务。
  - 为每个服务编写集成测试和 Dockerfile。
- **阶段 3：UI 与运维**
  - 重写 FastAPI 路由、前端页面。
  - 引入服务监控、日志查看、配置中心。
  - 端到端测试覆盖下单、风险触发、优化任务。
- **阶段 4：联调与回归**
  - 使用 docker-compose 启动全套服务，验证消息链路。
  - 编写 `tests/e2e/test_full_pipeline.py` 覆盖 UI -> 订单 -> 回执。
  - 压测行情与订单通道，记录吞吐。

## 8. 测试与质量保障
- **单元测试**：覆盖数据库访问层、规则引擎、策略适配、Parquet 工具。
- **集成测试**：使用 testcontainers 启动 Redis、MariaDB；验证服务间交互。
- **端到端测试**：模拟真实下单、行情数据流、风险触发、通知推送。
- **性能测试**：
  - 行情吞吐（IB -> Redis -> UI）。
  - MariaDB 写入/查询压力。
  - Parquet 文件生成速度与读取延迟。
- **故障注入**：
  - Redis/MariaDB 断开；
  - Parquet 写入失败；
  - IB 断线；验证服务容错。
- **监控验证**：UI 显示的指标与实际数据库/Redis 数据一致。
- **文档更新**：每阶段完成后更新部署手册与故障排查指南。

## 9. 部署与运维建议
- 使用 Docker Compose/Kubernetes 管理多服务部署，MariaDB 采用主从或托管服务。
- 提供 `scripts/deploy.sh` 启停脚本，包含数据库迁移、服务启动顺序。
- 配置备份策略：
  - MariaDB 定期快照/增量备份。
  - Parquet 文件同步到对象存储或离线磁盘。
  - 日志按需归档。
- 安全：开启 MariaDB/Redis 认证、TLS；日志脱敏；UI 权限控制。
- 监控：集成 Prometheus/Grafana（可选）读取 MariaDB/Redis 指标；UI 仪表板提供第一视角。

## 10. 风险与缓解措施
- **数据库迁移风险**：制定灰度方案，先双写（SQLite -> MariaDB），验证后切换。
- **行情文件损坏**：Parquet 写入前校验 checksum，定期跑一致性检查。
- **服务复杂度提升**：以容器化 + 自动化测试保障发布质量。
- **性能瓶颈**：为 MariaDB 设计索引、分区；Parquet 采用列式压缩。
- **团队学习成本**：提供培训、编码规范、示例脚本，保证成员熟悉新架构。
