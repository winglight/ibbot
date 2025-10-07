#!/usr/bin/env python3
"""
演示如何在命令行启动时加载 .env 文件的脚本

使用方法:
1. 直接运行: python start_with_env.py
2. 启动特定服务: python start_with_env.py orders
3. 使用 python-dotenv: python -m dotenv -f .env.local run uvicorn services.orders.main:app --host 0.0.0.0 --port 8050
"""

import os
import sys
from pathlib import Path

def load_env_files():
    """加载环境变量文件，优先级：.env.local > .env"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("❌ 需要安装 python-dotenv: pip install python-dotenv")
        return False
    
    project_root = Path(__file__).parent
    env_local = project_root / ".env.local"
    env_file = project_root / ".env"
    
    loaded_files = []
    
    # 优先加载 .env.local
    if env_local.exists():
        load_dotenv(env_local, override=True)
        loaded_files.append(".env.local")
        print(f"✅ 已加载 {env_local}")
    
    # 然后加载 .env（不覆盖已存在的变量）
    if env_file.exists():
        load_dotenv(env_file, override=False)
        loaded_files.append(".env")
        print(f"✅ 已加载 {env_file}")
    
    if not loaded_files:
        print("⚠️  未找到 .env.local 或 .env 文件")
        return False
    
    return True

def check_required_env():
    """检查必需的环境变量"""
    required_vars = [
        "IBBOT_DB_URL",
        "IBBOT_REDIS_URL"
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ 缺少必需的环境变量: {', '.join(missing_vars)}")
        return False
    
    print("✅ 所有必需的环境变量都已设置")
    return True

def show_env_info():
    """显示关键环境变量信息"""
    print("\n📋 关键环境变量:")
    key_vars = [
        "IBBOT_DB_URL",
        "IBBOT_REDIS_URL", 
        "WEB_HOST",
        "WEB_PORT"
    ]
    
    for var in key_vars:
        value = os.getenv(var, "未设置")
        # 隐藏敏感信息
        if "password" in var.lower() or "secret" in var.lower():
            value = "***"
        elif "://" in str(value):
            # 隐藏URL中的密码
            import re
            value = re.sub(r'://([^:]+):([^@]+)@', r'://\1:***@', str(value))
        
        print(f"  {var}: {value}")

def start_service(service_name=None):
    """启动服务"""
    if service_name:
        print(f"\n🚀 启动 {service_name} 服务...")
        # 这里可以添加启动特定服务的逻辑
        print(f"可以使用以下命令启动 {service_name} 服务:")
        print(f"uvicorn services.{service_name}.main:app --host 0.0.0.0 --port 8050")
    else:
        print("\n💡 环境变量已加载，现在可以启动任何服务")
        print("示例命令:")
        print("  uvicorn services.orders.main:app --host 0.0.0.0 --port 8050")
        print("  uvicorn services.strategy.main:app --host 0.0.0.0 --port 8045")

def main():
    """主函数"""
    print("🔧 IBBot 环境变量加载器")
    print("=" * 50)
    
    # 加载环境变量
    if not load_env_files():
        sys.exit(1)
    
    # 检查必需的环境变量
    if not check_required_env():
        sys.exit(1)
    
    # 显示环境变量信息
    show_env_info()
    
    # 启动服务
    service_name = sys.argv[1] if len(sys.argv) > 1 else None
    start_service(service_name)

if __name__ == "__main__":
    main()