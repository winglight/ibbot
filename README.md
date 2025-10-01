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
│ CoreContainer│      │ Queue / Risk / DOM │
│ queue_manager│      │ Order / Strategy   │
└──────┬───────┘      └─────────┬──────────┘
       │                        │
       ▼                        ▼
┌──────────────┐      ┌────────────────────┐
│ IBKR Client  │      │ Database / Cache   │
│ (ib_insync)  │      │ SQLite + Redis(opt)│
└──────────────┘      └────────────────────┘
```

- **core/** 提供 `CoreContainer` 单例，按需装配 Redis、配置提供者、WebSocket 管理器等 UI 依赖。
- **src/routes/** 按功能拆分的 REST/WebSocket 接口；`docs/api_doc.json` 自动从 OpenAPI 导出。
- **src/dom_*** 深度行情分析、参数调优与自动化调度。
- **src/optimizer_service/** 提供 DOM 优化任务编排与异步执行。
- FastAPI 主程序通过 `get_core_container()` 访问 Redis、Influx 持久化入口与配置提供者，替代旧的全局管理器引用。

## 🚀 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

复制模板后按需修改：

```bash
cp .env.example .env
```

关键变量：

- `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `JWT_SECRET`：后台认证与安全。
- `VISITOR_USERNAME` / `VISITOR_PASSWORD`：可选访客只读账号。
- `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` / `IBKR_PAPER_TRADING`：IBKR 连接配置。
- `AUTO_CONNECT=false`：开发阶段建议禁用自动连接。
- `PNL_BROADCAST_INTERVAL_SEC`、`ACCOUNT_BROADCAST_INTERVAL_SEC`：WebSocket 广播节奏。
- `ENABLED_MODULES`：控制 FastAPI lifespan 启动的后台模块，默认 `ALL`（即 `auto_connect` 与 `data_subscriptions` 均启用）。
- Redis 可选：`REDIS_URL` 或 `REDIS_HOST/PORT/DB`。

### 3. 启动服务

```bash
uvicorn main:app --reload
```

- 首页：http://localhost:8000/
- 自动生成的 RapiDoc：http://localhost:8000/rapidoc
- OpenAPI JSON：http://localhost:8000/api-docs.json

默认不会自动连接 IBKR，可通过前端连接面板或 `/api/connect` 进行连接。

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

## 📈 市场数据服务

- **REST API**：默认部署在 `http://localhost:8010`，提供 `/market`（全部订阅快照）、`/market/{symbol}`（单标的快照）与 `/health`（运行状态）等接口。主程序可通过 `MARKET_DATA_SERVICE_URL` 指向容器内地址（示例：`http://market-data-service:8010`）。
- **WebSocket**：`ws://localhost:8010/ws/market`，支持通过查询参数 `symbols=AAPL,MSFT`、`types=dom.metrics` 过滤；连接建立后也可发送 `{ "type": "subscribe", "symbols": ["MNQ"] }` 动态调整订阅。将 `MARKET_DATA_SERVICE_WS_URL` 指向 `ws://market-data-service:8010/ws/market` 后，前端即可复用原有实时面板。
- **Redis Stream**：归一化行情事件写入 `MARKET_DATA_STREAM_KEY`（默认 `stream:market-data`），字段包含 `symbol`、`type`、`data`、`ts_ns` 与 `latency_ms`。可结合现有队列监控面板展示流速与滞留。
- **依赖**：需 Redis（读取订阅配置、推送 Stream）与可选 InfluxDB（写入 `service_metrics` 指标）。对应环境变量如 `MARKET_DATA_STREAM_LENGTH`、`MARKET_DATA_METRICS_PERIOD`、`MARKET_DATA_METRICS_INFLUX_URL` 已加入 `.env.example`。
- **运维**：健康检查 `GET /health` 返回最近一次推送时间、Redis/Influx 写入状态与账户快照合并情况；日志默认写入 `logs/market-data-service.log`（也会输出到 stdout），指标可在 Influx 中查看 `service.<service_name>` 主题。

> 更多部署细节与 API 契约见 [docs/market_data_service.md](docs/market_data_service.md)。

## 🧪 测试

更新后的测试用例覆盖 JWT 登录、连接状态、通知配置与 DOM 配置。运行方式：

```bash
pytest
```

测试会自动生成独立的 SQLite 文件并跳过真实 IBKR/Redis 依赖。

## 🐳 Docker 与远程部署

- [docker/readme.md](docker/readme.md)：本地/生产 compose、镜像推送与 Cloudflare Tunnel。
- [vps-setup/README.md](vps-setup/README.md)：在 VPS 上一键安装依赖、配置 SSH 隧道与 Docker 上下文。

## 📚 更多资料

- DOM 服务与调优：`docs/dom_optimizer_guide.md`、`src/dom_service.py`。
- 风险管理设计：`docs/策略级风控模块设计.docx`、`src/risk_manager.py`。
- 手工测试指引：`docs/tests.md`。

如需扩展策略或接入外部系统，请参考 `src/strategies/readme.md` 与 `/api/strategy` 相关接口。
