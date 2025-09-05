#!/bin/bash

# VPS连接诊断脚本
# 用于诊断VPS环境与本地环境的连接差异

echo "====================================="
echo "🔍 VPS连接诊断工具"
echo "====================================="

# 检查SSH连接
echo "\n1. 检查SSH连接到VPS..."
if ssh -o ConnectTimeout=5 vps-ibbot "echo 'SSH连接正常'" 2>/dev/null; then
    echo "✅ SSH连接正常"
else
    echo "❌ SSH连接失败，请检查SSH配置"
    exit 1
fi

# 检查VPS上的Docker服务
echo "\n2. 检查VPS上的Docker服务..."
ssh vps-ibbot "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E '(ib-gateway|ibbot)'"

# 检查VPS上的端口监听
echo "\n3. 检查VPS上的端口监听状态..."
echo "检查端口 4001, 4002, 5900, 8000:"
ssh vps-ibbot "netstat -tlnp | grep -E ':(4001|4002|5900|8000)\s'"

# 检查VPS上IB Gateway容器内部端口
echo "\n4. 检查IB Gateway容器内部端口..."
echo "容器内部端口 4003, 4004:"
ssh vps-ibbot "docker exec ib-gateway netstat -tln | grep -E ':(4003|4004)\s'" 2>/dev/null || echo "⚠️ 无法检查容器内部端口"

# 检查VPS防火墙状态
echo "\n5. 检查VPS防火墙状态..."
ssh vps-ibbot "sudo ufw status" 2>/dev/null || echo "⚠️ 无法检查防火墙状态"

# 检查本地端口转发
echo "\n6. 检查本地端口转发状态..."
echo "本地端口 4001, 4002, 5900, 8080:"
netstat -an | grep -E ':(4001|4002|5900|8080)\s.*LISTEN' || echo "⚠️ 本地端口转发可能未启动"

# 测试从VPS内部连接IB Gateway
echo "\n7. 测试VPS内部连接IB Gateway..."
echo "测试连接到 localhost:4001:"
ssh vps-ibbot "timeout 3 nc -z localhost 4001 && echo '✅ VPS内部4001端口可连接' || echo '❌ VPS内部4001端口连接失败'"
echo "测试连接到 localhost:4002:"
ssh vps-ibbot "timeout 3 nc -z localhost 4002 && echo '✅ VPS内部4002端口可连接' || echo '❌ VPS内部4002端口连接失败'"

# 测试本地到VPS的端口转发
echo "\n8. 测试本地端口转发..."
echo "测试本地4001端口:"
timeout 3 nc -z localhost 4001 && echo "✅ 本地4001端口可连接" || echo "❌ 本地4001端口连接失败"
echo "测试本地4002端口:"
timeout 3 nc -z localhost 4002 && echo "✅ 本地4002端口可连接" || echo "❌ 本地4002端口连接失败"

# 检查IB Gateway日志
echo "\n9. 检查IB Gateway最近日志..."
ssh vps-ibbot "docker logs --tail 20 ib-gateway 2>/dev/null" || echo "⚠️ 无法获取IB Gateway日志"

echo "\n====================================="
echo "🔧 常见解决方案:"
echo "1. 如果VPS内部端口连接失败:"
echo "   - 重启IB Gateway: ssh vps-ibbot 'cd /path/to/docker && docker-compose restart ib-gateway'"
echo "   - 检查IB Gateway配置和日志"
echo "\n2. 如果本地端口转发失败:"
echo "   - 重新运行 ./quick-start.sh"
echo "   - 检查是否有其他程序占用端口"
echo "\n3. 如果连接被重置:"
echo "   - 检查IB Gateway是否完全启动"
echo "   - 确认API设置已启用"
echo "   - 尝试通过VNC连接查看IB Gateway界面"
echo "====================================="