#!/bin/bash

# VNC连接测试脚本
# 用于诊断VNC连接问题

echo "🔍 VNC连接诊断工具"
echo "==================="
echo ""

# 检查SSH连接
echo "1. 检查SSH连接..."
if ssh -o ConnectTimeout=5 vps-ibbot 'echo "SSH连接正常"' 2>/dev/null; then
    echo "✅ SSH连接正常"
else
    echo "❌ SSH连接失败，请检查SSH配置"
    exit 1
fi
echo ""

# 检查VPS上的Docker服务
echo "2. 检查VPS Docker服务..."
VPS_DOCKER_STATUS=$(ssh vps-ibbot 'docker ps --format "table {{.Names}}\t{{.Status}}"' 2>/dev/null)
if echo "$VPS_DOCKER_STATUS" | grep -q "ib-gateway"; then
    echo "✅ ib-gateway容器正在运行"
    echo "$VPS_DOCKER_STATUS"
else
    echo "❌ ib-gateway容器未运行"
    echo "尝试启动服务..."
    ssh vps-ibbot 'cd ~/ibbot && docker compose -f docker/docker-compose.yml up -d'
    sleep 5
fi
echo ""

# 检查VNC进程
echo "3. 检查VNC进程..."
VNC_PROCESS=$(ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway ps aux | grep vnc | grep -v grep' 2>/dev/null)
if [ -n "$VNC_PROCESS" ]; then
    echo "✅ VNC进程正在运行"
    echo "$VNC_PROCESS"
else
    echo "⚠️  VNC进程未运行，尝试启动..."
    ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway bash -c "export DISPLAY=:1 && x11vnc -nopw -forever -display :1 -rfbport 5900 &"' 2>/dev/null
    sleep 3
    echo "VNC服务启动完成"
fi
echo ""

# 检查端口
echo "4. 检查端口状态..."
VPS_PORT=$(ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway ss -lntp | grep 5900' 2>/dev/null)
if [ -n "$VPS_PORT" ]; then
    echo "✅ VPS端口5900正在监听"
    echo "$VPS_PORT"
else
    echo "❌ VPS端口5900未监听"
fi
echo ""

# 检查本地端口转发
echo "5. 检查本地端口转发..."
LOCAL_PORT=$(lsof -i :5900 2>/dev/null)
if [ -n "$LOCAL_PORT" ]; then
    echo "✅ 本地端口5900正在使用"
    echo "$LOCAL_PORT"
else
    echo "❌ 本地端口5900未被占用，可能端口转发未启动"
    echo "请运行: ./quick-start.sh 或 ./port-forward.sh"
fi
echo ""

# 测试VNC连接
echo "6. VNC连接测试..."
echo "请尝试以下连接方式："
echo "  - VNC Viewer: vnc://localhost:5900"
echo "  - 密码: $(grep VNC_SERVER_PASSWORD ../docker/.env | cut -d'=' -f2)"
echo ""
echo "如果仍无法连接，请检查："
echo "  1. VNC客户端是否已安装"
echo "  2. 防火墙设置"
echo "  3. VNC密码是否正确"
echo "  4. 是否有其他程序占用5900端口"
echo ""
echo "🔧 故障排除完成"