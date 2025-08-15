# IBKR Trading Bot

一个基于Interactive Brokers API的全功能交易机器人，支持实时数据获取、自动交易、风险管理、策略执行和Web界面管理。

## 🚀 核心特性

### 📈 交易功能
- ✅ **多市场支持**: 股票(STK)、期货(FUT)等多种证券类型
- ✅ **智能下单**: 市价单、限价单，支持SMART路由
- ✅ **订单管理**: 实时订单状态跟踪、批量取消、订单历史
- ✅ **实时数据**: 实时价格、历史数据、图表数据
- ✅ **账户管理**: 资金状况、持仓信息、P&L计算

### 🛡️ 风险管理
- ✅ **智能止损止盈**: 基于价格和百分比的自动风险控制
- ✅ **多级风险阈值**: 3%警告、8%自动平仓等可配置阈值
- ✅ **实时监控**: 持仓风险实时监控和自动处理
- ✅ **订单队列**: FIFO队列确保订单执行顺序
- ✅ **资金检查**: 下单前资金充足性验证

### ⚡ 异步队列系统
- ✅ **FIFO队列管理**: 先进先出的任务队列确保处理顺序
- ✅ **异步任务处理**: 非阻塞的任务执行机制
- ✅ **重试机制**: 失败任务自动重试，最多3次
- ✅ **状态跟踪**: 实时监控任务状态（待处理、处理中、已完成、失败）
- ✅ **错误处理**: 详细的错误信息记录和处理
- ✅ **队列监控**: 队列状态统计和任务详情查询

### 🤖 策略系统
- ✅ **外部策略**: 支持HTTP API调用外部策略服务
- ✅ **内部策略**: Python脚本策略执行
- ✅ **策略管理**: 策略启用/禁用、性能跟踪
- ✅ **执行记录**: 详细的策略执行历史和P&L分析
- ✅ **认证机制**: 策略API访问令牌管理
- ✅ **异步队列**: FIFO队列处理策略订单，支持重试和错误处理

### 🌐 Web界面
- ✅ **现代化UI**: 基于Bootstrap的响应式设计
- ✅ **实时更新**: WebSocket实时数据推送
- ✅ **用户认证**: JWT令牌认证系统
- ✅ **多功能面板**: 交易、风险、策略、日志管理
- ✅ **图表展示**: 实时价格图表和历史数据可视化

### 📱 通知系统
- ✅ **Telegram集成**: 实时交易通知和风险警报
- ✅ **多级通知**: 信息、警告、错误分级通知
- ✅ **每日摘要**: 自动生成账户日报
- ✅ **事件通知**: 订单执行、风险触发等关键事件

### 💾 数据管理
- ✅ **SQLite数据库**: 统一的数据存储和管理
- ✅ **数据迁移**: 从CSV/JSON文件自动迁移到数据库
- ✅ **备份机制**: 订单和配置数据自动备份
- ✅ **历史数据**: 完整的交易和策略执行历史

## 安装要求

### 系统要求
- Python 3.8+
- Interactive Brokers TWS或IB Gateway
- 网络连接

### Python依赖
```bash
pip install -r requirements.txt
```

主要依赖包：
- `ib_insync`: IBKR API客户端
- `fastapi`: Web API框架
- `uvicorn`: ASGI服务器
- `pandas`: 数据处理
- `python-telegram-bot`: Telegram通知
- `apscheduler`: 定时任务
- `loguru`: 日志记录

## 配置设置

### 1. 环境变量配置

复制示例配置文件：
```bash
cp .env.example .env
```

编辑 `.env` 文件：
```bash
# IBKR配置
IBKR_HOST=127.0.0.1
IBKR_PORT=7497  # TWS: 7497, IB Gateway: 4001
IBKR_CLIENT_ID=1
IBKR_PAPER_TRADING=True  # True=模拟账号, False=真实账号

# Telegram配置（可选）
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# 风险管理
LOSS_THRESHOLD_1=0.05  # 5%亏损阈值（通知）
LOSS_THRESHOLD_2=0.10  # 10%亏损阈值（自动平仓）
CHECK_INTERVAL=5  # 检查间隔（分钟）

# Web服务器
WEB_HOST=0.0.0.0
WEB_PORT=8000
```

