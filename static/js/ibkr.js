/**
 * IBKR Connection Manager
 * 负责与Interactive Brokers的连接管理
 */
class IBKRConnectionManager {
    constructor(app) {
        this.app = app;
        this.connected = false;
        this.ws = null;
        this.statusCheckInterval = null;
        this._wsThrottle = {};
        this._wsPendingMessages = [];
        this._lastConnectionStatus = null;
        this.connectionStatusMeta = null;
        this._pendingStatusResolvers = new Set();
        this.setupEventListeners();
        this.startPeriodicStatusCheck();
    }

    // 兼容旧代码：提供 isConnected 只读属性
    get isConnected() {
        return this.connected;
    }

    setupEventListeners() {
        // Connection buttons
        document.getElementById('connectBtn').addEventListener('click', () => this.connect());
        document.getElementById('disconnectBtn').addEventListener('click', () => this.disconnect());

        // Connection settings
        document.getElementById('connectionSettingsBtn').addEventListener('click', async () => {
            await this.ensureDefaultConnectionConfig();
            
            // Populate form with saved values
            const host = localStorage.getItem('conn_host') || '127.0.0.1';
            const port = localStorage.getItem('conn_port') || '4002';
            const clientId = localStorage.getItem('conn_clientId') || '1';
            const accountType = localStorage.getItem('conn_accountType') || 'paper';
            
            document.getElementById('host').value = host;
            document.getElementById('port').value = port;
            document.getElementById('clientId').value = clientId;
            document.getElementById('accountType').value = accountType;
            
            const modal = new bootstrap.Modal(document.getElementById('connectionModal'));
            modal.show();
        });
        
        // Save Connection Settings
        const saveConnBtn = document.getElementById('saveConnectionSettings');
        if (saveConnBtn) {
            saveConnBtn.addEventListener('click', () => {
                const host = document.getElementById('host').value;
                const port = document.getElementById('port').value;
                const clientId = document.getElementById('clientId').value;
                const accountType = document.getElementById('accountType').value;
                
                localStorage.setItem('conn_host', host);
                localStorage.setItem('conn_port', port);
                localStorage.setItem('conn_clientId', clientId);
                localStorage.setItem('conn_accountType', accountType);
                
                this.app.showAlert('success', 'Connection settings saved');
                const modalEl = document.getElementById('connectionModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();
            });
        }
    }

    _resetThrottleState() {
        if (!this._wsThrottle) return;
        Object.values(this._wsThrottle).forEach(state => {
            if (state && state.timer) {
                clearTimeout(state.timer);
            }
        });
        this._wsThrottle = {};
    }

    _notifyConnectionStatus(payload = {}) {
        const status = this.normalizeConnectionStatus(payload);
        const receivedAt = Date.now();
        status.receivedAt = Number.isFinite(status.receivedAt) ? status.receivedAt : receivedAt;
        this._lastConnectionStatus = {
            connected: status.connected,
            account_type: status.accountType,
            last_error: status.lastError,
            queue_depth: status.queueDepth,
            last_seen: status.lastSeen,
            redis: status.redis ? { ...status.redis } : undefined,
            stale: status.stale,
            received_at: receivedAt,
            receivedAt: status.receivedAt,
            source: status.source || 'websocket'
        };
        if (!this._pendingStatusResolvers || this._pendingStatusResolvers.size === 0) {
            return;
        }
        const resolvers = Array.from(this._pendingStatusResolvers);
        this._pendingStatusResolvers.clear();
        resolvers.forEach(resolver => {
            try {
                resolver({ ...this._lastConnectionStatus });
            } catch (error) {
                console.error('[ws] connection status resolver error', error);
            }
        });
    }

    sendMessage(payload) {
        if (!payload || typeof payload !== 'object') return false;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            return false;
        }
        try {
            this.ws.send(JSON.stringify(payload));
            return true;
        } catch (error) {
            console.error('[ws] failed to send message', error, payload);
            return false;
        }
    }

    _queuePendingMessage(key, payload, options = {}) {
        if (!payload || typeof payload !== 'object') return;
        if (!this._wsPendingMessages) {
            this._wsPendingMessages = [];
        }
        const minInterval = Math.max(0, Number(options.minInterval ?? 0));
        const entry = { key: key || payload?.type || `queued:${Date.now()}`, payload, options: { ...options, minInterval } };
        if (key) {
            const idx = this._wsPendingMessages.findIndex(item => item.key === key);
            if (idx !== -1) {
                this._wsPendingMessages[idx] = entry;
                return;
            }
        }
        this._wsPendingMessages.push(entry);
        if (this._wsPendingMessages.length > 50) {
            this._wsPendingMessages.shift();
        }
    }

