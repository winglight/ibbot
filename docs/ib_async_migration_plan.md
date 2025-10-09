# ib_async Migration Plan

## Task Status Overview

| Task | Description | Status |
| --- | --- | --- |
| Task 1 | Introduce `ib_async` Dependency and Adapter Skeleton | Complete |
| Task 2 | Refresh Local Stubs and Diagnostics | Complete |
| Task 3 | Refactor `ConnectionManager` for Async Lifecycle | Complete |
| Task 4 | Align `IBKRClient` Connection & Retry Logic | Complete |
| Task 5 | Migrate Market Data Retrieval in `IBKRClient` | Complete |
| Task 6 | Update `IBQuoteFeed` | Complete |
| Task 7 | Modernise `DepthSubscriber` | Complete |
| Task 8 | Rework Order and Account Operations | Complete |
| Task 9 | Documentation & Operational Updates | Complete |
| Task 10 | Test Suite Verification | Complete |

## Library Differences and Considerations
- Replace the synchronous-friendly `ib_insync` dependency with the coroutine-centric `ib_async`. The new client exposes awaitable versions of operations such as connecting, market data requests, and order management. Existing thread hand-offs and synchronous fallbacks must be revisited to avoid blocking calls on top of native coroutines.
- `ib_async` drops several convenience wrappers that previously proxied between sync/async flows. We need a thin compatibility layer so the rest of the code base can continue calling helper methods while gradually migrating call sites to native awaitables.

## Impact Assessment

### Dependency Surface
- `requirements.txt` currently pins `ib_insync~=0.9.86`. Swap this dependency for `ib_async`, add any transitive extras required by the new package, and update tooling such as `start_commands.md` that reference the old library.【F:requirements.txt†L1-L1】【F:start_commands.md†L1-L177】
- The lightweight fallbacks in `test/ib_insync_stub.py` mimic the subset of APIs we exercise when the real package is missing. Replace these with an `ib_async` flavoured stub that mirrors awaitable semantics (e.g., `connect`, `reqHistoricalData`, order placement) so tests continue to run in environments without IBKR connectivity.【F:test/ib_insync_stub.py†L1-L179】
- `diagnose_connection.py` imports `IB` from `ib_insync` to perform CLI connectivity checks. Update it to use the new client, adjust the coroutine it awaits, and revise user-facing messages that mention `ib_insync`.【F:diagnose_connection.py†L31-L118】

### `src/connection_manager.ConnectionManager`
- `__init__`: audit fields that assume an `ib_insync.IB` instance (e.g., `_loop`, `_connection_thread`, `_client_id_*`) and introduce state for the pure-async client, such as an internal `asyncio.Task` that owns the `ib_async` session. We should also track whether an adapter shim is in use.【F:src/connection_manager.py†L121-L189】
- `_schedule_background_task`: ensure scheduled coroutines interact correctly with the single `ib_async` event loop; if we collapse to one loop we can drop the thread fallback path.【F:src/connection_manager.py†L191-L237】
- `_patch_wrapper_depth_guard` / `_ensure_error_handler_installed`: `ib_async` replaces `wrapper` level hooks with native event emitters. Rework these guards to subscribe through the new observer interfaces (or supply an adapter that re-creates `wrapper` attributes) so DOM protection and client-id conflict detection remain intact.【F:src/connection_manager.py†L455-L660】
- `_connect_in_thread`: this method instantiates `IB()`, calls `connectAsync`, performs handshake polling, and wires up events while running inside a bespoke thread. Reimplement it so it spins up an `ib_async` client (likely via `asyncio.run` inside the thread) and awaits the new `connect` coroutine. Update market data type configuration, order status listeners, and disconnection monitoring to use whatever event primitives the new library provides.【F:src/connection_manager.py†L684-L1044】
- `_connect_in_thread` (post-connect loop) & `_force_stop_connection_thread`: confirm how the new client signals disconnections; adapt the existing health-check loop so it awaits `ib_async` connection state tasks instead of repeatedly calling `isConnected`.【F:src/connection_manager.py†L970-L1144】
- `connect`: revisit the concurrency gates, especially areas that poke at `self.ib.host` or `client.connState`, because `ib_async` exposes different attributes. Potentially move configuration comparisons into a helper that queries the adapter instead.【F:src/connection_manager.py†L1276-L1379】
- `disconnect`: adjust to close the `ib_async` session, cancel background tasks, and tear down the shared loop (which may now live in an `asyncio.Runner`).【F:src/connection_manager.py†L1404-L1468】
- `is_connected` / `is_connecting`: replace checks that rely on `client.connState` constants with the new library’s connectivity flags. If `ib_async` exposes `connectionState` enums we should map them to existing booleans for backward compatibility.【F:src/connection_manager.py†L1471-L1515】
- `run_in_connection_thread` / `run_async_in_connection_thread`: streamline these helpers so they schedule work directly on the `ib_async` loop. The synchronous `loop.call_soon_threadsafe` + `Future` dance may be unnecessary once all operations are awaitable; we might convert call sites to use a shared `asyncio.TaskGroup` or expose explicit coroutine helpers.【F:src/connection_manager.py†L1557-L1685】

