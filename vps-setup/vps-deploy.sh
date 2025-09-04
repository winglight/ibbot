#!/bin/bash

# VPS部署脚本
# 在VPS上运行此脚本来安装和配置必要的环境

set -e

echo "开始配置VPS环境..."
echo "======================================"

# 更新系统
echo "更新系统包..."
sudo apt update && sudo apt upgrade -y

# 安装必要的软件
echo "安装必要的软件..."
sudo apt install -y \
    curl \
    wget \
    git \
    ufw \
    htop \
    unzip

# 安装Docker
if ! command -v docker &> /dev/null; then
    echo "安装Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
else
    echo "Docker已安装"
fi

# 安装Docker Compose
if ! command -v docker-compose &> /dev/null; then
    echo "安装Docker Compose..."
    sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
else
    echo "Docker Compose已安装"
fi

# 配置防火墙
echo "配置防火墙..."
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw --force enable

# 创建项目目录
echo "创建项目目录..."
mkdir -p ~/ibbot
cd ~/ibbot

# 克隆项目（如果还没有）
if [ ! -d ".git" ]; then
    echo "请手动上传项目文件到 ~/ibbot 目录"
    echo "或者使用git clone命令克隆项目"
fi

echo "======================================"
echo "✅ VPS环境配置完成！"
echo ""
echo "下一步:"
echo "1. 上传项目文件到 ~/ibbot 目录"
echo "2. 配置 docker/.env 文件"
echo "3. 运行: docker compose -f docker/docker-compose.vps.yml up -d"
echo ""
echo "注意: 请重新登录以使Docker组权限生效"
echo "或运行: newgrp docker"