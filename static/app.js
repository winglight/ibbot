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

    init() {
        this.setupEventListeners();
        this.initSymbolCombobox();
        this.connectWebSocket();
        this.checkConnectionStatus();
        this.loadStrategies();
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
        document.getElementById('connectionSettingsBtn').addEventListener('click', () => {
            const modal = new bootstrap.Modal(document.getElementById('connectionModal'));
            modal.show();
        });
        
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
        const logsBtn = document.getElementById('logsBtn');
        if (logsBtn) {
            logsBtn.addEventListener('click', () => {
                this.loadLogs();
                const panel = document.getElementById('logsContent');
                if (panel) {
                    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            });
        }
        const refreshLogsBtn = document.getElementById('refreshLogsBtn');
        if (refreshLogsBtn) {
            refreshLogsBtn.addEventListener('click', () => this.loadLogs());
        }
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
                if (!nameInput || !typeSelect || !descInput) return;
                // reset form
                nameInput.value = '';
                typeSelect.value = 'external';
                descInput.value = '';
                if (useTemplateCheckbox) useTemplateCheckbox.checked = true;
                if (useTemplateGroup) useTemplateGroup.style.display = 'none';
                const modal = new bootstrap.Modal(document.getElementById('addStrategyModal'));
                modal.show();
            });
        }

        // Add Strategy type toggle and submit
        const strategyTypeSelect = document.getElementById('strategyTypeSelect');
        if (strategyTypeSelect) {
            strategyTypeSelect.addEventListener('change', (e) => {
                const useTemplateGroup = document.getElementById('useTemplateGroup');
                if (useTemplateGroup) {
                    useTemplateGroup.style.display = e.target.value === 'internal' ? '' : 'none';
                }
            });
        }
        const createStrategyBtn = document.getElementById('createStrategyBtn');
        if (createStrategyBtn) {
            createStrategyBtn.addEventListener('click', async () => {
                const name = (document.getElementById('strategyNameInput')?.value || '').trim();
                const strategy_type = document.getElementById('strategyTypeSelect')?.value || 'external';
                const description = document.getElementById('strategyDescriptionInput')?.value || '';
                const use_template = strategy_type === 'internal' ? !!document.getElementById('strategyUseTemplate')?.checked : false;
                if (!name) { this.showAlert('danger', 'Please enter strategy name'); return; }
                try {
                    await this.apiCall('/strategies', {
                        method: 'POST',
                        body: JSON.stringify({ name, strategy_type, description, use_template })
                    });
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
        document.getElementById('refreshAutoOrdersBtn').addEventListener('click', () => {
            console.log('Refresh auto orders button clicked');
            this.loadAutoOrders();
        });

        
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
            default:
                console.log('Unknown WebSocket message:', data);
        }
    }

    async apiCall(endpoint, options = {}) {
        try {
            const response = await fetch(`/api${endpoint}`, {
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers
                },
                ...options
            });
            
            const data = await response.json();
            
            if (!response.ok) {
                throw new Error(data.detail || 'API call failed');
            }
            
            return data;
        } catch (error) {
            this.showAlert('danger', error.message);
            throw error;
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
            
            this.loadAccountInfo();
            this.loadOrders();
            this.loadPortfolio();
            this.loadRiskRules();
            this.loadAutoOrders();
            this.loadStrategies();
            this.loadLogs();
        } catch (error) {
            console.error('Connection failed:', error);
        }
    }

    async disconnect() {
        try {
            await this.apiCall('/disconnect', { method: 'POST' });
            this.showAlert('info', 'Disconnected successfully');
            this.clearData();
        } catch (error) {
            console.error('Disconnection failed:', error);
        }
    }

    async checkConnectionStatus() {
        try {
            const data = await this.apiCall('/connection_status');
            this.updateConnectionStatus(data.connected, data.account_type);
            
            if (data.connected) {
                this.loadAccountInfo();
                this.loadOrders();
                this.loadPortfolio();
                this.loadRiskRules();
                this.loadAutoOrders();
                this.loadStrategies();
                this.loadLogs();
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
        } else {
            statusIndicator.className = 'status-indicator status-disconnected';
            statusText.textContent = 'Disconnected';
            connectBtn.disabled = false;
            disconnectBtn.disabled = true;
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
        
        // Add new listeners
        this.stopLossListener = () => this.updatePnLPreview();
        this.takeProfitListener = () => this.updatePnLPreview();
        
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
            const response = await fetch('/api/risk-config');
            const config = await response.json();
            
            const stopLossRatio = config.stop_loss_ratio || 0.05;
            const takeProfitRatio = config.take_profit_ratio || 0.05;
            
            // Determine base price for SL/TP calculation
            let basePrice = avgCost; // Default to average cost
            
            // If current price has grown (profit), use current price as base
            if (currentPrice > avgCost) {
                basePrice = currentPrice;
            }
            
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
            
            // Use preset values if provided, otherwise calculate suggested values
            const stopLoss = presetStopLoss || (basePrice * (1 - adjustedStopLossRatio)).toFixed(2);
            const takeProfit = presetTakeProfit || (basePrice * (1 + adjustedTakeProfitRatio)).toFixed(2);
            
            this.openRiskRuleModal(symbol, stopLoss, takeProfit, position, currentPrice, avgCost, true);
        } catch (error) {
            console.error('Failed to get risk config:', error);
            // Fallback to preset values or default calculated values
            const stopLoss = presetStopLoss || (currentPrice * 0.95).toFixed(2);
            const takeProfit = presetTakeProfit || (currentPrice * 1.05).toFixed(2);
            this.openRiskRuleModal(symbol, stopLoss, takeProfit, position, currentPrice, avgCost, true);
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
                
                // 判断是否可以编辑/取消
                const canEdit = order.status === 'Submitted' || order.status === 'PreSubmitted';
                const canCancel = order.status === 'Submitted' || order.status === 'PreSubmitted';
                
                // 提交者显示逻辑
                const sourceClass = order.order_source === '机器人' ? 'badge bg-success' : 'badge bg-primary';
                const sourceText = order.order_source || '人工';
                
                return `
                    <tr>
                        <td>${order.order_id}</td>
                        <td>${order.symbol}</td>
                        <td>${order.action}</td>
                        <td>${order.quantity}</td>
                        <td>${priceDisplay}</td>
                        <td><span class="badge bg-${this.getStatusColor(order.status)}">${order.status}</span></td>
                        <td><span class="${sourceClass}">${sourceText}</span></td>
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
                        <button class="btn btn-sm btn-outline-primary" onclick="app.editPositionRiskRule('${pos.symbol}', ${pos.current_price}, ${pos.avg_cost}, ${pos.position})" title="Set TP/SL">
                            <i class="fas fa-shield-alt"></i>
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
                        <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editPositionRiskRule('${rule.symbol}', ${currentPrice}, ${avgCost}, ${positionSize}, '${rule.stop_loss_price || ''}', '${rule.take_profit_price || ''}')" title="Edit">
                            <i class="fas fa-edit"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-success me-1" onclick="app.executeRiskRule('${rule.symbol}', ${currentPrice}, ${rule.stop_loss_price || 0}, ${rule.take_profit_price || 0}, ${positionSize})" title="Execute Rule" ${!rule.enabled || !positionSize ? 'disabled' : ''}>
                            <i class="fas fa-play"></i>
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
            await this.apiCall('/cancel_all_orders', { method: 'DELETE' });
            this.showAlert('success', 'All orders cancelled successfully');
        } catch (error) {
            console.error('Failed to cancel all orders:', error);
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
            const response = await fetch('/api/auto_orders');
            const data = await response.json();
            
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
        tbody.innerHTML = '';
        
        if (!orders || orders.length === 0) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No auto orders today</td></tr>';
            return;
        }
        
        orders.forEach(order => {
            const row = document.createElement('tr');
            
            const createdAt = new Date(order.created_at).toLocaleString();
            const price = order.price > 0 ? order.price.toFixed(2) : 'Market';
            const sourceClass = order.order_source === 'auto' ? 'badge bg-success' : 'badge bg-primary';
            
            row.innerHTML = `
                <td>${order.order_id}</td>
                <td>${order.symbol}</td>
                <td><span class="badge ${order.action === 'BUY' ? 'bg-success' : 'bg-danger'}">${order.action}</span></td>
                <td>${order.quantity}</td>
                <td>${order.order_type}</td>
                <td>${price}</td>
                <td>${order.status}</td>
                <td><span class="${sourceClass}">${order.order_source}</span></td>
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
            const response = await fetch('/api/cancel_order', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ order_id: orderId })
            });
            
            const result = await response.json();
            
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
                      <p class="text-muted">If authentication is enabled in settings, add header: <code>-H \"Authorization: Bearer &lt;TOKEN&gt;\"</code></p>`;
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

    async loadLogs() {
        try {
            const logLevel = document.getElementById('logLevel')?.value || 'all';
            const logLines = document.getElementById('logLines')?.value || '100';
            
            const response = await this.apiCall(`/logs?lines=${logLines}&level=${logLevel}`);
            
            if (response.success) {
                this.displayLogs(response.logs);
            }
        } catch (error) {
            console.error('Failed to load logs:', error);
            this.showAlert('danger', 'Failed to load logs');
        }
    }

    displayLogs(logs) {
        const logsContent = document.getElementById('logsContent');
        if (logsContent) {
            logsContent.textContent = logs.join('\n');
            // Auto scroll to bottom
            logsContent.scrollTop = logsContent.scrollHeight;
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
            const response = await fetch('/api/auto_orders');
            const data = await response.json();
            
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
        tbody.innerHTML = '';
        
        if (!orders || orders.length === 0) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No auto orders today</td></tr>';
            return;
        }
        
        orders.forEach(order => {
            const row = document.createElement('tr');
            
            const createdAt = new Date(order.created_at).toLocaleString();
            const price = order.price > 0 ? order.price.toFixed(2) : 'Market';
            const sourceClass = order.order_source === 'auto' ? 'badge bg-success' : 'badge bg-primary';
            
            row.innerHTML = `
                <td>${order.order_id}</td>
                <td>${order.symbol}</td>
                <td><span class="badge ${order.action === 'BUY' ? 'bg-success' : 'bg-danger'}">${order.action}</span></td>
                <td>${order.quantity}</td>
                <td>${order.order_type}</td>
                <td>${price}</td>
                <td>${order.status}</td>
                <td><span class="${sourceClass}">${order.order_source}</span></td>
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
            const response = await fetch('/api/cancel_order', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ order_id: orderId })
            });
            
            const result = await response.json();
            
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
        app.loadLogs();
    }
}, 30000);