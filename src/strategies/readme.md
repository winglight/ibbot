# 策略开发与运行指南（strategy_runner.py / strategy_template.py）

本目录提供一套最小策略开发与运行闭环：
- strategy_template.py：定义策略基类 BaseStrategy 和示例策略 ExampleBuyDownSellOpenStrategy；
- strategy_runner.py：通用 Runner，定时获取主程序行情、调用策略生成信号，并将订单提交给主程序。

本文档涵盖本地与 Docker 环境的使用、测试方法以及与主程序的集成方式。

---

## 1. 文件说明

- strategy_template.py
  - BaseStrategy：策略基类，约定了 initialize、generate_signal、on_data、on_order_filled、on_error 等接口，并提供绩效统计与信号日志记录。
  - ExampleBuyDownSellOpenStrategy：示例策略（低买高卖），可直接用于联调和示例测试。

- strategy_runner.py（CLI）
  - 周期性流程：
    1) 从主程序获取实时行情 GET /api/realtime_data/{symbol}
    2) 调用策略的 generate_signal 生成信号
    3) 将信号转为下单请求 POST /api/strategy
  - 关键参数：
    - --file / RUNNER_STRATEGY_FILE：策略文件名（位于 ./strategy 目录）
    - --symbols / RUNNER_SYMBOLS：逗号分隔的标的，如 "AAPL,MSFT"
    - --api / RUNNER_API_BASE：主程序 API 基址（本地默认 http://localhost:8000）
    - --exchange / RUNNER_EXCHANGE：交易所，默认 SMART
    - --sec-type / RUNNER_SEC_TYPE：证券类型，默认 STK
    - --interval / RUNNER_INTERVAL：轮询间隔（秒），默认 2.0
    - --strategy-name / RUNNER_STRATEGY_NAME：策略名（默认取策略实例的 name）
    - --strategy-token / RUNNER_STRATEGY_TOKEN：可选，外部策略鉴权 Token（如主程序开启）
    - --jwt / RUNNER_JWT：可选，若主程序受 JWT 保护，传入 Bearer Token

---

## 2. 快速开始（本地）

建议优先运行主程序和（可选）Mock 服务：

- 启动主程序（默认端口 8000）：
  - 命令行示例：
    python -m uvicorn main:app --host 127.0.0.1 --port 8000
  - 交互文档：
    - 主服务 RapiDoc: http://localhost:8000/rapidoc

- 启动 Mock 服务（可选，默认端口 18080）：
  - 命令行示例：
    python -m uvicorn mock_ib_gateway:app --host 127.0.0.1 --port 18080
  - 交互文档：
    - Mock RapiDoc: http://localhost:18080/rapidoc

- 运行 Runner（两种方式）：
  1) 作为模块运行（推荐）：
     python -m strategy.strategy_runner --file "Buy down and Sell open.py" --symbols AAPL --api http://localhost:8000
  2) 直接运行脚本（本项目已做兼容修复）：
     python strategy/strategy_runner.py --file "Buy down and Sell open.py" --symbols AAPL --api http://localhost:8000

- 查看帮助：
  python -m strategy.strategy_runner --help

- 常见导入错误的解决：
  - 错误信息：attempted relative import with no known parent package
  - 解决方式：优先使用模块方式运行（python -m strategy.strategy_runner）；或直接运行当前版本的 strategy_runner.py（已内置兜底导入逻辑）。

---

## 3. 自定义策略开发

1) 复制示例模板，创建新文件（位于 ./strategy 目录）：
   - 例如：my_strategy.py

2) 继承 BaseStrategy 并实现必要方法：
   - initialize：初始化参数
   - generate_signal(symbol, data)：根据行情生成信号，返回如 {"action": "BUY", "quantity": 100, "type": "MARKET", "symbol": symbol}
   - on_order_filled：可根据成交回报更新统计

3) 在文件内导出：
   - 方案 A：定义名为 strategy 的实例（推荐）
   - 方案 B：定义继承 BaseStrategy 的类，Runner 会自动发现并实例化

