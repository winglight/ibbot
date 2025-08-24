#!/bin/bash

# IB Gateway自动下载和安装脚本
# Author: Trading Bot
# Description: 自动下载并安装最新版本的IB Gateway

set -e  # 遇到错误立即退出

echo "🚀 开始下载和安装IB Gateway..."

# 定义变量
IB_GATEWAY_DIR="$HOME/Applications/IB Gateway"
DOWNLOADS_DIR="$HOME/Downloads"
LOG_FILE="ib_gateway_install.log"

# 创建日志文件
echo "$(date): Starting IB Gateway installation" > "$LOG_FILE"

# 检查是否已安装IB Gateway
check_existing_installation() {
    if [ -d "$IB_GATEWAY_DIR" ]; then
        echo "⚠️  检测到已有IB Gateway安装在: $IB_GATEWAY_DIR"
        read -p "是否要重新下载和安装最新版本? (y/N): " confirm
        if [[ $confirm != [yY] ]]; then
            echo "✅ 跳过安装，使用现有版本"
            return 1
        fi
    fi
    return 0
}

# 下载IB Gateway
download_ib_gateway() {
    echo "📥 正在下载IB Gateway..."
    
    # IB Gateway下载页面URL (稳定版本)
    DOWNLOAD_PAGE="https://www.interactivebrokers.com/en/trading/ibgateway-stable.php"
    
    # 获取当前最新的下载链接
    # 注意：IB的下载链接通常需要通过页面交互获取，这里提供一个通用的方法
    echo "🔍 获取下载链接..."
    
    # 使用curl获取页面内容并查找dmg链接
    DMG_URL=$(curl -s "$DOWNLOAD_PAGE" | grep -o 'https://[^"]*\.dmg' | head -1)
    
    if [ -z "$DMG_URL" ]; then
        echo "❌ 无法自动获取下载链接，请手动下载"
        echo "请访问: $DOWNLOAD_PAGE"
        echo "下载适用于macOS的IB Gateway dmg文件到Downloads文件夹"
        echo "然后重新运行此脚本"
        exit 1
    fi
    
    echo "📦 找到下载链接: $DMG_URL"
    
    # 提取文件名
    DMG_FILENAME=$(basename "$DMG_URL")
    DMG_PATH="$DOWNLOADS_DIR/$DMG_FILENAME"
    
    # 下载文件
    echo "⬇️  正在下载: $DMG_FILENAME"
    curl -L -o "$DMG_PATH" "$DMG_URL"
    
    if [ ! -f "$DMG_PATH" ]; then
        echo "❌ 下载失败"
        exit 1
    fi
    
    echo "✅ 下载完成: $DMG_PATH"
    echo "$DMG_PATH"
}

# 安装IB Gateway
install_ib_gateway() {
    local dmg_path="$1"
    
    echo "🔧 正在安装IB Gateway..."
    
    # 挂载DMG文件
    echo "📀 挂载DMG文件..."
    MOUNT_POINT=$(hdiutil attach "$dmg_path" | grep -o '/Volumes/[^[:space:]]*' | head -1)
    
    if [ -z "$MOUNT_POINT" ]; then
        echo "❌ DMG文件挂载失败"
        exit 1
    fi
    
    echo "✅ DMG已挂载到: $MOUNT_POINT"
    
    # 查找安装包
    PKG_FILE=$(find "$MOUNT_POINT" -name "*.pkg" -o -name "*.mpkg" | head -1)
    APP_FILE=$(find "$MOUNT_POINT" -name "*.app" | head -1)
    
    if [ -n "$PKG_FILE" ]; then
        echo "📦 找到安装包: $PKG_FILE"
        echo "🔐 正在安装 (可能需要管理员密码)..."
        sudo installer -pkg "$PKG_FILE" -target /
        echo "✅ 安装完成"
    elif [ -n "$APP_FILE" ]; then
        echo "📁 找到应用程序: $APP_FILE"
        echo "📋 正在复制到Applications文件夹..."
        cp -R "$APP_FILE" "$HOME/Applications/"
        echo "✅ 复制完成"
    else
        echo "❌ 在DMG中未找到安装文件"
        hdiutil detach "$MOUNT_POINT"
        exit 1
    fi
    
    # 卸载DMG
    echo "📤 卸载DMG文件..."
    hdiutil detach "$MOUNT_POINT"
    
    # 清理下载的DMG文件
    read -p "是否删除下载的DMG文件? (Y/n): " cleanup
    if [[ $cleanup != [nN] ]]; then
        rm -f "$dmg_path"
        echo "🗑️  已删除DMG文件"
    fi
}

