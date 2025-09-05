# ibbot VPS远程开发部署指南

基于远程开发方案A，实现在VPS上部署ibbot，本地通过SSH隧道进行开发和访问。

## 环境信息

- **VPS**: 147.135.37.200 (ubuntu用户)
- **本地**: macOS (chenyu用户)
- **SSH**: 已配置密钥登录

## 部署步骤

### 第一阶段：VPS端配置

#### 1. 上传部署脚本到VPS

```bash
# 在本地执行
scp vps-setup/vps-deploy.sh ubuntu@147.135.37.200:~/
```

#### 2. 在VPS上运行部署脚本

```bash
# SSH登录VPS
ssh ubuntu@147.135.37.200

# 运行部署脚本
chmod +x ~/vps-deploy.sh
./vps-deploy.sh

# 重新登录以使Docker组权限生效
exit
ssh ubuntu@147.135.37.200
```

#### 3. 上传项目文件到VPS

```bash
# 在本地项目根目录执行
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  ./ ubuntu@147.135.37.200:~/ibbot/
```

#### 4. 配置环境变量

```bash
# SSH登录VPS
ssh ubuntu@147.135.37.200

# 进入项目目录
cd ~/ibbot

# 复制环境变量模板
cp docker/.env.example docker/.env

# 编辑环境变量（请根据实际情况修改）
nano docker/.env
```

**重要配置项：**
```bash
# IBKR账户信息
TWS_USERID_PAPER=your_paper_account
TWS_PASSWORD_PAPER=your_password

# VNC密码
VNC_SERVER_PASSWORD=your_vnc_password

# 管理员账户
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your_admin_password
JWT_SECRET=your_jwt_secret

# 时区
TIME_ZONE=Asia/Shanghai
```

#### 5. 启动服务

```bash
# 在VPS上执行
cd ~/ibbot
docker compose -f docker/docker-compose.yml up -d

# 查看服务状态
docker compose -f docker/docker-compose.yml ps

# 查看日志
docker compose -f docker/docker-compose.yml logs -f
```

### 第二阶段：本地端配置

#### 1. 配置SSH快速连接

```bash
# 在本地执行
cat vps-setup/ssh-config >> ~/.ssh/config

# 测试SSH连接
ssh vps-ibbot
```

#### 2. 设置Docker远程上下文

```bash
# 在本地项目根目录执行
chmod +x vps-setup/setup-docker-context.sh
./vps-setup/setup-docker-context.sh
```

#### 3. 建立端口转发

**方式一：一键启动（推荐）**
```bash
chmod +x vps-setup/quick-start.sh
./vps-setup/quick-start.sh
```

**方式二：普通SSH转发**
```bash
chmod +x vps-setup/port-forward.sh
./vps-setup/port-forward.sh
```

**方式三：autossh保活转发**
```bash
chmod +x vps-setup/autossh-forward.sh
./vps-setup/autossh-forward.sh
```

### 第三阶段：开发工作流

#### 1. 访问服务

- **ibbot Web界面**: http://localhost:8000
- **VNC (IB Gateway)**: vnc://localhost:5900 (使用VNC客户端)
- **IBKR API**: localhost:4001, localhost:4002

#### 2. 代码编辑

**方式一：VS Code Remote-SSH（推荐）**
```bash
# 安装VS Code Remote-SSH插件
# 然后连接到 vps-ibbot
code --remote ssh-remote+vps-ibbot ~/ibbot
```

**方式二：本地编辑+同步**
```bash
# 实时同步本地修改到VPS
fswatch -o . | xargs -n1 -I{} rsync -avz --exclude='.git' \
  ./ ubuntu@147.135.37.200:~/ibbot/
```

#### 3. Docker操作

```bash
# 确保使用VPS上下文
docker context use vps-ibbot

# 重启服务
docker compose -f docker/docker-compose.yml restart

# 查看日志
docker compose -f docker/docker-compose.yml logs -f ibbot

# 进入容器
docker compose -f docker/docker-compose.yml exec ibbot bash

# 重新构建
docker compose -f docker/docker-compose.yml up -d --build
```

#### 4. 切换回本地Docker

```bash
docker context use default
```

## 常用命令速查

### VPS操作
```bash
# 查看服务状态
ssh vps-ibbot 'cd ~/ibbot && docker compose -f docker/docker-compose.yml ps'

# 重启服务
ssh vps-ibbot 'cd ~/ibbot && docker compose -f docker/docker-compose.yml restart'

# 查看系统资源
ssh vps-ibbot 'htop'
```

### 本地操作
```bash
# 同步代码到VPS
rsync -avz --exclude='.git' ./ ubuntu@147.135.37.200:~/ibbot/

# 建立端口转发
./vps-setup/autossh-forward.sh

# 使用VPS Docker
docker context use vps-ibbot
docker ps
```

## 故障排除

### 1. SSH连接问题
```bash
# 测试SSH连接
ssh -v vps-ibbot

# 检查SSH密钥
ssh-add -l
```

### 2. Docker连接问题
```bash
# 检查Docker上下文
docker context ls

# 重新创建上下文
docker context rm vps-ibbot
./vps-setup/setup-docker-context.sh
```

### 3. 端口转发问题
```bash
# 检查本地端口占用
lsof -i :8000

# 杀死占用端口的进程
kill -9 <PID>
```

### 4. VPS服务问题
```bash
# 检查VPS端口监听
ssh vps-ibbot 'ss -lntp | grep -E "8000|5900|4001|4002"'

# 检查防火墙状态
ssh vps-ibbot 'sudo ufw status'

# 查看Docker日志
ssh vps-ibbot 'cd ~/ibbot && docker compose -f docker/docker-compose.yml logs'
```

### 5. VNC连接问题
```bash
# 检查VNC服务状态
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway ps aux | grep vnc'

# 检查容器内5900端口
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway ss -lntp | grep 5900'

# 手动启动VNC服务（如果未运行）
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway bash -c "export DISPLAY=:1 && x11vnc -nopw -forever -display :1 -rfbport 5900 &"'

# 测试本地VNC连接
nc -zv localhost 5900
```

**VNC连接说明：**
- 使用VNC客户端连接 `vnc://localhost:5900`
- 如果连接失败，可能需要在容器内手动启动VNC服务
- VNC服务配置为无密码访问（仅通过SSH隧道安全访问）

## 安全注意事项

1. **端口安全**: 所有服务端口仅绑定到127.0.0.1，不暴露到公网
2. **防火墙**: VPS仅开放SSH端口(22)
3. **SSH安全**: 使用密钥登录，禁用密码登录
4. **VNC安全**: VNC仅通过SSH隧道访问，设置强密码

## 性能优化

1. **SSH压缩**: 已在配置中启用
2. **连接复用**: 使用ControlMaster减少连接开销
3. **autossh保活**: 自动重连断开的SSH隧道
4. **Docker缓存**: 利用Docker层缓存加速构建

## 备份策略

```bash
# 备份VPS数据
ssh vps-ibbot 'cd ~/ibbot && tar -czf backup-$(date +%Y%m%d).tar.gz data/ logs/ docker/.env'

# 下载备份到本地
scp vps-ibbot:~/ibbot/backup-*.tar.gz ./backups/
```