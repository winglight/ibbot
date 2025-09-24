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
        this._lastConnectionStatus = null;
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
        this._lastConnectionStatus = {
            connected: !!payload.connected,
            account_type: payload.account_type || '',
            last_error: payload.last_error,
            received_at: Date.now(),
            source: payload.source || 'websocket'
        };
        if (!this._pendingStatusResolvers || this._pendingStatusResolvers.size === 0) {
            return;
        }
        const resolvers = Array.from(this._pendingStatusResolvers);
        this._pendingStatusResolvers.clear();
        resolvers.forEach(resolver => {
            try {
                resolver(this._lastConnectionStatus);
            } catch (error) {
                console.error('[ws] connection status resolver error', error);
            }
        });
    }

    sendMessage(payload) {
        if (!payload || typeof payload !== 'object') return false;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('[ws] unable to send message, socket not open', payload?.type);
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
        const now = new Date();
        const defaultStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
        const payload = {
            type: 'request_orders_snapshot',
            limit: Number.isFinite(options.limit) ? Number(options.limit) : 50,
            start_date: options.startDate || defaultStart,
        };
        if (options.force) payload.force = true;
        if (options.useCache === false) payload.use_cache = false;
        return this._sendThrottled('orders_snapshot', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
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
        const payload = { type: 'request_risk_rules' };
        if (options.force) payload.force = true;
        return this._sendThrottled('risk_rules', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
    }

    requestStrategies(options = {}) {
        const payload = { type: 'request_strategies' };
        if (options.force) payload.force = true;
        return this._sendThrottled('strategies_snapshot', payload, {
            force: Boolean(options.force),
            minInterval: options.minInterval ?? 15000,
        });
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
            this.updateConnectionStatus(true, accountType);
            
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
            this.updateConnectionStatus(false);
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

            this.updateConnectionStatus(data.connected, data.account_type);

            if (data.connected) {
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
            this.updateConnectionStatus(false);
        }
    }

    /**
     * 更新连接状态UI
     */
    updateConnectionStatus(connected, accountType = '') {
        this.connected = connected;
        this.app.connected = connected; // 同步到主应用
        
        const statusIndicator = document.getElementById('connectionStatus');
        const statusText = document.getElementById('connectionText');
        const connectBtn = document.getElementById('connectBtn');
        const disconnectBtn = document.getElementById('disconnectBtn');
        
        if (connected) {
            statusIndicator.className = 'status-indicator status-connected';
            statusText.textContent = `Connected (${accountType})`;
            connectBtn.disabled = true;
            disconnectBtn.disabled = false;
            if (connectBtn) connectBtn.style.display = 'none';
            if (disconnectBtn) disconnectBtn.style.display = 'inline-block';
            
            // 连接成功后加载需要IB连接的数据
            this.app.loadOrders();
            this.app.loadPortfolio();
            this.app.riskRulesManager.loadRiskRules();
            // 重新启动定期状态检查
            this.stopPeriodicStatusCheck();
            this.startPeriodicStatusCheck();
        } else {
            statusIndicator.className = 'status-indicator status-disconnected';
            statusText.textContent = 'Disconnected';
            connectBtn.disabled = false;
            disconnectBtn.disabled = true;
            if (connectBtn) connectBtn.style.display = 'inline-block';
            if (disconnectBtn) disconnectBtn.style.display = 'none';
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
                if (backendStatus) {
                    console.log('后端状态:');
                    console.log('  connected:', backendStatus.connected);
                    console.log('  account_type:', backendStatus.account_type);
                } else {
                    console.log('后端状态: 未收到最新状态，使用缓存');
                }
                const backendConnected = backendStatus ? !!backendStatus.connected : this.connected;
                const statusMatch = this.connected === backendConnected;
                console.log('状态一致性:', statusMatch ? '✓ 一致' : '✗ 不一致');
                if (!statusMatch && backendStatus) {
                    console.log('正在同步状态...');
                    this.updateConnectionStatus(backendStatus.connected, backendStatus.account_type);
                }
                return {
                    frontend: { connected: this.connected, app_connected: this.app.connected },
                    backend: backendStatus || { note: 'no_response' },
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
            console.log('后端状态:');
            console.log('  connected:', backendStatus.connected);
            console.log('  account_type:', backendStatus.account_type);

            const statusMatch = this.connected === backendStatus.connected;
            console.log('状态一致性:', statusMatch ? '✓ 一致' : '✗ 不一致');

            if (!statusMatch) {
                console.log('正在同步状态...');
                this.updateConnectionStatus(backendStatus.connected, backendStatus.account_type);
            }

            return {
                frontend: { connected: this.connected, app_connected: this.app.connected },
                backend: backendStatus,
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
            this.requestConnectionStatus({ force: true });
            this.requestRiskRules({ force: true });
            this.requestStrategies({ force: true });
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
                this.updateConnectionStatus(data.connected, data.account_type);
                // 同步连接状态到主应用
                this.connected = data.connected;
                this.app.connected = data.connected;
                this._notifyConnectionStatus(data);
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