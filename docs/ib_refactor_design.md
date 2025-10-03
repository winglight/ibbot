# IB Trading Platform Refactor Design (v2)

## 1. 背景
- 现有 `main.py` 在导入阶段即初始化风控、策略、队列、订单、行情等所有核心对象，并通过内存引用互相耦合。
- 所有 IB 调用、数据库访问、行情刷新、风控循环都在同一个进程的事件循环内执行，线程桥接广泛使用同步 `result()` 阻塞，导致 UI 卡顿。
- 优化器、队列、风控、行情、通知等组件缺乏清晰的边界，没有统一的数据访问/日志/监控基础设施，难以扩展也难以排错。
- 用户要求将功能拆成独立程序，通过 Redis 队列/订阅协作，持久化改为 Redis + MariaDB，并让主程序集中监控与可视化。

## 2. 主要问题
1. **同步阻塞严重**：IB 桥接、（遗留）SQLite 访问、行情轮询、风控循环均在主线程同步等待，导致 FastAPI 响应超时。
2. **状态耦合**：模块直接持有彼此对象引用，缺乏消息协议；很难拆分或横向扩展。
3. **数据通道不明确**：行情、DOM、订单、日志写入由多个模块自行处理，缺乏统一策略，历史数据查询路径混乱。
4. **基础设施缺失**：没有统一的 Redis/MariaDB/日志访问库，环境变量散落在代码中，模块部署/监控困难。
5. **测试与验证薄弱**：缺少针对新架构的分步测试计划，难以保证拆分后的可靠性。

## 3. 总体设计

### 3.1 模块划分
- **基础能力库 (foundation-libs)**：
  - `ibbot.config`: 环境变量加载与配置中心。
  - `ibbot.redis`: Redis 连接池、队列、Pub/Sub、Stream 封装。
  - `src.db`: MariaDB 写入/查询适配，统一持久化入口。
  - `ibbot.logging`: 统一日志封装，默认写入 `data/logs/<module>.log` 并支持标准输出。
- **IB 指令执行服务 (ib-command-service)**：独占 IB Gateway/TWS 连接，串行处理 Redis 队列中的下单/撤单/查询指令，并回写结果。
- **IB 数据采集服务 (ib-feed-service)**：使用独立 IB 客户端订阅实时行情与 DOM，写入 Redis Pub/Sub/缓存，同时负责 MariaDB 持久化。
- **市场数据服务 (market-data-service)**：
  - 对外提供统一的 HTTP/WebSocket/Redis 接口，聚合实时价格、DOM、账号信息，统一缓存与二次分发。
  - 订阅 `ib-feed-service` 的 Redis 通道并维护可查询快照，使调用方只需访问一个服务即可获得行情/DOM。
- **风险控制服务 (risk-service)**：
  - 订阅市场数据服务推送的行情和账户信息，执行风险规则；通过 `ib-command-service` 队列发送保护性指令。
- **策略执行服务 (strategy-service)**：
  - 消费策略任务，调用市场数据服务获取行情，生成信号后将订单请求写入 `ib-command-service`。
- **队列监控/调度服务 (queue-monitor)**：
  - 负责 Redis 队列/Stream 的消费情况统计，供主程序展示；可选作为轻量守护进程。
- **通知服务 (notification-service)**：
  - 消费事件 Stream，统一发送 Telegram/邮件/WebSocket 推送。
- **优化器服务 (optimizer-service)**：
  - 复用 `foundation-libs`，提供策略/参数优化 API 与任务执行（不依赖主程序实例，可单独部署）。
- **主程序 (ui-gateway)**：
  - FastAPI + 前端，负责配置管理、监控、日志查看、任务编排、一键重启容器。

### 3.2 数据与通信流
- **Redis**
  - `queue:ib:commands`：主程序/策略/风控发送 IB 指令；`ib-command-service` 按 FIFO 串行消费。
  - `stream:ib:results`：`ib-command-service` 将执行结果写入（含订单状态、错误码），其他模块订阅。
  - `pubsub:ib:price:<symbol>`、`pubsub:ib:dom:<symbol>`：`ib-feed-service` 实时推送行情与 DOM。
  - `hash:market:snapshot:<symbol>`：市场数据服务维护的快照，供 REST/WS 查询。
  - `stream:strategy:events`、`stream:risk:events`、`stream:notification:events`：各模块事件总线。
  - `hash:service:health:<module>`：每个服务上报健康信息（心跳时间、最近错误）。
  - `hash:config:<module>`：配置参数，由主程序写入，模块订阅变化。
  - `list:queue:strategy`：策略任务队列。
