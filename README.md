# IBKR Trading Bot

一个基于Interactive Brokers API的全功能交易机器人，支持实时数据获取、自动交易、风险管理、策略执行和Web界面管理。

## 🚀 核心特性

### 📈 交易功能
- ✅ **多市场支持**: 股票(STK)、期货(FUT)等多种证券类型
- ✅ **智能下单**: 市价单、限价单，支持SMART路由
- ✅ **订单管理**: 实时订单状态跟踪、批量取消、订单历史
- ✅ **实时数据**: 实时价格、历史数据、图表数据
- ✅ **账户管理**: 资金状况、持仓信息、P&L计算

### 🛡️ 风险管理
- ✅ **智能止损止盈**: 基于价格和百分比的自动风险控制
- ✅ **多级风险阈值**: 3%警告、8%自动平仓等可配置阈值
- ✅ **实时监控**: 持仓风险实时监控和自动处理
- ✅ **订单队列**: FIFO队列确保订单执行顺序
- ✅ **资金检查**: 下单前资金充足性验证

### ⚡ 异步队列系统
- ✅ **FIFO队列管理**: 先进先出的任务队列确保处理顺序
- ✅ **异步任务处理**: 非阻塞的任务执行机制
- ✅ **重试机制**: 失败任务自动重试，最多3次
- ✅ **状态跟踪**: 实时监控任务状态（待处理、处理中、已完成、失败）
- ✅ **错误处理**: 详细的错误信息记录和处理
- ✅ **队列监控**: 队列状态统计和任务详情查询

### 🤖 策略系统
- ✅ **外部策略**: 支持HTTP API调用外部策略服务
- ✅ **内部策略**: Python脚本策略执行
- ✅ **策略管理**: 策略启用/禁用、性能跟踪
- ✅ **执行记录**: 详细的策略执行历史和P&L分析
- ✅ **认证机制**: 策略API访问令牌管理
- ✅ **异步队列**: FIFO队列处理策略订单，支持重试和错误处理

### 🌐 Web界面
- ✅ **现代化UI**: 基于Bootstrap的响应式设计
- ✅ **实时更新**: WebSocket实时数据推送
- ✅ **用户认证**: JWT令牌认证系统
- ✅ **多功能面板**: 交易、风险、策略、日志管理
- ✅ **图表展示**: 实时价格图表和历史数据可视化
- ✅ **PnL 日历**: 按策略展示每日盈亏情况

### 📱 通知系统
- ✅ **Telegram集成**: 实时交易通知和风险警报
- ✅ **多级通知**: 信息、警告、错误分级通知
- ✅ **每日摘要**: 自动生成账户日报
- ✅ **事件通知**: 订单执行、风险触发等关键事件

### 💾 数据管理
- ✅ **SQLite数据库**: 统一的数据存储和管理
- ✅ **数据迁移**: 从CSV/JSON文件自动迁移到数据库
- ✅ **备份机制**: 订单和配置数据自动备份
- ✅ **历史数据**: 完整的交易和策略执行历史

## PnL Calendar 设置

PnL Calendar 用于在策略详情页展示每日盈亏。默认前端已包含基础的日历组件，若需自定义或更新，可按以下步骤启用：

