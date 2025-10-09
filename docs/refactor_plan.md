# Refactor Implementation Plan

## Execution Progress Overview

| Step | Scope | Current Status | Notes |
| --- | --- | --- | --- |
| 1 | Service-scoped IB integrations | Not Started | Awaiting design review |
| 2 | Remove `ib_command` / `ib_feed` | Not Started | Blocked by Step 1 artifacts |
| 3 | Account service refactor | Not Started | Requires Step 1 client + Step 2 cleanup |
| 4 | Orders service refactor | Not Started | Depends on Step 1 + Step 2 completion |
| 5 | Market data service refactor | Not Started | Depends on Step 1 + Step 2 completion |
| 6 | Main FastAPI rewiring | Not Started | Requires Steps 2–5 completion |
| 7 | Strategy service updates | Not Started | Requires Step 5 data parity validation |
| 8 | Cross-cutting tasks | Not Started | Requires Step 1–7 completion |
| 9 | Acceptance validation | Not Started | Execute after Step 8 sign-off |

This plan describes the concrete code changes required to migrate the IB Bot stack away from the legacy `ib_command`/`ib_feed` bridge and toward dedicated Interactive Brokers (IB) TWS API integrations inside the Account, Orders, Market Data, and Strategy services. Follow the steps in sequence; every step contains the files to edit, new modules to introduce, and acceptance checks to perform before moving on.

> **Prerequisites**
>
> * Ensure IB Gateway / Trader Workstation is reachable, and record the host/port/client ID combinations reserved for each microservice.
> * Confirm Redis is running and reachable by every service (most services already expect `REDIS_URL`).
> * Capture the full list of existing REST routes and Redis pub/sub channels so parity checks can be run after the refactor.

## 1. Prepare service-scoped IB integrations

1. For each service (`services/account`, `services/orders`, `services/market_data`) introduce a local `ib_client.py` that owns its **own** `IBClient(EWrapper, EClient)` subclass, mirroring the connection lifecycle in `IBJts/samples/Python/Testbed/Program.py`:
   * File/class additions:
     - `services/<service>/ib_client.py` defines `class ServiceIBClient(EWrapper, EClient)`.
     - `ServiceIBClient` contains private state `_connection_ready`, `_next_valid_id`, `_reader: EReader | None`, `_reader_thread: threading.Thread | None`, `msg_queue: queue.Queue`, and queues for callback forwarding (`asyncio.Queue`).
   * Method-level requirements:
     - `async def connect(self)`: read `ACCOUNT_IB_HOST`/`PORT`/`CLIENT_ID` (or service-specific variants), call `self.connect(host, port, client_id)`, create an `EReader(self, self.msg_queue)` exactly as in the sample, start it on a dedicated thread, and launch a second pump thread that repeatedly invokes `self.run()` while pushing messages into the queue. Use an `asyncio.Event` that is set by `nextValidId` to gate readiness before resolving.
     - `async def disconnect(self)`: stop the reader (`self.reader.stop()`), join both background threads, clear `_connection_ready`, drain queues safely, and reset outstanding request IDs.
     - `def nextValidId(self, orderId)`: persist the ID, set `_connection_ready`, guard access with a `threading.Lock`, and drain any waiters awaiting ID allocation.
     - `def _forward_event(self, event_type, payload)`: push tuples onto the queue consumed by service-specific processors while ensuring serialization happens on the callback thread; when conversion requires async context use `loop.call_soon_threadsafe` as shown in the sample.
     - Provide request ID helpers (`def next_request_id()`) guarded by `threading.Lock` + `asyncio.Lock` hybrid to ensure thread-safety between callback and coroutine callers.
   * Lifecycle responsibilities:
     - No cross-service imports; each service instantiates its own client in `main.py`.
     - Use `asyncio.get_running_loop().call_soon_threadsafe` to bridge callback threads to async context rather than blocking IB threads; fallback to `run_in_executor` for CPU-heavy tasks.
   * Safety guarantees:
     - Mirror the sample's usage of `collections.defaultdict` for request tracking and ensure `reqId` counters never regress when reconnecting.
     - Persist only thread-safe caches inside the client (e.g., `queue.Queue`, `asyncio.Queue`) and keep durable state (positions/orders/ticks) in service-specific repositories/Redis to avoid cross-thread mutation risks.