- **MariaDB**
  - `orders`：记录订单请求/执行结果（时间、策略、状态、数量、错误码）。
  - `market_events`：存储行情/DOM/指标事件的 JSON 负载，便于历史回放。
  - `market_prices`：缓存最新盘口快照与观测时间。
  - `service_metrics`：服务写入吞吐、延迟、队列深度等监控指标。
- **日志**
  - 每个服务使用 `foundation-libs` 的日志封装写入 `data/logs/<module>.log`。
  - 主程序读取并索引日志文件供前端展示，可结合 WebSocket 推送最新日志。

### 3.3 Pub/Sub 工作流
1. `ib-feed-service` 从 IB 订阅行情与 DOM。
2. 收到更新后，写入 `pubsub:ib:*` 通道，并通过 `ibbot.redis` 工具更新 `hash:market:snapshot` 与 MariaDB。
3. `market-data-service` 订阅对应通道，聚合为快照；提供 REST `/market/<symbol>` 和 WebSocket `/ws/market` 接口返回最新价格、DOM、账户指标。
4. 其他模块（策略、风险、主程序）只需访问市场数据服务的接口或 Redis 快照，不再直接与 IB 服务交互。

### 3.4 订单流转
1. 任一模块调用基础库 `ibbot.redis.enqueue_ib_command()` 将订单/查询写入 `queue:ib:commands`，指令含 `command_id`。
2. `ib-command-service` 逐条执行，写入执行日志到 `data/logs/ib-command.log`，并将结果（成功/失败、订单状态、填充详情）写入 Redis Stream `stream:ib:results` 与 MariaDB `orders`。
3. 主程序、风险、策略订阅 `stream:ib:results` 或通过 `command_id` 查询 Redis Hash `hash:ib:result:<id>` 获取结果。

### 3.5 环境与配置
- 所有服务在启动时仅依赖必要环境变量（Redis/MariaDB 地址、服务名称等）；其他可运行时调整的参数（订阅列表、风控阈值、策略配置、行情刷新周期）通过配置模块加载：
  - `configuration` 模块从 Redis `hash:config:<module>` 读取，支持热更新（订阅 `pubsub:config:update`）。
  - 主程序提供页面与 API 管理配置，并写回 Redis。
  - 每个服务仅在首次启动时需要的环境变量保留在 `.env`（如 IB 账号、密码、客户端 ID）。

## 4. 独立模块设计

### 4.1 基础能力库 (foundation-libs)
- **redis 子模块**：
  - 初始化连接池、提供 `enqueue_ib_command`, `publish_market_update`, `read_stream`, `ack_stream` 等高层 API。
  - 封装 Pub/Sub 监听器，支持自动重连与订阅处理。
  - 提供队列/Stream 监控 API（队列长度、消费速率）。
- **db 子模块**：
  - 提供批量写入、查询、连接管理、写入速率统计。
  - 定义标准化的订单、行情、服务指标持久化结构。
- **logging 子模块**：
  - 统一格式化（JSON/文本），支持按模块名称写入 `data/logs`。
  - 提供日志读取工具，供主程序分页展示。
- **config 子模块**：
  - 支持 `.env` + Redis 配置合并，提供 `ConfigProvider` 接口；监听配置变更。
- 所有服务必须依赖这些库，不可直接访问底层客户端，保证一致性。

### 4.2 IB 指令执行服务
- 单一职责：顺序执行来自 Redis 队列的 IB 指令。
- 组件：
  - `CommandConsumer`: 从 `queue:ib:commands` 拉取任务，写执行状态。
  - `IBCommandExecutor`: 调用 IB API，处理异常与重试策略。
  - `ResultPublisher`: 写回 Redis/MariaDB，并记录日志。
- 健康监控：定期更新 `hash:service:health:ib-command`，字段含 `last_seen`, `queue_depth`, `last_error`。
- 验证：
  - 单元测试覆盖指令解析、重试逻辑。
  - 集成测试使用 IB 模拟环境验证下单与回执。

### 4.3 IB 数据采集服务
- 职责：管理 IB 行情订阅，推送至 Redis 并写入 MariaDB。
- 行情缓存：使用 `foundation-libs.redis` 更新 `hash:market:snapshot:<symbol>`。
- DOM 处理：将多个级别的数据打包为结构化 JSON，写入 MariaDB `market_events`。
- 落库职责明确：**此服务是行情/DOM 的唯一落库者**，其他服务禁止直接写入相关 MariaDB 表。
- 验证：
  - 模拟行情流测试吞吐与落库。
  - 对 MariaDB 查询接口编写回归测试，验证数据完整性。

