# Service Overview

The dashboard aggregates service health information from a central registry. The
table below lists each registered service along with the exposed port (where
applicable), the Redis health key that is monitored, and the current
implementation status.

| Service (slug) | Internal | Port(s) | Health key / override | Implementation status | Description |
| --- | --- | --- | --- | --- | --- |
| Trading Core (`ibbot`) | ✅ | 8000 | `hash:service:health:ibbot` | Production | 主应用 (ibbot) |
| Command Dispatcher (`ib_command`) | ❌ | — | `IB_COMMAND_HEALTH_KEY` (default `hash:service:health:ib-command`) | Retired | Legacy 指令下发服务 (已下线) |
| Connection Control (`connection_control`) | ✅ | 8056 | `hash:service:health:connection_control` | Production | IB 连接控制服务 (connection_control) |
| Account Service (`account`) | ✅ | 8020 | `ACCOUNT_SERVICE_HEALTH_KEY` (default `hash:service:health:account-service`) | Production | 账户服务 (account-service) |
| Order Gateway (`orders`) | ✅ | 8050 | `ORDERS_SERVICE_HEALTH_KEY` (default `hash:service:health:orders`) | Production | 订单服务 (orders) |
| Market Data Sync (`market_data`) | ✅ | 8010 | `hash:service:health:market-data` | Production | 行情落地服务 (market_data) |
| Risk Manager (`risk`) | ✅ | 8030 | `RISK_SERVICE_HEALTH_KEY` (default `hash:service:health:risk-service`) | Production | 风险控制服务 (risk-service) |
| Strategy Scheduler (`strategy`) | ✅ | 8045 | `STRATEGY_SERVICE_HEALTH_KEY` (default `hash:service:health:strategy`) | Production | 策略调度服务 (strategy) |
| Market Data Feed (`market_data_feed`) | ❌ | — | `hash:service:health:ib-feed` | Retired | Legacy 行情订阅服务 (已下线) |
Notes:

- Services marked as external are not restarted automatically and always report
  an `external` status in the UI.
- When an override environment variable is listed it takes precedence over the
  default Redis health key.
