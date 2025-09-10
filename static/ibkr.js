/**
 * IBKR Connection Manager
 * 负责与Interactive Brokers的连接管理
 */
class IBKRConnectionManager {
    constructor(app) {
        this.app = app;
        this.connected = false;
        this.ws = null;
        this.setupEventListeners();
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
            this.app.dataManager.loadAccountInfo();
            this.app.loadOrders();
            this.app.loadPortfolio();
            this.app.loadRiskRules();
            this.app.loadStrategies();
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
                    this.app.dataManager.loadAccountInfo();
                    this.app.loadOrders();
                    this.app.loadPortfolio();
                    this.app.loadRiskRules();
                    this.app.loadStrategies();
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
            this.app.loadRiskRules();
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
     * 连接WebSocket
     */
    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        
        this.ws = new WebSocket(wsUrl);
        
        this.ws.onopen = () => {
            console.log('WebSocket connected');
        };
        
        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            console.log('WebSocket message received:', data);
            this.handleWebSocketMessage(data);
        };
        
        this.ws.onclose = () => {
            console.log('WebSocket disconnected');
            // Reconnect after 5 seconds
            setTimeout(() => this.connectWebSocket(), 5000);
        };
        
        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };
    }

    /**
     * 处理WebSocket消息
     */
    handleWebSocketMessage(data) {
        switch (data.type) {
            case 'connection_status':
                this.updateConnectionStatus(data.connected, data.account_type);
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
            console.warn('IBKR not connected, opening settings modal');
            const connectionModalEl = document.getElementById('connectionModal');
            if (connectionModalEl) {
                const settingsModal = new bootstrap.Modal(connectionModalEl);
                settingsModal.show();
            }
            return true; // 表示已处理
        }
        return false; // 表示未处理，需要其他错误处理
    }
}

// 导出模块
window.IBKRConnectionManager = IBKRConnectionManager;