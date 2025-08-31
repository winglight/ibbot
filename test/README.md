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

### test_connection.py
**连接管理测试**
- 覆盖 /api/connection_status, /api/connect, /api/disconnect
- 使用不可达端口模拟连接失败路径，并验证 account_type 切换

使用方法：
```bash
cd test
python -m pytest -q test_connection.py
```

### test_external_strategy.py
**外部策略API测试**
- 覆盖策略设置、令牌生成、外部订单请求、执行记录、性能统计等

使用方法：
```bash
cd test
python -m pytest -q test_external_strategy.py
```

### test_strategy.py
**内部策略管理与执行测试**
- 覆盖策略CRUD、设置、启停、/api/strategy执行、执行记录、性能统计

使用方法：
```bash
cd test
python -m pytest -q test_strategy.py
```

## 运行模式说明（Mock 与 IB Gateway）

本项目支持两种运行模式：
- Mock服务模式：用于本地快速联调和CI环境。
  - Mock服务默认运行在 http://localhost:18080
  - 仅Mock服务提供的维护接口（非主应用转发）：
    - POST http://localhost:18080/api/mock/set_price 设置某标的最新价
    - POST http://localhost:18080/api/mock/reset 重置内部状态
- 直连IB Gateway模式：连接真实的IBKR TWS/Gateway（建议Paper账户）

切换方式：
- 通过环境变量控制主应用行为：
  - AUTO_CONNECT=false 以避免在Mock模式下尝试连接真实IBKR
  - 当检测到Mock模式时，主应用不会在启动时进行自动连接

## 环境要求

1. 后台接口测试：
   - 主应用程序必须运行在`http://localhost:8000`（可通过`API_BASE_URL`环境变量修改）
   - `.env`文件中必须配置正确的`ADMIN_USERNAME`和`ADMIN_PASSWORD`

2. 独立功能测试：
   - IB Gateway必须运行并监听端口4002（模拟交易）
   - 确保Gateway中启用了"Enable ActiveX and Socket EClients"

## 注意事项

- 所有测试默认在模拟交易环境下进行
- 如果使用Mock模式，可借助上述Mock维护接口快速构造行情与初始状态
- 若测试失败，请检查：主应用是否已启动、环境变量配置、Mock服务或Gateway连接状态

## 手工测试用例
- 详见 docs/tests.md，涵盖下单、风险规则与策略在真实IB数据下的手工验证步骤与期望结果。