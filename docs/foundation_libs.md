# 基础依赖与诊断脚本

本文档汇总 Redis、InfluxDB 与日志子系统的配置方式、核心接口以及诊断脚本的使用方法，便于在部署或排障时快速定位问题。

## 配置约定

所有基础依赖均通过 `src.ibbot.config.ConfigProvider` 加载，优先级为：

1. `.env.local` 中的私有覆写（若存在）以及当前进程的环境变量；
2. `.env` 中的共享默认值（通常由 `.env.example` 模板复制而来）；
3. 当 `.env` 缺失时，自动回退到 `.env.example` 模板提供的默认值；
4. （可选）Redis `hash:config:<module>` 中的热更新配置，模块名默认为 `foundation`。

下表列出了与三项依赖相关的关键键值，均可写入 `.env` 或 Redis 配置散列：

| 依赖 | 主要键 | 说明 |
| --- | --- | --- |
| Redis | `REDIS_URL` | 完整连接串（优先）。例如 `redis://localhost:6379/0`。 |
|      | `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` | 当未提供 `REDIS_URL` 时使用主机配置。 |
|      | `REDIS_PASSWORD` / `REDIS_SSL` | 可选认证与 SSL 标记（`true`/`false`）。 |
| InfluxDB | `INFLUX_URL` / `IBBOT_INFLUX_URL` / `DATA_LAYER_INFLUX_URL` | 客户端访问地址。脚本会按此顺序回退。 |
|      | `INFLUX_TOKEN` / `IBBOT_INFLUX_TOKEN` / `DATA_LAYER_INFLUX_TOKEN` | 访问令牌，可为空以使用匿名模式。 |
|      | `INFLUX_ORG` / `INFLUX_BUCKET` | 组织、桶信息；亦支持 `DATA_LAYER_*` 变体。 |
|      | `INFLUX_TIMEOUT_MS` / `DATA_LAYER_INFLUX_TIMEOUT_MS` | 请求超时（毫秒）。 |
| Logging | `FOUNDATION_LOGGER_NAME` | 诊断脚本写入日志时使用的 logger 名称，默认 `foundation.diagnostics`。 |
|        | `FOUNDATION_LOG_DIR` / `LOG_DIR` | 日志输出目录，默认 `data/logs`。 |

## 关键接口

- **Redis**：`src/ibbot/redis/__init__.py` 暴露 `get_redis_client`、`redis_set`、`read_stream` 等协程方法，所有连接参数均源自上述配置。
- **InfluxDB**：`src/ibbot/influx/__init__.py` 定义了 `InfluxConfig`、`write_points` 及多种 `build_*_point` 工具函数，可通过 `configure_influx` 或环境变量初始化。
- **Logging**：`src/ibbot/logging/__init__.py` 的 `get_logger`、`get_log_file` 与 `read_log_segment` 用于统一日志格式与读取。

## 诊断脚本

`scripts/diagnose_foundation.py` 依次检查 Redis、InfluxDB 与日志写入能力，可在部署后快速验证外部依赖。脚本会使用 `ConfigProvider` 读取上表配置并输出每一步的成功与失败原因。

### 使用示例

```bash
python scripts/diagnose_foundation.py --dry-run
python scripts/diagnose_foundation.py            # 实际连接并写入日志
python scripts/diagnose_foundation.py --module custom-foundation
```

- `--dry-run`：跳过真实连接，仅展示解析到的目标地址和日志文件位置。
- `--module`：指定 `hash:config:<module>` 名称，便于针对不同服务配置进行诊断。

脚本输出示例：

```
=== Foundation Diagnostics ===
Configuration module: foundation
Mode: dry-run (no external calls were executed)

[OK] Redis: Dry-run: skipped connection test.
    - target: redis://localhost:6379/0?ssl=0

[OK] InfluxDB: Dry-run: skipped health check.
    - url: http://localhost:8086
    - org: <not configured>
    - bucket: <not configured>

[OK] Logging: Dry-run: skipped log write.
    - log_file: /path/to/project/data/logs/foundation.diagnostics.log
```

若任一步骤失败，脚本会打印具体的异常信息，便于确认网络、凭证或权限问题。
