# 🚀 ibbot VPS部署快速指南

## 一、VPS端配置（首次部署）

### 1. 上传并运行部署脚本
```bash
# 上传脚本
scp vps-setup/vps-deploy.sh ubuntu@147.135.37.200:~/

# SSH登录VPS并运行
ssh ubuntu@147.135.37.200
chmod +x ~/vps-deploy.sh
./vps-deploy.sh

# 重新登录使Docker权限生效
exit && ssh ubuntu@147.135.37.200
```

### 2. 上传项目文件
```bash
# 在本地项目根目录执行
rsync -avz --exclude='.git' --exclude='__pycache__' ./ ubuntu@147.135.37.200:~/ibbot/
```

### 3. 配置环境变量
```bash
# 在VPS上执行
cd ~/ibbot
cp docker/.env.example docker/.env
nano docker/.env  # 编辑配置文件
```

**必须配置的项目：**
- `TWS_USERID_PAPER`: IBKR模拟账户用户名
- `TWS_PASSWORD_PAPER`: IBKR模拟账户密码
- `VNC_SERVER_PASSWORD`: VNC访问密码
- `ADMIN_USERNAME`: 管理员用户名
- `ADMIN_PASSWORD`: 管理员密码
- `JWT_SECRET`: JWT密钥（随机字符串）

### 4. 启动服务
```bash
# 在VPS上执行
docker compose -f docker/local.compose.yml up -d
```

## 二、本地端配置（一次性）

### 1. 配置SSH快速连接
```bash
cat vps-setup/ssh-config >> ~/.ssh/config
```

### 2. 配置Docker远程上下文
```bash
./vps-setup/setup-docker-context.sh
```

## 三、日常开发使用

### 🎯 方式一：一键启动VPS开发环境
```bash
./vps-setup/quick-start.sh
```

启动后可访问：
- **ibbot Web界面**: http://localhost:8000
- **VNC (IB Gateway)**: vnc://localhost:5900

### 🎯 方式二：本地运行连接VPS IB Gateway
```bash
# 1. 先启动SSH隧道
./vps-setup/quick-start.sh

# 2. 在新终端中启动本地程序
./start-local.sh
```

启动后可访问：
- **本地Web界面**: http://127.0.0.1:8001
- **VPS Web界面**: http://localhost:8000
- **VNC (IB Gateway)**: vnc://localhost:5900

### 🐳 Docker操作
```bash
# 确保使用VPS上下文
docker context use vps-ibbot

# 查看服务状态
docker ps

# 查看日志
docker compose -f docker/local.compose.yml logs -f

# 重启服务
docker compose -f docker/local.compose.yml restart
```

### 📝 代码编辑
**推荐方式：VS Code Remote-SSH**
1. 安装 Remote-SSH 插件
2. 连接到 `vps-ibbot`
3. 打开 `~/ibbot` 目录

### 🔄 代码同步
```bash
# 同步本地修改到VPS
rsync -avz --exclude='.git' ./ ubuntu@147.135.37.200:~/ibbot/
```

## 四、常用命令

```bash
# 检查VPS服务状态
ssh vps-ibbot 'cd ~/ibbot && docker compose -f docker/local.compose.yml ps'

# 重启VPS服务
ssh vps-ibbot 'cd ~/ibbot && docker compose -f docker/local.compose.yml restart'

# 查看VPS日志
ssh vps-ibbot 'cd ~/ibbot && docker compose -f docker/local.compose.yml logs -f ibbot'

# 切换回本地Docker
docker context use default
```

## 五、故障排除

### SSH连接问题
```bash
ssh -v vps-ibbot  # 详细连接信息
```

### 端口占用问题
```bash
lsof -i :8000  # 检查端口占用
kill -9 <PID>  # 杀死占用进程
```

### VNC连接问题
```bash
# 检查VNC服务状态
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/local.compose.yml exec ib-gateway ps aux | grep vnc'

# 手动启动VNC服务
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/local.compose.yml exec ib-gateway bash -c "export DISPLAY=:1 && x11vnc -nopw -forever -display :1 -rfbport 5900 &"'

# 测试VNC连接
nc -zv localhost 5900
```

### Docker连接问题
```bash
docker context ls  # 查看上下文列表
docker context use vps-ibbot  # 切换到VPS上下文
```

---

💡 **提示**: 详细文档请参考 `README.md`