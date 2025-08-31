# Docker 部署与使用指南

本目录包含用于开发/测试与生产部署的 Docker 相关文件与配置：

- Dockerfile：应用镜像的构建文件
- docker-compose.yml：本地开发/测试环境的编排文件（含 IB Gateway GUI）
- docker-compose.prod.yml：生产环境的编排文件（建议配合固定镜像版本）
- install_ib_gateway.sh：可选，宿主机自动检测/安装 IB Gateway 的脚本（非容器内运行）

注意：compose 文件移至 docker/ 目录后，所有相对路径（卷映射、Dockerfile 定位）均已按相对 docker/ 的路径进行了调整。

## 1. 前置要求

- 安装 Docker 与 Docker Compose 插件
- 确保宿主机端口未被占用：8000（Web）、5800（IB Gateway GUI/noVNC）、5900（VNC，可选）、4001/4002（IB API）
- 首次运行会在项目根目录创建持久化目录：`./logs`、`./data`、`./strategy`、`./data/ibgateway`

## 2. 开发/测试环境（docker-compose.yml）

在项目根目录执行以下命令：

- 构建镜像（首次或有变更时）
  - "docker compose -f docker/docker-compose.yml build"
- 启动（后台）
  - "docker compose -f docker/docker-compose.yml up -d"
- 查看状态
  - "docker compose -f docker/docker-compose.yml ps"
- 查看日志
  - "docker compose -f docker/docker-compose.yml logs -f"

启用 Mock 网关（可选 profile）
- 启动包含 mock 的环境：
  - "docker compose -f docker/docker-compose.yml --profile mock up -d"

默认会启动以下服务：
- ib-gateway：带 GUI 的 IB Gateway（noVNC）。首次进入需要完成登录与 API 设置。
- ibbot：交易机器人 Web 服务（默认 http://localhost:8000/）。
- mock-gateway：仅在启用 profile=mock 时启动，用于模拟 IB API（可选）。

访问 IB Gateway GUI：
- 浏览器访问：http://localhost:5800/
- 可选 VNC：localhost:5900

IB Gateway 内需完成以下设置：
- 登录（使用你的 TWS/IB 账户）
- Configure -> API -> Settings：
  - 勾选 “Enable ActiveX and Socket Clients”
  - Socket Port：4002（纸交易）或 4001（实盘）

数据持久化挂载（宿主机 -> 容器）：
- ./logs -> /app/logs
- ./data -> /app/data
- ./strategy -> /app/strategy
- ./data/ibgateway -> /config（IB Gateway 配置与缓存）

环境变量（主要）：
- ibbot：IBKR_HOST=ib-gateway，IBKR_PORT=4002（默认连接到本 compose 中的 IB Gateway）
- 建议覆盖 ADMIN_USERNAME/ADMIN_PASSWORD/JWT_SECRET 等安全相关参数

## 3. 生产部署（docker-compose.prod.yml）

生产环境推荐使用已发布的镜像版本，并在启动前配置必要的机密变量：

- 设置环境变量（示例）
  - "export ADMIN_PASSWORD='强密码'"
  - "export JWT_SECRET='强随机密钥'"
- 启动
  - "docker compose -f docker/docker-compose.prod.yml up -d"
- 状态/日志
  - "docker compose -f docker/docker-compose.prod.yml ps"
  - "docker compose -f docker/docker-compose.prod.yml logs -f"

如需切换实盘，将 IBKR_PORT 从 4002 改为 4001，并确保 IB Gateway 内部 API 端口同步调整。

持久化目录（相对于项目根目录）：
- ./logs、./data、./strategy、./data/ibgateway

## 4. 常见问题与排查

- 无法访问 5800：检查容器是否运行、宿主机防火墙、端口是否被占用。
- ibbot 无法连接 IB：确认 IB Gateway 已登录、API 已启用、端口与 IBKR_HOST/IBKR_PORT 一致。
- 端口冲突：修改 compose 中左侧的主机端口映射（如 5801:5800）。
- 安全：生产环境务必覆盖默认 ADMIN_PASSWORD 和 JWT_SECRET，并限制 5800/5900 的访问范围。
- 访问日志面板提示 “Authentication required”：先在 Web UI 登录（右上角），或通过 /api/login 获取 JWT 后再访问受保护接口。
- 日志文件位置：宿主机 ./logs/trading_bot.log（通过 compose 的卷映射），可用于排查问题。

## 5. 目录结构（节选）

