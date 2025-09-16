class DOMManager {
    constructor(app) {
        this.app = app;
        this.ws = null;
        this.symbol = '';
        this.numRows = 5;
        this.metricsChart = null;
        this.priceChart = null;
        this.metricsChartEl = null;
        this.priceChartEl = null;
        this.priceData = [];
        this.volumeData = [];
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
        this.priceChartEl = document.getElementById('domPriceChart');
        if (this.metricsChartEl && this.priceChartEl) {
            this.initCharts();
        }
        await this.loadConfig();
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
                    this.app.monitorTrendChart.data.datasets[0].data = this.app.monitorTrendData;
                    this.app.monitorTrendChart.update('none');
                }
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
        const pctx = this.priceChartEl.getContext('2d');
        this.priceChart = new Chart(pctx, {
            data: {
                datasets: [
                    { type: 'line', label: 'Price', data: [], yAxisID: 'y', borderColor: '#4caf50', pointRadius: 0, fill: false },
                    { type: 'bar', label: 'Volume', data: [], yAxisID: 'y1', backgroundColor: 'rgba(100,100,200,0.3)' }
                ]
            },
            options: {
                animation: false,
                scales: {
                    x: { type: 'time', time: { unit: 'second' } },
                    y: { position: 'left', title: { display: true, text: 'Price' } },
                    y1: { position: 'right', grid: { display: false }, ticks: { display: false }, title: { display: true, text: 'Vol' }, beginAtZero: true }
                }
            }
        });
    }

    resetCharts() {
        this.priceData = [];
        this.volumeData = [];
        if (this.metricsChart) {
            this.metricsChart.data.datasets.forEach(ds => {
                ds.data = [];
                if (ds.pointBackgroundColor) ds.pointBackgroundColor = [];
                if (Array.isArray(ds.backgroundColor)) ds.backgroundColor = [];
            });
            this.metricsChart.update();
        }
        if (this.priceChart) {
            this.priceChart.data.datasets[0].data = [];
            this.priceChart.data.datasets[1].data = [];
            this.priceChart.update();
        }
    }

    addMetricPoint(m) {
        const now = Date.now();
        const windowMs = 60 * 1000; // keep 1 minute of data to avoid huge time ranges
        const obi = typeof m.obi5 === 'number' ? m.obi5 : null;
        const ofi = typeof m.ofi === 'number' ? m.ofi : null;
        const trendScore = typeof m.trend_score === 'number' ? m.trend_score : null;
        
        // 调试日志：检查trend_score数据
        console.log('[DOM Manager] addMetricPoint - trend_score:', trendScore, 'raw data:', m.trend_score, 'full metric:', m);
        let suggestion = null;
        if (m.long === true) suggestion = 'buy';
        else if (m.short === true) suggestion = 'sell';
        else if (obi !== null && ofi !== null) {
            if (obi > 0.6 && ofi > 0) suggestion = 'buy';
            else if (obi < 0.4 && ofi < 0) suggestion = 'sell';
        }

        const reasonLines = [];
        if (trendScore !== null) {
            reasonLines.push(`Trend Score ${trendScore.toFixed(2)}`);
        }
        if (obi !== null) {
            const obiState = obi > 0.6 ? '买盘主导' : obi < 0.4 ? '卖盘压力' : '买卖均衡';
            reasonLines.push(`OBI5 ${obi.toFixed(2)} (${obiState})`);
        }
        if (ofi !== null) {
            const ofiState = ofi > 0 ? '买单流入' : ofi < 0 ? '卖单流出' : '成交均衡';
            reasonLines.push(`OFI ${ofi.toFixed(0)} (${ofiState})`);
        }
        if (typeof m.zscore_obi5 === 'number' && Math.abs(m.zscore_obi5) >= 1) {
            const state = m.zscore_obi5 > 0 ? '显著偏多' : '显著偏空';
            reasonLines.push(`Z-OBI ${m.zscore_obi5.toFixed(1)} (${state})`);
        }
        if (typeof m.zscore_ofi === 'number' && Math.abs(m.zscore_ofi) >= 1) {
            const state = m.zscore_ofi > 0 ? '积极买单' : '积极卖单';
            reasonLines.push(`Z-OFI ${m.zscore_ofi.toFixed(1)} (${state})`);
        }
        if (suggestion) {
            reasonLines.push(suggestion === 'buy' ? '触发做多信号' : '触发做空信号');
        }
        const reason = reasonLines.length ? reasonLines.join('\n') : '指标变化有限，保持观察';

        const pointColor = suggestion === 'buy'
            ? 'rgba(34,197,94,0.85)'
            : suggestion === 'sell'
                ? 'rgba(239,68,68,0.85)'
                : trendScore !== null && trendScore !== 0
                    ? trendScore > 0 ? 'rgba(59,130,246,0.65)' : 'rgba(248,113,113,0.65)'
                    : 'rgba(148,163,184,0.6)';
        const barColor = suggestion === 'buy'
            ? 'rgba(34,197,94,0.35)'
            : suggestion === 'sell'
                ? 'rgba(239,68,68,0.35)'
                : 'rgba(148,163,184,0.25)';
        if (this.metricsChart) {
            const ds = this.metricsChart.data.datasets;
            ds[0].data.push({ x: now, y: obi ?? 0 });
            ds[0].pointBackgroundColor.push(pointColor);
            ds[1].data.push({ x: now, y: ofi ?? 0 });
            ds[1].backgroundColor.push(barColor);
            ds[2].data.push({ x: now, y: m.zscore_obi5 ?? 0 });
            ds[3].data.push({ x: now, y: m.zscore_ofi ?? 0 });
            // prune old points and colors
            ds.forEach((dataset) => {
                dataset.data = dataset.data.filter(p => p.x >= now - windowMs);
                if (dataset.pointBackgroundColor) {
                    dataset.pointBackgroundColor = dataset.pointBackgroundColor.slice(-dataset.data.length);
                }
                if (Array.isArray(dataset.backgroundColor)) {
                    dataset.backgroundColor = dataset.backgroundColor.slice(-dataset.data.length);
                }
            });
            this.metricsChart.options.scales.x.min = now - windowMs;
            this.metricsChart.options.scales.x.max = now;
            this.metricsChart.update('none');
        }
        const price = m.mid_price ?? 0;
        const vol = Math.abs(m.ofi ?? 0);

        if (trendScore !== null) {
            const absScore = Math.abs(trendScore);
            const level = absScore >= 0.95 ? 4 : absScore >= 0.85 ? 3 : absScore >= 0.7 ? 2 : absScore >= 0.5 ? 1 : 0;
            const dir = trendScore > 0 ? 1 : trendScore < 0 ? -1 : 0;
            if (level > 0 && (level !== this.lastTrendLevel || dir !== this.lastTrendDir)) {
                this.app.addDomSignal({ time: now, price, score: trendScore, level });
            }
            this.lastTrendLevel = level;
            this.lastTrendDir = dir;

            if (typeof this.app.addMonitorTrendPoint === 'function') {
                console.log('[DOM Manager] 调用addMonitorTrendPoint - trendScore:', trendScore, 'level:', level, 'suggestion:', suggestion);
                this.app.addMonitorTrendPoint(now, trendScore, {
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
        this.priceData.push({ x: now, y: price });
        this.volumeData.push({ x: now, y: vol });
        this.priceData = this.priceData.filter(p => p.x >= now - windowMs);
        this.volumeData = this.volumeData.filter(p => p.x >= now - windowMs);
        if (this.priceChart) {
            this.priceChart.data.datasets[0].data = this.priceData;
            this.priceChart.data.datasets[1].data = this.volumeData;
            const maxVol = this.volumeData.reduce((mx, p) => Math.max(mx, p.y), 0);
            this.priceChart.options.scales.y1.max = (maxVol || 1) * 3;
            this.priceChart.options.scales.x.min = now - windowMs;
            this.priceChart.options.scales.x.max = now;
            this.priceChart.update('none');
        }
    }
}


window.DOMManager = DOMManager;

