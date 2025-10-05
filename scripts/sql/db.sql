CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL,
                    status TEXT DEFAULT 'Submitted',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    executed_at TEXT,
                    fill_price REAL,
                    order_source TEXT DEFAULT 'manual',
                    strategy_name TEXT,
                    rule_id TEXT,
                    parent_order_id TEXT,
                    exchange TEXT DEFAULT 'SMART',
                    sec_type TEXT DEFAULT 'STK',
                    notes TEXT,
                    commission REAL,
                    pnl REAL,
                    is_deleted INTEGER DEFAULT 0
                );
CREATE INDEX idx_orders_strategy_source_created
                ON orders(strategy_name, order_source, is_deleted, created_at DESC)
                ;
CREATE INDEX idx_orders_source_state_strategy_created
                ON orders(order_source, is_deleted, strategy_name, created_at DESC)
                ;
CREATE INDEX idx_orders_strategy_activity
                ON orders(strategy_name, is_deleted, executed_at, created_at)
                ;
CREATE INDEX idx_orders_created_flag
                ON orders(is_deleted, created_at DESC)
                ;
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
CREATE TABLE risk_rule_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    payload TEXT,
                    updated_at_text TEXT,
                    captured_at TEXT NOT NULL
                );
CREATE INDEX idx_risk_rule_snapshots_service_symbol
                ON risk_rule_snapshots(service, symbol)
                ;
CREATE INDEX idx_risk_rule_snapshots_captured
                ON risk_rule_snapshots(captured_at DESC)
                ;
