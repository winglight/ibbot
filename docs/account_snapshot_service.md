# Account Snapshot Service

The account snapshot service maintains cached IBKR account information and
positions in Redis by periodically dispatching commands to the IB command
queue.  It can run as a standalone worker or expose a lightweight FastAPI
application for operational tooling.

## Environment variables

| Variable | Description | Default |
| --- | --- | --- |
| `ACCOUNT_SNAPSHOT_REDIS_URL` | Redis URL for the service. Falls back to `REDIS_URL`/`DATA_LAYER_REDIS_URL` or host/port variables when unset. | `redis://127.0.0.1:6379/0` |
| `ACCOUNT_SNAPSHOT_SERVICE_NAME` | Identifier included in command/control messages. | `account-snapshot-service` |
| `ACCOUNT_SNAPSHOT_REFRESH_INTERVAL_SECONDS` | Interval for scheduled refreshes. Set to `0` to disable the periodic loop. | `300` |
| `ACCOUNT_SNAPSHOT_CHECK_INTERVAL_SECONDS` | Polling cadence for the scheduler. | `min(60, max(15, refresh_interval/2))` |
| `ACCOUNT_SNAPSHOT_REFRESH_TIMEOUT_SECONDS` | Timeout for scheduled refresh requests. | `15` |
| `ACCOUNT_SNAPSHOT_COMMAND_TIMEOUT_SECONDS` | Timeout while waiting for IB command results. | `ACCOUNT_SNAPSHOT_REFRESH_TIMEOUT_SECONDS` |
| `ACCOUNT_SNAPSHOT_COMMAND_QUEUE_KEY` | Redis list storing IB commands. | `queue:ib:commands` |
| `ACCOUNT_SNAPSHOT_COMMAND_RESULT_PREFIX` | Prefix for command result keys. | `hash:ib:result:` |
| `ACCOUNT_SNAPSHOT_RESULT_TTL_SECONDS` | Expiry applied to command result keys. | `300` |
| `ACCOUNT_SNAPSHOT_CONTROL_QUEUE_KEY` | Redis list used for control signals (fills, stale markers, manual refresh). | `queue:account-snapshot:control` |
| `ACCOUNT_SNAPSHOT_FAST_POSITIONS` | Whether scheduled refreshes request fast position snapshots. | `true` |

## Running the worker

```bash
python -m services.account_snapshot.main --mode worker
# or using the helper script
scripts/run_account_snapshot_service.py --mode worker
```

The worker keeps a single Redis connection open, listens for control messages
on `ACCOUNT_SNAPSHOT_CONTROL_QUEUE_KEY`, and periodically enqueues IB
commands on `ACCOUNT_SNAPSHOT_COMMAND_QUEUE_KEY`.

## Running the FastAPI application

```bash
python -m services.account_snapshot.main --mode api --host 0.0.0.0 --port 8082
```

Available endpoints:

- `GET /health` – service status, including last refresh timestamps.
- `POST /refresh` – trigger an immediate refresh of account info and positions.
- `POST /refresh/account-info` – refresh only account information.
- `POST /refresh/positions` – refresh only positions.
- `POST /mark-stale` – flag cached snapshots as stale (primarily for operational overrides).

## Control messages

UI components publish control messages to `ACCOUNT_SNAPSHOT_CONTROL_QUEUE_KEY`:

- `{"type": "fill"}` – postpones the next scheduled refresh after fills.
- `{"type": "refresh", "reason": "fill_event", "fast_positions": true}` –
  immediate refresh with optional reason metadata.
- `{"type": "mark_stale", "reason": "gateway_lost"}` – flags both snapshots as
  stale until fresh data is available.

The runtime also exposes a lightweight client (see `src/account_snapshot_service.py`)
that UI modules can reuse to push control messages or request on-demand
refreshes.
