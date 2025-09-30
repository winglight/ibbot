# IBBot 部署与诊断指引

本指南总结了使用 Docker Compose 与脚本工具部署 IBBot 的推荐流程，涵盖本地开发、生产环境切换以及部署后健康检查步骤。

## 1. 预备条件

- Docker Engine 20+ 与 docker compose 插件。
- 宿主机开放端口：`6379` (Redis)、`8086` (InfluxDB)、`8000/8001` (服务)、`4173` (Optimizer UI)、`5800/5900/4001/4002` (IB Gateway)。
- 根目录 `.env` 提供默认值，可通过 `.env.local` 覆盖敏感配置，例如账号、密钥或生产端口。

## 2. 快速启动

```bash
# 按依赖顺序启动 Redis → InfluxDB → IB Gateway → 主服务/优化器 → UI
./scripts/deploy.sh up --env local
```

脚本会自动：

1. 依次执行 `docker compose up -d <service>` 以确保依赖先行；
2. 输出 `docker compose ps` 显示容器状态；
3. 调用 `python scripts/diagnose_foundation.py` 检查 Redis、InfluxDB 与日志写入。

如需重启单个服务：

```bash
./scripts/deploy.sh restart ibbot
```

关闭所有容器：

```bash
./scripts/deploy.sh down
```

## 3. 环境切换（local ↔ production）

`scripts/deploy.sh` 提供 `--env` 参数用于切换 Compose 文件：

- `--env local`（默认）：使用根目录 `docker-compose.yml`，适合本地开发，数据默认写入仓库内 `data/` 目录。
- `--env production`：在上述基础上叠加 `docker-compose.prod.yml`，会将数据映射至 `/var/lib/ibbot/*`，并允许通过环境变量调整端口。

示例：

```bash
# 生产部署（需提前准备 .env.local）
./scripts/deploy.sh up --env production
```

脚本会自动加载 `.env` 与 `.env.local` 中的变量；若配置了 `CF_TUNNEL_TOKEN`，Cloudflare Tunnel 服务也会加入启动序列。

## 4. 部署后健康检查

除自动执行的诊断命令外，可手动重复以下步骤：

```bash
# 查看容器状态
./scripts/deploy.sh status

# 再次执行基础依赖检查
./scripts/deploy.sh diagnose
```

`scripts/diagnose_foundation.py` 会基于 `ConfigProvider` 读取 Redis/Influx/日志配置，并给出详细的成功或失败原因。若 InfluxDB 使用全新凭据，请确保 `.env.local` 中的 `INFLUX_TOKEN`、`INFLUX_ORG`、`INFLUX_BUCKET` 与初始化参数一致。

## 5. CI 基本校验

GitHub Actions `ci.yml` 增加了 Docker Compose 冒烟测试步骤，会在拉取代码后运行 `./scripts/deploy.sh up --env local --build`，
随后自动执行健康检查与诊断脚本，并在流水线结束时调用 `./scripts/deploy.sh down` 完成清理，可提前捕获镜像更新或依赖初始化异常。

## 6. 常见问题

| 现象 | 排查建议 |
| --- | --- |
| `deploy.sh` 提示缺少 compose 文件 | 确认仓库根目录存在 `docker-compose.yml`，或检查 `--env` 参数是否拼写正确。 |
| 诊断脚本 Redis 连接失败 | 核实宿主机 `6379` 端口是否被占用，或 `.env.local` 中的 `REDIS_URL` 是否覆盖为其他地址。 |
| InfluxDB 健康检查失败 | 检查 `INFLUX_*` 初始化变量是否匹配容器首次启动时设置的值；必要时删除 `data/influxdb*` 重新初始化。 |
| Optimizer UI 无法访问 | 确认 `optimizer_ui/` 目录存在构建产物，并检查宿主机 `4173` 端口是否冲突。 |

如需更深入的依赖说明，请参考 `docs/foundation_libs.md`。
