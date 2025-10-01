# Docker 部署与使用指南

本目录提供容器化部署 IBBot 所需的基础镜像与历史 Compose 示例。自本次更新后，**`docker/local.compose.yml`、`docker/prod.compose.yml` 与 `scripts/deploy.sh`**
成为推荐入口，用于在本地或生产环境按依赖顺序拉起 Redis、InfluxDB、服务端以及前端 UI。

## 1. 前置条件

- Docker Engine 20+ 与 docker compose 插件。
- 宿主机开放端口：`8000`(Web)、`5800`(noVNC)、`5900`(VNC，可选)、`4001/4002`(IB API)。
- 首次运行会自动创建持久化目录：`./data/logs`、`./data`、`./strategy`、`./data/ibgateway`。

## 2. 开发/测试环境（推荐）

```bash
# 以本地依赖顺序启动 Redis → InfluxDB → 服务 → UI
./scripts/deploy.sh up --env local

# 查看容器状态或再次运行诊断
./scripts/deploy.sh status
./scripts/deploy.sh diagnose

# 需要关闭时
./scripts/deploy.sh down
```

`docker/local.compose.yml` 包含以下组件：

- **redis**：缓存与配置中心，默认对宿主机开放 `6379` 端口。
- **influxdb**：行情写入与指标存储，默认开放 `8086`。
- **ib-gateway**：集成 noVNC 的 IB Gateway，`http://localhost:5800/` 可进行登录与授权。
- **ibbot / optimizer**：主服务与策略优化器，端口分别为 `8000/8001`。
- **account-service**：基于 FastAPI/FastAPI-Workers 的账户快照服务，默认端口 `8020`。
- **optimizer-ui**：静态部署的优化器前端，默认映射 `4173`。
- **cloudflared**：可选的 Cloudflare Tunnel，仅在 `CF_TUNNEL_TOKEN` 存在时自动加入启动序列。

`scripts/deploy.sh` 在启动完成后会自动执行：

1. `docker compose ps` 输出各服务状态；
2. `python scripts/diagnose_foundation.py` 检查 Redis、InfluxDB 与日志写入。

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
