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
            
            const data = await this.app.apiCall('/connection_status', {
                timeout: options.timeout ?? 4000,
                maxRetries: options.maxRetries ?? 0,
                useCache: false
            });
            
            this.updateConnectionStatus(data.connected, data.account_type);
            
            if (data.connected) {
                // Load data asynchronously to avoid blocking
                setTimeout(() => {
                    // this.app.dataManager.loadAccountInfo();
                    this.app.loadOrders();
                    this.app.loadPortfolio();
                    this.app.riskRulesManager.loadRiskRules();
                    this.app.strategiesManager.loadStrategies();
                }, 300);
            }
        } catch (error) {
            console.error('Failed to check connection status:', error);
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
                break;
            case 'order_placed':
                // 清除订单缓存以强制刷新
                if (this.app.ordersCache) {
                    delete this.app.ordersCache['orders_main'];
                }
                this.app.loadOrders();
                this.app.showAlert('success', `订单${data.order_id ? ' ' + data.order_id : ''} 下单成功`);
                break;
            case 'order_cancelled':
                // 清除订单缓存以强制刷新
                if (this.app.ordersCache) {
                    delete this.app.ordersCache['orders_main'];
                }
                this.app.loadOrders();
                this.app.showAlert('success', `订单 ${data.order_id} 已取消`);
                break;
            case 'all_orders_cancelled':
                // 清除订单缓存以强制刷新
                if (this.app.ordersCache) {
                    delete this.app.ordersCache['orders_main'];
                }
                this.app.loadOrders();
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
                this.app.loadOrders();
                break;
            case 'orders_snapshot':
                // 首次连接后的订单快照，直接渲染避免额外请求
                if (Array.isArray(data.orders) && this.app.orderManager && typeof this.app.orderManager.updateOrdersTableSmart === 'function') {
                    try {
                        const tbody = document.querySelector('#ordersTable tbody');
                        // 仅显示当天订单（本地时区），并按 submit_time 与 order_id 排序（与 _loadOrders 一致）
                        const now = new Date();
                        const startOfDayMs = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
                        const endOfDayMs = startOfDayMs + 24 * 60 * 60 * 1000;
                        const sorted = data.orders
                            .filter(o => {
                                if (!o || o.order_id === 0) return false;
                                if (!o.submit_time) return false;
                                const ts = new Date(o.submit_time).getTime();
                                return ts >= startOfDayMs && ts < endOfDayMs;
                            })
                            .sort((a, b) => {
                                const at = a.submit_time ? new Date(a.submit_time).getTime() : 0;
                                const bt = b.submit_time ? new Date(b.submit_time).getTime() : 0;
                                if (bt !== at) return bt - at;
                                const aid = Number(a.order_id) || 0;
                                const bid = Number(b.order_id) || 0;
                                return bid - aid;
                            });
                        this.app.orderManager.updateOrdersTableSmart(sorted, tbody);
                    } catch (e) {
                        console.error('Failed to render orders_snapshot:', e);
                    }
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
            case 'order_filled':
                // 订单成交通知，刷新订单列表
                console.log('Order filled notification received:', data);
                if (this.app.loadOrders) {
                    // 清除订单缓存以强制刷新
                    if (this.app.ordersCache) {
                        console.log('Clearing orders cache before refresh');
                        delete this.app.ordersCache['orders_main'];
                    }
                    console.log('Calling loadOrders after order_filled');
                    this.app.loadOrders();
                } else {
                    console.error('loadOrders method not available');
                }
                break;
            default:
                console.log('Unknown WebSocket message type:', data.type);
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