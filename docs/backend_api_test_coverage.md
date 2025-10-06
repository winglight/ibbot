# Backend API Test Coverage

This report compares FastAPI routes under `src/routes/` with exercised endpoints in the `tests/` directory.  Actionable tasks that can be triggered from the Codex UI are defined in `.codex/tasks/backend_api_tests.json`.

## src/routes/account.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/account/daily-summary | No | — |
| GET | /api/account_info | Yes | tests/routes/account/test_account_routes.py<br>tests/routes/test_account_snapshots.py |
| GET | /api/positions | Yes | tests/routes/account/test_account_routes.py<br>tests/routes/test_account_snapshots.py |
| POST | /api/orders/backfill_commissions | Yes | tests/routes/account/test_account_routes.py |

## src/routes/api_docs.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/docs/services | No | — |

## src/routes/auth.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| POST | /api/login | Yes | tests/ui/test_startup.py |

## src/routes/config.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/config/modules | No | — |
| GET | /api/config/{module} | No | — |
| GET | /api/config/{module}/history | No | — |
| GET | /config/modules | No | — |
| GET | /config/{module} | No | — |
| GET | /config/{module}/history | No | — |
| POST | /api/config/{module}/rollback | No | — |
| POST | /config/{module}/rollback | No | — |
| PUT | /api/config/{module} | Yes | tests/routes/risk/test_risk_routes.py |
| PUT | /config/{module} | No | — |

## src/routes/connection.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/connection_config | Yes | tests/routes/test_connection.py |
| GET | /api/connection_status | Yes | tests/routes/test_connection.py<br>tests/ui/test_startup.py |
| POST | /api/connect | Yes | tests/routes/test_connection.py |
| POST | /api/disconnect | Yes | tests/routes/test_connection.py |

## src/routes/dom.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/alerts | No | — |
| GET | /api/config/dom | No | — |
| GET | /api/dom/data | No | — |
| GET | /api/dom/history/{symbol} | No | — |
| GET | /api/dom/stats/{symbol} | No | — |
| POST | /api/alerts/{alert_id}/ack | No | — |
| POST | /api/dom/start | No | — |
| POST | /api/dom/stop | No | — |
| PUT | /api/config/dom | No | — |
| WEBSOCKET | /ws/dom/{symbol} | Not evaluated (websocket) | — |

## src/routes/logs.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/logs | No | — |
| GET | /api/logs/modules | No | — |
| GET | /api/logs/stats | No | — |

## src/routes/market_data.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/realtime_data/{symbol} | Yes | tests/routes/market_data/test_market_routes.py<br>tests/ui/test_api_routes.py |
| GET | /market/storage | No | — |
| POST | /api/chart_data | Yes | tests/routes/market_data/test_market_routes.py |
| POST | /api/current_price | Yes | tests/routes/market_data/test_market_routes.py |
| POST | /api/historical_data | Yes | tests/routes/market_data/test_market_routes.py |
| POST | /chart_data | Yes | tests/services/test_market_data_api.py |
| POST | /market/backfill | No | — |
| WEBSOCKET | /ws/market | Not evaluated (websocket) | — |

## src/routes/monitoring.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/monitoring/influx/write-rate | Yes | tests/routes/test_monitoring_metrics.py |
| GET | /api/monitoring/metrics/write-rate | Yes | tests/routes/test_monitoring_metrics.py |
| GET | /api/monitoring/queues | Yes | tests/routes/test_monitoring_metrics.py |
| GET | /monitoring/influx/write-rate | Yes | tests/routes/test_monitoring_metrics.py |
| POST | /api/monitoring/queues/clear-cache | No | — |

## src/routes/notifications.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| DELETE | /api/notifications/history | Yes | tests/routes/test_notifications.py |
| GET | /api/notifications/history | Yes | tests/routes/test_notifications.py |
| GET | /api/notifications/settings | Yes | tests/routes/test_notifications.py |
| POST | /api/notifications/history/clear | Yes | tests/routes/test_notifications.py |
| POST | /api/notifications/test | No | — |
| POST | /api/notify | Yes | tests/routes/test_notifications.py |
| PUT | /api/notifications/settings | Yes | tests/routes/test_notifications.py |

