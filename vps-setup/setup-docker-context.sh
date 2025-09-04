#!/bin/bash

# Docker远程上下文配置脚本
# 用于将本地Docker命令转发到VPS执行

echo "配置Docker远程上下文..."

# 创建VPS Docker上下文
echo "创建VPS Docker上下文..."
docker context create vps-ibbot --docker "host=ssh://vps-ibbot"

# 切换到VPS上下文
echo "切换到VPS Docker上下文..."
docker context use vps-ibbot

# 验证连接
echo "验证VPS Docker连接..."
docker version

if [ $? -eq 0 ]; then
    echo "✅ VPS Docker上下文配置成功！"
    echo "现在您可以在本地使用Docker命令，实际在VPS上执行"
    echo ""
    echo "常用命令:"
    echo "  docker compose -f docker/docker-compose.vps.yml up -d"
    echo "  docker compose -f docker/docker-compose.vps.yml logs -f"
    echo "  docker ps"
    echo ""
    echo "切换回本地Docker: docker context use default"
else
    echo "❌ VPS Docker连接失败，请检查SSH配置"
    exit 1
fi