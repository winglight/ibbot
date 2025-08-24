#!/bin/bash

# 启动脚本：使用 Conda py310 环境，自动安装/启动 IB Gateway，并启动交易机器人
set -e

echo "=============================="
echo "🚀 启动 IBKR Trading Bot"
echo "=============================="

# 1) 检查 .env
if [ ! -f ".env" ]; then
  echo "⚠️ 未找到 .env，正在从 .env.example 复制..."
  cp .env.example .env
  echo "请先编辑 .env 配置后再运行。"
  exit 1
fi

# 2) 创建必要目录
mkdir -p logs data

# 3) 检查 Conda 并启用
if ! command -v conda &> /dev/null; then
  echo "❌ 未检测到 conda，请先安装 Miniconda/Anaconda 后重试。"
  exit 1
fi

CONDA_BASE=$(conda info --base)
# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"

# 4) 创建/激活 py310 环境（Python 3.10）
ENV_NAME="py310"
if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "📦 创建 Conda 环境: $ENV_NAME (python=3.10)"
  conda create -y -n "$ENV_NAME" python=3.10
fi

echo "✅ 激活 Conda 环境: $ENV_NAME"
conda activate "$ENV_NAME"

# 5) 安装 Python 依赖
echo "📥 安装 Python 依赖..."
python -m pip install --upgrade pip
pip install -r requirements.txt

# 6) 自动检测/安装 IB Gateway（不自动启动）
# 优先尝试检测系统中已安装的 IB Gateway 并生成 .env.gateway
if [ -f "./docker/install_ib_gateway.sh" ]; then
  echo "🔍 检测系统中的 IB Gateway 安装..."
  bash ./docker/install_ib_gateway.sh || echo "⚠️ IB Gateway 检测/安装流程出现警告，可稍后手动处理。"
else
  echo "⚠️ 缺少 docker/install_ib_gateway.sh，无法自动检测/安装 IB Gateway。"
fi

# 不自动启动 IB Gateway，仅提示用户手动启动
if [ -f ".env.gateway" ]; then
  # shellcheck source=/dev/null
  source .env.gateway
  if [ -n "$IB_GATEWAY_EXECUTABLE" ] && [ -e "$IB_GATEWAY_EXECUTABLE" ]; then
    echo "ℹ️ 已检测到 IB Gateway 安装位置：$IB_GATEWAY_EXECUTABLE"
    echo ""
    echo "👉 请确保 IB Gateway 已启动并完成以下设置："
    echo "   1. 完成登录（用户名/密码）"
    echo "   2. Configure -> API -> Settings"
    echo "   3. 勾选 'Enable ActiveX and Socket Clients'"
    echo "   4. Socket port: 4002 (Paper Trading) 或 4001 (Live Trading)"
    echo "   5. 可选：勾选 'Create API message log file'"
    echo ""
    read -p "👆 设置完成后按 Enter 继续启动交易机器人..."
  else
    echo "ℹ️ 未能定位 IB Gateway 可执行文件，请手动启动 IB Gateway。"
    echo "👉 启动后请确保 API 端口设置为 4002 (Paper) 或 4001 (Live)"
    read -p "👆 IB Gateway 启动并配置完成后按 Enter 继续..."
  fi
else
  echo "ℹ️ 未找到 .env.gateway，请手动启动 IB Gateway。"
  echo "👉 启动后请确保 API 端口设置为 4002 (Paper) 或 4001 (Live)"
  read -p "👆 IB Gateway 启动并配置完成后按 Enter 继续..."
fi

# 7) 启动应用
echo "🚀 启动应用..."
python main.py