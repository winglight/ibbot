/**
 * 风险规则管理模块
 * 负责 Risk Rules 面板相关功能
 */
class RiskRulesManager {
    constructor(app) {
        this.app = app;
        this.unsubscribe = null;
        if (this.app && typeof this.app.registerWebSocketHandler === 'function') {
            this.unsubscribe = this.app.registerWebSocketHandler('risk_rules', (payload) => this.applySnapshot(payload));
        }
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
            // 当启用规则时：优先尝试保存监控输入框中的SL/TP，再切换启用状态
            if (enabled) {
                const slInputEl = document.getElementById('monitorStopLoss');
                const tpInputEl = document.getElementById('monitorTakeProfit');
                const sl = slInputEl && slInputEl.value ? parseFloat(slInputEl.value) : null;
                const tp = tpInputEl && tpInputEl.value ? parseFloat(tpInputEl.value) : null;

                const existingRule = this.app.riskRules && this.app.riskRules[symbol] ? this.app.riskRules[symbol] : null;
                const hasExistingPrice = !!(existingRule && ((existingRule.stop_loss_price && existingRule.stop_loss_price > 0) || (existingRule.take_profit_price && existingRule.take_profit_price > 0)));
                const hasInputPrice = !!((sl && sl > 0) || (tp && tp > 0));

                // 若既没有已存在的有效价格，也没有新的输入价格，则禁止启用
                if (!hasExistingPrice && !hasInputPrice) {
                    throw new Error('请先输入止损或止盈价，并点击“保存规则”或直接在监控框输入后再启用。');
                }

                // 若有新的输入，则进行保存（后端POST为幂等的按symbol保存/更新）
                if (hasInputPrice) {
                    await this.app.apiCall('/risk_rules', {
                        method: 'POST',
                        body: JSON.stringify({
                            symbol,
                            stop_loss_price: sl || undefined,
                            take_profit_price: tp || undefined
                        })
                    });

                    // 更新本地缓存，改善UX
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
}

window.RiskRulesManager = RiskRulesManager;