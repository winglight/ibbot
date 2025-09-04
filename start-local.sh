#!/bin/bash

# 本地启动脚本
# 使用本地环境配置连接VPS上的IB Gateway

set -e

echo "🚀 启动本地ibbot连接VPS IB Gateway"
echo "======================================"

# 检查SSH隧道是否已建立
echo "🔍 检查SSH隧道状态..."
if lsof -i :4002 >/dev/null 2>&1; then
    echo "✅ 端口4002已转发（IB Gateway连接）"
else
    echo "❌ 端口4002未转发，请先运行:"
    echo "   ./vps-setup/quick-start.sh"
    exit 1
fi

if lsof -i :8000 >/dev/null 2>&1; then
    echo "✅ 端口8000已转发（VPS Web UI）"
else
    echo "⚠️  端口8000未转发，VPS Web UI不可用"
fi

# 检查本地环境配置
if [ ! -f ".env.local" ]; then
    echo "❌ .env.local文件不存在，请先配置"
    exit 1
fi

echo "✅ 环境检查完成"
echo ""
echo "📱 启动信息:"
echo "   - 本地Web UI: http://127.0.0.1:8001"
echo "   - VPS Web UI: http://localhost:8000 (通过SSH隧道)"
echo "   - IB Gateway: 通过localhost:4002连接VPS"
echo "======================================"
echo ""

# 使用本地环境配置启动
export $(cat .env.local | grep -v '^#' | xargs)
python main.py