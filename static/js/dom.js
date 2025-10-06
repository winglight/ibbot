(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const domNamespace = appModules.dom = appModules.dom || {};
    const pluginNamespace = domNamespace.plugins = domNamespace.plugins || {};
    const managerNamespace = domNamespace.manager = domNamespace.manager || {};

    const DEFAULT_TREND_THRESHOLDS = [0.5, 0.7, 0.85, 0.95];

    const EVENT_SIDE_COLORS = {
        buy: '#22c55e',
        sell: '#ef4444',
        neutral: '#f97316',
    };

    const domEventMarkerPlugin = {
        id: 'domEventMarkers',
        afterDatasetsDraw(chart) {
            const { ctx } = chart;
            if (!ctx) return;
            chart.data.datasets.forEach((dataset, datasetIndex) => {
                if (!dataset || !dataset.domEvents) return;
                const meta = chart.getDatasetMeta(datasetIndex);
                if (!meta || !meta.data) return;
                meta.data.forEach((element, index) => {
                    const raw = dataset.data?.[index];
                    if (!element || !raw) return;
                    const { x, y } = element.getProps(['x', 'y'], true);
                    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
                    const code = typeof raw.code === 'string' ? raw.code : '?';
                    const color = raw.color || EVENT_SIDE_COLORS.neutral;
                    ctx.save();
                    ctx.fillStyle = color;
                    ctx.font = 'bold 12px "Inter", "Helvetica", sans-serif';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText(code, x, y);
                    ctx.restore();
                });
            });
        },
    };

    if (typeof window !== 'undefined') {
        window.domEventMarkerPlugin = domEventMarkerPlugin;
    }

    pluginNamespace.domEventMarkerPlugin = domEventMarkerPlugin;
    root.domEventMarkerPlugin = domEventMarkerPlugin;

    class DOMManager {
        constructor(app) {
            this.app = app;
            this.ws = null;
            this.symbol = '';
            this.numRows = 5;
            this.metricsChart = null;
            this.metricsChartEl = null;
            this.lastTrendLevel = 0;
            this.lastTrendDir = 0;
            this.startPromise = null;
            this.toggleButton = null;
            this.active = false;
            this.shouldReconnect = false;
            this.trendThresholds = [...DEFAULT_TREND_THRESHOLDS];
            this.init();
        }

        async init() {
            // Bind UI elements
            const settingsBtn = document.getElementById('domSettingsBtn');
            this.toggleButton = document.getElementById('monitorDomToggleBtn');
            const saveBtn = document.getElementById('saveDomSettingsBtn');
            if (settingsBtn) {
                settingsBtn.addEventListener('click', () => {
                    const manager = window.sidePanelManager;
                    if (manager?.openConfiguration) {
                        manager.openConfiguration({ focus: '#domSettingsCard' });
                        return;
                    }

                    const modalEl = document.getElementById('domSettingsModal');
                    if (!modalEl) return;
                    if (modalEl.parentElement) {
                        document.body.appendChild(modalEl);
                    }
                    const modal = (bootstrap?.Modal?.getOrCreateInstance)
                        ? bootstrap.Modal.getOrCreateInstance(modalEl)
                        : new bootstrap.Modal(modalEl);
                    modal.show();
                });
            }
            if (this.toggleButton) {
                this.toggleButton.addEventListener('click', () => this.toggle());
                this.updateToggleButton();
            }
            if (saveBtn) saveBtn.addEventListener('click', () => this.saveSettings());
            this.metricsChartEl = document.getElementById('domMetricsChart');
            if (this.metricsChartEl) {
                this.initCharts();
            }
            this.applyTrendThresholds(this.trendThresholds);
            await this.loadConfig();
        }

        computeTrendSignal(score) {
            const value = typeof score === 'number' ? score : null;
            if (value === null || Number.isNaN(value)) {
                return { level: 0, suggestion: null };
            }
            const absScore = Math.abs(value);
            const thresholds = (Array.isArray(this.trendThresholds) && this.trendThresholds.length)
                ? this.trendThresholds
                : DEFAULT_TREND_THRESHOLDS;
            let level = 0;
            for (let i = 0; i < thresholds.length; i += 1) {
                const threshold = Number(thresholds[i]);
                if (!Number.isFinite(threshold) || threshold <= 0) continue;
                if (absScore >= threshold) {
                    level = i + 1;
                }
            }
            if (!level) return { level: 0, suggestion: null };
            return { level, suggestion: value > 0 ? 'buy' : 'sell' };
        }

        buildTrendReason(metrics, suggestion, level) {
            const lines = [];
            if (metrics && typeof metrics.trend_score === 'number') {
                const trendLine = level > 0
                    ? `Trend Score ${metrics.trend_score.toFixed(2)} (Level ${level})`
                    : `Trend Score ${metrics.trend_score.toFixed(2)}`;
                lines.push(trendLine);
            }
            if (metrics && typeof metrics.obi5 === 'number') {
                const obiState = metrics.obi5 > 0.6 ? '买盘主导'
                    : metrics.obi5 < 0.4 ? '卖盘压力'
                        : '买卖均衡';
                lines.push(`OBI5 ${metrics.obi5.toFixed(2)} (${obiState})`);
            }
            if (metrics && typeof metrics.ofi === 'number') {
                const ofiState = metrics.ofi > 0 ? '买单流入'
                    : metrics.ofi < 0 ? '卖单流出'
                        : '成交均衡';
                lines.push(`OFI ${metrics.ofi.toFixed(0)} (${ofiState})`);
            }
            if (metrics && typeof metrics.zscore_obi5 === 'number' && Math.abs(metrics.zscore_obi5) >= 1) {
                const state = metrics.zscore_obi5 > 0 ? '显著偏多' : '显著偏空';
                lines.push(`Z-OBI ${metrics.zscore_obi5.toFixed(1)} (${state})`);
            }
            if (metrics && typeof metrics.zscore_ofi === 'number' && Math.abs(metrics.zscore_ofi) >= 1) {
                const state = metrics.zscore_ofi > 0 ? '积极买单' : '积极卖单';
                lines.push(`Z-OFI ${metrics.zscore_ofi.toFixed(1)} (${state})`);
            }
            if (Array.isArray(metrics?.events) && metrics.events.length) {
                const tags = metrics.events
                    .map((evt) => {
                        if (!evt || typeof evt !== 'object') return null;
                        if (typeof evt.label === 'string' && evt.label.trim()) return evt.label.trim();
                        if (typeof evt.code === 'string' && evt.code.trim()) return evt.code.trim();
                        if (typeof evt.key === 'string' && evt.key.trim()) return evt.key.trim();
                        return null;
                    })
                    .filter((value, index, arr) => value && arr.indexOf(value) === index);
                if (tags.length) {
                    lines.push(`事件 ${tags.join(' / ')}`);
                }
            }
            if (typeof metrics?.fake_breakout_prob === 'number' && Number.isFinite(metrics.fake_breakout_prob)) {
                const pct = Math.round(metrics.fake_breakout_prob * 100);
                lines.push(`假突破概率 ${pct}%`);
            }
            if (suggestion) {
                lines.push(suggestion === 'buy' ? '触发做多信号' : '触发做空信号');
            }
            if (!lines.length) {
                lines.push('指标变化有限，保持观察');
            }
            return lines.join('\n');
        }

        toNumber(value) {
            if (value === null || value === undefined || value === '') return null;
            if (typeof value === 'number') {
                return Number.isFinite(value) ? value : null;
            }
            const parsed = Number(value);
            return Number.isFinite(parsed) ? parsed : null;
        }

        resolveSignalFromThresholds(obi, ofi, thresholds) {
            const safeThresholds = thresholds && typeof thresholds === 'object' ? thresholds : {};
            const longCfg = safeThresholds.long && typeof safeThresholds.long === 'object' ? safeThresholds.long : {};
            const shortCfg = safeThresholds.short && typeof safeThresholds.short === 'object' ? safeThresholds.short : {};
            const longObi = this.toNumber(longCfg.obi ?? longCfg.obi5);
            const shortObi = this.toNumber(shortCfg.obi ?? shortCfg.obi5);
            const longOfi = this.toNumber(longCfg.ofi);
            const shortOfi = this.toNumber(shortCfg.ofi);

            let signal = 0;
            if (Number.isFinite(obi) && Number.isFinite(ofi)) {
                if (Number.isFinite(longObi) && Number.isFinite(longOfi) && obi >= longObi && ofi >= longOfi) {
                    signal = 1;
                } else if (Number.isFinite(shortObi) && Number.isFinite(shortOfi) && obi <= shortObi && ofi <= shortOfi) {
                    signal = -1;
                }
            }

            return {
                signal,
                longObi,
                shortObi,
                longOfi,
                shortOfi,
            };
        }

        resolveEventColor(event) {
            if (!event || typeof event !== 'object') return EVENT_SIDE_COLORS.neutral;
            const explicit = typeof event.color === 'string' ? event.color : null;
            if (explicit) return explicit;
            const side = typeof event.side === 'string' ? event.side.toLowerCase() : '';
            if (side === 'buy') return EVENT_SIDE_COLORS.buy;
            if (side === 'sell') return EVENT_SIDE_COLORS.sell;
            if (typeof event.key === 'string') {
                if (event.key.includes('buy')) return EVENT_SIDE_COLORS.buy;
                if (event.key.includes('sell')) return EVENT_SIDE_COLORS.sell;
            }
            return EVENT_SIDE_COLORS.neutral;
        }

        buildMonitorPointFromMetrics(entry) {
            if (!entry || typeof entry !== 'object') return null;
            const ts = this.extractTimestamp(entry);
            if (!Number.isFinite(ts)) return null;

            const trendScore = this.toNumber(entry.trend_score);
            const obi = this.toNumber(entry.obi5 ?? entry.obi);
            const ofi = this.toNumber(entry.ofi);
            const thresholds = entry.thresholds && typeof entry.thresholds === 'object'
                ? entry.thresholds
                : null;

            const { level: trendLevel, suggestion: trendSuggestion } = this.computeTrendSignal(trendScore);
            const reason = this.buildTrendReason(entry, trendSuggestion, trendLevel);
            const { signal, longObi, shortObi, longOfi, shortOfi } = this.resolveSignalFromThresholds(obi, ofi, thresholds);

            let suggestion = trendSuggestion;
            if (signal > 0) suggestion = 'buy';
            else if (signal < 0) suggestion = 'sell';

            const price = this.toNumber(entry.mid_price ?? entry.price ?? entry.signal_price);

            const events = Array.isArray(entry.events)
                ? entry.events.map(evt => (evt && typeof evt === 'object' ? { ...evt } : evt))
                : [];
            const rawFeatureFlags = entry.feature_flags ?? entry.featureFlags;
            const featureFlags = rawFeatureFlags && typeof rawFeatureFlags === 'object'
                ? { ...rawFeatureFlags }
                : null;
            const fakeBreakoutProb = this.toNumber(entry.fake_breakout_prob ?? entry.fakeBreakoutProb);

            return {
                x: ts,
                timestamp: ts,
                signal,
                score: trendScore,
                trend_score: trendScore,
                level: trendLevel,
                suggestion,
                reason,
                price,
                obi,
                ofi,
                thresholds: {
                    long: { obi: longObi, ofi: longOfi },
                    short: { obi: shortObi, ofi: shortOfi },
                },
                events,
                featureFlags,
                feature_flags: featureFlags,
                fake_breakout_prob: fakeBreakoutProb,
            };
        }

        resolveTrendColors(info) {
            const { level, suggestion } = info || {};
            const style = getTrendLevelStyle(level || 0);
            if (suggestion === 'buy') {
                return {
                    pointColor: style.buyColor || '#60a5fa',
                    barColor: style.barBuyColor || 'rgba(34,197,94,0.35)'
                };
            }
            if (suggestion === 'sell') {
                return {
                    pointColor: style.sellColor || '#f87171',
                    barColor: style.barSellColor || 'rgba(239,68,68,0.35)'
                };
            }
            return {
                pointColor: style.neutralColor || '#94a3b8',
                barColor: style.barNeutralColor || 'rgba(148,163,184,0.25)'
            };
        }

        extractTimestamp(data) {
            if (!data || typeof data !== 'object') return null;
            const candidates = [
                data.timestamp,
                data.timestamp_ms,
                data.timestamp_ns,
                data.ts_ns,
                data.time,
            ];
            for (const value of candidates) {
                if (typeof value === 'number' && Number.isFinite(value)) {
                    if (value > 1e15) return Math.floor(value / 1e6);
                    if (value > 1e12) return value;
                    if (value > 1e9) return Math.floor(value / 1e3);
                    return value * 1000;
                }
                if (typeof value === 'string') {
                    const parsed = Date.parse(value);
                    if (!Number.isNaN(parsed)) return parsed;
                }
            }
            return null;
        }

        canStart() {
            const monitorSymbol = this.app?.currentMonitor?.symbol;
            return typeof monitorSymbol === 'string' && monitorSymbol.trim().length > 0;
        }

        setToggleBusy(isBusy) {
            if (!this.toggleButton) return;
            this.toggleButton.dataset.busy = isBusy ? '1' : '0';
            if (isBusy) {
                this.toggleButton.disabled = true;
            } else {
                this.toggleButton.disabled = !this.active && !this.canStart();
            }
        }

        updateToggleButton() {
            if (!this.toggleButton) return;
            const isBusy = this.toggleButton.dataset?.busy === '1';
            if (this.active) {
                this.toggleButton.classList.remove('btn-outline-success');
                this.toggleButton.classList.add('btn-outline-danger');
                this.toggleButton.innerHTML = '<i class="fas fa-stop"></i> 停止DOM';
            } else {
                this.toggleButton.classList.add('btn-outline-success');
                this.toggleButton.classList.remove('btn-outline-danger');
                this.toggleButton.innerHTML = '<i class="fas fa-play"></i> 启动DOM';
            }
            if (!isBusy) {
                this.toggleButton.disabled = !this.active && !this.canStart();
            }
        }

        async handleMonitorSymbolChange(symbol) {
            const trimmed = typeof symbol === 'string' ? symbol.trim() : '';
            if (!trimmed) {
                if (!this.active) {
                    this.symbol = '';
                }
                this.updateToggleButton();
                return;
            }
            if (this.active && this.symbol && this.symbol !== trimmed) {
                await this.stop(this.symbol, { silent: true });
            }
            this.symbol = trimmed;
            this.updateToggleButton();
            try {
                await this.loadCachedData(trimmed, { replaceExisting: true });
            } catch (error) {
                console.error('Failed to preload DOM history:', error);
            }
        }

        async toggle() {
            if (this.active) {
                await this.stop();
            } else {
                await this.start();
            }
        }

        setNumberInputValue(elementId, value) {
            const element = document.getElementById(elementId);
            if (!element) return null;
            const numeric = this.toNumber(value);
            if (Number.isFinite(numeric)) {
                element.value = String(Math.trunc(numeric));
            } else {
                element.value = '';
            }
            return element;
        }

        async loadConfig() {
            try {
                const response = await this.app.apiCall('/config/dom', { allowReadOnly: true });
                const config = response && typeof response === 'object'
                    ? (response.config && typeof response.config === 'object' ? response.config : response)
                    : null;
                if (!config) return;

                const windowValue = this.toNumber(config.window ?? config.processor_window);
                const maxLevelsValue = this.toNumber(config.max_levels ?? config.maxLevels);
                const numRowsValue = this.toNumber(config.num_rows ?? config.numRows);
                const thresholds = Array.isArray(config.trend_thresholds)
                    ? config.trend_thresholds
                    : Array.isArray(config.trendThresholds)
                        ? config.trendThresholds
                        : null;

                this.setNumberInputValue('domWindow', windowValue);
                this.setNumberInputValue('domMaxLevels', maxLevelsValue);

                const numRowsEl = document.getElementById('domNumRows');
                if (numRowsEl) {
                    if (Number.isFinite(numRowsValue) && numRowsValue > 0) {
                        this.numRows = Math.max(1, Math.min(30, Math.trunc(numRowsValue)));
                        numRowsEl.value = String(this.numRows);
                    } else if (!numRowsEl.value) {
                        numRowsEl.value = String(this.numRows);
                    }
                }

                if (thresholds) {
                    this.applyTrendThresholds(thresholds);
                }
            } catch (e) {
                console.error('Failed to load DOM config', e);
            }
        }

        async saveSettings() {
            const windowEl = document.getElementById('domWindow');
            const maxLevelsEl = document.getElementById('domMaxLevels');
            const numRowsEl = document.getElementById('domNumRows');
            const thresholds = this.getTrendThresholdsFromForm();
            const window = parseInt(windowEl.value, 10) || 0;
            const maxLevels = parseInt(maxLevelsEl.value, 10) || 0;
            this.numRows = parseInt(numRowsEl.value, 10) || 5;
            this.trendThresholds = thresholds;
            try {
                await this.app.apiCall('/config/dom', {
                    method: 'PUT',
                    body: JSON.stringify({
                        window: window,
                        max_levels: maxLevels,
                        trend_thresholds: thresholds
                    })
                });
                const card = document.getElementById('domSettingsCard');
                if (card) {
                    card.classList.add('panel-focus-highlight');
                    setTimeout(() => card.classList.remove('panel-focus-highlight'), 1400);
                }
                this.app.showAlert('success', 'DOM settings saved');
            } catch (e) {
                console.error('Failed to save DOM settings', e);
                this.app.showAlert('danger', 'Failed to save DOM settings');
            }
        }

        getTrendThresholdInputs() {
            return [
                document.getElementById('domTrendThreshold1'),
                document.getElementById('domTrendThreshold2'),
                document.getElementById('domTrendThreshold3'),
                document.getElementById('domTrendThreshold4')
            ];
        }

        normalizeTrendThresholds(rawValues) {
            const source = Array.isArray(rawValues) ? rawValues : DEFAULT_TREND_THRESHOLDS;
            const numeric = [];
            for (const value of source) {
                if (value === null || value === undefined || value === '') continue;
                const parsed = typeof value === 'string' ? parseFloat(value) : Number(value);
                if (Number.isFinite(parsed) && parsed > 0) {
                    numeric.push(parsed);
                }
            }
            numeric.sort((a, b) => a - b);
            const final = [];
            if (!numeric.length) {
                return [...DEFAULT_TREND_THRESHOLDS];
            }
            for (let i = 0; i < numeric.length && final.length < 4; i += 1) {
                const current = numeric[i];
                if (!final.length) {
                    final.push(current);
                } else {
                    const last = final[final.length - 1];
                    final.push(Math.max(current, last));
                }
            }
            while (final.length < 4) {
                const fallback = DEFAULT_TREND_THRESHOLDS[final.length] ?? final[final.length - 1] ?? DEFAULT_TREND_THRESHOLDS[0];
                const last = final[final.length - 1] ?? 0;
                final.push(Math.max(fallback, last));
            }
            if (final.length > 4) {
                final.length = 4;
            }
            return final;
        }

        applyTrendThresholds(rawValues) {
            const normalized = this.normalizeTrendThresholds(rawValues);
            this.trendThresholds = normalized;
            const inputs = this.getTrendThresholdInputs();
            normalized.forEach((value, idx) => {
                const input = inputs[idx];
                if (input) {
                    input.value = Number.isFinite(value) ? value.toFixed(2) : '';
                }
            });
        }

        getTrendThresholdsFromForm() {
            const inputs = this.getTrendThresholdInputs();
            const values = inputs.map(input => (input ? input.value : undefined));
            return this.normalizeTrendThresholds(values);
        }

        async start() {
            if (this.startPromise) return this.startPromise;
            const monitorSymbol = this.app?.currentMonitor?.symbol;
            const targetSymbol = typeof monitorSymbol === 'string' && monitorSymbol.trim().length > 0
                ? monitorSymbol.trim()
                : (this.symbol || '').trim();
            if (!targetSymbol) {
                this.app.showAlert('warning', '请先选择需要监控的标的');
                this.updateToggleButton();
                return;
            }
            this.startPromise = (async () => {
                const previousSymbol = this.symbol;
                if (this.active && previousSymbol && previousSymbol !== targetSymbol) {
                    await this.stop(previousSymbol, { silent: true });
                }
                this.symbol = targetSymbol;
                const numRowsEl = document.getElementById('domNumRows');
                if (numRowsEl) {
                    const parsed = parseInt(numRowsEl.value, 10);
                    if (!Number.isNaN(parsed) && parsed > 0) {
                        this.numRows = parsed;
                    }
                }
                this.trendThresholds = this.getTrendThresholdsFromForm();
                this.resetCharts();
                this.app.domSignals = [];
                if (typeof this.app.updateDomSignalAnnotations === 'function') {
                    this.app.updateDomSignalAnnotations();
                }
                if (typeof this.app.resetMonitorTrendData === 'function') {
                    this.app.resetMonitorTrendData();
                } else if (Array.isArray(this.app.monitorTrendData)) {
                    this.app.monitorTrendData = [];
                    if (this.app.monitorTrendChart?.data?.datasets?.[0]) {
                        const formatted = typeof this.app.getFormattedTrendChartData === 'function'
                            ? this.app.getFormattedTrendChartData()
                            : [];
                        this.app.monitorTrendChart.data.datasets[0].data = formatted;
                        this.app.monitorTrendChart.update('none');
                    }
                }
                try {
                    await this.loadCachedData(this.symbol, { replaceExisting: true });
                } catch (error) {
                    console.error('Failed to refresh cached DOM history on start:', error);
                }
                this.setToggleBusy(true);
                try {
                    await this.app.apiCall('/dom/start', {
                        method: 'POST',
                        body: JSON.stringify({ symbol: this.symbol, num_rows: this.numRows })
                    });
                    this.active = true;
                    this.shouldReconnect = true;
                    this.updateToggleButton();
                    this.connectWS();
                    this.app.showAlert('success', `${this.symbol} DOM 已启动`);
                } catch (e) {
                    console.error('Failed to start DOM', e);
                    this.active = false;
                    this.shouldReconnect = false;
                    this.app.showAlert('danger', 'Failed to start DOM');
                } finally {
                    this.setToggleBusy(false);
                    this.updateToggleButton();
                }
            })();
            try {
                await this.startPromise;
            } finally {
                this.startPromise = null;
            }
        }

        async stop(symbol = this.symbol, { silent = false } = {}) {
            if (this.startPromise) {
                try { await this.startPromise; } catch (e) {}
            }
            const targetSymbol = typeof symbol === 'string' ? symbol.trim() : '';
            this.setToggleBusy(true);
            this.shouldReconnect = false;
            if (this.ws) {
                try { this.ws.onclose = null; } catch (e) {}
                this.ws.close();
            }
            this.ws = null;
            this.app.domSignals = [];
            if (typeof this.app.updateDomSignalAnnotations === 'function') {
                this.app.updateDomSignalAnnotations();
            }
            if (typeof this.app.resetMonitorTrendData === 'function') {
                this.app.resetMonitorTrendData();
            }
            if (!targetSymbol) {
                this.active = false;
                this.setToggleBusy(false);
                this.updateToggleButton();
                return;
            }
            try {
                await this.app.apiCall('/dom/stop', {
                    method: 'POST',
                    body: JSON.stringify({ symbol: targetSymbol })
                });
                if (!silent) {
                    this.app.showAlert('info', 'DOM stream stopped');
                }
            } catch (e) {
                console.error('Failed to stop DOM', e);
                this.app.showAlert('danger', 'Failed to stop DOM');
            } finally {
                if (targetSymbol === this.symbol) {
                    this.symbol = '';
                }
                this.active = false;
                this.setToggleBusy(false);
                this.updateToggleButton();
            }
        }

        async loadCachedData(symbol, options = {}) {
            const { replaceExisting = true } = options || {};
            const target = typeof symbol === 'string' ? symbol.trim() : '';
            if (!target) return;
            try {
                const response = await this.app.apiCall(`/dom/history/${encodeURIComponent(target)}`);
                if (!response || response.success !== true) return;
                const metricsHistory = Array.isArray(response.metrics) ? response.metrics.slice() : [];
                if (!metricsHistory.length) {
                    if (replaceExisting && typeof this.app.resetMonitorTrendData === 'function') {
                        this.app.resetMonitorTrendData();
                    }
                    return;
                }
                metricsHistory.sort((a, b) => {
                    const ta = this.extractTimestamp(a) || 0;
                    const tb = this.extractTimestamp(b) || 0;
                    return ta - tb;
                });

                const metricWindowMs = 60 * 1000;
                const monitorPoints = [];
                const metricPayload = [];
                metricsHistory.forEach((entry) => {
                    const point = this.buildMonitorPointFromMetrics(entry);
                    if (!point) return;
                    monitorPoints.push(point);
                    metricPayload.push({
                        entry,
                        ts: point.timestamp,
                        level: point.level,
                        suggestion: point.suggestion,
                    });
                });

                if (this.metricsChart && metricPayload.length) {
                    const datasets = this.metricsChart.data.datasets || [];
                    const trendDataset = datasets[0];
                    const eventDataset = datasets[1];
                    if (trendDataset) trendDataset.data = [];
                    if (eventDataset) eventDataset.data = [];

                    const lastTs = metricPayload[metricPayload.length - 1].ts;
                    const cutoff = lastTs - metricWindowMs;
                    const filtered = metricPayload.filter(item => item.ts >= cutoff);

                    filtered.forEach(({ entry, ts }) => {
                        const trend = this.toNumber(entry.trend_score);
                        if (trendDataset) {
                            trendDataset.data.push({ x: ts, y: Number.isFinite(trend) ? trend : null });
                        }
                        if (eventDataset) {
                            const eventList = Array.isArray(entry.events) ? entry.events : [];
                            eventList.forEach((evt) => {
                                if (!evt || typeof evt !== 'object') return;
                                const code = typeof evt.code === 'string' && evt.code.trim()
                                    ? evt.code.trim().slice(0, 2)
                                    : (evt.key ? String(evt.key).charAt(0).toUpperCase() : '?');
                                eventDataset.data.push({
                                    x: ts,
                                    y: Number.isFinite(trend) ? trend : 0,
                                    code,
                                    key: evt.key || null,
                                    label: evt.label || code,
                                    side: evt.side || null,
                                    color: this.resolveEventColor(evt),
                                });
                            });
                        }
                    });

                    if (filtered.length) {
                        const finalTs = filtered[filtered.length - 1].ts;
                        this.metricsChart.options.scales.x.min = finalTs - metricWindowMs;
                        this.metricsChart.options.scales.x.max = finalTs;
                    }
                    this.metricsChart.update('none');
                }

                const trendWindowMs = 60 * 1000;
                const trendMaxPoints = 240;
                const mergeEvents = (existingEvents, incomingEvents) => {
                    const combined = [
                        ...(Array.isArray(existingEvents) ? existingEvents : []),
                        ...(Array.isArray(incomingEvents) ? incomingEvents : []),
                    ];
                    if (!combined.length) return [];
                    const seen = new Set();
                    const result = [];
                    combined.forEach((evt) => {
                        if (!evt || typeof evt !== 'object') return;
                        const code = typeof evt.code === 'string' ? evt.code : '';
                        const key = `${evt.key || code}-${evt.side || ''}`;
                        if (seen.has(key)) return;
                        seen.add(key);
                        result.push({ ...evt });
                    });
                    return result;
                };

                const mergeFeatureFlags = (existingFlags, incomingFlags) => ({
                    ...(existingFlags && typeof existingFlags === 'object' ? existingFlags : {}),
                    ...(incomingFlags && typeof incomingFlags === 'object' ? incomingFlags : {}),
                });

                const trimmedTrendPoints = (() => {
                    if (!monitorPoints.length) return [];
                    const lastTs = monitorPoints[monitorPoints.length - 1].timestamp;
                    if (Number.isFinite(lastTs)) {
                        const cutoff = lastTs - trendWindowMs;
                        const sliced = monitorPoints.filter(point => point.timestamp >= cutoff);
                        if (sliced.length) {
                            return sliced.slice(-trendMaxPoints);
                        }
                    }
                    return monitorPoints.slice(-trendMaxPoints);
                })();

                if (trimmedTrendPoints.length) {
                    if (replaceExisting && typeof this.app.setMonitorTrendHistory === 'function') {
                        this.app.setMonitorTrendHistory(trimmedTrendPoints);
                    } else if (Array.isArray(this.app.monitorTrendData)) {
                        const merged = [...this.app.monitorTrendData];
                        trimmedTrendPoints.forEach(point => {
                            const idx = merged.findIndex(item => item && item.timestamp === point.timestamp);
                            if (idx >= 0) {
                                const existing = merged[idx] || {};
                                const combinedEvents = mergeEvents(existing.events, point.events);
                                const combinedFlags = mergeFeatureFlags(existing.featureFlags || existing.feature_flags, point.featureFlags || point.feature_flags);
                                merged[idx] = {
                                    ...existing,
                                    ...point,
                                    events: combinedEvents,
                                    featureFlags: combinedFlags,
                                    feature_flags: combinedFlags,
                                };
                            } else {
                                merged.push(point);
                            }
                        });
                        merged.sort((a, b) => a.timestamp - b.timestamp);
                        let constrained = merged;
                        const mergedLastTs = merged.length ? merged[merged.length - 1].timestamp : null;
                        if (Number.isFinite(mergedLastTs)) {
                            const cutoff = mergedLastTs - trendWindowMs;
                            constrained = merged.filter(item => item.timestamp >= cutoff).slice(-trendMaxPoints);
                        } else {
                            constrained = merged.slice(-trendMaxPoints);
                        }
                        if (typeof this.app.setMonitorTrendHistory === 'function') {
                            this.app.setMonitorTrendHistory(constrained);
                        } else {
                            this.app.monitorTrendData = constrained;
                        }
                    }

                    const lastPoint = trimmedTrendPoints[trimmedTrendPoints.length - 1];
                    const dir = (lastPoint.signal || 0) > 0 ? 1 : (lastPoint.signal || 0) < 0 ? -1 : 0;
                    this.lastTrendLevel = lastPoint.level || 0;
                    this.lastTrendDir = dir;
                } else if (replaceExisting && typeof this.app.resetMonitorTrendData === 'function') {
                    this.app.resetMonitorTrendData();
                }
            } catch (error) {
                console.error('Failed to load cached DOM data:', error);
            }
        }

        connectWS() {
            if (!this.symbol || !this.shouldReconnect) return;
            if (this.ws) {
                try { this.ws.onclose = null; } catch (e) {}
                this.ws.close();
            }
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            this.ws = new WebSocket(`${proto}://${location.host}/ws/dom/${this.symbol}`);
            this.ws.onmessage = (evt) => {
                try {
                    const data = JSON.parse(evt.data);
                    if (data.metrics && Object.keys(data.metrics).length > 0) {
                        console.log('DOM metrics', data.metrics);
                        this.addMetricPoint(data.metrics);
                    }
                } catch (e) {
                    console.error('Invalid DOM WS message', e);
                }
            };

            this.ws.onclose = () => {
                this.ws = null;
                if (this.shouldReconnect && this.active) {
                    setTimeout(() => this.connectWS(), 1000);
                }
            };
            this.ws.onerror = (err) => {
                console.error('DOM WebSocket error', err);
            };
        }

        initCharts() {
            if (!this.metricsChartEl) return;
            const ctx = this.metricsChartEl.getContext('2d');
            this.metricsChart = new Chart(ctx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: 'Trend Score',
                            data: [],
                            yAxisID: 'trend',
                            fill: false,
                            parsing: false,
                            tension: 0.25,
                            borderWidth: 2,
                            borderColor: '#60a5fa',
                            pointRadius: 3,
                            pointHoverRadius: 4,
                            pointBorderWidth: 0,
                            pointBackgroundColor: (ctxPoint) => {
                                const value = Number(ctxPoint.raw?.y);
                                if (value > 0.05) return EVENT_SIDE_COLORS.buy;
                                if (value < -0.05) return EVENT_SIDE_COLORS.sell;
                                return '#9ca3af';
                            },
                        },
                        {
                            label: '事件',
                            type: 'scatter',
                            data: [],
                            yAxisID: 'trend',
                            parsing: false,
                            showLine: false,
                            pointRadius: 0,
                            pointHitRadius: 10,
                            domEvents: true,
                        },
                    ],
                },
                options: {
                    animation: false,
                    parsing: false,
                    maintainAspectRatio: false,
                    scales: {
                        x: {
                            type: 'time',
                            time: { unit: 'second', tooltipFormat: 'HH:mm:ss' },
                            ticks: { maxRotation: 0 },
                        },
                        trend: {
                            position: 'left',
                            min: -1,
                            max: 1,
                            title: { display: true, text: 'Trend Score' },
                            grid: { color: 'rgba(148, 163, 184, 0.25)' },
                            ticks: { stepSize: 0.5 },
                        },
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            mode: 'nearest',
                            callbacks: {
                                label(context) {
                                    if (context.dataset?.domEvents) {
                                        const raw = context.raw || {};
                                        const label = raw.label || raw.code || '事件';
                                        return `${label}${raw.side ? ` (${raw.side === 'buy' ? '买' : '卖'})` : ''}`;
                                    }
                                    const value = Number(context.parsed?.y);
                                    if (!Number.isFinite(value)) return 'Trend: —';
                                    return `Trend: ${value.toFixed(2)}`;
                                },
                            },
                        },
                    },
                },
                plugins: [domEventMarkerPlugin],
            });
        }

        resetCharts() {
            if (this.metricsChart) {
                this.metricsChart.data.datasets.forEach((dataset) => {
                    dataset.data = [];
                    if (Array.isArray(dataset.pointBackgroundColor)) dataset.pointBackgroundColor = [];
                });
                this.metricsChart.update();
            }
        }

        addMetricPoint(m) {
            const windowMs = 60 * 1000; // keep 1 minute of data to avoid huge time ranges
            const sampleTime = this.extractTimestamp(m) ?? Date.now();
            const obi = this.toNumber(m.obi5 ?? m.obi);
            const ofi = this.toNumber(m.ofi);
            const trendScore = this.toNumber(m.trend_score);
            const fakeBreakoutProb = this.toNumber(m.fake_breakout_prob);
            const events = Array.isArray(m.events) ? m.events.map(evt => ({ ...evt })) : [];

            console.log('[DOM Manager] addMetricPoint - trend_score:', trendScore, 'raw data:', m.trend_score, 'full metric:', m);

            const { level, suggestion } = this.computeTrendSignal(trendScore);
            const metricsPayload = {
                trend_score: trendScore,
                obi5: obi,
                ofi,
                zscore_obi5: this.toNumber(m.zscore_obi5),
                zscore_ofi: this.toNumber(m.zscore_ofi),
                fake_breakout_prob: fakeBreakoutProb,
                events,
            };
            const reason = this.buildTrendReason(metricsPayload, suggestion, level);

            if (this.metricsChart) {
                const datasets = this.metricsChart.data.datasets || [];
                const trendDataset = datasets[0];
                const eventDataset = datasets[1];
                if (trendDataset) {
                    trendDataset.data.push({
                        x: sampleTime,
                        y: Number.isFinite(trendScore) ? trendScore : null,
                    });
                    trendDataset.data = trendDataset.data.filter((point) => point.x >= sampleTime - windowMs);
                }
                if (eventDataset) {
                    events.forEach((evt) => {
                        const baseCode = typeof evt.code === 'string' && evt.code.trim()
                            ? evt.code.trim().slice(0, 2)
                            : (evt.key ? String(evt.key).charAt(0).toUpperCase() : '?');
                        eventDataset.data.push({
                            x: sampleTime,
                            y: Number.isFinite(trendScore) ? trendScore : 0,
                            code: baseCode,
                            key: evt.key || null,
                            label: evt.label || baseCode,
                            side: evt.side || null,
                            color: this.resolveEventColor(evt),
                        });
                    });
                    eventDataset.data = eventDataset.data.filter((point) => point.x >= sampleTime - windowMs);
                }
                this.metricsChart.options.scales.x.min = sampleTime - windowMs;
                this.metricsChart.options.scales.x.max = sampleTime;
                this.metricsChart.update('none');
            }

            const price = this.toNumber(m.mid_price);
            if (trendScore !== null && Number.isFinite(trendScore)) {
                const dir = trendScore > 0 ? 1 : trendScore < 0 ? -1 : 0;
                if (level > 0 && (level !== this.lastTrendLevel || dir !== this.lastTrendDir)) {
                    this.app.addDomSignal({ time: sampleTime, price, score: trendScore, level });
                }
                this.lastTrendLevel = level;
                this.lastTrendDir = dir;

                if (typeof this.app.addMonitorTrendPoint === 'function') {
                    console.log('[DOM Manager] 调用addMonitorTrendPoint - trendScore:', trendScore, 'level:', level, 'suggestion:', suggestion);
                    this.app.addMonitorTrendPoint(sampleTime, trendScore, {
                        reason,
                        suggestion,
                        level,
                        price,
                        windowMs,
                        longBias: suggestion === 'buy',
                        shortBias: suggestion === 'sell',
                        maxPoints: 240,
                        threshold: 0.5,
                        obi,
                        ofi,
                        thresholds: m.thresholds,
                        events,
                        featureFlags: m.feature_flags && typeof m.feature_flags === 'object' ? { ...m.feature_flags } : {},
                        fakeBreakoutProb,
                    });
                } else {
                    console.warn('[DOM Manager] addMonitorTrendPoint方法不存在');
                }
            }
        }
    }


    managerNamespace.DOMManager = DOMManager;
    root.DOMManager = DOMManager;

    window.DOMManager = DOMManager;

})(typeof window !== "undefined" ? window : globalThis);