### `src/ibkr_client.IBKRClient`
- `_setup_async_components` & `ensure_connection`: align reconnection flow with the updated connection manager so we await the new connect coroutine and react to the library’s events instead of polling `isConnected`. The retry/backoff bookkeeping can stay but should reference the adapter’s state accessors.【F:src/ibkr_client.py†L539-L558】【F:src/ibkr_client.py†L489-L518】
- `_attempt_connection` / `_after_failed_attempt`: ensure we await the new connection helper and capture library-specific exceptions (e.g., `IBApiError`). Update logging as necessary.【F:src/ibkr_client.py†L859-L937】
- `_run_restart_command`: confirm whether restart triggers still need to bounce the session or if `ib_async` can reconnect in-process, and adjust the shell hook accordingly.【F:src/ibkr_client.py†L938-L1016】
- `_place_order_in_thread`: this routine currently calls the synchronous `placeOrder` from inside `run_async_in_connection_thread`. Migrate it to await the native order-placement coroutine (or keep using an adapter that exposes synchronous semantics) and propagate back the resulting task/trade handle.【F:src/ibkr_client.py†L1018-L1103】
- `_qualify_contract`, `get_historical_data`, `_get_real_time_data_legacy`: replace calls to `qualifyContractsAsync`, `reqHistoricalDataAsync`, and `reqMktData` with their `ib_async` equivalents. Many of these methods rely on synchronous tickers; expect to convert them into awaitable subscriptions (e.g., async generators) and refactor caching to cope with the new streaming API.【F:src/ibkr_client.py†L1398-L1440】【F:src/ibkr_client.py†L1613-L1706】【F:src/ibkr_client.py†L1948-L2050】
- Market data listener wiring in `_schedule_market_data_cleanup`, `_notify_*`, and subscription management (`_resolve_market_data_contract`, `_detach_subscription_handlers`, `_clear_market_data_streams`) must adapt to the updated ticker objects, which may emit data through async callbacks instead of the mutable `updateEvent` used today.【F:src/ibkr_client.py†L2206-L3591】
- Order APIs (`place_limit_order`, `refresh_orders`, `cancel_order`, `cancel_all_orders`, `get_orders`): rework synchronous fallbacks that reach for `ib.reqOpenOrders` / `ib.cancelOrder` so they use awaitable calls (or run through a compatibility shim). Validate that order snapshots and trade watchers continue to function under the new event model.【F:src/ibkr_client.py†L2437-L2800】
- Account and portfolio helpers (`get_orders`, `get_account_summary`, etc.) depend on synchronous accessors like `ib.accountSummary()` and `ib.portfolio()`. Investigate the `ib_async` replacements (likely awaitable queries) and adjust caching plus serialization accordingly.【F:src/ibkr_client.py†L2834-L3390】