### 2. IBKR TWS/Gateway设置

#### TWS设置
1. 启动TWS
2. 登录您的账户
3. 进入 `File` → `Global Configuration` → `API` → `Settings`
4. 勾选 `Enable ActiveX and Socket Clients`
5. 设置 `Socket port` 为 `7497`（模拟）或 `7496`（真实）
6. 添加可信IP地址：`127.0.0.1`

#### 设置
1. 启动IB Gateway
2. 登录您的账户
3. 设置API端口为 `4001`（模拟）或 `4000`（真实）
4. 启用API连接

### 3. Telegram通知设置（可选）

1. 创建Telegram Bot：
   - 在Telegram中搜索 `@BotFather`
   - 发送 `/newbot` 创建新机器人
   - 获取Bot Token

2. 获取Chat ID：
   - 向您的Bot发送消息
   - 访问 `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   - 从响应中获取 `chat.id`

## 使用方法

### 1. 启动应用

```bash
python main.py
```

### 2. 访问Web界面

打开浏览器访问：`http://localhost:8000`

### 3. 连接IBKR

1. 在Web界面的连接设置中配置参数
2. 点击"Connect"按钮
3. 确认连接状态显示为"Connected"

### 4. 基本操作

#### 获取市场数据
- 输入股票代码（如：AAPL）
- 点击"Get Real-time Data"获取实时数据
- 点击"Get Historical Data"下载历史数据

#### 下单交易
- 选择股票代码、数量、买卖方向
- 选择订单类型（市价单/限价单）
- 点击"Place Order"提交订单

#### 风险管理
- 设置止损价格和止盈价格
- 系统将自动监控并执行风险管理规则

## 📁 项目结构

```
ibbot/
├── main.py                    # 🚀 主程序入口和FastAPI服务器
├── requirements.txt           # 📦 Python依赖包列表
├── .env.example              # ⚙️ 环境变量配置示例
├── .env.gateway              # 🔧 IB Gateway配置
├── install_ib_gateway.sh     # 📥 IB Gateway安装脚本
├── start.sh                  # 🏃 启动脚本
├── README.md                 # 📖 项目文档
│
├── src/                      # 📂 核心模块
│   ├── __init__.py
│   ├── ibkr_client.py        # 🔌 IBKR API客户端
│   ├── connection_manager.py # 🔗 连接管理器
│   ├── risk_manager.py       # 🛡️ 风险管理模块
│   ├── strategy_manager.py   # 🤖 策略管理模块
│   ├── queue_manager.py      # ⚡ 异步队列管理器
│   ├── telegram_notifier.py  # 📱 Telegram通知模块
│   ├── database.py           # 💾 SQLite数据库管理
│   └── test.py              # 🧪 测试模块
│
├── static/                   # 🌐 Web前端文件
│   ├── index.html           # 🏠 主页面
│   ├── app.js               # ⚡ 前端JavaScript逻辑
│   └── style.css            # 🎨 样式文件
│
├── tunnel/                   # 🌉 网络隧道配置
│   └── config.yaml
│
├── data/                     # 💾 数据存储目录
│   ├── trading_bot.db       # 🗄️ SQLite数据库
│   ├── orders_backup.csv    # 📋 订单备份
│   └── *.csv                # 📊 历史数据文件
│
├── strategy/                 # 🤖 策略脚本目录
│   └── *.py                 # 📝 Python策略文件
│
└── logs/                     # 📝 日志文件目录
    └── *.log                # 📄 应用日志
```

### 🏗️ 架构设计

