#!/bin/bash

# ibbot VPS端口转发脚本
# 用于建立SSH隧道访问VPS上的服务

echo "正在建立SSH隧道连接到VPS..."
echo "VPS: 147.135.37.200"
echo "本地访问地址:"
echo "  - ibbot Web界面: http://localhost:8000"
echo "  - VNC (IB Gateway): localhost:5901"
echo "  - IBKR API端口: localhost:4001, localhost:4002"
echo ""
echo "按 Ctrl+C 停止端口转发"
echo "======================================"

# 建立多个端口转发
ssh -N \
  -L 8000:127.0.0.1:8000 \
  -L 5901:127.0.0.1:5900 \
  -L 4001:127.0.0.1:4001 \
  -L 4002:127.0.0.1:4002 \
  vps-ibbot