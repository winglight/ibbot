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
        this.init();
    }

    async init() {
        // Bind UI elements
        const settingsBtn = document.getElementById('domSettingsBtn');
        this.toggleButton = document.getElementById('monitorDomToggleBtn');
        const saveBtn = document.getElementById('saveDomSettingsBtn');
        if (settingsBtn) {
            settingsBtn.addEventListener('click', () => {
                const modalEl = document.getElementById('domSettingsModal');
                if (!modalEl) return;
                const modal = new bootstrap.Modal(modalEl);
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
        await this.loadConfig();
    }

    computeTrendSignal(score) {
        const value = typeof score === 'number' ? score : null;
        if (value === null || Number.isNaN(value)) {
            return { level: 0, suggestion: null };
        }
        const absScore = Math.abs(value);
        let level = 0;
        if (absScore >= 0.95) level = 4;
        else if (absScore >= 0.85) level = 3;
        else if (absScore >= 0.7) level = 2;
        else if (absScore >= 0.5) level = 1;
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
        if (suggestion) {
            lines.push(suggestion === 'buy' ? '触发做多信号' : '触发做空信号');
        }
        if (!lines.length) {
            lines.push('指标变化有限，保持观察');
        }
        return lines.join('\n');
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

    async loadConfig() {
        try {
            const data = await this.app.apiCall('/config/dom');
            if (data) {
                document.getElementById('domWindow').value = data.window;
                document.getElementById('domMaxLevels').value = data.max_levels;
            }
        } catch (e) {
            console.error('Failed to load DOM config', e);
        }
    }

    async saveSettings() {
        const windowEl = document.getElementById('domWindow');
        const maxLevelsEl = document.getElementById('domMaxLevels');
        const numRowsEl = document.getElementById('domNumRows');
        const window = parseInt(windowEl.value, 10) || 0;
        const maxLevels = parseInt(maxLevelsEl.value, 10) || 0;
        this.numRows = parseInt(numRowsEl.value, 10) || 5;
        try {
            await this.app.apiCall('/config/dom', {
                method: 'PUT',
                body: JSON.stringify({ window: window, max_levels: maxLevels })
            });
            const modalEl = document.getElementById('domSettingsModal');
            if (modalEl) bootstrap.Modal.getInstance(modalEl)?.hide();
            this.app.showAlert('success', 'DOM settings saved');
        } catch (e) {
            console.error('Failed to save DOM settings', e);
            this.app.showAlert('danger', 'Failed to save DOM settings');
        }
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

            const trendPoints = [];
            const metricWindowMs = 60 * 1000;
            const metricPayload = [];
            metricsHistory.forEach((entry) => {
                const ts = this.extractTimestamp(entry);
                if (!Number.isFinite(ts)) return;
                const trendScore = typeof entry.trend_score === 'number' ? entry.trend_score : null;
                const { level, suggestion } = this.computeTrendSignal(trendScore);
                const reason = this.buildTrendReason(entry, suggestion, level);
                trendPoints.push({
                    x: ts,
                    timestamp: ts,
                    y: trendScore ?? 0,
                    reason,
                    suggestion,
                    level,
                    price: typeof entry.mid_price === 'number' && Number.isFinite(entry.mid_price)
                        ? entry.mid_price
                        : undefined
                });
                metricPayload.push({ entry, ts, level, suggestion });
            });

            if (this.metricsChart && metricPayload.length) {
                const ds = this.metricsChart.data.datasets || [];
                const lastTs = metricPayload[metricPayload.length - 1].ts;
                const cutoff = lastTs - metricWindowMs;
                const filtered = metricPayload.filter(item => item.ts >= cutoff);
                if (ds[0]) {
                    ds[0].data = [];
                    ds[0].pointBackgroundColor = [];
                }
                if (ds[1]) {
                    ds[1].data = [];
                    ds[1].backgroundColor = [];
                }
                if (ds[2]) ds[2].data = [];
                if (ds[3]) ds[3].data = [];

                filtered.forEach(({ entry, ts, level, suggestion }) => {
                    const colors = this.resolveTrendColors({ level, suggestion });
                    const obi = typeof entry.obi5 === 'number' ? entry.obi5 : 0;
                    const ofi = typeof entry.ofi === 'number' ? entry.ofi : 0;
                    const zObi = typeof entry.zscore_obi5 === 'number' ? entry.zscore_obi5 : 0;
                    const zOfi = typeof entry.zscore_ofi === 'number' ? entry.zscore_ofi : 0;
                    if (ds[0]) {
                        ds[0].data.push({ x: ts, y: obi });
                        ds[0].pointBackgroundColor.push(colors.pointColor);
                    }
                    if (ds[1]) {
                        ds[1].data.push({ x: ts, y: ofi });
                        ds[1].backgroundColor.push(colors.barColor);
                    }
                    if (ds[2]) ds[2].data.push({ x: ts, y: zObi });
                    if (ds[3]) ds[3].data.push({ x: ts, y: zOfi });
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
            const trimmedTrendPoints = (() => {
                if (!trendPoints.length) return [];
                const lastTs = trendPoints[trendPoints.length - 1].timestamp;
                if (Number.isFinite(lastTs)) {
                    const cutoff = lastTs - trendWindowMs;
                    const sliced = trendPoints.filter(point => point.timestamp >= cutoff);
                    if (sliced.length) {
                        return sliced.slice(-trendMaxPoints);
                    }
                }
                return trendPoints.slice(-trendMaxPoints);
            })();

            if (trimmedTrendPoints.length) {
                if (replaceExisting && typeof this.app.setMonitorTrendHistory === 'function') {
                    this.app.setMonitorTrendHistory(trimmedTrendPoints);
                } else if (Array.isArray(this.app.monitorTrendData)) {
                    const merged = [...this.app.monitorTrendData];
                    trimmedTrendPoints.forEach(point => {
                        const idx = merged.findIndex(item => item && item.timestamp === point.timestamp);
                        if (idx >= 0) {
                            merged[idx] = { ...merged[idx], ...point };
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
                const dir = lastPoint.y > 0 ? 1 : lastPoint.y < 0 ? -1 : 0;
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
                    { label: 'OBI', data: [], borderColor: 'blue', pointBackgroundColor: [], fill: false, yAxisID: 'y' },
                    { type: 'bar', label: 'OFI', data: [], backgroundColor: [], yAxisID: 'y1' },
                    { label: 'Z-OBI', data: [], borderColor: 'green', borderDash: [5,5], pointRadius: 0, fill: false, yAxisID: 'y' },
                    { label: 'Z-OFI', data: [], borderColor: 'red', borderDash: [5,5], pointRadius: 0, fill: false, yAxisID: 'y1' }
                ]
            },
            options: {
                animation: false,
                scales: {
                    x: { type: 'time', time: { unit: 'second' } },
                    y: { position: 'left', beginAtZero: true, title: { display: true, text: 'OBI/Z-OBI' } },
                    y1: { position: 'right', beginAtZero: true, grid: { display: false }, title: { display: true, text: 'OFI/Z-OFI' } }
                }
            }
        });
    }

    resetCharts() {
        if (this.metricsChart) {
            this.metricsChart.data.datasets.forEach(ds => {
                ds.data = [];
                if (ds.pointBackgroundColor) ds.pointBackgroundColor = [];
                if (Array.isArray(ds.backgroundColor)) ds.backgroundColor = [];
            });
            this.metricsChart.update();
        }
    }

    addMetricPoint(m) {
        const windowMs = 60 * 1000; // keep 1 minute of data to avoid huge time ranges
        const sampleTime = this.extractTimestamp(m) ?? Date.now();
        const obi = typeof m.obi5 === 'number' ? m.obi5 : null;
        const ofi = typeof m.ofi === 'number' ? m.ofi : null;
        const trendScore = typeof m.trend_score === 'number' ? m.trend_score : null;

        // 调试日志：检查trend_score数据
        console.log('[DOM Manager] addMetricPoint - trend_score:', trendScore, 'raw data:', m.trend_score, 'full metric:', m);

        const { level, suggestion } = this.computeTrendSignal(trendScore);
        const metricsPayload = {
            trend_score: trendScore,
            obi5: obi,
            ofi,
            zscore_obi5: typeof m.zscore_obi5 === 'number' ? m.zscore_obi5 : null,
            zscore_ofi: typeof m.zscore_ofi === 'number' ? m.zscore_ofi : null
        };
        const reason = this.buildTrendReason(metricsPayload, suggestion, level);
        const colors = this.resolveTrendColors({ level, suggestion, trendScore });

        if (this.metricsChart) {
            const ds = this.metricsChart.data.datasets;
            ds[0].data.push({ x: sampleTime, y: obi ?? 0 });
            ds[0].pointBackgroundColor.push(colors.pointColor);
            ds[1].data.push({ x: sampleTime, y: ofi ?? 0 });
            ds[1].backgroundColor.push(colors.barColor);
            ds[2].data.push({ x: sampleTime, y: metricsPayload.zscore_obi5 ?? 0 });
            ds[3].data.push({ x: sampleTime, y: metricsPayload.zscore_ofi ?? 0 });
            // prune old points and colors
            ds.forEach((dataset) => {
                dataset.data = dataset.data.filter(p => p.x >= sampleTime - windowMs);
                if (dataset.pointBackgroundColor) {
                    dataset.pointBackgroundColor = dataset.pointBackgroundColor.slice(-dataset.data.length);
                }
                if (Array.isArray(dataset.backgroundColor)) {
                    dataset.backgroundColor = dataset.backgroundColor.slice(-dataset.data.length);
                }
            });
            this.metricsChart.options.scales.x.min = sampleTime - windowMs;
            this.metricsChart.options.scales.x.max = sampleTime;
            this.metricsChart.update('none');
        }
        const price = typeof m.mid_price === 'number' && Number.isFinite(m.mid_price) ? m.mid_price : 0;
        if (trendScore !== null) {
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
                    threshold: 0.5
                });
            } else {
                console.warn('[DOM Manager] addMonitorTrendPoint方法不存在');
            }
        }
    }
}


window.DOMManager = DOMManager;