- **main.py**: FastAPI应用主入口，包含所有API端点和WebSocket连接
- **src/**: 核心业务逻辑模块，采用模块化设计
- **static/**: 前端单页应用，使用Bootstrap + JavaScript
- **data/**: SQLite数据库和CSV备份文件存储
- **strategy/**: 用户自定义策略脚本存放目录

## 🔌 API接口文档

### 🔐 认证
- `POST /api/login` - 用户登录获取JWT令牌

### 🔗 连接管理
- `POST /api/connect` - 连接IBKR服务器
- `POST /api/disconnect` - 断开IBKR连接
- `GET /api/connection_status` - 获取连接状态

### 📊 市场数据
- `POST /api/historical_data` - 获取历史K线数据
- `POST /api/chart_data` - 获取图表数据
- `GET /api/realtime_data/{symbol}` - 获取实时价格数据

### 💼 交易操作
- `POST /api/place_order` - 提交交易订单
- `POST /api/cancel_order` - 取消指定订单
- `DELETE /api/cancel_order/{order_id}` - 取消订单(RESTful)
- `DELETE /api/cancel_all_orders` - 取消所有订单
- `GET /api/orders` - 获取订单列表
- `GET /api/order_queue_status` - 获取订单队列状态

### 👤 账户信息
- `GET /api/account_info` - 获取账户基本信息
- `GET /api/positions` - 获取持仓列表

### 🛡️ 风险管理
- `POST /api/risk_rules` - 添加风险规则
- `GET /api/risk_rules` - 获取风险规则列表
- `PUT /api/risk_rules/{symbol}` - 更新风险规则
- `DELETE /api/risk_rules/{symbol}` - 删除风险规则
- `GET /api/risk-config` - 获取风险配置
- `GET /api/auto_orders` - 获取自动订单列表

### 🤖 策略管理
- `GET /api/strategy/settings` - 获取策略设置
- `POST /api/strategy/settings` - 更新策略设置
- `POST /api/strategy/settings/generate-token` - 生成策略认证令牌
- `GET /api/strategies` - 获取策略列表
- `POST /api/strategies` - 添加新策略
- `GET /api/strategies/{name}` - 获取指定策略
- `PUT /api/strategies/{name}` - 更新策略
- `DELETE /api/strategies/{name}` - 删除策略
- `POST /api/strategies/{name}/toggle` - 启用/禁用策略
- `GET /api/strategies/{name}/performance` - 获取策略性能
- `POST /api/strategy` - 外部策略订单接口（异步队列处理）
- `GET /api/strategies/{name}/executions` - 获取策略执行记录
- `DELETE /api/strategies/{name}/executions` - 清除策略执行记录

### ⚡ 队列管理
- `GET /api/queue/status` - 获取队列状态统计
- `GET /api/queue/tasks` - 获取所有任务列表
- `GET /api/queue/task/{task_id}` - 获取指定任务详情

### 📝 系统管理
- `GET /api/logs` - 获取系统日志
- `WebSocket /ws` - 实时数据推送

> 📋 完整的API文档请参考 `api_doc.json` 文件

## 安全注意事项

1. **账户安全**
   - 建议先在模拟账户中测试
   - 设置合理的风险阈值
   - 定期检查交易记录

2. **API安全**
   - 限制API访问IP地址
   - 使用强密码保护账户
   - 定期更新软件版本

3. **风险管理**
   - 设置止损限制
   - 监控账户余额
   - 避免过度杠杆

## 故障排除

### 常见问题

1. **连接失败**
   - 检查TWS/Gateway是否运行
   - 确认API设置已启用
   - 检查端口号是否正确
   - 验证IP地址白名单

2. **订单失败**
   - 检查账户余额
   - 确认股票代码正确
   - 验证交易时间
   - 检查订单参数

3. **数据获取失败**
   - 确认市场开放时间
   - 检查股票代码格式
   - 验证数据权限

### 日志查看

日志文件位置：`logs/trading_bot.log`

```bash
# 查看实时日志
tail -f logs/trading_bot.log

# 查看错误日志
grep ERROR logs/trading_bot.log
```

## 开发说明

### 扩展功能

1. **添加新的交易策略**
   - 在 `risk_manager.py` 中添加新的规则类型
   - 实现相应的监控逻辑

2. **集成其他通知方式**
   - 参考 `telegram_notifier.py`
   - 实现新的通知类

3. **添加新的数据源**
   - 扩展 `ibkr_client.py`
   - 添加新的API接口

### 测试

```bash
# 运行连接测试
python -c "import asyncio; from main import test_connection; asyncio.run(test_connection())"
```

## 许可证

本项目仅供学习和研究使用。使用本软件进行实际交易的风险由用户自行承担。

## 免责声明

- 本软件不构成投资建议
- 交易有风险，投资需谨慎
- 作者不对任何交易损失负责
- 请在充分了解风险的情况下使用

## 支持

如有问题或建议，请提交Issue或Pull Request。