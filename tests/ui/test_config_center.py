from __future__ import annotations

import asyncio
import json
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any
import time

import fnmatch
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import config as config_module


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = defaultdict(dict)
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.sets: dict[str, set[str]] = defaultdict(set)
        self.published: list[tuple[str, str]] = []

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, mapping: dict[str, Any]) -> None:
        bucket = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            bucket[str(field)] = str(value)

    async def hdel(self, key: str, *fields: str) -> int:
        bucket = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in bucket:
                removed += 1
                del bucket[field]
        if not bucket and key in self.hashes:
            self.hashes[key] = {}
        return removed

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return len(self.published)

    async def lpush(self, key: str, value: str) -> int:
        self.lists[key].insert(0, value)
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = list(self.lists.get(key, []))
        length = len(items)
        if length == 0:
            return []
        if start < 0:
            start = max(0, length + start)
        if end < 0:
            end = length + end
        if end >= length or end == -1:
            end = length - 1
        if start >= length or end < start:
            return []
        return items[start : end + 1]

    async def ltrim(self, key: str, start: int, end: int) -> None:
        items = list(self.lists.get(key, []))
        length = len(items)
        if length == 0:
            return
        if start < 0:
            start = max(0, length + start)
        if end < 0:
            end = length + end
        if end >= length or end == -1:
            end = length - 1
        if start >= length or end < start:
            self.lists[key] = []
        else:
            self.lists[key] = items[start : end + 1]

    async def sadd(self, key: str, *values: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        for value in values:
            bucket.add(str(value))
        return len(bucket) - before

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def scan_iter(self, match: str | None = None):
        pattern = match or '*'
        for key in list(self.hashes.keys()):
            if fnmatch.fnmatch(key, pattern):
                yield key

    async def delete(self, key: str) -> int:
        removed = 0
        if key in self.hashes:
            del self.hashes[key]
            removed += 1
        if key in self.lists:
            del self.lists[key]
            removed += 1
        if key in self.sets:
            del self.sets[key]
            removed += 1
        return removed


@pytest.fixture()
def config_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(config_module.router)

    fake = FakeRedis()

    async def fake_ensure() -> FakeRedis:
        return fake

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = 'admin'
        request.state.username = 'tester'
        return 'tester'

    monkeypatch.setattr(config_module, 'ensure_redis', fake_ensure)
    monkeypatch.setattr(config_module, 'AUDIT_LOG_PATH', tmp_path / 'config_audit.log')
    app.dependency_overrides[config_module.get_current_user] = allow_user
    app.state.fake_redis = fake  # type: ignore[attr-defined]
    return app


def test_list_modules_includes_registry_and_hashes(config_app: FastAPI) -> None:
    fake: FakeRedis = config_app.state.fake_redis  # type: ignore[attr-defined]
    fake.hashes[f"{config_module.HASH_PREFIX}:risk"] = {'STOP': '0.1'}
    fake.sets[config_module.MODULE_SET_KEY].update({'risk', 'execution'})

    with TestClient(config_app) as client:
        response = client.get('/api/config/modules')

    assert response.status_code == 200
    payload = response.json()
    assert payload['modules'] == ['execution', 'risk']


def test_update_module_publishes_history_and_audit(config_app: FastAPI) -> None:
    fake: FakeRedis = config_app.state.fake_redis  # type: ignore[attr-defined]
    redis_key = f"{config_module.HASH_PREFIX}:risk"
    fake.hashes[redis_key] = {'STOP': '0.10', 'LEGACY': 'keep'}

    with TestClient(config_app) as client:
        response = client.put(
            '/api/config/risk',
            json={
                'values': {'STOP': 0.25, 'NEW': 'enabled', 'LEGACY': None},
                'message': 'raise stop',
            },
        )

    assert response.status_code == 200
    updated = fake.hashes[redis_key]
    assert updated == {'STOP': '0.25', 'NEW': 'enabled'}
    assert fake.published[-1] == (config_module.UPDATE_CHANNEL, 'risk')

    history_key = config_module.HISTORY_KEY_TEMPLATE.format(module='risk')
    assert fake.lists[history_key], 'history entry missing'
    entry = json.loads(fake.lists[history_key][0])
    assert entry['action'] == 'update'
    assert entry['values'] == {'STOP': '0.25', 'NEW': 'enabled'}
    assert entry['message'] == 'raise stop'

    log_path = config_module.AUDIT_LOG_PATH
    assert log_path.exists()
    log_entry = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert log_entry['action'] == 'update'
    assert log_entry['before'] == {'STOP': '0.10', 'LEGACY': 'keep'}
    assert 'version' in response.json()


def test_read_module_timeout_returns_503(
    config_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeRedis = config_app.state.fake_redis  # type: ignore[attr-defined]
    redis_key = f"{config_module.HASH_PREFIX}:risk"
    fake.hashes[redis_key] = {'STOP': '0.10'}

    async def slow_hgetall(key: str) -> dict[str, str]:
        await asyncio.sleep(0.2)
        return await FakeRedis.hgetall(fake, key)

    monkeypatch.setattr(fake, 'hgetall', slow_hgetall)
    monkeypatch.setattr(config_module, 'CONFIG_REDIS_COMMAND_TIMEOUT', 0.05)

    with TestClient(config_app) as client:
        start = time.perf_counter()
        response = client.get('/api/config/risk')
        duration = time.perf_counter() - start

    assert response.status_code == 503
    assert duration < 0.5
    assert response.json()['detail'] == 'Redis request timed out'


def test_update_module_timeout_rolls_back(
    config_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake: FakeRedis = config_app.state.fake_redis  # type: ignore[attr-defined]
    redis_key = f"{config_module.HASH_PREFIX}:risk"
    fake.hashes[redis_key] = {'STOP': '0.10'}

    async def flaky_hset(key: str, mapping: dict[str, Any]) -> None:
        call_count = getattr(flaky_hset, 'calls', 0)
        setattr(flaky_hset, 'calls', call_count + 1)
        if call_count == 0:
            raise asyncio.TimeoutError
        await FakeRedis.hset(fake, key, mapping)

    monkeypatch.setattr(fake, 'hset', flaky_hset)
    monkeypatch.setattr(config_module, 'CONFIG_REDIS_COMMAND_TIMEOUT', 0.05)

    with TestClient(config_app) as client:
        start = time.perf_counter()
        response = client.put(
            '/api/config/risk',
            json={'values': {'STOP': 0.25}},
        )
        duration = time.perf_counter() - start

    assert response.status_code == 503
    assert duration < 0.5
    assert response.json()['detail'] == 'Redis request timed out'
    # state should be rolled back to original snapshot
    assert fake.hashes[redis_key] == {'STOP': '0.10'}
    assert not fake.published
    history_key = config_module.HISTORY_KEY_TEMPLATE.format(module='risk')
    assert not fake.lists[history_key]


def test_update_rejects_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(config_module.router)
    fake = FakeRedis()

    async def fake_ensure() -> FakeRedis:
        return fake

    async def read_only(request: Request) -> str:
        request.state.is_read_only = True
        request.state.user_role = 'visitor'
        request.state.username = 'visitor'
        return 'visitor'

    monkeypatch.setattr(config_module, 'ensure_redis', fake_ensure)
    monkeypatch.setattr(config_module, 'AUDIT_LOG_PATH', tmp_path / 'config_audit.log')
    app.dependency_overrides[config_module.get_current_user] = read_only

    with TestClient(app) as client:
        response = client.put('/api/config/risk', json={'values': {'STOP': 1}})

    assert response.status_code == 403
    assert 'read-only' in response.json()['detail'].lower()


def test_rollback_restores_previous_revision(config_app: FastAPI) -> None:
    fake: FakeRedis = config_app.state.fake_redis  # type: ignore[attr-defined]
    redis_key = f"{config_module.HASH_PREFIX}:risk"
    fake.hashes[redis_key] = {'STOP': '0.40', 'TEMP': 'yes'}
    history_key = config_module.HISTORY_KEY_TEMPLATE.format(module='risk')
    fake.lists[history_key] = [
        json.dumps(
            {
                'version': 'rev-latest',
                'module': 'risk',
                'action': 'update',
                'values': {'STOP': '0.40', 'TEMP': 'yes'},
                'updated_at': '2024-05-06T00:00:00Z',
                'updated_by': 'tester',
            }
        ),
        json.dumps(
            {
                'version': 'rev-1',
                'module': 'risk',
                'action': 'update',
                'values': {'STOP': '0.15'},
                'updated_at': '2024-05-01T00:00:00Z',
                'updated_by': 'tester',
            }
        ),
    ]

    with TestClient(config_app) as client:
        response = client.post(
            '/api/config/risk/rollback',
            json={'version': 'rev-1', 'message': 'rollback to safe baseline'},
        )

    assert response.status_code == 200
    assert fake.hashes[redis_key] == {'STOP': '0.15'}
    assert fake.published[-1] == (config_module.UPDATE_CHANNEL, 'risk')

    history_entry = json.loads(fake.lists[history_key][0])
    assert history_entry['action'] == 'rollback'
    assert history_entry['restored_version'] == 'rev-1'
    assert history_entry['values'] == {'STOP': '0.15'}

    log_entry = json.loads(config_module.AUDIT_LOG_PATH.read_text().strip().splitlines()[-1])
    assert log_entry['action'] == 'rollback'
    assert log_entry['restored_version'] == 'rev-1'


def test_frontend_config_manager_handles_save_and_rollback() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = (repo_root / 'static/js/config_center.js').as_posix()

    node_script = f"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync({json.dumps(script_path)}, 'utf8');

function makeClassList() {{
  const store = new Set();
  return {{
    add: (...classes) => classes.forEach((cls) => store.add(cls)),
    remove: (...classes) => classes.forEach((cls) => store.delete(cls)),
    toggle: (cls, force) => {{
      if (force === undefined) {{
        if (store.has(cls)) {{ store.delete(cls); }} else {{ store.add(cls); }}
        return store.has(cls);
      }}
      if (force) {{ store.add(cls); }} else {{ store.delete(cls); }}
      return force;
    }},
    contains: (cls) => store.has(cls),
  }};
}}

function createElement(id) {{
  return {{
    id,
    value: '',
    disabled: false,
    innerHTML: '',
    textContent: '',
    dataset: {{}},
    classList: makeClassList(),
    listeners: {{}},
    addEventListener(type, handler) {{ this.listeners[type] = handler; }},
    removeEventListener() {{}},
  }};
}}

const document = {{
  elements: new Map(),
  getElementById(id) {{
    return this.elements.get(id) || null;
  }},
}};

function register(id, element) {{
  document.elements.set(id, element);
  return element;
}}

const moduleSelect = register('configCenterModuleSelect', createElement('configCenterModuleSelect'));
moduleSelect.options = [];
moduleSelect.remove = function(index) {{
  if (Array.isArray(this.options)) {{
    this.options.splice(index, 1);
  }}
}};
const textarea = register('configCenterTextarea', createElement('configCenterTextarea'));
const saveButton = register('configCenterSaveButton', createElement('configCenterSaveButton'));
const resetButton = register('configCenterResetButton', createElement('configCenterResetButton'));
const reloadButton = register('configCenterReloadButton', createElement('configCenterReloadButton'));
const editorContainer = register('configCenterEditor', createElement('configCenterEditor'));
const messageInput = register('configCenterMessageInput', createElement('configCenterMessageInput'));
const statusText = register('configCenterStatusText', createElement('configCenterStatusText'));
const historyList = register('configCenterHistoryList', createElement('configCenterHistoryList'));
historyList.querySelectorAll = () => [];
const historyReload = register('configCenterHistoryRefresh', createElement('configCenterHistoryRefresh'));

const context = {{
  console,
  setTimeout,
  clearTimeout,
  window: {{}},
  document,
  appModules: {{}},
}};
context.globalThis = context;
context.window = context;

vm.createContext(context);
vm.runInContext(code, context);

const calls = [];
const statusLog = [];
const configs = {{
  risk: {{ STOP_LOSS_RATIO: '0.1' }},
}};
const histories = {{
  risk: [
    {{
      version: 'rev-1',
      module: 'risk',
      action: 'update',
      updated_by: 'tester',
      updated_at: '2024-05-01T00:00:00Z',
      values: {{ STOP_LOSS_RATIO: '0.1' }},
    }},
  ],
}};

const manager = context.appModules.configCenter.createManager({{
  document,
  moduleSelectId: 'configCenterModuleSelect',
  textareaId: 'configCenterTextarea',
  saveButtonId: 'configCenterSaveButton',
  resetButtonId: 'configCenterResetButton',
  reloadButtonId: 'configCenterReloadButton',
  editorId: 'configCenterEditor',
  messageInputId: 'configCenterMessageInput',
  statusTextId: 'configCenterStatusText',
  historyListId: 'configCenterHistoryList',
  historyReloadId: 'configCenterHistoryRefresh',
  loadModules: async () => {{
    calls.push(['modules']);
    return {{ modules: ['risk'] }};
  }},
  loadConfig: async (module) => {{
    calls.push(['config', module]);
    return {{ module, values: configs[module] || {{}} }};
  }},
  saveConfig: async (module, payload) => {{
    calls.push(['save', module, payload]);
    configs[module] = payload.values;
    return {{ module, values: payload.values }};
  }},
  loadHistory: async (module) => {{
    calls.push(['history', module]);
    return {{ entries: histories[module] || [] }};
  }},
  rollback: async (module, payload) => {{
    calls.push(['rollback', module, payload]);
    configs[module] = {{ STOP_LOSS_RATIO: '0.05' }};
    return {{ module, values: configs[module] }};
  }},
  onStatus: (level, message) => {{
    statusLog.push([level, message]);
  }},
}});

async function main() {{
  await new Promise((resolve) => setTimeout(resolve, 0));
  const initialTextarea = textarea.value;
  messageInput.value = 'tune risk';
  textarea.value = JSON.stringify({{ STOP_LOSS_RATIO: 0.2, NEW: 'abc' }});
  await manager.save();
  const afterSave = textarea.value;
  await manager.rollbackVersion('rev-1');
  const afterRollback = textarea.value;
  manager.setReadOnly(true);
  const disabledWhenReadOnly = saveButton.disabled === true;
  manager.setReadOnly(false);
  const disabledWhenWritable = saveButton.disabled === true;
  return {{
    calls,
    statusLog,
    initialTextarea,
    afterSave,
    afterRollback,
    disabledWhenReadOnly,
    disabledWhenWritable,
    editorHidden: editorContainer.classList.contains('d-none'),
  }};
}}

main().then((result) => {{
  console.log(JSON.stringify(result));
}}).catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""

    completed = subprocess.run(
        ['node', '-e', node_script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    payload: dict[str, Any] = json.loads(completed.stdout.strip())

    assert payload['calls'][0] == ['modules']
    assert ['save', 'risk', {'values': {'STOP_LOSS_RATIO': 0.2, 'NEW': 'abc'}, 'message': 'tune risk'}] in payload['calls']
    assert any(call[0] == 'rollback' for call in payload['calls'])
    assert payload['initialTextarea'].startswith('{')
    assert 'STOP_LOSS_RATIO' in payload['afterSave']
    assert payload['afterRollback'].strip().startswith('{')
    assert payload['disabledWhenReadOnly'] is True
    assert payload['disabledWhenWritable'] is False
    assert payload['editorHidden'] is False
