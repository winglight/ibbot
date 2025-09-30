# Docker 部署与使用指南

本目录提供容器化部署 IBBot 所需的文件：

- `Dockerfile`：基础镜像构建脚本。
- `docker-compose.yml`：开发/联调环境，包含 IB Gateway (noVNC)、ibbot、cloudflared（可选）。
- `docker-compose.prod.yml`：生产环境最小化部署模板。
- `.env.example`：Compose 级别环境变量示例。

## 1. 前置条件

- Docker Engine 20+ 与 docker compose 插件。
- 宿主机开放端口：`8000`(Web)、`5800`(noVNC)、`5900`(VNC，可选)、`4001/4002`(IB API)。
- 首次运行会自动创建持久化目录：`./data/logs`、`./data`、`./strategy`、`./data/ibgateway`。

## 2. 开发/测试环境 (`docker-compose.yml`)

在项目根目录执行：

```bash
# 构建镜像
docker compose -f docker/docker-compose.yml build

# 启动后台服务
docker compose -f docker/docker-compose.yml up -d

# 查看运行状态与日志
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f
```

默认会拉起：

- **ib-gateway**：基于 noVNC 的 IB Gateway 容器，浏览器访问 http://localhost:5800/ 完成登录与 API 设置。
- **ibbot**：FastAPI 主服务，暴露在 http://localhost:8000/。
- **cloudflared** *(可选)*：若在 `docker/.env` 配置 `CF_TUNNEL_TOKEN`，可自动启动 Cloudflare Tunnel。

常用卷映射：

| 宿主机路径 | 容器路径 | 说明 |
| --- | --- | --- |
| `./data/logs` | `/app/logs` | 结构化日志 |
| `./data` | `/app/data` | SQLite、缓存文件等 |
| `./strategy` | `/app/strategy` | 策略脚本 |
| `./data/ibgateway` | `/config` | IB Gateway 配置缓存 |

开发模式默认将 ibbot 连接目标设置为 `ib-gateway:4002`（纸交易）。可在 `docker/.env` 或 Compose overrides 中覆盖。

## 3. 生产环境 (`docker-compose.prod.yml`)

建议使用已经推送到注册表的镜像，并在启动前导出必要的机密变量：

```bash
export ADMIN_PASSWORD='StrongPassword'
export JWT_SECRET='RandomSecret'
export TELEGRAM_BOT_TOKEN='...'  # 可选
export CF_TUNNEL_TOKEN='...'      # 若使用 cloudflared

docker compose -f docker/docker-compose.prod.yml up -d
```

更换为实盘时，请同步修改：

- Compose 环境变量 `IBKR_PORT=4001`
- IB Gateway 内部的 API 端口配置

## 4. Cloudflare Tunnel（可选）

1. 在 Cloudflare Zero Trust 创建隧道并获取 `CF_TUNNEL_TOKEN`。
2. 将 token 写入 `docker/.env`。
3. 通过 compose 启动 cloudflared：

```bash
# 开发环境仅启动隧道
docker compose -f docker/docker-compose.yml up -d cloudflared

# 生产环境
docker compose -f docker/docker-compose.prod.yml up -d cloudflared
```

在 Cloudflare 控制台新增 `Public Hostname`，将域名指向 `http://ibbot:8000` 即可。

## 5. 推送镜像到 GHCR（可选）

```bash
# 登录（GITHUB_TOKEN 需具备 write/read:packages 权限）
printf '%s' "$GITHUB_TOKEN" | docker login ghcr.io -u <GITHUB_USERNAME> --password-stdin

# 构建并标记镜像
docker build -f docker/Dockerfile -t ibbot:local .
docker tag ibbot:local ghcr.io/<OWNER>/ibbot:<TAG>

# 推送
docker push ghcr.io/<OWNER>/ibbot:<TAG>
```

将生产 compose 中 `image:` 指向新的 GHCR 路径即可。

## 6. 常见问题

| 现象 | 排查建议 |
| --- | --- |
| 无法访问 http://localhost:5800/ | 检查容器状态、宿主机防火墙或端口冲突 |
| ibbot 连接失败 | 确认 IB Gateway 已登录且 API 端口与 `IBKR_HOST/PORT` 一致 |
| 5800/5900 暴露过宽 | 通过安全组或 compose 自定义网络限制访问来源 |
| 访问日志接口提示 `Authentication required` | 先通过 `/api/login` 获取 JWT，并在请求头附带 `Authorization: Bearer <token>` |
| 日志位置 | 宿主机 `./data/logs/trading_bot.log` |

更多部署实践可参考根目录 README 及 `vps-setup/README.md`。