### 4.4 市场数据服务
- 对外 API：
  - REST `/market/<symbol>` 返回价格+DOM+账户快照。
  - WebSocket `/ws/market` 推送订阅符号列表。
  - Redis `stream:market-service` 提供统一的事件流。
- 内部：订阅 `ib-feed-service` 的 Pub/Sub，维护内存快照；定期将统计信息写入 `service_metrics`。
- 还提供历史查询聚合：从 MariaDB 拉取数据并缓存。
- 验证：
  - 单元测试覆盖缓存更新、接口响应。
  - 性能测试验证高频订阅下的延迟。

### 4.5 风险控制服务
- 接入：
  - 从市场数据服务 API 获取最新行情与仓位。
  - 订阅 `stream:ib:results` 以追踪订单结果。
- 规则：
  - 配置从 `config:risk` 读取；变更通过 Pub/Sub 下发。
  - 触发保护性操作时，将命令写入 `queue:ib:commands`。
- 日志：写 `data/logs/risk-service.log`，关键事件亦写入 MariaDB 以备审计和指标统计。
- 验证：
  - 单元测试验证规则判定。
  - 集成测试模拟行情触发止损。

### 4.6 策略执行服务
- 消费 `list:queue:strategy`；每个任务内：
  1. 通过市场数据服务拉取行情。
  2. 运行策略逻辑。
  3. 生成订单写入 `queue:ib:commands`。
  4. 将执行状态写入 `stream:strategy:events`。
- 支持多进程/协程调度，但通过 Redis 控制并发。
- 验证：
  - 单元测试策略适配层。
  - 集成测试验证策略任务 -> 下单 -> 回执链路。

### 4.7 优化器服务
- 独立 FastAPI 或 CLI 入口，部署为单独容器。
- 使用基础库：
  - 从 Redis 获取策略配置、参数空间、历史绩效。
  - 读取 MariaDB 历史行情作为训练数据。
  - 将优化任务进度写入 `stream:optimizer:events`，结果写 Redis `hash:optimizer:result:<task>`。
- 可选使用自身的任务队列 `list:queue:optimizer`。
- 主程序通过 REST/WebSocket 订阅其事件，展示优化进度。

### 4.8 通知服务
- 监听 `stream:ib:results`、`stream:risk:events`、`stream:strategy:events`。
- 基于配置决定推送渠道；结果记录 MariaDB 以便追踪。

### 4.9 主程序 (ui-gateway)
- 功能：
  - 登录、配置管理、服务监控。
  - 订单、策略、风控状态展示。
  - 日志查看：读取 `data/logs` 文件并分页/搜索；支持实时 tail。
  - Redis 队列/Stream 可视化：展示队列长度、消费速率、最后消费时间。
  - MariaDB 数据写入监控：读取 `service_metrics` 中的写入数量、延迟。
  - 一键重启：通过容器编排（Docker API 或脚本）触发指定服务重启。
- 依赖：仅通过基础库访问 Redis/MariaDB，不直接访问 IB。

## 5. 主程序修改方案
1. **模块化启动**：
  - 移除 `core/__init__` 的全局单例，改为在 FastAPI 启动事件中实例化配置、Redis、MariaDB 客户端。
   - 根据配置向各服务暴露的接口发起健康检查，写入 Redis `hash:service:health`。
2. **API 重写**：
   - 下单类 API -> 调用 `enqueue_ib_command`，立即返回 `command_id`。
   - 策略调度 API -> 向 `queue:strategy` 写任务，并订阅事件流反馈。
   - 行情展示 -> 调用市场数据服务 REST/WS。
3. **监控面板**：
   - 汇总 Redis 队列/Stream 指标（队列长度、滞留时间）。
  - 展示 MariaDB 写入速率（通过 `service_metrics`）。
   - 读取 `data/logs` 并提供下载/搜索。
4. **容器控制**：
   - 集成 Docker Engine API 或 `docker-compose` 脚本，实现服务重启按钮。
5. **配置中心**：
   - UI 表单写入 Redis `hash:config:<module>`；触发 `pubsub:config:update`。
6. **安全与权限**：
  - 服务控制与配置修改需鉴权，预留审计日志记录到 MariaDB。

## 6. 详细开发步骤与验证
### 阶段 0：准备
- 任务：整理 `.env`，创建基础目录 `data/logs`，编写 README。
- 验证：
  - 编写 `tests/config/test_env_loading.py` 测试配置加载。
  - 文档：更新部署说明。

