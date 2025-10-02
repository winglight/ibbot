# Service Overview

The dashboard aggregates service health information from a central registry. The
table below lists each registered service along with the exposed port (where
applicable), the Redis health key that is monitored, and the current
implementation status.

| Service (slug) | Internal | Port(s) | Health key / override | Implementation status | Description |
| --- | --- | --- | --- | --- | --- |
| Trading Core (`ibbot`) | ✅ | 8000 | `hash:service:health:ibbot` | Production | 主应用 (ibbot) |
| Command Dispatcher (`ib_command`) | ✅ | — | `IB_COMMAND_HEALTH_KEY` (default `hash:service:health:ib-command`) | Production | 指令下发服务 (ib_command) |
| Connection Control (`connection_control`) | ✅ | 8056 | `hash:service:health:connection_control` | Production | IB 连接控制服务 (connection_control) |
| Account Service (`account`) | ✅ | 8020 | `ACCOUNT_SERVICE_HEALTH_KEY` (default `hash:service:health:account-service`) | Production | 账户服务 (account-service) |
| Order Gateway (`orders`) | ✅ | — | `ORDERS_SERVICE_HEALTH_KEY` (default `hash:service:health:orders`) | Production | 订单服务 (orders) |
| Market Data Feed (`ib_feed`) | ✅ | — | `hash:service:health:ib-feed` | In development | 行情订阅服务 (ib_feed) |
| Market Data Sync (`market_data`) | ✅ | 8010 | `hash:service:health:market-data` | In development | 行情落地服务 (market_data) |
| Risk Manager (`risk`) | ✅ | — | `RISK_SERVICE_HEALTH_KEY` (default `hash:service:health:risk-service`) | Production | 风险控制服务 (risk-service) |
| Strategy Scheduler (`strategy`) | ✅ | — | `STRATEGY_SERVICE_HEALTH_KEY` (default `hash:service:health:strategy`) | Production | 策略调度服务 (strategy) |
| Notification Relay (`notification`) | ✅ | — | `NOTIFICATION_SERVICE_HEALTH_KEY` (default `hash:service:health:notification`) | Production | 通知推送服务 (notification) |
| Queue Monitor (`queue_monitor`) | ✅ | 8011 | `hash:service:health:queue` | Production | 队列监控服务 (queue_monitor) |
| Optimizer Engine (`optimizer`) | ✅ | 8001 | `hash:service:health:optimizer` | Beta | 优化器 (optimizer) |
| IB Gateway (`ib-gateway`) | ❌ | 4001/4002/5900 | — | External dependency | IB Gateway 由外部部署提供，需手动确认运行状态。 |
| Cloudflare Tunnel (`cloudflared`) | ❌ | — | — | External dependency | Cloudflared 隧道为外部依赖，请使用独立监控确认状态。 |

Notes:

- Services marked as external are not restarted automatically and always report
  an `external` status in the UI.
- When an override environment variable is listed it takes precedence over the
  default Redis health key.