2. Add a lightweight shared events module, `src/ib/events.py`:
   * Define Pydantic models `AccountUpdateEvent`, `PortfolioPositionEvent`, `OrderStatusEvent`, `ExecutionEvent`, `MarketTickEvent`, `DOMLevelEvent`, `ErrorEvent` with field-level validation mirroring IB message structure and ensuring decimal fields use `Decimal` to preserve precision (matching IB callbacks).
   * Provide factory functions `from_ib_wrapper(...)` to translate raw callback parameters into structured models.
   * Export enums for Redis channel suffixes (`class RedisChannel(Enum)`).
3. Create `src/ib/redis_streaming.py` with utilities shared by services:
   * Implement `class RedisPublisher` encapsulating an aioredis connection pool, JSON serialization (via `orjson`), and error logging; expose `async def publish(channel: str, payload: BaseModel | dict)`.
   * Provide helper coroutines (`async def publish_account_event(pub, event)`, etc.) that map event models to canonical channel names and convert decimals to strings for precision.
   * Include retry logic with exponential backoff when Redis is temporarily unavailable, capped at 3 retries.
4. Add focused unit tests `tests/ib/test_events_and_redis.py`:
   * Use `pytest.mark.asyncio` tests to verify serialization, channel naming, and error handling with `async_mock` Redis clients.
   * Cover edge cases for NaN handling, timezone normalization, and partial DOM level updates.

### Data flow impact after Step 1

* Account/Position information: No external behaviour change yet; new client isolates request routing per service and prepares future event fan-out. The background reader threads mirror IB's sample behaviour so state transitions stay deterministic.
* DOM/Volume-price data: Definitions for events and Redis publishers clarify how DOM updates will be standardized but no live feeds moved yet. Thread-safe queues buffer tick snapshots before publication.
* Strategy logic: Unchanged; strategies remain connected to legacy streams until services begin publishing via new models.

## 2. Remove `ib_command` and `ib_feed` services

1. Delete the `services/ib_command` and `services/ib_feed` packages (FastAPI apps, Docker configs, start scripts) and ensure their removal from import paths.
2. Update deployment assets:
   * `docker-compose.yml`, Helm charts, and `scripts/` runners should drop container definitions and environment variables for the removed services.
   * Adjust GitHub workflows or CI scripts that referenced the deprecated services.
3. Modify shared routing modules:
   * `src/routes/service_clients.py`: remove classes like `IBCommandClient`; ensure dependency injection no longer instantiates them.
   * Provide temporary shims or feature flags only if tests still rely on stubbed endpoints.
4. Update Redis helper layer:
   * Delete `request_ib_command`, `IBCommandError`, and similar adapters from `src/ibbot/redis.py`.
   * Introduce placeholder functions that raise informative errors when legacy callers have not yet been migrated (to be removed in Step 6).
5. Test clean-up:
   * Delete or refactor `tests/services/test_ib_command_*` to target the new service clients when available.
   * Ensure coverage for removal by asserting import failures of removed modules in smoke tests.

### Data flow impact after Step 2

* Account/Position information: Legacy command bridge is offline; downstream modules must rely on service-local IB clients once they are wired (Step 3 onward).
* DOM/Volume-price data: No streaming occurs from `ib_feed`; temporary outage acceptable during transition plan.
* Strategy logic: Strategies should be flagged as read-only during this stage; they must not attempt to auto-trade until new Redis channels are validated.

## 3. Refactor the Account service

