-- Bootstrap schema for ibbot MariaDB persistence (legacy-compatible).

CREATE TABLE IF NOT EXISTS orders (
    id INT NOT NULL AUTO_INCREMENT,
    order_id VARCHAR(191) NOT NULL,
    command_id VARCHAR(191) NULL,
    client_order_id VARCHAR(191) NULL,
    ib_order_id VARCHAR(191) NULL,
    symbol VARCHAR(64) NOT NULL,
    action VARCHAR(16) NULL,
    side VARCHAR(16) NULL,
    quantity DOUBLE NULL,
    order_type VARCHAR(32) NULL,
    price DOUBLE NULL,
    limit_price DOUBLE NULL,
    stop_price DOUBLE NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'Submitted',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    executed_at DATETIME(6) NULL,
    order_timestamp DATETIME(6) NULL,
    fill_price DOUBLE NULL,
    filled_quantity DOUBLE NULL,
    remaining_quantity DOUBLE NULL,
    avg_fill_price DOUBLE NULL,
    order_source VARCHAR(64) NULL,
    strategy VARCHAR(128) NULL,
    strategy_name VARCHAR(128) NULL,
    rule_id VARCHAR(128) NULL,
    parent_order_id VARCHAR(128) NULL,
    exchange VARCHAR(64) NULL DEFAULT 'SMART',
    sec_type VARCHAR(32) NULL DEFAULT 'STK',
    notes TEXT NULL,
    commission DOUBLE NULL,
    pnl DOUBLE NULL,
    realized_pnl DOUBLE NULL,
    unrealized_pnl DOUBLE NULL,
    `trigger` VARCHAR(64) NULL,
    `trigger_type` VARCHAR(64) NULL,
    `trigger_status` VARCHAR(64) NULL,
    trigger_price DOUBLE NULL,
    account VARCHAR(64) NULL,
    source VARCHAR(64) NULL,
    raw_payload LONGTEXT NULL,
    is_deleted TINYINT(1) NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY ux_orders_order_id (order_id),
    UNIQUE KEY ux_orders_command_id (command_id),
    UNIQUE KEY ux_orders_client_order_id (client_order_id),
    UNIQUE KEY ux_orders_ib_order_id (ib_order_id),
    KEY ix_orders_symbol (symbol),
    KEY ix_orders_status_created (status, created_at),
    KEY ix_orders_strategy_legacy (strategy),
    KEY ix_orders_strategy (strategy_name),
    KEY ix_orders_parent (parent_order_id),
    KEY ix_orders_created (created_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS risk_rules (
    id INT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(64) NOT NULL UNIQUE,
    stop_loss_price DOUBLE NULL,
    take_profit_price DOUBLE NULL,
    max_loss_percent DOUBLE NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    max_time_span INT NULL,
    timestamp DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    auto_trailing TINYINT(1) NOT NULL DEFAULT 0,
    atr_params JSON NULL,
    rule_type VARCHAR(32) NOT NULL DEFAULT 'fixed',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX ix_risk_rules_symbol (symbol)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS strategies (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(128) NOT NULL UNIQUE,
    strategy_type VARCHAR(64) NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    api_endpoint VARCHAR(191) NULL,
    file_path VARCHAR(191) NULL,
    description TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX ix_strategies_name (name)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS historical_data (
    id INT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(64) NOT NULL,
    `date` DATE NOT NULL,
    `time` TIME NULL,
    `open` DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume BIGINT NOT NULL,
    `interval` VARCHAR(32) NOT NULL DEFAULT '1d',
    adj_close DOUBLE NULL,
    UNIQUE KEY ux_historical_data_symbol_interval (symbol, `date`, `time`, `interval`),
    INDEX ix_historical_data_symbol (symbol),
    INDEX ix_historical_data_interval (`interval`)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS config (
    id INT AUTO_INCREMENT PRIMARY KEY,
    config_key VARCHAR(128) NOT NULL UNIQUE,
    config_value TEXT NOT NULL,
    description TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sizing_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(64) NOT NULL,
    action VARCHAR(16) NOT NULL,
    suggested_quantity DOUBLE NOT NULL,
    current_price DOUBLE NULL,
    account_value DOUBLE NULL,
    position_size DOUBLE NULL,
    risk_percent DOUBLE NULL,
    sizing_method VARCHAR(64) NOT NULL,
    calculation_details JSON NULL,
    g_rules_checked JSON NULL,
    timestamp DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_sizing_history_symbol (symbol),
    INDEX ix_sizing_history_timestamp (timestamp)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS notification_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    channel VARCHAR(64) NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    message TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    retry_count INT NOT NULL DEFAULT 0,
    error_message TEXT NULL,
    sent_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_notification_history_status (status),
    INDEX ix_notification_history_created (created_at)
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
    INDEX ix_market_prices_symbol (symbol),
    INDEX ix_market_prices_observed_at (observed_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS risk_config_snapshots (
    id INT AUTO_INCREMENT PRIMARY KEY,
    service VARCHAR(128) NOT NULL,
    payload JSON NOT NULL,
    rules_count INT NOT NULL DEFAULT 0,
    updated_at_text VARCHAR(64) NULL,
    captured_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_risk_config_service (service),
    INDEX ix_risk_config_captured (captured_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS risk_rule_snapshots (
    id INT AUTO_INCREMENT PRIMARY KEY,
    service VARCHAR(128) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    payload JSON NOT NULL,
    updated_at_text VARCHAR(64) NULL,
    captured_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_risk_rule_service (service),
    INDEX ix_risk_rule_symbol (symbol),
    INDEX ix_risk_rule_captured (captured_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS market_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    event_type VARCHAR(128) NOT NULL,
    topic VARCHAR(256) NOT NULL,
    symbol VARCHAR(64) NULL,
    source VARCHAR(128) NULL,
    payload JSON NOT NULL,
    meta JSON NULL,
    ts_ns BIGINT NULL,
    recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    ingested_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX ix_market_events_event_type (event_type),
    INDEX ix_market_events_topic (topic),
    INDEX ix_market_events_symbol (symbol),
    INDEX ix_market_events_recorded_at (recorded_at),
    INDEX ix_market_events_ingested (ingested_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
