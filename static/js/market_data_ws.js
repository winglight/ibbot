(function (globalScope) {
    const globalObject = globalScope || (typeof window !== 'undefined' ? window : globalThis);
    const root = globalObject.appModules = globalObject.appModules || {};
    const marketNamespace = root.marketData = root.marketData || {};
    const wsNamespace = marketNamespace.ws = marketNamespace.ws || {};

    const DEFAULT_INTERVAL_MS = 1000;
    const MIN_RECONNECT_DELAY_MS = 1000;
    const MAX_RECONNECT_DELAY_MS = 15000;

    class MarketDataWebSocketClient {
        constructor(options = {}) {
            this.endpoint = options.endpoint || this._buildEndpoint();
            this.intervalMs = Number(options.intervalMs) || DEFAULT_INTERVAL_MS;
            this.ws = null;
            this._connecting = null;
            this._shouldReconnect = true;
            this._reconnectDelay = MIN_RECONNECT_DELAY_MS;
            this._reconnectTimer = null;
            this._pendingMessages = [];
            this._desiredSymbols = new Set();
            this._activeSymbols = new Set();
            this._symbolSubscribers = new Map();
            this._globalSubscribers = new Set();
            this.latestQuotes = {};
        }

        static getInstance(options = {}) {
            if (!MarketDataWebSocketClient._instance) {
                MarketDataWebSocketClient._instance = new MarketDataWebSocketClient(options);
            }
            return MarketDataWebSocketClient._instance;
        }

        _buildEndpoint() {
            const locationRef = globalObject.location;
            if (!locationRef) {
                return 'ws://localhost/ws/market';
            }
            const protocol = locationRef.protocol === 'https:' ? 'wss://' : 'ws://';
            return `${protocol}${locationRef.host}/ws/market`;
        }

        _log(...args) {
            if (globalObject.console && globalObject.console.debug) {
                globalObject.console.debug('[market-ws]', ...args);
            }
        }

        async connect() {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                return this.ws;
            }
            if (this._connecting) {
                return this._connecting;
            }
            this._shouldReconnect = true;
            this._connecting = new Promise((resolve, reject) => {
                try {
                    const ws = new WebSocket(this.endpoint);
                    this.ws = ws;

                    ws.addEventListener('open', () => {
                        this._log('connected');
                        this._reconnectDelay = MIN_RECONNECT_DELAY_MS;
                        this._flushPending();
                        this._send({ type: 'set_interval', interval_ms: this.intervalMs });
                        if (this._desiredSymbols.size > 0) {
                            this._send({
                                type: 'subscribe',
                                symbols: Array.from(this._desiredSymbols),
                            });
                        }
                        resolve(ws);
                    });

                    ws.addEventListener('message', (event) => {
                        this._handleMessage(event);
                    });

                    ws.addEventListener('close', () => {
                        this._log('closed');
                        this._connecting = null;
                        this.ws = null;
                        if (this._shouldReconnect) {
                            this._scheduleReconnect();
                        }
                    });

                    ws.addEventListener('error', (error) => {
                        this._log('error', error);
                        if (this._connecting) {
                            reject(error);
                            this._connecting = null;
                        }
                        if (this.ws) {
                            try {
                                this.ws.close();
                            } catch (err) {
                                this._log('close failed after error', err);
                            }
                        }
                    });
                } catch (error) {
                    this._connecting = null;
                    reject(error);
                }
            });
            return this._connecting;
        }

        disconnect() {
            this._shouldReconnect = false;
            if (this._reconnectTimer) {
                clearTimeout(this._reconnectTimer);
                this._reconnectTimer = null;
            }
            if (this.ws) {
                try {
                    this.ws.close();
                } catch (err) {
                    this._log('disconnect close failed', err);
                }
            }
            this.ws = null;
            this._connecting = null;
        }

        _scheduleReconnect() {
            if (this._reconnectTimer || !this._shouldReconnect) {
                return;
            }
            const delay = Math.min(this._reconnectDelay, MAX_RECONNECT_DELAY_MS);
            this._reconnectDelay = Math.min(delay * 2, MAX_RECONNECT_DELAY_MS);
            this._reconnectTimer = globalObject.setTimeout(async () => {
                this._reconnectTimer = null;
                try {
                    await this.connect();
                } catch (err) {
                    this._log('reconnect failed', err);
                    this._scheduleReconnect();
                }
            }, delay);
        }

        _flushPending() {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                return;
            }
            const queue = this._pendingMessages.splice(0);
            queue.forEach((payload) => {
                try {
                    this.ws.send(JSON.stringify(payload));
                } catch (err) {
                    this._log('send failed', err);
                }
            });
        }

        _send(payload) {
            if (!payload || typeof payload !== 'object') {
                return;
            }
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                try {
                    this.ws.send(JSON.stringify(payload));
                } catch (err) {
                    this._log('send error', err);
                }
            } else {
                this._pendingMessages.push(payload);
                this.connect().catch((err) => {
                    this._log('connect error during send', err);
                });
            }
        }

        subscribe(symbols, handler, options = {}) {
            const list = Array.isArray(symbols) ? symbols : [symbols];
            const newSymbols = [];
            list.forEach((raw) => {
                if (typeof raw !== 'string') return;
                const normalized = raw.trim().toUpperCase();
                if (!normalized) return;
                if (!this._desiredSymbols.has(normalized)) {
                    newSymbols.push(normalized);
                }
                this._desiredSymbols.add(normalized);
                if (typeof handler === 'function') {
                    const bucket = this._symbolSubscribers.get(normalized) || new Set();
                    bucket.add(handler);
                    this._symbolSubscribers.set(normalized, bucket);
                }
            });

            if (options.intervalMs && Number(options.intervalMs) > 0) {
                this.setInterval(Number(options.intervalMs));
            }

            if (newSymbols.length > 0) {
                this._send({ type: 'subscribe', symbols: newSymbols });
            } else if (!this.ws) {
                this.connect().catch((err) => this._log('connect failed', err));
            }

            return () => this.unsubscribe(list, handler);
        }

        unsubscribe(symbols, handler) {
            const list = Array.isArray(symbols) ? symbols : [symbols];
            const removeSymbols = [];
            list.forEach((raw) => {
                if (typeof raw !== 'string') return;
                const normalized = raw.trim().toUpperCase();
                if (!normalized) return;
                if (typeof handler === 'function') {
                    const bucket = this._symbolSubscribers.get(normalized);
                    if (bucket) {
                        bucket.delete(handler);
                        if (bucket.size === 0) {
                            this._symbolSubscribers.delete(normalized);
                        }
                    }
                } else {
                    this._symbolSubscribers.delete(normalized);
                }
                if (!this._symbolSubscribers.has(normalized)) {
                    this._desiredSymbols.delete(normalized);
                    removeSymbols.push(normalized);
                }
            });

            if (removeSymbols.length > 0) {
                this._send({ type: 'unsubscribe', symbols: removeSymbols });
            }
        }

        setInterval(intervalMs) {
            const numeric = Number(intervalMs);
            if (!Number.isFinite(numeric) || numeric <= 0) {
                return;
            }
            const ms = Math.round(numeric);
            if (ms === this.intervalMs) {
                return;
            }
            this.intervalMs = ms;
            this._send({ type: 'set_interval', interval_ms: ms });
        }

        onQuotesUpdate(callback) {
            if (typeof callback === 'function') {
                this._globalSubscribers.add(callback);
                if (!this.ws) {
                    this.connect().catch((err) => this._log('connect failed', err));
                }
            }
            return () => this._globalSubscribers.delete(callback);
        }

        _handleMessage(event) {
            let payload = null;
            try {
                payload = JSON.parse(event.data);
            } catch (err) {
                this._log('parse error', err, event.data);
                return;
            }

            const type = (payload && payload.type) || '';
            switch (type) {
                case 'subscription_ack':
                    this._handleSubscriptionAck(payload);
                    break;
                case 'interval_ack':
                    this._log('interval ack', payload.interval_ms);
                    break;
                case 'market_quotes':
                    this.latestQuotes = payload.quotes || {};
                    this._notifySubscribers(payload);
                    break;
                case 'error':
                    this._log('server error', payload);
                    break;
                case 'pong':
                    break;
                default:
                    this._log('unhandled message', payload);
            }
        }

        _handleSubscriptionAck(payload) {
            const symbols = Array.isArray(payload.symbols) ? payload.symbols : [];
            if (payload.action === 'subscribe') {
                symbols.forEach((symbol) => this._activeSymbols.add(symbol));
            } else if (payload.action === 'unsubscribe') {
                symbols.forEach((symbol) => this._activeSymbols.delete(symbol));
            }
        }

        _notifySubscribers(payload) {
            const quotes = payload.quotes || {};
            if (this._globalSubscribers.size > 0) {
                this._globalSubscribers.forEach((callback) => {
                    try {
                        callback(payload);
                    } catch (err) {
                        this._log('global subscriber failed', err);
                    }
                });
            }
            Object.keys(quotes).forEach((symbol) => {
                const callbacks = this._symbolSubscribers.get(symbol);
                if (!callbacks || callbacks.size === 0) {
                    return;
                }
                callbacks.forEach((callback) => {
                    try {
                        callback(quotes[symbol], payload);
                    } catch (err) {
                        this._log('symbol subscriber error', err, symbol);
                    }
                });
            });
        }
    }

    wsNamespace.MarketDataWebSocketClient = MarketDataWebSocketClient;
    if (!globalObject.MarketDataWebSocketClient) {
        globalObject.MarketDataWebSocketClient = MarketDataWebSocketClient;
    }
})(typeof window !== 'undefined' ? window : globalThis);
