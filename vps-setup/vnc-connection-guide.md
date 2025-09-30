# VNC连接问题解决指南

## 问题描述
VNC连接时弹出密码输入框，输入密码后一直转圈，无法进入桌面。

## 解决方案

### 1. 服务端修复
运行修复脚本：
```bash
./fix-vnc.sh
```

### 2. 客户端设置优化

#### 推荐的VNC客户端
- **RealVNC Viewer** (推荐)
- **TightVNC Viewer**
- **TigerVNC Viewer**
- **macOS内置VNC** (Screen Sharing)

#### 连接设置
- **地址**: `localhost:5900` 或 `vnc://localhost:5900`
- **密码**: `t8Dgc9T6Y&uRI!O!`

#### 重要的客户端配置

**RealVNC Viewer设置：**
1. 右键连接 → Properties
2. Options标签页：
   - Picture quality: `Medium` 或 `Low`
   - Encryption: `Prefer off` 或 `Always off`
   - Colour level: `Medium` 或 `Low`

**TightVNC设置：**
1. Options → Connection
   - Encoding: `Tight`
   - Compression level: `9`
   - JPEG quality: `6`

**macOS Screen Sharing设置：**
1. 在Finder中按 `Cmd+K`
2. 输入: `vnc://localhost:5900`
3. 如果连接失败，尝试: `vnc://127.0.0.1:5900`

### 3. 故障排除步骤

#### 步骤1：检查端口转发
```bash
lsof -i :5900
```
应该看到SSH进程监听5900端口。

#### 步骤2：检查VPS上的VNC服务
```bash
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/local.compose.yml exec ib-gateway ps aux | grep vnc'
```

#### 步骤3：重启VNC服务
```bash
./fix-vnc.sh
```

#### 步骤4：测试不同的连接方式
1. 尝试不同的VNC客户端
2. 降低图像质量设置
3. 禁用加密
4. 使用不同的端口（如果5900被占用）

### 4. 常见问题

**问题：连接后看到黑屏或空白桌面**
- 这是正常的，因为容器中没有安装完整的桌面环境
- IB Gateway应用会在启动后显示在VNC中

**问题：连接超时**
- 检查SSH隧道是否正常：`./quick-start.sh`
- 检查防火墙设置

**问题：密码错误**
- 确认使用正确的密码：`t8Dgc9T6Y&uRI!O!`
- 某些客户端对特殊字符敏感，尝试复制粘贴

**问题：连接后立即断开**
- 尝试启用VNC的共享模式
- 检查是否有其他VNC连接占用

### 5. 高级设置

#### 修改VNC服务参数
如果默认设置不工作，可以手动调整VNC参数：

```bash
# 停止现有VNC
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/local.compose.yml exec ib-gateway pkill x11vnc'

# 启动带有不同参数的VNC
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/local.compose.yml exec ib-gateway bash -c "
export DISPLAY=:1
x11vnc -passwd t8Dgc9T6Y&uRI!O! \
    -forever \
    -display :1 \
    -rfbport 5900 \
    -shared \
    -noxrecord \
    -noxfixes \
    -noxdamage \
    -wait 20 \
    -defer 20 \
    -speeds lan \
    -quality 9 \
    -compress 0 \
    -cursor most \
    -bg
"'
```

#### 使用无密码模式（仅用于测试）
```bash
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/local.compose.yml exec ib-gateway bash -c "
export DISPLAY=:1
x11vnc -nopw -forever -display :1 -rfbport 5900 -shared -bg
"'
```

### 6. 验证连接

成功连接后，你应该能看到：
1. 黑色或灰色的桌面背景
2. IB Gateway登录窗口（如果应用正在运行）
3. 鼠标光标可以正常移动
4. 可以与应用程序交互

如果仍然无法连接，请运行诊断脚本：
```bash
./test-vnc.sh
```