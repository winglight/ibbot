# Service Overview

The table below enumerates every service exposed through `/api/services/status`. It captures the
identifier used by the API/UI, the default network port (when applicable), the Redis health hash key,
and a short note on the current implementation status of the health reporting.

| Service ID | Display Name | Port | Health Key (default) | Implementation Status |
|------------|--------------|------|----------------------|-----------------------|
| `ib-gateway` | IB Gateway | 4002 | – | External IBKR container – monitor manually. |
| `ibbot` | Trading Core | 8000 | – | Main application process (reports uptime only). |
| `ib_command` | Command Dispatcher | – | `hash:service:health:ib-command` | Reports queue depth and connection status. |
| `account` | Account Service | 8020 | `hash:service:health:account-service` | Reports health and readiness probe results. |
| `orders` | Order Orchestrator | – | `hash:service:health:orders` | Publishes active order metrics. |
| `risk` | Risk Manager | – | `hash:service:health:risk-service` | Reports engine status and last error. |
| `strategy` | Strategy Scheduler | – | `hash:service:health:strategy` | Reports scheduler heartbeat. |
| `notification` | Notification Relay | – | `hash:service:health:notification` | Reports delivery health. |
| `account_snapshot` | Account Snapshot | – | `hash:service:health:account-snapshot` | Placeholder – telemetry pending. |
| `ib_feed` | Market Data Feed | – | `hash:service:health:ib-feed` | Health key reserved; reporting integration pending. |
| `market_data` | Market Data Sync | 8010 | `hash:service:health:market-data` | Aggregation service – health reporting in progress. |
| `queue_monitor` | Queue Monitor | 8011 | `hash:service:health:queue` | Publishes queue health and metrics snapshots. |
| `optimizer` | Optimizer Engine | 8001 | `hash:service:health:optimizer` | Health telemetry not yet implemented. |
| `cloudflared` | Cloudflare Tunnel | – | – | External tunnel process – monitor separately. |
| `dom-service`* | DOM 数据服务 | – | `DOM_SERVICE_HEALTH_KEY` (optional) | Optional external DOM publisher; defaults to offline placeholder when unset. |

*The DOM service identifier honours the `DOM_SERVICE_NAME` environment variable. When
`DOM_SERVICE_HEALTH_KEY` is unset the API exposes an offline placeholder entry to signal that the DOM
service is managed outside the stack.

The Redis health hash keys listed above can be overridden via the corresponding `*_SERVICE_HEALTH_KEY`
environment variables (e.g. `IB_FEED_SERVICE_HEALTH_KEY`).

## Implementation Notes

The registry is the single source of truth for the service list. When a service does not yet publish
health telemetry, the backend surfaces the entry with `health_key` set to `null` and the frontend renders
it with a "no data" badge. This allows new microservices to appear in the dashboard immediately while
highlighting integration gaps that still need attention.
