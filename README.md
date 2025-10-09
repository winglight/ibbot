# IBKR Trading Bot

IBBot 是一个围绕 Interactive Brokers API 构建的全栈交易与风险管理平台。系统涵盖连接管理、行情聚合、订单与风险控制、DOM 深度分析、策略调度、通知推送以及 Web/接口访问，旨在提供可扩展的自动化交易基础设施。

## 🌟 核心能力总览

| 模块 | 能力亮点 |
| --- | --- |
| 连接管理 | 统一的 TWS/Gateway 配置、连接超时保护、错误缓存与状态广播 |
| 行情中心 | 历史/实时行情接口、价格流缓存、DOM 深度流与统计分析 |
| 订单执行 | 市价/限价/撤单/反手、风控校验、批量取消与佣金补录 |
| 风险控制 | 风险规则 CRUD、ATR 跟踪、动态仓位、断路器与策略风控联动 |
| 策略系统 | 内置策略生命周期管理、外部策略 API 入队、任务队列与执行历史 |
| 通知中心 | 浏览器/Telegram 多通道通知、事件筛选、测试推送与历史查询 |
| 队列与快照 | FIFO 异步任务队列、心跳监控、账户快照与 WebSocket 广播 |
| 运维支持 | Docker 编排、VPS 快速部署脚本、Cloudflare Tunnel 示例 |

更多细化条目请参阅 [docs/features.csv](docs/features.csv)。

## 🧱 架构速览

```
                  ┌──────────────────┐
                  │ FastAPI (main)   │
                  └──────┬───────────┘
                         │ REST / WebSocket
                         ▼
            ┌────────────────────────────┐
            │ Service Routers & Clients  │
            └────┬──────────┬────────────┘
                 │          │
        ┌────────▼───┐  ┌───▼────────┐
        │Account svc │  │Orders svc  │
        └────┬───────┘  └────┬───────┘
             │               │
             │               │
        ┌────▼────────┐  ┌───▼────────────┐
        │Market data  │  │Strategy svc    │
        └────┬────────┘  └────┬───────────┘
             │                 │
             ▼                 ▼
        Redis streams & typed IB event models
```