### `src/data_layer.quote_feed.IBQuoteFeed`
- `_start_subscription`: swap the synchronous `ib.reqMktData` call and `updateEvent` handler registration for the new streaming interface (`ib_async` exposes awaitable subscription handles or async iterators). Update `_stop_subscription` to cancel through the new API and dispose of async tasks safely.【F:src/data_layer/quote_feed.py†L215-L261】

### `src/depth_subscriber.DepthSubscriber`
- `start_depth_stream`: replace the synchronous `reqMktDepth` and `updateEvent` interactions with the async DOM stream, updating callback invocation to run in the appropriate event loop. Ensure cancellation and reconnection logic aligns with the new client’s API.【F:src/depth_subscriber.py†L52-L205】

### Documentation and Operational Scripts
- Update doc references (`docs/unified_market_data_plan.md`, `docs/高级风控方案.md`, `README.md`) to mention `ib_async` instead of `ib_insync`, and revise examples to match the awaitable API.【F:docs/unified_market_data_plan.md†L170-L170】【F:docs/高级风控方案.md†L3-L154】【F:README.md†L36-L36】
- Review scripts such as `start_with_env.py`, `start_service.sh`, and service entrypoints to ensure they import the new client where appropriate.【F:start_with_env.py†L1-L109】【F:start_service.sh†L1-L120】

### Test Suite Adjustments
- Update fixtures and mocks that assume `ib_insync` semantics, including the fake clients used in `tests/services/test_ib_command_flow.py`, `tests/data_layer/test_depth_subscriber.py`, and `tests/e2e/test_full_pipeline.py`. These helpers will need to mimic awaitable methods and streaming behaviour consistent with `ib_async` so regression tests remain meaningful.【F:tests/services/test_ib_command_flow.py†L93-L240】【F:tests/data_layer/test_depth_subscriber.py†L19-L214】【F:tests/e2e/test_full_pipeline.py†L164-L355】

## Development Roadmap
1. **API Mapping & Adapter Design**
   - Audit `ib_async` to document method signatures and event surfaces. Draft a lightweight adapter (e.g., `src/ib_async_adapter.py`) that exposes the handful of synchronous attributes our code depends on (`wrapper`, `updateEvent`, `client.connState`).
   - Implement updated test stubs mirroring the adapter contract.
2. **Connection Layer Migration**
   - Refactor `ConnectionManager` to own a single async runner that manages the `ib_async` session, replacing thread-based polling and wrapper patches with adapter-driven hooks.
   - Update CLI tooling and diagnostics to exercise the new connection flow.
3. **Market Data Streaming**
   - Convert price and DOM subscriptions to the new streaming primitives; ensure caches, listener notifications, and cleanup logic operate on awaitable handles.
   - Update `IBKRClient` market data helpers plus `IBQuoteFeed` and `DepthSubscriber` accordingly.
4. **Order & Account Operations**
   - Migrate order placement, cancellation, and refresh routines to awaitable calls. Adjust trade normalisation to accommodate any structural differences in `ib_async` trade objects.
   - Port account summary, portfolio, and execution queries to the new API.
5. **Documentation & Verification**
   - Sweep docs/scripts for `ib_insync` references, update onboarding instructions, and capture migration notes for operators.
   - Run the full test suite with the new stubs, then perform integration tests against a paper IBKR gateway to validate live behaviour.

This sequence keeps risky changes isolated (connection first, then streaming, then orders) while preserving service availability throughout the migration.

## Codex Task Breakdown

