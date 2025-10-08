# Docker 部署与使用指南

本目录提供容器化部署 IBBot 所需的基础镜像与历史 Compose 示例。自本次更新后，**`docker/local.compose.yml`、`docker/prod.compose.yml` 与 `scripts/deploy.sh`**
成为推荐入口，用于在本地或生产环境按依赖顺序拉起 Redis、MariaDB、服务端以及前端 UI。

## 1. 前置条件

- Docker Engine 20+ 与 docker compose 插件。
- 宿主机开放端口：`8000`(Web)、`8020`(Account Service)、`8050`(Orders Service)、`8030`(Risk Service)、`8045`(Strategy Service)、`5800`(noVNC)、`5900`(VNC，可选)、`4001/4002`(IB API)。
- 首次运行会自动创建持久化目录：`./data/logs`、`./data`、`./strategy`、`./data/ibgateway`。

## 2. 开发/测试环境（推荐）

```bash
# 以本地依赖顺序启动 Redis → MariaDB → 服务 → UI
./scripts/deploy.sh up --env local

# 查看容器状态或再次运行诊断
./scripts/deploy.sh status
./scripts/deploy.sh diagnose

# 需要关闭时
./scripts/deploy.sh down
```

`docker/local.compose.yml` 包含以下组件：

- **redis**：缓存与配置中心，默认对宿主机开放 `6379` 端口。
- **mariadb**：行情写入与指标存储，默认开放 `3306`。
- **ib-gateway**：集成 noVNC 的 IB Gateway，`http://localhost:5800/` 可进行登录与授权。
- **account**：账户 API 服务，默认映射 `8020`，必须在主进程之前启动。
- **orders**：订单指令服务，默认映射 `8050`，负责下单/撤单操作。
- **ibbot / optimizer**：主服务与策略优化器，端口分别为 `8000/8001`。
- **risk**：风险控制 REST 服务，默认映射 `8030`。
- **notification**：通知调度服务，默认映射 `8055`，负责监听 Redis 队列并触发消息推送。
- **strategy**：策略管理 REST 服务，默认映射 `8045`。
- **optimizer-ui**：静态部署的优化器前端，默认映射 `4173`。
- **cloudflared**：可选的 Cloudflare Tunnel，仅在 `CF_TUNNEL_TOKEN` 存在时自动加入启动序列。

`scripts/deploy.sh` 在启动完成后会自动执行：

1. `docker compose ps` 输出各服务状态；
2. `python scripts/diagnose_foundation.py` 检查 Redis、MariaDB 与日志写入。

常用挂载目录位于根目录 `data/` 下，可直接在宿主机查看日志与持久化文件。

## 3. 生产环境覆盖层

若需要部署到 VPS/服务器，可在同一命令基础上增加 `--env production`，脚本会额外加载 `docker/prod.compose.yml`：

```bash
./scripts/deploy.sh up --env production
```

该覆盖文件将持久化目录映射至 `/var/lib/ibbot/*`，并为主要端口提供环境变量化的重定向。实际部署时请根据生产环境调整 `.env.local` 与相关变量，例如：

- `TWS_USERID_PAPER` / `TWS_PASSWORD_PAPER` → 实盘凭据；
- `AUTO_CONNECT`、`IBKR_PAPER_TRADING` → 生产连接策略；
- `CF_TUNNEL_TOKEN` → 若需开启 Cloudflare 隧道。

## 4. Cloudflare Tunnel（可选）

1. 在 Cloudflare Zero Trust 创建隧道并获取 `CF_TUNNEL_TOKEN`。
2. 将 token 写入 `.env.local` 或直接导出环境变量。
3. 使用部署脚本或 compose profile 启动 cloudflared：

```bash
# 使用脚本（自动判断 token 是否存在）
./scripts/deploy.sh restart cloudflared

# 或手动指定 profile
docker compose --profile tunnel up -d cloudflared
```

在 Cloudflare 控制台新增 `Public Hostname`，将域名指向 `http://ibbot:8000` 即可。

## 5. 基础镜像与分层构建方案

本项目将以“基础镜像 + 应用镜像”的分层方式交付，便于后续仅更新业务代码即可完成发布。以下步骤默认 **protobuf 生成的 `.pb` 文件已经在仓库中准备好且可复用**，因此在容器内无需重复执行 `protoc`。

### 5.1 准备 TWS API 源码

1. 在仓库根目录执行 `git submodule update --init --recursive`，或手动确认 `IBJts` 目录已经更新到官方最新版本。
2. 构建上下文中必须包含 `IBJts/source/pythonclient` 的全部内容，以及已经生成好的 protobuf Python 文件。

### 5.2 构建基础镜像

仓库已经提供 `docker/Dockerfile.base`，以 `python:3.11-slim` 为基础镜像并完成如下步骤：

1. 安装最小化的系统依赖（`build-essential`、`curl`、`gcc`、`g++` 等）。
2. 将 `IBJts/source/pythonclient` 复制到镜像中并执行 `python setup.py install` 安装 TWS API。
3. 复制 `requirements.txt`，执行 `pip install -r requirements.txt` 以及 `pip install gunicorn "uvicorn[standard]"`。

> ⚠️ 当前方案假设 protobuf `.pb` 文件已在仓库中准备就绪，如需重新生成，请在基础镜像中额外安装 `protobuf-compiler` 并调用生成脚本。

构建命令示例：

```bash
docker build -f docker/Dockerfile.base -t ghcr.io/<OWNER>/ibbot-base:py311 .
docker push ghcr.io/<OWNER>/ibbot-base:py311
```

### 5.3 构建项目镜像

`docker/Dockerfile` 默认通过 `BASE_IMAGE` 构建参数引用上述基础镜像，避免重复安装依赖，仅负责复制业务代码、生成运行所需的目录以及写入入口脚本。

```bash
# 如需使用自定义 tag，可在构建时覆盖 BASE_IMAGE
docker build \
  -f docker/Dockerfile \
  --build-arg BASE_IMAGE=ghcr.io/<OWNER>/ibbot-base:py311 \
  -t ghcr.io/<OWNER>/ibbot:latest .

docker push ghcr.io/<OWNER>/ibbot:latest
```

Compose 文件（`docker/docker-compose.yml`）已通过 `x-app-build` 共享该参数，确保所有依赖应用镜像的服务均使用相同的基础镜像层。

## 6. 推送镜像到 GHCR（可选）

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

## 7. 常见问题

| 现象 | 排查建议 |
| --- | --- |
| 无法访问 http://localhost:5800/ | 检查容器状态、宿主机防火墙或端口冲突 |
| ibbot 连接失败 | 确认 IB Gateway 已登录且 API 端口与 `IBKR_HOST/PORT` 一致 |
| 5800/5900 暴露过宽 | 通过安全组或 compose 自定义网络限制访问来源 |
| 访问日志接口提示 `Authentication required` | 先通过 `/api/login` 获取 JWT，并在请求头附带 `Authorization: Bearer <token>` |
| 日志位置 | 宿主机 `./data/logs/trading_bot.log` |

更多部署实践可参考根目录 README 及 `vps-setup/README.md`。
