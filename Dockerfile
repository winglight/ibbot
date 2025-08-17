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
else\n\
    echo "Executing command: $@"\n\
    exec "$@"\n\
fi' > /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]