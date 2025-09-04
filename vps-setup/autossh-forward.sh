#!/bin/bash

# ibbot VPS autossh端口转发脚本（保活版本）
# 使用autossh确保SSH隧道连接稳定

# 检查autossh是否安装
if ! command -v autossh &> /dev/null; then
    echo "autossh未安装，正在安装..."
    brew install autossh
fi

echo "正在使用autossh建立稳定的SSH隧道连接..."
echo "VPS: 147.135.37.200"
echo "本地访问地址:"
echo "  - ibbot Web界面: http://localhost:8000"
echo "  - VNC (IB Gateway): localhost:5901"
echo "  - IBKR API端口: localhost:4001, localhost:4002"
echo ""
echo "autossh将自动重连，按 Ctrl+C 停止"
echo "======================================"

# 使用autossh建立稳定的端口转发
autossh -M 0 -N \
  -o "ServerAliveInterval 30" \
  -o "ServerAliveCountMax 3" \
  -L 8000:127.0.0.1:8000 \
  -L 5901:127.0.0.1:5900 \
  -L 4001:127.0.0.1:4001 \
  -L 4002:127.0.0.1:4002 \
  vps-ibbot