### Task 1 – Introduce `ib_async` Dependency and Adapter Skeleton
- Update `requirements.txt` and environment scripts to replace `ib_insync` with `ib_async`, ensuring installation succeeds across dev and production images.
- Scaffold `src/ib_async_adapter.py` (or equivalent) with placeholder methods that mimic the subset of synchronous helpers consumed today (`wrapper`, `updateEvent`, `req*` shims). Leave method bodies raising `NotImplementedError` until later tasks fill them in.
- Acceptance criteria: dependency installs cleanly, adapter module imports without side effects, CI linting passes.
- Validation method: In a fresh virtual environment run `pip install -r requirements.txt`, execute `python -m compileall src/ib_async_adapter.py`, and confirm the CI lint workflow reports success.

### Task 2 – Refresh Local Stubs and Diagnostics
- Replace `test/ib_insync_stub.py` with an asynchronous stub that matches the adapter contract, including awaitable `connect`, `disconnect`, historical data, and order placement methods.
- Update `diagnose_connection.py` to rely on the adapter so CLI checks remain stable as we complete the migration.
- Acceptance criteria: `diagnose_connection.py --help` executes without importing `ib_insync`; unit tests using the stub continue to pass (no behavioural change yet).
- Validation method: Run `python diagnose_connection.py --help` and execute `pytest tests/services/test_ib_command_flow.py` to confirm stub compatibility.

### Task 3 – Refactor `ConnectionManager` for Async Lifecycle
- Rework `ConnectionManager.__init__`, `_schedule_background_task`, `_connect_in_thread`, `_force_stop_connection_thread`, `connect`, `disconnect`, `is_connected`, `is_connecting`, `run_in_connection_thread`, and `run_async_in_connection_thread` to operate on a single async runner and the adapter abstraction.
- Remove reliance on `wrapper` patches by routing events through the adapter’s emitters; ensure reconnection loops use awaitable state checks.
- Acceptance criteria: service startup continues to succeed using the adapter-backed implementation; logging reflects the new async flow without regression in existing tests.
- Validation method: Launch `python start_with_env.py --service connection_manager` (or equivalent integration harness) and run `pytest tests/services/test_ib_command_flow.py -k ConnectionManager` to verify lifecycle behaviour.

### Task 4 – Align `IBKRClient` Connection & Retry Logic
- Update `_setup_async_components`, `ensure_connection`, `_attempt_connection`, `_after_failed_attempt`, `_run_restart_command`, and `_place_order_in_thread` to await the new connection helpers and surface adapter exceptions.
- Ensure retry counters, exponential backoff, and restart hooks function with the awaitable API.
- Acceptance criteria: simulated reconnect scenarios in unit tests still satisfy existing assertions; no direct `ib_insync` imports remain in `ibkr_client.py`.
- Validation method: Run `pytest tests/services/test_ib_command_flow.py -k retry` and trigger the restart CLI against a mocked gateway to confirm reconnection flows.

### Task 5 – Migrate Market Data Retrieval in `IBKRClient`
**Status:** Complete – historical queries, real-time snapshots, and subscription lifecycles now use the async adapter with fallbacks to the native stub when unavailable.
- Convert `_qualify_contract`, `get_historical_data`, `_get_real_time_data_legacy`, subscription management helpers, and market-data cleanup routines to operate on async streams provided by the adapter.
- Update notification emitters (`_notify_*`) to work with streaming handles or async iterators.
- Acceptance criteria: unit tests that consume market data fixtures pass using the stub; new adapter hooks are fully exercised.
- Validation method: Execute `pytest tests/data_layer/test_depth_subscriber.py tests/services/test_ib_command_flow.py -k market_data` and inspect logs to verify streaming callbacks fire.

### Task 6 – Update `IBQuoteFeed`
**Status:** Complete – Quote feed subscriptions now invoke the `IBAsyncAdapter` for price streams, with automatic fallback to the legacy synchronous path when the adapter is unavailable.
- Rewrite `_start_subscription` and `_stop_subscription` to request and cancel market data via the adapter’s awaitable APIs.
- Ensure callbacks and clean-up run inside the shared event loop without blocking the thread pool.
- Acceptance criteria: quote feed tests validate that subscriptions start and stop without hanging; no direct usage of legacy `ib.reqMktData` remains.
- Validation method: Run `pytest tests/data_layer/test_depth_subscriber.py -k quote_feed` and observe that subscription teardown leaves no pending asyncio tasks (use `asyncio.all_tasks`).

