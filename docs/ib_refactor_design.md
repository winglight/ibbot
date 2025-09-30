# IB Trading Platform Refactor Design

## 1. 背景
- 现有 `main.py`/`core.__init__` 在导入阶段就构造出连接管理、风控、策略、队列、订单、通知、DOM 等所有核心组件，并把它们互相注入依赖。
- 所有组件在同一个事件循环内运行，共享一个 SQLite 数据库连接、单个 IB 客户端、同一个线程池以及内存队列。
- 队列执行、风控监控、行情订阅、订单处理等耗时逻辑直接调用同步 API；一旦某个任务阻塞，整个 FastAPI Web 层也会被拖慢。
- 随着策略数量和 IB 请求量增加，主程序频繁出现响应超时、UI 卡顿甚至死锁，严重影响实盘能力。

## 2. 主要问题
1. **IB 桥接阻塞**：`ConnectionManager.run_in_connection_thread()` 和下单函数通过 `future.result()` 阻塞线程，导致 `asyncio` 事件循环与线程池被占满。
2. **数据库串行化**：SQLite 单连接+互斥锁，查询/写入均在主线程同步执行，队列/风控循环会等待磁盘 IO 完成。
3. **状态耦合**：组件通过内存引用共享状态，任何耗时操作都会阻塞其它逻辑；无法平滑拆分或横向扩容。
4. **无统一异步通信机制**：策略任务、风控事件、行情更新都依赖本地函数调用；缺乏跨进程/跨机器通信的能力。
5. **缺少可靠持久化方案**：高频行情和 DOM 数据落在 SQLite，无法支撑高吞吐；风险、订单、配置等结构化数据缺乏多实例共享能力。

## 3. 总体设计

### 3.1 模块划分
- **IB 网关服务 (ib-gateway-service)**：专职维护与 IB Gateway/TWS 的连接，负责行情订阅、DOM、账户/持仓刷新、订单执行。
- **风险控制服务 (risk-service)**：订阅行情与账户事件，执行风控规则、持仓监控和保护性下单。
- **策略执行服务 (strategy-worker)**：消费策略任务队列，执行长耗时运算及策略下单请求。
- **通知服务 (notification-service)**：处理 Telegram、WebSocket、邮件等外部通知，解耦 IO 阻塞。
- **主控/前端服务 (ui-gateway)**：FastAPI Web/UI 服务，负责配置管理、可视化和任务编排，不直接访问 IB。
- **数据服务 (可选扩展)**：若需要，可引入历史数据回放、报告生成等独立模块。

### 3.2 交互方式
- **Redis**
  - Pub/Sub：行情/DOM/账户更新 (`market:price:<symbol>`、`market:dom:<symbol>` 等)。
  - Stream / List 队列：策略任务队列 (`queue:strategy`)、订单执行命令 (`queue:orders`)、通知任务 (`queue:notifications`)。
  - Hash/Key-Value：运行配置、风控参数、服务健康状态。
- **MariaDB**
  - 保存订单、风控规则、策略配置、执行日志等结构化数据，支持多服务读写。
- **InfluxDB**
  - 持久化 tick、DOM 快照、历史行情等高频时序数据。
- **gRPC/HTTP 健康探针**（选项）：各服务对外暴露健康检查/管理接口，主控服务汇总显示。

### 3.3 基础服务
- Redis 集群或单节点（具备持久化 AOF/快照）。
- MariaDB 高可用实例（或云 RDS）。
- InfluxDB OSS/Cloud（Retention Policy、Downsampling）。
- 日志与追踪：统一采集（例如 Loki/ELK）以及分布式追踪（可选）。

## 4. 独立模块设计

### 4.1 IB 网关服务
- **职责**：
  - 管理 IB API 连接、重连、心跳监控。
  - 统一执行订单请求，串行化处理并写入 MariaDB。
  - 订阅行情/DOM/账户/持仓并推送到 Redis；同步写入 InfluxDB。
- **关键组件**：
  - `IBSessionManager`：封装连接生命周期、心跳、重连策略。
  - `MarketPublisher`：将行情、DOM、账户更新序列化后发布到 Redis/InfluxDB。
  - `OrderExecutor`：消费 `queue:orders`，执行下单/撤单，写入结果 (`orders:<id>`)。
- **接口契约**：
  - `queue:orders` 命令格式包含请求 ID、指令类型、参数（合约、数量、价格、策略标签等）。
  - 处理完成后向 Redis Hash `order:result:<request_id>` 写入结果并发送事件到 `stream:order-events`。

### 4.2 风险控制服务
- **职责**：
  - 订阅 `stream:order-events`、行情/账户 Pub/Sub，维护实时仓位、风险指标。
  - 执行规则（止损、敞口、账户权益等），通过 `queue:orders` 发送保护性操作。
  - 将风控状态写入 Redis (`risk:state`)、MariaDB（日志）。
- **实现要点**：
  - 内置规则引擎，可热加载 Redis Hash `risk:config`。
  - 所有 IB 交互通过队列间接执行，避免直接依赖 IB 客户端。

