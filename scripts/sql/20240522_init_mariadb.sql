-- Bootstrap schema for ibbot MariaDB persistence.

CREATE TABLE IF NOT EXISTS orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL UNIQUE,
    symbol VARCHAR(64) NOT NULL,
    side VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL,
    exchange VARCHAR(64) NULL,
    quantity DOUBLE NOT NULL,
    price DOUBLE NOT NULL,
    order_type VARCHAR(32) NULL,
    `trigger` VARCHAR(32) NULL,
    trigger_type VARCHAR(32) NULL,
    trigger_status VARCHAR(32) NULL,
    trigger_price DOUBLE NULL,
    stop_price DOUBLE NULL,
    commission DOUBLE NULL,
    realized_pnl DOUBLE NULL,
    unrealized_pnl DOUBLE NULL,
    filled_quantity DOUBLE NULL,
    remaining_quantity DOUBLE NULL,
    avg_fill_price DOUBLE NULL,
    limit_price DOUBLE NULL,
    account VARCHAR(64) NULL,
    source VARCHAR(64) NULL,
    strategy VARCHAR(128) NULL,
    sec_type VARCHAR(32) NULL,
    notes TEXT NULL,
    order_timestamp DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    raw_payload JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX ix_orders_symbol(symbol),
    INDEX ix_orders_strategy(strategy)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS market_prices (
    id INT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(64) NOT NULL,
    bid DOUBLE NULL,
    ask DOUBLE NULL,
    bid_size DOUBLE NULL,
    ask_size DOUBLE NULL,
    source VARCHAR(64) NULL,
    observed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    extra JSON NULL,
    INDEX ix_market_prices_symbol(symbol),
    INDEX ix_market_prices_observed_at(observed_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS service_metrics (
    id INT AUTO_INCREMENT PRIMARY KEY,
    service VARCHAR(128) NOT NULL,
    metric VARCHAR(128) NOT NULL,
    value DOUBLE NOT NULL,
    status VARCHAR(32) NULL,
    host VARCHAR(128) NULL,
    details JSON NULL,
    recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_service_metrics_metric(metric),
    INDEX ix_service_metrics_service(service),
    INDEX ix_service_metrics_recorded(recorded_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS risk_config_snapshots (
    id INT AUTO_INCREMENT PRIMARY KEY,
    service VARCHAR(128) NOT NULL,
    payload JSON NOT NULL,
    rules_count INT NOT NULL DEFAULT 0,
    updated_at_text VARCHAR(64) NULL,
    captured_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_risk_config_service(service),
    INDEX ix_risk_config_captured(captured_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS risk_rule_snapshots (
    id INT AUTO_INCREMENT PRIMARY KEY,
    service VARCHAR(128) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    payload JSON NOT NULL,
    updated_at_text VARCHAR(64) NULL,
    captured_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_risk_rule_service(service),
    INDEX ix_risk_rule_symbol(symbol),
    INDEX ix_risk_rule_captured(captured_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS market_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(128) NOT NULL,
    topic VARCHAR(256) NOT NULL,
    symbol VARCHAR(64) NULL,
    source VARCHAR(128) NULL,
    payload JSON NOT NULL,
    meta JSON NULL,
    event_time DATETIME(6) NULL,
    ingested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_market_events_event_type(event_type),
    INDEX ix_market_events_topic(topic),
    INDEX ix_market_events_symbol(symbol),
    INDEX ix_market_events_ingested(ingested_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
