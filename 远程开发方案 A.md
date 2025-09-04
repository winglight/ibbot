# 远程开发方案 A（Markdown 版）

> 目标：**不卡顿、零公网暴露**，在本地像平时一样 `docker compose up`、重启与调试，但计算都在 VPS 上完成；需要 VNC 访问容器 5900 端口，但不暴露到公网。

---

## 目录

- [远程开发方案 A（Markdown 版）](#远程开发方案-amarkdown-版)
  - [目录](#目录)
  - [整体思路](#整体思路)
  - [前提条件](#前提条件)
  - [VPS 端设置](#vps-端设置)
    - [1) docker-compose 示例（仅回环绑定）](#1-docker-compose-示例仅回环绑定)
    - [2) 防火墙/安全组建议](#2-防火墙安全组建议)
    - [3) 端口监听自检](#3-端口监听自检)
  - [本地（MBA）端设置](#本地mba端设置)
    - [4) SSH 快速连接配置](#4-ssh-快速连接配置)
    - [5) 本地端口转发（VNC/Traefik/服务）](#5-本地端口转发vnctraefik服务)
    - [6) autossh 保活（可选）](#6-autossh-保活可选)
  - [远程 Docker Context（把算力放到 VPS）](#远程-docker-context把算力放到-vps)
  - [推荐工作流](#推荐工作流)
  - [性能与稳定性优化](#性能与稳定性优化)
  - [常见问题速查](#常见问题速查)
  - [附录：命令速查表](#附录命令速查表)

---

## 整体思路

* **VPS**：所有容器端口只绑定到 `127.0.0.1`（本机回环），**不暴露公网**。
* **本地**：通过 `ssh -L` 建立**本地端口转发**访问 VNC（5900）与 Traefik/服务面板；
* **开发控制**：使用 `docker context` 把本地 Docker 客户端“遥控”到 VPS，MBA 不再吃性能。

---

## 前提条件

* 可 SSH 登录 VPS（建议密钥登录，禁用密码）。
* VPS 已安装 Docker / Docker Compose。
* 本地 macOS（MBA）已安装：`ssh`、`docker`、（可选）`autossh`、VS Code（可选 Remote-SSH 插件）。

---

## VPS 端设置

### 1) docker-compose 示例（仅回环绑定）

> **关键点**：端口映射时显式绑定到 `127.0.0.1`，不要使用 `0.0.0.0` 或省略主机地址。

```yaml
# docker-compose.yml（示例，按需替换镜像与端口）
services:
  vncbox:
    image: dorowu/ubuntu-desktop-lxde-vnc   # 示例：图形化桌面 + VNC
    ports:
      - "127.0.0.1:5900:5900"               # VNC 仅回环，不对公网
    # volumes: [./data:/data]               # 需要时挂载
    # networks: [internal]

  traefik:
    image: traefik:v3
    command:
      - "--api.insecure=true"
      - "--api.dashboard=true"
      - "--entrypoints.web.address=127.0.0.1:8000"  # 示例：你的 dev 入口仅本机监听
    ports:
      - "127.0.0.1:8080:8080"               # Traefik 面板仅本机
      - "127.0.0.1:8000:8000"               # 开发入口仅本机
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    # networks: [internal]

# 可选：内部网络
networks:
  internal:
    driver: bridge
```

> 可将其他需要调试的服务也改成 `"127.0.0.1:HOST:CONTAINER"` 形式，或直接不暴露 ports，仅通过反向代理或 `ssh -L` 访问。

### 2) 防火墙/安全组建议

* 云厂商安全组：**拒绝入站**所有不需要的端口，仅开放 22（SSH）。
* VPS 本机防火墙（以 UFW 为例）：

  ```bash
  sudo ufw default deny incoming
  sudo ufw default allow outgoing
  sudo ufw allow 22/tcp
  sudo ufw enable
  ```

### 3) 端口监听自检

```bash
# 仅应看到 127.0.0.1:5900 / 127.0.0.1:8080 / 127.0.0.1:8000 等
ss -lntp | grep -E '5900|8080|8000'
```

---

## 本地（MBA）端设置

### 4) SSH 快速连接配置

在 `~/.ssh/config` 添加（根据你的信息修改）：

```sshconfig
Host vps
  HostName <你的VPS IP或域名>
  User <你的用户>
  IdentityFile ~/.ssh/id_ed25519
  Compression yes
  ControlMaster auto
  ControlPath ~/.ssh/cm-%r@%h:%p
  ControlPersist 10m
```

### 5) 本地端口转发（VNC/Traefik/服务）

一次性打开多个本地转发（前台运行）：

```bash
ssh -N \
  -L 5901:127.0.0.1:5900 \   # 本地 VNC -> VPS:5900
  -L 8080:127.0.0.1:8080 \   # 本地浏览器 -> Traefik 面板
  -L 8000:127.0.0.1:8000 \   # 本地浏览器 -> 你的 dev 入口
  vps
```

现在：

* 用 VNC 客户端连 `localhost:5901`（实际通到 VPS 回环 5900）
* 打开 Traefik 面板：`http://localhost:8080`
* 打开你的服务：`http://localhost:8000`

### 6) autossh 保活（可选）

```bash
brew install autossh
autossh -M 0 -N \
  -L 5901:127.0.0.1:5900 \
  -L 8080:127.0.0.1:8080 \
  -L 8000:127.0.0.1:8000 \
  vps
```

---

## 远程 Docker Context（把算力放到 VPS）

```bash
# 仅需一次
docker context create vps --docker "host=ssh://vps"
docker context use vps

# 之后本地的 docker 命令都在 VPS 上执行
docker compose up -d --build
docker compose logs -f <service>
docker ps
docker exec -it <container> /bin/sh
```

> **代码编辑方式**（二选一或组合）
>
> * VS Code + Remote-SSH：直接编辑 VPS 上的工程（零同步、最快）。
> * 本地编辑 + 同步：用 Mutagen / rsync-watcher 将本地目录同步到 VPS 指定路径。

---

## 推荐工作流

1. **编辑代码**：VS Code Remote-SSH 连接 `vps`，在 VPS 目录直接开发。
2. **启动/重启服务**：本地执行（实际上在 VPS 上）

   ```bash
   docker --context vps compose up -d --build
   docker --context vps compose restart <service>
   ```
3. **调试与日志**：

   ```bash
   docker --context vps logs -f <service>
   ```
4. **访问页面/面板**：本地浏览器打开 `http://localhost:8000` / `http://localhost:8080`。
5. **VNC（按需）**：仅在需要图形化场景时打开 VNC（`localhost:5901`），避免长时间占用带宽。

---

## 性能与稳定性优化

* **SSH 压缩**：已在 `~/.ssh/config` 启用 `Compression yes`。
* **VNC 优化**：降低色深（16-bit）、关闭壁纸/透明/动画。
* **网络稳定**：用 `autossh` 保活；交互 Shell 可用 `mosh`（需 VPS 安装服务端）。
* **构建提速**：尽量使用缓存层、分离依赖层；仅重建改动的服务。
* **日志观测**：优先 `docker logs`、健康检查与探针；必要时再启用 VNC。

---

## 常见问题速查

* **端口仍被公网看到？**

  * 检查 compose 是否为 `"127.0.0.1:HOST:CONTAINER"`，避免写成 `"HOST:CONTAINER"` 或 `"0.0.0.0:HOST:CONTAINER"`。
  * 再查防火墙与安全组规则。
* **VNC 卡顿明显**：

  * 首选文本/日志调试；VNC 降色深、启用 SSH 压缩；仅在必要时短时使用。
* **需要 VPS 访问本地开发口（反向）**：

  ```bash
  # 本地开启反向隧道：VPS 访问 127.0.0.1:9000 == 你的本地 3000
  ssh -N -R 9000:localhost:3000 vps
  ```
* **切回本地 Docker**：

  ```bash
  docker context use default
  ```

---

## 附录：命令速查表

```bash
# —— VPS 侧 ——
# 启动服务
docker compose up -d
# 查看监听端口
ss -lntp | grep -E '5900|8080|8000'
# UFW 基本规则
sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw allow 22/tcp && sudo ufw enable

# —— 本地（MBA）侧 ——
# SSH 本地转发（前台）
ssh -N -L 5901:127.0.0.1:5900 -L 8080:127.0.0.1:8080 -L 8000:127.0.0.1:8000 vps
# autossh 保活
autossh -M 0 -N -L 5901:127.0.0.1:5900 -L 8080:127.0.0.1:8080 -L 8000:127.0.0.1:8000 vps
# Docker 远程上下文
docker context create vps --docker "host=ssh://vps"
docker context use vps
docker --context vps compose up -d --build
docker --context vps logs -f <service>
```