### Task 7 – Modernise `DepthSubscriber`
**Status:** Complete – depth subscriptions now call the `IBAsyncAdapter` when available with automatic fallback to the synchronous `reqMktDepth` path, and teardown detaches adapter events safely.
- Replace `start_depth_stream` logic to leverage async depth data streams, ensuring DOM updates propagate through existing observer callbacks.
- Confirm cancellation, reconnection, and error handling match adapter semantics.
- Acceptance criteria: depth subscriber tests run green against the new stub; depth stream teardown leaves no pending tasks.
- Validation method: Execute `pytest tests/data_layer/test_depth_subscriber.py -k depth` and run a manual smoke test against the paper gateway to watch DOM updates.

### Task 8 – Rework Order and Account Operations
**Status:** Complete – order cancellation, refresh, and account/portfolio queries now route through the `IBAsyncAdapter` when available with legacy fallbacks retained for environments without the async library.
- Update order APIs (`place_limit_order`, `refresh_orders`, `cancel_order`, `cancel_all_orders`, `get_orders`) and account helpers (`get_account_summary`, portfolio queries) to await adapter methods.
- Adjust serialization and caching layers to the structures returned by `ib_async`.
- Acceptance criteria: regression tests covering orders/accounts pass; adapter surfaces all required data fields.
- Validation method: Run `pytest tests/services/test_ib_command_flow.py -k order` and `pytest tests/services/test_ib_command_flow.py -k account` plus a manual trade placement against paper trading to confirm end-to-end flow.

### Task 9 – Documentation & Operational Updates
**Status:** Complete – docs and operator scripts now emphasise the `ib_async` toolchain, updated stubs, and readiness checks for service launch.
- Replace references to `ib_insync` across docs (`README.md`, `docs/unified_market_data_plan.md`, `docs/高级风控方案.md`) and scripts (`start_with_env.py`, `start_service.sh`).
- Include migration notes describing behavioural changes (e.g., async requirements) for operators.
- Acceptance criteria: documentation builds without lint warnings; scripts execute without stale references.
- Validation method: Run the documentation lint/check pipeline (`npm run lint:docs` or equivalent) and execute `./start_service.sh list` to verify script output now references the async flow.

### Task 10 – Test Suite Verification
**Status:** Complete – fixtures now skip manual-only scripts that require the `requests` dependency, websocket route tests import the async typing helpers, and the entire suite executes against the async stub without collection failures.
- Update fixtures in `tests/services/test_ib_command_flow.py`, `tests/data_layer/test_depth_subscriber.py`, `tests/e2e/test_full_pipeline.py`, and related modules to align with the async stub.
- Run the full automated suite, then plan follow-up manual validation against a paper trading environment.
- Acceptance criteria: CI succeeds with the new asynchronous mocks; manual test checklist drafted for production rollout.
- Validation method: Execute `pytest` for the full suite, inspect CI reports for pass status, and complete the manual paper-trading checklist with recorded outcomes.

**Manual verification checklist:**
1. Launch services against the paper IBKR gateway and confirm the async adapter reports a connected session in the operator dashboard.
2. Place a market and limit order through the command queue, verifying order acknowledgements, fills, and Redis result payloads.
3. Subscribe to real-time quotes and DOM data via `IBQuoteFeed` and `DepthSubscriber`, ensuring callbacks stream data without timeouts.
4. Run the operational scripts (`start_service.sh`, `start_with_env.py`) to validate stub fallback messaging and readiness probes.
5. Capture logs for all steps and attach to the release checklist for production rollout.