- docker/
  - Dockerfile
  - docker-compose.yml
  - docker-compose.prod.yml
  - install_ib_gateway.sh
  - readme.md（本文）

如需进一步个性化部署（例如启用/禁用 mock、加入反向代理、TLS 等），请按需调整 compose 文件并测试后再用于生产。

## 6. 通过 Cloudflare Tunnel 暴露外网访问（可选）

本项目提供 cloudflared 容器，便捷地将 ibbot Web 服务（容器内 ibbot:8000）通过 Cloudflare 的 Zero Trust Tunnel 安全暴露到公网域名。

支持两种模式：
- 令牌模式（推荐，简单快捷）：在 Cloudflare Zero Trust 后台创建隧道，复制其 TOKEN，放入 docker/.env 的 CF_TUNNEL_TOKEN。
- 配置文件模式（高级）：在项目 tunnel/ 目录维护 cloudflared 的 config.yml 与凭据 JSON，compose 将其挂载到 /etc/cloudflared。

### 6.1 目录与文件
- docker/.env：包含 CF_TUNNEL_TOKEN 等变量
- tunnel/config.yaml：示例 config（如使用配置文件模式）
- tunnel/<TunnelID>.json：Cloudflare 提供的凭据文件（创建隧道后下载）

### 6.2 Compose 服务
docker/docker-compose.yml 与 docker/docker-compose.prod.yml 已包含 cloudflared 服务：
- 使用 env_file: docker/.env
- 令牌模式：读取 CF_TUNNEL_TOKEN 并执行 `cloudflared tunnel run`
- 配置文件模式：将 ../tunnel 挂载到 /etc/cloudflared（只读）

### 6.3 令牌模式（推荐）
1) 在 Cloudflare Dashboard -> Zero Trust -> Networks -> Tunnels 创建隧道
2) 选择 Docker 部署，复制生成的 Token（或 docker run 命令中的 token）
3) 在 docker/.env 设置：
```
CF_TUNNEL_TOKEN=eyJ...your-token
```
4) 启动或重启 compose：
```
# 开发/测试
docker compose -f docker/docker-compose.yml up -d cloudflared
# 生产
docker compose -f docker/docker-compose.prod.yml up -d cloudflared
```
5) 在 Cloudflare 隧道的 Public Hostname 中添加一条路由：
- Hostname: your.domain.com
- Service: http://ibbot:8000
- No TLS Verify: 可选（内部 HTTP）
说明：cloudflared 与 ibbot 在同一 docker 网络 ibbot-network 内，主机名 ibbot 即服务名。

### 6.4 配置文件模式（高级）
1) 在项目根目录 tunnel/ 放置以下文件：
- config.yml（或 config.yaml），示例内容参见 tunnel/config.yaml：
```
ingress:
  - hostname: your.domain.com
    service: http://ibbot:8000
  - service: http_status:404
```
- <TunnelID>.json 凭据文件（从 Cloudflare 下载）
- 在 config.yml 顶部可指定：
```
tunnel: <your-tunnel-name>
credentials-file: /etc/cloudflared/<TunnelID>.json
```
2) 启动 cloudflared：
```
# 开发/测试
docker compose -f docker/docker-compose.yml up -d cloudflared
# 生产
docker compose -f docker/docker-compose.prod.yml up -d cloudflared
```
3) 如需自定义其他参数，可在 docker/.env 中加入 CF_TUNNEL_ADDITIONAL_ARGS，并修改 compose 中 cloudflared 的 command 行以拼接参数。

### 6.5 访问与验证
- 浏览器访问：https://your.domain.com
- 验证：
  - Web UI 正常加载与登录
  - 日志面板：首次访问需要登录，否则报 “Authentication required”
  - 容器日志：
    - cloudflared：`docker compose -f docker/docker-compose.yml logs -f cloudflared`
    - ibbot：`docker compose -f docker/docker-compose.yml logs -f ibbot`

### 6.6 常见问题
- 访问 4xx：确认 Cloudflare 隧道已联通，Public Hostname 指向 http://ibbot:8000，且 ibbot 容器在同一网络 ibbot-network。
- 证书/凭据：令牌模式无需本地凭据文件；配置文件模式需同时提供 config.yml 与 <TunnelID>.json。
- 安全：外网开放后请务必设置强管理员密码、启用 HTTPS（Cloudflare 边缘），并考虑启用 Cloudflare Access 进行零信任访问控制。