    _flushPendingMessages() {
        if (!this._wsPendingMessages || this._wsPendingMessages.length === 0) {
            return;
        }
        const pending = this._wsPendingMessages.slice();
        this._wsPendingMessages.length = 0;
        pending.forEach(({ key, payload, options }) => {
            const retryOptions = { ...options, force: true };
            setTimeout(() => {
                this._sendThrottled(key, payload, retryOptions);
            }, 0);
        });
    }

    _sendThrottled(key, payload, options = {}) {
        if (!key) return 'failed';
        const minInterval = Math.max(0, Number(options.minInterval ?? 15000));
        if (!this._wsThrottle[key]) {
            this._wsThrottle[key] = { lastSent: 0, timer: null, queued: null };
        }
        const state = this._wsThrottle[key];
        const now = Date.now();
        const allowNow = options.force || (now - state.lastSent >= minInterval);
        if (allowNow) {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                state.queued = payload;
                this._queuePendingMessage(key, payload, { ...options, minInterval });
                return 'queued';
            }
            if (state.timer) {
                clearTimeout(state.timer);
                state.timer = null;
            }
            state.lastSent = now;
            state.queued = null;
            return this.sendMessage(payload) ? 'sent' : 'failed';
        }

        state.queued = payload;
        if (!state.timer) {
            const delay = Math.max(minInterval - (now - state.lastSent), 0) + 50;
            state.timer = setTimeout(() => {
                state.timer = null;
                if (state.queued) {
                    const queuedPayload = state.queued;
                    state.queued = null;
                    this._sendThrottled(key, queuedPayload, { force: true, minInterval });
                }
            }, delay);
        }
        return 'queued';
    }

    requestOrdersSnapshot(options = {}) {
        if (this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('orders', true);
        }
        const payload = {
            type: 'request_orders_snapshot',
            limit: Number.isFinite(options.limit) ? Number(options.limit) : 10,
        };
        if (options.startDate) {
            payload.start_date = options.startDate;
        }
        if (Number.isFinite(options.page)) {
            payload.page = Math.max(1, Number(options.page));
        }
        if (options.force) payload.force = true;
        if (options.useCache === false) payload.use_cache = false;
        const result = this._sendThrottled('orders_snapshot', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
        if (result === 'failed' && this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('orders', false);
        }
        return result;
    }

    requestLogsSnapshot(options = {}) {
        const payload = { type: 'request_logs_snapshot' };
        if (options.range) payload.range = options.range;
        if (options.module) payload.module = options.module;
        if (options.level) payload.level = options.level;
        if (options.direction) payload.direction = options.direction;
        if (Number.isFinite(options.page_size)) payload.page_size = Math.max(1, Number(options.page_size));
        if (Number.isFinite(options.lines)) payload.lines = Math.max(1, Number(options.lines));
        if (options.search) payload.search = options.search;
        if (options.filter_request_id) payload.filter_request_id = options.filter_request_id;
        if (options.requestId) payload.request_id = options.requestId;
        if (options.force) payload.force = true;
        if (options.cursor !== undefined && options.cursor !== null) {
            payload.cursor = Number.isFinite(Number(options.cursor)) ? Number(options.cursor) : options.cursor;
        }

        const minInterval = Number.isFinite(options.minInterval) ? Math.max(0, Number(options.minInterval)) : 4000;
        return this._sendThrottled('logs_snapshot', payload, {
            force: Boolean(options.force),
            minInterval,
        });
    }

    requestInfluxWriteRate(options = {}) {
        const payload = { type: 'request_influx_write_rate' };
        if (options.window) payload.window = options.window;
        if (options.service) payload.service = options.service;
        if (options.metric) payload.metric = options.metric;
        if (options.aggregate) payload.aggregate = options.aggregate;
        if (options.requestId) payload.request_id = options.requestId;
        if (options.force) payload.force = true;
        const minInterval = Number.isFinite(options.minInterval) ? Math.max(0, Number(options.minInterval)) : 10000;
        return this._sendThrottled('influx_write_rate', payload, {
            force: Boolean(options.force),
            minInterval,
        });
    }

    requestMonitoringQueues(options = {}) {
        const payload = { type: 'request_monitoring_queues' };
        if (Array.isArray(options.queues) && options.queues.length) {
            payload.queues = options.queues.map((value) => String(value));
        }
        if (Array.isArray(options.streams) && options.streams.length) {
            payload.streams = options.streams.map((value) => String(value));
        }
        if (options.requestId) payload.request_id = options.requestId;
        if (options.force) payload.force = true;
        const minInterval = Number.isFinite(options.minInterval) ? Math.max(0, Number(options.minInterval)) : 10000;
        return this._sendThrottled('monitoring_queues', payload, {
            force: Boolean(options.force),
            minInterval,
        });
    }

    requestServiceStatuses(options = {}) {
        const payload = { type: 'request_service_statuses' };
        if (options.force) payload.force = true;
        const minInterval = Number.isFinite(options.minInterval)
            ? Math.max(0, Number(options.minInterval))
            : 15000;
        return this._sendThrottled('service_statuses', payload, {
            force: Boolean(options.force),
            minInterval,
        });
    }

    requestDomAutomationStatus(options = {}) {
        const payload = { type: 'request_dom_automation_status' };
        if (options.force) payload.force = true;
        return this._sendThrottled('dom_automation_status', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
    }

    requestDomStats(options = {}) {
        const symbol = options.symbol || '';
        const payload = { type: 'request_dom_stats', symbol };
        if (options.lookbackSeconds != null) payload.lookback_seconds = options.lookbackSeconds;
        if (options.maxPoints != null) payload.max_points = options.maxPoints;
        if (options.start) payload.start = options.start;
        if (options.end) payload.end = options.end;
        if (options.force) payload.force = true;
        const key = `dom_stats:${symbol || 'unknown'}`;
        return this._sendThrottled(key, payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
    }

    requestPositionsSnapshot(options = {}) {
        if (this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('portfolio', true);
        }
        const payload = { type: 'request_positions_snapshot' };
        if (options.fast !== undefined) payload.fast = Boolean(options.fast);
        if (options.useCache !== undefined) payload.use_cache = Boolean(options.useCache);
        if (options.force) payload.force = true;
        const result = this._sendThrottled('positions_snapshot', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
        if (result === 'failed' && this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('portfolio', false);
        }
        return result;
    }

    requestStrategyPerformance(options = {}) {
        const name = options.name || '';
        if (!name) {
            console.warn('[ws] requestStrategyPerformance requires name');
            return false;
        }
        const payload = {
            type: 'request_strategy_performance',
            name,
            range: options.range || '1d',
        };
        if (options.start) payload.start = options.start;
        if (options.end) payload.end = options.end;
        if (options.force) payload.force = true;
        const key = `strategy_performance:${name}:${payload.range}`;
        return this._sendThrottled(key, payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
    }

    requestConnectionStatus(options = {}) {
        const payload = { type: 'request_connection_status' };
        if (options.force) payload.force = true;
        return this._sendThrottled('connection_status', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
    }

    requestRiskRules(options = {}) {
        if (this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('risk-rules', true);
        }
        const payload = { type: 'request_risk_rules' };
        if (options.force) payload.force = true;
        const result = this._sendThrottled('risk_rules', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
        if (result === 'failed' && this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('risk-rules', false);
        }
        return result;
    }

    requestSizingHistory(options = {}) {
        const payload = { type: 'request_sizing_history' };
        if (options.symbol) payload.symbol = options.symbol;
        if (options.since) payload.since = options.since;
        if (options.context) payload.context = options.context;
        if (options.force) payload.force = true;

        const limit = Number.isFinite(options.limit) ? Number(options.limit) : 50;
        payload.limit = limit;

        const keyParts = [options.symbol || '', options.since || '', limit];
        const key = `sizing_history:${keyParts.join(':')}`;
        return this._sendThrottled(key, payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 10000,
        });
    }

    requestStrategies(options = {}) {
        if (this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('strategies', true);
        }
        const payload = { type: 'request_strategies' };
        if (options.force) payload.force = true;
        const result = this._sendThrottled('strategies_snapshot', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
        if (result === 'failed' && this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('strategies', false);
        }
        return result;
    }

    requestStrategyStatus(options = {}) {
        const name = options.name || options.strategyName || '';
        if (!name) {
            console.warn('[ws] requestStrategyStatus requires name');
            return 'failed';
        }
        const payload = { type: 'request_strategy_status', name };
        if (options.force) payload.force = true;
        const key = `strategy_status:${name}`;
        return this._sendThrottled(key, payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
    }

    /**
     * 确保默认连接配置存在
     */
    async ensureDefaultConnectionConfig(options = {}) {
        try {
            const hasHost = !!localStorage.getItem('conn_host');
            const hasPort = !!localStorage.getItem('conn_port');
            const hasClient = !!localStorage.getItem('conn_clientId');
            const hasAcct = !!localStorage.getItem('conn_accountType');
            if (hasHost && hasPort && hasClient && hasAcct) return;
            
            const res = await this.app.apiCall('/connection_config', {
                timeout: options.timeout ?? 5000,
                maxRetries: options.maxRetries ?? 1
            });
            
            if (res) {
                if (!hasHost && res.host) localStorage.setItem('conn_host', res.host);
                if (!hasPort && res.port != null) localStorage.setItem('conn_port', String(res.port));
                if (!hasClient && res.client_id != null) localStorage.setItem('conn_clientId', String(res.client_id));
                if (!hasAcct) localStorage.setItem('conn_accountType', res.paper_trading ? 'paper' : 'live');
            }
        } catch (e) {
            // ignore errors, fallback remains
        }
    }

    /**
     * 连接到IBKR
     */
    async connect() {
        const config = {
            host: document.getElementById('host').value,
            port: parseInt(document.getElementById('port').value),
            client_id: parseInt(document.getElementById('clientId').value),
            paper_trading: (document.getElementById('accountType')?.value || 'paper') === 'paper'
        };
        
        try {
            await this.app.apiCall('/connect', {
                method: 'POST',
                body: JSON.stringify(config)
            });
            
            this.app.showAlert('success', 'Connected successfully');
            // Immediately update UI buttons
            const accountType = (document.getElementById('accountType')?.value || 'paper');
            this.updateConnectionStatus(true, accountType, { source: 'http', stale: false });
            
            // Close connection modal
            const modalElement = document.getElementById('connectionModal');
            const modal = bootstrap.Modal.getInstance(modalElement);
            if (modal) {
                modal.hide();
            }
            
            // Force cleanup modal state
            setTimeout(() => {
                // Remove all modal backdrops
                document.querySelectorAll('.modal-backdrop').forEach(backdrop => {
                    backdrop.remove();
                });
                
                // Clean body classes and styles
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
                
                // Ensure modal is hidden
                if (modalElement) {
                    modalElement.style.display = 'none';
                    modalElement.classList.remove('show');
                    modalElement.setAttribute('aria-hidden', 'true');
                    modalElement.removeAttribute('aria-modal');
                }
            }, 100);

            // Persist the latest selections
            const hostVal = document.getElementById('host')?.value;
            const portVal = document.getElementById('port')?.value;
            const clientIdVal = document.getElementById('clientId')?.value;
            const acctVal = document.getElementById('accountType')?.value || 'paper';
            if (hostVal) localStorage.setItem('conn_host', hostVal);
            if (portVal) localStorage.setItem('conn_port', portVal);
            if (clientIdVal) localStorage.setItem('conn_clientId', clientIdVal);
            if (acctVal) localStorage.setItem('conn_accountType', acctVal);
            
            // Load data after successful connection
            // this.app.dataManager.loadAccountInfo();
            this.app.loadOrders();
            this.app.loadPortfolio();
            this.app.riskRulesManager.loadRiskRules();
            this.app.strategiesManager.loadStrategies();
        } catch (error) {
            console.error('Connection failed:', error);
        }
    }

    /**
     * 断开IBKR连接
     */
    async disconnect() {
        try {
            await this.app.apiCall('/disconnect', { method: 'POST' });
            this.app.showAlert('info', 'Disconnected successfully');
            this.updateConnectionStatus(false, '', { source: 'http', stale: false });
            this.app.clearData();
            // 停止定期状态检查
            this.stopPeriodicStatusCheck();
        } catch (error) {
            console.error('Disconnection failed:', error);
        }
    }

    /**
     * 检查连接状态
     */
    async checkConnectionStatus(options = {}) {
        try {
            // 未登录则跳过，避免产生401以及触发登录弹窗循环
            const token = localStorage.getItem('access_token');
            if (!token) return;

            const wsReady = this.ws && this.ws.readyState === WebSocket.OPEN;
            if (wsReady) {
                const result = this.requestConnectionStatus({
                    force: Boolean(options.force),
                    minInterval: options.minInterval ?? 15000,
                });
                if (result !== 'failed') {
                    return result;
                }
            }

            const data = await this.app.apiCall('/connection_status', {
                timeout: options.timeout ?? 4000,
                maxRetries: options.maxRetries ?? 0,
                useCache: false
            });

            const status = this.normalizeConnectionStatus(data);
            this.updateConnectionStatus(status.connected, status.accountType, status);

            if (status.connected) {
                // Load data asynchronously to avoid blocking
                setTimeout(() => {
                    this.app.loadOrders();
                    this.app.loadPortfolio();
                    this.app.riskRulesManager.loadRiskRules();
                    this.app.strategiesManager.loadStrategies();
                }, 300);
            }
        } catch (error) {
            console.warn('Connection status check failed:', error?.message || error);
            this.updateConnectionStatus(false, '', { source: 'http', last_error: error?.message, stale: false });
        }
    }

    /**
     * 更新连接状态UI
     */
    _normalizeSource(source) {
        if (!source) return '';
        const text = String(source).trim();
        if (!text) return '';
        const lower = text.toLowerCase();
        if (['redis', 'http', 'websocket'].includes(lower)) {
            return lower;
        }
        return text;
    }

    _describeConnectionSource(source) {
        const normalized = this._normalizeSource(source);
        if (!normalized) return '';
        if (normalized === 'redis') return 'ib-command';
        if (normalized === 'http') return 'gateway';
        if (normalized === 'websocket') return 'websocket';
        return normalized;
    }

    normalizeConnectionStatus(payload = {}) {
        const result = {
            connected: false,
            accountType: '',
            source: '',
            stale: false,
            lastError: null,
            queueDepth: null,
            lastSeen: null,
            redis: null,
            receivedAt: null,
        };
        if (!payload || typeof payload !== 'object') {
            return result;
        }

        const safe = payload;
        const redisData = safe.redis && typeof safe.redis === 'object'
            ? { ...safe.redis }
            : null;

        if (typeof safe.connected === 'boolean') {
            result.connected = safe.connected;
        } else if (redisData && typeof redisData.connected === 'boolean') {
            result.connected = redisData.connected;
        }

        const accountType = safe.account_type ?? safe.accountType;
        if (typeof accountType === 'string' && accountType.trim()) {
            result.accountType = accountType;
        } else if (redisData && typeof redisData.account_type === 'string' && redisData.account_type.trim()) {
            result.accountType = redisData.account_type;
        }

        const source = safe.source || (redisData ? redisData.source : '');
        if (typeof source === 'string' && source) {
            result.source = source;
        }

        const receivedAtRaw = safe.received_at ?? safe.receivedAt;
        if (receivedAtRaw !== undefined && receivedAtRaw !== null) {
            const receivedAtNum = Number(receivedAtRaw);
            if (Number.isFinite(receivedAtNum)) {
                result.receivedAt = receivedAtNum;
            }
        }

        if (typeof safe.stale === 'boolean') {
            result.stale = safe.stale;
        } else if (redisData && typeof redisData.stale === 'boolean') {
            result.stale = redisData.stale;
        }

        const lastError = safe.last_error ?? safe.lastError;
        if (typeof lastError === 'string' && lastError.trim()) {
            result.lastError = lastError;
        } else if (redisData) {
            const redisError = redisData.last_error ?? redisData.lastError;
            if (typeof redisError === 'string' && redisError.trim()) {
                result.lastError = redisError;
            }
        }

        const queueDepth = safe.queue_depth ?? safe.queueDepth;
        if (queueDepth !== undefined && queueDepth !== null) {
            const depthNum = Number(queueDepth);
            if (Number.isFinite(depthNum)) {
                result.queueDepth = depthNum;
            }
        } else if (redisData) {
            const redisDepth = redisData.queue_depth ?? redisData.queueDepth;
            if (redisDepth !== undefined && redisDepth !== null) {
                const depthNum = Number(redisDepth);
                if (Number.isFinite(depthNum)) {
                    result.queueDepth = depthNum;
                }
            }
        }

        const lastSeen = safe.last_seen ?? safe.lastSeen;
        if (typeof lastSeen === 'string') {
            result.lastSeen = lastSeen;
        } else if (redisData) {
            const redisSeen = redisData.last_seen ?? redisData.lastSeen;
            if (typeof redisSeen === 'string') {
                result.lastSeen = redisSeen;
            }
        }

        if (redisData) {
            redisData.source = this._normalizeSource(redisData.source || 'redis');
            redisData.stale = Boolean(redisData.stale);
            if (!Number.isFinite(result.receivedAt)) {
                const redisReceived = redisData.received_at ?? redisData.receivedAt;
                if (redisReceived !== undefined && redisReceived !== null) {
                    const redisReceivedNum = Number(redisReceived);
                    if (Number.isFinite(redisReceivedNum)) {
                        result.receivedAt = redisReceivedNum;
                    }
                }
            }
            result.redis = redisData;
            if (!result.source) {
                result.source = redisData.source;
            }
        }

        result.source = this._normalizeSource(result.source);
        if (!result.source && typeof safe.connected === 'boolean') {
            result.source = 'http';
        }
        result.accountType = result.accountType || '';

        return result;
    }

    updateConnectionStatus(connected, accountType = '', meta = {}) {
        const payload = { ...(meta && typeof meta === 'object' ? meta : {}) };
        if (typeof connected === 'boolean') {
            payload.connected = connected;
        }
        if (accountType && !payload.account_type && !payload.accountType) {
            payload.account_type = accountType;
        }

        const normalized = this.normalizeConnectionStatus(payload);
        if (!normalized.accountType && accountType) {
            normalized.accountType = accountType;
        }
        normalized.connected = Boolean(connected);
        if (!Number.isFinite(normalized.receivedAt)) {
            normalized.receivedAt = Date.now();
        }

        this.connectionStatusMeta = normalized;
        this.connected = normalized.connected;
        this.app.connected = normalized.connected; // 同步到主应用

        const statusIndicator = document.getElementById('connectionStatus');
        const statusText = document.getElementById('connectionText');
        const connectBtn = document.getElementById('connectBtn');
        const disconnectBtn = document.getElementById('disconnectBtn');

        const displayAccountType = normalized.accountType || accountType || '';
        const sourceLabel = this._describeConnectionSource(normalized.source);
        const suffixParts = [];
        if (displayAccountType) suffixParts.push(displayAccountType);
        if (sourceLabel) suffixParts.push(sourceLabel);
        if (normalized.stale && normalized.source === 'redis') {
            suffixParts.push('stale');
        }
        const suffix = suffixParts.length ? ` (${suffixParts.join(' · ')})` : '';

        if (normalized.connected) {
            if (statusIndicator) statusIndicator.className = 'status-indicator status-connected';
            if (statusText) statusText.textContent = `Connected${suffix || (displayAccountType ? ` (${displayAccountType})` : '')}`;
            if (connectBtn) {
                connectBtn.disabled = true;
                connectBtn.style.display = 'none';
            }
            if (disconnectBtn) {
                disconnectBtn.disabled = false;
                disconnectBtn.style.display = 'inline-block';
            }

            // 连接成功后加载需要IB连接的数据
            this.app.loadOrders();
            this.app.loadPortfolio();
            this.app.riskRulesManager.loadRiskRules();
            // 重新启动定期状态检查
            this.stopPeriodicStatusCheck();
            this.startPeriodicStatusCheck();
        } else {
            if (statusIndicator) statusIndicator.className = 'status-indicator status-disconnected';
            const downParts = [];
            if (sourceLabel) downParts.push(sourceLabel);
            if (normalized.redis && normalized.redis.stale) {
                downParts.push('stale');
            }
            const disconnectText = downParts.length ? `Disconnected (${downParts.join(' · ')})` : 'Disconnected';
            if (statusText) statusText.textContent = disconnectText;
            if (connectBtn) {
                connectBtn.disabled = false;
                connectBtn.style.display = 'inline-block';
            }
            if (disconnectBtn) {
                disconnectBtn.disabled = true;
                disconnectBtn.style.display = 'none';
            }
        }
    }

    /**
     * 启动定期状态检查
     */
    startPeriodicStatusCheck() {
        // 每30秒检查一次连接状态
        this.statusCheckInterval = setInterval(() => {
            this.checkConnectionStatus({ timeout: 3000, maxRetries: 0 });
        }, 30000);
    }

    /**
     * 停止定期状态检查
     */
    stopPeriodicStatusCheck() {
        if (this.statusCheckInterval) {
            clearInterval(this.statusCheckInterval);
            this.statusCheckInterval = null;
        }
    }

    /**
     * 调试工具：检查前后端连接状态
     */
    async debugConnectionStatus() {
        console.log('=== IB连接状态调试 ===');
        console.log('前端状态:');
        console.log('  this.connected:', this.connected);
        console.log('  this.app.connected:', this.app.connected);

        const waitForStatus = () => new Promise(resolve => {
            let resolver;
            const timeout = setTimeout(() => {
                if (this._pendingStatusResolvers && this._pendingStatusResolvers.has(resolver)) {
                    this._pendingStatusResolvers.delete(resolver);
                }
                resolve(this._lastConnectionStatus);
            }, 1500);
            resolver = (payload) => {
                clearTimeout(timeout);
                resolve(payload || this._lastConnectionStatus);
            };
            if (this._pendingStatusResolvers) {
                this._pendingStatusResolvers.add(resolver);
            }
        });

        const wsReady = this.ws && this.ws.readyState === WebSocket.OPEN;
        if (wsReady) {
            const sendResult = this.requestConnectionStatus({ force: true });
            if (sendResult !== 'failed') {
                const backendStatus = await waitForStatus();
                const normalized = backendStatus ? this.normalizeConnectionStatus(backendStatus) : null;
                if (normalized) {
                    console.log('后端状态:');
                    console.log('  connected:', normalized.connected);
                    console.log('  account_type:', normalized.accountType);
                } else {
                    console.log('后端状态: 未收到最新状态，使用缓存');
                }
                const backendConnected = normalized ? !!normalized.connected : this.connected;
                const statusMatch = this.connected === backendConnected;
                console.log('状态一致性:', statusMatch ? '✓ 一致' : '✗ 不一致');
                if (!statusMatch && normalized) {
                    console.log('正在同步状态...');
                    this.updateConnectionStatus(normalized.connected, normalized.accountType, normalized);
                }
                return {
                    frontend: { connected: this.connected, app_connected: this.app.connected },
                    backend: normalized || backendStatus || { note: 'no_response' },
                    consistent: statusMatch
                };
            }
        }

        try {
            const backendStatus = await this.app.apiCall('/connection_status', {
                timeout: 3000,
                maxRetries: 0,
                useCache: false
            });
            const normalized = this.normalizeConnectionStatus(backendStatus);
            console.log('后端状态:');
            console.log('  connected:', normalized.connected);
            console.log('  account_type:', normalized.accountType);

            const statusMatch = this.connected === normalized.connected;
            console.log('状态一致性:', statusMatch ? '✓ 一致' : '✗ 不一致');

            if (!statusMatch) {
                console.log('正在同步状态...');
                this.updateConnectionStatus(normalized.connected, normalized.accountType, normalized);
            }

            return {
                frontend: { connected: this.connected, app_connected: this.app.connected },
                backend: normalized,
                consistent: statusMatch
            };
        } catch (error) {
            console.error('无法获取后端状态:', error);
            return {
                frontend: { connected: this.connected, app_connected: this.app.connected },
                backend: { error: error.message },
                consistent: false
            };
        }
    }

    /**
     * 连接WebSocket
     */
    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        
        console.log('[ws] preparing to connect', { wsUrl });
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            console.log('[ws] connected');
            this._resetThrottleState();
            this._flushPendingMessages();
            this.requestConnectionStatus({ force: true });
            this.requestRiskRules({ force: true });
            this.requestStrategies({ force: true });
            this.requestServiceStatuses({ force: true });
            if (this.app && this.app.currentStrategyDetails) {
                this.requestStrategyStatus({ name: this.app.currentStrategyDetails, force: true });
            }
        };
        
        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                console.log('[ws] message received', data?.type || 'unknown', data);
                this.handleWebSocketMessage(data);
            } catch (e) {
                console.error('[ws] message parse error', e, event?.data);
            }
        };
        
        this.ws.onclose = (ev) => {
            console.log('[ws] disconnected', { code: ev?.code, reason: ev?.reason });
            this._resetThrottleState();
            // Reconnect after 5 seconds
            setTimeout(() => this.connectWebSocket(), 5000);
        };
        
        this.ws.onerror = (error) => {
            console.error('[ws] error', error);
        };
    }

    /**
     * 处理WebSocket消息
     */
    handleWebSocketMessage(data) {
        switch (data.type) {
            case 'connection_status':
                const status = this.normalizeConnectionStatus(data);
                this.updateConnectionStatus(status.connected, status.accountType, status);
                this._notifyConnectionStatus(status);
                break;
            case 'order_placed':
                // 清除订单缓存以强制刷新
                if (this.app.ordersCache) {
                    delete this.app.ordersCache['orders_main'];
                }
                this.requestOrdersSnapshot({ force: true, reason: 'order_placed' });
                this.app.showAlert('success', `订单${data.order_id ? ' ' + data.order_id : ''} 下单成功`);
                break;
            case 'order_cancelled':
                // 清除订单缓存以强制刷新
                if (this.app.ordersCache) {
                    delete this.app.ordersCache['orders_main'];
                }
                this.requestOrdersSnapshot({ force: true, reason: 'order_cancelled' });
                this.app.showAlert('success', `订单 ${data.order_id} 已取消`);
                break;
            case 'all_orders_cancelled':
                // 清除订单缓存以强制刷新
                if (this.app.ordersCache) {
                    delete this.app.ordersCache['orders_main'];
                }
                this.requestOrdersSnapshot({ force: true, reason: 'all_orders_cancelled' });
                this.app.showAlert('success', `已取消 ${typeof data.count === 'number' ? data.count : ''} 个订单`);
                break;
            case 'account_update':
                if (data.account_info) {
                    this.app.dataManager.updateAccountInfoUI(data.account_info);
                }
                break;
            case 'portfolio_update':
                if (this.app?.dataManager && typeof this.app.dataManager.handlePositionsSnapshot === 'function') {
                    const handled = this.app.dataManager.handlePositionsSnapshot({
                        type: 'position_update',
                        positions: data.portfolio,
                        source: data.source || 'websocket',
                    });
                    if (handled) break;
                }
                if (data.portfolio) {
                    this.app.dataManager.updatePositionsUI(data.portfolio);
                }
                break;
            case 'order_update':
                // 清除订单缓存以强制刷新
                if (this.app.ordersCache) {
                    delete this.app.ordersCache['orders_main'];
                }
                this.requestOrdersSnapshot({ reason: 'order_update' });
                break;
            case 'orders_snapshot':
                if (this.app && typeof this.app.handleWebSocketPayload === 'function') {
                    this.app.handleWebSocketPayload(data);
                }
                break;
            case 'position_update':
                if (this.app?.dataManager && typeof this.app.dataManager.handlePositionsSnapshot === 'function') {
                    const handled = this.app.dataManager.handlePositionsSnapshot(data);
                    if (handled) break;
                }
                if (data.positions) {
                    this.app.dataManager.updatePositionsUI(data.positions);
                }
                break;
            case 'notification':
                if (data.message) {
                    const level = data.level || 'info';
                    this.app.showAlert(level, data.message);
                }
                break;
            case 'service_statuses':
                if (this.app && typeof this.app.handleWebSocketPayload === 'function') {
                    this.app.handleWebSocketPayload(data);
                }
                break;
            case 'realtime_data':
                // Forward realtime price updates to the monitor modal
                if (this.app && typeof this.app.updateMonitorRealtime === 'function') {
                    this.app.updateMonitorRealtime(data);
                }
                break;
            case 'order_filled':
                // 订单成交通知，刷新订单列表
                console.log('Order filled notification received:', data);
                if (this.app.loadOrders) {
                    // 清除订单缓存以强制刷新
                    if (this.app.ordersCache) {
                        console.log('Clearing orders cache before refresh');
                        delete this.app.ordersCache['orders_main'];
                    }
                    console.log('Requesting orders snapshot after order_filled');
                    this.requestOrdersSnapshot({ force: true, reason: 'order_filled' });
                } else {
                    console.error('loadOrders method not available');
                }
                break;
            default:
                if (this.app && typeof this.app.handleWebSocketPayload === 'function') {
                    const handled = this.app.handleWebSocketPayload(data);
                    if (!handled) {
                        console.log('Unknown WebSocket message type:', data.type);
                    }
                } else {
                    console.log('Unknown WebSocket message type:', data.type);
                }
        }
    }

    /**
     * 处理IBKR连接错误
     */
    handleConnectionError(error) {
        // 特殊处理IBKR连接错误 - 直接打开设置窗口而不显示错误提示
        if (error.message === 'Not connected to IBKR') {
            console.warn('IBKR not connected, opening settings modal (debounced)');
            const connectionModalEl = document.getElementById('connectionModal');
            if (connectionModalEl) {
                const alreadyOpen = connectionModalEl.classList.contains('show') || connectionModalEl.dataset.opening === 'true';
                if (!alreadyOpen) {
                    connectionModalEl.dataset.opening = 'true';
                    const settingsModal = bootstrap.Modal.getOrCreateInstance(connectionModalEl);
                    settingsModal.show();
                    const clearFlag = () => { delete connectionModalEl.dataset.opening; };
                    connectionModalEl.addEventListener('hidden.bs.modal', clearFlag, { once: true });
                    setTimeout(clearFlag, 3000);
                }
            }
            return true; // 表示已处理
        }
        return false; // 表示未处理，需要其他错误处理
    }
}

// 导出模块
window.IBKRConnectionManager = IBKRConnectionManager;

// 全局调试工具
window.debugIBConnection = function() {
    if (window.app && window.app.ibkrManager) {
        return window.app.ibkrManager.debugConnectionStatus();
    } else {
        console.error('IBKR Manager not available');
        return null;
    }
};

// 使用说明
console.log('IB连接调试工具已加载，使用 debugIBConnection() 检查连接状态');