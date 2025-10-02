# Service Overview

The following table summarises the services surfaced in the operations dashboard. It reflects the canonical metadata exported by `/api/services/status` and the central registry in `src/routes/service_registry.py`.

| Service | Ports | Health Key | Implementation Status |
| --- | --- | --- | --- |
| ibbot | 8000 | – | Core application process; status reported from controller runtime. |
| ib_command | – | `hash:service:health:ib-command` | Redis health hash published by the IB command dispatcher. |
| ib_feed | – | – | No unified health check; assumed running while process is up. |
| risk-service | – | `hash:service:health:risk-service` | Redis health hash emitted by the risk service. |
| strategy | – | `hash:service:health:strategy` | Redis health hash emitted by the strategy scheduler. |
| account-service | 8020 | – | Readiness determined via periodic API probe (no Redis health key). |
| account-snapshot | – | `hash:service:health:account-snapshot` | Redis health hash maintained by the account snapshot worker. |
| queue_monitor | 8011 | `hash:service:health:queue` | Redis metrics (`hash:service:queue-monitor`) with fallback to the queue health hash. |
| market_data | 8010 | – | No unified health check; assumed running while process is up. |
| notification | – | – | No unified health check; assumed running while process is up. |
| optimizer | 8001 | – | No unified health check; assumed running while process is up. |
| ib-gateway | 4001, 4002, 5800, 5900 | – | External IBKR gateway; treated as running when reachable. |
| cloudflared | – | – | External tunnel process; treated as running when process is up. |
| dom-service | – | *(configurable via `DOM_SERVICE_HEALTH_KEY`)* | External DOM bridge; reported offline unless a health key is configured and reporting data. |

> **Note:** Health key environment variables (`*_SERVICE_HEALTH_KEY`, `DOM_SERVICE_HEALTH_KEY`) can override the defaults listed above. When a service does not provide health data, the dashboard supplies descriptive placeholder text while leaving restart controls available for internal services.
