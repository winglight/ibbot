# 测试文件说明

本目录包含IBKR Bot的测试文件，用于验证各种功能的正确性。

## 文件说明

### test_api_order.py
**后台接口测试**
- 测试通过Web API进行下单和取消订单的功能
- 自动从`.env`文件读取登录用户名和密码进行认证
- 需要主应用程序运行在后台

使用方法：
```bash
cd test
python test_api_order.py
```

### test_order_fix.py
**独立功能测试**
- 使用独立的IB Gateway连接进行下单测试
- 不依赖主应用程序，直接连接到IB Gateway
- 使用与`test.py`相同的连接方法

使用方法：
```bash
cd test
python test_order_fix.py
```

### test.py
**基础连接测试**
- 提供基础的IB Gateway连接方法
- 测试市场数据连接和实时行情
- 其他测试文件可以参考此文件的连接方式

使用方法：
```bash
cd test
python test.py
```

## 环境要求

1. **后台接口测试**：
   - 主应用程序必须运行在`http://localhost:8000`
   - `.env`文件中必须配置正确的`ADMIN_USERNAME`和`ADMIN_PASSWORD`

2. **独立功能测试**：
   - IB Gateway必须运行并监听端口4002（模拟交易）
   - 确保Gateway中启用了"Enable ActiveX and Socket EClients"

## 注意事项

- 所有测试都使用模拟交易环境，不会影响真实资金
- 测试订单使用远低于市价的价格，确保不会意外成交
- 如果测试失败，请检查IB Gateway连接状态和端口配置