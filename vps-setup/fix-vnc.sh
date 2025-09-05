#!/bin/bash

# VNC连接修复脚本
# 解决VNC连接后一直转圈的问题

echo "🔧 修复VNC连接问题"
echo "=================="
echo ""

# 停止现有的VNC服务
echo "1. 停止现有VNC服务..."
ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway pkill x11vnc' 2>/dev/null
sleep 2

# 检查Xvfb显示服务
echo "2. 检查显示服务..."
XVFB_STATUS=$(ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway ps aux | grep Xvfb | grep -v grep')
if [ -n "$XVFB_STATUS" ]; then
    echo "✅ Xvfb显示服务正在运行"
else
    echo "⚠️  Xvfb显示服务未运行，尝试启动..."
    ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway bash -c "Xvfb :1 -ac -screen 0 1024x768x24 &"'
    sleep 3
fi
echo ""

# 启动优化的VNC服务
echo "3. 启动优化的VNC服务..."
VNC_PASSWORD=$(grep VNC_SERVER_PASSWORD ../docker/.env | cut -d'=' -f2)

# 使用更兼容的VNC参数
ssh vps-ibbot "docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway bash -c '
export DISPLAY=:1
x11vnc -passwd $VNC_PASSWORD \
    -forever \
    -display :1 \
    -rfbport 5900 \
    -shared \
    -noxrecord \
    -noxfixes \
    -noxdamage \
    -wait 10 \
    -defer 10 \
    -speeds modem \
    -quality 6 \
    -compress 9 \
    -cursor arrow \
    -bg
'"

sleep 3

# 验证VNC服务状态
echo "4. 验证VNC服务状态..."
VNC_PROCESS=$(ssh vps-ibbot 'docker compose -f ~/ibbot/docker/docker-compose.yml exec ib-gateway ps aux | grep x11vnc | grep -v grep')
if [ -n "$VNC_PROCESS" ]; then
    echo "✅ VNC服务启动成功"
    echo "$VNC_PROCESS"
else
    echo "❌ VNC服务启动失败"
    exit 1
fi
echo ""

# 测试连接
echo "5. 连接信息"
echo "VNC地址: vnc://localhost:5900"
echo "密码: $VNC_PASSWORD"
echo ""
echo "💡 连接提示:"
echo "  1. 使用VNC Viewer或其他VNC客户端"
echo "  2. 如果仍然转圈，尝试以下方法:"
echo "     - 使用不同的VNC客户端（如RealVNC、TightVNC）"
echo "     - 在VNC客户端中禁用加密"
echo "     - 设置颜色深度为256色或更低"
echo "     - 降低图像质量设置"
echo ""
echo "🔧 VNC修复完成"