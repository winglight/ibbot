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
┌───────────────┐     ┌─────────────────────┐
│ FastAPI (main)│◄────┤ JWT Auth / Routers  │
└─────┬─────────┘     └─────────┬───────────┘
      │                          │
      ▼                          ▼
┌──────────────┐      ┌────────────────────┐
│ core/* 单例 │      │ Queue / Risk / DOM │
│ queue_manager│      │ Order / Strategy   │
└──────┬───────┘      └─────────┬──────────┘
       │                        │
       ▼                        ▼
┌──────────────┐      ┌────────────────────┐
│ IBKR Client  │      │ Services / Cache   │
│ (ib_insync)  │      │ HTTP APIs + Redis  │
└──────────────┘      └────────────────────┘
```

- **core/** 初始化共享实例（通知、策略、风险、队列、DOM、ATR 等；订单流由独立的 `services/orders` 提供）。
- **src/routes/** 按功能拆分的 REST/WebSocket 接口；`docs/api_doc.json` 自动从 OpenAPI 导出。
- **src/dom_*** 深度行情分析、参数调优与自动化调度。
- **src/optimizer_service/** 提供 DOM 优化任务编排与异步执行。
- **src/config/** 统一加载 `.env` 与 `.env.local`，供 CLI 与服务端脚本复用。

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

常用环境变量速查表：

| 变量 | 用途 | 关联模块/服务 |
| --- | --- | --- |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `JWT_SECRET` | 配置后台登录与 JWT 密钥 | Web API |
| `VISITOR_USERNAME` / `VISITOR_PASSWORD` | 可选访客账号，提供只读界面 | Web API |
| `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` / `IBKR_PAPER_TRADING` | IBKR 连接主机、端口、客户端 ID 与交易环境 | 连接管理、`ib_command` |
| `AUTO_CONNECT` / `IBBOT_DISABLE_TRADING` | 控制启动时是否自动连接 IBKR、是否禁用交易 | Web API、策略/订单模块 |
| `IBBOT_REDIS_URL` | 全局 Redis 连接串，未单独指定时供各服务共用 | 全部后台服务 |
| `ACCOUNT_SERVICE_URL` / `ACCOUNT_SERVICE_TIMEOUT` | 账户服务 HTTP 接入点，默认 `http://localhost:8020` | `services/account` |
| `IB_COMMAND_REDIS_URL` / `IB_FEED_REDIS_URL` / `STRATEGY_REDIS_URL` 等 | 覆盖对应微服务的 Redis 连接 | `ib_command`、`ib_feed`、`strategy`、`notification` 等 |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | 当使用分散配置时拼接 Redis 地址 | Redis 客户端封装 |
| `IB_COMMAND_HEALTH_POLL_INTERVAL_SEC` / `IB_COMMAND_HEALTH_STALE_AFTER_SEC` | 控制 ib-command 健康检查的轮询频率与过期阈值 | 主进程、`ib_command` |
| `IB_COMMAND_ACCOUNT_TYPE` / `IB_ACCOUNT_TYPE` | 覆写连接状态响应中的账户类型标签 | Web API |
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
python scripts/run_service.py ib_feed --dry-run

# 实际运行单个服务（等同于 python -m services.ib_feed.main）
python scripts/run_service.py ib_feed

# 批量启动核心服务，默认包含 connection_control/account/orders/ib_command/ib_feed/strategy/risk/notification
python scripts/run_all.py

# 仅做导入与环境自检，可自定义服务组合
python scripts/run_all.py ib_feed strategy --dry-run

# 在需要逐一排查服务时，也可以单独执行 dry-run
python scripts/run_all.py --dry-run ib_command
```

> ⚠️ 主进程依赖 `services/account` 与 `services/orders` 提供账户/订单接口，请优先启动这两个服务（示例：`uvicorn services.account.main:app`、`uvicorn services.orders.main:app`）。Docker Compose 与部署脚本已调整为在启动 `ibbot` 之前拉起相关服务。

上述命令无需额外设置 `PYTHONPATH`，直接使用 `python scripts/run_all.py ...` 即可完成导入检测与服务启动。

常见服务的外部依赖如下表所示：

| 服务模块 | 启动命令 | 主要依赖 | 说明 |
| --- | --- | --- | --- |
| `account` | `uvicorn services.account.main:app --port 8020` | Redis（账户快照缓存） | 提供账户 REST 接口，默认监听 `8020` 端口，请在主进程前启动。 |
| `orders` | `uvicorn services.orders.main:app --port 8050` | Redis（命令队列/健康上报） | 提供下单、撤单与仓位指令 REST 接口，默认监听 `8050` 端口。 |
| `ib_command` | `python scripts/run_service.py ib_command` | Redis（任务队列） | 可选对接 IB Gateway/TWS，使用 `--dry-run` 可跳过连接。 |
| `ib_feed` | `python scripts/run_service.py ib_feed` | Redis（行情缓存） | 连接 IBKR 抓取实时/历史行情。 |
| `strategy` | `uvicorn services.strategy.http:app --port 8045` | Redis（策略调度/事件流） | 提供策略管理 REST 接口并维护任务队列。 |
| `risk` | `uvicorn services.risk.http:app --port 8030` | Redis（风险配置） | 提供风险配置 REST 接口并刷新风控运行时。 |
| `notification` | `python scripts/run_service.py notification` | Redis（事件队列） | 推送通知到浏览器/Telegram。 |
| `optimizer` | `python scripts/run_service.py optimizer` | Redis、MariaDB | 需要历史数据（MariaDB）与事件流（Redis）。 |

### Redis 健康键约定

| Redis 键 | 默认来源 | 主要字段 |
| --- | --- | --- |
| `hash:service:health:ib-command` | `ib_command` 服务（可由 `IB_COMMAND_HEALTH_KEY` 覆写） | `queue_depth`、`last_seen`、`last_error`、`last_error_code`、`stale` |
| `hash:service:health:account-service` | 账户服务（`ACCOUNT_SERVICE_HEALTH_KEY`） | `healthy`、`last_error`、`last_seen`、快照时间戳 |
| `hash:service:health:orders` | 订单服务（`ORDERS_SERVICE_HEALTH_KEY`） | `healthy`、`last_seen`、`active_orders` |
| `hash:service:health:risk-service` | `risk` 服务（`RISK_SERVICE_HEALTH_KEY`） | `status`、`last_seen`、`last_error` |
| `hash:service:health:strategy` | `strategy` 服务（`STRATEGY_SERVICE_HEALTH_KEY`） | `status`/`healthy`、`last_error`、`last_seen` |
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

## 🐳 Docker 与远程部署

- [docker/readme.md](docker/readme.md)：本地/生产 compose、镜像推送与 Cloudflare Tunnel。
- [vps-setup/README.md](vps-setup/README.md)：在 VPS 上一键安装依赖、配置 SSH 隧道与 Docker 上下文。

### Compose 服务矩阵与 Profiles

| Compose 服务 | 后台启动命令 | Profile | 角色说明 |
| --- | --- | --- | --- |
| `ibbot` | `gunicorn main:app`（容器入口） | _默认_ | Web API 与前端静态资源。 |
| `ib-command` | `python -m services.ib_command.main` | `ib-services` | 消费指令队列、执行 IBKR 命令、回写执行结果。 |
| `ib-feed` | `python -m services.ib_feed.main` | `ib-services` | 订阅 IBKR 行情、发布价格/DOM、写入 MariaDB。 |
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

- DOM 服务与调优：`docs/dom_optimizer_guide.md`、`src/dom_service.py`。
- 风险管理设计：`docs/策略级风控模块设计.docx`、`src/risk_manager.py`。
- 手工测试指引：`docs/tests.md`。
- 基础依赖配置与诊断：`docs/foundation_libs.md`。

如需扩展策略或接入外部系统，请参考 `src/strategies/readme.md` 与 `/api/strategy` 相关接口。
