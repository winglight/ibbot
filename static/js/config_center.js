(function (global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const namespace = appModules.configCenter = appModules.configCenter || {};

    const noop = () => {};

    function resolveElement(document, target) {
        if (!target) return null;
        if (typeof target === 'string') {
            return document?.getElementById?.(target) || null;
        }
        return target || null;
    }

    function formatTimestamp(value) {
        if (!value) return '';
        try {
            const date = new Date(value);
            if (!Number.isNaN(date.getTime())) {
                return date.toLocaleString();
            }
        } catch (error) {
            console.warn('Failed to format timestamp', error);
        }
        return String(value);
    }

    function createManager(options = {}) {
        const document = options.document || (typeof global.document !== 'undefined' ? global.document : null);
        const moduleSelect = resolveElement(document, options.moduleSelectId);
        const textarea = resolveElement(document, options.textareaId);
        const saveButton = resolveElement(document, options.saveButtonId);
        const resetButton = resolveElement(document, options.resetButtonId);
        const reloadButton = resolveElement(document, options.reloadButtonId);
        const editorContainer = resolveElement(document, options.editorId);
        const messageInput = resolveElement(document, options.messageInputId);
        const statusText = resolveElement(document, options.statusTextId);
        const historyList = resolveElement(document, options.historyListId);
        const historyReload = resolveElement(document, options.historyReloadId);

        if (!moduleSelect || !textarea || !saveButton || !editorContainer) {
            console.warn('Config center requires module select, textarea, save button, and editor container');
            return null;
        }

        const state = {
            currentModule: '',
            initialJson: '',
            readOnly: false,
            loading: false,
            modules: [],
            history: [],
        };

        const callbacks = {
            loadModules: typeof options.loadModules === 'function' ? options.loadModules : async () => ({ modules: [] }),
            loadConfig: typeof options.loadConfig === 'function' ? options.loadConfig : async () => ({ module: '', values: {} }),
            saveConfig: typeof options.saveConfig === 'function' ? options.saveConfig : async () => ({ success: false }),
            loadHistory: typeof options.loadHistory === 'function' ? options.loadHistory : async () => ({ entries: [] }),
            rollback: typeof options.rollback === 'function' ? options.rollback : async () => ({ success: false }),
            onStatus: typeof options.onStatus === 'function' ? options.onStatus : noop,
        };

        function setStatus(message, tone = 'muted') {
            if (!statusText) return;
            statusText.textContent = message || '';
            if (statusText.classList?.remove) {
                statusText.classList.remove('text-muted', 'text-danger', 'text-success');
                statusText.classList.add(tone === 'error' ? 'text-danger' : tone === 'success' ? 'text-success' : 'text-muted');
            }
        }

        function toggleEditor(visible) {
            if (!editorContainer) return;
            if (editorContainer.classList?.toggle) {
                editorContainer.classList.toggle('d-none', !visible);
            } else {
                editorContainer.hidden = !visible;
            }
        }

        function updateControls() {
            const disabled = state.readOnly || state.loading || !state.currentModule;
            if (textarea) textarea.disabled = disabled;
            if (messageInput) messageInput.disabled = disabled;
            if (saveButton) saveButton.disabled = disabled;
            if (resetButton) resetButton.disabled = state.loading || !state.currentModule;
            if (historyList?.querySelectorAll) {
                const buttons = historyList.querySelectorAll('button[data-version]');
                buttons.forEach((btn) => {
                    btn.disabled = state.readOnly || state.loading;
                });
            }
        }

        function normaliseValues(values) {
            if (!values || typeof values !== 'object') return {};
            const result = {};
            for (const [key, value] of Object.entries(values)) {
                result[key] = value;
            }
            return result;
        }

        function setTextareaFromValues(values) {
            const content = JSON.stringify(normaliseValues(values), null, 2);
            textarea.value = content;
            state.initialJson = content;
        }

        function resetEditor() {
            textarea.value = state.initialJson;
            if (messageInput) messageInput.value = '';
            setStatus('已重置为最新配置');
        }

        function renderHistory(entries) {
            state.history = Array.isArray(entries) ? entries.slice() : [];
            if (!historyList) {
                return;
            }
            if (!Array.isArray(entries) || entries.length === 0) {
                historyList.innerHTML = '<li class="list-group-item text-muted">暂无历史版本</li>';
                updateControls();
                return;
            }

            const fragments = entries.map((entry) => {
                const version = entry.version || '';
                const by = entry.updated_by || 'unknown';
                const at = formatTimestamp(entry.updated_at);
                const action = entry.action === 'rollback' ? '回滚' : '更新';
                const message = entry.message ? `<div class="text-muted">${entry.message}</div>` : '';
                return (
                    `<li class="list-group-item d-flex flex-column gap-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <div>
                                <strong>${action}</strong>
                                <span class="text-muted ms-2">${by}</span>
                                <span class="text-muted ms-2">${at}</span>
                            </div>
                            <button type="button" class="btn btn-outline-secondary btn-sm" data-version="${version}">
                                <i class="fas fa-undo me-1"></i>回滚
                            </button>
                        </div>
                        <code class="small text-break">${version}</code>
                        ${message}
                    </li>`
                );
            });
            historyList.innerHTML = fragments.join('');
            updateControls();
        }

        async function refreshHistory(module) {
            if (!module) {
                renderHistory([]);
                return;
            }
            try {
                const response = await callbacks.loadHistory(module);
                renderHistory(response?.entries || []);
            } catch (error) {
                console.error('Failed to load config history', error);
                renderHistory([]);
                setStatus('历史记录加载失败', 'error');
            }
        }

        async function selectModule(module) {
            const trimmed = (module || '').trim();
            state.currentModule = trimmed;
            if (!trimmed) {
                toggleEditor(false);
                setStatus('请选择需要编辑的服务');
                updateControls();
                return;
            }

            state.loading = true;
            updateControls();
            setStatus('正在加载配置…');
            try {
                const response = await callbacks.loadConfig(trimmed);
                setTextareaFromValues(response?.values || {});
                toggleEditor(true);
                await refreshHistory(trimmed);
                setStatus('配置已加载', 'success');
            } catch (error) {
                console.error('Failed to load config module', trimmed, error);
                toggleEditor(false);
                setStatus('配置加载失败', 'error');
                callbacks.onStatus('error', '配置加载失败');
            } finally {
                state.loading = false;
                updateControls();
            }
        }

        async function refreshModules() {
            try {
                const response = await callbacks.loadModules();
                const modules = Array.isArray(response?.modules) ? response.modules.map((m) => String(m)) : [];
                state.modules = modules;
                const previous = moduleSelect.value;

                if (moduleSelect.options && typeof moduleSelect.options.length === 'number') {
                    const length = moduleSelect.options.length;
                    for (let idx = length - 1; idx >= 0; idx -= 1) {
                        if (moduleSelect.remove) {
                            moduleSelect.remove(idx);
                        }
                    }
                }

                if (moduleSelect.innerHTML !== undefined) {
                    const options = ['<option value="">选择服务模块</option>'];
                    for (const module of modules) {
                        options.push(`<option value="${module}">${module}</option>`);
                    }
                    moduleSelect.innerHTML = options.join('');
                }

                if (modules.includes(previous)) {
                    moduleSelect.value = previous;
                    await selectModule(previous);
                } else if (modules.length > 0) {
                    moduleSelect.value = modules[0];
                    await selectModule(modules[0]);
                } else {
                    moduleSelect.value = '';
                    await selectModule('');
                }
            } catch (error) {
                console.error('Failed to load config module list', error);
                setStatus('无法加载服务列表', 'error');
                callbacks.onStatus('error', '无法加载服务列表');
            }
        }

        async function saveCurrentModule() {
            if (!state.currentModule || state.readOnly || state.loading) {
                return;
            }

            let parsed;
            const raw = textarea.value.trim();
            if (!raw) {
                parsed = {};
            } else {
                try {
                    parsed = JSON.parse(raw);
                } catch (error) {
                    setStatus('JSON 解析失败，请检查格式', 'error');
                    callbacks.onStatus('error', 'JSON 解析失败，请检查格式');
                    return;
                }
            }

            state.loading = true;
            updateControls();
            setStatus('正在保存配置…');

            try {
                const payload = { values: parsed };
                if (messageInput?.value?.trim()) {
                    payload.message = messageInput.value.trim();
                }
                const response = await callbacks.saveConfig(state.currentModule, payload);
                setTextareaFromValues(response?.values || parsed);
                if (messageInput) messageInput.value = '';
                setStatus('配置已保存', 'success');
                callbacks.onStatus('success', '配置已保存');
                await refreshHistory(state.currentModule);
            } catch (error) {
                console.error('Failed to save configuration', error);
                setStatus('保存失败', 'error');
                callbacks.onStatus('error', '保存配置失败');
            } finally {
                state.loading = false;
                updateControls();
            }
        }

        async function rollbackVersion(version) {
            if (!version || !state.currentModule || state.readOnly) {
                return;
            }
            state.loading = true;
            updateControls();
            setStatus('正在回滚配置…');
            try {
                const payload = { version };
                if (messageInput?.value?.trim()) {
                    payload.message = messageInput.value.trim();
                }
                const response = await callbacks.rollback(state.currentModule, payload);
                setTextareaFromValues(response?.values || {});
                if (messageInput) messageInput.value = '';
                setStatus('已回滚到历史版本', 'success');
                callbacks.onStatus('success', '已回滚到历史版本');
                await refreshHistory(state.currentModule);
            } catch (error) {
                console.error('Failed to rollback configuration', error);
                setStatus('回滚失败', 'error');
                callbacks.onStatus('error', '回滚失败');
            } finally {
                state.loading = false;
                updateControls();
            }
        }

        moduleSelect.addEventListener?.('change', (event) => {
            const target = event?.target || moduleSelect;
            void selectModule(target?.value || '');
        });

        if (reloadButton) {
            reloadButton.addEventListener?.('click', () => {
                void refreshModules();
            });
        }

        if (saveButton) {
            saveButton.addEventListener?.('click', () => {
                void saveCurrentModule();
            });
        }

        if (resetButton) {
            resetButton.addEventListener?.('click', () => {
                resetEditor();
            });
        }

        if (historyReload) {
            historyReload.addEventListener?.('click', () => {
                void refreshHistory(state.currentModule);
            });
        }

        if (historyList) {
            historyList.addEventListener?.('click', (event) => {
                const version = event?.target?.dataset?.version;
                if (version) {
                    event.preventDefault?.();
                    void rollbackVersion(version);
                }
            });
        }

        toggleEditor(false);
        setStatus('正在加载服务列表…');
        void refreshModules();

        return {
            setReadOnly(flag) {
                state.readOnly = !!flag;
                updateControls();
            },
            refreshModules,
            selectModule,
            rollbackVersion,
            save: saveCurrentModule,
            reset: resetEditor,
            getState() {
                return {
                    module: state.currentModule,
                    readOnly: state.readOnly,
                    loading: state.loading,
                    initialJson: state.initialJson,
                    modules: state.modules.slice(),
                    history: state.history.slice(),
                };
            },
            elements: {
                moduleSelect,
                textarea,
                saveButton,
                resetButton,
                reloadButton,
                editorContainer,
                messageInput,
                statusText,
                historyList,
            },
        };
    }

    namespace.createManager = createManager;
    namespace.ready = true;

    const rootTarget = typeof root !== 'undefined' ? root : null;
    const dispatchReadyEvent = (target) => {
        if (!target || typeof target.dispatchEvent !== 'function') {
            return;
        }

        let event = null;
        if (typeof Event === 'function') {
            event = new Event('configCenter:ready');
        } else if (typeof CustomEvent === 'function') {
            event = new CustomEvent('configCenter:ready');
        }

        if (!event) {
            return;
        }

        try {
            target.dispatchEvent(event);
        } catch (error) {
            if (typeof console !== 'undefined' && typeof console.warn === 'function') {
                console.warn('Failed to dispatch config center ready event', error);
            }
        }
    };

    if (rootTarget) {
        dispatchReadyEvent(rootTarget);
    }
})(typeof window !== 'undefined' ? window : globalThis);