1. Expand `services/account/ib_client.py`:
   * Methods to implement:
     - `async def subscribe_account_updates(account_codes: list[str])`: call `reqAccountUpdates(True, account)` for each account and push `AccountUpdateEvent` via callback queue.
     - `async def unsubscribe_account_updates()`: call `reqAccountUpdates(False, "")` and clear caches.
     - `async def fetch_account_summary(tags: str)`: wrap `reqAccountSummary`, collate responses, and return `dict[str, Decimal]` with timezone-aware timestamps.
     - `async def fetch_positions()`: manage `reqPositions` / `cancelPositions`, return list of `PortfolioPositionEvent` models.
     - `async def fetch_daily_pnl(account)`: leverage `reqPnL` and ensure `cancelPnL` after response window closes.
     - `async def fetch_historical_nav(contract, duration, bar_size)`: provide direct `reqHistoricalData` integration with data validation.
   * Callback logic:
     - Implement `_on_account_value`, `_on_portfolio_update`, `_on_position_end` to convert IB payloads into events using `src/ib/events.py` factories. Callbacks should perform minimal work and immediately schedule async handlers via `loop.call_soon_threadsafe`, keeping the IB reader thread responsive as demonstrated in `Program.py`.
   * Concurrency/storage rules:
     - Maintain per-account caches in thread-safe dicts guarded by `threading.RLock` and flush updates into Redis/task queues; never mutate FastAPI request state directly from the IB callback thread.
     - Persist long-lived aggregates (e.g., last account summary) in the service data layer/Redis rather than IB client attributes to avoid cross-thread races.
2. Modify `services/account/main.py`:
   * Instantiate `ServiceIBClient` during FastAPI startup and inject into route dependencies.
   * Add background task `account_event_loop()` that consumes the client queue and publishes to Redis using `publish_account_event`, ensuring consumption uses `asyncio.Queue.get()` to avoid busy waiting and verifying shutdown waits for queue draining.
   * Endpoints:
     - `POST /subscriptions/account`: body includes account codes and tags; starts client connection if not running, registers subscription count, and returns first snapshot.
     - `DELETE /subscriptions/account`: decrements subscription count and disconnects when zero consumers remain.
     - `GET /accounts/{account_id}/summary`: synchronous fetch using `await client.fetch_account_summary(tags)`.
     - `GET /accounts/{account_id}/positions`: returns normalized positions (with currency + contract identifiers).
   * Ensure dependency injection handles Redis publisher lifecycle and health updates via `update_service_health(service="account", status=ServiceStatus.UP)`.
3. Update snapshot consumers:
   * `src/account_snapshot_service.py`: replace legacy `request_ib_command` usage with either Redis subscription or direct client call; maintain caching semantics and TTLs.
   * Adjust caches in `src/cache/account.py` (if present) to map to new event model schema and ensure cache writes happen on the event loop thread.
4. Observability:
   * Emit structured logs containing IB host/port, account codes, and subscription counts.
   * Integrate Prometheus counters for published events.
5. Testing:
   * `tests/services/account/test_account_streaming.py`: mock `ServiceIBClient` queue outputs, assert Redis publisher receives `AccountUpdateEvent` JSON.
   * `tests/services/account/test_account_endpoints.py`: cover summary/positions/PnL endpoints and ensure proper error translation.

### Data flow impact after Step 3

* Account/Position information: Streams migrate to service-local IB client publishing via Redis; historical endpoints source directly from IB with thread-safe caching and queue handoff.
* DOM/Volume-price data: Still pending; only account snapshots affected.
* Strategy logic: No immediate change but strategies consuming account data should verify compatibility with new Redis payload schema and new event ordering guarantees.

## 4. Refactor the Orders service

