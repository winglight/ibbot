class TradingApp {
    constructor() {
        this.ws = null;
        this.connected = false;
        this.charts = {}; // Store chart instances
        this.executionsPagination = {
            currentPage: 1,
            pageSize: 25,
            totalItems: 0,
            allExecutions: []
        };
        this.init();
    }

    async init() {
        try { 
            if (window.Chart && window.ChartAnnotation && window.Chart.register) { 
                window.Chart.register(window.ChartAnnotation); 
            }
            if (window.Chart && window.zoomPlugin && window.Chart.register) {
                window.Chart.register(window.zoomPlugin);
            }
        } catch (e) {}
        this.setupEventListeners();
        this.initSymbolCombobox();
        this.connectWebSocket();
        this.toggleAuthButtons(!!localStorage.getItem('access_token'));
        await this.ensureDefaultConnectionConfig();
        await this.tryAutoLogin();
        await this.checkConnectionStatus();
        await this.loadStrategies();
        await this.loadLogs();
    }

    // Show login modal
    showLoginModal() {
        const modalEl = document.getElementById('loginModal');
        if (!modalEl) return;
        const remember = localStorage.getItem('rememberLogin') === '1';
        const savedUser = localStorage.getItem('loginUsername') || '';
        const savedPass = localStorage.getItem('loginPassword') || '';
        if (remember) {
            const u = document.getElementById('loginUsername');
            const p = document.getElementById('loginPassword');
            const r = document.getElementById('rememberLogin');
            if (u) u.value = savedUser;
            if (p) p.value = savedPass;
            if (r) r.checked = true;
        }
        const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
        modal.show();
    }

    async handleLoginSubmit() {
        try {
            const username = (document.getElementById('loginUsername')?.value || '').trim();
            const password = (document.getElementById('loginPassword')?.value || '').trim();
            const remember = !!document.getElementById('rememberLogin')?.checked;
            if (remember) {
                localStorage.setItem('rememberLogin', '1');
                localStorage.setItem('loginUsername', username);
                localStorage.setItem('loginPassword', password);
            } else {
                localStorage.removeItem('rememberLogin');
                localStorage.removeItem('loginUsername');
                localStorage.removeItem('loginPassword');
            }
            if (!username || !password) { this.showAlert('danger', '请输入用户名和密码'); return; }
            const res = await this.apiCall('/login', {
                method: 'POST',
                body: JSON.stringify({ username, password })
            });
            if (res?.access_token) {
                localStorage.setItem('access_token', res.access_token);
                this.toggleAuthButtons(true);
                const modalEl = document.getElementById('loginModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();
                this.showAlert('success', '登录成功');
                this.checkConnectionStatus();
                this.loadStrategies();
                this.loadOrders();
                this.loadPortfolio();
                this.loadRiskRules();
                this.loadLogs();
                // this.loadAutoOrders();
            }
        } catch (e) {}
    }

    async tryAutoLogin() {
        const auto = localStorage.getItem('enableAutoLogin') === '1';
        const savedUser = localStorage.getItem('loginUsername');
        const savedPass = localStorage.getItem('loginPassword');
        if (!auto || !savedUser || !savedPass) return;
        try {
            const res = await this.apiCall('/login', {
                method: 'POST',
                body: JSON.stringify({ username: savedUser, password: savedPass })
            });
            if (res?.access_token) {
                localStorage.setItem('access_token', res.access_token);
                this.toggleAuthButtons(true);
                await this.ensureDefaultConnectionConfig();
                // Auto-connect using saved connection settings if available
                const host = localStorage.getItem('conn_host');
                const port = localStorage.getItem('conn_port');
                const clientId = localStorage.getItem('conn_clientId');
                const accountType = localStorage.getItem('conn_accountType') || 'paper';
                if (host && port && clientId) {
                    try {
                        await this.apiCall('/connect', {
                            method: 'POST',
                            body: JSON.stringify({
                                host,
                                port: parseInt(port),
                                client_id: parseInt(clientId),
                                paper_trading: accountType === 'paper'
                            })
                        });
                        // After successful connect, load data
                        this.loadAccountInfo();
                        this.loadOrders();
                        this.loadPortfolio();
                        this.loadRiskRules();
                        // this.loadAutoOrders();
                        this.loadStrategies();
                        this.loadLogs();
                    } catch (e) {
                        console.warn('Auto-connect failed:', e);
                    }
                }
            }
        } catch {}
    }

    toggleAuthButtons(isLoggedIn) {
        const loginBtn = document.getElementById('loginBtn');
        const logoutBtn = document.getElementById('logoutBtn');
        if (loginBtn) loginBtn.style.display = isLoggedIn ? 'none' : 'inline-block';
        if (logoutBtn) logoutBtn.style.display = isLoggedIn ? 'inline-block' : 'none';
    }

    logout() {
        localStorage.removeItem('access_token');
        this.toggleAuthButtons(false);
        this.showAlert('info', '已退出登录');
    }

    setupEventListeners() {
        // Wait for DOM to be ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.bindEventListeners());
        } else {
            this.bindEventListeners();
        }
    }

    bindEventListeners() {
        // Connection
        document.getElementById('connectBtn').addEventListener('click', () => this.connect());
        document.getElementById('disconnectBtn').addEventListener('click', () => this.disconnect());
        
        // Settings button
        document.getElementById('connectionSettingsBtn').addEventListener('click', async () => {
            await this.ensureDefaultConnectionConfig();
            // Prefill fields from localStorage
            const hostEl = document.getElementById('host');
            const portEl = document.getElementById('port');
            const clientIdEl = document.getElementById('clientId');
            const accountTypeEl = document.getElementById('accountType');
            const autoEl = document.getElementById('enableAutoLogin');
            if (hostEl) hostEl.value = localStorage.getItem('conn_host') || hostEl.value || '127.0.0.1';
            if (portEl) portEl.value = localStorage.getItem('conn_port') || portEl.value || '4002';
            if (clientIdEl) clientIdEl.value = localStorage.getItem('conn_clientId') || clientIdEl.value || '1';
            if (accountTypeEl) accountTypeEl.value = localStorage.getItem('conn_accountType') || accountTypeEl.value || 'paper';
            if (autoEl) autoEl.checked = localStorage.getItem('enableAutoLogin') === '1';
            const modal = new bootstrap.Modal(document.getElementById('connectionModal'));
            modal.show();
        });
        
        // Login events
        const loginBtn = document.getElementById('loginBtn');
        if (loginBtn) loginBtn.addEventListener('click', () => this.showLoginModal());
        const loginSubmitBtn = document.getElementById('loginSubmitBtn');
        if (loginSubmitBtn) loginSubmitBtn.addEventListener('click', () => this.handleLoginSubmit());
        const loginPasswordInput = document.getElementById('loginPassword');
        if (loginPasswordInput) loginPasswordInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') this.handleLoginSubmit(); });
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn) logoutBtn.addEventListener('click', () => this.logout());

        // Save Connection Settings
        const saveConnBtn = document.getElementById('saveConnectionSettings');
        if (saveConnBtn) {
            saveConnBtn.addEventListener('click', () => {
                const host = document.getElementById('host')?.value || '';
                const port = document.getElementById('port')?.value || '';
                const clientId = document.getElementById('clientId')?.value || '';
                const accountType = document.getElementById('accountType')?.value || '';
                const auto = !!document.getElementById('enableAutoLogin')?.checked;
                if (host) localStorage.setItem('conn_host', host);
                if (port) localStorage.setItem('conn_port', port);
                if (clientId) localStorage.setItem('conn_clientId', clientId);
                if (accountType) localStorage.setItem('conn_accountType', accountType);
                localStorage.setItem('enableAutoLogin', auto ? '1' : '0');
                this.showAlert('success', 'Connection settings saved');
                const modalEl = document.getElementById('connectionModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();
            });
        }

        // Strategy and Logs buttons
        const strategyBtn = document.getElementById('strategyBtn');
        if (strategyBtn) {
            strategyBtn.addEventListener('click', () => {
                this.loadStrategies();
                const panel = document.getElementById('strategiesTable');
                if (panel) {
                    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            });
        }
        const refreshLogsBtn = document.getElementById('refreshLogsBtn');
        if (refreshLogsBtn) refreshLogsBtn.addEventListener('click', () => this.loadLogs());

        const searchLogsBtn = document.getElementById('searchLogsBtn');
        if (searchLogsBtn) searchLogsBtn.addEventListener('click', () => this.loadLogs());

        const logModule = document.getElementById('logModule');
        if (logModule) logModule.addEventListener('change', () => this.loadLogs());

        const logRange = document.getElementById('logRange');
        if (logRange) logRange.addEventListener('change', () => this.loadLogs());

        const logStatsBtn = document.getElementById('logStatsBtn');
        if (logStatsBtn) logStatsBtn.addEventListener('click', () => this.showLogStats());
        const refreshStrategiesBtn = document.getElementById('refreshStrategiesBtn');
        if (refreshStrategiesBtn) {
            refreshStrategiesBtn.addEventListener('click', () => this.loadStrategies());
        }
        const addStrategyBtn = document.getElementById('addStrategyBtn');
        if (addStrategyBtn) {
            addStrategyBtn.addEventListener('click', async () => {
                // Open Add Strategy modal instead of prompts
                const nameInput = document.getElementById('strategyNameInput');
                const typeSelect = document.getElementById('strategyTypeSelect');
                const descInput = document.getElementById('strategyDescriptionInput');
                const useTemplateGroup = document.getElementById('useTemplateGroup');
                const useTemplateCheckbox = document.getElementById('strategyUseTemplate');
                const fileGroup = document.getElementById('strategyFileGroup');
                const fileSelect = document.getElementById('strategyFileSelect');
                const slTpGroup = document.getElementById('slTpGroup');
                if (!nameInput || !typeSelect || !descInput) return;
                // reset form
                nameInput.value = '';
                typeSelect.value = 'external';
                descInput.value = '';
                if (useTemplateCheckbox) useTemplateCheckbox.checked = true;
                if (useTemplateGroup) useTemplateGroup.style.display = 'none';
                if (fileGroup) fileGroup.style.display = 'none';
                if (slTpGroup) slTpGroup.style.display = 'none';
                if (fileSelect) fileSelect.innerHTML = '';
                const modal = new bootstrap.Modal(document.getElementById('addStrategyModal'));
                modal.show();
            });
        }

        // Add Strategy type toggle and submit
        const strategyTypeSelect = document.getElementById('strategyTypeSelect');
        if (strategyTypeSelect) {
            strategyTypeSelect.addEventListener('change', async (e) => {
                const useTemplateGroup = document.getElementById('useTemplateGroup');
                const fileGroup = document.getElementById('strategyFileGroup');
                const fileSelect = document.getElementById('strategyFileSelect');
                const slTpGroup = document.getElementById('slTpGroup');
                const isInternal = e.target.value === 'internal';
                if (useTemplateGroup) useTemplateGroup.style.display = isInternal ? '' : 'none';
                if (fileGroup) fileGroup.style.display = isInternal ? '' : 'none';
                if (slTpGroup) slTpGroup.style.display = isInternal ? '' : 'none';
                if (isInternal && fileSelect) {
                    try {
                        const data = await this.apiCall('/strategy/files');
                        if (data && data.success) {
                            const files = data.files || [];
                            let html = '<option value="new">[New File]</option>';
                            for (const f of files) { html += `<option value="${f}">${f}</option>`; }
                            fileSelect.innerHTML = html;
                        }
                    } catch (err) { console.warn('Failed to load strategy files', err); }
                }
            });
        }
        const createStrategyBtn = document.getElementById('createStrategyBtn');
        if (createStrategyBtn) {
            createStrategyBtn.addEventListener('click', async () => {
                const nameInput = document.getElementById('strategyNameInput');
                const nameRaw = (nameInput?.value || '').trim();
                const strategy_type = document.getElementById('strategyTypeSelect')?.value || 'external';
                const description = document.getElementById('strategyDescriptionInput')?.value || '';
                const use_template = strategy_type === 'internal' ? !!document.getElementById('strategyUseTemplate')?.checked : false;
                const selected_file = strategy_type === 'internal' ? (document.getElementById('strategyFileSelect')?.value || 'new') : undefined;
                const sl_enabled = strategy_type === 'internal' ? !!document.getElementById('slEnabledSwitch')?.checked : false;
                const tp_enabled = strategy_type === 'internal' ? !!document.getElementById('tpEnabledSwitch')?.checked : false;
                const sl_price_val = document.getElementById('slPriceInput')?.value;
                const tp_price_val = document.getElementById('tpPriceInput')?.value;
                const sl_price = sl_enabled && sl_price_val !== '' ? parseFloat(sl_price_val) : undefined;
                const tp_price = tp_enabled && tp_price_val !== '' ? parseFloat(tp_price_val) : undefined;
                const sl_type = document.getElementById('slTypeSelect')?.value || 'amount';
                const tp_type = document.getElementById('tpTypeSelect')?.value || 'amount';
                let name = nameRaw;
                if (strategy_type === 'internal' && (!name || name.length === 0)) {
                    const sel = selected_file || 'new';
                    if (sel && sel !== 'new') {
                        name = sel.replace(/\.py$/i, '');
                        if (nameInput) nameInput.value = name;
                    }
                }
                if (!name) { this.showAlert('danger', 'Please enter strategy name'); return; }
                try {
                    const payload = { name, strategy_type, description };
                    if (strategy_type === 'internal') {
                        Object.assign(payload, { use_template, selected_file, sl_enabled, tp_enabled, sl_price, tp_price, sl_type, tp_type });
                    }
                    await this.apiCall('/strategies', { method: 'POST', body: JSON.stringify(payload) });
                    this.showAlert('success', `Strategy ${name} created`);
                    const modalEl = document.getElementById('addStrategyModal');
                    const modal = bootstrap.Modal.getInstance(modalEl);
                    if (modal) modal.hide();
                    this.loadStrategies();
                } catch (e) {}
            });
        }
        
        // Strategy Settings button
        const strategySettingsBtn = document.getElementById('strategySettingsBtn');
        if (strategySettingsBtn) {
            strategySettingsBtn.addEventListener('click', async () => {
                await this.loadStrategySettings();
                const modal = new bootstrap.Modal(document.getElementById('strategySettingsModal'));
                modal.show();
            });
        }
        const saveStrategySettingsBtn = document.getElementById('saveStrategySettingsBtn');
        if (saveStrategySettingsBtn) {
            saveStrategySettingsBtn.addEventListener('click', async () => {
                await this.saveStrategySettings();
            });
        }
        const generateStrategyTokenBtn = document.getElementById('generateStrategyTokenBtn');
        if (generateStrategyTokenBtn) {
            generateStrategyTokenBtn.addEventListener('click', async () => {
                try {
                    const data = await this.apiCall('/strategy/settings/generate-token', { method: 'POST' });
                    if (data.success && data.token) {
                        document.getElementById('strategyAuthToken').value = data.token;
                        this.showAlert('success', 'Token generated');
                    } else {
                        this.showAlert('danger', 'Failed to generate token');
                    }
                } catch (err) {
                    this.showAlert('danger', 'Failed to generate token');
                }
            });
        }

        // Place order button
        document.getElementById('placeOrderBtn').addEventListener('click', () => {
            const lastQuantity = localStorage.getItem('lastOrderQuantity');
            if (lastQuantity) {
                document.getElementById('orderQuantity').value = lastQuantity;
            }
            const modal = new bootstrap.Modal(document.getElementById('orderModal'));
            modal.show();
        });
        
        // Add risk rule button
        document.getElementById('addRiskRuleBtn').addEventListener('click', () => {
            this.openRiskRuleModal();
        });
        
        // Submit order button
        document.getElementById('submitOrderBtn').addEventListener('click', () => {
            this.placeOrder();
        });
        
        // Submit risk rule button
        document.getElementById('submitRiskRuleBtn').addEventListener('click', () => {
            this.addRiskRule();
        });
        
        // Order form
        document.getElementById('orderForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.placeOrder();
        });
        
        // Order type change
        document.getElementById('orderType').addEventListener('change', (e) => {
            const priceInput = document.getElementById('orderPrice');
            priceInput.disabled = e.target.value === 'MARKET';
            if (e.target.value === 'MARKET') {
                priceInput.value = '';
            }
        });
        
        // Market data

        
        // Risk management
        document.getElementById('riskRuleForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.addRiskRule();
        });
        
        // Refresh buttons
        document.getElementById('refreshOrdersBtn').addEventListener('click', () => {
            console.log('Refresh orders button clicked');
            this.loadOrders();
        });
        document.getElementById('refreshPositionsBtn').addEventListener('click', () => {
            console.log('Refresh positions button clicked');
            this.loadPortfolio();
        });
        document.getElementById('refreshRiskRulesBtn').addEventListener('click', () => {
            console.log('Refresh risk rules button clicked');
            this.loadRiskRules();
        });
        // Auto Orders panel removed - no longer needed

        
        // Cancel all orders
        document.getElementById('cancelAllOrdersBtn').addEventListener('click', () => this.cancelAllOrders());
        
        console.log('All event listeners bound successfully');
    }

    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        
        this.ws = new WebSocket(wsUrl);
        
        this.ws.onopen = () => {
            console.log('WebSocket connected');
        };
        
        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
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

    handleWebSocketMessage(data) {
        switch (data.type) {
            case 'connection_status':
                this.updateConnectionStatus(data.connected, data.account_type);
                break;
            case 'order_placed':
            case 'order_cancelled':
            case 'all_orders_cancelled':
                this.loadOrders();
                this.showAlert('success', 'Order updated successfully');
                break;
            case 'account_update':
                if (data.account_info) {
                    this.updateAccountInfoUI(data.account_info);
                }
                break;
            case 'positions_update':
                if (data.positions) {
                    this.updatePositionsUI(data.positions);
                }
                break;
            default:
                console.log('Unknown WebSocket message:', data);
        }
    }

    async apiCall(endpoint, options = {}) {
        try {
            const headers = {
                'Content-Type': 'application/json',
                ...options.headers
            };
            
            // Add JWT token to Authorization header if available
            const token = localStorage.getItem('access_token');
            if (token) {
                headers['Authorization'] = `Bearer ${token}`;
            }
            
            const response = await fetch(`/api${endpoint}`, {
                headers,
                ...options
            });
            
            // Handle 401 Unauthorized - redirect to login
            if (response.status === 401) {
                localStorage.removeItem('access_token');
                this.toggleAuthButtons(false);
                this.showLoginModal();
                throw new Error('Authentication required');
            }
            
            const data = await response.json();
            const reqId = response.headers.get('x-request-id');
            if (reqId) data.request_id = reqId;
            
            if (!response.ok) {
                throw new Error(data.detail || 'API call failed');
            }
            
            return data;
        } catch (error) {
            if (error.message !== 'Authentication required') {
                this.showAlert('danger', error.message);
            }
            throw error;
        }
    }

    async ensureDefaultConnectionConfig() {
        try {
            const hasHost = !!localStorage.getItem('conn_host');
            const hasPort = !!localStorage.getItem('conn_port');
            const hasClient = !!localStorage.getItem('conn_clientId');
            const hasAcct = !!localStorage.getItem('conn_accountType');
            if (hasHost && hasPort && hasClient && hasAcct) return;
            const res = await this.apiCall('/connection_config');
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

    async connect() {
        const config = {
            host: document.getElementById('host').value,
            port: parseInt(document.getElementById('port').value),
            client_id: parseInt(document.getElementById('clientId').value),
            paper_trading: (document.getElementById('accountType')?.value || 'paper') === 'paper'
        };
        
        try {
            await this.apiCall('/connect', {
                method: 'POST',
                body: JSON.stringify(config)
            });
            
            this.showAlert('success', 'Connected successfully');
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
            
            this.loadAccountInfo();
            this.loadOrders();
            this.loadPortfolio();
            this.loadRiskRules();
            // this.loadAutoOrders();
            this.loadStrategies();
            // this.loadLogs();
        } catch (error) {
            console.error('Connection failed:', error);
        }
    }

    async disconnect() {
        try {
            await this.apiCall('/disconnect', { method: 'POST' });
            this.showAlert('info', 'Disconnected successfully');
            this.updateConnectionStatus(false);
            this.clearData();
        } catch (error) {
            console.error('Disconnection failed:', error);
        }
    }

    async checkConnectionStatus() {
        try {
            // 未登录则跳过，避免产生401以及触发登录弹窗循环
            const token = localStorage.getItem('access_token');
            if (!token) return;
            const data = await this.apiCall('/connection_status');
            this.updateConnectionStatus(data.connected, data.account_type);
            
            if (data.connected) {
                this.loadAccountInfo();
                this.loadOrders();
                this.loadPortfolio();
                this.loadRiskRules();
                this.loadAutoOrders();
                this.loadStrategies();
            }
        } catch (error) {
            console.error('Failed to check connection status:', error);
        }
    }

    updateConnectionStatus(connected, accountType = '') {
        this.connected = connected;
        
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
        } else {
            statusIndicator.className = 'status-indicator status-disconnected';
            statusText.textContent = 'Disconnected';
            connectBtn.disabled = false;
            disconnectBtn.disabled = true;
            if (connectBtn) connectBtn.style.display = 'inline-block';
            if (disconnectBtn) disconnectBtn.style.display = 'none';
        }
    }

    async loadAccountInfo() {
        try {
            const data = await this.apiCall('/account_info');
            const info = data.account_info;
            
            const accountInfoDiv = document.getElementById('accountInfo');
            accountInfoDiv.innerHTML = `
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>Account ID:</strong> ${info.account_id || 'N/A'}</p>
                        <p><strong>Account Type:</strong> ${info.account_type}</p>
                        <p><strong>Net Liquidation:</strong> $${this.formatNumber(info.net_liquidation)}</p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>Available Funds:</strong> $${this.formatNumber(info.available_funds)}</p>
                        <p><strong>Buying Power:</strong> $${this.formatNumber(info.buying_power)}</p>
                        <p class="${info.unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Unrealized P&L:</strong> $${this.formatNumber(info.unrealized_pnl)}
                        </p>
                    </div>
                </div>
            `;
        } catch (error) {
            console.error('Failed to load account info:', error);
        }
    }

    updateAccountInfoUI(info) {
        try {
            const accountInfoDiv = document.getElementById('accountInfo');
            if (!accountInfoDiv) return;
            accountInfoDiv.innerHTML = `
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>Account ID:</strong> ${info.account_id || 'N/A'}</p>
                        <p><strong>Account Type:</strong> ${info.account_type}</p>
                        <p><strong>Net Liquidation:</strong> $${this.formatNumber(info.net_liquidation)}</p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>Available Funds:</strong> $${this.formatNumber(info.available_funds)}</p>
                        <p><strong>Buying Power:</strong> $${this.formatNumber(info.buying_power)}</p>
                        <p class="${info.unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Unrealized P&L:</strong> $${this.formatNumber(info.unrealized_pnl)}
                        </p>
                    </div>
                </div>
            `;
        } catch (e) {
            console.error('updateAccountInfoUI failed', e);
        }
    }

    updatePositionsUI(positions) {
        try {
            const tbody = document.querySelector('#positionsTable tbody');
            if (!tbody) return;
            tbody.innerHTML = positions.map(pos => `
                <tr>
                    <td>${pos.symbol}</td>
                    <td>${pos.position}</td>
                    <td>$${this.formatNumber(pos.avg_cost)}</td>
                    <td>$${this.formatNumber(pos.current_price)}</td>
                    <td class="${pos.unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                        $${this.formatNumber(pos.unrealized_pnl)}
                    </td>
                    <td class="${pos.unrealized_pnl_percent >= 0 ? 'profit' : 'loss'}">
                        ${this.formatNumber(pos.unrealized_pnl_percent)}%
                    </td>
                    <td>
                        <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editPositionRiskRule('${pos.symbol}', ${pos.current_price}, ${pos.avg_cost}, ${pos.position})" title="Set TP/SL">
                            <i class="fas fa-shield-alt"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-success" onclick="app.openMonitorModal('${pos.symbol}', ${pos.position}, ${pos.current_price}, ${pos.avg_cost}, ${pos.unrealized_pnl}, ${pos.unrealized_pnl_percent})" title="Monitor">
                            <i class="fas fa-eye"></i>
                        </button>
                    </td>
                </tr>
            `).join('');
        } catch (e) {
            console.error('updatePositionsUI failed', e);
        }
    }

    async placeOrder() {
        const symbol = document.getElementById('orderSymbol').value.toUpperCase();
        
        // 自动检测证券类型
        const futureSymbols = ['MNQ', 'NQ', 'ES', 'MES', 'YM', 'MYM', 'RTY', 'M2K'];
        const secType = futureSymbols.includes(symbol) ? 'FUT' : 'STK';
        
        const quantityValue = parseInt(document.getElementById('orderQuantity').value);
        const order = {
            symbol: symbol,
            quantity: quantityValue,
            action: document.getElementById('orderAction').value,
            order_type: document.getElementById('orderType').value,
            price: document.getElementById('orderPrice').value ? parseFloat(document.getElementById('orderPrice').value) : null,
            sec_type: secType
        };
        
        if (!order.symbol || !order.quantity) {
            this.showAlert('warning', 'Please fill in all required fields');
            return;
        }
        
        if (order.order_type === 'LIMIT' && !order.price) {
            this.showAlert('warning', 'Price is required for limit orders');
            return;
        }
        
        try {
            await this.apiCall('/place_order', {
                method: 'POST',
                body: JSON.stringify(order)
            });
            
            this.showAlert('success', 'Order placed successfully');
            
            // Save the quantity value before resetting
            localStorage.setItem('lastOrderQuantity', quantityValue);
            
            document.getElementById('orderForm').reset();
            document.getElementById('orderPrice').disabled = true;
            
            // Restore the quantity value
            document.getElementById('orderQuantity').value = quantityValue;
            
            // Close modal
            const modal = bootstrap.Modal.getInstance(document.getElementById('orderModal'));
            if (modal) {
                modal.hide();
                // Force remove backdrop if it exists
                setTimeout(() => {
                    const backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) {
                        backdrop.remove();
                    }
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('overflow');
                    document.body.style.removeProperty('padding-right');
                }, 300);
            }
            
            // Refresh orders list
            this.loadOrders();
        } catch (error) {
            console.error('Failed to place order:', error);
        }
    }

    openRiskRuleModal(symbol = '', stopLoss = '', takeProfit = '', position = 0, currentPrice = 0, avgCost = 0, isEditMode = false) {
        document.getElementById('riskSymbol').value = symbol;
        document.getElementById('stopLossPrice').value = stopLoss;
        document.getElementById('takeProfitPrice').value = takeProfit;
        
        // Store position data for PnL calculations
        this.currentRiskRuleData = {
            symbol: symbol,
            position: position,
            currentPrice: currentPrice,
            avgCost: avgCost
        };
        
        // Always show PnL preview section
        const pnlSection = document.getElementById('pnlPreviewSection');
        pnlSection.style.display = 'block';
        this.updatePnLPreview();
        
        // Set button text based on mode
        const submitBtn = document.getElementById('submitRiskRuleBtn');
        if (isEditMode) {
            submitBtn.innerHTML = '<i class="fas fa-save"></i> Save Changes';
        } else {
            submitBtn.innerHTML = '<i class="fas fa-plus"></i> Add Risk Rule';
        }
        
        // Add event listeners for real-time updates
        this.addPnLUpdateListeners();
        
        const modal = new bootstrap.Modal(document.getElementById('riskRuleModal'));
        
        // Clean up listeners when modal is closed
        document.getElementById('riskRuleModal').addEventListener('hidden.bs.modal', () => {
            this.removePnLUpdateListeners();
        }, { once: true });
        
        modal.show();
    }

    updatePnLPreview() {
        if (!this.currentRiskRuleData) return;
        
        const { position, currentPrice, avgCost } = this.currentRiskRuleData;
        const stopLossPrice = parseFloat(document.getElementById('stopLossPrice').value) || 0;
        const takeProfitPrice = parseFloat(document.getElementById('takeProfitPrice').value) || 0;
        
        // Check if we have valid position data
        if (position === 0 || !avgCost) {
            // Show placeholder when no position data
            document.getElementById('stopLossPnL').textContent = 'N/A';
            document.getElementById('takeProfitPnL').textContent = 'N/A';
            
            // Set neutral colors
            document.getElementById('stopLossPnL').className = 'fw-bold text-muted';
            document.getElementById('takeProfitPnL').className = 'fw-bold text-muted';
            return;
        }
        
        // Calculate PnL based on actual position size
        const stopLossPnL = (stopLossPrice - avgCost) * position;
        const takeProfitPnL = (takeProfitPrice - avgCost) * position;
        
        // Update display
        document.getElementById('stopLossPnL').textContent = `$${stopLossPnL.toFixed(2)}`;
        document.getElementById('takeProfitPnL').textContent = `$${takeProfitPnL.toFixed(2)}`;
        
        // Update colors based on positive/negative values
        const stopLossElement = document.getElementById('stopLossPnL');
        const takeProfitElement = document.getElementById('takeProfitPnL');
        
        stopLossElement.className = stopLossPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
        takeProfitElement.className = takeProfitPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
    }

    addPnLUpdateListeners() {
        // Remove existing listeners to avoid duplicates
        this.removePnLUpdateListeners();
        
        const symbol = document.getElementById('riskRuleSymbol').value;
        
        // Add new listeners with price correction
        this.stopLossListener = (e) => {
            const correctedPrice = this.correctPriceStep(e.target.value, symbol);
            if (correctedPrice !== parseFloat(e.target.value)) {
                e.target.value = correctedPrice;
            }
            this.updatePnLPreview();
        };
        
        this.takeProfitListener = (e) => {
            const correctedPrice = this.correctPriceStep(e.target.value, symbol);
            if (correctedPrice !== parseFloat(e.target.value)) {
                e.target.value = correctedPrice;
            }
            this.updatePnLPreview();
        };
        
        document.getElementById('stopLossPrice').addEventListener('input', this.stopLossListener);
        document.getElementById('takeProfitPrice').addEventListener('input', this.takeProfitListener);
    }

    removePnLUpdateListeners() {
        if (this.stopLossListener) {
            document.getElementById('stopLossPrice').removeEventListener('input', this.stopLossListener);
        }
        if (this.takeProfitListener) {
            document.getElementById('takeProfitPrice').removeEventListener('input', this.takeProfitListener);
        }
    }

    async editPositionRiskRule(symbol, currentPrice, avgCost, position = 0, presetStopLoss = '', presetTakeProfit = '') {
        try {
            // Get risk configuration from API
            const config = await this.apiCall('/risk-config');
            
            const stopLossRatio = config.stop_loss_ratio || 0.05;
            const takeProfitRatio = config.take_profit_ratio || 0.05;
            
            // Determine if this is a short position (negative position)
            const isShortPosition = position < 0;
            
            // Determine base price for SL/TP calculation
            let basePrice = avgCost; // Default to average cost
            
            // Apply leverage adjustment for leveraged assets
            let adjustedStopLossRatio = stopLossRatio;
            let adjustedTakeProfitRatio = takeProfitRatio;
            
            if (symbol === 'MNQ') {
                // For MNQ futures, typical leverage is 1:20
                // Divide the ratios by leverage to reflect actual risk
                const leverageRatio = 20;
                adjustedStopLossRatio = stopLossRatio / leverageRatio;
                adjustedTakeProfitRatio = takeProfitRatio / leverageRatio;
            }
            
            let stopLoss, takeProfit;
            
            // Use preset values if provided, otherwise calculate suggested values
            if (presetStopLoss !== '' && presetTakeProfit !== '') {
                stopLoss = presetStopLoss;
                takeProfit = presetTakeProfit;
            } else {
                // Calculate suggested values based on position direction
                if (isShortPosition) {
                    // For short positions: stop loss is above avg cost, take profit is below avg cost
                    stopLoss = (basePrice * (1 + adjustedStopLossRatio)).toFixed(2);
                    takeProfit = (basePrice * (1 - adjustedTakeProfitRatio)).toFixed(2);
                } else {
                    // For long positions: stop loss is below avg cost, take profit is above avg cost
                    stopLoss = (basePrice * (1 - adjustedStopLossRatio)).toFixed(2);
                    takeProfit = (basePrice * (1 + adjustedTakeProfitRatio)).toFixed(2);
                }
            }
            
            this.openRiskRuleModal(symbol, stopLoss, takeProfit, position, currentPrice, avgCost, true);
        } catch (error) {
            console.error('Failed to get risk config:', error);
            // Fallback to preset values or default calculated values
            let stopLoss, takeProfit;
            const isShortPosition = position < 0;
            
            if (presetStopLoss !== '' && presetTakeProfit !== '') {
                stopLoss = presetStopLoss;
                takeProfit = presetTakeProfit;
            } else {
                if (isShortPosition) {
                    // For short positions: stop loss above current price, take profit below
                    stopLoss = presetStopLoss || (currentPrice * 1.05).toFixed(2);
                    takeProfit = presetTakeProfit || (currentPrice * 0.95).toFixed(2);
                } else {
                    // For long positions: stop loss below current price, take profit above
                    stopLoss = presetStopLoss || (currentPrice * 0.95).toFixed(2);
                    takeProfit = presetTakeProfit || (currentPrice * 1.05).toFixed(2);
                }
            }
            
            this.openRiskRuleModal(symbol, stopLoss, takeProfit, position, currentPrice, avgCost, true);
        }
    }

    async openMonitorModal(symbol, positionQty = 0, currentPrice = 0, avgCost = 0, pnl = 0, pnlPercent = 0) {
        try {
            // 填充基础字段
            const symEl = document.getElementById('monitorSymbol');
            if (!symEl) return;
            symEl.textContent = symbol || '—';
            const qtyEl = document.getElementById('monitorPositionQty');
            if (qtyEl) qtyEl.textContent = (positionQty || 0).toString();
            const curEl = document.getElementById('monitorCurrentPrice');
            if (curEl) curEl.textContent = `$${this.formatNumber(currentPrice || 0)}`;
            const avgEl = document.getElementById('monitorAvgCost');
            if (avgEl) avgEl.textContent = `$${this.formatNumber(avgCost || 0)}`;
            const pnlEl = document.getElementById('monitorPnL');
            if (pnlEl) pnlEl.textContent = `$${this.formatNumber(pnl || 0)}`;
            const pnlPctEl = document.getElementById('monitorPnLPercent');
            if (pnlPctEl) pnlPctEl.textContent = `(${this.formatNumber(pnlPercent || 0)}%)`;
            const valueEl = document.getElementById('monitorPositionValue');
            if (valueEl) valueEl.textContent = `$${this.formatNumber((avgCost || 0) * (positionQty || 0))}`;

            // 保存当前监控上下文
            this.currentMonitor = {
                symbol,
                positionQty: Number(positionQty) || 0,
                currentPrice: Number(currentPrice) || 0,
                avgCost: Number(avgCost) || 0,
                pnl: Number(pnl) || 0,
                pnlPercent: Number(pnlPercent) || 0,
            };

            // 清理输入框
            const slInput = document.getElementById('monitorStopLoss');
            if (slInput) slInput.value = '';
            const tpInput = document.getElementById('monitorTakeProfit');
            if (tpInput) tpInput.value = '';

            // 预填充风险规则与开关状态
            try {
                let rule = (this.riskRules && this.riskRules[symbol]) ? this.riskRules[symbol] : null;
                if (!rule) {
                    const riskResp = await this.apiCall('/risk_rules');
                    this.riskRules = riskResp.rules || {};
                    rule = this.riskRules[symbol];
                }
                if (rule) {
                    if (slInput) slInput.value = rule.stop_loss_price ?? '';
                    if (tpInput) tpInput.value = rule.take_profit_price ?? '';
                    const toggle = document.getElementById('monitorRiskToggle');
                    if (toggle) toggle.checked = !!rule.enabled;
                } else {
                    const toggle = document.getElementById('monitorRiskToggle');
                    if (toggle) toggle.checked = false;
                }
            } catch (prefillErr) {
                console.warn('Prefill risk rule failed:', prefillErr);
                const toggle = document.getElementById('monitorRiskToggle');
                if (toggle) toggle.checked = false;
            }

            // 显示模态框
            const modalEl = document.getElementById('monitorModal');
            if (modalEl) {
                const modal = new bootstrap.Modal(modalEl);
                modal.show();

                // 绑定按钮与输入事件
                const refreshBtn = document.getElementById('refreshMonitorChartBtn');
                if (refreshBtn) {
                    refreshBtn.onclick = () => this.loadMonitorChart(symbol);
                }
                const realtimeBtn = document.getElementById('monitorRealtimeBtn');
                if (realtimeBtn) {
                    const setActive = (active) => {
                        realtimeBtn.classList.toggle('btn-success', active);
                        realtimeBtn.classList.toggle('btn-info', !active);
                        const icon = realtimeBtn.querySelector('i');
                        if (icon) {
                            icon.classList.toggle('fa-play', !active);
                            icon.classList.toggle('fa-pause', active);
                        }
                    };
                    // 停止可能已存在的定时器
                    if (this.monitorRealtimeTimer) {
                        clearInterval(this.monitorRealtimeTimer);
                        this.monitorRealtimeTimer = null;
                        setActive(false);
                    }
                    realtimeBtn.onclick = () => {
                        if (this.monitorRealtimeTimer) {
                            clearInterval(this.monitorRealtimeTimer);
                            this.monitorRealtimeTimer = null;
                            setActive(false);
                        } else {
                            setActive(true);
                            this.monitorRealtimeTimer = setInterval(() => this.updateMonitorRealtime(), 2000);
                            this.updateMonitorRealtime();
                        }
                    };
                }

                const slInput = document.getElementById('monitorStopLoss');
                if (slInput) {
                    slInput.oninput = (e) => {
                        const correctedPrice = this.correctPriceStep(e.target.value, symbol);
                        if (correctedPrice !== parseFloat(e.target.value)) {
                            e.target.value = correctedPrice;
                        }
                        this.updateMonitorStopLossLine();
                        this.updateExpectedPnL();
                    };
                }
                const tpInput = document.getElementById('monitorTakeProfit');
                if (tpInput) {
                    tpInput.oninput = (e) => {
                        const correctedPrice = this.correctPriceStep(e.target.value, symbol);
                        if (correctedPrice !== parseFloat(e.target.value)) {
                            e.target.value = correctedPrice;
                        }
                        this.updateMonitorTakeProfitLine();
                        this.updateExpectedPnL();
                    };
                }

                const toggle = document.getElementById('monitorRiskToggle');
                if (toggle) {
                    // 切换启用/禁用
                    toggle.onchange = async () => {
                        try {
                            await this.toggleRiskRule(symbol, toggle.checked);
                            if (this.riskRules && this.riskRules[symbol]) {
                                this.riskRules[symbol].enabled = toggle.checked;
                            }
                        } catch (e) {
                            console.error('toggle risk rule failed:', e);
                            // revert UI on failure
                            toggle.checked = !toggle.checked;
                        }
                    };
                }

                const saveBtn = document.getElementById('saveMonitorRiskBtn');
                if (saveBtn) {
                    saveBtn.onclick = async () => {
                        const sl = slInput?.value ? parseFloat(slInput.value) : null;
                        const tp = tpInput?.value ? parseFloat(tpInput.value) : null;

                        if (!sl && !tp) {
                            this.showAlert('warning', '请至少输入止损或止盈价');
                            return;
                        }

                        try {
                            await this.apiCall('/risk_rules', {
                                method: 'POST',
                                body: JSON.stringify({
                                    symbol: symbol,
                                    stop_loss_price: sl,
                                    take_profit_price: tp
                                })
                            });
                            // 更新本地缓存
                            if (!this.riskRules) this.riskRules = {};
                            this.riskRules[symbol] = Object.assign({}, this.riskRules[symbol] || {}, {
                                symbol,
                                stop_loss_price: sl,
                                take_profit_price: tp,
                            });
                            this.showAlert('success', '已保存风险规则');
                        } catch (err) {
                            console.error('保存风险规则失败:', err);
                            this.showAlert('danger', `保存失败: ${err.message || err}`);
                        }
                    };
                }

                // 关闭时清理实时定时器
                modalEl.addEventListener('hidden.bs.modal', () => {
                    if (this.monitorRealtimeTimer) {
                        clearInterval(this.monitorRealtimeTimer);
                        this.monitorRealtimeTimer = null;
                    }
                }, { once: true });

                // 初次载入图表
                this.loadMonitorChart(symbol);
                
                // 初始化预期PnL显示
                this.updateExpectedPnL();
            }
        } catch (e) {
            console.error('openMonitorModal failed:', e);
            this.showAlert('danger', `打开监控失败: ${e.message || e}`);
        }
    }

    async loadMonitorChart(symbol) {
        try {
            const chartContainer = document.getElementById('monitorChartContainer');
            if (!chartContainer) return;

            // Clear previous chart
            chartContainer.innerHTML = `<canvas id="monitorChart" width="800" height="400"></canvas>`;

            // Request chart data from API
            const intervalSelect = document.getElementById('monitorInterval');
            const durationSelect = document.getElementById('monitorDuration');
            const barSize = intervalSelect?.value || '1 min';
            const duration = durationSelect?.value || '1 D';
            const response = await this.apiCall('/chart_data', {
                method: 'POST',
                body: JSON.stringify({
                    symbol: symbol,
                    duration: duration,
                    bar_size: barSize,
                    data_type: 'TRADES'
                })
            });

            if (!response.data || response.data.length === 0) {
                chartContainer.innerHTML = '<div class="alert alert-warning">No chart data available</div>';
                return;
            }

            // Wait a bit for DOM to update
            setTimeout(() => {
                const ctx = document.getElementById('monitorChart')?.getContext('2d');
                if (!ctx) return;

                // Prepare data for Chart.js
                const labels = response.data.map(bar => {
                    // Handle different date formats from API
                    let date;
                    if (typeof bar.date === 'string') {
                        // Try parsing ISO format or timestamp
                        date = new Date(bar.date);
                        if (isNaN(date.getTime())) {
                            // Try parsing as timestamp in seconds
                            date = new Date(parseFloat(bar.date) * 1000);
                        }
                    } else if (typeof bar.date === 'number') {
                        // Handle timestamp (assume seconds, convert to milliseconds if needed)
                        date = new Date(bar.date > 1e10 ? bar.date : bar.date * 1000);
                    } else {
                        date = new Date();
                    }
                    
                    // Format based on interval
                    const interval = intervalSelect?.value || '5 mins';
                    if (interval.includes('day') || duration.includes('M') || duration.includes('Y')) {
                        return date.toLocaleDateString('en-US', { 
                            month: 'short', 
                            day: 'numeric'
                        });
                    } else if (interval.includes('hour') || duration === '1 W') {
                        return date.toLocaleString('en-US', { 
                            month: 'short',
                            day: 'numeric',
                            hour: '2-digit',
                            hour12: false 
                        });
                    } else {
                        return date.toLocaleTimeString('en-US', { 
                            hour: '2-digit', 
                            minute: '2-digit',
                            hour12: false 
                        });
                    }
                });
                const prices = response.data.map(bar => bar.close);

                // Get stop loss and take profit values for annotations
                const stopLossInput = document.getElementById('monitorStopLoss');
                const takeProfitInput = document.getElementById('monitorTakeProfit');
                const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;
                const takeProfitPrice = takeProfitInput?.value ? parseFloat(takeProfitInput.value) : null;

                // Get current price and average cost from monitor context
                const currentPrice = this.currentMonitor?.currentPrice || 0;
                const avgCost = this.currentMonitor?.avgCost || 0;

                // 获取标的最小价格单位（根据symbol类型确定）
                const getMinPriceStep = (symbol) => {
                    if (symbol.includes('NQ') || symbol.includes('MNQ')) {
                        return 0.25; // 纳斯达克期货最小单位
                    } else if (symbol.includes('ES') || symbol.includes('MES')) {
                        return 0.25; // 标普500期货最小单位
                    } else {
                        return 0.01; // 股票默认最小单位
                    }
                };
                const minStep = getMinPriceStep(symbol);

                // Prepare annotations
                const annotations = {};
                if (stopLossPrice) {
                    annotations.stopLoss = {
                        type: 'line',
                        yMin: stopLossPrice,
                        yMax: stopLossPrice,
                        borderColor: 'rgb(255, 99, 132)',
                        borderWidth: 2,
                        borderDash: [5, 5],
                        // 使线条可拖动
                        adjustScaleRange: false,
                        scaleID: 'y',
                        value: stopLossPrice,
                        endValue: stopLossPrice,
                        label: {
                            content: `Stop Loss: $${stopLossPrice.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(255, 99, 132, 0.8)',
                            color: 'white',
                            padding: 4
                        },
                        // 拖动配置
                        enter: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'ns-resize';
                        },
                        leave: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'default';
                        }
                    };
                }
                if (takeProfitPrice) {
                    annotations.takeProfit = {
                        type: 'line',
                        yMin: takeProfitPrice,
                        yMax: takeProfitPrice,
                        borderColor: 'rgb(75, 192, 192)',
                        borderWidth: 2,
                        borderDash: [5, 5],
                        // 使线条可拖动
                        adjustScaleRange: false,
                        scaleID: 'y',
                        value: takeProfitPrice,
                        endValue: takeProfitPrice,
                        label: {
                            content: `Take Profit: $${takeProfitPrice.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(75, 192, 192, 0.8)',
                            color: 'white',
                            padding: 4
                        },
                        // 拖动配置
                        enter: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'ns-resize';
                        },
                        leave: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'default';
                        }
                    };
                }

                // Add current price line
                if (currentPrice > 0) {
                    annotations.currentPrice = {
                        type: 'line',
                        yMin: currentPrice,
                        yMax: currentPrice,
                        borderColor: 'rgb(255, 206, 84)',
                        borderWidth: 2,
                        // 实线（无虚线配置）
                        label: {
                            content: `$${currentPrice.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(255, 206, 84, 0.8)',
                            color: 'black',
                            padding: 4
                        }
                    };
                }

                // Add average cost line
                if (avgCost > 0) {
                    annotations.avgCost = {
                        type: 'line',
                        yMin: avgCost,
                        yMax: avgCost,
                        borderColor: 'rgb(153, 102, 255)',
                        borderWidth: 2,
                        // 实线（无虚线配置）
                        label: {
                            content: `$${avgCost.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(153, 102, 255, 0.8)',
                            color: 'white',
                            padding: 4
                        }
                    };
                }

                // Destroy existing chart if exists
                if (this.monitorChart) {
                    this.monitorChart.destroy();
                }

                // Create new chart
                this.monitorChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: `${symbol} Price`,
                            data: prices,
                            borderColor: 'rgb(54, 162, 235)',
                            backgroundColor: 'rgba(54, 162, 235, 0.1)',
                            tension: 0.1,
                            pointRadius: 1,
                            pointHoverRadius: 3
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: {
                            intersect: false,
                            mode: 'index'
                        },

                        scales: {
                            x: {
                                display: true,
                                title: {
                                    display: true,
                                    text: 'Time'
                                },
                                // 根据数据量自动调整显示范围
                                min: labels.length > 50 ? labels[Math.max(0, labels.length - 50)] : undefined,
                                max: labels.length > 50 ? labels[labels.length - 1] : undefined
                            },
                            y: {
                                display: true,
                                title: {
                                    display: true,
                                    text: 'Price ($)'
                                },
                                beginAtZero: false
                            }
                        },
                        // 添加缩放和平移插件配置
                        plugins: {
                            title: {
                                display: true,
                                text: `${symbol} - Real-time Price Chart`
                            },
                            legend: {
                                display: false
                            },
                            annotation: {
                                annotations: annotations
                            },
                            zoom: {
                                limits: {
                                    x: {min: 'original', max: 'original'},
                                    y: {min: 'original', max: 'original'}
                                },
                                pan: {
                                    enabled: true,
                                    mode: 'x',
                                    modifierKey: null
                                },
                                zoom: {
                                    wheel: {
                                        enabled: true,
                                        modifierKey: null
                                    },
                                    pinch: {
                                        enabled: true
                                    },
                                    mode: 'x'
                                }
                            }
                        },
                        // 添加拖动事件监听器
                        onHover: (event, activeElements, chart) => {
                            // 检查是否悬停在可拖动的annotation线上（只有SL/TP线可拖动）
                            const annotations = chart.options.plugins.annotation.annotations;
                            if (annotations) {
                                let isOverDraggableLine = false;
                                Object.keys(annotations).forEach(key => {
                                    // 只有stopLoss和takeProfit线可以拖动
                                    if ((key === 'stopLoss' || key === 'takeProfit') && annotations[key].type === 'line') {
                                        const annotation = annotations[key];
                                        const canvasPosition = Chart.helpers.getRelativePosition(event, chart);
                                        const dataY = chart.scales.y.getValueForPixel(canvasPosition.y);
                                        const lineY = annotation.yMin || annotation.value;
                                        if (Math.abs(dataY - lineY) < (chart.scales.y.max - chart.scales.y.min) * 0.02) {
                                            isOverDraggableLine = true;
                                        }
                                    }
                                });
                                chart.canvas.style.cursor = isOverDraggableLine ? 'ns-resize' : 'default';
                            }
                        },
                        // 移除onClick事件，改用mousedown处理拖动开始
                    },
                    // 添加鼠标移动事件监听器
                    onResize: function(chart, size) {
                        // 图表大小改变时的处理
                    }
                });

                // 添加鼠标事件监听器实现正确的拖动逻辑
                const canvas = this.monitorChart.canvas;

                canvas.addEventListener('mousedown', (event) => {
                    // 处理可拖动annotation线的拖动开始（只有SL/TP线可拖动）
                    const rect = canvas.getBoundingClientRect();
                    const canvasPosition = {
                        x: event.clientX - rect.left,
                        y: event.clientY - rect.top
                    };
                    const dataY = this.monitorChart.scales.y.getValueForPixel(canvasPosition.y);
                    
                    const annotations = this.monitorChart.options.plugins.annotation.annotations;
                    if (annotations) {
                        Object.keys(annotations).forEach(key => {
                            // 只有stopLoss和takeProfit线可以拖动
                            if ((key === 'stopLoss' || key === 'takeProfit') && annotations[key].type === 'line') {
                                const annotation = annotations[key];
                                const lineY = annotation.yMin || annotation.value;
                                if (Math.abs(dataY - lineY) < (this.monitorChart.scales.y.max - this.monitorChart.scales.y.min) * 0.02) {
                                    // 开始拖动
                                    this.monitorChart._draggedAnnotation = key;
                                    this.monitorChart._isDragging = true;
                                    event.preventDefault(); // 防止默认行为
                                }
                            }
                        });
                    }
                });

                canvas.addEventListener('mousemove', (event) => {
                    if (this.monitorChart._isDragging && this.monitorChart._draggedAnnotation) {
                        const rect = canvas.getBoundingClientRect();
                        const canvasPosition = {
                            x: event.clientX - rect.left,
                            y: event.clientY - rect.top
                        };
                        const dataY = this.monitorChart.scales.y.getValueForPixel(canvasPosition.y);
                        
                        // 根据最小步长调整价格
                        const adjustedPrice = Math.round(dataY / minStep) * minStep;
                        
                        const annotationKey = this.monitorChart._draggedAnnotation;
                        const annotation = this.monitorChart.options.plugins.annotation.annotations[annotationKey];
                        
                        if (annotation) {
                            // 更新annotation位置
                            annotation.yMin = adjustedPrice;
                            annotation.yMax = adjustedPrice;
                            annotation.value = adjustedPrice;
                            annotation.endValue = adjustedPrice;
                            annotation.label.content = `$${adjustedPrice.toFixed(2)}`;
                            
                            // 同步更新输入框
                            if (annotationKey === 'stopLoss') {
                                const stopLossInput = document.getElementById('monitorStopLoss');
                                if (stopLossInput) {
                                    stopLossInput.value = adjustedPrice.toFixed(2);
                                }
                            } else if (annotationKey === 'takeProfit') {
                                const takeProfitInput = document.getElementById('monitorTakeProfit');
                                if (takeProfitInput) {
                                    takeProfitInput.value = adjustedPrice.toFixed(2);
                                }
                            }
                            
                            // 更新预期PnL
                            this.updateExpectedPnL();
                            
                            // 更新图表
                            this.monitorChart.update('none');
                        }
                    }
                });

                canvas.addEventListener('mouseup', () => {
                    if (this.monitorChart) {
                        this.monitorChart._isDragging = false;
                        this.monitorChart._draggedAnnotation = null;
                        // 拖动结束后更新预期PnL
                        this.updateExpectedPnL();
                    }
                });

                canvas.addEventListener('mouseleave', () => {
                    if (this.monitorChart) {
                        this.monitorChart._isDragging = false;
                        this.monitorChart._draggedAnnotation = null;
                        canvas.style.cursor = 'default';
                    }
                });
                     }, 100);

        } catch (error) {
            console.error('Failed to load monitor chart:', error);
            const chartContainer = document.getElementById('monitorChartContainer');
            if (chartContainer) {
                chartContainer.innerHTML = `<div class="alert alert-danger">Failed to load chart: ${error.message}</div>`;
            }
        }
    }

    updateMonitorStopLossLine() {
        if (!this.monitorChart) return;
        
        const stopLossInput = document.getElementById('monitorStopLoss');
        const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;

        // Update or remove stop loss annotation
        if (stopLossPrice && !isNaN(stopLossPrice)) {
            this.monitorChart.options.plugins.annotation.annotations.stopLoss = {
                type: 'line',
                yMin: stopLossPrice,
                yMax: stopLossPrice,
                borderColor: 'rgb(255, 99, 132)',
                borderWidth: 2,
                borderDash: [5, 5],
                label: {
                    content: `Stop Loss: $${stopLossPrice.toFixed(2)}`,
                    enabled: true,
                    position: 'start'
                }
            };
        } else {
            delete this.monitorChart.options.plugins.annotation.annotations.stopLoss;
        }
        
        this.monitorChart.update('none');
    }

    updateMonitorTakeProfitLine() {
        if (!this.monitorChart) return;
        
        const takeProfitInput = document.getElementById('monitorTakeProfit');
        const takeProfitPrice = takeProfitInput?.value ? parseFloat(takeProfitInput.value) : null;

        // Update or remove take profit annotation
        if (takeProfitPrice && !isNaN(takeProfitPrice)) {
            this.monitorChart.options.plugins.annotation.annotations.takeProfit = {
                type: 'line',
                yMin: takeProfitPrice,
                yMax: takeProfitPrice,
                borderColor: 'rgb(75, 192, 192)',
                borderWidth: 2,
                borderDash: [5, 5],
                label: {
                    content: `Take Profit: $${takeProfitPrice.toFixed(2)}`,
                    enabled: true,
                    position: 'start'
                }
            };
        } else {
            delete this.monitorChart.options.plugins.annotation.annotations.takeProfit;
        }
        
        this.monitorChart.update('none');
    }

    async updateMonitorRealtime() {
        try {
            if (!this.currentMonitor || !this.currentMonitor.symbol) return;
            const symbol = this.currentMonitor.symbol;

            const resp = await this.apiCall(`/realtime_data/${encodeURIComponent(symbol)}`);
            if (!resp || resp.success === false) {
                console.warn('Realtime API returned error', resp?.error || resp);
                return;
            }
            const data = resp.data || resp;
            let price = null;
            if (typeof data.last === 'number') price = data.last;
            else if (typeof data.price === 'number') price = data.price;
            else if (typeof data.close === 'number') price = data.close;
            if (price == null) return;

            const qty = Number(this.currentMonitor.positionQty || 0);
            const avgCost = Number(this.currentMonitor.avgCost || 0);

            const curEl = document.getElementById('monitorCurrentPrice');
            if (curEl) curEl.textContent = `$${this.formatNumber(price)}`;

            const valueEl = document.getElementById('monitorPositionValue');
            if (valueEl) valueEl.textContent = `$${this.formatNumber(Math.abs(qty) * price)}`;

            const pnl = (price - avgCost) * qty;
            const pnlEl = document.getElementById('monitorPnL');
            if (pnlEl) {
                pnlEl.textContent = `$${this.formatNumber(pnl)}`;
                pnlEl.classList.toggle('text-success', pnl >= 0);
                pnlEl.classList.toggle('text-danger', pnl < 0);
            }

            const denom = Math.max(1e-9, Math.abs(avgCost) * Math.max(1, Math.abs(qty)));
            const pnlPct = (pnl / denom) * 100;
            const pnlPctEl = document.getElementById('monitorPnLPercent');
            if (pnlPctEl) {
                pnlPctEl.textContent = `(${this.formatNumber(pnlPct)}%)`;
                pnlPctEl.classList.toggle('text-success', pnl >= 0);
                pnlPctEl.classList.toggle('text-danger', pnl < 0);
            }

            if (this.monitorChart && this.monitorChart.data && this.monitorChart.data.datasets && this.monitorChart.data.datasets.length > 0) {
                const labels = this.monitorChart.data.labels || [];
                const dataset = this.monitorChart.data.datasets[0];
                const nowLabel = new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                labels.push(nowLabel);
                dataset.data.push(price);
                const MAX_POINTS = 300;
                if (labels.length > MAX_POINTS) {
                    labels.shift();
                    dataset.data.shift();
                }
                this.monitorChart.update('none');
            }
        } catch (e) {
            console.warn('updateMonitorRealtime failed:', e);
        }
    }

    updateExpectedPnL() {
        try {
            if (!this.currentMonitor) return;
            
            const stopLossInput = document.getElementById('monitorStopLoss');
            const takeProfitInput = document.getElementById('monitorTakeProfit');
            const expectedSection = document.getElementById('expectedPnLSection');
            const expectedStopLossPnL = document.getElementById('expectedStopLossPnL');
            const expectedTakeProfitPnL = document.getElementById('expectedTakeProfitPnL');
            
            if (!expectedSection || !expectedStopLossPnL || !expectedTakeProfitPnL) return;
            
            const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;
            const takeProfitPrice = takeProfitInput?.value ? parseFloat(takeProfitInput.value) : null;
            const positionQty = this.currentMonitor.positionQty || 0;
            const avgCost = this.currentMonitor.avgCost || 0;
            
            // 只有当有持仓且设置了止损或止盈价格时才显示预期PnL
            if (positionQty !== 0 && (stopLossPrice || takeProfitPrice)) {
                expectedSection.style.display = 'block';
                
                // 计算止损预期PnL
                if (stopLossPrice && avgCost > 0) {
                    const stopLossPnL = (stopLossPrice - avgCost) * positionQty;
                    expectedStopLossPnL.textContent = `$${this.formatNumber(stopLossPnL)}`;
                    expectedStopLossPnL.className = stopLossPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
                } else {
                    expectedStopLossPnL.textContent = '$0.00';
                    expectedStopLossPnL.className = 'fw-bold text-muted';
                }
                
                // 计算止盈预期PnL
                if (takeProfitPrice && avgCost > 0) {
                    const takeProfitPnL = (takeProfitPrice - avgCost) * positionQty;
                    expectedTakeProfitPnL.textContent = `$${this.formatNumber(takeProfitPnL)}`;
                    expectedTakeProfitPnL.className = takeProfitPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
                } else {
                    expectedTakeProfitPnL.textContent = '$0.00';
                    expectedTakeProfitPnL.className = 'fw-bold text-muted';
                }
            } else {
                expectedSection.style.display = 'none';
            }
        } catch (err) {
            console.error('updateExpectedPnL failed:', err);
        }
    }

    displayChart(symbol, data) {
         // Show chart modal
          const modal = new bootstrap.Modal(document.getElementById('chartModal'));
          document.getElementById('chartSymbol').textContent = `${symbol} Price Chart`;
         
         const chartContainer = document.getElementById('chartContainer');
         if (!chartContainer) return;
         
         // Clear previous chart
         chartContainer.innerHTML = `<canvas id="priceChart" width="800" height="400"></canvas>`;
         
         modal.show();
         
         // Wait for modal to be shown before creating chart
         setTimeout(() => {
             const ctx = document.getElementById('priceChart').getContext('2d');
             
             // Prepare data for Chart.js
             const labels = data.map(bar => new Date(bar.date).toLocaleDateString());
             const prices = data.map(bar => bar.close);
             
             new Chart(ctx, {
                 type: 'line',
                 data: {
                     labels: labels,
                     datasets: [{
                         label: `${symbol} Price`,
                         data: prices,
                         borderColor: 'rgb(75, 192, 192)',
                         backgroundColor: 'rgba(75, 192, 192, 0.1)',
                         tension: 0.1
                     }]
                 },
                 options: {
                     responsive: true,
                     plugins: {
                         title: {
                             display: true,
                             text: `${symbol} Price Chart`
                         }
                     },
                     scales: {
                         y: {
                             beginAtZero: false
                         }
                     }
                 }
             });
         }, 300);
     }



    async addRiskRule() {
        const rule = {
            symbol: document.getElementById('riskSymbol').value.toUpperCase(),
            stop_loss_price: document.getElementById('stopLossPrice').value ? parseFloat(document.getElementById('stopLossPrice').value) : null,
            take_profit_price: document.getElementById('takeProfitPrice').value ? parseFloat(document.getElementById('takeProfitPrice').value) : null
        };
        
        if (!rule.symbol) {
            this.showAlert('warning', 'Please enter a symbol');
            return;
        }
        
        if (!rule.stop_loss_price && !rule.take_profit_price) {
            this.showAlert('warning', 'Please enter at least one price level');
            return;
        }
        
        try {
            await this.apiCall('/risk_rules', {
                method: 'POST',
                body: JSON.stringify(rule)
            });
            
            this.showAlert('success', 'Risk rule added successfully');
            document.getElementById('riskRuleForm').reset();
            this.loadRiskRules();
            
            // Close modal
            const modal = bootstrap.Modal.getInstance(document.getElementById('riskRuleModal'));
            if (modal) {
                modal.hide();
                // Force remove backdrop if it exists
                setTimeout(() => {
                    const backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) {
                        backdrop.remove();
                    }
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('overflow');
                    document.body.style.removeProperty('padding-right');
                }, 300);
            }
        } catch (error) {
            console.error('Failed to add risk rule:', error);
        }
    }

    async loadOrders() {
        console.log('loadOrders method called');
        try {
            console.log('Fetching orders from API...');
            const data = await this.apiCall('/orders');
            console.log('Orders data received:', data);
            
            // 按创建时间反序排列（最新的在前）
            if (data.orders && Array.isArray(data.orders)) {
                data.orders.sort((a, b) => {
                    const timeA = new Date(a.submit_time || 0).getTime();
                    const timeB = new Date(b.submit_time || 0).getTime();
                    return timeB - timeA; // 反序排列
                });
            }
            
            const tbody = document.querySelector('#ordersTable tbody');
            
            tbody.innerHTML = data.orders.map(order => {
                // 计算价格显示
                let priceDisplay = '';
                if (order.order_type === 'MKT') {
                    // 市价单显示成交价
                    priceDisplay = order.avg_fill_price && order.avg_fill_price > 0 ? 
                        `$${this.formatNumber(order.avg_fill_price)}` : 'Market';
                } else {
                    // 限价单显示设定价格
                    priceDisplay = order.limit_price ? `$${this.formatNumber(order.limit_price)}` : 'N/A';
                }
                
                // 特殊处理ID为20的risk rule触发订单状态
                let displayStatus = order.status;
                if (order.order_id === 20 && order.status === 'PendingSubmit') {
                    displayStatus = 'Filled';
                }
                
                // 判断是否可以编辑/取消
                const canEdit = order.status === 'Submitted' || order.status === 'PreSubmitted';
                const canCancel = order.status === 'Submitted' || order.status === 'PreSubmitted';
                
                // 来源显示逻辑
                let sourceDisplay = '—';
                if (order.order_source) {
                    if (order.order_source === 'manual' || order.order_source.startsWith('manual_') || order.order_source === '人工') {
                        // 手动单直接显示字段值
                        sourceDisplay = order.order_source;
                    } else if (order.order_source === 'sl') {
                        sourceDisplay = '<span class="badge bg-danger">止损</span>';
                    } else if (order.order_source === 'tp') {
                        sourceDisplay = '<span class="badge bg-success">止盈</span>';
                    } else if (order.order_source === 'strategy') {
                        sourceDisplay = '<span class="badge bg-info">策略</span>';
                    } else if (order.order_source === '机器人' || order.order_source === 'auto') {
                        sourceDisplay = '<span class="badge bg-secondary">自动</span>';
                    } else {
                        // 其他情况直接显示字段值
                        sourceDisplay = order.order_source;
                    }
                }
                
                return `
                    <tr>
                        <td>${order.order_id}</td>
                        <td>${order.symbol}</td>
                        <td>${order.action}</td>
                        <td>${order.quantity}</td>
                        <td>${priceDisplay}</td>
                        <td><span class="badge bg-${this.getStatusColor(displayStatus)}">${displayStatus}</span></td>
                        <td>${sourceDisplay}</td>
                        <td>
                            ${canEdit ? 
                                `<button class="btn btn-sm btn-primary me-1" onclick="app.editOrder(${order.order_id}, '${order.symbol}', '${order.action}', ${order.quantity}, '${order.order_type}', ${order.limit_price || 0})" title="Edit">
                                    <i class="fas fa-edit"></i>
                                </button>` : ''}
                            ${canCancel ? 
                                `<button class="btn btn-sm btn-danger" onclick="app.cancelOrder(${order.order_id})" title="Cancel">
                                    <i class="fas fa-times"></i>
                                </button>` : ''}
                        </td>
                    </tr>
                `;
            }).join('');
        } catch (error) {
            console.error('Failed to load orders:', error);
        }
    }

    async loadPortfolio() {
        try {
            const data = await this.apiCall('/positions');
            const tbody = document.querySelector('#positionsTable tbody');
            
            tbody.innerHTML = data.positions.map(pos => `
                <tr>
                    <td>${pos.symbol}</td>
                    <td>${pos.position}</td>
                    <td>$${this.formatNumber(pos.avg_cost)}</td>
                    <td>$${this.formatNumber(pos.current_price)}</td>
                    <td class="${pos.unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                        $${this.formatNumber(pos.unrealized_pnl)}
                    </td>
                    <td class="${pos.unrealized_pnl_percent >= 0 ? 'profit' : 'loss'}">
                        ${this.formatNumber(pos.unrealized_pnl_percent)}%
                    </td>
                    <td>
                        <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editPositionRiskRule('${pos.symbol}', ${pos.current_price}, ${pos.avg_cost}, ${pos.position})" title="Set TP/SL">
                            <i class="fas fa-shield-alt"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-success" onclick="app.openMonitorModal('${pos.symbol}', ${pos.position}, ${pos.current_price}, ${pos.avg_cost}, ${pos.unrealized_pnl}, ${pos.unrealized_pnl_percent})" title="Monitor">
                            <i class="fas fa-eye"></i>
                        </button>
                    </td>
                </tr>
            `).join('');
        } catch (error) {
            console.error('Failed to load portfolio:', error);
        }
    }

    async loadRiskRules() {
        try {
            const [riskData, portfolioData] = await Promise.all([
                this.apiCall('/risk_rules'),
                this.apiCall('/positions')
            ]);
            
            // Cache risk rules for quick access elsewhere (e.g., monitor modal)
            this.riskRules = riskData.rules || {};
            
            const tbody = document.querySelector('#riskRulesTable tbody');
            
            // Create a map of portfolio by symbol for quick lookup
            const portfolioMap = {};
            portfolioData.positions.forEach(pos => {
                portfolioMap[pos.symbol] = pos;
            });
            
            tbody.innerHTML = Object.values(riskData.rules).map(rule => {
                const portfolioItem = portfolioMap[rule.symbol];
                const currentPrice = portfolioItem ? portfolioItem.current_price : 0;
                const avgCost = portfolioItem ? portfolioItem.avg_cost : 0;
                const positionSize = portfolioItem ? portfolioItem.position : 0;
                
                return `
                <tr>
                    <td>${rule.symbol}</td>
                    <td>${rule.stop_loss_price ? '$' + this.formatNumber(rule.stop_loss_price) : 'N/A'}</td>
                    <td>${rule.take_profit_price ? '$' + this.formatNumber(rule.take_profit_price) : 'N/A'}</td>
                    <td><span class="badge bg-${rule.enabled ? 'success' : 'secondary'}">${rule.enabled ? 'Active' : 'Disabled'}</span></td>
                    <td>
                        <button class="btn btn-sm btn-outline-info me-1" onclick="app.showRiskRuleOrders('${rule.symbol}')" title="Details">
                            <i class="fas fa-list"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editPositionRiskRule('${rule.symbol}', ${currentPrice}, ${avgCost}, ${positionSize}, '${rule.stop_loss_price || ''}', '${rule.take_profit_price || ''}')" title="Edit">
                            <i class="fas fa-edit"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-${rule.enabled ? 'warning' : 'success'} me-1" onclick="app.toggleRiskRule('${rule.symbol}', ${!rule.enabled})" title="${rule.enabled ? 'Disable Rule' : 'Enable Rule'}">
                            <i class="fas fa-${rule.enabled ? 'pause' : 'play'}"></i>
                        </button>
                        <button class="btn btn-sm btn-danger" onclick="app.removeRiskRule('${rule.symbol}')" title="Delete">
                            <i class="fas fa-trash"></i>
                        </button>
                    </td>
                </tr>
                `;
            }).join('');
        } catch (error) {
            console.error('Failed to load risk rules:', error);
        }
    }



    async loadStrategySettings() {
        try {
            const data = await this.apiCall('/strategy/settings');
            const s = data.settings || {};
            const el1 = document.getElementById('strategyApiEndpoint');
            if (!el1) return;
            document.getElementById('strategyApiEndpoint').value = s.api_endpoint || '/api/strategy';
            document.getElementById('strategyApiMethod').value = s.api_method || 'POST';
            document.getElementById('strategyApiPayload').value = JSON.stringify(s.api_payload_template || {
                strategy: '{{strategy_name}}', symbol: '{{symbol}}', action: '{{action}}', qty: '{{quantity}}', type: '{{order_type}}', price: '{{price}}'
            }, null, 2);
            document.getElementById('strategyPath').value = s.strategy_path || 'strategy/';
            document.getElementById('enableStrategyAuth').checked = !!s.enable_auth;
            document.getElementById('strategyAuthToken').value = s.auth_token || '';
        } catch (error) {
            console.error('Failed to load strategy settings:', error);
        }
    }

    async saveStrategySettings() {
        try {
            const api_endpoint = document.getElementById('strategyApiEndpoint').value.trim() || '/api/strategy';
            const api_method = document.getElementById('strategyApiMethod').value || 'POST';
            const payloadText = document.getElementById('strategyApiPayload').value || '{}';
            let api_payload_template = {};
            try { api_payload_template = JSON.parse(payloadText); } catch (e) { this.showAlert('danger', 'Invalid JSON for payload template'); return; }
            const strategy_path = document.getElementById('strategyPath').value.trim() || 'strategy/';
            const enable_auth = document.getElementById('enableStrategyAuth').checked;
            const auth_token = document.getElementById('strategyAuthToken').value || null;
            await this.apiCall('/strategy/settings', {
                method: 'POST',
                body: JSON.stringify({ api_endpoint, api_method, api_payload_template, strategy_path, enable_auth, auth_token })
            });
            this.showAlert('success', 'Strategy settings saved');
            const modalEl = document.getElementById('strategySettingsModal');
            const modal = bootstrap.Modal.getInstance(modalEl);
            if (modal) modal.hide();
        } catch (error) {
            console.error('Failed to save strategy settings:', error);
            this.showAlert('danger', 'Failed to save strategy settings');
        }
    }

    async cancelAllOrders() {
        if (!confirm('Are you sure you want to cancel all orders?')) {
            return;
        }
        
        try {
            const result = await this.apiCall('/cancel_all_orders', { method: 'DELETE' });
            if (result.success) {
                const cancelledCount = result.cancelled_count || 0;
                if (cancelledCount > 0) {
                    this.showAlert('success', `Successfully cancelled ${cancelledCount} orders`);
                } else {
                    this.showAlert('info', 'No cancellable orders found');
                }
                this.loadOrders(); // 刷新订单列表
            } else {
                this.showAlert('danger', `Failed to cancel orders: ${result.error || 'Unknown error'}`);
            }
        } catch (error) {
            console.error('Failed to cancel all orders:', error);
            this.showAlert('danger', 'Error occurred while cancelling orders');
        }
    }

    async removeRiskRule(symbol) {
        if (!confirm(`Are you sure you want to remove the risk rule for ${symbol}?`)) {
            return;
        }
        
        try {
            await this.apiCall(`/risk_rules/${symbol}`, { method: 'DELETE' });
            this.showAlert('success', 'Risk rule removed successfully');
            this.loadRiskRules();
        } catch (error) {
            console.error('Failed to remove risk rule:', error);
        }
    }

    async toggleRiskRule(symbol, enabled) {
        try {
            await this.apiCall(`/risk_rules/${symbol}?enabled=${enabled}`, { method: 'PUT' });
            this.showAlert('success', `Risk rule for ${symbol} ${enabled ? 'enabled' : 'disabled'} successfully`);
            await this.loadRiskRules();
        } catch (error) {
            console.error('Failed to update risk rule:', error);
            this.showAlert('danger', `Failed to ${enabled ? 'enable' : 'disable'} risk rule`);
        }
    }

    async executeRiskRule(symbol, currentPrice, stopLossPrice, takeProfitPrice, positionSize) {
        if (!positionSize || positionSize === 0) {
            this.showAlert('No position found for ' + symbol, 'warning');
            return;
        }

        try {
            // Determine which rule to execute based on current price
            let orderType = 'LIMIT';
            let targetPrice = null;
            let action = positionSize > 0 ? 'SELL' : 'BUY';
            let ruleType = '';
            let slippage = 0;

            // Check stop loss condition
            if (stopLossPrice && ((positionSize > 0 && currentPrice <= stopLossPrice) || (positionSize < 0 && currentPrice >= stopLossPrice))) {
                targetPrice = stopLossPrice;
                ruleType = 'Stop Loss';
                // If price has already passed stop loss, use market order
                if ((positionSize > 0 && currentPrice < stopLossPrice) || (positionSize < 0 && currentPrice > stopLossPrice)) {
                    orderType = 'MARKET';
                    slippage = Math.abs(currentPrice - stopLossPrice) / stopLossPrice * 100;
                }
            }
            // Check take profit condition
            else if (takeProfitPrice && ((positionSize > 0 && currentPrice >= takeProfitPrice) || (positionSize < 0 && currentPrice <= takeProfitPrice))) {
                targetPrice = takeProfitPrice;
                ruleType = 'Take Profit';
                // If price has already passed take profit, use market order
                if ((positionSize > 0 && currentPrice > takeProfitPrice) || (positionSize < 0 && currentPrice < takeProfitPrice)) {
                    orderType = 'MARKET';
                    slippage = Math.abs(currentPrice - takeProfitPrice) / takeProfitPrice * 100;
                }
            }
            else {
                this.showAlert('No rule conditions met for execution', 'info');
                return;
            }

            // Confirm execution
            let confirmMessage = `Execute ${ruleType} for ${symbol}?\n`;
            confirmMessage += `Current Price: $${currentPrice.toFixed(2)}\n`;
            confirmMessage += `Target Price: $${targetPrice.toFixed(2)}\n`;
            confirmMessage += `Order Type: ${orderType}\n`;
            confirmMessage += `Quantity: ${Math.abs(positionSize)}\n`;
            if (slippage > 0) {
                confirmMessage += `Estimated Slippage: ${slippage.toFixed(4)}%\n`;
            }

            if (!confirm(confirmMessage)) {
                return;
            }

            // Prepare order data
            const orderData = {
                symbol: symbol,
                action: action,
                quantity: Math.abs(positionSize),
                order_type: orderType
            };

            if (orderType === 'LIMIT') {
                orderData.price = targetPrice;
            }

            // Place the order
            const response = await this.apiCall('/place_order', 'POST', orderData);
            
            let message = `${ruleType} order placed successfully for ${symbol}`;
            if (slippage > 0) {
                message += ` (Slippage: ${slippage.toFixed(4)}%)`;
            }
            
            this.showAlert(message, 'success');
            
            // Refresh data
            await Promise.all([
                this.loadPortfolio(),
                this.loadRiskRules()
            ]);

        } catch (error) {
            this.showAlert('Failed to execute risk rule: ' + error.message, 'danger');
        }
    }

    clearData() {
        document.getElementById('accountInfo').innerHTML = '<p class="text-muted">Connect to view account information</p>';
        document.querySelector('#ordersTable tbody').innerHTML = '';
        document.querySelector('#positionsTable tbody').innerHTML = '';
        document.querySelector('#autoOrdersTable tbody').innerHTML = '';
        document.querySelector('#riskRulesTable tbody').innerHTML = '';


    }

    getStatusColor(status) {
        switch (status.toLowerCase()) {
            case 'filled': return 'success';
            case 'cancelled': case 'canceled': return 'danger';
            case 'submitted': case 'presubmitted': return 'primary';
            case 'pendingsubmit': return 'warning';
            case 'pendingcancel': return 'info';
            case 'inactive': return 'secondary';
            case 'apiwaiting': return 'warning';
            case 'apipending': return 'warning';
            default: return 'secondary';
        }
    }

    formatNumber(num) {
        if (num === null || num === undefined) return 'N/A';
        return parseFloat(num).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    // 根据标的类型修正价格到允许的最小单位
    correctPriceStep(price, symbol) {
        if (!price || isNaN(price)) return price;
        
        const numPrice = parseFloat(price);
        
        // 根据标的类型确定最小单位
        let step = 0.01; // 默认股票最小单位
        
        if (symbol) {
            const upperSymbol = symbol.toUpperCase();
            // NQ/MNQ 期货最小单位是0.25
            if (upperSymbol.includes('NQ') || upperSymbol.includes('MNQ')) {
                step = 0.25;
            }
            // ES/MES 期货最小单位是0.25
            else if (upperSymbol.includes('ES') || upperSymbol.includes('MES')) {
                step = 0.25;
            }
            // YM/MYM 期货最小单位是1.0
            else if (upperSymbol.includes('YM') || upperSymbol.includes('MYM')) {
                step = 1.0;
            }
            // RTY/M2K 期货最小单位是0.1
            else if (upperSymbol.includes('RTY') || upperSymbol.includes('M2K')) {
                step = 0.1;
            }
        }
        
        // 修正价格到最近的有效单位
        const correctedPrice = Math.round(numPrice / step) * step;
        return parseFloat(correctedPrice.toFixed(step < 1 ? 2 : 0));
    }

    showAlert(type, message) {
        const alertContainer = document.getElementById('alertContainer');
        const alertId = 'alert-' + Date.now();
        
        const alertHtml = `
            <div id="${alertId}" class="alert alert-${type} alert-dismissible fade show" role="alert">
                ${message}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            </div>
        `;
        
        alertContainer.insertAdjacentHTML('beforeend', alertHtml);
        
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            const alert = document.getElementById(alertId);
            if (alert) {
                const bsAlert = new bootstrap.Alert(alert);
                bsAlert.close();
            }
        }, 5000);
    }

    // Symbol combobox functionality
    initSymbolCombobox() {
        this.setupSymbolInput('orderSymbol', 'orderSymbolDropdown');
    }

    setupSymbolInput(inputId, dropdownId) {
        const input = document.getElementById(inputId);
        const dropdown = document.getElementById(dropdownId);
        let searchTimeout;

        // Load historical symbols on focus
        input.addEventListener('focus', () => {
            this.showHistoricalSymbols(dropdownId);
            dropdown.classList.add('show');
        });

        // Hide dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!input.contains(e.target) && !dropdown.contains(e.target)) {
                dropdown.classList.remove('show');
            }
        });

        // Search symbols on input
        input.addEventListener('input', (e) => {
            const query = e.target.value.trim();
            
            clearTimeout(searchTimeout);
            
            if (query.length === 0) {
                this.showHistoricalSymbols(dropdownId);
                return;
            }

            if (query.length >= 1) {
                searchTimeout = setTimeout(() => {
                    this.searchSymbols(query, dropdownId);
                }, 300);
            }
        });

        // Handle keyboard navigation
        input.addEventListener('keydown', (e) => {
            const items = dropdown.querySelectorAll('.dropdown-item');
            const activeItem = dropdown.querySelector('.dropdown-item.active');
            let activeIndex = Array.from(items).indexOf(activeItem);

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeIndex = activeIndex < items.length - 1 ? activeIndex + 1 : 0;
                this.setActiveDropdownItem(items, activeIndex);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeIndex = activeIndex > 0 ? activeIndex - 1 : items.length - 1;
                this.setActiveDropdownItem(items, activeIndex);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (activeItem) {
                    activeItem.click();
                }
            } else if (e.key === 'Escape') {
                dropdown.classList.remove('show');
                input.blur();
            }
        });
    }

    setActiveDropdownItem(items, activeIndex) {
        items.forEach((item, index) => {
            item.classList.toggle('active', index === activeIndex);
        });
    }

    showHistoricalSymbols(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        const historicalSymbols = this.getHistoricalSymbols();
        
        let html = '';
        
        if (historicalSymbols.length > 0) {
            html += '<h6 class="dropdown-header">Recent Symbols</h6>';
            historicalSymbols.forEach(symbol => {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            });
            html += '<div class="dropdown-divider"></div>';
        }
        
        html += '<h6 class="dropdown-header">Popular Symbols</h6>';
        const popularSymbols = ['MNQ', 'NQ', 'ES', 'YM', 'AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA', 'AMZN'];
        popularSymbols.forEach(symbol => {
            if (!historicalSymbols.includes(symbol)) {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            }
        });
        
        dropdown.innerHTML = html;
        
        // Add click handlers
        dropdown.querySelectorAll('.dropdown-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const symbol = e.target.getAttribute('data-symbol');
                const inputId = dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol';
                document.getElementById(inputId).value = symbol;
                dropdown.classList.remove('show');
                this.addToHistoricalSymbols(symbol);
            });
        });
    }

    async searchSymbols(query, dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        
        try {
            // Show loading
            dropdown.innerHTML = '<div class="dropdown-item-text">Searching...</div>';
            dropdown.classList.add('show');
            
            // For now, we'll use a simple client-side filter of common symbols
            // In a real implementation, this would call an API
            const allSymbols = [
                'MNQ', 'NQ', 'ES', 'YM', 'RTY', 'CL', 'GC', 'SI',
                'AAPL', 'MSFT', 'GOOGL', 'GOOG', 'TSLA', 'NVDA', 'AMZN', 'META',
                'NFLX', 'AMD', 'INTC', 'CRM', 'ORCL', 'ADBE', 'PYPL', 'UBER',
                'SPOT', 'ZM', 'DOCU', 'SNOW', 'PLTR', 'COIN', 'RBLX', 'HOOD'
            ];
            
            const filteredSymbols = allSymbols.filter(symbol => 
                symbol.toLowerCase().includes(query.toLowerCase())
            );
            
            let html = '';
            
            if (filteredSymbols.length > 0) {
                html += '<h6 class="dropdown-header">Search Results</h6>';
                filteredSymbols.slice(0, 10).forEach(symbol => {
                    html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
                });
            } else {
                html += '<div class="dropdown-item-text text-muted">No symbols found</div>';
            }
            
            dropdown.innerHTML = html;
            
            // Add click handlers
            dropdown.querySelectorAll('.dropdown-item').forEach(item => {
                item.addEventListener('click', (e) => {
                    e.preventDefault();
                    const symbol = e.target.getAttribute('data-symbol');
                    const inputId = dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol';
                    document.getElementById(inputId).value = symbol;
                    dropdown.classList.remove('show');
                    this.addToHistoricalSymbols(symbol);
                });
            });
            
        } catch (error) {
            console.error('Error searching symbols:', error);
            dropdown.innerHTML = '<div class="dropdown-item-text text-danger">Search failed</div>';
        }
    }

    getHistoricalSymbols() {
        const stored = localStorage.getItem('historicalSymbols');
        return stored ? JSON.parse(stored) : [];
    }

    addToHistoricalSymbols(symbol) {
        let historical = this.getHistoricalSymbols();
        
        // Remove if already exists
        historical = historical.filter(s => s !== symbol);
        
        // Add to beginning
        historical.unshift(symbol);
        
        // Keep only last 10
        historical = historical.slice(0, 10);
        
        localStorage.setItem('historicalSymbols', JSON.stringify(historical));
    }

    async loadAutoOrders() {
        try {
            const data = await this.apiCall('/auto_orders');
            
            if (data.success) {
                this.displayAutoOrders(data.orders);
            } else {
                console.error('Failed to load auto orders:', data.message);
            }
        } catch (error) {
            console.error('Error loading auto orders:', error);
        }
    }

    displayAutoOrders(orders) {
        // Auto Orders panel removed - method kept for compatibility
        console.log('Auto orders display skipped - panel removed');
        return;
    }

    // 编辑订单
    editOrder(orderId, symbol, action, quantity, orderType, limitPrice) {
        // 填充下单表单
        document.getElementById('orderSymbol').value = symbol;
        document.getElementById('orderAction').value = action;
        document.getElementById('orderQuantity').value = quantity;
        document.getElementById('orderType').value = orderType;
        
        const priceInput = document.getElementById('orderPrice');
        if (orderType === 'LMT' && limitPrice) {
            priceInput.value = limitPrice;
            priceInput.disabled = false;
        } else {
            priceInput.value = '';
            priceInput.disabled = true;
        }
        
        // 显示下单模态框
        const modal = new bootstrap.Modal(document.getElementById('orderModal'));
        modal.show();
        
        // 先取消原订单
        this.cancelOrder(orderId);
        
        this.showAlert('info', `正在编辑订单 ${orderId}，原订单已取消，请重新提交`);
    }

    // 取消订单
    async cancelOrder(orderId) {
        try {
            const result = await this.apiCall('/cancel_order', {
                method: 'POST',
                body: JSON.stringify({ order_id: orderId })
            });
            
            if (result.success) {
                this.showAlert('success', `订单 ${orderId} 已成功取消`);
                this.loadOrders(); // 刷新订单列表
            } else {
                this.showAlert('danger', `取消订单失败: ${result.error}`);
            }
        } catch (error) {
            console.error('Cancel order error:', error);
            this.showAlert('danger', '取消订单时发生错误');
        }
    }

    async loadStrategies() {
        try {
            const data = await this.apiCall('/strategies');
            if (data.success) {
                this.displayStrategies(data.strategies);
            }
        } catch (error) {
            console.error('Failed to load strategies:', error);
        }
    }

    displayStrategies(strategies) {
        const tbody = document.querySelector('#strategiesTable tbody');
        if (!tbody) return;
        
        tbody.innerHTML = '';
        strategies.forEach(strategy => {
            const row = document.createElement('tr');
            
            const nameCell = document.createElement('td');
            nameCell.textContent = strategy.name;
            
            const typeCell = document.createElement('td');
            typeCell.innerHTML = `<span class="badge ${strategy.strategy_type === 'external' ? 'bg-primary' : 'bg-success'}">${strategy.strategy_type}</span>`;
            
            const enabledCell = document.createElement('td');
            const toggleSwitch = document.createElement('div');
            toggleSwitch.className = 'form-check form-switch';
            toggleSwitch.innerHTML = `
                <input class="form-check-input" type="checkbox" ${strategy.enabled ? 'checked' : ''} 
                       onchange="app.toggleStrategy('${strategy.name}', this.checked)">
            `;
            enabledCell.appendChild(toggleSwitch);
            
            const triggerCountCell = document.createElement('td');
            triggerCountCell.textContent = strategy.trigger_count || 0;
            
            const lastTriggeredCell = document.createElement('td');
            if (strategy.last_triggered) {
                const date = new Date(strategy.last_triggered);
                lastTriggeredCell.textContent = date.toLocaleString();
            } else {
                lastTriggeredCell.textContent = '-';
            }
            
            const descCell = document.createElement('td');
            descCell.textContent = strategy.description || '-';
            
            const actionsCell = document.createElement('td');
            actionsCell.innerHTML = `
                <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editStrategy('${strategy.name}')">Edit</button>
                <button class="btn btn-sm btn-outline-info me-1" onclick="app.openStrategyDetails('${strategy.name}')">Details</button>
                <button class="btn btn-sm btn-outline-danger" onclick="app.deleteStrategy('${strategy.name}')">Delete</button>
            `;
            
            row.appendChild(nameCell);
            row.appendChild(typeCell);
            row.appendChild(enabledCell);
            row.appendChild(triggerCountCell);
            row.appendChild(lastTriggeredCell);
            row.appendChild(descCell);
            row.appendChild(actionsCell);
            
            tbody.appendChild(row);
        });
    }

    async toggleStrategy(name, enabled) {
        try {
            await this.apiCall(`/strategies/${name}/toggle?enabled=${enabled}`, { method: 'POST' });
            this.showAlert('success', `Strategy ${name} ${enabled ? 'enabled' : 'disabled'}`);
        } catch (error) {
            console.error('Failed to toggle strategy:', error);
            this.showAlert('danger', 'Failed to toggle strategy');
            // Reload to revert the switch
            this.loadStrategies();
        }
    }

    async editStrategy(name) {
        try {
            const res = await this.apiCall(`/strategies/${name}`);
            if (!res.success) throw new Error('Failed to load strategy');
            const s = res.strategy;
            const nameEl = document.getElementById('editStrategyName');
            if (nameEl) nameEl.textContent = s.name;
            const desc = document.getElementById('editStrategyDescriptionInput');
            const api = document.getElementById('editStrategyApiInput');
            if (desc) desc.value = s.description || '';
            if (api) api.value = s.api_endpoint || '';

            // SL/TP: 仅对内部策略显示并填充
            const sltpGroup = document.getElementById('editSlTpGroup');
            if (sltpGroup) sltpGroup.style.display = s.strategy_type === 'internal' ? '' : 'none';
            const slEnabled = document.getElementById('editSlEnabledSwitch');
            const tpEnabled = document.getElementById('editTpEnabledSwitch');
            const slPrice = document.getElementById('editSlPriceInput');
            const tpPrice = document.getElementById('editTpPriceInput');
            const slType = document.getElementById('editSlTypeSelect');
            const tpType = document.getElementById('editTpTypeSelect');
            if (slEnabled) slEnabled.checked = !!s.sl_enabled;
            if (tpEnabled) tpEnabled.checked = !!s.tp_enabled;
            if (slPrice) slPrice.value = s.sl_price ?? '';
            if (tpPrice) tpPrice.value = s.tp_price ?? '';
            if (slType) slType.value = s.sl_type || 'amount';
            if (tpType) tpType.value = s.tp_type || 'amount';

            const modal = new bootstrap.Modal(document.getElementById('editStrategyModal'));
            modal.show();

            const updateBtn = document.getElementById('updateStrategyBtn');
            if (updateBtn) {
                const newBtn = updateBtn.cloneNode(true);
                updateBtn.parentNode.replaceChild(newBtn, updateBtn);
                newBtn.addEventListener('click', async () => {
                    const updates = {
                        description: document.getElementById('editStrategyDescriptionInput')?.value || '',
                        api_endpoint: document.getElementById('editStrategyApiInput')?.value || undefined,
                    };
                    if (s.strategy_type === 'internal') {
                        updates.sl_enabled = !!document.getElementById('editSlEnabledSwitch')?.checked;
                        updates.tp_enabled = !!document.getElementById('editTpEnabledSwitch')?.checked;
                        const slv = document.getElementById('editSlPriceInput')?.value;
                        const tpv = document.getElementById('editTpPriceInput')?.value;
                        updates.sl_price = slv ? Number(slv) : null;
                        updates.tp_price = tpv ? Number(tpv) : null;
                        updates.sl_type = document.getElementById('editSlTypeSelect')?.value || 'amount';
                        updates.tp_type = document.getElementById('editTpTypeSelect')?.value || 'amount';
                    }
                    try {
                        await this.apiCall(`/strategies/${name}`, { method: 'PUT', body: JSON.stringify(updates) });
                        this.showAlert('success', `Strategy ${name} updated`);
                        const modalEl = document.getElementById('editStrategyModal');
                        const inst = bootstrap.Modal.getInstance(modalEl);
                        if (inst) inst.hide();
                        this.loadStrategies();
                    } catch (e) {
                        console.error(e);
                        this.showAlert('danger', 'Failed to update strategy');
                    }
                });
            }
        } catch (e) {
            console.error('Edit strategy error:', e);
            this.showAlert('danger', 'Failed to open edit modal');
        }
    }

    async openStrategyDetails(name) {
        try {
            const nameEl = document.getElementById('detailsStrategyName');
            if (nameEl) nameEl.textContent = name;
            let s = null;
            try {
                const res = await this.apiCall(`/strategies/${name}`);
                if (res.success) s = res.strategy;
            } catch {}
            const typeEl = document.getElementById('detailsStrategyType');
            const trigEl = document.getElementById('detailsTriggerCount');
            const lastEl = document.getElementById('detailsLastTriggered');
            if (typeEl) typeEl.textContent = s?.strategy_type || '-';
            if (trigEl) trigEl.textContent = s?.trigger_count ?? 0;
            if (lastEl) lastEl.textContent = s?.last_triggered ? new Date(s.last_triggered).toLocaleString() : '-';

            // API Examples
            const container = document.getElementById('apiExampleContent');
            if (container) {
                if (s?.strategy_type === 'external') {
                    const endpoint = s.api_endpoint || '/api/strategy';
                    const samplePayload = { symbol: 'AAPL', action: 'BUY', quantity: 10, price: 190.12, meta: { note: 'optional' } };
                    const payloadStr = JSON.stringify(samplePayload).replace(/\"/g, '\\\"');
                    const curl = `curl -X POST \"${endpoint}\" \\\n  -H \"Content-Type: application/json\" \\\n  -d \"${payloadStr}\"`;
                    container.innerHTML = `
                      <div class="mb-2"><span class="badge bg-primary">external</span> endpoint: <code>${endpoint}</code></div>
                      <pre class="bg-light p-3"><code>${curl}</code></pre>
                      <p class="text-muted">如果在策略设置中开启鉴权，需要在请求体中加入 <code>{ token: &lt;TOKEN&gt; }</code> 字段，其中 TOKEN 来自策略设置</p>`;
                } else {
                    container.innerHTML = '<p class="text-muted">API examples are only available for external strategies.</p>';
                }
            }

            const modal = new bootstrap.Modal(document.getElementById('strategyDetailsModal'));
            modal.show();

            const rangeSel = document.getElementById('performanceRange');
            const customSection = document.getElementById('customRangeSection');
            if (rangeSel && customSection) {
                rangeSel.value = '1d';
                rangeSel.onchange = () => {
                    customSection.style.display = rangeSel.value === 'custom' ? '' : 'none';
                    if (rangeSel.value !== 'custom') {
                        this.loadStrategyPerformance(name, { range: rangeSel.value });
                    }
                };
            }
            const applyBtn = document.getElementById('applyCustomRangeBtn');
            if (applyBtn) {
                applyBtn.onclick = () => {
                    const start = document.getElementById('perfStart').value;
                    const end = document.getElementById('perfEnd').value;
                    if (start && end) {
                        this.loadStrategyPerformance(name, { range: 'custom', start, end });
                    }
                };
            }

            // Pagination controls and export
            const pageSizeSelect = document.getElementById('executionsPageSize');
            if (pageSizeSelect) {
                pageSizeSelect.value = String(this.executionsPagination.pageSize);
                pageSizeSelect.onchange = () => {
                    this.executionsPagination.pageSize = parseInt(pageSizeSelect.value, 10) || 25;
                    this.executionsPagination.currentPage = 1;
                    this.renderExecutionsTable();
                    this.renderExecutionsPagination();
                };
            }
            const exportBtn = document.getElementById('exportExecutionsBtn');
            if (exportBtn) {
                exportBtn.onclick = () => this.exportExecutionsCsv(name);
            }
            
            const refreshBtn = document.getElementById('refreshExecutionsBtn');
            if (refreshBtn) {
                refreshBtn.onclick = () => {
                    this.loadStrategyExecutions(name);
                    this.loadStrategyPerformance(name, { range: '1d' });
                };
            }

            const clearBtn = document.getElementById('clearExecutionsBtn');
            if (clearBtn) {
                clearBtn.onclick = async () => {
                    try {
                        const strategy = document.getElementById('detailsStrategyName')?.textContent || name;
                        if (!strategy) return;
                        if (!confirm(`确定要清空策略 "${strategy}" 的全部执行记录吗？此操作不可撤销！`)) {
                            return;
                        }
                        await this.apiCall(`/strategies/${strategy}/executions`, { method: 'DELETE' });
                        this.executionsPagination.allExecutions = [];
                        this.executionsPagination.totalItems = 0;
                        this.executionsPagination.currentPage = 1;
                        this.renderExecutionsTable();
                        this.renderExecutionsPagination();
                        this.showAlert('success', '已清空该策略的执行记录');
                    } catch (e) {
                        console.error('Clear executions error:', e);
                        this.showAlert('danger', '清空执行记录失败');
                    }
                };
            }

            await Promise.all([
                this.loadStrategyPerformance(name, { range: '1d' }),
                this.loadStrategyExecutions(name)
            ]);
        } catch (e) {
            console.error('Open details error:', e);
            this.showAlert('danger', 'Failed to open strategy details');
        }
    }

    async loadStrategyPerformance(name, { range = '1d', start = null, end = null } = {}) {
        try {
            const params = new URLSearchParams();
            if (range) params.set('range', range);
            if (start) params.set('start', start);
            if (end) params.set('end', end);
            const res = await this.apiCall(`/strategies/${name}/performance?${params.toString()}`);
            if (!res.success) return;
            const m = res.metrics || {};
            const el = (id) => document.getElementById(id);
            el('kpiPnL').textContent = `$${(m.total_pnl ?? 0).toFixed(2)}`;
            el('kpiWinRate').textContent = `${(m.win_rate ?? 0).toFixed(2)}%`;
            el('kpiMaxDD').textContent = `$${(m.max_drawdown ?? 0).toFixed(2)}`;
            el('kpiSharpe').textContent = `${(m.sharpe_ratio ?? 0).toFixed(2)}`;
            el('kpiTrades').textContent = `${m.total_executions ?? 0}`;
            el('kpiAvgDur').textContent = `${m.avg_trade_duration ?? 0}`;

            // Charts from executions over selected range
            const execRes = await this.apiCall(`/strategies/${name}/executions?limit=1000`);
            const executions = (execRes.success ? (execRes.executions || []) : []).slice().sort((a,b)=> new Date(a.execution_time) - new Date(b.execution_time));
            const labels = [];
            const cumData = [];
            let cum = 0;
            executions.forEach(ex => {
                const pnl = Number(ex.pnl || 0);
                cum += pnl;
                labels.push(ex.execution_time ? new Date(ex.execution_time).toLocaleString() : '');
                cumData.push(cum);
            });
            this.renderLineChart('cumulativePnLChart', 'Cumulative PnL', labels, cumData, 'rgba(54, 162, 235, 1)');
            const ddData = [];
            let peak = -Infinity;
            cum = 0;
            executions.forEach(ex => {
                cum += Number(ex.pnl || 0);
                peak = Math.max(peak, cum);
                ddData.push(cum - peak);
            });
            this.renderLineChart('drawdownChart', 'Drawdown', labels, ddData, 'rgba(255, 99, 132, 1)');
            const pnls = executions.map(ex => Number(ex.pnl || 0));
            const bins = this.makeHistogram(pnls, 20);
            this.renderBarChart('pnlDistributionChart', 'PnL Distribution', bins.labels, bins.counts, 'rgba(75, 192, 192, 0.7)');
            const wins = pnls.filter(v => v > 0).length;
            const losses = pnls.filter(v => v <= 0).length;
            this.renderPieChart('winLossChart', ['Win','Loss'], [wins, losses], ['#2ecc71', '#e74c3c']);
        } catch (e) {
            console.error('Load performance error:', e);
        }
    }

    async loadStrategyExecutions(name, limit = 100) {
        try {
            const res = await this.apiCall(`/strategies/${name}/executions?limit=${limit}`);
            if (!res.success) return;
            const all = res.executions || [];
            this.executionsPagination.allExecutions = all;
            this.executionsPagination.totalItems = all.length;
            this.executionsPagination.currentPage = 1;
            this.renderExecutionsTable();
            this.renderExecutionsPagination();
        } catch (e) {
            console.error('Load executions error:', e);
        }
    }

    renderExecutionsTable() {
        const tbody = document.querySelector('#executionsTable tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        const { currentPage, pageSize, allExecutions } = this.executionsPagination;
        const start = (currentPage - 1) * pageSize;
        const end = start + pageSize;
        const pageItems = allExecutions.slice(start, end);
        pageItems.forEach(exe => {
            const tr = document.createElement('tr');
            const t = exe.execution_time ? new Date(exe.execution_time).toLocaleString() : '-';
            tr.innerHTML = `
              <td>${t}</td>
              <td>${exe.symbol ?? '-'}</td>
              <td>${exe.action ?? '-'}</td>
              <td>${exe.quantity ?? '-'}</td>
              <td>${exe.price != null ? exe.price : '-'}</td>
              <td><span class="badge ${exe.status === 'executed' ? 'bg-success' : 'bg-secondary'}">${exe.status}</span></td>
              <td>${exe.pnl != null ? Number(exe.pnl).toFixed(2) : '-'}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    renderExecutionsPagination() {
        const ul = document.getElementById('executionsPagination');
        if (!ul) return;
        const { currentPage, pageSize, totalItems } = this.executionsPagination;
        const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
        const makeLi = (label, page, disabled = false, active = false) => {
            const li = document.createElement('li');
            li.className = `page-item ${disabled ? 'disabled' : ''} ${active ? 'active' : ''}`;
            const a = document.createElement('a');
            a.className = 'page-link';
            a.href = '#';
            a.textContent = label;
            a.onclick = (e) => {
                e.preventDefault();
                if (disabled || page === currentPage) return;
                this.executionsPagination.currentPage = page;
                this.renderExecutionsTable();
                this.renderExecutionsPagination();
            };
            li.appendChild(a);
            return li;
        };

        ul.innerHTML = '';
        ul.appendChild(makeLi('«', Math.max(1, currentPage - 1), currentPage === 1));

        const windowSize = 5;
        const start = Math.max(1, currentPage - Math.floor(windowSize / 2));
        const end = Math.min(totalPages, start + windowSize - 1);
        for (let p = start; p <= end; p++) {
            ul.appendChild(makeLi(String(p), p, false, p === currentPage));
        }

        ul.appendChild(makeLi('»', Math.min(totalPages, currentPage + 1), currentPage === totalPages));
    }

    exportExecutionsCsv(name) {
        const rows = [
            ['Time','Symbol','Action','Qty','Price','Status','PnL']
        ];
        const all = this.executionsPagination.allExecutions || [];
        all.forEach(exe => {
            rows.push([
                exe.execution_time ? new Date(exe.execution_time).toISOString() : '',
                exe.symbol ?? '',
                exe.action ?? '',
                exe.quantity ?? '',
                exe.price ?? '',
                exe.status ?? '',
                exe.pnl ?? ''
            ]);
        });
        const csv = rows.map(r => r.map(v => typeof v === 'string' && v.includes(',') ? `"${v.replace(/"/g,'""')}"` : v).join(',')).join('\n');
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${(document.getElementById('detailsStrategyName')?.textContent || name)}_executions.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    // Chart helpers
    ensureChartDestroyed(id) {
        if (this.charts[id]) {
            try { this.charts[id].destroy(); } catch {}
            delete this.charts[id];
        }
    }

    renderLineChart(canvasId, label, labels, data, color) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        this.ensureChartDestroyed(canvasId);
        this.charts[canvasId] = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets: [{ label, data, borderColor: color, backgroundColor: color, fill: false, tension: 0.2, pointRadius: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, scales: { x: { display: true }, y: { display: true } } }
        });
    }

    renderBarChart(canvasId, label, labels, data, color) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        this.ensureChartDestroyed(canvasId);
        this.charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: { labels, datasets: [{ label, data, backgroundColor: color }] },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    renderPieChart(canvasId, labels, data, colors) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        this.ensureChartDestroyed(canvasId);
        this.charts[canvasId] = new Chart(ctx, {
            type: 'pie',
            data: { labels, datasets: [{ data, backgroundColor: colors }] },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    makeHistogram(values, bins) {
        const min = Math.min(...values);
        const max = Math.max(...values);
        const step = (max - min) / bins;
        const labels = [];
        const counts = new Array(bins).fill(0);
        for (let i = 0; i < bins; i++) {
            labels.push(`${(min + i * step).toFixed(2)}`);
        }
        values.forEach(v => {
            const idx = Math.min(bins - 1, Math.floor((v - min) / step));
            counts[idx]++;
        });
        return { labels, counts };
    }

    async deleteStrategy(name) {
        if (!confirm(`Are you sure you want to delete strategy "${name}"?`)) {
            return;
        }
        
        try {
            await this.apiCall(`/strategies/${name}`, { method: 'DELETE' });
            this.showAlert('success', `Strategy ${name} deleted`);
            this.loadStrategies();
        } catch (error) {
            console.error('Failed to delete strategy:', error);
            this.showAlert('danger', 'Failed to delete strategy');
        }
    }

    // Logs functionality removed - panel no longer exists

    async executeRiskRule(symbol, currentPrice, stopLossPrice, takeProfitPrice, positionSize) {
        if (!positionSize || positionSize === 0) {
            this.showAlert('No position found for ' + symbol, 'warning');
            return;
        }

        try {
            // Determine which rule to execute based on current price
            let orderType = 'LIMIT';
            let targetPrice = null;
            let action = positionSize > 0 ? 'SELL' : 'BUY';
            let ruleType = '';
            let slippage = 0;

            // Check stop loss condition
            if (stopLossPrice && ((positionSize > 0 && currentPrice <= stopLossPrice) || (positionSize < 0 && currentPrice >= stopLossPrice))) {
                targetPrice = stopLossPrice;
                ruleType = 'Stop Loss';
                // If price has already passed stop loss, use market order
                if ((positionSize > 0 && currentPrice < stopLossPrice) || (positionSize < 0 && currentPrice > stopLossPrice)) {
                    orderType = 'MARKET';
                    slippage = Math.abs(currentPrice - stopLossPrice) / stopLossPrice * 100;
                }
            }
            // Check take profit condition
            else if (takeProfitPrice && ((positionSize > 0 && currentPrice >= takeProfitPrice) || (positionSize < 0 && currentPrice <= takeProfitPrice))) {
                targetPrice = takeProfitPrice;
                ruleType = 'Take Profit';
                // If price has already passed take profit, use market order
                if ((positionSize > 0 && currentPrice > takeProfitPrice) || (positionSize < 0 && currentPrice < takeProfitPrice)) {
                    orderType = 'MARKET';
                    slippage = Math.abs(currentPrice - takeProfitPrice) / takeProfitPrice * 100;
                }
            }
            else {
                this.showAlert('No rule conditions met for execution', 'info');
                return;
            }

            // Confirm execution
            let confirmMessage = `Execute ${ruleType} for ${symbol}?\n`;
            confirmMessage += `Current Price: $${currentPrice.toFixed(2)}\n`;
            confirmMessage += `Target Price: $${targetPrice.toFixed(2)}\n`;
            confirmMessage += `Order Type: ${orderType}\n`;
            confirmMessage += `Quantity: ${Math.abs(positionSize)}\n`;
            if (slippage > 0) {
                confirmMessage += `Estimated Slippage: ${slippage.toFixed(4)}%\n`;
            }

            if (!confirm(confirmMessage)) {
                return;
            }

            // Prepare order data
            const orderData = {
                symbol: symbol,
                action: action,
                quantity: Math.abs(positionSize),
                order_type: orderType
            };

            if (orderType === 'LIMIT') {
                orderData.price = targetPrice;
            }

            // Place the order
            const response = await this.apiCall('/place_order', 'POST', orderData);
            
            let message = `${ruleType} order placed successfully for ${symbol}`;
            if (slippage > 0) {
                message += ` (Slippage: ${slippage.toFixed(4)}%)`;
            }
            
            this.showAlert(message, 'success');
            
            // Refresh data
            await Promise.all([
                this.loadPortfolio(),
                this.loadRiskRules()
            ]);

        } catch (error) {
            this.showAlert('Failed to execute risk rule: ' + error.message, 'danger');
        }
    }

    clearData() {
        document.getElementById('accountInfo').innerHTML = '<p class="text-muted">Connect to view account information</p>';
        document.querySelector('#ordersTable tbody').innerHTML = '';
        document.querySelector('#positionsTable tbody').innerHTML = '';
        document.querySelector('#autoOrdersTable tbody').innerHTML = '';
        document.querySelector('#riskRulesTable tbody').innerHTML = '';


    }

    getStatusColor(status) {
        switch (status.toLowerCase()) {
            case 'filled': return 'success';
            case 'cancelled': case 'canceled': return 'danger';
            case 'submitted': case 'presubmitted': return 'primary';
            case 'pendingsubmit': return 'warning';
            case 'pendingcancel': return 'info';
            case 'inactive': return 'secondary';
            case 'apiwaiting': return 'warning';
            case 'apipending': return 'warning';
            default: return 'secondary';
        }
    }

    formatNumber(num) {
        if (num === null || num === undefined) return 'N/A';
        return parseFloat(num).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    showAlert(type, message) {
        const alertContainer = document.getElementById('alertContainer');
        const alertId = 'alert-' + Date.now();
        
        const alertHtml = `
            <div id="${alertId}" class="alert alert-${type} alert-dismissible fade show" role="alert">
                ${message}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            </div>
        `;
        
        alertContainer.insertAdjacentHTML('beforeend', alertHtml);
        
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            const alert = document.getElementById(alertId);
            if (alert) {
                const bsAlert = new bootstrap.Alert(alert);
                bsAlert.close();
            }
        }, 5000);
    }

    // Symbol combobox functionality
    initSymbolCombobox() {
        this.setupSymbolInput('orderSymbol', 'orderSymbolDropdown');
    }

    setupSymbolInput(inputId, dropdownId) {
        const input = document.getElementById(inputId);
        const dropdown = document.getElementById(dropdownId);
        let searchTimeout;

        // Load historical symbols on focus
        input.addEventListener('focus', () => {
            this.showHistoricalSymbols(dropdownId);
            dropdown.classList.add('show');
        });

        // Hide dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!input.contains(e.target) && !dropdown.contains(e.target)) {
                dropdown.classList.remove('show');
            }
        });

        // Search symbols on input
        input.addEventListener('input', (e) => {
            const query = e.target.value.trim();
            
            clearTimeout(searchTimeout);
            
            if (query.length === 0) {
                this.showHistoricalSymbols(dropdownId);
                return;
            }

            if (query.length >= 1) {
                searchTimeout = setTimeout(() => {
                    this.searchSymbols(query, dropdownId);
                }, 300);
            }
        });

        // Handle keyboard navigation
        input.addEventListener('keydown', (e) => {
            const items = dropdown.querySelectorAll('.dropdown-item');
            const activeItem = dropdown.querySelector('.dropdown-item.active');
            let activeIndex = Array.from(items).indexOf(activeItem);

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeIndex = activeIndex < items.length - 1 ? activeIndex + 1 : 0;
                this.setActiveDropdownItem(items, activeIndex);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeIndex = activeIndex > 0 ? activeIndex - 1 : items.length - 1;
                this.setActiveDropdownItem(items, activeIndex);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (activeItem) {
                    activeItem.click();
                }
            } else if (e.key === 'Escape') {
                dropdown.classList.remove('show');
                input.blur();
            }
        });
    }

    setActiveDropdownItem(items, activeIndex) {
        items.forEach((item, index) => {
            item.classList.toggle('active', index === activeIndex);
        });
    }

    showHistoricalSymbols(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        const historicalSymbols = this.getHistoricalSymbols();
        
        let html = '';
        
        if (historicalSymbols.length > 0) {
            html += '<h6 class="dropdown-header">Recent Symbols</h6>';
            historicalSymbols.forEach(symbol => {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            });
            html += '<div class="dropdown-divider"></div>';
        }
        
        html += '<h6 class="dropdown-header">Popular Symbols</h6>';
        const popularSymbols = ['MNQ', 'NQ', 'ES', 'YM', 'AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA', 'AMZN'];
        popularSymbols.forEach(symbol => {
            if (!historicalSymbols.includes(symbol)) {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            }
        });
        
        dropdown.innerHTML = html;
        
        // Add click handlers
        dropdown.querySelectorAll('.dropdown-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const symbol = e.target.getAttribute('data-symbol');
                const inputId = dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol';
                document.getElementById(inputId).value = symbol;
                dropdown.classList.remove('show');
                this.addToHistoricalSymbols(symbol);
            });
        });
    }

    async searchSymbols(query, dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        
        try {
            // Show loading
            dropdown.innerHTML = '<div class="dropdown-item-text">Searching...</div>';
            dropdown.classList.add('show');
            
            // For now, we'll use a simple client-side filter of common symbols
            // In a real implementation, this would call an API
            const allSymbols = [
                'MNQ', 'NQ', 'ES', 'YM', 'RTY', 'CL', 'GC', 'SI',
                'AAPL', 'MSFT', 'GOOGL', 'GOOG', 'TSLA', 'NVDA', 'AMZN', 'META',
                'NFLX', 'AMD', 'INTC', 'CRM', 'ORCL', 'ADBE', 'PYPL', 'UBER',
                'SPOT', 'ZM', 'DOCU', 'SNOW', 'PLTR', 'COIN', 'RBLX', 'HOOD'
            ];
            
            const filteredSymbols = allSymbols.filter(symbol => 
                symbol.toLowerCase().includes(query.toLowerCase())
            );
            
            let html = '';
            
            if (filteredSymbols.length > 0) {
                html += '<h6 class="dropdown-header">Search Results</h6>';
                filteredSymbols.slice(0, 10).forEach(symbol => {
                    html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
                });
            } else {
                html += '<div class="dropdown-item-text text-muted">No symbols found</div>';
            }
            
            dropdown.innerHTML = html;
            
            // Add click handlers
            dropdown.querySelectorAll('.dropdown-item').forEach(item => {
                item.addEventListener('click', (e) => {
                    e.preventDefault();
                    const symbol = e.target.getAttribute('data-symbol');
                    const inputId = dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol';
                    document.getElementById(inputId).value = symbol;
                    dropdown.classList.remove('show');
                    this.addToHistoricalSymbols(symbol);
                });
            });
            
        } catch (error) {
            console.error('Error searching symbols:', error);
            dropdown.innerHTML = '<div class="dropdown-item-text text-danger">Search failed</div>';
        }
    }

    getHistoricalSymbols() {
        const stored = localStorage.getItem('historicalSymbols');
        return stored ? JSON.parse(stored) : [];
    }

    addToHistoricalSymbols(symbol) {
        let historical = this.getHistoricalSymbols();
        
        // Remove if already exists
        historical = historical.filter(s => s !== symbol);
        
        // Add to beginning
        historical.unshift(symbol);
        
        // Keep only last 10
        historical = historical.slice(0, 10);
        
        localStorage.setItem('historicalSymbols', JSON.stringify(historical));
    }

    async loadAutoOrders() {
        try {
            const data = await this.apiCall('/auto_orders');
            
            if (data.success) {
                this.displayAutoOrders(data.orders);
            } else {
                console.error('Failed to load auto orders:', data.message);
            }
        } catch (error) {
            console.error('Error loading auto orders:', error);
        }
    }

    displayAutoOrders(orders) {
        const tbody = document.querySelector('#autoOrdersTable tbody');
        if (!tbody) {
            console.log('autoOrdersTable not found - panel has been removed');
            return;
        }
        tbody.innerHTML = '';
        
        if (!orders || orders.length === 0) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No auto orders today</td></tr>';
            return;
        }
        
        orders.forEach(order => {
            const row = document.createElement('tr');
            
            const createdAt = new Date(order.created_at).toLocaleString();
            const price = order.price > 0 ? order.price.toFixed(2) : 'Market';
            
            // 来源显示逻辑
            let sourceDisplay = '—';
            if (order.order_source) {
                if (order.order_source === 'manual' || order.order_source.startsWith('manual_') || order.order_source === '人工') {
                    // 手动单直接显示字段值
                    sourceDisplay = order.order_source;
                } else if (order.order_source === 'sl') {
                    sourceDisplay = '<span class="badge bg-danger">止损</span>';
                } else if (order.order_source === 'tp') {
                    sourceDisplay = '<span class="badge bg-success">止盈</span>';
                } else if (order.order_source === 'strategy') {
                    sourceDisplay = '<span class="badge bg-info">策略</span>';
                } else if (order.order_source === '机器人' || order.order_source === 'auto') {
                    sourceDisplay = '<span class="badge bg-secondary">自动</span>';
                } else {
                    // 其他情况直接显示字段值
                    sourceDisplay = order.order_source;
                }
            }
            
            row.innerHTML = `
                <td>${order.order_id}</td>
                <td>${order.symbol}</td>
                <td><span class="badge ${order.action === 'BUY' ? 'bg-success' : 'bg-danger'}">${order.action}</span></td>
                <td>${order.quantity}</td>
                <td>${order.order_type}</td>
                <td>${price}</td>
                <td>${order.status}</td>
                <td>${sourceDisplay}</td>
                <td>${order.rule_id}</td>
                <td>${createdAt}</td>
            `;
            
            tbody.appendChild(row);
        });
    }

    // 编辑订单
    editOrder(orderId, symbol, action, quantity, orderType, limitPrice) {
        // 填充下单表单
        document.getElementById('orderSymbol').value = symbol;
        document.getElementById('orderAction').value = action;
        document.getElementById('orderQuantity').value = quantity;
        document.getElementById('orderType').value = orderType;
        
        const priceInput = document.getElementById('orderPrice');
        if (orderType === 'LMT' && limitPrice) {
            priceInput.value = limitPrice;
            priceInput.disabled = false;
        } else {
            priceInput.value = '';
            priceInput.disabled = true;
        }
        
        // 显示下单模态框
        const modal = new bootstrap.Modal(document.getElementById('orderModal'));
        modal.show();
        
        // 先取消原订单
        this.cancelOrder(orderId);
        
        this.showAlert('info', `正在编辑订单 ${orderId}，原订单已取消，请重新提交`);
    }

    // 取消订单
    async cancelOrder(orderId) {
        try {
            const result = await this.apiCall('/cancel_order', {
                method: 'POST',
                body: JSON.stringify({ order_id: orderId })
            });
            
            if (result.success) {
                this.showAlert('success', `订单 ${orderId} 已成功取消`);
                this.loadOrders(); // 刷新订单列表
            } else {
                this.showAlert('danger', `取消订单失败: ${result.error}`);
            }
        } catch (error) {
            console.error('Cancel order error:', error);
            this.showAlert('danger', '取消订单时发生错误');
        }
    }

    async loadLogs() {
        try {
            const module = document.getElementById('logModule')?.value || 'all';
            const range = document.getElementById('logRange')?.value || 'today';
            const requestId = document.getElementById('logRequestId')?.value.trim();
            let url = `/logs?lines=200&range=${range}`;
            if (module !== 'all') url += `&module=${module}`;
            if (requestId) url += `&request_id=${encodeURIComponent(requestId)}`;
            const res = await this.apiCall(url);
            if (res?.logs) {
                const content = document.getElementById('logsContent');
                if (content) content.textContent = res.logs.join('');
            }
        } catch (e) {
            console.error('Load logs error:', e);
            this.showAlert('danger', '加载日志失败');
        }
    }

    async showLogStats() {
        try {
            const module = document.getElementById('logModule')?.value || 'all';
            const range = document.getElementById('logRange')?.value || 'today';
            let url = `/logs/stats?range=${range}`;
            if (module !== 'all') url += `&module=${module}`;
            const res = await this.apiCall(url);
            const tbody = document.querySelector('#logStatsTable tbody');
            if (tbody) {
                tbody.innerHTML = '';
                for (const [mod, paths] of Object.entries(res.stats || {})) {
                    for (const [path, s] of Object.entries(paths)) {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `<td>${mod}</td><td>${path}</td><td>${s.count}</td><td>${s.avg_time}</td><td>${(s.success_rate*100).toFixed(1)}%</td>`;
                        tbody.appendChild(tr);
                    }
                }
            }
            const modalEl = document.getElementById('logStatsModal');
            if (modalEl) {
                const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
                modal.show();
            }
        } catch (e) {
            console.error('Load log stats error:', e);
            this.showAlert('danger', '加载日志统计失败');
        }
    }

    async showRiskRuleOrders(symbol) {
        try {
            // 获取今天的订单数据和持仓数据
            const [ordersData, portfolioData] = await Promise.all([
                this.apiCall('/orders'),
                this.apiCall('/positions')
            ]);
            const today = new Date().toISOString().split('T')[0];
            
            // 获取该symbol的持仓信息
            const position = portfolioData.positions.find(pos => pos.symbol === symbol);
            const avgCost = position ? position.avg_cost : 0;
            
            // 过滤出今天的、由风险规则触发的、指定symbol的订单
            // 风险规则触发的订单特征：order_source为'sl'或'tp'等风险规则类型 且 symbol匹配
            const riskRuleOrders = ordersData.orders.filter(order => {
                const orderDate = new Date(order.submit_time || order.created_at).toISOString().split('T')[0];
                return orderDate === today && 
                       order.symbol === symbol && 
                       (order.order_source === 'sl' || order.order_source === 'tp' || order.trigger_type === 'risk_rule');
            });

            // 计算统计数据
            let totalOrders = riskRuleOrders.length;
            let totalProfit = 0;
            let totalLoss = 0;

            // 根据成交价和平均成本计算PnL
            riskRuleOrders.forEach(order => {
                if (order.status === 'Filled' && order.avg_fill_price && avgCost > 0) {
                    // 计算每股盈亏：(成交价 - 平均成本) * 数量
                    // 对于卖出订单，盈亏 = (成交价 - 平均成本) * 数量
                    // 对于买入订单，盈亏 = (平均成本 - 成交价) * 数量（通常风险规则不会有买入）
                    let pnlPerShare = 0;
                    if (order.action === 'SELL') {
                        pnlPerShare = order.avg_fill_price - avgCost;
                    } else if (order.action === 'BUY') {
                        pnlPerShare = avgCost - order.avg_fill_price;
                    }
                    
                    const totalPnl = pnlPerShare * order.quantity;
                    
                    if (totalPnl > 0) {
                        totalProfit += totalPnl;
                    } else {
                        totalLoss += Math.abs(totalPnl);
                    }
                }
            });

            // 更新规则信息
            document.getElementById('ruleInfoText').textContent = `${symbol} 风险规则触发的订单`;
            
            // 更新统计卡片
            document.getElementById('totalOrdersCount').textContent = totalOrders;
            document.getElementById('totalProfit').textContent = '$' + this.formatNumber(totalProfit);
            document.getElementById('totalLoss').textContent = '$' + this.formatNumber(totalLoss);

            // 渲染订单表格
            const tbody = document.querySelector('#riskRuleOrdersTable tbody');
            tbody.innerHTML = riskRuleOrders.map(order => {
                // 确定触发类型显示文本
                let triggerTypeText = '风险规则';
                let triggerTypeClass = 'bg-warning';
                
                // 根据订单来源确定显示文本
                if (order.order_source === 'sl') {
                    triggerTypeText = '止损';
                    triggerTypeClass = 'bg-danger';
                } else if (order.order_source === 'tp') {
                    triggerTypeText = '止盈';
                    triggerTypeClass = 'bg-success';
                } else if (order.order_source === 'strategy') {
                    triggerTypeText = '策略';
                    triggerTypeClass = 'bg-info';
                } else if (order.order_source === 'auto' || order.order_source === '机器人') {
                    triggerTypeText = '自动';
                    triggerTypeClass = 'bg-secondary';
                } else if (order.trigger_type === 'risk_rule') {
                    // 兼容旧的trigger_type字段
                    triggerTypeText = '风险规则';
                    triggerTypeClass = 'bg-warning';
                } else {
                    // 手动订单，显示order_source值或用户名
                    triggerTypeText = order.order_source || '手动';
                    triggerTypeClass = 'bg-info';
                }
                
                // 价格显示逻辑
                let priceDisplay = '';
                if (order.order_type === 'MKT') {
                    priceDisplay = order.avg_fill_price && order.avg_fill_price > 0 ? 
                        this.formatNumber(order.avg_fill_price) : 'Market';
                } else {
                    priceDisplay = order.limit_price ? this.formatNumber(order.limit_price) : 'N/A';
                }
                
                // 成交价显示
                const executionPrice = order.avg_fill_price || 0;
                
                // 计算该订单的PnL
                let orderPnl = 0;
                if (order.status === 'Filled' && order.avg_fill_price && avgCost > 0) {
                    let pnlPerShare = 0;
                    if (order.action === 'SELL') {
                        pnlPerShare = order.avg_fill_price - avgCost;
                    } else if (order.action === 'BUY') {
                        pnlPerShare = avgCost - order.avg_fill_price;
                    }
                    orderPnl = pnlPerShare * order.quantity;
                }
                
                // PnL显示样式
                const pnlClass = orderPnl > 0 ? 'text-success' : orderPnl < 0 ? 'text-danger' : 'text-muted';
                const pnlText = orderPnl !== 0 ? `$${this.formatNumber(Math.abs(orderPnl))}` : '$0.00';
                
                return `
                <tr>
                    <td>${order.order_id}</td>
                    <td>${new Date(order.submit_time || order.created_at).toLocaleString()}</td>
                    <td>${order.symbol}</td>
                    <td><span class="badge bg-${order.action === 'BUY' ? 'success' : 'danger'}">${order.action}</span></td>
                    <td>${order.quantity}</td>
                    <td>$${priceDisplay}</td>
                    <td><span class="badge bg-${this.getStatusColor(order.status)}">${order.status}</span></td>
                    <td>$${this.formatNumber(executionPrice)}</td>
                    <td class="${pnlClass}">${pnlText}</td>
                    <td><span class="badge ${triggerTypeClass}">${triggerTypeText}</span></td>
                </tr>
                `;
            }).join('');

            // 如果没有找到订单，显示提示信息
            if (riskRuleOrders.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="10" class="text-center text-muted">
                            今日暂无该风险规则触发的订单
                        </td>
                    </tr>
                `;
            }

            // 显示模态框
            const modalEl = document.getElementById('riskRuleOrdersModal');
            const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
            modal.show();

        } catch (error) {
            console.error('Failed to load risk rule orders:', error);
            this.showAlert('danger', '加载风险规则订单详情时发生错误');
        }
    }
}

// Initialize the app
const app = new TradingApp();

// Auto-refresh data every 30 seconds when connected
setInterval(() => {
    if (app.connected) {
        app.loadAccountInfo();
        app.loadPortfolio();
        app.loadOrders();
        app.loadRiskRules();
        app.loadAutoOrders();
        app.loadStrategies();
    }
}, 30000);