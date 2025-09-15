class DOMManager {
    constructor(app) {
        this.app = app;
        this.ws = null;
        this.symbol = '';
        this.numRows = 5;
        this.signalCanvas = null;
        this.signalCtx = null;
        this.signalTooltip = null;
        this.signalContainer = null;
        this.signalHistory = [];
        this.barWidth = 8;
        this.metricsChart = null;
        this.priceChart = null;
        this.metricsChartEl = null;
        this.priceChartEl = null;
        this.priceData = [];
        this.volumeData = [];
        this.eventColors = {
            buy_absorb: 'rgba(0,200,0,0.8)',
            sell_absorb: 'rgba(200,0,0,0.8)',
            sweep_up: 'rgba(0,150,0,0.8)',
            sweep_down: 'rgba(150,0,0,0.8)'
        };
        // Mapping for human-readable descriptions in tooltip
        this.eventLabels = {
            buy_absorb: '买方吸收',
            sell_absorb: '卖方吸收',
            sweep_up: '向上扫单',
            sweep_down: '向下扫单'
        };
        this.init();
    }

    getSavedSymbol() {
        try {
            return localStorage.getItem('domSymbol') || '';
        } catch (e) {
            return '';
        }
    }

    setSymbol(sym) {
        this.symbol = sym;
        try {
            if (sym) localStorage.setItem('domSymbol', sym);
        } catch (e) {
            // ignore storage issues
        }
    }

    async init() {
        // Bind UI elements
        const settingsBtn = document.getElementById('domSettingsBtn');
        const startBtn = document.getElementById('startDomBtn');
        const stopBtn = document.getElementById('stopDomBtn');
        const saveBtn = document.getElementById('saveDomSettingsBtn');
        if (settingsBtn) {
            settingsBtn.addEventListener('click', () => {
                const modalEl = document.getElementById('domSettingsModal');
                if (!modalEl) return;
                const modal = new bootstrap.Modal(modalEl);
                modal.show();
            });
        }
        if (startBtn) startBtn.addEventListener('click', () => this.start());
        if (stopBtn) stopBtn.addEventListener('click', () => this.stop());
        if (saveBtn) saveBtn.addEventListener('click', () => this.saveSettings());
        this.signalCanvas = document.getElementById('domSignalCanvas');
        this.signalContainer = document.getElementById('domSignalBarContainer');
        if (this.signalContainer) {
            this.signalContainer.style.display = 'none';
        }
        if (this.signalCanvas) {
            this.signalCtx = this.signalCanvas.getContext('2d');
            this.signalTooltip = document.getElementById('domSignalTooltip');
            this.signalCanvas.addEventListener('mousemove', (e) => this.onSignalHover(e));
            this.signalCanvas.addEventListener('mouseleave', () => this.hideTooltip());
        }
        this.metricsChartEl = document.getElementById('domMetricsChart');
        this.priceChartEl = document.getElementById('domPriceChart');
        if (this.metricsChartEl && this.priceChartEl) {
            this.initCharts();
        }
        const saved = this.getSavedSymbol();
        if (saved) {
            this.symbol = saved;
            const symbolEl = document.getElementById('domSymbol');
            if (symbolEl) symbolEl.value = saved;
        }
        await this.loadConfig();

        // 如果之前已启动DOM流，刷新后自动重连
        try {
            const wasActive = localStorage.getItem('domActive') === '1';
            if (wasActive && this.symbol) {
                this.resetCharts();
                this.resetSignals();
                this.connectWS();
                if (this.signalContainer) this.signalContainer.style.display = 'block';
            }
        } catch (e) {
            // ignore storage issues
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
        const symbolEl = document.getElementById('domSymbol');
        const windowEl = document.getElementById('domWindow');
        const maxLevelsEl = document.getElementById('domMaxLevels');
        const numRowsEl = document.getElementById('domNumRows');
        this.setSymbol(symbolEl.value.trim());
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
        if (!this.symbol) {
            this.setSymbol(document.getElementById('domSymbol').value.trim());
        }
        if (!this.symbol) {
            this.app.showAlert('warning', 'Please enter symbol in settings');
            return;
        }
        this.resetCharts();
        this.resetSignals();
        try {
            await this.app.apiCall('/dom/start', {
                method: 'POST',
                body: JSON.stringify({ symbol: this.symbol, num_rows: this.numRows })
            });
            this.connectWS();
            if (this.signalContainer) this.signalContainer.style.display = 'block';
            this.app.showAlert('success', 'DOM stream started');
            try { localStorage.setItem('domActive', '1'); } catch (e) {}
        } catch (e) {
            console.error('Failed to start DOM', e);
            this.app.showAlert('danger', 'Failed to start DOM');
        }
    }

    async stop() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.resetSignals();
        this.hideTooltip();
        if (!this.symbol) return;
        try {
            await this.app.apiCall('/dom/stop', {
                method: 'POST',
                body: JSON.stringify({ symbol: this.symbol })
            });
            this.app.showAlert('info', 'DOM stream stopped');
            if (this.signalContainer) this.signalContainer.style.display = 'none';
            try { localStorage.setItem('domActive', '0'); } catch (e) {}
        } catch (e) {
            console.error('Failed to stop DOM', e);
            this.app.showAlert('danger', 'Failed to stop DOM');
        }
    }

    connectWS() {
        if (this.ws) this.ws.close();
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        this.ws = new WebSocket(`${proto}://${location.host}/ws/dom/${this.symbol}`);
        this.ws.onmessage = (evt) => {
            try {
                const data = JSON.parse(evt.data);
                if (data.metrics) {
                    this.addMetricPoint(data.metrics);
                }
                if (Array.isArray(data.events)) {
                    data.events.forEach((ev) => this.addEvent(ev));
                }
            } catch (e) {
                console.error('Invalid DOM WS message', e);
            }
        };
    }

    addEvent(ev) {
        if (!this.signalCtx) return;
        const label = this.eventLabels[ev] || ev;
        const color = this.eventColors[ev] || 'gray';
        this.signalHistory.push({ event: ev, label, color });
        const maxBars = Math.floor(this.signalCanvas.offsetWidth / this.barWidth);
        if (this.signalHistory.length > maxBars) {
            this.signalHistory.shift();
        }
        this.renderSignals();
    }

    renderSignals() {
        if (!this.signalCtx) return;
        const canvas = this.signalCanvas;
        canvas.width = canvas.offsetWidth;
        const ctx = this.signalCtx;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const mid = canvas.height / 2;
        this.signalHistory.forEach((item, idx) => {
            const x = idx * this.barWidth;
            ctx.fillStyle = item.color;
            if (item.event === 'buy_absorb' || item.event === 'sweep_up') {
                ctx.fillRect(x, 0, this.barWidth - 1, mid);
            } else {
                ctx.fillRect(x, mid, this.barWidth - 1, mid);
            }
        });
    }

    onSignalHover(e) {
        if (!this.signalCanvas || !this.signalTooltip) return;
        const rect = this.signalCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const idx = Math.floor(x / this.barWidth);
        const item = this.signalHistory[idx];
        if (!item) {
            this.hideTooltip();
            return;
        }
        this.signalTooltip.textContent = item.label;
        this.signalTooltip.style.left = `${idx * this.barWidth}px`;
        this.signalTooltip.style.top = item.event === 'buy_absorb' || item.event === 'sweep_up' ? '0px' : `${this.signalCanvas.height / 2}px`;
        this.signalTooltip.style.display = 'block';
    }

    hideTooltip() {
        if (this.signalTooltip) this.signalTooltip.style.display = 'none';
    }

    resetSignals() {
        this.signalHistory = [];
        if (this.signalCtx && this.signalCanvas) {
            this.signalCtx.clearRect(0, 0, this.signalCanvas.width, this.signalCanvas.height);
        }
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
        const color = m.long ? 'green' : m.short ? 'red' : 'white';
        if (this.metricsChart) {
            const ds = this.metricsChart.data.datasets;
            ds[0].data.push({ x: now, y: m.obi5 ?? 0 });
            ds[0].pointBackgroundColor.push(color);
            ds[1].data.push({ x: now, y: m.ofi ?? 0 });
            ds[1].backgroundColor.push(color);
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

