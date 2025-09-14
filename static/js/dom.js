class DOMManager {
    constructor(app) {
        this.app = app;
        this.ws = null;
        this.symbol = '';
        this.numRows = 5;
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
            } catch (e) {
                console.error('Invalid DOM WS message', e);
            }
        };
    }
}


window.DOMManager = DOMManager;

