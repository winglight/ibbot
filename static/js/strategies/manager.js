(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const strategies = appModules.strategies = appModules.strategies || {};
    const managerNs = strategies.manager = strategies.manager || {};

    class StrategiesManager {
        constructor(app) {
            this.app = app;
            this.refreshIntervalMs = 15000;
            this.strategyPerformanceCells = new Map();
            this.strategyPerformanceCache = new Map();
            this.strategyReceptionSwitchSeq = 0;
            this.strategyDataPushBusy = new Set();
            this.strategyToggleBusy = new Set();
            this.strategyParameterInputs = {};
            this.strategies = [];
            this.strategiesLoaded = false;
            this.sizingHistoryCache = { table: [], latest: null, meta: null, context: null, receivedAt: 0 };
            this.summaryStatusLoading = false;
            this.summaryStatusTarget = null;
            this.summaryPushUpdating = false;
            this.summaryPushAwaitingData = false;
            this.summaryPushAwaitingTimer = null;
            this.data_push_state = {};

            if (app && typeof app === 'object' && !Object.getOwnPropertyDescriptor(app, 'strategyRefreshIntervalMs')) {
                Object.defineProperty(app, 'strategyRefreshIntervalMs', {
                    configurable: true,
                    enumerable: false,
                    get: () => this.refreshIntervalMs,
                    set: (value) => {
                        const numeric = Number(value);
                        if (Number.isFinite(numeric) && numeric > 0) {
                            this.refreshIntervalMs = numeric;
                        }
                    }
                });
            }
        }

        getManagedProperties() {
            return [
                'strategyPerformanceCells',
                'strategyPerformanceCache',
                'strategyReceptionSwitchSeq',
                'strategyDataPushBusy',
                'strategyToggleBusy',
                'strategyParameterInputs',
                'strategies',
                'strategiesLoaded',
                'sizingHistoryCache',
                'summaryStatusLoading',
                'summaryStatusTarget',
                'summaryPushUpdating',
                'summaryPushAwaitingData',
                'summaryPushAwaitingTimer',
                'data_push_state'
            ];
        }

        async loadStrategies(options = {}) {
            const manager = this.app?.ibkrManager;
            if (manager && typeof manager.requestStrategies === 'function') {
                const minInterval = options.minInterval ?? this.refreshIntervalMs ?? 15000;
                const result = manager.requestStrategies({
                    force: Boolean(options.force),
                    minInterval,
                });
                if (result === 'failed') {
                    console.warn('Failed to send strategies request via websocket');
                }
                return result;
            }

            console.warn('WebSocket strategies request unavailable');
            return 'failed';
        }

        handleStrategyPerformanceMessage(message) {
            if (!message || !message.name) return false;
            const entry = this.strategyPerformanceCells.get(message.name);
            if (!entry) return false;

            const { pnlCell, tradesCell } = entry;
            if (!pnlCell || !tradesCell) return true;
            if (!message.success) {
                const err = message?.error || '';
                if (typeof err === 'string' && err.toLowerCase().includes('rate')) {
                    return true;
                }
                if (!this.strategyPerformanceCache.has(message.name)) {
                    pnlCell.textContent = '-';
                    tradesCell.textContent = '-';
                }
                return true;
            }

            const metrics = message.metrics || {};
            const totalPnL = Number(metrics.total_pnl ?? 0);
            const totalExec = Number(metrics.total_executions ?? 0);
            const formattedPnL = `$${(Number.isFinite(totalPnL) ? totalPnL : 0).toFixed(2)}`;
            const formattedTrades = `${Math.max(0, Math.floor((Number.isFinite(totalExec) ? totalExec : 0) / 2))}`;
            pnlCell.textContent = formattedPnL;
            tradesCell.textContent = formattedTrades;
            this.strategyPerformanceCache.set(message.name, { pnl: formattedPnL, trades: formattedTrades });
            return true;
        }

        handleStrategiesSnapshot(payload) {
            if (!payload || payload.type !== 'strategies_snapshot') return false;

            this.app?.setPanelLoading?.('strategies', false);
            if (!payload.success) {
                console.warn('Strategies snapshot failed:', payload?.error || 'unknown');
                return true;
            }

            const strategies = Array.isArray(payload.strategies) ? payload.strategies : [];
            this.strategies = strategies;
            this.strategiesLoaded = true;
            this.displayStrategies(strategies);
            return true;
        }

        handleSizingHistorySnapshot(payload) {
            if (!payload || payload.type !== 'sizing_history') return false;

            if (!payload.success) {
                console.warn('Sizing history snapshot failed:', payload?.error || 'unknown');
                if (payload.context === 'gs_rules') {
                    this.applySizingRuleStatusesFromHistory(null);
                }
                return true;
            }

            const history = Array.isArray(payload.data) ? payload.data : [];
            const meta = payload.meta && typeof payload.meta === 'object' ? payload.meta : {};
            const context = payload.context;

            const limitValue = Number(meta?.limit ?? 0);
            const isTablePayload = !context || context === 'table' || (Number.isFinite(limitValue) && limitValue > 1);

            this.sizingHistoryCache.meta = meta;
            this.sizingHistoryCache.context = context;
            this.sizingHistoryCache.receivedAt = Date.now();
            if (isTablePayload) {
                this.sizingHistoryCache.table = history;
            }
            if (history.length > 0) {
                this.sizingHistoryCache.latest = history[0];
            } else if (isTablePayload) {
                this.sizingHistoryCache.latest = null;
            }

            if (isTablePayload) {
                this.renderSizingHistory(history);
            }

            const latestRecord = history[0] || this.sizingHistoryCache.latest || null;
            this.applySizingRuleStatusesFromHistory(latestRecord);

            return true;
        }

        renderSizingHistory(history = []) {
            const tbody = document.querySelector('#sizingHistoryTable tbody');
            if (!tbody) return;

            const records = Array.isArray(history) ? history : [];
            if (records.length === 0) {
                tbody.innerHTML = `
                <tr>
                    <td colspan="6" class="text-center text-muted">
                        No sizing history available
                    </td>
                </tr>
            `;
                return;
            }

            const formatNumber = (value) => this.app?.formatNumber?.(value) ?? value;
            const rows = records.map((item) => {
                const timeValue = item?.sent_at || item?.created_at || item?.timestamp;
                let timestamp = '-';
                if (timeValue) {
                    try {
                        timestamp = new Date(timeValue).toLocaleString();
                    } catch (_) {
                        timestamp = String(timeValue);
                    }
                }
                const currentPrice = item?.current_price;
                const priceDisplay = (currentPrice !== null && currentPrice !== undefined)
                    ? `$${formatNumber(currentPrice)}`
                    : 'N/A';
                return `
                <tr>
                    <td>${timestamp}</td>
                    <td>${item?.symbol ?? '-'}</td>
                    <td><span class="badge bg-${item?.action === 'BUY' ? 'success' : 'danger'}">${item?.action ?? '-'}</span></td>
                    <td>${item?.suggested_quantity ?? '-'}</td>
                    <td>${item?.sizing_method ?? '-'}</td>
                    <td>${priceDisplay}</td>
                </tr>
            `;
            }).join('');

            tbody.innerHTML = rows;
        }

        applySizingRuleStatusesFromHistory(latestRecord) {
            const record = latestRecord && typeof latestRecord === 'object' ? latestRecord : null;
            const gStatus = record?.g_rules_status && typeof record.g_rules_status === 'object'
                ? record.g_rules_status
                : {};
            const sStatus = record?.s_rules_status && typeof record.s_rules_status === 'object'
                ? record.s_rules_status
                : {};
            const lastCheck = record?.timestamp || record?.sent_at || record?.created_at || null;

            const normalize = this.app?.normalizeRuleStatus?.bind(this.app);

            for (let i = 0; i <= 5; i += 1) {
                const key = `g${i}`;
                const status = normalize ? normalize(gStatus[key]) : (gStatus[key] || {});
                this.updateRuleStatus(key, status, lastCheck);
            }
            ['s0', 's1', 's2', 's3', 's4'].forEach((key) => {
                const status = normalize ? normalize(sStatus[key]) : (sStatus[key] || {});
                this.updateRuleStatus(key, status, lastCheck);
            });
        }

        handleStrategyStatusMessage(payload) {
            return this.applyStrategyStatusPayload(payload);
        }

        applyStrategyStatusPayload(payload) {
            if (!payload || payload.type !== 'strategy_status') {
                return false;
            }

            const targetName = payload.name || this.summaryStatusTarget || this.app?.currentStrategyDetails;
            if (!targetName) {
                return false;
            }
            if (this.summaryStatusTarget && payload.name && payload.name !== this.summaryStatusTarget) {
                return false;
            }

            const receivingEl = document.getElementById('summaryStatusReceiving');
            if (!receivingEl) {
                return false;
            }

            const runtimeEl = document.getElementById('summaryStatusRuntime');
            const processedEl = document.getElementById('summaryStatusProcessed');
            const thresholdEl = document.getElementById('summaryStatusThresholdHits');
            const buyEl = document.getElementById('summaryStatusBuySignals');
            const sellEl = document.getElementById('summaryStatusSellSignals');
            const statusPush = document.getElementById('summaryDataPushStatus');
            const pushSwitch = document.getElementById('summaryDataPushSwitch');
            const logList = document.getElementById('summaryDataLogList');
            const slPriceEl = document.getElementById('summaryStatusStopLoss');
            const tpPriceEl = document.getElementById('summaryStatusTakeProfit');

            this.summaryStatusLoading = false;
            this.summaryStatusTarget = targetName;

            if (!payload.success) {
                receivingEl.textContent = '-';
                runtimeEl && (runtimeEl.textContent = '-');
                processedEl && (processedEl.textContent = '-');
                thresholdEl && (thresholdEl.textContent = '-');
                buyEl && (buyEl.textContent = '-');
                sellEl && (sellEl.textContent = '-');
                slPriceEl && (slPriceEl.textContent = '-');
                tpPriceEl && (tpPriceEl.textContent = '-');
                if (statusPush) statusPush.textContent = '未推送';
                if (logList) logList.innerHTML = '<li class="text-muted">暂无记录</li>';
                this.summaryPushAwaitingData = false;
                this.data_push_state = {};
                if (this.summaryPushAwaitingTimer) {
                    clearTimeout(this.summaryPushAwaitingTimer);
                    this.summaryPushAwaitingTimer = null;
                }
                this.app?.setStrategyChartRiskLevels?.({ slEnabled: false, tpEnabled: false, slPrice: null, tpPrice: null });
                return true;
            }

            const status = payload.status || {};
            const pushState = status.data_push || {};
            const reasonText = status.data_push_reason || pushState.status_reason || '';
            const slEnabled = Boolean(status.sl_enabled);
            const tpEnabled = Boolean(status.tp_enabled);
            const rawSlPrice = status.sl_price;
            const rawTpPrice = status.tp_price;
            const slPriceNumber = (slEnabled && rawSlPrice !== null && rawSlPrice !== undefined)
                ? Number(rawSlPrice)
                : null;
            const tpPriceNumber = (tpEnabled && rawTpPrice !== null && rawTpPrice !== undefined)
                ? Number(rawTpPrice)
                : null;
            const formatNumber = this.app?.formatNumber?.bind(this.app);
            if (slPriceEl) {
                if (slEnabled) {
                    if (Number.isFinite(slPriceNumber) && formatNumber) {
                        slPriceEl.textContent = `$${formatNumber(slPriceNumber)}`;
                    } else {
                        slPriceEl.textContent = '—';
                    }
                } else {
                    slPriceEl.textContent = '未启用';
                }
            }
            if (tpPriceEl) {
                if (tpEnabled) {
                    if (Number.isFinite(tpPriceNumber) && formatNumber) {
                        tpPriceEl.textContent = `$${formatNumber(tpPriceNumber)}`;
                    } else {
                        tpPriceEl.textContent = '—';
                    }
                } else {
                    tpPriceEl.textContent = '未启用';
                }
            }
            this.app?.setStrategyChartRiskLevels?.({
                slEnabled,
                tpEnabled,
                slPrice: Number.isFinite(slPriceNumber) ? slPriceNumber : null,
                tpPrice: Number.isFinite(tpPriceNumber) ? tpPriceNumber : null,
            });
            if (status.is_receiving_data) {
                receivingEl.textContent = 'Yes';
            } else if (reasonText) {
                receivingEl.textContent = `No — ${reasonText}`;
            } else {
                receivingEl.textContent = 'No';
            }
            const formatRuntime = this.app?.formatRuntimeDuration?.bind(this.app);
            runtimeEl && (runtimeEl.textContent =
                typeof status.runtime_seconds === 'number' && formatRuntime
                    ? formatRuntime(status.runtime_seconds)
                    : '-');
            processedEl && (processedEl.textContent = status.processed_count ?? '-');
            thresholdEl && (thresholdEl.textContent = status.threshold_hits ?? '-');
            buyEl && (buyEl.textContent = status.buy_signals ?? '-');
            sellEl && (sellEl.textContent = status.sell_signals ?? '-');

            let awaitingData = !!pushState.awaiting_data && !!pushState.active;
            if (status.is_receiving_data) {
                awaitingData = false;
            }
            if (!pushState.active) {
                awaitingData = false;
            }

            this.summaryPushAwaitingData = awaitingData;
            if (awaitingData) {
                if (!this.summaryPushAwaitingTimer) {
                    this.summaryPushAwaitingTimer = setTimeout(() => {
                        this.summaryPushAwaitingTimer = null;
                        if (this.app?.currentStrategyDetails === targetName) {
                            this.app?.loadStrategyStatus?.(targetName, { force: true });
                        }
                    }, 1500);
                }
            } else if (this.summaryPushAwaitingTimer) {
                clearTimeout(this.summaryPushAwaitingTimer);
                this.summaryPushAwaitingTimer = null;
            }

            if (pushSwitch && !this.summaryPushUpdating) {
                pushSwitch.checked = !!pushState.active;
            }
            if (statusPush) {
                if (pushState.active) {
                    if (!awaitingData && reasonText) {
                        statusPush.textContent = reasonText;
                    } else {
                        statusPush.textContent = awaitingData ? '等待DOM数据…' : '已推送';
                    }
                } else {
                    statusPush.textContent = reasonText || '未推送';
                }
            }

            const logs = Array.isArray(status.data_push_logs) ? status.data_push_logs : [];
            const escapeHtml = this.app?.escapeHtml?.bind(this.app);
            if (logList) {
                if (!logs.length) {
                    logList.innerHTML = '<li class="text-muted">暂无记录</li>';
                } else {
                    const items = logs.map((entry, idx) => {
                        if (!entry || typeof entry !== 'object') return '';
                        const level = String(entry.level || 'info').toLowerCase();
                        let badgeClass = 'bg-secondary';
                        if (level === 'error') badgeClass = 'bg-danger';
                        else if (level === 'warning') badgeClass = 'bg-warning text-dark';
                        const timestamp = entry.timestamp && escapeHtml
                            ? `<span class="text-muted me-2">${escapeHtml(String(entry.timestamp))}</span>`
                            : (entry.timestamp ? `<span class="text-muted me-2">${entry.timestamp}</span>` : '');
                        const message = escapeHtml ? escapeHtml(String(entry.message || '')) : String(entry.message || '');
                        let detailHtml = '';
                        if (entry.details && typeof entry.details === 'object') {
                            const detailParts = Object.entries(entry.details).map(([key, value]) => {
                                const k = escapeHtml ? escapeHtml(String(key)) : String(key);
                                const v = escapeHtml ? escapeHtml(String(value ?? '')) : String(value ?? '');
                                return `${k}=${v}`;
                            });
                            if (detailParts.length) {
                                detailHtml = `<div class="text-muted ms-4">${detailParts.join(', ')}</div>`;
                            }
                        }
                        const borderClass = idx === 0 ? '' : ' border-top';
                        return `<li class="py-2${borderClass}"><span class="badge ${badgeClass} me-2">${level.toUpperCase()}</span>${timestamp}${message}${detailHtml}</li>`;
                    }).filter(Boolean);
                    logList.innerHTML = items.join('') || '<li class="text-muted">暂无记录</li>';
                }
            }

            this.data_push_state = {
                enabled: !!pushState.active,
                active: !!pushState.active,
                subscription: pushState.subscription,
                symbol: pushState.symbol,
                awaiting_data: awaitingData,
                last_data_timestamp: pushState.last_data_timestamp || null,
                status_reason: reasonText || null,
                logs,
            };

            return true;
        }

        async loadGSRulesStatus() {
            const latest = this.sizingHistoryCache?.latest || null;
            this.applySizingRuleStatusesFromHistory(latest);

            const manager = this.app?.ibkrManager;
            if (manager && typeof manager.requestSizingHistory === 'function') {
                const result = manager.requestSizingHistory({
                    limit: 1,
                    context: 'gs_rules',
                    minInterval: 5000,
                });
                if (result === 'failed') {
                    console.warn('Failed to request G/S rules status via websocket');
                }
                return result;
            }

            console.warn('WebSocket sizing history request unavailable for G/S rules');
            return 'failed';
        }

        updateRuleStatus(ruleId, status, lastCheck) {
            const raw = status && typeof status === 'object' ? status : {};
            const passed = raw.passed !== undefined ? !!raw.passed : true;
            const message = raw.message || (passed ? '正常' : '违规');

            const statusElement = document.getElementById(`${ruleId}_status`);
            const messageElement = document.getElementById(`${ruleId}_message`);
            const lastElement = document.getElementById(`${ruleId}_last`);

            if (statusElement) {
                statusElement.className = `badge ${passed ? 'bg-success' : 'bg-danger'}`;
                statusElement.textContent = passed ? '通过' : '违规';
            }

            if (messageElement) {
                messageElement.textContent = message;
            }

            if (lastElement) {
                const ts = lastCheck || raw?.timestamp || raw?.lastCheck;
                if (ts) {
                    try {
                        lastElement.textContent = new Date(ts).toLocaleString();
                    } catch (_) {
                        lastElement.textContent = ts;
                    }
                }
            }
        }

        resolveStrategySymbol(strategy) {
            if (!strategy || typeof strategy !== 'object') {
                return '';
            }
            const candidates = [
                strategy.primary_symbol,
                strategy.symbol,
                strategy.ticker,
                strategy?.settings?.primary_symbol,
                strategy?.meta?.primary_symbol,
                strategy?.meta?.symbol,
            ];
            for (const candidate of candidates) {
                if (typeof candidate === 'string') {
                    const trimmed = candidate.trim();
                    if (trimmed) {
                        return trimmed.toUpperCase();
                    }
                }
            }
            if (Array.isArray(strategy.symbols)) {
                const match = strategy.symbols.find((item) => typeof item === 'string' && item.trim());
                if (match) {
                    return match.trim().toUpperCase();
                }
            }
            return '';
        }

        formatStrategyDataReception(strategy) {
            const fallback = {
                text: '-',
                tooltip: '',
                active: false,
                awaitingData: false,
                reason: '',
                isReceiving: false,
                subscription: null,
                symbol: '',
                statusLabel: '-',
                toggleDisabled: false,
            };
            if (!strategy || typeof strategy !== 'object') {
                return fallback;
            }

            const info = (strategy.data_reception && typeof strategy.data_reception === 'object')
                ? strategy.data_reception
                : (strategy.data_push_state && typeof strategy.data_push_state === 'object')
                    ? strategy.data_push_state
                    : null;
            const strategyEnabled = typeof strategy.enabled === 'boolean' ? strategy.enabled : true;

            let isReceiving;
            if (typeof strategy.is_receiving_data === 'boolean') {
                isReceiving = strategy.is_receiving_data;
            } else if (info && typeof info.is_receiving === 'boolean') {
                isReceiving = Boolean(info.is_receiving);
            } else {
                isReceiving = false;
            }

            const awaitingData = info && typeof info.awaiting_data !== 'undefined'
                ? Boolean(info.awaiting_data)
                : false;
            const reasonText = (info && info.status_reason)
                || strategy.data_push_reason
                || (strategy.strategy_type && strategy.strategy_type !== 'internal' ? '外部策略' : '');
            const lastTimestamp = info && info.last_data_timestamp ? info.last_data_timestamp : null;
            const isExternal = strategy.strategy_type && strategy.strategy_type !== 'internal';
            const active = info && typeof info.active !== 'undefined'
                ? Boolean(info.active)
                : Boolean(isReceiving);

            let detailText = '';
            if (isReceiving) {
                detailText = reasonText || '数据接收中';
            } else if (awaitingData && !reasonText) {
                detailText = '等待数据';
            } else if (reasonText) {
                detailText = reasonText;
            } else if (isExternal) {
                detailText = '外部策略';
            }

            const tooltipParts = [];
            if (info && typeof info.active !== 'undefined') {
                tooltipParts.push(info.active ? '推送已开启' : '推送未开启');
            }
            if (awaitingData && !isReceiving) {
                tooltipParts.push('正在等待数据');
            }
            if (reasonText) {
                tooltipParts.push(`状态说明：${reasonText}`);
            }
            if (info && info.subscription) {
                tooltipParts.push(`订阅：${info.subscription}`);
            }
            if (info && info.symbol) {
                tooltipParts.push(`标的：${info.symbol}`);
            }
            if (lastTimestamp) {
                tooltipParts.push(`最后数据时间：${lastTimestamp}`);
            }
            const statusLabel = (() => {
                if (!isExternal && active) {
                    if (awaitingData) return '等待DOM数据…';
                    if (reasonText) return reasonText;
                    return '已推送';
                }
                if (reasonText) return reasonText;
                if (isExternal) return '未推送';
                return active ? '已推送' : '未推送';
            })();

            const resolvedSubscription = info && info.subscription
                ? info.subscription
                : (strategy.data_subscription || null);
            const resolvedSymbol = info && info.symbol
                ? info.symbol
                : this.resolveStrategySymbol(strategy);

            const baseInfo = {
                text: detailText || (isReceiving ? '数据接收中' : (active ? '推送已开启' : '未推送')),
                tooltip: tooltipParts.join('；'),
                active,
                awaitingData,
                reason: reasonText || '',
                isReceiving: Boolean(isReceiving),
                subscription: resolvedSubscription || null,
                symbol: resolvedSymbol || '',
                statusLabel,
                toggleDisabled: Boolean(isExternal),
            };

            if (!strategyEnabled) {
                return {
                    ...baseInfo,
                    active: false,
                    awaitingData: false,
                    text: '策略已禁用',
                    statusLabel: '策略已禁用',
                    toggleDisabled: true,
                    reason: baseInfo.reason || '策略已禁用',
                };
            }

            return baseInfo;
        }

        displayStrategies(strategies) {
            const tbody = document.querySelector('#strategiesTable tbody');
            if (!tbody) return;

            tbody.innerHTML = '';
            this.strategyPerformanceCells.clear();
            const visibleStrategies = new Set();
            strategies.forEach(strategy => {
                visibleStrategies.add(strategy.name);
                const row = document.createElement('tr');

                const nameCell = document.createElement('td');
                nameCell.textContent = strategy.name;
                nameCell.style.cursor = 'pointer';
                nameCell.style.color = '#0066cc';
                nameCell.style.textDecoration = 'underline';
                nameCell.addEventListener('click', () => {
                    this.app?.openStrategyDetails?.(strategy.name);
                });

                const symbolCell = document.createElement('td');
                const resolvedSymbol = this.resolveStrategySymbol(strategy);
                symbolCell.textContent = resolvedSymbol || '-';
                if (resolvedSymbol) {
                    symbolCell.title = resolvedSymbol;
                }

                const statusCell = document.createElement('td');
                const toggleSwitch = document.createElement('div');
                toggleSwitch.className = 'form-check form-switch';
                const statusInput = document.createElement('input');
                statusInput.className = 'form-check-input';
                statusInput.type = 'checkbox';
                statusInput.checked = !!strategy.enabled;
                statusInput.setAttribute('aria-label', `${strategy.name} 状态开关`);
                statusInput.addEventListener('change', () => {
                    this.toggleStrategy(strategy.name, statusInput.checked, {
                        controlEl: statusInput,
                        previousState: !statusInput.checked,
                    });
                });
                toggleSwitch.appendChild(statusInput);
                statusCell.appendChild(toggleSwitch);

                const receptionCell = document.createElement('td');
                const receptionInfo = this.formatStrategyDataReception(strategy);
                receptionCell.classList.add('align-middle');
                if (!this.strategyReceptionSwitchSeq) {
                    this.strategyReceptionSwitchSeq = 0;
                }
                const receptionSwitchId = `strategyReceptionSwitch-${++this.strategyReceptionSwitchSeq}`;
                const receptionSwitchWrap = document.createElement('div');
                receptionSwitchWrap.className = 'form-check form-switch mb-1';
                const receptionSwitch = document.createElement('input');
                receptionSwitch.className = 'form-check-input';
                receptionSwitch.type = 'checkbox';
                receptionSwitch.id = receptionSwitchId;
                receptionSwitch.checked = !!receptionInfo.active;
                if (receptionInfo.toggleDisabled) {
                    receptionSwitch.disabled = true;
                    receptionSwitch.title = receptionInfo.reason || '仅内部策略支持数据推送';
                }
                const receptionLabel = document.createElement('label');
                receptionLabel.className = 'form-check-label visually-hidden';
                receptionLabel.setAttribute('for', receptionSwitchId);
                receptionLabel.textContent = '数据推送';
                receptionSwitchWrap.appendChild(receptionSwitch);
                receptionSwitchWrap.appendChild(receptionLabel);
                receptionCell.appendChild(receptionSwitchWrap);

                const receptionDetail = document.createElement('div');
                receptionDetail.className = 'small text-muted';
                receptionDetail.textContent = receptionInfo.text || '-';
                if (receptionInfo.tooltip) {
                    receptionDetail.title = receptionInfo.tooltip;
                }
                receptionCell.appendChild(receptionDetail);

                if (!receptionInfo.toggleDisabled) {
                    receptionSwitch.addEventListener('change', () => {
                        this.toggleStrategyDataReception(strategy, {
                            controlEl: receptionSwitch,
                            statusEl: receptionDetail,
                            enabled: receptionSwitch.checked,
                        });
                    });
                }

                const pnlCell = document.createElement('td');
                const cachedMetrics = this.strategyPerformanceCache.get(strategy.name);
                if (cachedMetrics && typeof cachedMetrics === 'object') {
                    pnlCell.textContent = cachedMetrics.pnl ?? '...';
                } else {
                    pnlCell.textContent = '...';
                }
                const tradesCell = document.createElement('td');
                if (cachedMetrics && typeof cachedMetrics === 'object') {
                    tradesCell.textContent = cachedMetrics.trades ?? '...';
                } else {
                    tradesCell.textContent = '...';
                }

                row.appendChild(nameCell);
                row.appendChild(symbolCell);
                row.appendChild(statusCell);
                row.appendChild(receptionCell);
                row.appendChild(pnlCell);
                row.appendChild(tradesCell);

                tbody.appendChild(row);

                this.strategyPerformanceCells.set(strategy.name, { pnlCell, tradesCell });
                const ibkrManager = this.app?.ibkrManager;
                if (ibkrManager && typeof ibkrManager.requestStrategyPerformance === 'function') {
                    ibkrManager.requestStrategyPerformance({ name: strategy.name, range: '1m' });
                }
            });

            for (const name of Array.from(this.strategyPerformanceCache.keys())) {
                if (!visibleStrategies.has(name)) {
                    this.strategyPerformanceCache.delete(name);
                }
            }
        }

        async toggleStrategy(name, enabled, options = {}) {
            if (!name) {
                return;
            }

            const controlEl = options.controlEl || null;
            const previousState = typeof options.previousState === 'boolean' ? options.previousState : !enabled;

            if (!this.strategyToggleBusy) {
                this.strategyToggleBusy = new Set();
            }
            if (this.strategyToggleBusy.has(name)) {
                if (controlEl) {
                    controlEl.checked = previousState;
                }
                return;
            }

            this.strategyToggleBusy.add(name);
            if (controlEl) {
                controlEl.disabled = true;
            }

            try {
                await this.app.apiCall(`/strategies/${encodeURIComponent(name)}/toggle?enabled=${enabled}`, { method: 'POST' });
                this.app.showAlert('success', `Strategy ${name} ${enabled ? 'enabled' : 'disabled'}`);
                this.loadStrategies({ force: true, minInterval: 0 });
            } catch (error) {
                console.error('Failed to toggle strategy:', error);
                this.app.showAlert('danger', 'Failed to toggle strategy');
                if (controlEl) {
                    controlEl.checked = previousState;
                }
                this.loadStrategies();
            } finally {
                if (controlEl) {
                    controlEl.disabled = false;
                }
                this.strategyToggleBusy.delete(name);
            }
        }

        async toggleStrategyDataReception(strategy, { enabled, controlEl, statusEl } = {}) {
            if (!strategy || !strategy.name) return;
            const name = strategy.name;
            if (!this.strategyDataPushBusy) {
                this.strategyDataPushBusy = new Set();
            }
            if (this.strategyDataPushBusy.has(name)) {
                if (controlEl) {
                    controlEl.checked = !controlEl.checked;
                }
                return;
            }

            const originalInfo = this.formatStrategyDataReception(strategy);
            let finalInfo = originalInfo;
            const revertToOriginal = () => {
                if (controlEl) {
                    controlEl.checked = originalInfo.active;
                    controlEl.disabled = originalInfo.toggleDisabled;
                }
                if (statusEl) {
                    statusEl.textContent = originalInfo.text || '-';
                    statusEl.title = originalInfo.tooltip || '';
                }
            };

            const targetEnabled = typeof enabled === 'boolean'
                ? enabled
                : controlEl
                    ? !!controlEl.checked
                    : !originalInfo.active;

            if (targetEnabled && !strategy.enabled) {
                this.app.showAlert('warning', '启用策略后才能开启数据推送');
                revertToOriginal();
                return;
            }

            const info = (strategy.data_reception && typeof strategy.data_reception === 'object')
                ? strategy.data_reception
                : (strategy.data_push_state && typeof strategy.data_push_state === 'object')
                    ? strategy.data_push_state
                    : {};

            const subscription = info?.subscription
                || strategy.data_subscription
                || strategy.subscription
                || null;
            const symbolCandidate = info?.symbol
                || this.resolveStrategySymbol(strategy)
                || '';
            const symbol = symbolCandidate ? String(symbolCandidate).trim().toUpperCase() : '';

            if (targetEnabled && !subscription) {
                this.app.showAlert('warning', '请选择数据订阅后再开启推送');
                revertToOriginal();
                return;
            }
            if (targetEnabled && !symbol) {
                this.app.showAlert('warning', '请先配置主标的后再开启推送');
                revertToOriginal();
                return;
            }

            this.strategyDataPushBusy.add(name);
            if (controlEl) {
                controlEl.disabled = true;
            }
            if (statusEl) {
                statusEl.textContent = targetEnabled ? '推送中…' : '未推送';
                statusEl.title = '';
            }

            try {
                const res = await this.app.apiCall(`/strategies/${encodeURIComponent(name)}/data-push`, {
                    method: 'POST',
                    body: JSON.stringify({
                        enabled: targetEnabled,
                        subscription: subscription || null,
                        symbol: symbol || null,
                    }),
                });

                if (!res || typeof res !== 'object') {
                    throw new Error('Empty response');
                }

                const state = res.state || {};
                if (!res.success) {
                    this.app.showAlert('danger', res.error || 'Failed to update data push state');
                    finalInfo = originalInfo;
                    return;
                }

                const awaitingData = !!state.awaiting_data && !!state.active;
                strategy.data_reception = {
                    ...(strategy.data_reception || {}),
                    active: !!state.active,
                    awaiting_data: awaitingData,
                    status_reason: state.status_reason || null,
                    subscription: state.subscription || subscription || null,
                    symbol: state.symbol || symbol || null,
                    last_data_timestamp: state.last_data_timestamp
                        || (strategy.data_reception?.last_data_timestamp ?? null),
                };
                if (state.active && !awaitingData && !state.status_reason) {
                    strategy.is_receiving_data = true;
                } else if (!state.active) {
                    strategy.is_receiving_data = false;
                }
                if (state.status_reason) {
                    strategy.data_push_reason = state.status_reason;
                } else if (!state.active) {
                    strategy.data_push_reason = '';
                }

                finalInfo = this.formatStrategyDataReception(strategy);
                this.loadStrategies({ force: true });
            } catch (error) {
                console.error('Failed to update strategy data push state from list', error);
                this.app.showAlert('danger', 'Failed to update data push state');
                finalInfo = originalInfo;
            } finally {
                if (controlEl) {
                    controlEl.disabled = finalInfo.toggleDisabled;
                    controlEl.checked = finalInfo.active;
                }
                if (statusEl) {
                    statusEl.textContent = finalInfo.text || '-';
                    statusEl.title = finalInfo.tooltip || '';
                }
                this.strategyDataPushBusy.delete(name);
            }
        }

        async editStrategy(name) {
            try {
                const res = await this.app.apiCall(`/strategies/${encodeURIComponent(name)}`);
                if (!res.success) throw new Error('Failed to load strategy');
                const s = res.strategy;
                const nameEl = document.getElementById('editStrategyName');
                if (nameEl) nameEl.textContent = s.name;
                const desc = document.getElementById('editStrategyDescriptionInput');
                const api = document.getElementById('editStrategyApiInput');
                if (desc) desc.value = s.description || '';
                if (api) api.value = s.api_endpoint || '';

                const sltpGroup = document.getElementById('editSlTpGroup');
                const subscriptionGroup = document.getElementById('editSubscriptionGroup');
                const positionTimingGroup = document.getElementById('editPositionTimingGroup');
                if (sltpGroup) sltpGroup.style.display = s.strategy_type === 'internal' ? '' : 'none';
                if (subscriptionGroup) subscriptionGroup.style.display = s.strategy_type === 'internal' ? '' : 'none';
                if (positionTimingGroup) positionTimingGroup.style.display = s.strategy_type === 'internal' ? '' : 'none';
                const parameterGroup = document.getElementById('editParameterGroup');
                const parameterList = document.getElementById('editParameterList');
                this.strategyParameterInputs = {};
                if (parameterGroup && parameterList) {
                    parameterList.innerHTML = '';
                    const schemaSource = Array.isArray(s.parameters) && s.parameters.length
                        ? s.parameters
                        : (Array.isArray(s.configurable_parameters) ? s.configurable_parameters : []);
                    const schema = Array.isArray(schemaSource) ? schemaSource : [];
                    if (s.strategy_type === 'internal' && schema.length) {
                        parameterGroup.style.display = '';
                        schema.forEach((param) => {
                            if (!param?.name) return;
                            const wrapper = document.createElement('div');
                            wrapper.className = 'border rounded p-2';
                            const controlId = `editParam-${param.name}`;
                            const label = document.createElement('label');
                            label.className = 'form-label small mb-1';
                            label.setAttribute('for', controlId);
                            label.textContent = param.label || param.name;
                            wrapper.appendChild(label);

                            let inputEl;
                            if (param.type === 'bool') {
                                const switchWrap = document.createElement('div');
                                switchWrap.className = 'form-check form-switch';
                                const checkbox = document.createElement('input');
                                checkbox.className = 'form-check-input';
                                checkbox.type = 'checkbox';
                                checkbox.id = controlId;
                                checkbox.checked = param.value !== undefined ? Boolean(param.value) : Boolean(param.default);
                                const textLabel = document.createElement('label');
                                textLabel.className = 'form-check-label';
                                textLabel.setAttribute('for', controlId);
                                textLabel.textContent = 'Enabled';
                                switchWrap.appendChild(checkbox);
                                switchWrap.appendChild(textLabel);
                                wrapper.appendChild(switchWrap);
                                inputEl = checkbox;
                            } else {
                                const input = document.createElement('input');
                                input.className = 'form-control form-control-sm';
                                input.type = 'number';
                                input.id = controlId;
                                const stepValue = param.step !== undefined ? param.step : (param.type === 'int' ? 1 : 0.1);
                                if (stepValue !== undefined && stepValue !== null) input.step = stepValue;
                                if (param.min !== undefined && param.min !== null) input.min = param.min;
                                if (param.max !== undefined && param.max !== null) input.max = param.max;
                                const initial = param.value !== undefined && param.value !== null
                                    ? param.value
                                    : (param.allow_null ? '' : (param.default ?? ''));
                                input.value = initial !== null && initial !== undefined ? initial : '';
                                input.required = !param.allow_null;
                                wrapper.appendChild(input);
                                inputEl = input;
                            }

                            const help = document.createElement('div');
                            help.className = 'form-text';
                            const helpParts = [];
                            if (param.default !== undefined && param.default !== null) helpParts.push(`默认 ${param.default}`);
                            const rangeParts = [];
                            if (param.min !== undefined && param.min !== null) rangeParts.push(`≥ ${param.min}`);
                            if (param.max !== undefined && param.max !== null) rangeParts.push(`≤ ${param.max}`);
                            if (rangeParts.length) helpParts.push(rangeParts.join('，'));
                            if (param.allow_null) helpParts.push('可留空');
                            if (param.description) helpParts.push(param.description);
                            help.textContent = helpParts.join('； ') || '可调整参数';
                            wrapper.appendChild(help);

                            parameterList.appendChild(wrapper);
                            this.strategyParameterInputs[param.name] = { input: inputEl, meta: param };
                        });
                    } else {
                        parameterGroup.style.display = 'none';
                    }
                }
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
                const editDataSubscription = document.getElementById('editDataSubscriptionSelect');
                if (editDataSubscription) editDataSubscription.value = s.data_subscription || '';
                const editCooldownInput = document.getElementById('editPositionAddCooldownInput');
                if (editCooldownInput) editCooldownInput.value = s.position_add_cooldown_minutes ?? '';
                const editCloseInput = document.getElementById('editCloseBeforeCloseInput');
                if (editCloseInput) editCloseInput.value = s.close_before_close_minutes ?? '';

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
                            const dataSub = document.getElementById('editDataSubscriptionSelect')?.value || '';
                            updates.data_subscription = dataSub || null;
                            const coolRaw = document.getElementById('editPositionAddCooldownInput')?.value;
                            const closeRaw = document.getElementById('editCloseBeforeCloseInput')?.value;
                            const coolVal = coolRaw !== undefined && coolRaw !== '' ? parseInt(coolRaw, 10) : null;
                            const closeVal = closeRaw !== undefined && closeRaw !== '' ? parseInt(closeRaw, 10) : null;
                            updates.position_add_cooldown_minutes = Number.isFinite(coolVal) ? coolVal : null;
                            updates.close_before_close_minutes = Number.isFinite(closeVal) ? closeVal : null;
                            const parameterUpdates = {};
                            let validationError = null;
                            if (this.strategyParameterInputs) {
                                Object.entries(this.strategyParameterInputs).forEach(([paramName, ctx]) => {
                                    if (validationError) return;
                                    const { input, meta } = ctx;
                                    let value;
                                    if (meta.type === 'bool') {
                                        value = !!input.checked;
                                    } else {
                                        const raw = input.value;
                                        if (raw === '' || raw === null || raw === undefined) {
                                            value = meta.allow_null ? null : (meta.default ?? null);
                                        } else if (meta.type === 'int') {
                                            value = parseInt(raw, 10);
                                        } else if (meta.type === 'float') {
                                            value = parseFloat(raw);
                                        } else {
                                            value = raw;
                                        }
                                        if ((meta.type === 'int' || meta.type === 'float')) {
                                            if (value === null && meta.allow_null) {
                                                // ok
                                            } else if (!Number.isFinite(value)) {
                                                validationError = `参数 ${meta.label || paramName} 无效`;
                                                return;
                                            }
                                        }
                                    }
                                    if ((value === null || value === undefined) && !meta.allow_null && meta.type !== 'bool') {
                                        validationError = `参数 ${meta.label || paramName} 必须填写`;
                                        return;
                                    }
                                    parameterUpdates[paramName] = value;
                                });
                            }
                            if (validationError) {
                                this.app.showAlert('danger', validationError);
                                return;
                            }
                            if (Object.keys(parameterUpdates).length) {
                                updates.parameters = parameterUpdates;
                            }
                        }
                        try {
                            await this.app.apiCall(`/strategies/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(updates) });
                            this.app.showAlert('success', `Strategy ${name} updated`);
                            const modalEl = document.getElementById('editStrategyModal');
                            const inst = bootstrap.Modal.getInstance(modalEl);
                            if (inst) inst.hide();
                            this.loadStrategies();
                        } catch (e) {
                            console.error(e);
                            this.app.showAlert('danger', 'Failed to update strategy');
                        }
                    });
                }
            } catch (e) {
                console.error('Edit strategy error:', e);
                this.app.showAlert('danger', 'Failed to open edit modal');
            }
        }

        async deleteStrategy(name) {
            if (!confirm(`Are you sure you want to delete strategy "${name}"?`)) {
                return;
            }

            try {
                await this.app.apiCall(`/strategies/${encodeURIComponent(name)}`, { method: 'DELETE' });
                this.app.showAlert('success', `Strategy ${name} deleted`);
                this.loadStrategies();
            } catch (error) {
                console.error('Failed to delete strategy:', error);
                this.app.showAlert('danger', 'Failed to delete strategy');
            }
        }

        async loadStrategySettings() {
            try {
                const data = await this.app.apiCall('/strategy/settings');
                const s = data.settings || {};
                const el1 = document.getElementById('strategyApiEndpoint');
                if (!el1) return;
                document.getElementById('strategyApiEndpoint').value = s.api_endpoint || '/api/strategy';
                document.getElementById('strategyApiMethod').value = s.api_method || 'POST';
                document.getElementById('strategyApiPayload').value = JSON.stringify(s.api_payload_template || {
                    strategy: '{{strategy_name}}', symbol: '{{symbol}}', action: '{{action}}', qty: '{{quantity}}', type: '{{order_type}}', price: '{{price}}'
                }, null, 2);
                document.getElementById('strategyPath').value = s.strategy_path || 'src/strategies/';
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
                try { api_payload_template = JSON.parse(payloadText); } catch (e) { this.app.showAlert('danger', 'Invalid JSON for payload template'); return; }
                const strategy_path = document.getElementById('strategyPath').value.trim() || 'src/strategies/';
                const enable_auth = document.getElementById('enableStrategyAuth').checked;
                const auth_token = document.getElementById('strategyAuthToken').value || null;
                await this.app.apiCall('/strategy/settings', {
                    method: 'POST',
                    body: JSON.stringify({ api_endpoint, api_method, api_payload_template, strategy_path, enable_auth, auth_token })
                });
                this.app.showAlert('success', 'Strategy settings saved');
                const modalEl = document.getElementById('strategySettingsModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();
            } catch (error) {
                console.error('Failed to save strategy settings:', error);
                this.app.showAlert('danger', 'Failed to save strategy settings');
            }
        }
    }

    managerNs.StrategiesManager = StrategiesManager;
    root.StrategiesManager = StrategiesManager;
})(typeof window !== 'undefined' ? window : globalThis);
