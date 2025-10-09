# Refactor Implementation Plan

This plan describes the concrete code changes required to migrate the IB Bot stack away from the legacy `ib_command`/`ib_feed` bridge and toward dedicated Interactive Brokers (IB) TWS API integrations inside the Account, Orders, Market Data, and Strategy services. Follow the steps in sequence; every step contains the files to edit, new modules to introduce, and acceptance checks to perform before moving on.

> **Prerequisites**
>
> * Ensure IB Gateway / Trader Workstation is reachable, and record the host/port/client ID combinations reserved for each microservice.
> * Confirm Redis is running and reachable by every service (most services already expect `REDIS_URL`).
> * Capture the full list of existing REST routes and Redis pub/sub channels so parity checks can be run after the refactor.

## 1. Prepare service-scoped IB integrations

1. For each service (`services/account`, `services/orders`, `services/market_data`) introduce a local `ib_client.py` that owns its **own** `IBClient(EWrapper, EClient)` subclass:
   * The subclass should encapsulate connection lifecycle, request ID allocation, and asyncio-safe callback forwarding **within the service only**.
   * Read host/port/client ID from service-specific environment variables (e.g., `ACCOUNT_IB_HOST`, `ORDERS_IB_HOST`, `MARKET_DATA_IB_HOST`) so that every service maintains an independent TWS session.
   * Expose `async def connect()` / `disconnect()` coroutines plus helper awaitables that wait for `nextValidId` confirmation before marking the service ready.
2. Add a lightweight shared events module, `src/ib/events.py`, containing Pydantic models / TypedDicts for account updates, order updates, market data ticks, DOM levels, executions, and errors mirrored from `IBJts/samples/Python/Testbed/Program.py`. The models are connection-agnostic and may be reused by all services.
3. Create `src/ib/redis_streaming.py` with utilities that can be imported by any service:
   * Implement `class RedisPublisher` with helpers to serialize event payloads to JSON and publish on configured channels.
   * Provide helpers like `publish_account_event`, `publish_order_event`, `publish_market_event` that individual services call from their wrapper callbacks. Do **not** centralize TWS connections here—these helpers only deal with Redis serialization.
4. Add focused unit tests for the reusable helpers under `tests/ib/test_events_and_redis.py` using mocks to ensure payload validation and Redis publishing work without sharing IB state.

## 2. Remove `ib_command` and `ib_feed` services

1. Delete the `services/ib_command` and `services/ib_feed` packages entirely (FastAPI apps, Docker configs, and start scripts).
2. Remove their references from `docker-compose.yml`, Kubernetes manifests, `scripts/` runner utilities, and documentation.
3. Update `src/routes/service_clients.py` to drop the client wrappers for the removed services and eliminate any constants referencing their health checks.
4. Delete Redis command invocation helpers from `src/ibbot/redis.py` (`request_ib_command`, `IBCommandError`, etc.) and replace callers with direct service method invocations (to be introduced in later steps).
5. Adjust unit tests that expect ib_command endpoints (likely under `tests/services/test_ib_command_*`). Remove or rewrite them to align with the new architecture.

## 3. Refactor the Account service

1. Expand `services/account/ib_client.py` (from Step 1) with coroutines for:
   * Subscribing to account value / portfolio updates via `reqAccountUpdates`.
   * Fetching account summary via `reqAccountSummary`.
   * Requesting positions via `reqPositions`.
   * Pulling daily PnL via `reqPnL` if needed.
   * Requesting historical data for account-specific contracts when applicable.
   * Each coroutine should rely on the service-local connection instance; the module must never import an IB client from another service.
2. Modify `services/account/main.py`:
   * Replace Redis command requests with direct calls to the local `ib_client.py` instance.
   * Add startup/shutdown hooks to create a background task that:
     - Establishes the service-owned IB session using the dedicated host/port/client ID variables.
     - Subscribes to account updates and republishes them via Redis using `RedisPublisher` helpers.
   * Refactor HTTP endpoints to control subscription state:
     - `POST /subscriptions/account` to start streaming (connect if necessary, register channels, mark service health).
     - `DELETE /subscriptions/account` to stop streaming and disconnect when no consumers remain.
     - Provide configuration endpoints for client ID, account code filters, etc.
   * Implement direct call endpoints for historical account snapshots that query IB synchronously and return JSON without touching Redis.
3. Update `src/account_snapshot_service.py` and related caches to source data from the new Redis streams or direct IB queries instead of the legacy command responses.
4. Ensure the account service publishes health metrics through `src/ibbot.update_service_health` based on the state of its own IB connection.
5. Add/adjust tests (`tests/services/account/test_account_streaming.py`) covering subscription lifecycle, Redis publications, and historical endpoint responses using mocked IB clients scoped to the account service.

## 4. Refactor the Orders service

