#!/bin/bash

# IBBot 服务启动脚本
# 使用方法: ./start_service.sh [service_name] [port]

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 服务配置
declare -A SERVICES=(
    ["orders"]="services.orders.main:app 8050"
    ["strategy"]="services.strategy.main:app 8045"
    ["ib_feed"]="services.ib_feed.main:app 8015"
    ["account"]="services.account.main:app 8020"
    ["risk"]="services.risk.main:app 8030"
    ["market_data"]="services.market_data.main:app 8010"
    ["notification"]="services.notification.main:app 8055"
    ["main"]="main:app 8005"
)

# 显示帮助信息
show_help() {
    echo -e "${BLUE}IBBot 服务启动脚本${NC}"
    echo "使用方法: $0 [service_name] [port]"
    echo ""
    echo "可用服务:"
    for service in "${!SERVICES[@]}"; do
        IFS=' ' read -r app default_port <<< "${SERVICES[$service]}"
        echo -e "  ${GREEN}$service${NC} - $app (默认端口: $default_port)"
    done
    echo ""
    echo "示例:"
    echo "  $0 orders          # 启动 orders 服务在默认端口 8050"
    echo "  $0 strategy 8046   # 启动 strategy 服务在端口 8046"
    echo "  $0 list            # 列出所有可用服务"
}

# 列出所有服务
list_services() {
    echo -e "${BLUE}可用的服务:${NC}"
    for service in "${!SERVICES[@]}"; do
        IFS=' ' read -r app default_port <<< "${SERVICES[$service]}"
        echo -e "  ${GREEN}$service${NC} - $app (端口: $default_port)"
    done
}

# 检查环境文件
check_env_files() {
    if [[ -f ".env.local" ]]; then
        echo -e "${GREEN}✅ 找到 .env.local 文件${NC}"
        return 0
    elif [[ -f ".env" ]]; then
        echo -e "${YELLOW}⚠️  使用 .env 文件 (建议创建 .env.local)${NC}"
        return 0
    else
        echo -e "${RED}❌ 未找到 .env.local 或 .env 文件${NC}"
        return 1
    fi
}

check_ib_async() {
    if python -c "import ib_async" >/dev/null 2>&1; then
        echo -e "${GREEN}✅ 检测到 ib_async 库，可直接连接 IBKR${NC}"
        return 0
    fi

    if python -c "from test.ib_insync_stub import IB" >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  未检测到 ib_async，将使用 test.ib_insync_stub.IB 进行本地模拟${NC}"
        return 0
    fi

    echo -e "${RED}❌ 未找到 ib_async 库或兼容的测试 stub，请先运行 pip install -r requirements.txt${NC}"
    return 1
}

# 启动服务
start_service() {
    local service_name="$1"
    local custom_port="$2"
    
    if [[ -z "$service_name" ]]; then
        show_help
        exit 1
    fi
    
    if [[ "$service_name" == "help" || "$service_name" == "-h" || "$service_name" == "--help" ]]; then
        show_help
        exit 0
    fi
    
    if [[ "$service_name" == "list" ]]; then
        list_services
        exit 0
    fi
    
    if [[ ! -v SERVICES["$service_name"] ]]; then
        echo -e "${RED}❌ 未知服务: $service_name${NC}"
        echo ""
        list_services
        exit 1
    fi
    
    # 检查环境文件
    if ! check_env_files; then
        exit 1
    fi

    # 检查 ib_async 依赖（允许主函数预先标记已完成）
    if [[ -z "${IBBOT_IB_ASYNC_CHECKED:-}" ]]; then
        if ! check_ib_async; then
            exit 1
        fi
    fi
    
    # 获取服务配置
    IFS=' ' read -r app default_port <<< "${SERVICES[$service_name]}"
    local port="${custom_port:-$default_port}"
    
    # 确定使用的环境文件
    local env_file=".env.local"
    if [[ ! -f ".env.local" ]]; then
        env_file=".env"
    fi
    
    echo -e "${BLUE}🚀 启动服务: $service_name${NC}"
    echo -e "${BLUE}📱 应用: $app${NC}"
    echo -e "${BLUE}🌐 端口: $port${NC}"
    echo -e "${BLUE}📄 环境文件: $env_file${NC}"
    echo -e "${BLUE}🔗 URL: http://localhost:$port${NC}"
    echo ""
    
    # 检查端口是否被占用
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  端口 $port 已被占用${NC}"
        read -p "是否继续? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
    
    # 启动服务
    echo -e "${GREEN}启动中...${NC}"
    python -m dotenv -f "$env_file" run uvicorn "$app" --host 0.0.0.0 --port "$port"
}

# 主函数
main() {
    # 检查是否在项目根目录
    if [[ ! -f "pyproject.toml" && ! -f "requirements.txt" ]]; then
        echo -e "${RED}❌ 请在项目根目录下运行此脚本${NC}"
        exit 1
    fi
    
    # 检查 python-dotenv 是否安装
    if ! python -c "import dotenv" 2>/dev/null; then
        echo -e "${YELLOW}⚠️  python-dotenv 未安装，正在安装...${NC}"
        pip install python-dotenv
    fi

    if ! check_ib_async; then
        exit 1
    fi

    export IBBOT_IB_ASYNC_CHECKED=1

    start_service "$1" "$2"
}

# 运行主函数
main "$@"
