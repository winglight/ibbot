# 自动化测试说明

此目录包含针对 IBBot 当前核心功能的单元与集成测试。新的测试集聚焦以下能力：

| 文件 | 覆盖范围 |
| --- | --- |
| `test_auth.py` | `/api/login` 登录成功、访客只读角色校验 |
| `test_connection.py` | 连接配置、连接状态 API 的基本返回结构 |
| `test_queue.py` | 队列状态与任务查询接口 |
| `test_notifications.py` | 通知配置读取、敏感信息脱敏 |
| `test_dom_config.py` | DOM 配置读取接口与窗口参数更新 |
| `test_api_docs.py` | OpenAPI 元数据可用性与 `docs/api_doc.json` 与实际 schema 对齐 |

## 运行方式

```bash
pytest
```

测试使用 `fastapi.testclient.TestClient` 启动应用，并自动：

- 固定管理员/访客账号，避免依赖本地 `.env`。
- 使用内存桩件替代远程服务，不修改生产数据。
- 跳过真实 IBKR 和 Redis 连接，保持测试快速稳定。

## 常见问题

| 现象 | 原因/解决方式 |
| --- | --- |
| Redis 相关警告 | 测试环境默认未安装 `redis-py`，警告可忽略。 |
| `fastapi` 版本差异导致 schema 变化 | 若更新依赖导致 OpenAPI 有变化，请重新执行 `python tools/export_api_doc.py` 或直接运行 `pytest` 让快照测试失败后更新 `docs/api_doc.json`。 |

更多手工回归测试步骤请参阅 `docs/tests.md`。
