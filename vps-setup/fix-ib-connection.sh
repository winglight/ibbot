#!/bin/bash

# IB Gateway连接问题修复脚本
# 解决 "Connection reset by peer" 错误

echo "🔧 IB Gateway连接问题修复脚本"
echo "====================================="

# 检查Docker服务状态
echo "📋 检查Docker服务状态..."
docker compose -f docker/docker-compose.yml ps

# 检查IB Gateway容器日志
echo ""
echo "📋 检查IB Gateway容器日志..."
docker compose -f docker/docker-compose.yml logs --tail=20 ib-gateway

# 检查端口监听状态
echo ""
echo "📋 检查IB Gateway端口监听状态..."
echo "容器内部端口状态:"
docker compose -f docker/docker-compose.yml exec ib-gateway netstat -tlnp | grep -E "(4003|4004|5900)"

echo ""
echo "宿主机端口状态:"
netstat -tlnp | grep -E "(4001|4002|5900)" 2>/dev/null || lsof -i :4001,4002,5900 2>/dev/null

# 重启IB Gateway服务
echo ""
echo "🔄 重启IB Gateway服务..."
docker compose -f docker/docker-compose.yml restart ib-gateway

echo "⏳ 等待服务启动..."
sleep 15

# 检查服务启动状态
echo "📋 检查服务启动状态..."
docker compose -f docker/docker-compose.yml ps ib-gateway

# 测试端口连通性
echo ""
echo "🔍 测试端口连通性..."
for port in 4001 4002; do
    echo -n "测试端口 $port: "
    if timeout 5 bash -c "</dev/tcp/localhost/$port" 2>/dev/null; then
        echo "✅ 连通"
    else
        echo "❌ 不通"
    fi
done

# 提供解决方案
echo ""
echo "💡 如果连接仍然失败，请尝试以下解决方案:"
echo "1. 检查IB Gateway配置:"
echo "   - 确保API连接已启用"
echo "   - 检查端口设置 (Paper: 4002, Live: 4001)"
echo "   - 确保'Accept incoming connection'已勾选"
echo ""
echo "2. 检查环境变量配置:"
echo "   - TWS_ACCEPT_INCOMING=accept"
echo "   - READ_ONLY_API=no"
echo "   - TRADING_MODE=paper (或 live)"
echo ""
echo "3. 重新构建容器:"
echo "   docker compose -f docker/docker-compose.yml down"
echo "   docker compose -f docker/docker-compose.yml up -d --build"
echo ""
echo "4. 检查防火墙设置"
echo "5. 尝试使用VNC连接查看IB Gateway界面状态"
echo "   VNC地址: localhost:5900"

echo ""
echo "🔧 修复脚本执行完成"