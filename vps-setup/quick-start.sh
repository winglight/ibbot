#!/bin/bash

# ibbot VPS快速启动脚本
# 一键启动VPS开发环境

# 优雅退出处理
trap 'echo ""; echo "🛑 SSH隧道已停止"; echo "✅ 端口转发已断开"; exit 0' INT TERM

echo "🚀 ibbot VPS开发环境快速启动"
echo "======================================"

# 检查SSH连接
echo "📡 检查VPS连接..."
SSH_TARGET="vps-ibbot"
SSH_OPTS=(-o BatchMode=yes \
          -o ConnectTimeout=5 \
          -o PreferredAuthentications=publickey \
          -o PasswordAuthentication=no \
          -o KbdInteractiveAuthentication=no)


if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" 'echo "VPS连接正常"'; then
    echo "✅ VPS连接正常"
else
    echo "❌ VPS连接失败，请检查:"
    echo "   1. SSH配置是否正确"
    echo "   2. VPS是否在线"
    echo "   3. 网络连接是否正常"
    echo "   4. 是否已配置免密登陆 (如需密码/二次认证请先手动执行: ssh $SSH_TARGET)"
    exit 1
fi



# 设置Docker远程上下文
echo "🐳 配置Docker远程上下文..."
if docker context ls | grep -q "vps-ibbot"; then
    echo "✅ Docker上下文已存在"
else
    echo "创建Docker上下文..."
    docker context create vps-ibbot --docker "host=ssh://vps-ibbot"
fi

docker context use vps-ibbot
echo "✅ 已切换到VPS Docker上下文"

# 检查本地端口占用和清理SSH隧道
echo "🔌 检查本地端口..."
# 端口说明：
# - 7800: ib-controller API
# - 5900: IB Gateway VNC
# - 5800: IB Gateway noVNC Web UI（浏览器登录入口）
# - 4001/4002: IB API（SSL/明文）
# - 8000/8001: 主 Web 服务 / API 服务
# - 4173: Optimizer UI
# - 8010/8011/8020/8030/8045/8050/8055/8056: 各业务子服务
# - 3306: MariaDB
# - 6379: Redis
PORTS=(7800 5900 5800 4001 4002 8001 8000 4173 8010 8011 8020 8030 8045 8050 8055 8056 3306 6379)

# 先清理可能残留的SSH隧道进程
echo "🧹 清理残留的SSH隧道进程..."
# 清理所有与vps-ibbot相关的SSH隧道
pkill -f "ssh.*vps-ibbot.*-L" 2>/dev/null || true
pkill -f "autossh.*vps-ibbot" 2>/dev/null || true
# 清理可能监听目标端口的SSH进程
for port in "${PORTS[@]}"; do
    SSH_PIDS=$(lsof -ti :$port 2>/dev/null | grep -v '^$' || true)
    if [ -n "$SSH_PIDS" ]; then
        echo "清理占用端口 $port 的进程: $SSH_PIDS"
        echo "$SSH_PIDS" | xargs kill -9 2>/dev/null || true
    fi
done
sleep 2

BUSY_PORTS=()
for port in "${PORTS[@]}"; do
    if lsof -i :$port >/dev/null 2>&1; then
        echo "⚠️  端口 $port 已被占用，尝试释放..."
        lsof -ti :$port | xargs kill -9 2>/dev/null || true
        sleep 1
        if lsof -i :$port >/dev/null 2>&1; then
            BUSY_PORTS+=("$port")
        fi
    fi
done

if [ ${#BUSY_PORTS[@]} -ne 0 ]; then
    echo "❌ 以下端口仍被占用，无法建立端口转发: ${BUSY_PORTS[*]}"
    echo "👉 请手动关闭相关程序或调整脚本中的端口配置后重试"
    exit 1
fi

echo "✅ 端口检查完成"

# 启动端口转发
echo "🌉 启动SSH隧道..."
echo "正在建立端口转发，请保持此窗口打开"
echo ""
echo "🎯 访问地址:"
echo "   📱 ibbot Web界面: http://localhost:8000"
echo "   🖥️  VNC (IB Gateway): localhost:5900"
echo "   🔌 IBKR API: localhost:4001, localhost:4002"
echo ""
echo "💡 提示:"
echo "   - 按 Ctrl+C 停止端口转发"
echo "   - 在新终端中运行: docker context use vps-ibbot"
echo "   - 然后可以使用: docker ps, docker logs 等命令"
echo "======================================"

# 启动autossh端口转发
if command -v autossh &> /dev/null; then
    echo "🔗 使用autossh启动端口转发..."
    autossh -M 0 -N \
        -o "ServerAliveInterval 30" \
        -o "ServerAliveCountMax 3" \
        -o "ExitOnForwardFailure yes" \
        -L 7800:127.0.0.1:7800 \
        -L 5900:127.0.0.1:5900 \
        -L 5800:127.0.0.1:5800 \
        -L 4001:127.0.0.1:4001 \
        -L 4002:127.0.0.1:4002 \
        -L 8001:127.0.0.1:8001 \
        -L 8000:127.0.0.1:8000 \
        -L 4173:127.0.0.1:4173 \
        -L 8010:127.0.0.1:8010 \
        -L 8011:127.0.0.1:8011 \
        -L 8020:127.0.0.1:8020 \
        -L 8030:127.0.0.1:8030 \
        -L 8045:127.0.0.1:8045 \
        -L 8050:127.0.0.1:8050 \
        -L 8055:127.0.0.1:8055 \
        -L 8056:127.0.0.1:8056 \
        -L 3306:127.0.0.1:3306 \
        -L 6379:127.0.0.1:6379 \
        vps-ibbot
else
    echo "⚠️  autossh未安装，使用普通SSH转发"
    echo "建议安装autossh: brew install autossh"
    echo "🔗 使用SSH启动端口转发..."
    ssh -N \
        -o "ServerAliveInterval 30" \
        -o "ServerAliveCountMax 3" \
        -o "ExitOnForwardFailure yes" \
        -L 7800:127.0.0.1:7800 \
        -L 5900:127.0.0.1:5900 \
        -L 5800:127.0.0.1:5800 \
        -L 4001:127.0.0.1:4001 \
        -L 4002:127.0.0.1:4002 \
        -L 8001:127.0.0.1:8001 \
        -L 8000:127.0.0.1:8000 \
        -L 4173:127.0.0.1:4173 \
        -L 8010:127.0.0.1:8010 \
        -L 8011:127.0.0.1:8011 \
        -L 8020:127.0.0.1:8020 \
        -L 8030:127.0.0.1:8030 \
        -L 8045:127.0.0.1:8045 \
        -L 8050:127.0.0.1:8050 \
        -L 8055:127.0.0.1:8055 \
        -L 8056:127.0.0.1:8056 \
        -L 3306:127.0.0.1:3306 \
        -L 6379:127.0.0.1:6379 \
        vps-ibbot
fi