1. Expand `services/orders/ib_client.py`:
   * Implement command coroutines:
     - `async def place_order(contract, order)`: allocate order ID via `next_request_id`, call `placeOrder`, await corresponding `OrderStatusEvent` with statuses `Submitted`/`PreSubmitted` before returning.
     - `async def cancel_order(order_id)`: ensure cancellation is idempotent; wait for `Cancelled` status event.
     - `async def fetch_open_orders(include_all_accounts: bool)`: wrap `reqAllOpenOrders`/`reqOpenOrders` accordingly.
     - `async def fetch_completed_orders(days_back: int)`: use `reqCompletedOrders`; filter results by time window.
     - `async def fetch_executions(filters)`: translate filter dict into `ExecutionFilter` and call `reqExecutions`.
     - `async def refresh_order_ids()`: call `reqIds` on reconnect to sync `next_valid_id`.
   * Callback handlers:
     - `_on_order_status`, `_on_open_order`, `_on_execution_detail`, `_on_commission_report` convert to `OrderStatusEvent`/`ExecutionEvent` and queue for consumption. Emulate the sample's pattern by avoiding blocking operations on the callback thread and forwarding data using `loop.call_soon_threadsafe`. Guarantee that commission and execution data share an immutable snapshot ID to prevent partial writes.
   * Thread-safety:
     - Protect the order ID allocator with `threading.Lock` and persist the last server-provided `nextValidId` on disk/Redis to survive service restarts without duplicates.
     - Keep mutable tracking maps (`permid` -> order) inside the async service layer; IB client retains only thread-safe references and read-only views to prevent cross-thread data corruption.
2. Update `services/orders/main.py`:
   * Startup: connect client, start queue consumer that publishes `orders:status`, `orders:executions` via `RedisPublisher`.
   * REST routes:
     - `POST /orders`: accept contract/order schemas, call `place_order`, and return enriched status JSON.
     - `DELETE /orders/{order_id}`: cancel order and report final status.
     - `GET /orders/open`: call `fetch_open_orders` and map to API schema.
     - `GET /orders/completed`: proxy to `fetch_completed_orders`.
     - `GET /orders/executions`: fetch filtered execution reports.
     - Subscription endpoints `POST/DELETE /subscriptions/orders` manage consumer counts like the account service.
   * Error handling: translate IB error codes to HTTP responses; log rejected orders with context.
3. Update dependencies:
   * `src/routes/service_clients.py`: ensure order-related routers call the new endpoints; remove references to `ib_command` wrappers.
   * Front-end adapters: adjust to new payload format (e.g., include `permid`, `client_id`, `transmit` fields) while maintaining compatibility with existing UI components.
4. Testing:
   * `tests/services/orders/test_orders_api.py`: simulate order placement lifecycle, partial fills, rejected orders, and verify Redis emissions for each event.
   * Add contract fixture factories for common instruments to simplify tests.

### Data flow impact after Step 4

* Account/Position information: Order fills and commission events now flow through Redis, enabling account service to reconcile positions without direct IB calls while preserving ordering guarantees from the queue consumers.
* DOM/Volume-price data: Still pending; only order execution streams updated.
* Strategy logic: Trading strategies must switch to consuming `orders:status`/`orders:executions` channels for decision feedback loops, with confidence that thread-safe buffering prevents missed states during reconnects.

## 5. Refactor the Market Data service

1. Expand `services/market_data/ib_client.py`:
   * Quote subscriptions:
     - `async def subscribe_quotes(contract, snapshot: bool, tick_types: list[int])`: deduplicate subscriptions via a key `(conId, snapshot, tuple(sorted(tick_types)))` and manage reference counts.
     - `async def unsubscribe_quotes(subscription_id)`: decrement reference count, call `cancelMktData` when zero.
   * DOM streams:
     - `async def subscribe_dom(contract, depth: int)`: call `reqMktDepth`; maintain depth cache keyed by `(conId, exchange)`.
     - `async def unsubscribe_dom(subscription_id)` handles `cancelMktDepth` and cache cleanup.
   * Tick-by-tick / bars:
     - Implement `async def subscribe_tick_by_tick(contract, tick_type)` and `async def subscribe_real_time_bars(contract, bar_size, what_to_show)` with matching unsubscribe methods.
   * Historical data:
     - `async def fetch_historical(contract, duration, bar_size, what_to_show, keep_up_to_date)` returning list of OHLCV bars.
   * Callback translation:
     - `_on_tick_price`, `_on_tick_size`, `_on_tick_by_tick_all_last`, `_on_update_mkt_depth` convert to `MarketTickEvent` / `DOMLevelEvent`. Each callback must only normalize payloads and then forward them via `loop.call_soon_threadsafe` to avoid holding the IB reader thread, similar to the approach in `Program.py`.
   * Thread-safety/data retention:
     - Maintain DOM depth caches using `threading.RLock` and copy data structures before publishing so downstream consumers never observe partially mutated depth ladders.
     - Track subscription metadata in immutable dataclasses stored in the async layer; IB client only keeps identifiers and thread-safe counters.
