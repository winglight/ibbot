# ibbot Gateway Design Principles

The UI gateway coordinates multiple micro-services and long running workers.
To keep the FastAPI layer responsive and predictable we follow the principles
below when adding new routes or background features.

## 1. Queue side-effectful work

* **Orders and IB actions** – routes that trigger trades or account
  mutations must enqueue a command on the Redis list configured for the
  `ib-command-service` (`queue:ib:commands` by default).  The handler returns
  immediately with a `command_id` so clients can poll for status.
* **Strategy operations** – scheduling strategy tasks should push JSON
  payloads onto the strategy queue (`list:queue:strategy`).  Strategy workers
  consume the queue asynchronously and publish lifecycle events.
* **Risk configuration** – the risk service derives its runtime state from the
  MariaDB-backed configuration table (`config`, keys prefixed with
  `risk-service:`).  Route handlers should write through `ConfigProvider`
  (which persists to MariaDB and broadcasts cache invalidations) instead of
  calling the service synchronously.

## 2. Prefer lightweight service calls for reads

Read-only endpoints call the appropriate service HTTP API directly.  Each
docstring should note whether cached data is used so the caller understands
freshness guarantees.  For example, `GET /api/risk_rules` checks Redis for a
cached snapshot and only falls back to the HTTP client when the cache is
missing.

## 3. Avoid forbidden dependencies

* Do **not** import `core.connection_manager` from routes.
* Do **not** access the legacy `get_db_manager` or SQLite connections from new
  handlers; use service APIs instead.
* Persist configuration through the shared MariaDB `config` table (via
  `ConfigProvider`) or dedicated services to keep database concerns out of the
  gateway.

## 4. Provide audit and metadata

All mutating routes should emit audit success/failure events and include
metadata such as `command_id`, the submitting user and any key identifiers
like `symbol`.  This makes it easy to track asynchronous operations in logs
and monitoring tools.

## Examples

* `POST /api/place_order` – builds a command payload, enqueues it on
  `queue:ib:commands`, logs success/failure and broadcasts websocket updates.
* `PUT /api/risk-config` – stringifies the submitted fields, writes them
  through `ConfigProvider` (which updates the database) and relies on
  background polling to refresh services without blocking the API request.
* `GET /api/order_queue_status` – uses the risk service HTTP client for a
  lightweight status snapshot without touching Redis directly.