## src/routes/orders.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| DELETE | /api/cancel_all_orders | Yes | tests/routes/test_orders.py |
| DELETE | /api/cancel_order/{order_id} | Yes | tests/routes/test_orders.py |
| GET | /api/orders | Yes | tests/routes/test_orders.py |
| GET | /api/orders/{order_id} | Yes | tests/routes/test_orders.py |
| GET | /api/orders/{order_id}/events | Yes | tests/routes/test_orders.py |
| POST | /api/cancel_order | Yes | tests/routes/test_orders.py |
| POST | /api/close_position | Yes | tests/routes/test_orders.py |
| POST | /api/place_order | Yes | tests/e2e/test_full_pipeline.py<br>tests/routes/test_orders.py<br>tests/ui/test_api_routes.py<br>tests/ui/test_security.py |
| POST | /api/reverse_position | Yes | tests/routes/test_orders.py |

## src/routes/queue.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/queue/status | Yes | tests/routes/test_queue_routes.py |
| GET | /api/queue/task/{queue_id} | Yes | tests/routes/test_queue_routes.py |
| GET | /api/queue/tasks | Yes | tests/routes/test_queue_routes.py |

## src/routes/risk.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| DELETE | /api/risk_rules/{symbol} | Yes | tests/routes/risk/test_risk_routes.py |
| GET | /api/config/rules | No | — |
| GET | /api/order_queue_status | No | — |
| GET | /api/risk-config | Yes | tests/routes/risk/test_risk_routes.py |
| GET | /api/risk_rules | Yes | tests/routes/risk/test_risk_routes.py<br>tests/ui/test_startup.py |
| GET | /api/sizing/history | No | — |
| POST | /api/position-sizing | Yes | tests/routes/risk/test_risk_routes.py |
| POST | /api/risk_rules | Yes | tests/routes/risk/test_risk_routes.py |
| PUT | /api/config/rules | Yes | tests/routes/risk/test_risk_routes.py |
| PUT | /api/risk-config | Yes | tests/routes/risk/test_risk_routes.py |
| PUT | /api/risk_rules/{symbol} | Yes | tests/routes/risk/test_risk_routes.py |

## src/routes/runbooks.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/runbooks | No | — |
| GET | /api/runbooks/{runbook_id} | No | — |
| POST | /api/runbooks/{runbook_id}/start | No | — |

## src/routes/service.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| GET | /api/services/status | Yes | tests/routes/test_service_status_route.py |
| POST | /api/services/restart | Yes | tests/ui/test_service_restart.py |
| POST | /api/services/restart/local | Yes | tests/ui/test_service_restart.py |

## src/routes/strategy.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| DELETE | /api/strategies/{name} | No | — |
| DELETE | /api/strategies/{name}/executions | No | — |
| GET | /api/strategies | Yes | tests/routes/strategy/test_strategy_routes.py |
| GET | /api/strategies/{name} | No | — |
| GET | /api/strategies/{name}/executions | No | — |
| GET | /api/strategies/{name}/internal-info | No | — |
| GET | /api/strategies/{name}/performance | No | — |
| GET | /api/strategies/{name}/pnl/daily | No | — |
| GET | /api/strategies/{name}/risk-settings | No | — |
| GET | /api/strategies/{name}/status | No | — |
| GET | /api/strategy/files | No | — |
| GET | /api/strategy/settings | Yes | tests/routes/strategy/test_strategy_routes.py |
| POST | /api/strategies | No | — |
| POST | /api/strategies/{name}/data-push | No | — |
| POST | /api/strategies/{name}/internal-info/parameter | No | — |
| POST | /api/strategies/{name}/rename | No | — |
| POST | /api/strategies/{name}/reset | No | — |
| POST | /api/strategies/{name}/toggle | No | — |
| POST | /api/strategy | Yes | tests/routes/strategy/test_strategy_routes.py<br>tests/ui/test_api_routes.py |
| POST | /api/strategy/settings | Yes | tests/routes/strategy/test_strategy_routes.py |
| POST | /api/strategy/settings/generate-token | No | — |
| PUT | /api/strategies/{name} | No | — |
| PUT | /api/strategies/{name}/risk-settings | No | — |

## src/routes/websocket.py

| Method | Path | Covered? | Test files |
| --- | --- | --- | --- |
| WEBSOCKET | /ws | Not evaluated (websocket) | — |
