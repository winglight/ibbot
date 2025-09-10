// Strategies module (delegation wrapper)
// This class delegates strategy-related calls to the existing app methods.
// Gradually migrate full strategy logic here to match the RiskRulesManager pattern.

class StrategiesManager {
  constructor(app) {
    this.app = app;
  }

  _delegate(method, ...args) {
    if (this.app && typeof this.app[method] === 'function') {
      return this.app[method](...args);
    }
    console.error(`StrategiesManager: app.${method} is not defined`);
    return null;
  }

  async loadStrategies() {
    return await this._delegate('loadStrategies');
  }

  displayStrategies(strategies) {
    return this._delegate('displayStrategies', strategies);
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