// Strategies module (delegation wrapper)
// This class delegates strategy-related calls to the existing app methods.
// Gradually migrate full strategy logic here to match the RiskRulesManager pattern.

class StrategiesManager {
  constructor(app) {
    this.app = app;
    this.refreshIntervalMs = 5000;
    this.refreshTimer = null;
    this._visibilityHandler = null;
    this.startAutoRefresh();
  }

  _delegate(method, ...args) {
    if (this.app && typeof this.app[method] === 'function') {
      return this.app[method](...args);
    }
    console.error(`StrategiesManager: app.${method} is not defined`);
    return null;
  }

  async loadStrategies(options = {}) {
    return await this._delegate('loadStrategies', options);
  }

  displayStrategies(strategies) {
    return this._delegate('displayStrategies', strategies);
  }

  startAutoRefresh() {
    this.stopAutoRefresh();
    const interval = Math.max(1000, this.app?.strategyRefreshIntervalMs || this.refreshIntervalMs);
    this.refreshIntervalMs = interval;

    if (typeof document !== 'undefined') {
      this._visibilityHandler = () => {
        if (document.visibilityState === 'visible') {
          this.loadStrategies({ force: true, minInterval: 0 });
        }
      };
      document.addEventListener('visibilitychange', this._visibilityHandler);
    }

    this.refreshTimer = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        return;
      }
      const ws = this.app?.ibkrManager?.ws;
      if (ws && ws.readyState !== WebSocket.OPEN) {
        return;
      }
      this.loadStrategies({ minInterval: interval });
    }, interval);
  }

  stopAutoRefresh() {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    if (this._visibilityHandler && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this._visibilityHandler);
      this._visibilityHandler = null;
    }
  }

  async toggleStrategy(name, enabled) {
    return await this._delegate('toggleStrategy', name, enabled);
  }

  async editStrategy(name) {
    return await this._delegate('editStrategy', name);
  }

  async deleteStrategy(name) {
    return await this._delegate('deleteStrategy', name);
  }

  async loadStrategySettings() {
    return await this._delegate('loadStrategySettings');
  }

  async saveStrategySettings() {
    return await this._delegate('saveStrategySettings');
  }

  openStrategyDetails(name) {
    return this._delegate('openStrategyDetails', name);
  }
}

// Expose to global to match existing pattern (e.g., RiskRulesManager)
window.StrategiesManager = StrategiesManager;