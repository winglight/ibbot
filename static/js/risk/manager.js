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
                const ruleType = (rule.rule_type || 'fixed').toLowerCase();
                const ruleTypeLabel = ruleType === 'trailing' ? 'Trailing' : 'Fixed';

                return `
                    <tr>
                        <td>${rule.symbol}</td>
                        <td>${rule.stop_loss_price ? '$' + this.app.formatNumber(rule.stop_loss_price) : 'N/A'}</td>
                        <td>${rule.take_profit_price ? '$' + this.app.formatNumber(rule.take_profit_price) : 'N/A'}</td>
                        <td>${rule.max_time_span || 'N/A'}</td>
                        <td>${ruleTypeLabel}</td>
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

        normalizeRuleStatus(status) {
            if (!status || typeof status !== 'object') {
                return { passed: true, message: '正常' };
            }
            const passed = status.passed !== undefined ? !!status.passed : true;
            let message = status.message;
            if (!message) {
                const errors = Array.isArray(status.errors) ? status.errors : [];
                if (errors.length > 0) {
                    message = errors.map((err) => {
                        if (typeof err === 'string') return err;
                        try {
                            return JSON.stringify(err);
                        } catch (_) {
                            return String(err);
                        }
                    }).join(' | ');
                }
            }
            if (!message) {
                message = passed ? '正常' : '违规';
            }
            return { passed, message };
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
                        const existingType = existingRule?.rule_type || 'fixed';
                        await this.app.apiCall('/risk_rules', {
                            method: 'POST',
                            body: JSON.stringify({
                                symbol,
                                rule_type: existingType,
                                stop_loss_price: sl || undefined,
                                take_profit_price: tp || undefined,
                                atr_params: existingType === 'trailing' ? existingRule?.atr_params || undefined : undefined,
                            })
                        });

                        if (!this.app.riskRules) this.app.riskRules = {};
                        this.app.riskRules[symbol] = Object.assign({}, existingRule || {}, {
                            symbol,
                            stop_loss_price: sl ?? (existingRule ? existingRule.stop_loss_price : undefined),
                            take_profit_price: tp ?? (existingRule ? existingRule.take_profit_price : undefined),
                            rule_type: existingType,
                            atr_params: existingType === 'trailing' ? existingRule?.atr_params || null : existingRule?.atr_params || null,
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

        async loadConfigData() {
            try {
                const response = await this.app.apiCall('/config/rules', { method: 'GET' });

                if (response && response.g_rules) {
                    const gRules = response.g_rules;
                    document.getElementById('g0MaxDrawdown').value = gRules.g0_max_drawdown || 5.0;
                    document.getElementById('g1MaxLoss').value = gRules.g1_max_loss || 2.0;
                    document.getElementById('g2MaxPositionPerSymbol').value = gRules.g2_max_position_per_symbol || 10.0;
                    document.getElementById('g3CorrelationCheck').value = gRules.g3_correlation_check ? 'enabled' : 'disabled';
                    document.getElementById('g4LiquidityCheck').value = gRules.g4_liquidity_check ? 'enabled' : 'disabled';
                    document.getElementById('g5TimeWindow').value = gRules.g5_time_window ? 'enabled' : 'disabled';
                }

                if (response && response.s_rules) {
                    const sRules = response.s_rules;
                    document.getElementById('s0PositionSizing').value = sRules.s0_position_sizing || 'fixed';
                    document.getElementById('s1PositionValue').value = sRules.s1_risk_percentage || 1.0;
                    document.getElementById('s2MaxPositions').value = sRules.s2_max_positions || 10;
                    document.getElementById('s3Rebalancing').value = sRules.s3_volatility_adjustment ? 'enabled' : 'disabled';
                    document.getElementById('s4VolatilityAdjustment').value = sRules.s4_volatility_adjustment ? 'enabled' : 'disabled';
                }
            } catch (error) {
                console.error('加载配置数据失败:', error);
                this.resetConfigRules();
            }
        }

        resetConfigRules() {
            const setValue = (id, value) => {
                const el = document.getElementById(id);
                if (el) el.value = value;
            };

            setValue('g0MaxDrawdown', 5.0);
            setValue('g1MaxLoss', 2.0);
            setValue('g2MaxPositionPerSymbol', 10.0);
            setValue('g3CorrelationCheck', 'enabled');
            setValue('g4LiquidityCheck', 'enabled');
            setValue('g5TimeWindow', 'enabled');

            setValue('s0PositionSizing', 'fixed');
            setValue('s1PositionValue', 1.0);
            setValue('s2MaxPositions', 10);
            setValue('s3Rebalancing', 'enabled');
            setValue('s4VolatilityAdjustment', 'enabled');

            this.updatePositionValueField();
            this.app.showAlert('info', '配置已重置为默认值');
        }

        async saveConfigRules() {
            try {
                const gRulesConfig = {
                    g0_max_drawdown: parseFloat(document.getElementById('g0MaxDrawdown').value) || 5.0,
                    g1_max_loss: parseFloat(document.getElementById('g1MaxLoss').value) || 2.0,
                    g2_max_position_per_symbol: parseFloat(document.getElementById('g2MaxPositionPerSymbol').value) || 10.0,
                    g3_correlation_check: document.getElementById('g3CorrelationCheck').value === 'enabled',
                    g4_liquidity_check: document.getElementById('g4LiquidityCheck').value === 'enabled',
                    g5_time_window: document.getElementById('g5TimeWindow').value === 'enabled'
                };

                const sRulesConfig = {
                    s0_position_sizing: document.getElementById('s0PositionSizing').value || 'fixed',
                    s1_risk_percentage: parseFloat(document.getElementById('s1PositionValue').value) || 1.0,
                    s2_max_positions: parseInt(document.getElementById('s2MaxPositions').value, 10) || 10,
                    s3_volatility_adjustment: document.getElementById('s3Rebalancing').value === 'enabled',
                    s4_volatility_adjustment: document.getElementById('s4VolatilityAdjustment').value === 'enabled'
                };

                await this.app.apiCall('/config/rules', {
                    method: 'PUT',
                    body: JSON.stringify({
                        g_rules: gRulesConfig,
                        s_rules: sRulesConfig
                    })
                });

                this.app.showAlert('success', '配置已保存');

                const modal = bootstrap.Modal.getInstance(document.getElementById('configRulesModal'));
                modal?.hide();

                this.app.loadGSRulesStatus?.();
            } catch (error) {
                console.error('保存配置失败:', error);
                this.app.showAlert('danger', '保存配置失败');
                throw error;
            }
        }

        async showConfigModal(activeTab = 'g-rules') {
            const gRulesTab = document.getElementById('config-g-rules-tab');
            const sRulesTab = document.getElementById('config-s-rules-tab');
            const gRulesPane = document.getElementById('config-g-rules');
            const sRulesPane = document.getElementById('config-s-rules');

            if (gRulesTab && sRulesTab && gRulesPane && sRulesPane) {
                if (activeTab === 'g-rules') {
                    gRulesTab.classList.add('active');
                    gRulesTab.setAttribute('aria-selected', 'true');
                    sRulesTab.classList.remove('active');
                    sRulesTab.setAttribute('aria-selected', 'false');
                    gRulesPane.classList.add('show', 'active');
                    sRulesPane.classList.remove('show', 'active');
                } else {
                    sRulesTab.classList.add('active');
                    sRulesTab.setAttribute('aria-selected', 'true');
                    gRulesTab.classList.remove('active');
                    gRulesTab.setAttribute('aria-selected', 'false');
                    sRulesPane.classList.add('show', 'active');
                    gRulesPane.classList.remove('show', 'active');
                }
            }

            await this.loadConfigData();

            const saveBtn = document.getElementById('saveConfigRulesBtn');
            const resetBtn = document.getElementById('resetConfigRulesBtn');
            if (saveBtn && resetBtn) {
                const newSaveBtn = saveBtn.cloneNode(true);
                const newResetBtn = resetBtn.cloneNode(true);
                saveBtn.parentNode.replaceChild(newSaveBtn, saveBtn);
                resetBtn.parentNode.replaceChild(newResetBtn, resetBtn);

                newSaveBtn.addEventListener('click', async () => {
                    try {
                        await this.saveConfigRules();
                    } catch (err) {
                        console.error('Failed to save config rules:', err);
                    }
                });
                newResetBtn.addEventListener('click', () => this.resetConfigRules());
            }

            this.updatePositionValueField();

            const modalEl = document.getElementById('configRulesModal');
            if (!modalEl) return;
            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();
        }

        updatePositionValueField() {
            return this.app.orderManager?.updatePositionValueField?.();
        }

        openRiskRuleModal(symbol = '', stopLoss = '', takeProfit = '', maxTime = '', position = 0, currentPrice = 0, avgCost = 0, isEditMode = false, ruleType = 'fixed', atrParams = null) {
            document.getElementById('riskSymbol').value = symbol;
            document.getElementById('riskStopLossPrice').value = stopLoss;
            document.getElementById('riskTakeProfitPrice').value = takeProfit;
            document.getElementById('riskMaxTime').value = maxTime;

            const normalizedType = (ruleType || 'fixed').toLowerCase();
            const ruleTypeSelect = document.getElementById('riskRuleType');
            if (ruleTypeSelect) {
                ruleTypeSelect.value = normalizedType === 'trailing' ? 'trailing' : 'fixed';
                if (!ruleTypeSelect.dataset.listenerAttached) {
                    ruleTypeSelect.addEventListener('change', () => {
                        this.handleRuleTypeChange();
                    });
                    ruleTypeSelect.dataset.listenerAttached = '1';
                }
            }

            this.populateAtrParams(normalizedType === 'trailing' ? atrParams : null);

            this.currentRiskRuleData = {
                symbol: symbol,
                position: position,
                currentPrice: currentPrice,
                avgCost: avgCost,
                ruleType: normalizedType,
                atrParams: atrParams || null,
            };

            const pnlSection = document.getElementById('pnlPreviewSection');
            if (pnlSection) {
                pnlSection.style.display = normalizedType === 'trailing' ? 'none' : 'block';
            }

            const submitBtn = document.getElementById('submitRiskRuleBtn');
            if (isEditMode) {
                submitBtn.innerHTML = '<i class="fas fa-save"></i> Save Changes';
            } else {
                submitBtn.innerHTML = '<i class="fas fa-plus"></i> Add Risk Rule';
            }

            this.handleRuleTypeChange();

            const modal = new bootstrap.Modal(document.getElementById('riskRuleModal'));

            document.getElementById('riskRuleModal').addEventListener('hidden.bs.modal', () => {
                this.removePnLUpdateListeners();
            }, { once: true });

            modal.show();
        }

        updatePnLPreview() {
            if (!this.currentRiskRuleData) return;
            if (this.getSelectedRuleType() === 'trailing') {
                const pnlSection = document.getElementById('pnlPreviewSection');
                if (pnlSection) pnlSection.style.display = 'none';
                return;
            }

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

            if (this.getSelectedRuleType() === 'trailing') {
                return;
            }

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

        getSelectedRuleType() {
            const select = document.getElementById('riskRuleType');
            if (!select) return 'fixed';
            const value = (select.value || '').toLowerCase();
            return value === 'trailing' ? 'trailing' : 'fixed';
        }

        populateAtrParams(params) {
            const fields = [
                { id: 'riskAtrLookback', key: 'lookback' },
                { id: 'riskAtrBarMinutes', key: 'bar_minutes' },
                { id: 'riskAtrStreamInterval', key: 'stream_interval' },
                { id: 'riskAtrUpdateThrottle', key: 'update_throttle' },
                { id: 'riskAtrMultiplierSl', key: 'multiplier_sl' },
                { id: 'riskAtrMultiplierTp', key: 'multiplier_tp' },
                { id: 'riskAtrDeltaThreshold', key: 'delta_threshold' },
            ];
            fields.forEach(({ id, key }) => {
                const el = document.getElementById(id);
                if (!el) return;
                if (params && params[key] !== undefined && params[key] !== null) {
                    el.value = params[key];
                } else {
                    el.value = '';
                }
            });
        }

        collectAtrParams() {
            if (this.getSelectedRuleType() !== 'trailing') {
                return null;
            }
            const fields = [
                { id: 'riskAtrLookback', key: 'lookback', type: 'int' },
                { id: 'riskAtrBarMinutes', key: 'bar_minutes', type: 'int' },
                { id: 'riskAtrStreamInterval', key: 'stream_interval', type: 'int' },
                { id: 'riskAtrUpdateThrottle', key: 'update_throttle', type: 'float' },
                { id: 'riskAtrMultiplierSl', key: 'multiplier_sl', type: 'float' },
                { id: 'riskAtrMultiplierTp', key: 'multiplier_tp', type: 'float' },
                { id: 'riskAtrDeltaThreshold', key: 'delta_threshold', type: 'float' },
            ];
            const payload = {};
            fields.forEach(({ id, key, type }) => {
                const el = document.getElementById(id);
                if (!el) return;
                const raw = el.value;
                if (raw === '' || raw === null || raw === undefined) {
                    return;
                }
                const parsed = type === 'int' ? parseInt(raw, 10) : parseFloat(raw);
                if (Number.isFinite(parsed)) {
                    payload[key] = parsed;
                }
            });
            return Object.keys(payload).length > 0 ? payload : null;
        }

        handleRuleTypeChange() {
            const ruleType = this.getSelectedRuleType();
            const isTrailing = ruleType === 'trailing';
            document.querySelectorAll('[data-rule-section="fixed"]').forEach((el) => {
                el.style.display = isTrailing ? 'none' : '';
            });
            document.querySelectorAll('[data-rule-section="trailing"]').forEach((el) => {
                el.style.display = isTrailing ? '' : 'none';
            });
            const pnlSection = document.getElementById('pnlPreviewSection');
            if (pnlSection) {
                pnlSection.style.display = isTrailing ? 'none' : 'block';
            }
            if (isTrailing) {
                this.removePnLUpdateListeners();
            } else {
                this.addPnLUpdateListeners();
                this.updatePnLPreview();
            }
            if (this.currentRiskRuleData) {
                this.currentRiskRuleData.ruleType = ruleType;
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

                const existingRule = (this.app?.riskRules && this.app.riskRules[symbol]) || null;
                const ruleType = existingRule?.rule_type || 'fixed';
                const atrParams = existingRule?.atr_params || null;
                const maxTimeSpan = existingRule?.max_time_span || '';

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

                this.openRiskRuleModal(symbol, stopLoss, takeProfit, maxTimeSpan || '', position, currentPrice, avgCost, true, ruleType, atrParams);
            } catch (error) {
                console.error('Failed to get risk config:', error);
                let stopLoss, takeProfit;
                const isShortPosition = position < 0;

                const existingRule = (this.app?.riskRules && this.app.riskRules[symbol]) || null;
                const ruleType = existingRule?.rule_type || 'fixed';
                const atrParams = existingRule?.atr_params || null;
                const maxTimeSpan = existingRule?.max_time_span || '';

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

                this.openRiskRuleModal(symbol, stopLoss, takeProfit, maxTimeSpan || '', position, currentPrice, avgCost, true, ruleType, atrParams);
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
            const symbolInput = document.getElementById('riskSymbol');
            const symbol = (symbolInput?.value || '').trim().toUpperCase();
            const ruleType = this.getSelectedRuleType();
            const payload = {
                symbol,
                rule_type: ruleType,
            };

            const maxTimeRaw = document.getElementById('riskMaxTime')?.value;
            if (maxTimeRaw) {
                payload.max_time_span = maxTimeRaw;
            }

            if (!symbol) {
                this.app.showAlert('warning', 'Please enter a symbol');
                return;
            }

            if (ruleType === 'trailing') {
                const atrParams = this.collectAtrParams();
                if (atrParams) {
                    payload.atr_params = atrParams;
                }
            } else {
                const stopLossRaw = document.getElementById('riskStopLossPrice')?.value;
                const takeProfitRaw = document.getElementById('riskTakeProfitPrice')?.value;
                const stopLossPrice = stopLossRaw ? parseFloat(stopLossRaw) : null;
                const takeProfitPrice = takeProfitRaw ? parseFloat(takeProfitRaw) : null;
                const validStopLoss = Number.isFinite(stopLossPrice) ? stopLossPrice : null;
                const validTakeProfit = Number.isFinite(takeProfitPrice) ? takeProfitPrice : null;

                if (!validStopLoss && !validTakeProfit) {
                    this.app.showAlert('warning', 'Please enter at least one price level');
                    return;
                }

                if (validStopLoss !== null) {
                    payload.stop_loss_price = validStopLoss;
                }
                if (validTakeProfit !== null) {
                    payload.take_profit_price = validTakeProfit;
                }
            }

            try {
                await this.app.apiCall('/risk_rules', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });

                this.app.showAlert('success', 'Risk rule added successfully');
                document.getElementById('riskRuleForm').reset();
                const ruleTypeSelect = document.getElementById('riskRuleType');
                if (ruleTypeSelect) {
                    ruleTypeSelect.value = 'fixed';
                }
                this.populateAtrParams(null);
                this.handleRuleTypeChange();
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

        async fetchRiskRuleOrders(symbol) {
            const ordersData = await this.app.apiCall('/orders');
            const positions = (this.app.dataManager.getAllPositions && this.app.dataManager.getAllPositions()) || [];
            const posMap = {};
            (positions || []).forEach((p) => {
                if (p && p.symbol) {
                    posMap[String(p.symbol).toUpperCase()] = p;
                }
            });

            const target = String(symbol || '').toUpperCase();

            const filtered = (ordersData.orders || []).filter((order) => {
                const os = String(order.order_source || '').toLowerCase();
                const isRisk = os === 'sl' || os === 'tp' || String(order.trigger_type || '').toLowerCase() === 'risk_rule';
                const symU = String(order.symbol || '').toUpperCase();
                const ridU = String(order.rule_id || '').toUpperCase();
                const isFilled = String(order.status || '').toLowerCase() === 'filled';
                return isRisk && (symU === target || ridU === target) && isFilled;
            }).sort((a, b) => new Date(b.submit_time || b.created_at || 0) - new Date(a.submit_time || a.created_at || 0));

            let totalProfit = 0;
            let totalLoss = 0;

            const orders = filtered.map((order) => {
                const pos = posMap[String(order.symbol || '').toUpperCase()];
                const avgCost = pos ? pos.avg_cost : 0;
                let orderPnl = 0;
                if (String(order.status || '').toLowerCase() === 'filled' && order.avg_fill_price && avgCost > 0) {
                    let pnlPerShare = 0;
                    if (order.action === 'SELL') {
                        pnlPerShare = order.avg_fill_price - avgCost;
                    } else if (order.action === 'BUY') {
                        pnlPerShare = avgCost - order.avg_fill_price;
                    }
                    orderPnl = pnlPerShare * (order.quantity || 0);
                    if (orderPnl > 0) totalProfit += orderPnl; else totalLoss += Math.abs(orderPnl);
                }
                return { order, avgCost, orderPnl };
            });

            return {
                symbol: target,
                totalOrders: orders.length,
                totalProfit,
                totalLoss,
                orders,
            };
        }

        async showRiskRuleOrders(symbol) {
            try {
                const data = await this.fetchRiskRuleOrders(symbol);

                document.getElementById('ruleInfoText').textContent = `${symbol} 风险规则触发的订单`;

                document.getElementById('totalOrdersCount').textContent = data.totalOrders;
                document.getElementById('totalProfit').textContent = '$' + this.app.formatNumber(data.totalProfit);
                document.getElementById('totalLoss').textContent = '$' + this.app.formatNumber(data.totalLoss);

                const tbody = document.querySelector('#riskRuleOrdersTable tbody');
                tbody.innerHTML = (data.orders || []).map(({ order, orderPnl }) => {
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

                if ((data.orders || []).length === 0) {
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
