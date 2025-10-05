CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT,
    order_id TEXT,
    client_order_id TEXT,
    ib_order_id TEXT,
    symbol TEXT NOT NULL,
    action TEXT,
    side TEXT,
    quantity REAL,
    order_type TEXT,
    price REAL,
    limit_price REAL,
    stop_price REAL,
    status TEXT DEFAULT 'Submitted',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT,
    order_timestamp TEXT,
    fill_price REAL,
    filled_quantity REAL,
    remaining_quantity REAL,
    avg_fill_price REAL,
    order_source TEXT,
    strategy TEXT,
    strategy_name TEXT,
    rule_id TEXT,
    parent_order_id TEXT,
    exchange TEXT DEFAULT 'SMART',
    sec_type TEXT DEFAULT 'STK',
    notes TEXT,
    commission REAL,
    pnl REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    trigger TEXT,
    trigger_type TEXT,
    trigger_status TEXT,
    trigger_price REAL,
    account TEXT,
    source TEXT,
    raw_payload TEXT,
    is_deleted INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX ix_orders_command_id ON orders(command_id);
CREATE UNIQUE INDEX ix_orders_client_order_id ON orders(client_order_id);
CREATE UNIQUE INDEX ix_orders_ib_order_id ON orders(ib_order_id);
CREATE INDEX ix_orders_symbol ON orders(symbol);
CREATE INDEX ix_orders_status_created ON orders(status, created_at);
CREATE INDEX ix_orders_strategy ON orders(strategy);
CREATE INDEX ix_orders_strategy_name ON orders(strategy_name);
CREATE INDEX ix_orders_parent ON orders(parent_order_id);
CREATE INDEX ix_orders_created ON orders(created_at);

CREATE TABLE risk_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT UNIQUE NOT NULL,
                    stop_loss_price REAL,
                    take_profit_price REAL,
                    max_loss_percent REAL,
                    enabled BOOLEAN DEFAULT 1,
                    max_time_span INTEGER,
                    timestamp TEXT NOT NULL,
                    auto_trailing BOOLEAN DEFAULT 0,
                    atr_params TEXT,
                    rule_type TEXT DEFAULT 'fixed'
                );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE strategies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    strategy_type TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    api_endpoint TEXT,
                    file_path TEXT,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
CREATE TABLE historical_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    time TEXT,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    interval TEXT NOT NULL,
                    adj_close REAL,
                    UNIQUE(symbol, date, time, interval)
                );
CREATE TABLE config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_key TEXT UNIQUE NOT NULL,
                    config_value TEXT NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
CREATE TABLE sizing_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    suggested_quantity INTEGER NOT NULL,
                    current_price REAL,
                    account_value REAL,
                    position_size REAL,
                    risk_percent REAL,
                    sizing_method TEXT NOT NULL,
                    calculation_details TEXT,
                    g_rules_checked TEXT,
                    timestamp TEXT NOT NULL
                );
CREATE TABLE notification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL
                );
CREATE TABLE market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    symbol TEXT,
                    source TEXT,
                    payload TEXT,
                    meta TEXT,
                    ts_ns INTEGER,
                    recorded_at TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
CREATE INDEX idx_market_events_topic_recorded
                ON market_events(topic, recorded_at DESC)
                ;
CREATE INDEX idx_market_events_type_ingested
                ON market_events(event_type, ingested_at DESC)
                ;
CREATE TABLE market_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    bid REAL,
                    ask REAL,
                    bid_size REAL,
                    ask_size REAL,
                    source TEXT,
                    observed_at TEXT NOT NULL,
                    extra TEXT
                );
CREATE INDEX idx_market_prices_symbol_observed
                ON market_prices(symbol, observed_at DESC)
                ;
CREATE TABLE risk_config_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    payload TEXT,
                    rules_count INTEGER DEFAULT 0,
                    updated_at_text TEXT,
                    captured_at TEXT NOT NULL
                );
CREATE INDEX idx_risk_config_snapshots_service_time
                ON risk_config_snapshots(service, captured_at DESC)
                ;