4) 运行 Runner 指向你的策略文件：
   python -m strategy.strategy_runner --file my_strategy.py --symbols AAPL --api http://localhost:8000

---

## 4. 与主程序的集成点

Runner 依赖主程序的两个核心接口：
- GET /api/realtime_data/{symbol}
  - 用于拉取实时行情（支持 exchange、sec_type 等参数）
- POST /api/strategy
  - 用于提交由策略生成的下单请求

鉴权支持：
- 若主程序开启 JWT，需要在 Runner 启动时通过 --jwt 传入 Bearer Token
- 若主程序配置了策略侧 Token，可通过 --strategy-token 传入（也支持通过 X-Strategy-Token 头部）

---

## 5. 本地测试流程（含 Mock 网关）

准备：
- 启动主程序（http://localhost:8000）
- 启动 Mock（http://localhost:18080）

测试路径一：完全通过 Runner 驱动
1) 启动 Runner：
   python -m strategy.strategy_runner --file "Buy down and Sell open.py" --symbols AAPL --api http://localhost:8000 --interval 2
2) 通过主程序或 Mock 提供数据：
   - 在主程序 RapiDoc 中，使用 /api/mock/set_price 或 /api/mock/feed_data 推送行情
3) 观察 Runner 日志是否生成订单，并在主程序中查看队列/订单状态

测试路径二：手动触发策略（仅 Mock 路径）
- 在主程序 RapiDoc 中调用 /api/mock/trigger_strategy，可直接以模拟数据驱动一次策略执行

---

## 6. Docker 环境

本仓库提供 docker-compose.yml，包含两个服务：
- ibbot（主程序）：映射 8000:8000
- mock-gateway（Mock 网关）：映射 18080:18080

启动：
- docker compose up -d
- 主服务 RapiDoc: http://localhost:8000/rapidoc
- Mock RapiDoc: http://localhost:18080/rapidoc

在宿主机运行 Runner 连接容器：
- python -m strategy.strategy_runner --file "Buy down and Sell open.py" --symbols AAPL --api http://localhost:8000

在容器内运行 Runner：
- 进入主程序容器：
  docker exec -it ibbot-main bash
- 容器内运行：
  python -m strategy.strategy_runner --file "Buy down and Sell open.py" --symbols AAPL --api http://ibbot:8000
  或直接指向 localhost:8000（容器内主程序监听 0.0.0.0:8000，且同容器内访问可用）

策略文件与数据持久化：
- docker-compose 已将 ./strategy、./data、./logs 挂载到容器 /app/ 下，便于本地开发与持久化。

---

## 7. 示例命令速查

- 查看可用策略文件：
  ls ./strategy/*.py

- 运行示例策略：
  python -m strategy.strategy_runner --file "Buy down and Sell open.py" --symbols AAPL --api http://localhost:8000 --interval 2

- 使用环境变量方式：
  RUNNER_STRATEGY_FILE="Buy down and Sell open.py" \
  RUNNER_SYMBOLS="AAPL,MSFT" \
  RUNNER_API_BASE="http://localhost:8000" \
  RUNNER_INTERVAL=2.0 \
  python -m strategy.strategy_runner

---

## 8. 常见问题（FAQ）

- Q: 运行 python strategy/strategy_runner.py 报相对导入错误？
  - A: 本项目已在文件中增加兜底导入逻辑；更推荐使用模块方式：python -m strategy.strategy_runner。

- Q: Runner 一直报连接失败？
  - A: 请确认主程序已在 http://localhost:8000 启动；若在 Docker 中，确保容器正常、健康检查通过，并使用正确的 API 基址。

- Q: 如何验证数据链路？
  - A: 打开主服务 RapiDoc，使用 /api/mock/set_price 或 /api/mock/feed_data 推送行情，观察 Runner 日志与主程序订单/队列。

如需更丰富的示例或自动化测试流程，可参考仓库 test 目录以及主程序 RapiDoc 描述。