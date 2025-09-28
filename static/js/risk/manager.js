(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const riskNs = appModules.risk = appModules.risk || {};
    const managerNs = riskNs.manager = riskNs.manager || {};

    class RiskRulesManager {
        constructor(app) {
            this.app = app;
            this.unsubscribe = null;
            this.currentRiskRuleData = null;
            this.stopLossListener = null;
            this.takeProfitListener = null;

            if (this.app && typeof this.app.registerWebSocketHandler === 'function') {
                this.unsubscribe = this.app.registerWebSocketHandler('risk_rules', (payload) => this.applySnapshot(payload));
            }
        }

        getCurrentRuleData() {
            return this.currentRiskRuleData;
        }

        async loadRiskRules(options = {}) {
            try {
                if (this.app && this.app.ibkrManager && typeof this.app.ibkrManager.requestRiskRules === 'function') {
                    const result = this.app.ibkrManager.requestRiskRules({
                        force: Boolean(options.force),
                        minInterval: options.minInterval ?? 15000
                    });
                    if (result !== 'failed') {
                        return result;
                    }
                }
                return await this._fetchViaHttp();
            } catch (error) {
                console.error('Failed to load risk rules:', error);
                if (this.app && typeof this.app.setPanelLoading === 'function') {
                    this.app.setPanelLoading('risk-rules', false);
                }
                return 'failed';
            }
        }

        async _fetchViaHttp() {
            try {
                if (this.app && typeof this.app.setPanelLoading === 'function') {
                    this.app.setPanelLoading('risk-rules', true);
                }
                const riskData = await this.app.apiCall('/risk_rules');
                this.applySnapshot({ type: 'risk_rules', success: riskData?.success !== false, rules: riskData?.rules || {}, source: 'http' });
                return 'http';
            } catch (error) {
                console.error('Failed to load risk rules via HTTP:', error);
                if (this.app && typeof this.app.setPanelLoading === 'function') {
                    this.app.setPanelLoading('risk-rules', false);
                }
                throw error;
            }
        }

        applySnapshot(payload) {
            if (!payload || payload.type !== 'risk_rules') {
                return false;
            }
            if (this.app && typeof this.app.setPanelLoading === 'function') {
                this.app.setPanelLoading('risk-rules', false);
            }
            if (!payload.success) {
                console.warn('Risk rules snapshot error:', payload.error || 'unknown');
                return false;
            }

            const rules = payload.rules || {};
            this.app.riskRules = rules;
            this.renderRiskRulesTable(rules);
            return true;
        }

        renderRiskRulesTable(rules) {
            const tbody = document.querySelector('#riskRulesTable2 tbody');
            if (!tbody) return;

            const portfolioData = { positions: this.app.dataManager.getAllPositions() };
            const portfolioMap = {};
            portfolioData.positions.forEach(pos => {
                portfolioMap[pos.symbol] = pos;
            });

            tbody.innerHTML = Object.values(rules).map(rule => {
                const portfolioItem = portfolioMap[rule.symbol];
                const currentPrice = portfolioItem ? portfolioItem.current_price : 0;
                const avgCost = portfolioItem ? portfolioItem.avg_cost : 0;
                const positionSize = portfolioItem ? portfolioItem.position : 0;

                const stopLossValue = rule.stop_loss_price || '';
                const takeProfitValue = rule.take_profit_price || '';
                const symbolEscaped = rule.symbol.replace(/'/g, "\\'");

                return `
                    <tr>
                        <td>${rule.symbol}</td>
                        <td>${rule.stop_loss_price ? '$' + this.app.formatNumber(rule.stop_loss_price) : 'N/A'}</td>
                        <td>${rule.take_profit_price ? '$' + this.app.formatNumber(rule.take_profit_price) : 'N/A'}</td>
                        <td>${rule.max_time_span || 'N/A'}</td>
                        <td><span class="badge bg-${rule.enabled ? 'success' : 'secondary'}">${rule.enabled ? 'Active' : 'Disabled'}</span></td>
                        <td>
                            <button class="btn btn-sm btn-outline-info me-1" onclick="app.showRiskRuleOrders('${symbolEscaped}')" title="Details">
                                <i class="fas fa-list"></i>
                            </button>
                            <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editPositionRiskRule('${symbolEscaped}', ${currentPrice || 0}, ${avgCost || 0}, ${positionSize || 0}, '${stopLossValue}', '${takeProfitValue}')" title="Edit">
                                <i class="fas fa-edit"></i>
                            </button>
                            <button class="btn btn-sm btn-outline-${rule.enabled ? 'warning' : 'success'} me-1" onclick="app.toggleRiskRule('${symbolEscaped}', ${!rule.enabled})" title="${rule.enabled ? 'Disable Rule' : 'Enable Rule'}">
                                <i class="fas fa-${rule.enabled ? 'pause' : 'play'}"></i>
                            </button>
                            <button class="btn btn-sm btn-danger" onclick="app.removeRiskRule('${symbolEscaped}')" title="Delete">
                                <i class="fas fa-trash"></i>
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        async removeRiskRule(symbol) {
            if (!confirm(`Are you sure you want to remove the risk rule for ${symbol}?`)) {
                return;
            }

            try {
                await this.app.apiCall(`/risk_rules/${symbol}`, { method: 'DELETE' });
                this.app.showAlert('success', 'Risk rule removed successfully');
                await this.loadRiskRules({ force: true });
            } catch (error) {
                console.error('Failed to remove risk rule:', error);
            }
        }

        async toggleRiskRule(symbol, enabled) {
            try {
                if (enabled) {
                    const slInputEl = document.getElementById('monitorStopLoss');
                    const tpInputEl = document.getElementById('monitorTakeProfit');
                    const sl = slInputEl && slInputEl.value ? parseFloat(slInputEl.value) : null;
                    const tp = tpInputEl && tpInputEl.value ? parseFloat(tpInputEl.value) : null;

                    const existingRule = this.app.riskRules && this.app.riskRules[symbol] ? this.app.riskRules[symbol] : null;
                    const hasExistingPrice = !!(existingRule && ((existingRule.stop_loss_price && existingRule.stop_loss_price > 0) || (existingRule.take_profit_price && existingRule.take_profit_price > 0)));
                    const hasInputPrice = !!((sl && sl > 0) || (tp && tp > 0));

                    if (!hasExistingPrice && !hasInputPrice) {
                        throw new Error('请先输入止损或止盈价，并点击“保存规则”或直接在监控框输入后再启用。');
                    }

                    if (hasInputPrice) {
                        await this.app.apiCall('/risk_rules', {
                            method: 'POST',
                            body: JSON.stringify({
                                symbol,
                                stop_loss_price: sl || undefined,
                                take_profit_price: tp || undefined
                            })
                        });

                        if (!this.app.riskRules) this.app.riskRules = {};
                        this.app.riskRules[symbol] = Object.assign({}, existingRule || {}, {
                            symbol,
                            stop_loss_price: sl ?? (existingRule ? existingRule.stop_loss_price : undefined),
                            take_profit_price: tp ?? (existingRule ? existingRule.take_profit_price : undefined)
                        });
                    }
                }

                await this.app.apiCall(`/risk_rules/${symbol}?enabled=${enabled}`, { method: 'PUT' });
                this.app.showAlert('success', `Risk rule for ${symbol} ${enabled ? 'enabled' : 'disabled'} successfully`);
                await this.loadRiskRules({ force: true });
            } catch (error) {
                console.error('Failed to update risk rule:', error);
                const msg = error && error.message ? error.message : `Failed to ${enabled ? 'enable' : 'disable'} risk rule`;
                this.app.showAlert('danger', msg);
                throw error;
            }
        }

        openRiskRuleModal(symbol = '', stopLoss = '', takeProfit = '', maxTime = '', position = 0, currentPrice = 0, avgCost = 0, isEditMode = false) {
            document.getElementById('riskSymbol').value = symbol;
            document.getElementById('riskStopLossPrice').value = stopLoss;
            document.getElementById('riskTakeProfitPrice').value = takeProfit;
            document.getElementById('riskMaxTime').value = maxTime;

            this.currentRiskRuleData = {
                symbol: symbol,
                position: position,
                currentPrice: currentPrice,
                avgCost: avgCost
            };

            const pnlSection = document.getElementById('pnlPreviewSection');
            pnlSection.style.display = 'block';
            this.updatePnLPreview();

            const submitBtn = document.getElementById('submitRiskRuleBtn');
            if (isEditMode) {
                submitBtn.innerHTML = '<i class="fas fa-save"></i> Save Changes';
            } else {
                submitBtn.innerHTML = '<i class="fas fa-plus"></i> Add Risk Rule';
            }

            this.addPnLUpdateListeners();

            const modal = new bootstrap.Modal(document.getElementById('riskRuleModal'));

            document.getElementById('riskRuleModal').addEventListener('hidden.bs.modal', () => {
                this.removePnLUpdateListeners();
            }, { once: true });

            modal.show();
        }

        updatePnLPreview() {
            if (!this.currentRiskRuleData) return;

            const { position, currentPrice, avgCost } = this.currentRiskRuleData;
            const stopLossPrice = parseFloat(document.getElementById('riskStopLossPrice').value) || 0;
            const takeProfitPrice = parseFloat(document.getElementById('riskTakeProfitPrice').value) || 0;

            if (position === 0 || !avgCost) {
                document.getElementById('stopLossPnL').textContent = 'N/A';
                document.getElementById('takeProfitPnL').textContent = 'N/A';
                document.getElementById('stopLossPnL').className = 'fw-bold text-muted';
                document.getElementById('takeProfitPnL').className = 'fw-bold text-muted';
                return;
            }

            const stopLossPnL = (stopLossPrice - avgCost) * position;
            const takeProfitPnL = (takeProfitPrice - avgCost) * position;

            document.getElementById('stopLossPnL').textContent = `$${stopLossPnL.toFixed(2)}`;
            document.getElementById('takeProfitPnL').textContent = `$${takeProfitPnL.toFixed(2)}`;

            const stopLossElement = document.getElementById('stopLossPnL');
            const takeProfitElement = document.getElementById('takeProfitPnL');

            stopLossElement.className = stopLossPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
            takeProfitElement.className = takeProfitPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
        }

        addPnLUpdateListeners() {
            this.removePnLUpdateListeners();

            const symbol = document.getElementById('riskSymbol').value;

            this.stopLossListener = (e) => {
                const correctedPrice = this.app.correctPriceStep(e.target.value, symbol);
                if (correctedPrice !== parseFloat(e.target.value)) {
                    e.target.value = correctedPrice;
                }
                this.updatePnLPreview();
            };

            this.takeProfitListener = (e) => {
                const correctedPrice = this.app.correctPriceStep(e.target.value, symbol);
                if (correctedPrice !== parseFloat(e.target.value)) {
                    e.target.value = correctedPrice;
                }
                this.updatePnLPreview();
            };

            document.getElementById('riskStopLossPrice').addEventListener('input', this.stopLossListener);
            document.getElementById('riskTakeProfitPrice').addEventListener('input', this.takeProfitListener);
        }

        removePnLUpdateListeners() {
            if (this.stopLossListener) {
                document.getElementById('riskStopLossPrice').removeEventListener('input', this.stopLossListener);
            }
            if (this.takeProfitListener) {
                document.getElementById('riskTakeProfitPrice').removeEventListener('input', this.takeProfitListener);
            }
        }

        async editPositionRiskRule(symbol, currentPrice, avgCost, position = 0, presetStopLoss = '', presetTakeProfit = '') {
            try {
                const config = await this.app.apiCall('/risk-config');

                const stopLossRatio = config.stop_loss_ratio || 0.025;
                const takeProfitRatio = config.take_profit_ratio || 0.025;

                const isShortPosition = position < 0;

                let basePrice = avgCost;

                let adjustedStopLossRatio = stopLossRatio;
                let adjustedTakeProfitRatio = takeProfitRatio;

                const leverageMap = (config && config.leverage) ? config.leverage : {};
                const lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || (symbol === 'MNQ' ? (leverageMap['MNQ'] || 20) : null);
                if (lev && !isNaN(lev) && lev > 0) {
                    adjustedStopLossRatio = stopLossRatio / lev;
                    adjustedTakeProfitRatio = takeProfitRatio / lev;
                }

                let stopLoss, takeProfit;

                if (presetStopLoss !== '' && presetTakeProfit !== '') {
                    stopLoss = presetStopLoss;
                    takeProfit = presetTakeProfit;
                } else {
                    if (isShortPosition) {
                        stopLoss = (basePrice * (1 + adjustedStopLossRatio)).toFixed(2);
                        takeProfit = (basePrice * (1 - adjustedTakeProfitRatio)).toFixed(2);
                    } else {
                        stopLoss = (basePrice * (1 - adjustedStopLossRatio)).toFixed(2);
                        takeProfit = (basePrice * (1 + adjustedTakeProfitRatio)).toFixed(2);
                    }
                }

                this.openRiskRuleModal(symbol, stopLoss, takeProfit, '', position, currentPrice, avgCost, true);
            } catch (error) {
                console.error('Failed to get risk config:', error);
                let stopLoss, takeProfit;
                const isShortPosition = position < 0;

                if (presetStopLoss !== '' && presetTakeProfit !== '') {
                    stopLoss = presetStopLoss;
                    takeProfit = presetTakeProfit;
                } else {
                    let stopLossRatio = 0.025;
                    let takeProfitRatio = 0.025;

                    try {
                        const config = await this.app.apiCall('/risk-config');
                        const leverageMap = (config && config.leverage) ? config.leverage : {};
                        const lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || (symbol === 'MNQ' ? (leverageMap['MNQ'] || 20) : null);
                        if (lev && !isNaN(lev) && lev > 0) {
                            stopLossRatio = stopLossRatio / lev;
                            takeProfitRatio = takeProfitRatio / lev;
                        }
                    } catch (leverageError) {
                        console.warn('Failed to get leverage info, using default ratios:', leverageError);
                    }

                    if (isShortPosition) {
                        stopLoss = presetStopLoss || (avgCost * (1 + stopLossRatio)).toFixed(2);
                        takeProfit = presetTakeProfit || (avgCost * (1 - takeProfitRatio)).toFixed(2);
                    } else {
                        stopLoss = presetStopLoss || (avgCost * (1 - stopLossRatio)).toFixed(2);
                        takeProfit = presetTakeProfit || (avgCost * (1 + takeProfitRatio)).toFixed(2);
                    }
                }

                this.openRiskRuleModal(symbol, stopLoss, takeProfit, '', position, currentPrice, avgCost, true);
            }
        }

        showActiveRiskRule(rule) {
            try {
                const activeRiskRuleDisplay = document.getElementById('activeRiskRuleDisplay');
                const activeStopLossPrice = document.getElementById('activeStopLossPrice');
                const activeTakeProfitPrice = document.getElementById('activeTakeProfitPrice');

                if (activeRiskRuleDisplay && activeStopLossPrice && activeTakeProfitPrice) {
                    if (rule.stop_loss_price) {
                        activeStopLossPrice.textContent = `$${this.app.formatNumber(rule.stop_loss_price)}`;
                    } else {
                        activeStopLossPrice.textContent = '未设置';
                    }

                    if (rule.take_profit_price) {
                        activeTakeProfitPrice.textContent = `$${this.app.formatNumber(rule.take_profit_price)}`;
                    } else {
                        activeTakeProfitPrice.textContent = '未设置';
                    }

                    activeRiskRuleDisplay.style.display = 'block';
                }
            } catch (err) {
                console.error('showActiveRiskRule failed:', err);
            }
        }

        hideActiveRiskRule() {
            try {
                const activeRiskRuleDisplay = document.getElementById('activeRiskRuleDisplay');
                if (activeRiskRuleDisplay) {
                    activeRiskRuleDisplay.style.display = 'none';
                }
            } catch (err) {
                console.error('hideActiveRiskRule failed:', err);
            }
        }

        async addRiskRule() {
            const rule = {
                symbol: document.getElementById('riskSymbol').value.toUpperCase(),
                stop_loss_price: document.getElementById('riskStopLossPrice').value ? parseFloat(document.getElementById('riskStopLossPrice').value) : null,
                take_profit_price: document.getElementById('riskTakeProfitPrice').value ? parseFloat(document.getElementById('riskTakeProfitPrice').value) : null,
                max_time_span: document.getElementById('riskMaxTime').value || null
            };

            if (!rule.symbol) {
                this.app.showAlert('warning', 'Please enter a symbol');
                return;
            }

            if (!rule.stop_loss_price && !rule.take_profit_price) {
                this.app.showAlert('warning', 'Please enter at least one price level');
                return;
            }

            try {
                await this.app.apiCall('/risk_rules', {
                    method: 'POST',
                    body: JSON.stringify(rule)
                });

                this.app.showAlert('success', 'Risk rule added successfully');
                document.getElementById('riskRuleForm').reset();
                this.loadRiskRules({ force: true });

                const modal = bootstrap.Modal.getInstance(document.getElementById('riskRuleModal'));
                if (modal) {
                    modal.hide();
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

        async showRiskRuleOrders(symbol) {
            try {
                const ordersData = await this.app.apiCall('/orders');

                const positions = (this.app.dataManager.getAllPositions && this.app.dataManager.getAllPositions()) || [];
                const posMap = {};
                (positions || []).forEach(p => { if (p && p.symbol) posMap[String(p.symbol).toUpperCase()] = p; });

                const target = String(symbol || '').toUpperCase();

                const riskRuleOrders = (ordersData.orders || []).filter(order => {
                    const os = String(order.order_source || '').toLowerCase();
                    const isRisk = os === 'sl' || os === 'tp' || String(order.trigger_type || '').toLowerCase() === 'risk_rule';
                    const symU = String(order.symbol || '').toUpperCase();
                    const ridU = String(order.rule_id || '').toUpperCase();
                    const isFilled = String(order.status || '').toLowerCase() === 'filled';
                    return isRisk && (symU === target || ridU === target) && isFilled;
                }).sort((a,b) => new Date(b.submit_time || b.created_at || 0) - new Date(a.submit_time || a.created_at || 0));

                let totalOrders = riskRuleOrders.length;
                let totalProfit = 0;
                let totalLoss = 0;

                riskRuleOrders.forEach(order => {
                    const pos = posMap[String(order.symbol || '').toUpperCase()];
                    const avgCost = pos ? pos.avg_cost : 0;
                    if (String(order.status || '').toLowerCase() === 'filled' && order.avg_fill_price && avgCost > 0) {
                        let pnlPerShare = 0;
                        if (order.action === 'SELL') pnlPerShare = order.avg_fill_price - avgCost; else if (order.action === 'BUY') pnlPerShare = avgCost - order.avg_fill_price;
                        const totalPnl = pnlPerShare * (order.quantity || 0);
                        if (totalPnl > 0) totalProfit += totalPnl; else totalLoss += Math.abs(totalPnl);
                    }
                });

                document.getElementById('ruleInfoText').textContent = `${symbol} 风险规则触发的订单`;

                document.getElementById('totalOrdersCount').textContent = totalOrders;
                document.getElementById('totalProfit').textContent = '$' + this.app.formatNumber(totalProfit);
                document.getElementById('totalLoss').textContent = '$' + this.app.formatNumber(totalLoss);

                const tbody = document.querySelector('#riskRuleOrdersTable tbody');
                tbody.innerHTML = (riskRuleOrders || []).map(order => {
                    let triggerTypeText = '风险规则';
                    let triggerTypeClass = 'bg-warning';
                    if (order.order_source === 'sl') { triggerTypeText = '止损'; triggerTypeClass = 'bg-danger'; }
                    else if (order.order_source === 'tp') { triggerTypeText = '止盈'; triggerTypeClass = 'bg-success'; }
                    else if (order.order_source === 'strategy') { triggerTypeText = '策略'; triggerTypeClass = 'bg-info'; }
                    else if (order.order_source === 'auto' || order.order_source === '机器人') { triggerTypeText = '自动'; triggerTypeClass = 'bg-secondary'; }
                    else if (String(order.trigger_type || '').toLowerCase() === 'risk_rule') { triggerTypeText = '风险规则'; triggerTypeClass = 'bg-warning'; }
                    else { triggerTypeText = order.order_source || '手动'; triggerTypeClass = 'bg-info'; }

                    let priceDisplay = '';
                    if (order.order_type === 'MKT' || order.order_type === 'MARKET') {
                        priceDisplay = order.avg_fill_price && order.avg_fill_price > 0 ? this.app.formatNumber(order.avg_fill_price) : 'Market';
                    } else {
                        priceDisplay = order.limit_price ? this.app.formatNumber(order.limit_price) : 'N/A';
                    }

                    const pos = posMap[String(order.symbol || '').toUpperCase()];
                    const avgCost = pos ? pos.avg_cost : 0;
                    let orderPnl = 0;
                    if (String(order.status || '').toLowerCase() === 'filled' && order.avg_fill_price && avgCost > 0) {
                        let pnlPerShare = 0;
                        if (order.action === 'SELL') pnlPerShare = order.avg_fill_price - avgCost; else if (order.action === 'BUY') pnlPerShare = avgCost - order.avg_fill_price;
                        orderPnl = pnlPerShare * (order.quantity || 0);
                    }
                    const pnlClass = orderPnl > 0 ? 'text-success' : orderPnl < 0 ? 'text-danger' : 'text-muted';
                    const pnlText = `$${this.app.formatNumber(orderPnl)}`;
                    const executionPrice = order.avg_fill_price || 0;

                    return `
                    <tr>
                        <td>${order.order_id}</td>
                        <td>${new Date(order.submit_time || order.created_at).toLocaleString()}</td>
                        <td>${order.symbol}</td>
                        <td><span class="badge bg-${order.action === 'BUY' ? 'success' : 'danger'}">${order.action}</span></td>
                        <td>${order.quantity}</td>
                        <td>$${priceDisplay}</td>
                        <td><span class="badge bg-${this.app.getStatusColor(order.status)}">${order.status}</span></td>
                        <td>$${this.app.formatNumber(executionPrice)}</td>
                        <td class="${pnlClass}">${pnlText}</td>
                        <td><span class="badge ${triggerTypeClass}">${triggerTypeText}</span></td>
                    </tr>
                    `;
                }).join('');

                if ((riskRuleOrders || []).length === 0) {
                    tbody.innerHTML = `
                        <tr>
                            <td colspan="10" class="text-center text-muted">暂无该风险规则关联的订单</td>
                        </tr>
                    `;
                }

                const modalEl = document.getElementById('riskRuleOrdersModal');
                const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
                modal.show();

            } catch (error) {
                console.error('Failed to load risk rule orders:', error);
                this.app.showAlert('danger', '加载风险规则订单详情时发生错误');
            }
        }
    }

    managerNs.RiskRulesManager = RiskRulesManager;
    root.RiskRulesManager = RiskRulesManager;
})(typeof window !== 'undefined' ? window : globalThis);