1. 获取 [winglight/PNLCalendar](https://github.com/winglight/PNLCalendar) 项目的 JS 与 CSS 文件（或添加为 git submodule）。
2. 将 `pnlcalendar.js` 和 `pnlcalendar.css` 复制到项目的 `static/` 目录。
3. 重新加载网页后即可在策略详情的 **PnL Calendar** 标签中查看每日 PnL。

自定义选项（在 `app.js` 中调用 `loadPnLCalendar` 时传入）：

- `locale`：本地化语言代码（默认 `en-US`）。
- `positiveColor`：盈利用背景色，默认 `#d4edda`。
- `negativeColor`：亏损用背景色，默认 `#f8d7da`。

## 安装要求

### 系统要求
- Python 3.8+
- Interactive Brokers TWS或IB Gateway
- 网络连接

### Python依赖
```bash
pip install -r requirements.txt
```

主要依赖包：
- `ib_insync`: IBKR API客户端
- `fastapi`: Web API框架
- `uvicorn`: ASGI服务器
- `pandas`: 数据处理
- `python-telegram-bot`: Telegram通知
- `apscheduler`: 定时任务
- `loguru`: 日志记录

## 配置设置

### 1. 环境变量配置

复制示例配置文件：
```bash
cp .env.example .env
```

编辑 `.env` 文件：
```bash
# IBKR配置
IBKR_HOST=127.0.0.1
IBKR_PORT=7497  # TWS: 7497, IB Gateway: 4001
IBKR_CLIENT_ID=1
IBKR_PAPER_TRADING=True  # True=模拟账号, False=真实账号

# Telegram配置（可选）
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# 风险管理
LOSS_THRESHOLD_1=0.05  # 5%亏损阈值（通知）
LOSS_THRESHOLD_2=0.10  # 10%亏损阈值（自动平仓）
CHECK_INTERVAL=5  # 检查间隔（分钟）

# Web服务器
WEB_HOST=0.0.0.0
WEB_PORT=8000
```

### 2. IBKR TWS/Gateway设置

#### TWS设置
1. 启动TWS
2. 登录您的账户
3. 进入 `File` → `Global Configuration` → `API` → `Settings`
4. 勾选 `Enable ActiveX and Socket Clients`
5. 设置 `Socket port` 为 `7497`（模拟）或 `7496`（真实）
6. 添加可信IP地址：`127.0.0.1`

#### IB Gateway设置
1. 启动IB Gateway
2. 登录您的账户
3. 设置API端口为 `4001`（模拟）或 `4000`（真实）
4. 启用API连接

### 3. Telegram通知设置（可选）

1. 创建Telegram Bot：
   - 在Telegram中搜索 `@BotFather`
   - 发送 `/newbot` 创建新机器人
   - 获取Bot Token

2. 获取Chat ID：
   - 向您的Bot发送消息
   - 访问 `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   - 从响应中获取 `chat.id`

## 使用方法

### 1. 启动应用

```bash
python main.py
```

### 2. 访问Web界面

打开浏览器访问：默认 `http://localhost:8000`（可通过环境变量 `WEB_HOST` 和 `WEB_PORT` 修改）

### 3. 连接IBKR

1. 在Web界面的连接设置中配置参数
2. 点击"Connect"按钮
3. 确认连接状态显示为"Connected"

### 4. 基本操作

#### 获取市场数据
- 输入股票代码（如：AAPL）
- 点击"Get Real-time Data"获取实时数据
- 点击"Get Historical Data"下载历史数据

#### 下单交易
- 选择股票代码、数量、买卖方向
- 选择订单类型（市价单/限价单）
- 点击"Place Order"提交订单

#### 风险管理
- 设置止损价格和止盈价格
- 系统将自动监控并执行风险管理规则

#### 查看系统日志
- 打开“日志”面板，支持按日期范围、模块和等级筛选
- 日志等级筛选为包容式：选择 warning 显示 warning、error、critical；选择 error 显示 error、critical；选择 all 显示全部
- 如遇 Authentication required，请先在右上角使用管理员账号登录（默认来自 .env 的 ADMIN_USERNAME/ADMIN_PASSWORD）
- 后端日志文件默认保存在 logs/trading_bot.log，可用前端面板或直接查看文件进行验证

## 🐳 Docker使用

### 使用 GitHub Container Registry 镜像安装（推荐）

无需本地构建，直接拉取已发布的版本化镜像（与 GitHub Releases 同步的 vX.Y.Z 标签）：

```bash
# 拉取发布版镜像（将 <owner> 替换为你的 GitHub 用户名或组织名，例如 winglight）
docker pull ghcr.io/<owner>/ibbot:v1.0.0
```

- 可用版本：请在本仓库的 GitHub Releases 页面查看版本号（tag），选择所需的 vX.Y.Z 标签。
- 如镜像仓库为私有，可先登录 GHCR（公共镜像无需）：
  ```bash
  echo $CR_PAT | docker login ghcr.io -u <github_username> --password-stdin
  ```
  其中 CR_PAT 为具有 packages:read 权限的 GitHub Personal Access Token。

#### 使用 latest 标签（可选）

- latest 标签通常指向最新发布的镜像，用于快速体验或自动化环境；生产环境推荐固定到明确的版本号（如 v1.0.0）以获得可重复部署和可追溯性。
- 如果当前仓库未提供 latest 标签，请使用具体的版本标签（vX.Y.Z）。

```bash
# 拉取最新版本（如仓库提供 latest 标签）
docker pull ghcr.io/<owner>/ibbot:latest

# 快速运行（等价于使用最新版本）
docker run -d \
  --name ibbot-main \
  -p 8000:8000 \
  ghcr.io/<owner>/ibbot:latest
```

#### 直接运行镜像（无需 .env 文件）

**基础运行（开箱即用，用于快速测试）：**

```bash
# 运行主应用（自动生成基础配置）
docker run -d \
  --name ibbot-main \
  -p 8000:8000 \
  ghcr.io/<owner>/ibbot:v1.0.0

**生产环境运行（推荐覆盖关键安全配置）：**

```bash
# 运行主应用并覆盖关键配置
docker run -d \
  --name ibbot-main \
  -p 8000:8000 \
  -e ADMIN_PASSWORD=your_secure_password \
  -e JWT_SECRET=your_production_jwt_secret \
  -e IBKR_HOST=host.docker.internal \
  -e IBKR_PORT=4002 \
  -e AUTO_CONNECT=false \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/strategy:/app/strategy \
  ghcr.io/<owner>/ibbot:v1.0.0

# 查看日志
docker logs -f ibbot-main
```

**运行策略运行器：**

```bash
# 独立运行策略运行器
docker run -it --rm \
  -e RUNNER_API_BASE=http://host.docker.internal:8000 \
  -e RUNNER_STRATEGY_TOKEN=your_strategy_token \
  ghcr.io/<owner>/ibbot:v1.0.0 runner --file "Buy down and Sell open.py" --symbols AAPL

# 并行运行主应用和策略运行器
docker run -d \
  --name ibbot-parallel \
  -p 8000:8000 \
  -e ADMIN_PASSWORD=your_secure_password \
  -e JWT_SECRET=your_production_jwt_secret \
  ghcr.io/<owner>/ibbot:v1.0.0 parallel --file "Buy down and Sell open.py" --symbols AAPL
```

> **安全提示**：生产环境务必覆盖 `ADMIN_PASSWORD` 和 `JWT_SECRET`，不要使用默认值。

> **配置说明**：容器启动时会自动从 `.env.example` 生成基础 `.env` 配置，无需外挂 `.env` 文件。如需使用自定义 `.env` 文件，可通过 `-v $(pwd)/.env:/app/.env:ro` 挂载。

### 1. 快速开始

使用Docker Compose一键启动整个应用栈：

```bash
# 构建并启动所有服务
docker-compose -f docker/docker-compose.prod.yml up -d

# 查看服务状态
docker-compose ps

# 查看日志
docker-compose logs ibbot
```

访问Web界面：`http://localhost:8000`

### 2. Docker 单独使用

#### 构建镜像

```bash
# 构建主应用镜像
docker build -t ibbot:latest .
```

#### 运行主应用

```bash
# 运行主应用（连接外部IB Gateway，无需 .env 文件）
docker run -d \
  --name ibbot-main \
  -p 8000:8000 \
  -e IBKR_HOST=host.docker.internal \
  -e IBKR_PORT=4002 \
  -e AUTO_CONNECT=false \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/strategy:/app/strategy \
  ibbot:latest

# 查看日志
docker logs -f ibbot-main
```


#### 运行测试

```bash
# 运行测试套件
docker run --rm \
  -e API_BASE_URL=http://host.docker.internal:8000 \
  ibbot:latest test
```

### 3. 环境变量配置

Docker容器支持以下环境变量：

#### 服务器配置
```bash
WEB_HOST=0.0.0.0          # Web服务绑定地址
WEB_PORT=8000             # Web服务端口
```

#### IBKR连接配置
```bash
IBKR_HOST=127.0.0.1       # IB Gateway主机
IBKR_PORT=4002            # IB Gateway端口
IBKR_CLIENT_ID=1          # 客户端ID
IBKR_PAPER_TRADING=True   # 模拟交易模式
```

#### 认证配置
```bash
ADMIN_USERNAME=admin               # 管理员用户名
ADMIN_PASSWORD=change_me           # 管理员密码（生产环境务必修改）
JWT_SECRET=please_change_me        # JWT密钥（生产环境务必修改）
JWT_ALGORITHM=HS256               # JWT算法
JWT_EXPIRE_DAYS=1                 # JWT过期天数
```

#### 功能开关
```bash
AUTO_CONNECT=false        # 自动连接IBKR
ENABLE_TELEGRAM=false     # 启用Telegram通知
ENABLE_WEBSOCKET=true     # 启用WebSocket
```

### 4. 数据持久化

使用Docker卷持久化重要数据：

```bash
# 创建数据目录
mkdir -p logs data strategy

# 运行时挂载数据卷（推荐：不再需要挂载 .env）
docker run -d \
  --name ibbot \
  -p 8000:8000 \
  -e ADMIN_PASSWORD=your_secure_password \
  -e JWT_SECRET=your_production_secret \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/strategy:/app/strategy \
  ibbot:latest
```

**可选：使用外部 .env 文件**

如果你仍希望使用外部 `.env` 文件管理配置：

```bash
# 方式1：直接挂载 .env 文件
docker run -d \
  --name ibbot \
  -p 8000:8000 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/strategy:/app/strategy \
  ibbot:latest

# 方式2：使用 docker-compose 的 env_file（完整示例）
# 新建或编辑 docker-compose.yml，添加如下内容：
```
version: '3.8'

services:
  ibbot:
    image: ghcr.io/<owner>/ibbot:v1.0.0  # 亦可使用 :latest（如仓库提供）
    container_name: ibbot-main
    env_file:
      - .env
    environment:
      - WEB_HOST=0.0.0.0
      - WEB_PORT=8000
      # 如需额外覆盖敏感配置，建议在环境变量中注入（优先级高于 env_file）
      # - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      # - JWT_SECRET=${JWT_SECRET}
    ports:
      - "8000:8000"
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
      - ./strategy:/app/strategy
    networks:
      - ibbot-network
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

networks:
  ibbot-network:
    driver: bridge
```

### 生产部署示例：docker-compose.prod.yml

以下是一个生产环境的 Compose 样例，包含健康检查和重启策略的最佳实践。建议固定镜像版本号（vX.Y.Z），避免使用 moving tag 导致的不可控变更；若需要快速跟随最新版本，可改用 :latest（如仓库提供）。

```yaml
version: '3.8'

services:
  ibbot:
    image: ghcr.io/<owner>/ibbot:v1.0.0  # 生产推荐固定版本；可改为 :latest（如仓库提供）
    container_name: ibbot
    environment:
      # Web 服务
      - WEB_HOST=0.0.0.0
      - WEB_PORT=8000

      # IBKR 实盘/纸交易连接（视环境修改）
      - IBKR_HOST=ib-gateway
      - IBKR_PORT=4002

      # 认证与安全（务必覆盖默认值）
      - ADMIN_USERNAME=${ADMIN_USERNAME}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - JWT_SECRET=${JWT_SECRET}
      - JWT_ALGORITHM=HS256
      - JWT_EXPIRE_DAYS=1

      # 功能开关
      - AUTO_CONNECT=false
      - ENABLE_WEBSOCKET=true

    ports:
      - "8000:8000"
    volumes:
      - ../logs:/app/logs
      - ../data:/app/data
      - ../strategy:/app/strategy
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
    networks:
      - ibbot-network
    depends_on:
      - ib-gateway
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: ibbot-cloudflared
    restart: unless-stopped
    env_file:
      - ./.env
    environment:
      - TUNNEL_TOKEN=${CF_TUNNEL_TOKEN}
    # Token mode only
    command: ["tunnel", "--no-autoupdate", "run"]
    depends_on:
      - ibbot
    networks:
      - ibbot-network

networks:
  ibbot-network:
    driver: bridge
```

部署与运维：

```bash
# 使用生产配置启动（确保已设置环境变量 ADMIN_PASSWORD / JWT_SECRET）
export ADMIN_PASSWORD=your_secure_password
export JWT_SECRET=your_production_jwt_secret

docker compose -f docker/docker-compose.prod.yml up -d

# 查看服务与健康状态
docker compose -f docker/docker-compose.prod.yml ps

# 查看日志
docker compose -f docker/docker-compose.prod.yml logs -f
```

> 提示：
> - 建议将服务置于反向代理（如 Nginx、Traefik）之后，并开启 TLS。
> - 可配合进程/节点级监控（如 Prometheus + Grafana）观测健康状态与日志。
> - 生产环境优先使用明确版本号；如需使用 :latest，请建立回滚策略。