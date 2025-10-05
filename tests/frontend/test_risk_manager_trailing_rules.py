import json
import subprocess
from pathlib import Path


def test_trailing_rule_modal_toggles_sections() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    manager_path = repo_root / "static" / "js" / "risk" / "manager.js"

    script = f"""
const fs = require('fs');
const vm = require('vm');

class FakeElement {{
  constructor(tag) {{
    this.tagName = String(tag || '').toUpperCase();
    this.children = [];
    this.style = {{}};
  }}
  appendChild(child) {{
    this.children.push(child);
    return child;
  }}
}}

const elements = new Map();
const queryMap = new Map();

function registerElement(id, element) {{
  if (id) {{
    element.id = id;
    elements.set(id, element);
  }}
  return element;
}}

const document = {{
  getElementById(id) {{ return elements.get(id) || null; }},
  querySelectorAll(selector) {{ return queryMap.get(selector) || []; }},
  createElement(tag) {{ return new FakeElement(tag); }},
  addEventListener() {{}},
  removeEventListener() {{}},
}};

const context = {{
  console,
  document,
  bootstrap: {{ Modal: class {{ constructor() {{}} show() {{}} }} }},
}};
context.window = context;
context.global = context;
context.appModules = {{}};

vm.createContext(context);
const source = fs.readFileSync('{manager_path.as_posix()}', 'utf8');
vm.runInContext(source, context);

const RiskRulesManager = context.appModules.risk.manager.RiskRulesManager;
const app = {{
  registerWebSocketHandler: () => () => {{}},
  apiCall: async () => ({{}}),
  setPanelLoading: () => {{}},
}};

const manager = new RiskRulesManager(app);
manager.currentRiskRuleData = {{ ruleType: 'fixed' }};

const fixedA = registerElement('fixedA', new FakeElement('div'));
const fixedB = registerElement('fixedB', new FakeElement('div'));
queryMap.set('.fixed-rule-field', [fixedA, fixedB]);

const atrSection = registerElement('atrParamsSection', new FakeElement('section'));
const pnlSection = registerElement('pnlPreviewSection', new FakeElement('section'));

const events = [];
manager.removePnLUpdateListeners = () => events.push('remove');
manager.addPnLUpdateListeners = () => events.push('add');
manager.updatePnLPreview = () => events.push('preview');

manager.handleRuleTypeSwitch('trailing');

const trailingState = {{
  currentRuleType: manager.currentRiskRuleData.ruleType,
  fixedDisplays: [fixedA.style.display || '', fixedB.style.display || ''],
  atrDisplay: atrSection.style.display || '',
  pnlDisplay: pnlSection.style.display || '',
  events: [...events],
}};

events.length = 0;
manager.handleRuleTypeSwitch('fixed');

const fixedState = {{
  currentRuleType: manager.currentRiskRuleData.ruleType,
  fixedDisplays: [fixedA.style.display || '', fixedB.style.display || ''],
  atrDisplay: atrSection.style.display || '',
  pnlDisplay: pnlSection.style.display || '',
  events: [...events],
}};

console.log(JSON.stringify({{ trailingState, fixedState }}));
"""

    completed = subprocess.run(
        [
            "node",
            "-e",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)

    trailing = payload["trailingState"]
    assert trailing["currentRuleType"] == "trailing"
    assert trailing["fixedDisplays"] == ["none", "none"]
    assert trailing["atrDisplay"] == "block"
    assert trailing["pnlDisplay"] == "none"
    assert trailing["events"] == ["remove"]

    fixed = payload["fixedState"]
    assert fixed["currentRuleType"] == "fixed"
    assert fixed["fixedDisplays"] == ["", ""]
    assert fixed["atrDisplay"] == "none"
    assert fixed["pnlDisplay"] == "block"
    assert fixed["events"] == ["remove", "add", "preview"]