1. Expand `services/orders/ib_client.py` (from Step 1) with methods for:
   * `place_order`, `cancel_order`, `reqAllOpenOrders`, `reqOpenOrders`, `reqCompletedOrders`, `reqExecutions`, and `reqIds`.
   * Streaming order status updates, open orders, and execution reports to Redis channels (`orders:status`, `orders:executions`).
2. Rebuild `services/orders/main.py`:
   * Replace uses of `request_ib_command` with direct method calls into `ib_client.py`.
   * Provide REST endpoints mirroring existing functionality (place, cancel, replace, fetch open orders, fetch executions) but powered by live IB API calls.
   * Implement subscription management endpoints using Redis pub/sub for streaming updates, reusing the same pattern as the account service.
   * Ensure order placement waits for acknowledgement (`orderStatus`) to confirm submission before responding.
3. Update dependencies (e.g., `src/routes/service_clients.py`) to interact with the new REST contract (if route paths changed) and ensure front-end / other services continue working.
4. Expand tests to validate order placement/cancellation flows with mocked IB clients and Redis publishers (`tests/services/orders/test_orders_api.py`). Include regression cases for partial fills and error handling.

## 5. Refactor the Market Data service

1. Expand `services/market_data/ib_client.py` (from Step 1) so it manages:
   * Market data subscriptions (`reqMktData`, `cancelMktData`).
   * Market depth / DOM streams (`reqMktDepth`, `cancelMktDepth`).
   * Tick-by-tick data, real-time bars, scanners, and historical data requests as required by the UI/features.
   * Subscription bookkeeping so multiple consumers can share a single IB subscription when tick parameters match.
2. Update `services/market_data/main.py`:
   * Replace command dispatches with direct IB API calls.
   * Add endpoints:
     - `POST /subscriptions/quotes` to start or update a quote stream (publishing ticks to Redis channel `md:quotes:{symbol}`).
     - `DELETE /subscriptions/quotes` to cancel streams.
     - Equivalent endpoints for DOM (`md:dom:{symbol}`), tick-by-tick, and scanners.
     - `GET /historical` for direct historical data retrieval without Redis.
   * On startup, create the IB connection and keep a background task running to forward wrapper events to Redis publishers.
3. Provide configuration hooks for market data type (`reqMarketDataType`), delayed data fallback, and regulatory snapshots.
4. Write unit/integration tests mocking IB responses to ensure each subscription type emits the expected Redis payloads (`tests/services/market_data/test_market_data_streams.py`).

## 6. Rewire the main FastAPI application (`main.py`)

1. Remove any dependencies on `ib_command`/`ib_feed` clients.
2. Introduce new HTTP routes that orchestrate subscriptions by proxying to the refactored services (if the monolith still coordinates them).
3. Ensure Redis pub/sub channel names are documented and accessible to the front-end/strategy modules.
4. Update health-check endpoints to reflect the new service responsibilities.
5. Adjust configuration management (ENV variables, settings schemas) to capture IB host/port/client IDs per service.

## 7. Strategy service updates

1. Rewrite DOM and real-time price subscription logic in `services/strategy/*` to consume data exclusively through Redis pub/sub channels produced by the Market Data service.
2. Update strategy configuration to declare required channels and provide a mapping from instrument identifiers to Redis subjects.
3. Ensure strategy execution acknowledges subscription readiness (e.g., wait for first tick before acting) and handles reconnection if Redis stream drops.
4. Provide tests for strategy modules verifying they process Redis updates into trading signals as before (`tests/services/strategy/test_strategy_market_data.py`).

## 8. Cross-cutting tasks

1. Update environment variable documentation (`docs/configuration.md` or README) with the new IB connection settings and Redis channel structure.
2. Adjust Dockerfiles and deployment manifests so each service has IB host/port/client ID variables and no longer launches `ib_command`/`ib_feed` containers.
3. Review logging configuration to include IB connection status, request IDs, and error handling for diagnostics.
4. Execute integration tests / smoke tests:
   * Start Redis, the Account, Orders, Market Data, and Strategy services.
   * Validate that account balances, open orders, DOM, and quotes stream through Redis and can be observed via redis-cli subscribers.
   * Run automated test suites (`pytest` and any end-to-end scripts) to ensure full coverage.
5. Perform final cleanup: remove unused modules, stale configs, and update CHANGELOG / release notes summarizing the architectural shift.

## 9. Acceptance criteria

* All features previously available through `ib_command`/`ib_feed` remain functional via the new architecture.
* Services maintain healthy reconnect behaviour when TWS restarts.
* Redis pub/sub channels provide complete, real-time data for account, orders, and market data.
* Strategy service consumes DOM and price feeds exclusively from Redis and can operate without direct IB connectivity.
* Automated tests cover the new infrastructure layers.