### 4.3 策略执行服务
- **职责**：
  - 作为消费者监听 `queue:strategy`，运行策略脚本或模型评估。
  - 需要行情数据时订阅 Redis Pub/Sub 或查询 InfluxDB/MariaDB。
  - 生成的交易指令通过 `queue:orders` 提交，等待 `order:result`。
- **实现要点**：
  - 支持并发策略实例，使用协程池或多进程。
  - 提供任务状态回写 (`stream:strategy-events`) 供 UI 订阅。

### 4.4 通知服务
- **职责**：
  - 订阅 `stream:order-events`、`stream:risk-events`、`queue:notifications`。
  - 调用 Telegram、邮件、WebSocket 推送等 IO 操作。
  - 写入 MariaDB（通知日志）并更新 Redis (`notify:status`)。

### 4.5 主控/前端服务
- **职责**：
  - 提供配置管理、监控仪表板、策略/队列调度 API。
  - 所有长耗时操作改为向 Redis 队列发送消息，并通过事件流/回调获知结果。
  - 展示来自 Redis 的实时行情、风控状态、订单执行情况。
- **调整点**：
  - 重写 `core/__init__`，移除全局单例与同步 IB 调用。
  - 将数据库访问层替换为 MariaDB/InfluxDB 客户端。
  - WebSocket 推送改为订阅 Redis 事件。

## 5. 主程序修改方案
1. **依赖注入重构**：
   - 删除导入时初始化的全局单例，改为在 FastAPI 启动时创建 Redis/MariaDB 客户端。
   - `StrategyManager`/`RiskManager` 等只保留业务逻辑，通信依赖 Redis/MariaDB。
2. **队列重写**：
   - 替换 `QueueManager` 内存队列为 Redis Stream/List 封装；提供 ACK、重试、可视化。
3. **订单流程调整**：
   - 下单 API 仅写入 `queue:orders` 并立即返回请求 ID，前端通过事件流追踪。
   - 订单详情从 MariaDB/Redis 查询，不直接访问 IB 客户端。
4. **行情展示**：
   - WebSocket 订阅 Redis Pub/Sub 或 `stream:market-events`，将消息透传给前端。
5. **风控与策略接口**：
   - 读取/修改配置时写入 Redis Hash/MariaDB；触发策略任务通过 `queue:strategy`。
6. **健康监控**：
   - 主程序汇总各服务心跳（Redis Key、HTTP probe），显示在仪表板。

## 6. 开发步骤
1. **基础设施搭建**
   - 部署/配置 Redis、MariaDB、InfluxDB。
   - 编写连接封装与配置管理。
2. **数据层迁移**
   - 抽象数据库访问层；实现 MariaDB ORM/DAO。
   - 将历史行情写入 InfluxDB；提供查询接口。
   - 编写迁移脚本从 SQLite 导出数据到 MariaDB。
3. **Redis 队列/事件框架**
   - 定义消息协议、序列化格式、错误码。
   - 实现 `queue:orders`、`queue:strategy`、事件流封装。
4. **IB 网关服务实现**
   - 改造 `IBKRClient` 适配新异步模型。
   - 实现订单执行器、行情发布器、持久化流程。
   - 增加健康检查、日志。
5. **风控服务重构**
   - 调整 `RiskManager` 使用 Redis/MariaDB。
   - 实现订阅/规则执行、保护性订单下发。
6. **策略执行服务重构**
   - 改写 `StrategyManager` 任务流，适配 Redis 队列。
   - 引入任务状态回写机制。
7. **通知服务抽离**
   - 把 `NotificationManager` 改为消费队列/事件。
8. **主控服务改造**
   - 重写依赖注入、API、WebSocket。
   - 接入新的数据源与事件通道。
9. **清理与文档**
   - 移除旧单例、线程阻塞代码。
   - 补充运维、部署、监控文档。

### 里程碑建议
- **M1**：数据层迁移 + Redis 队列基础，保持旧 IB 流程可运行。
- **M2**：IB 网关服务上线，主程序改用消息驱动下单/行情。
- **M3**：风控/策略/通知服务独立，完成整体解耦。
- **M4**：主控服务简化完成，所有功能通过新架构运行。

## 7. 测试方案
1. **单元测试**
   - Redis 队列协议解析、ACK/重试逻辑。
   - MariaDB DAO、InfluxDB 写入/查询。
   - 风控规则、策略决策核心函数。
2. **集成测试**
   - 使用容器化 IB 模拟器或回测环境，验证订单流与行情发布。
   - 端到端测试：UI 提交订单 -> Redis -> IB 服务 -> 订单结果事件 -> UI。
   - 风控触发测试：构造行情事件验证保护订单下发。
3. **性能测试**
   - 压测 Redis 队列和 MariaDB 写入吞吐。
   - 高频行情写入 InfluxDB 的负载测试。
4. **故障注入**
   - 模拟 IB 断线、Redis 网络中断、数据库异常，验证重试和降级策略。
5. **回归测试**
   - 核对策略绩效、订单记录、通知是否与旧系统一致。
6. **部署验证**
   - 在预生产环境运行全套服务 24 小时，检查资源消耗和日志。

