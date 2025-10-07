# 在命令行启动时加载 .env 文件的方法

## 方法1: 使用 python-dotenv 命令行工具 (推荐)

```bash
# 安装 python-dotenv (如果还没安装)
pip install python-dotenv

# 使用 .env.local 文件启动服务
python -m dotenv -f .env.local run uvicorn services.orders.main:app --host 0.0.0.0 --port 8050

# 使用 .env 文件启动服务
python -m dotenv -f .env run uvicorn services.orders.main:app --host 0.0.0.0 --port 8050

# 自动查找 .env 文件启动服务
python -m dotenv run uvicorn services.orders.main:app --host 0.0.0.0 --port 8050
```

## 方法2: 使用项目提供的 run_service.py 脚本

```bash
# 启动 orders 服务
python scripts/run_service.py orders

# 启动 strategy 服务  
python scripts/run_service.py strategy

# 启动 ib_feed 服务
python scripts/run_service.py ib_feed
```

## 方法3: 使用自定义的环境变量加载脚本

```bash
# 运行我们刚创建的演示脚本
python start_with_env.py

# 或者指定服务名称
python start_with_env.py orders
```

## 方法4: 在 shell 中手动导出环境变量

```bash
# 使用 export 命令加载 .env 文件
export $(grep -v '^#' .env.local | xargs)

# 然后启动服务
uvicorn services.orders.main:app --host 0.0.0.0 --port 8050
```

## 方法5: 使用 source 命令 (需要将 .env 转换为 shell 格式)

```bash
# 创建一个 shell 兼容的环境文件
sed 's/^/export /' .env.local > .env.local.sh

# 加载环境变量
source .env.local.sh

# 启动服务
uvicorn services.orders.main:app --host 0.0.0.0 --port 8050
```

## 方法6: 使用 Docker Compose (如果有 docker-compose.yml)

```bash
# 使用 docker-compose，它会自动加载 .env 文件
docker-compose up orders
```

## 推荐的启动命令

### 启动 Orders 服务
```bash
python -m dotenv -f .env.local run uvicorn services.orders.main:app --host 0.0.0.0 --port 8050
```

### 启动 Strategy 服务
```bash
python -m dotenv -f .env.local run uvicorn services.strategy.main:app --host 0.0.0.0 --port 8045
```

### 启动 IB Feed 服务
```bash
python -m dotenv -f .env.local run uvicorn services.ib_feed.main:app --host 0.0.0.0 --port 8015
```

### 启动主 Web 应用
```bash
python -m dotenv -f .env.local run uvicorn main:app --host 0.0.0.0 --port 8005
```

## 验证环境变量是否正确加载

```bash
# 检查数据库 URL
python -m dotenv -f .env.local run python -c "import os; print('DB URL:', os.getenv('IBBOT_DB_URL'))"

# 检查 Redis URL  
python -m dotenv -f .env.local run python -c "import os; print('Redis URL:', os.getenv('IBBOT_REDIS_URL'))"
```

## 注意事项

1. **优先级**: `.env.local` > `.env` > 系统环境变量
2. **安全性**: 不要将包含敏感信息的 `.env.local` 文件提交到版本控制
3. **路径**: 确保在项目根目录下运行命令
4. **依赖**: 某些方法需要安装 `python-dotenv` 包