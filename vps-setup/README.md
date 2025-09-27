# IBBot VPS 远程部署指南

本目录收录在云主机上快速部署与远程开发 IBBot 的脚本。脚本针对 Ubuntu 20.04+/Debian 系列设计，可根据需要调整。

## 包含内容

- `vps-deploy.sh`：安装 Docker、docker compose 插件、基本依赖。
- `quick-start.sh`：创建 SSH 隧道与常用端口转发（8000/5800/5900/4001/4002）。
- `port-forward.sh` / `autossh-forward.sh`：手动或持久化的端口转发方案。
- `setup-docker-context.sh`：在本地配置 Docker Context，方便直接 `docker compose` 操作远程主机。
- `ssh-config`：SSH 配置样例，可追加到本地 `~/.ssh/config`。

## 推荐工作流

### 1. VPS 侧初始化

```bash
# 上传脚本（示例）
scp -r vps-setup user@your-vps:/opt/ibbot/vps-setup

ssh user@your-vps
cd /opt/ibbot/vps-setup
chmod +x vps-deploy.sh
./vps-deploy.sh
```

脚本会安装 Docker、docker compose 插件，并将当前用户加入 `docker` 组。重新登录后继续执行。

### 2. 同步项目代码

```bash
rsync -avz --exclude='.git' --exclude='__pycache__' ./ user@your-vps:/opt/ibbot/app/
```

或使用 VS Code Remote-SSH / Git 克隆等方式。

### 3. 配置 Compose 环境变量

```bash
cd /opt/ibbot/app
cp docker/.env.example docker/.env
nano docker/.env
```

需要重点关注：

- `TWS_USERID_PAPER` / `TWS_PASSWORD_PAPER`：纸交易凭据（如使用 IB Gateway）。
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `JWT_SECRET`：Web 端登录。
- `CF_TUNNEL_TOKEN`：如需 Cloudflare Tunnel。
- `TIME_ZONE`：容器时区，默认 `Asia/Shanghai`。

### 4. 启动服务

```bash
cd /opt/ibbot/app
docker compose -f docker/docker-compose.yml up -d
```

通过以下地址访问：

- Web UI：http://localhost:8000（经 SSH 隧道映射后）
- IB Gateway noVNC：http://localhost:5800
- 若使用 autossh/quick-start 脚本，将会自动建立本地端口转发。

### 5. 本地辅助脚本

在本地机器执行：

```bash
# 追加 SSH 配置
cat vps-setup/ssh-config >> ~/.ssh/config

# 配置 Docker Context（远程执行 compose）
chmod +x vps-setup/setup-docker-context.sh
./vps-setup/setup-docker-context.sh
```

之后可使用 `docker --context ibbot-vps compose ...` 直接管理远程容器。

### 6. 端口转发模式

- `quick-start.sh`：一次性启动 SSH 隧道，适合临时会话。
- `port-forward.sh`：手动指定本地端口，便于自定义映射。
- `autossh-forward.sh`：依赖 `autossh` 持续保持连接，适合长期运行。

## 其他建议

- 为远程用户配置 SSH 密钥登录，并限制 root 直连。
- 使用防火墙限制 5800/5900 访问来源，或仅通过隧道访问。
- 定期备份 `./data`、`./logs`、`./strategy` 目录。
- 如需系统服务化，可使用 `systemd` 封装 `docker compose` 命令。

更多容器化细节参见 [docker/readme.md](../docker/readme.md)。
