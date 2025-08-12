class TradingApp {
    constructor() {
        this.ws = null;
        this.connected = false;
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.initSymbolCombobox();
        this.connectWebSocket();
        this.checkConnectionStatus();
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
        
        // Place order button
        document.getElementById('placeOrderBtn').addEventListener('click', () => {
            // Restore last quantity value if available
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
            paper_trading: document.getElementById('paperTrading').value === 'true'
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



    // cancelOrder method moved to bottom of class to avoid duplication

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
    }
}, 30000);