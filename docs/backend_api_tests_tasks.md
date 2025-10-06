# 后台接口测试补齐任务清单

## 背景
根据 `docs/backend_api_test_coverage.md` 的比对结果，部分后台接口尚未被自动化测试覆盖，另有个别过期测试需要清理。以下任务拆解旨在指导后续补测与整改工作，并可在代码评审/项目管理工具中逐条勾选跟踪。

## 基础准备
- [ ] 复核覆盖报告中标记为 "No" 的接口，整理各模块（account、api_docs、config、dom、logs、market_data、monitoring、notifications、runbooks、strategy）的待测列表。
- [ ] 盘点 `tests/routes` 与相关辅助模块，记录可复用的 `TestClient` fixture、mock 工具，以及需要废弃的旧接口用例。

## 模块化接口补测
### Account
- [ ] 为 `/api/account/daily-summary` 编写测试，覆盖正常返回、Redis 缓存缺失、非法 `days` 参数等分支，mock `load_all_daily_totals`、`read_net_liquidation_snapshot` 等依赖。

### API Docs
- [ ] 新增 `/api/docs/services` 的测试文件，验证组合后的服务列表输出与 `ServiceSpecTarget` 的映射逻辑。

### Config
- [ ] 准备具备 `hgetall`、`hset`、`lrange`、`publish` 等方法的 Fake Redis fixture。
- [ ] 覆盖配置模块枚举、读取、历史、更新、回滚以及 `/config/**` 别名路径。
- [ ] 模拟 Redis 不可用或权限受限时的 503/403 响应分支。

### DOM
- [ ] mock `dom_service`，覆盖启动/停止、配置读取与更新、行情数据、告警列表以及 ACK 未找到时的错误处理。

### Logs
- [ ] 利用临时文件或 monkeypatch `aiofiles`/`os.path.exists`，覆盖 `/api/logs`、`/api/logs/modules`、`/api/logs/stats` 的正常与异常路径（分页、过滤、文件缺失等）。

### Market Data
- [ ] 通过 stub `MarketDataServiceClient`，补测 `run_market_backfill` 与 `proxy_market_storage` 成功、参数校验失败（400）、资源缺失（404）与上游错误（502/503）等场景。

### Monitoring
- [ ] 为 `/api/monitoring/queues/clear-cache` 添加测试，覆盖缓存清理成功、Redis 获取失败及 flush 异常时的错误码。

### Notifications
- [ ] 扩充 `/api/notifications/test` 测试，断言 channel 映射（all/chrome/default）与 `_dispatch_notification` 调用参数。

### Runbooks
- [ ] 通过 monkeypatch `runbook_manager`，覆盖列表、详情、启动成功及 404/409 等异常分支。

### Strategy
- [ ] 在 `test_strategy_routes.py` 中补充剩余 `GET/POST/PUT/DELETE` 端点测试，覆盖 `ServiceResponseError`、`ServiceClientError` 等错误处理，并校验请求负载与布尔参数转换。

## 现有测试清理
- [ ] 移除或修复指向废弃接口、结构已变的断言，确保测试套件与最新 API 定义一致。
- [ ] 抽取公共 fixture/helper，减少重复逻辑。

## 文档与验收
- [ ] 更新覆盖报告，将新补测的端点标记为已覆盖，并记录对应测试文件。
- [ ] 运行受影响目录的针对性 `pytest` 与全量回归，确保新增测试稳定通过。
- [ ] 若 CI 配置需调整（依赖、环境变量等），同步更新相关脚本或说明。
