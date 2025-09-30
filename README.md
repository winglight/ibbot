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
│ IBKR Client  │      │ Database / Cache   │
│ (ib_insync)  │      │ SQLite + Redis(opt)│
└──────────────┘      └────────────────────┘
```

- **core/** 初始化共享实例（通知、策略、风险、订单、队列、DOM、ATR 等）。
- **src/routes/** 按功能拆分的 REST/WebSocket 接口；`docs/api_doc.json` 自动从 OpenAPI 导出。
- **src/dom_*** 深度行情分析、参数调优与自动化调度。
- **src/optimizer_service/** 提供 DOM 优化任务编排与异步执行。
- **src/config/** 统一加载 `.env` 与 `.env.local`，供 CLI 与服务端脚本复用。

## 📁 基础目录结构

```text
.
├── .env                     # Canonical defaults loaded by src.config.env
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
- `.env` 负责共享默认值，`.env.local` 可覆盖敏感凭据而无需提交。

## 🚀 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

根目录的 `.env` 已经整理出全部默认值并附带注释。`src.config.load_project_env` 会先加载 `.env`，如果存在 `.env.local` 则自动覆盖对应项，便于区分共享配置与私密凭据。

可选的本地覆盖步骤：

```bash
cp .env .env.local  # 创建本地覆写文件
```

随后在 `.env.local` 中修改关键变量，例如：

- `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `JWT_SECRET`：后台认证与安全。
- `VISITOR_USERNAME` / `VISITOR_PASSWORD`：访客只读账号（可留空）。
- `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID` / `IBKR_PAPER_TRADING`：IBKR 连接配置。
- `AUTO_CONNECT` / `IBBOT_DISABLE_TRADING`：启动时自动连接与交易禁用开关。
- `REDIS_URL` 或 `REDIS_HOST`/`REDIS_PORT`/`REDIS_DB`：缓存与队列依赖。
- `DATA_LAYER_*`、`TELEGRAM_*` 等按需启用的可选能力。

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
