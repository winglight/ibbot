# Async Executor Overview

The project uses a shared :class:`AsyncExecutor` wrapper to isolate blocking
workloads from the asyncio event loop. This executor is initialised during
application startup (see `core/__init__.py`) and exposes two dedicated thread
pools:

* **Database pool** – sized for low-latency SQLite interactions. By default the
  pool uses **4** workers. This can be tuned by setting the
  `DB_EXECUTOR_WORKERS` environment variable before the application starts.
* **CPU pool** – a general-purpose pool for CPU-bound helper tasks. Its default
  size is the minimum of 32 and the host's CPU count. Override it with the
  `CPU_EXECUTOR_WORKERS` environment variable.

When `core/__init__.py` configures the executor it also registers
`async_executor.shutdown` via `atexit`. This means the pools automatically drain
when the process exits, preventing dangling threads during normal operation.

## Picking the right pool

* Use **`run_in_db_executor`** (or `AsyncExecutor.db_executor`) for all blocking
  database calls, including the asynchronous helpers on
  `src/database.DatabaseManager`. These coroutines proxy their work to the
  shared executor and therefore **must be awaited** to ensure the work finishes
  and the connection is returned to the pool.
* Use **`run_in_cpu_executor`** (or `AsyncExecutor.cpu_executor`) for
  CPU-intensive tasks such as cache rebuilding, strategy bootstrap work, and
  other routines that should not block the main loop.

Both helpers automatically detect whether they are running inside an event loop
and will fall back to the synchronous `ThreadPoolExecutor.submit` API when
necessary (e.g., during application bootstrap).

## Shutdown considerations

* **Production/server mode** – the global executor is shut down automatically by
  the `atexit` hook mentioned above.
* **Tests and one-off scripts** – when instantiating custom executors (for
  example inside fixtures) call `get_async_executor().shutdown(wait=True)` or
  `AsyncExecutor.shutdown(wait=True)` to ensure the background threads terminate
  before the test suite finishes.

For additional context see the inline documentation in
`src/utils/executor.py` and the smoke tests in `test/test_async_executor.py`.