2. Update `services/market_data/main.py`:
   * Startup tasks connect IB client, set `reqMarketDataType` based on env configuration, and start queue consumers that publish to Redis channels `md:quotes:{symbol}`, `md:dom:{symbol}`, etc.
   * REST/WS endpoints:
     - `POST /subscriptions/quotes`: request body includes contract spec, tick types, optional `snapshot`; returns subscription ID.
     - `DELETE /subscriptions/quotes/{subscription_id}`: removes subscription.
     - Equivalent endpoints for DOM (`/subscriptions/dom`), tick-by-tick, scanners, and bars.
     - `GET /historical`: direct call to `fetch_historical` returning OHLCV arrays with timezone metadata.
   * Backpressure: implement rate limiting to prevent >50 concurrent subscriptions per account unless override env var set.
3. Configuration hooks:
   * Environment variables `MARKET_DATA_TYPE`, `MARKET_DATA_DELAYED_FALLBACK`, `MARKET_DATA_REGULATORY_SNAPSHOT` interpreted during startup to adjust IB requests.
   * Provide admin endpoint `POST /config/reload` to reapply configuration at runtime.
4. Testing:
   * `tests/services/market_data/test_market_data_streams.py`: simulate tick and DOM callbacks, assert Redis payloads contain normalized price/size data and subscription metadata.
   * Add soak test verifying subscription deduplication prevents duplicate IB requests.

### Data flow impact after Step 5

* Account/Position information: No additional changes; market data service remains read-only for account state and continues to offload durable storage to Redis rather than the IB thread.
* DOM/Volume-price data: Live quotes and depth now stream through Redis with standardized schemas, replacing legacy `ib_feed` topics while preserving DOM cache integrity through copy-on-write updates.
* Strategy logic: Strategies must reconfigure to consume `md:quotes:*` and `md:dom:*` channels; latency should decrease due to direct service integration backed by the reader-thread-safe handoff.

## 6. Rewire the main FastAPI application (`main.py`)

1. Dependency cleanup:
   * Remove imports of `ib_command`, `ib_feed`, and any shared `IBClient` singletons.
   * Update dependency injection to use HTTP clients targeting the Account, Orders, and Market Data services.
2. Route updates:
   * Revise subscription orchestration endpoints to invoke the new REST endpoints created in Steps 3–5.
   * Provide consolidated metadata endpoint `/ib/channels` listing Redis subjects per instrument/service.
3. Health checks:
   * Extend `/health` to aggregate status from Redis (ping), Account, Orders, and Market Data services, returning structured JSON.
4. Configuration:
   * Expand `Settings` dataclasses to include `ACCOUNT_IB_HOST`, `ORDERS_IB_HOST`, `MARKET_DATA_IB_HOST`, etc., and ensure they propagate to deployment manifests.
   * Update CLI arguments and environment variable docs to match new configuration keys.
5. Middleware/logging:
   * Ensure request tracing propagates correlation IDs to downstream services and logs Redis channel operations.

### Data flow impact after Step 6

