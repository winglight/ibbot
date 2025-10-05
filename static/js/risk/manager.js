(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const riskNs = appModules.risk = appModules.risk || {};
    const managerNs = riskNs.manager = riskNs.manager || {};

    const ATR_PARAM_FIELDS = [
        { id: 'atrLookback', key: 'lookback', parser: (value) => parseInt(value, 10) },
        { id: 'atrBarMinutes', key: 'bar_minutes', parser: (value) => parseInt(value, 10) },
        { id: 'atrStreamInterval', key: 'stream_interval', parser: (value) => parseInt(value, 10) },
        { id: 'atrUpdateThrottle', key: 'update_throttle', parser: (value) => parseFloat(value) },
        { id: 'atrMultiplierSl', key: 'multiplier_sl', parser: (value) => parseFloat(value) },
        { id: 'atrMultiplierTp', key: 'multiplier_tp', parser: (value) => parseFloat(value) },
        { id: 'atrDeltaThreshold', key: 'delta_threshold', parser: (value) => parseFloat(value) },
    ];

    class RiskRulesManager {
        constructor(app) {
            this.app = app;
            this.unsubscribe = null;
            this.currentRiskRuleData = null;
            this.stopLossListener = null;
            this.takeProfitListener = null;
            this.ruleTypeChangeHandler = null;

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
                return await this._fetchViaHttp(options);
            } catch (error) {
                console.error('Failed to load risk rules:', error);
                if (this.app && typeof this.app.setPanelLoading === 'function') {
                    this.app.setPanelLoading('risk-rules', false);
                }
                return 'failed';
            }
        }

        async _fetchViaHttp(options = {}) {
            try {
                if (this.app && typeof this.app.setPanelLoading === 'function') {
                    this.app.setPanelLoading('risk-rules', true);
                }
                const forceRefresh = Boolean(options.force);
                // Administrators can set options.force to true to bypass the in-memory snapshot.
                const path = forceRefresh ? '/risk_rules?force_refresh=true' : '/risk_rules';
                const riskData = await this.app.apiCall(path);
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

        async fetchRiskConfig(options = {}) {
            // Pass { force: true } to bypass the runtime snapshot and pull directly from Redis.
            const forceRefresh = Boolean(options.force);
            const path = forceRefresh ? '/risk-config?force_refresh=true' : '/risk-config';
            return this.app.apiCall(path);
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
            const normalizedRules = {};
            if (Array.isArray(rules)) {
                rules.forEach((rule) => {
                    const normalized = this.buildNormalizedRule(rule);
                    if (normalized) {
                        normalizedRules[normalized.symbol] = normalized;
                    }
                });
            } else {
                Object.entries(rules).forEach(([symbolKey, rule]) => {
                    const normalized = this.buildNormalizedRule(rule, symbolKey);
                    if (normalized) {
                        normalizedRules[normalized.symbol] = normalized;
                    }
                });
            }
            this.app.riskRules = normalizedRules;
            this.renderRiskRulesTable(normalizedRules);
            return true;
        }

        formatRiskRulePrice(value) {
            if (value === null || value === undefined) {
                return 'N/A';
            }
            const numeric = Number(value);
            if (!Number.isFinite(numeric) || numeric <= 0) {
                return 'N/A';
            }
            if (this.app && typeof this.app.formatNumber === 'function') {
                return '$' + this.app.formatNumber(numeric);
            }
            return `$${numeric.toFixed(2)}`;
        }

        formatRiskRuleMaxTime(value) {
            if (value === null || value === undefined) {
                return 'N/A';
            }
            if (value === '') {
                return 'N/A';
            }
            const numeric = Number(value);
            if (Number.isFinite(numeric)) {
                if (numeric <= 0) {
                    return 'N/A';
                }
                return `${numeric}`;
            }
            return value;
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
                const normalizedType = this.normalizeRuleType(rule);
                const typeLabel = normalizedType === 'trailing' ? 'Trailing' : 'Fixed';
                const typeClass = normalizedType === 'trailing' ? 'primary' : 'secondary';
                const portfolioItem = portfolioMap[rule.symbol];
                const currentPrice = portfolioItem ? portfolioItem.current_price : 0;
                const avgCost = portfolioItem ? portfolioItem.avg_cost : 0;
                const positionSize = portfolioItem ? portfolioItem.position : 0;

                const stopLossValue = (() => {
                    const numeric = Number(rule.stop_loss_price);
                    return Number.isFinite(numeric) && numeric > 0 ? rule.stop_loss_price : '';
                })();
                const takeProfitValue = (() => {
                    const numeric = Number(rule.take_profit_price);
                    return Number.isFinite(numeric) && numeric > 0 ? rule.take_profit_price : '';
                })();
                const symbolEscaped = rule.symbol.replace(/'/g, "\\'");
                const stopLossDisplay = this.formatRiskRulePrice(rule.stop_loss_price);
                const takeProfitDisplay = this.formatRiskRulePrice(rule.take_profit_price);
                const maxTimeDisplay = this.formatRiskRuleMaxTime(rule.max_time_span);

                return `
                    <tr>
                        <td>${rule.symbol}</td>
                        <td><span class="badge bg-${typeClass}">${typeLabel}</span></td>
                        <td>${stopLossDisplay}</td>
                        <td>${takeProfitDisplay}</td>
                        <td>${maxTimeDisplay}</td>
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

        normalizeRuleType(rule) {
            if (!rule) return 'fixed';
            const type = (rule.rule_type || '').toString().toLowerCase();
            if (type === 'trailing') return 'trailing';
            if (type === 'fixed') return 'fixed';
            return rule.auto_trailing ? 'trailing' : 'fixed';
        }

        buildNormalizedRule(rule, fallbackSymbol) {
            if (!rule) return null;
            const symbol = (rule.symbol || fallbackSymbol || '').toString().toUpperCase();
            if (!symbol) return null;
            const normalizedType = this.normalizeRuleType(rule);
            const atrParams = rule.atr_params && typeof rule.atr_params === 'object' ? { ...rule.atr_params } : {};
            return {
                ...rule,
                symbol,
                rule_type: normalizedType,
                atr_params: atrParams,
            };
        }

        updateRuleTypeSections(ruleType) {
            const normalized = (ruleType || 'fixed').toLowerCase();
            const isTrailing = normalized === 'trailing';
            document.querySelectorAll('.fixed-rule-field').forEach((el) => {
                if (!el) return;
                el.style.display = isTrailing ? 'none' : '';
            });
            const atrSection = document.getElementById('atrParamsSection');
            if (atrSection) {
                atrSection.style.display = isTrailing ? 'block' : 'none';
            }
            const pnlSection = document.getElementById('pnlPreviewSection');
            if (pnlSection) {
                pnlSection.style.display = isTrailing ? 'none' : 'block';
            }
        }

        collectAtrParams() {
            const params = {};
            ATR_PARAM_FIELDS.forEach(({ id, key, parser }) => {
                const el = document.getElementById(id);
                if (!el) return;
                const raw = el.value;
                if (raw === '' || raw === null || raw === undefined) return;
                const value = parser(raw);
                if (Number.isFinite(value)) {
                    params[key] = value;
                }
            });
            return params;
        }

        populateAtrParams(atrParams = {}) {
            ATR_PARAM_FIELDS.forEach(({ id, key }) => {
                const el = document.getElementById(id);
                if (!el) return;
                const defaultValue = el.getAttribute('data-default');
                if (Object.prototype.hasOwnProperty.call(atrParams, key)) {
                    const value = atrParams[key];
                    el.value = value !== undefined && value !== null ? value : (defaultValue ?? el.defaultValue ?? '');
                } else if (defaultValue !== null && defaultValue !== undefined) {
                    el.value = defaultValue;
                } else if (el.defaultValue !== undefined) {
                    el.value = el.defaultValue;
                } else {
                    el.value = '';
                }
            });
        }

        resetRuleTypeListener() {
            if (this.ruleTypeChangeHandler) {
                const typeSelect = document.getElementById('riskRuleType');
                if (typeSelect) {
                    typeSelect.removeEventListener('change', this.ruleTypeChangeHandler);
                }
                this.ruleTypeChangeHandler = null;
            }
        }

        handleRuleTypeSwitch(ruleType) {
            const normalized = (ruleType || 'fixed').toLowerCase();
            this.updateRuleTypeSections(normalized);
            if (this.currentRiskRuleData) {
                this.currentRiskRuleData.ruleType = normalized;
            }
            this.removePnLUpdateListeners();
            if (normalized === 'fixed') {
                this.addPnLUpdateListeners();
                this.updatePnLPreview();
            }
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
                const normalizedSymbol = (symbol || '').toString().toUpperCase();
                const existingRule = this.app?.riskRules?.[normalizedSymbol] || null;
                const normalizedType = this.normalizeRuleType(existingRule);
                const isTrailingRule = normalizedType === 'trailing';

                let sl = null;
                let tp = null;
                let hasInputPrice = false;
                let hasExistingPrice = false;

                if (enabled) {
                    const slInputEl = document.getElementById('monitorStopLoss');
                    const tpInputEl = document.getElementById('monitorTakeProfit');
                    sl = slInputEl && slInputEl.value ? parseFloat(slInputEl.value) : null;
                    tp = tpInputEl && tpInputEl.value ? parseFloat(tpInputEl.value) : null;

                    if (existingRule) {
                        const existingSl = Number(existingRule.stop_loss_price);
                        const existingTp = Number(existingRule.take_profit_price);
                        hasExistingPrice = Number.isFinite(existingSl) && existingSl > 0;
                        hasExistingPrice = hasExistingPrice || (Number.isFinite(existingTp) && existingTp > 0);
                    }

                    hasInputPrice = Number.isFinite(sl) && sl > 0;
                    hasInputPrice = hasInputPrice || (Number.isFinite(tp) && tp > 0);

                    if (!isTrailingRule && !hasExistingPrice && !hasInputPrice) {
                        throw new Error('请先输入止损或止盈价，并点击“保存规则”或直接在监控框输入后再启用。');
                    }

                    if (!isTrailingRule && hasInputPrice) {
                        await this.app.apiCall('/risk_rules', {
                            method: 'POST',
                            body: JSON.stringify({
                                symbol: normalizedSymbol,
                                stop_loss_price: Number.isFinite(sl) && sl > 0 ? sl : undefined,
                                take_profit_price: Number.isFinite(tp) && tp > 0 ? tp : undefined,
                            })
                        });
                    }
                }

                await this.app.apiCall(`/risk_rules/${normalizedSymbol}?enabled=${enabled}`, { method: 'PUT' });

                if (!this.app.riskRules) {
                    this.app.riskRules = {};
                }

                const updatedRule = Object.assign({}, existingRule || {}, {
                    symbol: normalizedSymbol,
                    rule_type: normalizedType || (existingRule?.rule_type) || 'fixed',
                    atr_params: existingRule && existingRule.atr_params ? { ...existingRule.atr_params } : {},
                    enabled,
                });

                if (!isTrailingRule && enabled && hasInputPrice) {
                    updatedRule.stop_loss_price = Number.isFinite(sl) && sl > 0 ? sl : (existingRule ? existingRule.stop_loss_price : undefined);
                    updatedRule.take_profit_price = Number.isFinite(tp) && tp > 0 ? tp : (existingRule ? existingRule.take_profit_price : undefined);
                } else if (!enabled && existingRule) {
                    updatedRule.stop_loss_price = existingRule.stop_loss_price;
                    updatedRule.take_profit_price = existingRule.take_profit_price;
                }

                this.app.riskRules[normalizedSymbol] = updatedRule;

                this.app.showAlert('success', `Risk rule for ${normalizedSymbol} ${enabled ? 'enabled' : 'disabled'} successfully`);
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

        openRiskRuleModal(symbol = '', stopLoss = '', takeProfit = '', maxTime = '', position = 0, currentPrice = 0, avgCost = 0, isEditMode = false, ruleType = 'fixed', atrParams = {}) {
            const symbolInput = document.getElementById('riskSymbol');
            const stopLossInput = document.getElementById('riskStopLossPrice');
            const takeProfitInput = document.getElementById('riskTakeProfitPrice');
            const maxTimeInput = document.getElementById('riskMaxTime');
            if (symbolInput) symbolInput.value = symbol;
            if (stopLossInput) stopLossInput.value = stopLoss;
            if (takeProfitInput) takeProfitInput.value = takeProfit;
            if (maxTimeInput) maxTimeInput.value = maxTime ?? '';

            const normalizedType = (ruleType || 'fixed').toLowerCase();
            const atrPayload = atrParams && typeof atrParams === 'object' ? { ...atrParams } : {};

            this.currentRiskRuleData = {
                symbol,
                position,
                currentPrice,
                avgCost,
                ruleType: normalizedType,
                atrParams: atrPayload,
            };

            this.populateAtrParams(atrPayload);

            const typeSelect = document.getElementById('riskRuleType');
            if (typeSelect) {
                typeSelect.value = normalizedType;
            }

            const submitBtn = document.getElementById('submitRiskRuleBtn');
            if (submitBtn) {
                if (isEditMode) {
                    submitBtn.innerHTML = '<i class="fas fa-save"></i> Save Changes';
                } else {
                    submitBtn.innerHTML = '<i class="fas fa-plus"></i> Add Risk Rule';
                }
            }

            this.handleRuleTypeSwitch(normalizedType);

            if (typeSelect) {
                this.resetRuleTypeListener();
                this.ruleTypeChangeHandler = (event) => {
                    const selectedType = event?.target?.value || 'fixed';
                    this.handleRuleTypeSwitch(selectedType);
                };
                typeSelect.addEventListener('change', this.ruleTypeChangeHandler);
            }

            const modalElement = document.getElementById('riskRuleModal');
            if (!modalElement) {
                return;
            }
            const modal = new bootstrap.Modal(modalElement);

            modalElement.addEventListener('hidden.bs.modal', () => {
                this.removePnLUpdateListeners();
                this.resetRuleTypeListener();
                this.currentRiskRuleData = null;
            }, { once: true });

            modal.show();
        }

        updatePnLPreview() {
            if (!this.currentRiskRuleData) return;
            if ((this.currentRiskRuleData.ruleType || 'fixed') === 'trailing') {
                return;
            }

            const { position, currentPrice, avgCost } = this.currentRiskRuleData;
            const stopLossPrice = parseFloat(document.getElementById('riskStopLossPrice').value) || 0;
            const takeProfitPrice = parseFloat(document.getElementById('riskTakeProfitPrice').value) || 0;

            if (position === 0 || !avgCost) {
                const stopLossEl = document.getElementById('stopLossPnL');
                const takeProfitEl = document.getElementById('takeProfitPnL');
                if (stopLossEl) {
                    stopLossEl.textContent = 'N/A';
                    stopLossEl.className = 'fw-bold text-muted';
                }
                if (takeProfitEl) {
                    takeProfitEl.textContent = 'N/A';
                    takeProfitEl.className = 'fw-bold text-muted';
                }
                return;
            }

            const stopLossPnL = (stopLossPrice - avgCost) * position;
            const takeProfitPnL = (takeProfitPrice - avgCost) * position;

            const stopLossElement = document.getElementById('stopLossPnL');
            const takeProfitElement = document.getElementById('takeProfitPnL');
            if (stopLossElement) {
                stopLossElement.textContent = `$${stopLossPnL.toFixed(2)}`;
                stopLossElement.className = stopLossPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
            }
            if (takeProfitElement) {
                takeProfitElement.textContent = `$${takeProfitPnL.toFixed(2)}`;
                takeProfitElement.className = takeProfitPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
            }
        }

        addPnLUpdateListeners() {
            this.removePnLUpdateListeners();

            if (this.currentRiskRuleData && (this.currentRiskRuleData.ruleType || 'fixed') === 'trailing') {
                return;
            }

            const symbolInput = document.getElementById('riskSymbol');
            if (!symbolInput) return;
            const symbol = symbolInput.value;
            const stopLossEl = document.getElementById('riskStopLossPrice');
            const takeProfitEl = document.getElementById('riskTakeProfitPrice');
            if (!stopLossEl || !takeProfitEl) return;

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

            stopLossEl.addEventListener('input', this.stopLossListener);
            takeProfitEl.addEventListener('input', this.takeProfitListener);
        }

        removePnLUpdateListeners() {
            if (this.stopLossListener) {
                const stopLossEl = document.getElementById('riskStopLossPrice');
                if (stopLossEl) {
                    stopLossEl.removeEventListener('input', this.stopLossListener);
                }
            }
            if (this.takeProfitListener) {
                const takeProfitEl = document.getElementById('riskTakeProfitPrice');
                if (takeProfitEl) {
                    takeProfitEl.removeEventListener('input', this.takeProfitListener);
                }
            }
        }

        async editPositionRiskRule(symbol, currentPrice, avgCost, position = 0, presetStopLoss = '', presetTakeProfit = '') {
            try {
                const config = await this.fetchRiskConfig();

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

                const existingRule = this.app?.riskRules?.[symbol] || null;
                const normalizedType = this.normalizeRuleType(existingRule);
                const atrParams = existingRule?.atr_params || {};
                const existingMaxTime = existingRule?.max_time_span ?? '';

                if (normalizedType === 'trailing') {
                    this.openRiskRuleModal(symbol, '', '', existingMaxTime || '', position, currentPrice, avgCost, true, normalizedType, atrParams);
                    return;
                }

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

                this.openRiskRuleModal(symbol, stopLoss, takeProfit, existingMaxTime || '', position, currentPrice, avgCost, true, normalizedType, atrParams);
            } catch (error) {
                console.error('Failed to get risk config:', error);
                let stopLoss, takeProfit;
                const isShortPosition = position < 0;

                const existingRule = this.app?.riskRules?.[symbol] || null;
                const normalizedType = this.normalizeRuleType(existingRule);
                const atrParams = existingRule?.atr_params || {};
                const existingMaxTime = existingRule?.max_time_span ?? '';

                if (normalizedType === 'trailing') {
                    this.openRiskRuleModal(symbol, '', '', existingMaxTime || '', position, currentPrice, avgCost, true, normalizedType, atrParams);
                    return;
                }

                if (presetStopLoss !== '' && presetTakeProfit !== '') {
                    stopLoss = presetStopLoss;
                    takeProfit = presetTakeProfit;
                } else {
                    let stopLossRatio = 0.025;
                    let takeProfitRatio = 0.025;

                    try {
                        const config = await this.fetchRiskConfig();
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

                this.openRiskRuleModal(symbol, stopLoss, takeProfit, existingMaxTime || '', position, currentPrice, avgCost, true, normalizedType, atrParams);
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
            const ruleTypeSelect = document.getElementById('riskRuleType');
            const selectedRuleType = ruleTypeSelect ? (ruleTypeSelect.value || 'fixed').toLowerCase() : 'fixed';
            const atrParams = this.collectAtrParams();
            const rule = {
                symbol: document.getElementById('riskSymbol').value.toUpperCase(),
                stop_loss_price: document.getElementById('riskStopLossPrice').value ? parseFloat(document.getElementById('riskStopLossPrice').value) : null,
                take_profit_price: document.getElementById('riskTakeProfitPrice').value ? parseFloat(document.getElementById('riskTakeProfitPrice').value) : null,
                max_time_span: document.getElementById('riskMaxTime').value || null,
                rule_type: selectedRuleType,
            };

            if (selectedRuleType === 'trailing') {
                rule.stop_loss_price = null;
                rule.take_profit_price = null;
                rule.max_time_span = null;
                rule.atr_params = atrParams;
            }

            if (!rule.symbol) {
                this.app.showAlert('warning', 'Please enter a symbol');
                return;
            }

            if (selectedRuleType !== 'trailing' && !rule.stop_loss_price && !rule.take_profit_price) {
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
                this.populateAtrParams({});
                this.handleRuleTypeSwitch('fixed');
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
                const escapeHtml = (value) => {
                    if (this.app && typeof this.app.escapeHtml === 'function') {
                        return this.app.escapeHtml(value);
                    }
                    if (value === null || value === undefined) return '';
                    return String(value)
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;')
                        .replace(/'/g, '&#39;');
                };

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

                    const rawOrderId = order?.order_id !== undefined && order?.order_id !== null
                        ? String(order.order_id).trim()
                        : '';
                    const orderIdInfo = this.app?.formatOrderIdDisplay?.(rawOrderId);
                    const orderIdCell = (() => {
                        if (orderIdInfo && typeof orderIdInfo.html === 'string') {
                            return orderIdInfo.html;
                        }
                        if (!rawOrderId) {
                            return '<span class="order-id-display text-muted">—</span>';
                        }
                        const safeFull = escapeHtml(rawOrderId);
                        const safeShort = escapeHtml(rawOrderId.slice(-3));
                        return `<span class="order-id-display" title="${safeFull}" aria-label="${safeFull}" data-full-order-id="${safeFull}">${safeShort}</span>`;
                    })();
                    const rowAttr = orderIdInfo?.hasValue
                        ? ` data-order-id="${orderIdInfo.escapedFull}"`
                        : (rawOrderId ? ` data-order-id="${escapeHtml(rawOrderId)}"` : '');

                    return `
                    <tr${rowAttr}>
                        <td>${orderIdCell}</td>
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
