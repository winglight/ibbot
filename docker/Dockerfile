# ============================================================
# Dockerfile — IBKR Trading Bot (production-ready)
# - Uses gunicorn + uvicorn workers by default (multi-process)
# - Robust .env loader in entrypoint (ignores comments/blank lines)
# - Easily override APP_MODULE/WEB_* via environment
# ============================================================

ARG BASE_IMAGE=ibbot-base:py311
FROM ${BASE_IMAGE} AS base

WORKDIR /app

# -------- App code --------
COPY . .

# Ensure runtime dirs exist (mounted volumes will override)
RUN mkdir -p logs data strategy

# -------- Defaults (override in compose/env) --------
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    APP_MODULE=main:app \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    WEB_WORKERS=4 \
    WEB_BACKLOG=2048 \
    WEB_TIMEOUT=60

EXPOSE 8000

# -------- Robust entrypoint with .env defaults --------
# - Loads .env only for keys not already set in environment
# - Ignores blank lines and comments, strips trailing inline comments
# - Starts gunicorn multi-workers by default; legacy mode available
RUN set -eux; \
  cat > /entrypoint.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# Create .env from example if missing (non-fatal)
if [[ ! -f ".env" ]]; then
  if [[ -f ".env.example" ]]; then
    echo "No .env found, generating from .env.example..."
    cp .env.example .env
  else
    echo "No .env or .env.example found. Continuing with environment defaults."
  fi
fi

# Load .env as defaults only (do NOT override existing env vars)
# - strip CRLF
# - remove full-line comments/blank lines
# - strip trailing inline comments after values
if [[ -f ".env" ]]; then
  while IFS='=' read -r k v; do
    # trim key/value whitespace
    k="${k#"${k%%[![:space:]]*}"}"; k="${k%"${k##*[![:space:]]}"}"
    v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
    # only accept valid shell var names
    if [[ -z "$k" || ! "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      continue
    fi
    # set only if not present in current environment
    if [[ -z "${!k+x}" ]]; then
      export "$k=$v"
    fi
  done < <(sed -e 's/\r$//' \
               -e 's/[[:space:]]*#.*$//' \
               -e '/^[[:space:]]*$/d' \
               -e '/^[[:space:]]*#/d' .env)
fi

# Build gunicorn command (multi workers by default)
GUNICORN_CMD=(gunicorn "${APP_MODULE:-main:app}"
  -k uvicorn.workers.UvicornWorker
  -w "${WEB_WORKERS:-4}"
  -b "${WEB_HOST:-0.0.0.0}:${WEB_PORT:-8000}"
  --backlog "${WEB_BACKLOG:-2048}"
  --timeout "${WEB_TIMEOUT:-60}"
  --graceful-timeout 20
  --keep-alive 15
  --access-logfile -
  --error-logfile -)

# Modes:
#  - (default)     : run gunicorn multi-workers
#  - USE_LEGACY=1  : run python main.py (legacy single process)
#  - test          : run pytest
#  - runner        : run strategy runner
#  - parallel      : run gunicorn + runner together
if [[ "$#" -eq 0 ]]; then
  if [[ "${USE_LEGACY:-0}" == "1" ]]; then
    echo "[LEGACY] Starting via python main.py..."
    exec python main.py
  else
    echo "Starting via gunicorn (multi-workers)..."
    exec "${GUNICORN_CMD[@]}" ${GUNICORN_EXTRA:-}
  fi
elif [[ "$1" == "test" ]]; then
  echo "Running tests..."
  exec pytest test/ -v
elif [[ "$1" == "runner" ]]; then
  echo "Starting Strategy Runner..."
  shift
  exec python -m strategy.strategy_runner "$@"
elif [[ "$1" == "parallel" ]]; then
  echo "Starting gunicorn app in background..."
  "${GUNICORN_CMD[@]}" ${GUNICORN_EXTRA:-} &
  MAIN_PID=$!
  echo "App (gunicorn) PID: $MAIN_PID"
  echo "Starting Strategy Runner..."
  shift
  python -m strategy.strategy_runner "$@" &
  RUNNER_PID=$!
  echo "Runner PID: $RUNNER_PID"
  # wait for either to exit
  wait -n "$MAIN_PID" "$RUNNER_PID"
  EC=$?
  echo "One process exited (code=$EC), terminating..."
  kill "$MAIN_PID" "$RUNNER_PID" 2>/dev/null || true
  exit "$EC"
else
  echo "Executing command: $*"
  exec "$@"
fi
EOF
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
