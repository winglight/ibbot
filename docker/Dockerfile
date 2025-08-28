# Multi-stage build for IBKR Trading Bot
FROM python:3.11-slim as base

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p logs data strategy

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose ports
EXPOSE 8000 18080

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
\
# Load .env as defaults only (do NOT override existing environment variables)\n\
if [ -f ".env" ]; then\n\
    while IFS="=" read -r key value; do\n\
        # Skip blank lines and comments\n\
        [ -z "$key" ] && continue\n\
        case "$key" in \#*) continue;; esac\n\
        # Preserve variables already provided by Docker/Compose\n\
        if [ -z "${!key+x}" ]; then\n\
            export "$key=$value"\n\
        fi\n\
    done < .env\n\
fi\n\
\
# Default to main app if no command specified\n\
if [ "$#" -eq 0 ]; then\n\
    echo "Starting IBKR Trading Bot main application..."\n\
    exec python main.py\n\
elif [ "$1" = "mock" ]; then\n\
    echo "Starting Mock IB Gateway..."\n\
    exec python mock_ib_gateway.py\n\
elif [ "$1" = "test" ]; then\n\
    echo "Running tests..."\n\
    exec pytest test/ -v\n\
elif [ "$1" = "runner" ]; then\n\
    echo "Starting Strategy Runner..."\n\
    shift  # Remove "runner" from arguments\n\
    exec python -m strategy.strategy_runner "$@"\n\
elif [ "$1" = "parallel" ]; then\n\
    echo "Starting main app in background..."\n\
    python main.py &\n\
    MAIN_PID=$!\n\
    echo "Main app started with PID: $MAIN_PID"\n\
    \n\
    echo "Starting Strategy Runner..."\n\
    shift  # Remove "parallel" from arguments\n\
    python -m strategy.strategy_runner "$@" &\n\
    RUNNER_PID=$!\n\
    echo "Strategy Runner started with PID: $RUNNER_PID"\n\
    \n\
    # Wait for either process to exit\n\
    wait -n $MAIN_PID $RUNNER_PID\n\
    EXIT_CODE=$?\n\
    echo "One process exited with code $EXIT_CODE, terminating all..."\n\
    kill $MAIN_PID $RUNNER_PID 2>/dev/null || true\n\
    exit $EXIT_CODE\n\
else\n\
    echo "Executing command: $@"\n\
    exec "$@"\n\
fi' > /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]