* Account/Position information: Main API now proxies to dedicated services; ensures consistent Redis channel metadata.
* DOM/Volume-price data: Central app no longer fetches data directly; documentation clarifies channels for consumers.
* Strategy logic: Orchestration layer can coordinate subscription lifecycles via new endpoints, improving resilience.

## 7. Strategy service updates

1. Subscription rewiring:
   * Update strategy base classes in `services/strategy/base.py` to initialize Redis pub/sub clients (via `asyncio` tasks) instead of direct IB connections.
   * Modify strategy implementations (e.g., `services/strategy/mean_reversion.py`, `services/strategy/scalping.py`) to consume `MarketTickEvent` and `DOMLevelEvent` payloads from Redis, explicitly decoupling any residual references to IB client singletons so strategy threads remain purely async.
2. Configuration:
   * Extend strategy config schema with `required_channels`, `symbol_to_channel_map`, and `latency_budget_ms` fields.
   * Provide CLI/ENV overrides for channel mappings and allow toggling delayed data usage.
3. Execution flow:
   * Ensure strategies await first quote/DOM event before placing orders; integrate heartbeat monitoring to resubscribe when Redis disconnects.
   * Use new order client wrappers from Step 4 to place/cancel orders.
4. Testing:
   * `tests/services/strategy/test_strategy_market_data.py`: mock Redis subscription generator, feed synthetic ticks/DOM updates, and assert strategies emit correct trading signals.

### Data flow impact after Step 7

* Account/Position information: Strategies now rely on account Redis channels to track exposure; ensure risk checks align with new payload schema.
* DOM/Volume-price data: Strategies fully consume the standardized Redis data from Market Data service.
* Strategy logic: Event-driven loops simplified; removal of direct IB integration reduces coupling and improves testability while ensuring no strategy coroutine touches the IB reader threads.

## 8. Cross-cutting tasks

1. Documentation updates:
   * Revise `docs/configuration.md`, `README.md`, and deployment runbooks to describe per-service IB connectivity and Redis channel taxonomy.
   * Include migration notes summarizing removed legacy services and new endpoints.
2. Deployment manifests:
   * Update Dockerfiles to include necessary Python modules (`orjson`, `aioredis`) and remove legacy startup scripts.
   * Adjust Kubernetes secrets/config maps with per-service client IDs and ensure health probes point to new endpoints.
3. Observability & logging:
   * Standardize logging format across services (JSON with fields `service`, `ib_client_id`, `request_id`).
   * Add metrics exporters for Redis publish failures, IB reconnects, and subscription counts.
4. Integration testing:
   * Spin up Redis + all refactored services; use automated script to trigger account/order/market data workflows and record Redis payloads for regression baselines.
   * Execute `pytest`, smoke tests, and strategy dry runs to validate end-to-end behaviour, including multithreaded stress tests that simulate rapid reconnects to ensure reader threads shut down cleanly.
5. Final cleanup:
   * Remove unused modules, stale configs, and update CHANGELOG / release notes summarizing the architectural shift.

### Data flow impact after Step 8

* Account/Position information: Documentation reflects new channels; observability ensures data quality across services.
* DOM/Volume-price data: Integration tests confirm continuous streaming and latency metrics.
* Strategy logic: Dry runs confirm readiness for production cutover.

## 9. Acceptance criteria

* All features previously available through `ib_command`/`ib_feed` remain functional via the new architecture.
* Services maintain healthy reconnect behaviour when TWS restarts.
* Redis pub/sub channels provide complete, real-time data for account, orders, and market data.
* Strategy service consumes DOM and price feeds exclusively from Redis and can operate without direct IB connectivity.
* Automated tests cover the new infrastructure layers.

### Data flow impact after Step 9

* Account/Position information: Final validation confirms parity with legacy system and reconcilable audit logs.
* DOM/Volume-price data: Benchmarks demonstrate stable throughput/latency meeting production SLOs.
* Strategy logic: Strategies operate end-to-end using new infrastructure with automated rollback plan documented.