### 阶段 1：基础能力库
1. 实现 `ibbot.config`、`ibbot.redis`、`src.db`、`ibbot.logging`。
2. 提供示例脚本 `scripts/diagnose_foundation.py` 验证连通性。
3. 单元测试：
   - Redis 队列入队/出队、Pub/Sub 自动重连 (`tests/foundation/test_redis.py`)。
  - MariaDB 持久化 mock 测试（参考 `tests/data_layer/test_persistence.py`）。
   - 日志封装写入 `data/logs` (`tests/foundation/test_logging.py`)。
4. 文档：在 `docs/` 增加基础库使用说明。

### 阶段 2：独立服务实现
#### 2.1 IB 指令执行服务
- 编写服务入口 `services/ib_command/main.py`。
- 集成测试：使用 Mock IB 或 sandbox，验证指令执行顺序；在 `tests/services/test_ib_command_flow.py` 编写。
- 文档：更新 README 子章节说明部署与配置。

#### 2.2 IB 数据采集服务
- 入口 `services/ib_feed/main.py`。
- 验证：模拟行情源，断线重连测试；记录 MariaDB 写入速率。
- 单元测试：DOM 序列化、Pub/Sub 发布。

#### 2.3 市场数据服务
- 入口 `services/market_data/main.py`。
- 编写接口测试（FastAPI test client）覆盖 REST/WS。
- 文档：说明订阅流程、API 契约。

#### 2.4 风险控制服务
- 重写规则引擎，编写单元测试覆盖风险判定。
- 集成测试：模拟行情触发止损；验证命令写入队列。

#### 2.5 策略执行服务
- 调整策略接口以适配新任务协议。
- 测试：端到端任务 -> 订单 -> 回执链路。

#### 2.6 优化器服务
- 将现有 `optimizer_main.py` 复用基础库；拆分配置、队列、数据读取。
- 编写测试覆盖任务提交、进度回写。

#### 2.7 通知服务
- 单元测试覆盖事件 -> 推送流程（使用假客户端）。

### 阶段 3：主程序改造
1. 移除旧依赖注入；整合新 API。
2. 开发监控仪表板：
   - Redis 队列视图（读基础库监控 API）。
   - MariaDB 写入视图（查询 `service_metrics`）。
   - 日志查看页面（读 `data/logs`）。
3. 实现服务健康检查与一键重启。
4. 测试：
   - FastAPI 集成测试覆盖新的路由。
   - 前端端到端测试（如 Cypress/Playwright）验证监控页面。

### 阶段 4：联调与回归
- 使用 docker-compose 启动所有服务，验证消息流。
- 编写 `tests/e2e/test_full_pipeline.py` 模拟完整下单流程。
- 文档：
  - 更新 `docs/ib_refactor_design.md` 外的操作手册。
  - 提供故障排查指南。

## 7. 测试方案
- **单元测试**：覆盖基础库、规则引擎、策略适配、消息协议。
- **集成测试**：针对每个服务的关键路径编写，使用 Fake Redis/MariaDB 或 testcontainers。
- **端到端测试**：
  - 场景 1：UI 提交订单 -> Redis 队列 -> IB 指令执行 -> 结果回写 -> UI 显示。
  - 场景 2：行情订阅 -> 市场数据服务 -> 策略消费 -> 风控响应。
  - 场景 3：优化器任务 -> 结果查看。
- **性能测试**：
  - 行情吞吐（ib-feed -> market-data -> UI）。
  - 队列消费速度（策略/IB 指令）。
  - MariaDB 写入速率监控。
- **故障注入**：
  - Redis 断开、MariaDB 不可用、IB 断线；验证自动重连与报警。
- **监控验证**：
  - 主程序监控页面能显示日志、队列、MariaDB 指标，且与实际数据一致。
- **文档验证**：
  - 每个阶段完成后，更新相应的开发文档与测试说明，确保团队可复现。

## 8. 其他注意事项
- 数据层迁移：由 orders/risk/strategy 微服务持久化各自领域数据，核心服务仅维护 Redis/MariaDB 缓存；历史 SQLite 数据可按需离线导入这些服务。
- 每个服务都应打包为独立容器，主程序可以通过 Docker Compose 或 Kubernetes 管理，一键重启调用容器管理 API。
- 兼容旧流程：在新服务稳定前，可保留旧模块的只读模式，逐步切换。
- 安全：对 Redis/MariaDB 使用访问控制，服务间通信需鉴权；日志中避免泄露敏感信息。
- 部署脚本：提供统一的部署/启动脚本 `scripts/deploy.sh`，确保服务按依赖顺序启动。