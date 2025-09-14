class DOMManager {
    constructor(app) {
        this.app = app;
        this.ws = null;
        this.symbol = '';
        this.numRows = 5;
        this.signalCanvas = null;
        this.signalCtx = null;
        this.signalTooltip = null;
        this.signalHistory = [];
        this.barWidth = 8;
        this.eventColors = {
            buy_absorb: 'rgba(0,200,0,0.8)',
            sell_absorb: 'rgba(200,0,0,0.8)',
            sweep_up: 'rgba(0,150,0,0.8)',
            sweep_down: 'rgba(150,0,0,0.8)'
        };
        this.eventLabels = {
            buy_absorb: 'Buy Absorb',
            sell_absorb: 'Sell Absorb',
            sweep_up: 'Sweep Up',
            sweep_down: 'Sweep Down'
        };
        this.init();
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
        if (this.signalCanvas) {
            this.signalCtx = this.signalCanvas.getContext('2d');
            this.signalTooltip = document.getElementById('domSignalTooltip');
            this.signalCanvas.addEventListener('mousemove', (e) => this.onSignalHover(e));
            this.signalCanvas.addEventListener('mouseleave', () => this.hideTooltip());
        }
        await this.loadConfig();
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
        this.symbol = symbolEl.value.trim();
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
            this.symbol = document.getElementById('domSymbol').value.trim();
        }
        if (!this.symbol) {
            this.app.showAlert('warning', 'Please enter symbol in settings');
            return;
        }
        try {
            await this.app.apiCall('/dom/start', {
                method: 'POST',
                body: JSON.stringify({ symbol: this.symbol, num_rows: this.numRows })
            });
            this.connectWS();
            this.app.showAlert('success', 'DOM stream started');
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
        if (!this.symbol) return;
        try {
            await this.app.apiCall('/dom/stop', {
                method: 'POST',
                body: JSON.stringify({ symbol: this.symbol })
            });
            this.app.showAlert('info', 'DOM stream stopped');
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
                    const m = data.metrics;
                    document.getElementById('domObi').textContent = (m.obi ?? 0).toFixed(2);
                    document.getElementById('domOfi').textContent = (m.ofi ?? 0).toFixed(2);
                    document.getElementById('domCvd').textContent = (m.cvd ?? 0).toFixed(2);
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
}


window.DOMManager = DOMManager;

