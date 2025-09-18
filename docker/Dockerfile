# Multi-stage build for IBKR Trading Bot
FROM python:3.11-slim as base

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    # （可选）排障工具：需要的话解开下一行注释
    # procps net-tools iproute2 dnsutils iputils-ping mtr-tiny \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn "uvicorn[standard]"

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p logs data strategy

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Web defaults (可被 compose 覆盖)
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8000
ENV WEB_WORKERS=4
ENV WEB_BACKLOG=2048
ENV WEB_TIMEOUT=60

# Expose ports
EXPOSE 8000

# Create entrypoint script
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
# Ensure .env exists (fallback to .env.example if provided)\n\
if [ ! -f ".env" ]; then\n\
    if [ -f ".env.example" ]; then\n\
        echo "No .env found, generating from .env.example..."\n\
        cp .env.example .env\n\
    else\n\
        echo "No .env or .env.example found. Continuing with environment defaults."\n\
    fi\n\
fi\n\
\n\
# Load .env as defaults only (do NOT override existing environment variables)\n\
if [ -f ".env" ]; then\n\
    while IFS=\"=\" read -r key value; do\n\
        [ -z \"$key\" ] && continue\n\
        case \"$key\" in \\#*) continue;; esac\n\
        if [ -z \"${!key+x}\" ]; then\n\
            export \"$key=$value\"\n\
        fi\n\
    done < .env\n\
fi\n\
\n\
# Build gunicorn command\n\
GUNICORN_CMD=(gunicorn app:app \\\n\
  -k uvicorn.workers.UvicornWorker \\\n\
  -w \"${WEB_WORKERS:-4}\" \\\n\
  -b \"${WEB_HOST:-0.0.0.0}:${WEB_PORT:-8000}\" \\\n\
  --backlog \"${WEB_BACKLOG:-2048}\" \\\n\
  --timeout \"${WEB_TIMEOUT:-60}\" \\\n\
  --graceful-timeout 20 \\\n\
  --keep-alive 15 \\\n\
  --access-logfile - \\\n\
  --error-logfile -)\n\
\n\
# Default behavior: run gunicorn multi-workers unless overridden\n\
if [ \"$#\" -eq 0 ]; then\n\
    if [ \"${USE_LEGACY:-0}\" = \"1\" ]; then\n\
        echo \"[LEGACY] Starting IBKR Trading Bot via python main.py...\"\n\
        exec python main.py\n\
    else\n\
        echo \"Starting IBKR Trading Bot via gunicorn (multi-workers)...\"\n\
        exec \"${GUNICORN_CMD[@]}\" ${GUNICORN_EXTRA:-}\n\
    fi\n\
elif [ \"$1\" = \"test\" ]; then\n\
    echo \"Running tests...\"\n\
    exec pytest test/ -v\n\
elif [ \"$1\" = \"runner\" ]; then\n\
    echo \"Starting Strategy Runner...\"\n\
    shift\n\
    exec python -m strategy.strategy_runner \"$@\"\n\
elif [ \"$1\" = \"parallel\" ]; then\n\
    echo \"Starting gunicorn app in background...\"\n\
    \"${GUNICORN_CMD[@]}\" ${GUNICORN_EXTRA:-} &\n\
    MAIN_PID=$!\n\
    echo \"App (gunicorn) started with PID: $MAIN_PID\"\n\
    echo \"Starting Strategy Runner...\"\n\
    shift\n\
    python -m strategy.strategy_runner \"$@\" &\n\
    RUNNER_PID=$!\n\
    echo \"Strategy Runner started with PID: $RUNNER_PID\"\n\
    wait -n $MAIN_PID $RUNNER_PID\n\
    EXIT_CODE=$?\n\
    echo \"One process exited with code $EXIT_CODE, terminating all...\"\n\
    kill $MAIN_PID $RUNNER_PID 2>/dev/null || true\n\
    exit $EXIT_CODE\n\
else\n\
    echo \"Executing command: $@\"\n\
    exec \"$@\"\n\
fi\n' > /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