- **services/** 目录下的 account/orders/market_data/strategy 微服务各自维护 `ServiceIBClient`，直接与 IB Gateway/TWS 建立连接并将
  事件发布至 Redis。【F:services/account/ib_client.py†L20-L59】【F:services/orders/ib_client.py†L16-L148】【F:services/market_data/ib_client.py†L34-L115】
- **src/routes/** 汇总主进程 API，包括 `/ib/channels` 在内的服务发现元数据以及连接控制端点。【F:src/routes/ib.py†L1-L171】
- **src/ib/** 提供共享的事件模型与 Redis 发布工具，保证跨服务的消息结构一致。【F:src/ib/events.py†L1-L305】【F:src/ib/redis_streaming.py†L1-L162】
- **core/**、`src/dom_*` 与 `src/optimizer_service/` 仍负责策略、风控、DOM 分析等计算型能力，可通过 Redis 通道消费各服务输出。
- **src/config/** 统一加载 `.env` 与 `.env.local`，供 CLI、脚本与微服务复用配置。

### 🗄️ 持久化模型

订单、风控、策略等领域数据现由独立的微服务负责持久化与回放。网关进程只通过 HTTP 客户端和 Redis/事件流访问这些服务，不再依赖本地
SQLite 文件（历史的 `data/trading_bot.db` 已移除）。这意味着：

- 任何状态写入都通过对应的服务 API 或 Redis 键完成，网关自身保持无状态。
- 本地开发与 CI 无需准备 SQLite 数据库文件；所有测试默认断言不会创建 `data/trading_bot.db`。
- 迁移脚本应直接与 orders/risk/strategy 服务对接，而不是向网关注入数据库连接。

### 📦 MariaDB 数据仓库

- 设置 `IBBOT_DB_URL`（或兼容的 `DATA_LAYER_DB_URL`）为 `mysql+asyncmy://` 形式以启用 MariaDB 持久化。
- `services/orders`、`services/strategy` 与数据层会在启动时自动检查并创建缺失的必需表（使用 `scripts/sql/db.sql`）。若需手动兜底，可运行 `python -m src.db.bootstrap` 或直接执行该 SQL 脚本。
- 如需从旧版 SQLite (`data/trading_bot.db`) 导入订单、风控和配置数据，可执行 `python scripts/sqlite_to_mariadb.py`（支持传入 `--tables` 指定子集）。
- 所有仓储工厂统一由 `src.db` 模块暴露，例如 `from src.db import order_repository`；旧的 `src.ibbot.db` 包已废弃并在导入时抛出迁移提示（详见 [docs/db_api_migration.md](docs/db_api_migration.md)）。
- 生产环境建议使用 Alembic 管理迁移：在项目根目录执行 `alembic upgrade head` 会根据 `IBBOT_DB_URL` 中的数据库应用最新结构。

## 📁 基础目录结构

```text
.
├── .env.example             # Template defaults copied to `.env` / `.env.local`
├── data/
│   ├── .gitkeep             # 保留目录结构，实际数据按需生成
│   └── logs/                # 运行期日志输出位置
├── docker/                  # Dockerfile 与 Compose 定义
├── src/
│   ├── config/              # 环境变量加载与配置辅助
│   ├── routes/              # REST / WebSocket 路由
│   ├── data_layer/          # 行情与持久化抽象
│   └── utils/               # 公共工具、缓存、策略辅助
├── static/                  # 前端静态资源
├── optimizer_ui/            # 优化器 Web UI 构建产物
└── vps-setup/               # VPS 部署与诊断脚本
```

- `data/` 下的内容在部署时自动生成，`data/logs/` 会被 Docker 与本地脚本复用。
- `.env.example` 提供共享默认值模板；将其复制为 `.env`（团队共用）或 `.env.local`（私有覆写）。

## 🚀 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

根目录提供 `.env.example` 模板，包含全部默认值及注释。首次启动前建议：

```bash
cp .env.example .env        # 团队共享的基础默认值
cp .env.example .env.local  # 本地覆写，保存凭据与私有配置
```

`src.config.load_project_env` 会同时加载 `.env` 与 `.env.local`（若存在），因此请将敏感配置写入 `.env.local` 并按需覆盖 `.env` 中的公共默认值。

常用环境变量速查表（更多细节请参阅 [docs/configuration.md](docs/configuration.md)）：

| 变量 | 用途 | 关联模块/服务 |
| --- | --- | --- |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `JWT_SECRET` | 配置后台登录与 JWT 密钥 | Web API |
| `VISITOR_USERNAME` / `VISITOR_PASSWORD` | 可选访客账号，提供只读界面 | Web API |
| `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` / `IBKR_PAPER_TRADING` | 主进程连接控制器使用的默认 IB Gateway 参数 | 连接管理 |
| `ACCOUNT_IB_HOST` / `ACCOUNT_IB_PORT` / `ACCOUNT_IB_CLIENT_ID` | Account 服务专用的 IB Gateway 连接参数 | `services/account` |
| `ORDERS_IB_HOST` / `ORDERS_IB_PORT` / `ORDERS_IB_CLIENT_ID` | Orders 服务专用的 IB Gateway 连接参数 | `services/orders` |
| `MARKET_DATA_IB_HOST` / `MARKET_DATA_IB_PORT` / `MARKET_DATA_IB_CLIENT_ID` | Market Data 服务专用的 IB Gateway 连接参数 | `services/market_data` |
| `MARKET_DATA_TYPE` / `MARKET_DATA_DELAYED_FALLBACK` / `MARKET_DATA_MAX_SUBSCRIPTIONS` | 控制行情类型、延迟兜底与并发订阅上限 | `services/market_data` |
| `STRATEGY_DEFAULT_REQUIRED_CHANNELS` / `STRATEGY_DEFAULT_CHANNEL_MAP` / `STRATEGY_DEFAULT_LATENCY_BUDGET_MS` / `STRATEGY_USE_DELAYED_DATA` | 策略服务默认订阅与延迟预算 | `services/strategy` |
| `AUTO_CONNECT` / `IBBOT_DISABLE_TRADING` | 控制启动时是否自动连接 IBKR、是否禁用交易 | Web API、策略/订单模块 |
| `IBBOT_REDIS_URL` | 全局 Redis 连接串，未单独指定时供各服务共用 | 全部后台服务 |
| `ACCOUNT_SERVICE_URL` / `ACCOUNT_SERVICE_TIMEOUT` | 账户服务 HTTP 接入点，默认 `http://localhost:8020` | `services/account` |
| `STRATEGY_REDIS_URL` / `NOTIFICATION_REDIS_URL` 等 | 覆盖对应微服务的 Redis 连接 | `strategy`、`notification` 等 |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | 当使用分散配置时拼接 Redis 地址 | Redis 客户端封装 |
| `DATA_LAYER_*`、`TELEGRAM_*` | 行情落地、通知推送等可选能力 | Market Data、Notification |

### 3. 启动服务

```bash
uvicorn main:app --reload
```

- 首页：http://localhost:8000/
- 自动生成的 RapiDoc：http://localhost:8000/rapidoc
- OpenAPI JSON：http://localhost:8000/api-docs.json

默认不会自动连接 IBKR，可通过前端连接面板或 `/api/connect` 进行连接。

### 3.1 直接运行后台服务（可选）

`scripts/run_service.py` 与 `scripts/run_all.py` 提供了“开箱即用”的服务启动封装，执行前会自动加载 `.env` 与 `.env.local` 配置。

```bash
# 检查某个服务是否可以直接启动（不会连接外部依赖）
python scripts/run_service.py strategy --dry-run

# 实际运行单个服务（等同于 python -m services.strategy.http:app）
python scripts/run_service.py strategy

# 批量启动核心服务，默认包含 connection_control/account/orders/strategy/risk/notification
python scripts/run_all.py

# 仅做导入与环境自检，可自定义服务组合
python scripts/run_all.py strategy risk --dry-run

# 在需要逐一排查服务时，也可以单独执行 dry-run
python scripts/run_all.py --dry-run strategy
```

> ⚠️ 主进程依赖 `services/account` 与 `services/orders` 提供账户/订单接口，请优先启动这两个服务（示例：`uvicorn services.account.main:app`、`uvicorn services.orders.main:app`）。Docker Compose 与部署脚本已调整为在启动 `ibbot` 之前拉起相关服务。

上述命令无需额外设置 `PYTHONPATH`，直接使用 `python scripts/run_all.py ...` 即可完成导入检测与服务启动。

常见服务的外部依赖如下表所示：

### 4. 验证 IB API 控制器流程

官方 IB API 现为默认运行时依赖，建议在接入或升级后执行以下自检步骤：

1. **运行测试套件**：`pytest` 会调用内置的 `IBAPIController` stub，覆盖行情、下单、账户、连接管理等核心路径，确保在没有 `ib_insync` 的环境下也可通过。
2. **执行诊断脚本**：运行 `python diagnose_connection.py`，脚本会基于 `IBAPIController` 逐个尝试客户端 ID，验证与 TWS/Gateway 的 TCP 连接、握手与数据库配置。
3. **检查运行日志**：`src/connection_manager` 会在启动时输出 `IBKR connections handled exclusively via IBAPIController`，确认主进程没有再尝试加载 `ib_insync` 兼容层。
4. **生产前验证**：在仿真或正式账户中启动主服务后，通过前端连接面板或 `/api/connect` 触发连接，确认市场数据、订单与账户快照均能更新；所有 IB 交互均通过官方 TWS API 完成。

| 服务模块 | 启动命令 | 主要依赖 | 说明 |
| --- | --- | --- | --- |
| `account` | `uvicorn services.account.main:app --port 8020` | Redis（账户快照缓存） | 提供账户 REST 接口，默认监听 `8020` 端口，请在主进程前启动。 |
| `orders` | `uvicorn services.orders.main:app --port 8050` | Redis（命令队列/健康上报） | 提供下单、撤单与仓位指令 REST 接口，默认监听 `8050` 端口。 |
| `strategy` | `uvicorn services.strategy.http:app --port 8045` | Redis（策略调度/事件流） | 提供策略管理 REST 接口并维护任务队列。 |
| `risk` | `uvicorn services.risk.http:app --port 8030` | Redis（风险配置） | 提供风险配置 REST 接口并刷新风控运行时。 |
| `notification` | `python scripts/run_service.py notification` | Redis（事件队列） | 推送通知到浏览器/Telegram。 |
| `optimizer` | `python scripts/run_service.py optimizer` | Redis、MariaDB | 需要历史数据（MariaDB）与事件流（Redis）。 |

#### 4.1 验收与长时回归验证

- **自动化验收套件**：`pytest tests/acceptance/test_refactor_acceptance.py -q` 会模拟 Account/Orders/Market Data 服务的 Redis 推送并验证策略事件流是否收到全部频道的数据。
- **长时 Soak 验证**：`python scripts/refactor_soak.py --iterations 6 --duration 30` 在真实 IB Gateway/TWS 会话中循环执行集成采集，生成 `summary.json` 与 `iteration_*.json` 记录每轮消息数。若 `summary.json` 的 `failures` 数组为空，即表示所有频道在测试窗口内持续产出数据。

#### 订单服务持久化更新

自 2024-08 起，订单服务的历史/活跃订单全部持久化在数据库中。Redis 仅承担命令队列与健康状态上报，不再缓存订单快照：

- `services/orders` 在启动时会直接从数据库仓储初始化内存状态，不会再尝试加载 Redis 快照或写入 `json:orders:*` 键。
- 主程序、账户服务与 WebSocket 路由均通过 `OrdersServiceClient` 查询订单，避免绕过服务层访问 Redis。

部署升级后建议执行一次 Redis 清理，移除旧版快照键防止遗留数据被误用：

1. 连接到运行时 Redis 实例（可使用 `redis-cli` 或可视化工具）。
2. 检查是否存在 `json:orders:*`、`hash:orders:*` 或 `stream:orders_snapshot` 等历史键：
   ```bash
   redis-cli --scan --pattern 'json:orders:*'
   redis-cli --scan --pattern 'hash:orders:*'
   ```
3. 对确认不再使用的键执行删除，可结合 `xargs` 一次性清理：
   ```bash
   redis-cli --scan --pattern 'json:orders:*' | xargs -r redis-cli del
   redis-cli --scan --pattern 'hash:orders:*' | xargs -r redis-cli del
   ```
4. 重启 `services/orders` 与主程序，确认订单列表数据来自数据库且健康指标正常刷新。

### Redis 健康键约定

| Redis 键 | 默认来源 | 主要字段 |
| --- | --- | --- |
| `hash:service:health:account-service` | 账户服务（`ACCOUNT_SERVICE_HEALTH_KEY`） | `healthy`、`last_error`、`last_seen`、快照时间戳 |
| `hash:service:health:orders` | 订单服务（`ORDERS_SERVICE_HEALTH_KEY`） | `healthy`、`last_seen`、`active_orders` |
| `hash:service:health:risk-service` | `risk` 服务（`RISK_SERVICE_HEALTH_KEY`） | `status`、`last_seen`、`last_error` |
| `hash:service:health:strategy` | `strategy` 服务（`STRATEGY_SERVICE_HEALTH_KEY`） | `status`/`healthy`、`last_error`、`last_seen` |

> 注：历史键 `hash:service:health:ib-command` 不再更新，相关健康状态由各服务本地指标取代。
| `hash:service:health:notification` | 通知服务（`NOTIFICATION_SERVICE_HEALTH_KEY`） | `healthy`、`last_error`、`pending_retries` |
| `hash:service:health:queue` | 队列监控守护进程（`QUEUE_MONITOR_HEALTH_KEY`） | `status`、`last_seen`、聚合队列指标 |
| `hash:service:queue-monitor` | `queue_monitor` 服务 | `timestamp`、队列统计字段 |

这些健康键由 `src/service_status.py` 汇总，前端“Service Health”与连接状态接口均依赖其结构。新增的 `last_error_code` 字段会出现在 ib-command 健康记录与 `/api/connection_status` 响应中，用于暴露最新的 IBKR 错误码。

若本地尚未安装 Redis/MariaDB，可使用最小化的 docker compose 组合快速拉起：

```bash
docker compose up redis mariadb
```

完成后，服务脚本会复用 `.env`/`.env.local` 中声明的连接配置。

### 4. 基本操作

1. 调用 `/api/login` 获取 JWT，前端也提供登录面板。
2. 在“Connection”面板设置主机/端口后点击 Connect，或调用 `/api/connect`。
3. 使用行情面板或 `/api/realtime_data/{symbol}`、`/api/historical_data` 拉取数据。
4. 在订单面板或 `/api/place_order` 下单，并使用队列/日志面板观察执行情况。
5. 通过风险、策略、DOM 面板调整配置；所有操作均有对应 API。

## 📡 WebSocket 与通知

- `/ws`：推送订单、账户、策略、队列等事件。
- `/ws/dom/{symbol}`：推送特定标的的 DOM 深度与指标。
- Notification Center 支持浏览器与 Telegram 通道，配置接口为 `/api/notifications/settings`。

### 前端连接状态契约

前端“Connection”面板与 WebSocket 首条 `connection_status` 消息共用同一响应结构：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `connected` | `bool` | 当前是否已建立可用的 ib-command 连接。 |
| `account_type` | `str` | “Paper”/“Live”等账户标签，受 `IB_COMMAND_ACCOUNT_TYPE`、`IB_ACCOUNT_TYPE` 控制。 |
| `queue_depth` | `int` \| `null` | ib-command 队列深度，未知时返回 `null`。 |
| `last_seen` | `str` \| `null` | 最近一次健康心跳的 ISO8601 时间戳。 |
| `last_error` | `str` \| `null` | 最近错误描述，无错误时为 `null`。 |
| `error_code` | `int` \| `null` | 最近一次 IBKR 错误码（新增字段），用于前端提示具体拒单/断连原因。 |
| `stale` | `bool` | 健康数据是否已过期。 |
| `source` | `str` | 数据来源，`redis`/`http` 等。 |

当 Redis 健康键缺失时，接口会返回 `connected=false`、`stale=true`、`error_code=null` 以及默认的 `last_error` 提示，前端可据此展示“等待 ib-command 服务”的状态。

## 🧪 测试

更新后的测试用例覆盖 JWT 登录、连接状态（含 `error_code` 字段）、通知配置与 DOM 配置。运行方式：

```bash
pytest

npm test
```

`pytest` 会注入隔离的服务桩并跳过真实 IBKR/Redis 依赖。`npm test` 运行 Playwright 端到端脚本（默认检查静态前端契约），首次使用前请执行 `npm install` 安装依赖。

- 若需验证 Redis 流与各服务订阅是否按预期工作，可执行 `python scripts/refactor_integration_check.py`。脚本会串行调用账户/订单/行情服务的订阅端点，并在指定时间窗内抓取 Redis 消息写入 `data/regression/ib_stream_baseline.json` 以便回归比对。【F:scripts/refactor_integration_check.py†L1-L165】

## 🐳 Docker 与远程部署

- [docker/readme.md](docker/readme.md)：本地/生产 compose、镜像推送与 Cloudflare Tunnel。
- [vps-setup/README.md](vps-setup/README.md)：在 VPS 上一键安装依赖、配置 SSH 隧道与 Docker 上下文。

### Compose 服务矩阵与 Profiles

| Compose 服务 | 后台启动命令 | Profile | 角色说明 |
| --- | --- | --- | --- |
| `ibbot` | `gunicorn main:app`（容器入口） | _默认_ | Web API 与前端静态资源。 |
| `market-data` | `python -m uvicorn services.market_data.main:app --host 0.0.0.0 --port 8010` | `market` | 聚合行情快照，暴露 REST/WebSocket（默认端口 8010）。 |
| `risk` | `python -m services.risk.main` | `risk` | 消费行情/执行结果流，运行风险引擎并触发保护动作。 |
| `strategy` | `python -m services.strategy.main` | `strategy` | 执行策略任务、写入事件流并下发 IB 指令。 |
| `notification` | `python -m services.notification.main` | `notifications` | 订阅事件流并广播到浏览器/Telegram。 |
| `queue-monitor` | `python -m uvicorn services.queue_monitor.main:create_app --host 0.0.0.0 --port 8011` | `monitoring` | 统计 Redis 队列/Stream 指标（默认端口 8011）。 |
| `optimizer` | `python optimizer_main.py` | _默认_ | DOM 优化任务调度与进度跟踪。 |
| `optimizer-ui` | `nginx` 静态服务 | _默认_ | 暴露优化器前端（默认端口 4173）。 |
| `cloudflared` | `cloudflared tunnel run` | `tunnel` | 按需暴露公网隧道，需要 `CF_TUNNEL_TOKEN`。 |

> 提示：所有后台服务默认挂载 `data/`、`data/logs/`，方便在主机查看运行日志或健康快照。

在本地或生产环境选择性启用服务：

- **快速全量启动**：`./scripts/deploy.sh up`（本地）或 `./scripts/deploy.sh up --env production`。
- **通过 Profile 精简组合**：设置 `COMPOSE_PROFILES` 环境变量，例如：
  - 仅联调行情链路：`COMPOSE_PROFILES="ib-services,market" ./scripts/deploy.sh up`。
  - 生产环境禁用通知与监控：`COMPOSE_PROFILES="ib-services,market,risk,strategy" ./scripts/deploy.sh up --env production`。
  - 使用原生命令：`COMPOSE_PROFILES=monitoring docker compose --profile monitoring up queue-monitor`。

未在 `COMPOSE_PROFILES` 中声明的 Profile 将被跳过；手动指定服务名（例如 `docker compose up market-data`）仍可单独拉起对应容器。

## 📚 更多资料

- DOM 服务与数据：`docs/dom_optimizer_guide.md`、`src/dom_service.py`。
- 风险管理设计：`docs/策略级风控模块设计.docx`、`src/risk_manager.py`。
- 手工测试指引：`docs/tests.md`。
- 基础依赖配置与诊断：`docs/foundation_libs.md`。

如需扩展策略或接入外部系统，请参考 `src/strategies/readme.md` 与 `/api/strategy` 相关接口。
