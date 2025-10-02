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
    return json.loads(completed.stdout.strip() or "{}")


def test_apply_freshness_metrics_updates_elements() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    restart_path = repo_root / "static" / "js" / "service_restart.js"

    script = f"""
const fs = require('fs');
const vm = require('vm');

class FakeClassList {{
  constructor() {{
    this._set = new Set();
  }}
  add(...classes) {{
    classes.forEach((cls) => {{
      if (cls) {{
        this._set.add(String(cls));
      }}
    }});
    return this._set.size;
  }}
  remove(...classes) {{
    classes.forEach((cls) => {{
      this._set.delete(String(cls));
    }});
    return this._set.size;
  }}
  toggle(cls, force) {{
    const value = String(cls);
    if (force === undefined) {{
      if (this._set.has(value)) {{
        this._set.delete(value);
        return false;
      }}
      this._set.add(value);
      return true;
    }}
    if (force) {{
      this._set.add(value);
      return true;
    }}
    this._set.delete(value);
    return false;
  }}
  contains(cls) {{
    return this._set.has(String(cls));
  }}
  values() {{
    return this._set.values();
  }}
}}

class FakeElement {{
  constructor(tag) {{
    this.tagName = String(tag || '').toUpperCase();
    this.children = [];
    this.dataset = {{}};
    this.classList = new FakeClassList();
    this.textContent = '';
    this.title = '';
    this.hidden = false;
  }}
  appendChild(child) {{
    this.children.push(child);
    return child;
  }}
  querySelector() {{
    return null;
  }}
}}

const restartSource = fs.readFileSync({json.dumps(str(restart_path))}, 'utf8');

const globalObj = global;
if (!globalObj.appModules) {{
  globalObj.appModules = {{}};
}}
if (!globalObj.document) {{
  globalObj.document = {{
    createElement(tag) {{ return new FakeElement(tag); }},
    createDocumentFragment() {{ return new FakeElement('#fragment'); }},
    getElementById() {{ return null; }},
    querySelectorAll() {{ return []; }},
  }};
}}

globalObj.window = globalObj;

vm.runInThisContext(restartSource, {{ filename: 'service_restart.js' }});

const module = globalObj.appModules.serviceRestart;
if (!module || !module.__test__ || typeof module.__test__.applyFreshnessMetrics !== 'function') {{
  throw new Error('applyFreshnessMetrics helper unavailable');
}}
const apply = module.__test__.applyFreshnessMetrics;

function snapshot(item) {{
  return {{
    containerClasses: Array.from(item.metricsContainer.classList.values()),
    badgeText: item.metricsBadge.textContent,
    badgeClasses: Array.from(item.metricsBadge.classList.values()),
    badgeTitle: item.metricsBadge.title,
    timestampText: item.metricsTimestamp.textContent,
    timestampClasses: Array.from(item.metricsTimestamp.classList.values()),
    reasonText: item.metricsReason.textContent,
    reasonClasses: Array.from(item.metricsReason.classList.values()),
    reasonTitle: item.metricsReason.title,
    dataset: Object.assign({{}}, item.metricsContainer.dataset),
  }};
}}

const item = {{
  metricsContainer: new FakeElement('div'),
  metricsBadge: new FakeElement('span'),
  metricsTimestamp: new FakeElement('span'),
  metricsReason: new FakeElement('span'),
}};
item.metricsContainer.classList.add('d-none');
item.metricsBadge.classList.add('d-none');
item.metricsTimestamp.classList.add('d-none');
item.metricsReason.classList.add('d-none');

const first = apply(item, {{
  stale: 'true',
  snapshot_updated_at: '2024-01-01T00:00:00Z',
  stale_reason: 'cache-miss',
}});
const afterFirst = snapshot(item);

const second = apply(item, null);
const afterSecond = snapshot(item);

console.log(JSON.stringify({{
  first,
  afterFirst,
  second,
  afterSecond,
}}));
"""

    result = _run_node_script(script)

    first = result.get("first") or {}
    after_first = result.get("afterFirst") or {}
    after_second = result.get("afterSecond") or {}

    assert first.get("stale") is True
    assert after_first["dataset"].get("freshness") == "stale"
    assert "bg-danger" in after_first.get("badgeClasses", [])
    assert after_first.get("badgeText") == "已过期"
    assert after_first.get("timestampText") == "快照时间：2024-01-01T00:00:00Z"
    assert after_first.get("reasonText") == "原因：cache-miss"

    assert "d-none" in after_second.get("containerClasses", [])
    assert after_second.get("badgeText") == "未知"
    assert after_second.get("timestampText") == ""
    assert after_second.get("reasonText") == ""
