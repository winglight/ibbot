# IBKR Trading Bot

一个基于Interactive Brokers API的全功能交易机器人，支持实时数据获取、自动交易、风险管理和Web界面。

## 功能特性

### 核心交易功能
- ✅ 连接IBKR交易服务器（支持模拟和真实账号）
- ✅ 获取指定股票历史数据并保存到CSV文件
- ✅ 实时获取股票数据
- ✅ 下单功能（支持限价单和市价单）
- ✅ 取消订单（支持取消指定订单和取消所有订单）
- ✅ 获取订单列表（包括已下单、已取消、已填充等状态）
- ✅ 查询账户基础信息（账户ID、可用资金、P&L等）
- ✅ 获取持仓列表（包括盈亏计算）
- ✅ 查询交易记录

### 风险管理功能
- ✅ 基于亏损阈值的自动监控和通知
- ✅ 自动止损平仓功能
- ✅ 止盈止损价格设置
- ✅ 单线程FIFO订单队列处理
- ✅ 资金充足性检查

### 通知功能
- ✅ Telegram消息通知
- ✅ 亏损警报
- ✅ 自动平仓通知
- ✅ 订单状态通知
- ✅ 每日账户摘要

### Web界面
- ✅ 现代化响应式Web UI
- ✅ 实时数据展示
- ✅ 账户设置和登录
- ✅ 交易操作界面
- ✅ 风险管理设置
- ✅ WebSocket实时更新

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

#### IB Gateway设置
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

## 项目结构

```
ibbot/
├── main.py                 # 主程序入口
├── ibkr_client.py          # IBKR API客户端
├── telegram_notifier.py    # Telegram通知模块
├── risk_manager.py         # 风险管理模块
├── web_api.py             # Web API服务器
├── requirements.txt        # Python依赖
├── .env.example           # 环境变量示例
├── README.md              # 项目文档
├── static/                # 静态文件
│   ├── index.html         # 主页面
│   └── app.js            # 前端JavaScript
├── logs/                  # 日志文件
└── data/                  # 数据文件
```

## API接口

### 连接管理
- `POST /api/connect` - 连接IBKR
- `POST /api/disconnect` - 断开连接
- `GET /api/connection_status` - 获取连接状态

### 市场数据
- `POST /api/historical_data` - 获取历史数据
- `GET /api/realtime_data/{symbol}` - 获取实时数据

### 交易操作
- `POST /api/place_order` - 下单
- `DELETE /api/cancel_order/{order_id}` - 取消订单
- `DELETE /api/cancel_all_orders` - 取消所有订单
- `GET /api/orders` - 获取订单列表

### 账户信息
- `GET /api/account_info` - 获取账户信息
- `GET /api/positions` - 获取持仓列表


### 风险管理
- `POST /api/risk_rules` - 添加风险规则
- `GET /api/risk_rules` - 获取风险规则
- `DELETE /api/risk_rules/{symbol}` - 删除风险规则

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