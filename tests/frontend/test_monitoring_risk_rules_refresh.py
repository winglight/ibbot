from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run_node_script(script: str) -> dict:
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = completed.stdout.strip()
    return json.loads(stdout or "{}")


def test_trailing_rules_refresh_sl_tp_values() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    manager_path = repo_root / "static" / "js" / "monitoring" / "manager.js"

    script_template = r"""
const fs = require('fs');
const vm = require('vm');

class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  add(...classes) {
    classes.forEach((cls) => {
      if (cls) {
        this._set.add(String(cls));
      }
    });
    return this._set.size;
  }
  remove(...classes) {
    classes.forEach((cls) => this._set.delete(String(cls)));
    return this._set.size;
  }
  toggle(cls, force) {
    const value = String(cls);
    if (force === undefined) {
      if (this._set.has(value)) {
        this._set.delete(value);
        return false;
      }
      this._set.add(value);
      return true;
    }
    if (force) {
      this._set.add(value);
      return true;
    }
    this._set.delete(value);
    return false;
  }
  contains(cls) {
    return this._set.has(String(cls));
  }
  values() {
    return this._set.values();
  }
}

class FakeElement {
  constructor(tag, id) {
    this.tagName = String(tag || '').toUpperCase();
    this.id = id || null;
    this.children = [];
    this.dataset = {};
    this.classList = new FakeClassList();
    this.textContent = '';
    this.value = '';
    this.checked = false;
    this.style = {};
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  querySelector(selector) {
    if (!selector) return null;
    if (selector === 'i') {
      return this.children.find((child) => child.tagName === 'I') || null;
    }
    return null;
  }
  querySelectorAll() {
    return [];
  }
  setAttribute(name, value) {
    if (!this.dataset) this.dataset = {};
    this.dataset[name] = value;
  }
  getAttribute(name) {
    if (!this.dataset) return undefined;
    return this.dataset[name];
  }
  getContext() {
    return {};
  }
}

const source = fs.readFileSync(__MANAGER_PATH__, 'utf8');

const globalObj = global;
if (!globalObj.appModules) {
  globalObj.appModules = {};
}
if (!globalObj.appModules.utils) {
  globalObj.appModules.utils = {};
}
Object.assign(globalObj.appModules.utils, {
  formatNumber(value) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric.toFixed(2);
    return '0.00';
  },
  escapeHtml(value) {
    return String(value ?? '');
  },
  formatDuration(value) {
    return `${Number(value || 0)}s`;
  },
  isSameDay() {
    return true;
  },
  TrendSignalLabelPlugin: {
    afterDatasetsDraw() {},
  },
  slugifyStrategyName(value) {
    return String(value || '').toLowerCase();
  },
});

if (!globalObj.appModules.monitoring) {
  globalObj.appModules.monitoring = {};
}
if (!globalObj.appModules.monitoring.constants) {
  globalObj.appModules.monitoring.constants = {};
}
Object.assign(globalObj.appModules.monitoring.constants, {
  MONITOR_DURATION_OPTIONS: {},
  INTERVAL_MINUTES_MAP: {},
  MONITOR_EVENT_SIDE_COLORS: {},
  MONITOR_CHART_MAX_MINUTES: 30,
  MONITOR_CHART_MAX_POINTS: 3000,
  MONITOR_SOURCE_TZ: 'UTC',
  MONITOR_FALLBACK_TZ: 'UTC',
});
if (!globalObj.appModules.monitoring.utils) {
  globalObj.appModules.monitoring.utils = {};
}
Object.assign(globalObj.appModules.monitoring.utils, {
  normalizeIntervalKey(value) {
    return value;
  },
  getMonitorDurationOptions() {
    return [];
  },
  getMonitorDurationMinutes(value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number') return value;
    const numeric = Number(String(value).replace(/[^0-9.\-]/g, ''));
    return Number.isFinite(numeric) ? numeric : null;
  },
  refreshMonitorDurationSelect() {},
});

globalObj.window = globalObj;

const elements = new Map();
function registerElement(id, element) {
  if (id) {
    element.id = id;
    elements.set(id, element);
  }
  return element;
}

globalObj.document = {
  createElement: (tag) => new FakeElement(tag),
  createDocumentFragment: () => new FakeElement('#fragment'),
  getElementById: (id) => elements.get(id) || null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  removeEventListener: () => {},
  fullscreenElement: null,
  exitFullscreen: () => Promise.resolve(),
};

globalObj.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
globalObj.clearTimeout = () => {};

globalObj.bootstrap = {
  Modal: class {
    constructor(element) {
      this.element = element;
      this.shown = false;
    }
    show() {
      this.shown = true;
    }
  },
};

vm.runInThisContext(source, { filename: 'monitoring_manager.js' });

const MonitoringManager = globalObj.appModules.monitoring.manager.MonitoringManager;

const apiCalls = [];

const app = {
  formatNumber: (value) => globalObj.appModules.utils.formatNumber(value),
  showAlert: () => {},
  loadOrders: async () => {},
  apiCall: async (path, options) => {
    apiCalls.push(path);
    if (path === '/risk_rules?force_refresh=true') {
      return {
        rules: {
          ES: {
            symbol: 'ES',
            rule_type: 'trailing',
            enabled: true,
            stop_loss_price: 101.5,
            take_profit_price: 111.25,
          },
        },
      };
    }
    if (path === '/risk_rules') {
      return {
        rules: {
          ES: {
            symbol: 'ES',
            rule_type: 'trailing',
            enabled: true,
            stop_loss_price: 95,
            take_profit_price: 105,
          },
        },
      };
    }
    if (path === '/risk-config') {
      return {
        stop_loss_ratio: 0.025,
        take_profit_ratio: 0.025,
        leverage: {},
      };
    }
    return {};
  },
  correctPriceStep: (value) => value,
  getContractMultiplier: () => 1,
  domManager: null,
  dataManager: {
    getPosition: () => null,
  },
  orders: [],
};

const manager = new MonitoringManager(app);
manager.orders = [];
manager.resetMonitorTrendData = () => {};
manager.updateExpectedPnL = () => {};
manager.showActiveRiskRule = () => {};
manager.hideActiveRiskRule = () => {};
manager.updateMonitorStopLossLine = () => {};
manager.updateMonitorTakeProfitLine = () => {};

manager.riskRules = {
  ES: {
    symbol: 'ES',
    rule_type: 'trailing',
    enabled: true,
    stop_loss_price: 90,
    take_profit_price: 110,
  },
};

registerElement('monitorSymbol', new FakeElement('span'));
registerElement('monitorPositionCombined', new FakeElement('span'));
registerElement('monitorCurrentPrice', new FakeElement('span'));
registerElement('monitorAvgCost', new FakeElement('span'));
registerElement('monitorPnL', new FakeElement('span'));
registerElement('monitorPnLPercent', new FakeElement('span'));
registerElement('monitorStopLoss', new FakeElement('input'));
registerElement('monitorTakeProfit', new FakeElement('input'));
registerElement('monitorRiskToggle', new FakeElement('input'));
registerElement('monitorDuration', new FakeElement('select'));

(async () => {
  await manager.openMonitorModal('ES', 1, 100, 100, 0, 0);
  const stopLossEl = elements.get('monitorStopLoss');
  const takeProfitEl = elements.get('monitorTakeProfit');
  console.log(JSON.stringify({
    apiCalls,
    stopLoss: stopLossEl ? stopLossEl.value : null,
    takeProfit: takeProfitEl ? takeProfitEl.value : null,
    finalRule: manager.riskRules ? manager.riskRules['ES'] : null,
  }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""

    script = script_template.replace("__MANAGER_PATH__", json.dumps(str(manager_path)))

    result = _run_node_script(script)

    api_calls = result.get("apiCalls") or []
    stop_loss = result.get("stopLoss")
    take_profit = result.get("takeProfit")
    final_rule = result.get("finalRule") or {}

    assert "/risk_rules?force_refresh=true" in api_calls
    assert stop_loss == 101.5 or stop_loss == "101.5"
    assert take_profit == 111.25 or take_profit == "111.25"
    assert final_rule.get("stop_loss_price") == 101.5
    assert final_rule.get("take_profit_price") == 111.25