# 手动安装模式
manual_install_mode() {
    echo "🔍 检查Downloads文件夹中的IB Gateway DMG文件..."
    
    # 查找最新的IB Gateway DMG文件
    DMG_FILE=$(find "$DOWNLOADS_DIR" -name "*ibg*.dmg" -o -name "*gateway*.dmg" -o -name "*IB*Gateway*.dmg" | head -1)
    
    if [ -n "$DMG_FILE" ]; then
        echo "✅ 找到DMG文件: $DMG_FILE"
        install_ib_gateway "$DMG_FILE"
    else
        echo "❌ 未找到IB Gateway DMG文件"
        echo "请手动下载IB Gateway到Downloads文件夹："
        echo "1. 访问: https://www.interactivebrokers.com/en/trading/ibgateway-stable.php"
        echo "2. 下载适用于macOS的版本"
        echo "3. 重新运行此脚本"
        exit 1
    fi
}

# 验证安装
verify_installation() {
    echo "🔍 验证安装..."
    
    # 检查常见的安装位置
    POSSIBLE_PATHS=(
        "/Applications/IB Gateway 10.37"
        "/Applications/IB Gateway"*
        "$HOME/Applications/IB Gateway"*
        "$HOME/Applications/Trader Workstation"*
        "/Applications/Trader Workstation"*
    )
    
    GATEWAY_PATH=""
    for path in "${POSSIBLE_PATHS[@]}"; do
        if [ -d "$path" ]; then
            GATEWAY_PATH="$path"
            echo "✅ 找到IB Gateway安装位置: $GATEWAY_PATH"
            break
        fi
    done
    
    if [ -z "$GATEWAY_PATH" ]; then
        echo "⚠️  无法自动定位IB Gateway安装位置"
        echo "请手动检查以下位置："
        for path in "${POSSIBLE_PATHS[@]}"; do
            echo "  - $path"
        done
        return 1
    fi
    
    # 查找可执行文件
    # 优先寻找 .app 目录
    GATEWAY_APP=$(find "$GATEWAY_PATH" -maxdepth 2 -name "*.app" -type d | head -1)
    if [ -n "$GATEWAY_APP" ]; then
        GATEWAY_EXECUTABLE="$GATEWAY_APP"
    else
        # 回退到二进制可执行文件
        GATEWAY_EXECUTABLE=$(find "$GATEWAY_PATH" -maxdepth 3 -name "ibgateway" -type f | head -1)
    fi

    if [ -n "$GATEWAY_EXECUTABLE" ]; then
        echo "✅ 找到可执行文件: $GATEWAY_EXECUTABLE"
        echo "IB_GATEWAY_PATH=\"$GATEWAY_PATH\"" > .env.gateway
        echo "IB_GATEWAY_EXECUTABLE=\"$GATEWAY_EXECUTABLE\"" >> .env.gateway
        echo "📝 IB Gateway路径已保存到.env.gateway文件"
    else
        echo "⚠️  无法找到IB Gateway可执行文件"
    fi
    
    return 0
}

# 主函数
main() {
    echo "🔧 IB Gateway自动安装程序"
    echo "========================="
    
    # 确保必要的目录存在
    mkdir -p "$HOME/Applications"
    
    # 首先验证是否已有安装
    echo "🔍 检测现有IB Gateway安装..."
    if verify_installation; then
        echo "✅ 检测到已安装的IB Gateway，无需重新安装"
        echo "📝 已生成.env.gateway配置文件"
        return 0
    fi
    
    # 没有检测到安装，开始安装流程
    echo "❌ 未检测到IB Gateway安装"
    
    # 检查现有安装
    if check_existing_installation; then
        # 尝试自动下载
        echo "🌐 尝试自动下载..."
        if DMG_PATH=$(download_ib_gateway); then
            install_ib_gateway "$DMG_PATH"
        else
            echo "⚠️  自动下载失败，切换到手动模式..."
            manual_install_mode
        fi
    fi
    
    # 验证安装
    if verify_installation; then
        echo ""
        echo "🎉 IB Gateway安装完成！"
        echo "📋 下一步："
        echo "   1. 运行 IB Gateway 并配置你的账户信息"
        echo "   2. 确保API连接已启用 (Configure -> API -> Settings)"
        echo "   3. 设置API端口为7497 (Paper Trading)或7496 (Live Trading)"
        echo "   4. 运行交易机器人: ./start.sh"
    else
        echo "❌ 安装验证失败，请检查手动安装"
        exit 1
    fi
    
    echo "$(date): IB Gateway installation completed" >> "$LOG_FILE"
}

# 运行主函数
main "$@"