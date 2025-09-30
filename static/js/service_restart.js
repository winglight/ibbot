(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const module = appModules.serviceRestart = appModules.serviceRestart || {};

    const STATUS_CLASS_MAP = {
        idle: ['text-muted'],
        running: ['text-info'],
        success: ['text-success'],
        error: ['text-danger']
    };

    function applyStatusClass(element, state) {
        if (!element || !element.classList) return;
        const allClasses = new Set(['text-muted', 'text-info', 'text-success', 'text-danger']);
        allClasses.forEach((cls) => element.classList.remove(cls));
        const target = STATUS_CLASS_MAP[state];
        if (target && target.length) {
            target.forEach((cls) => element.classList.add(cls));
        } else {
            element.classList.add('text-muted');
        }
    }

    function getSelectedLabel(select, fallback, describeService) {
        if (typeof describeService === 'function') {
            return describeService(fallback, select) || fallback;
        }
        const option = select?.selectedOptions?.[0];
        if (option) {
            const label = option.dataset?.label || option.textContent || option.value;
            return (label || '').trim() || fallback;
        }
        return fallback;
    }

    function createController(options = {}) {
        const doc = options.document || root.document;
        if (!doc) {
            throw new Error('document is required to bind service restart controls');
        }

        const button = options.button || (options.buttonId ? doc.getElementById(options.buttonId) : null);
        const select = options.select || (options.selectId ? doc.getElementById(options.selectId) : null);
        const status = options.status || (options.statusId ? doc.getElementById(options.statusId) : null);

        if (!button || !select || !status) {
            return null;
        }

        const spinner = options.spinner
            || (options.spinnerSelector ? button.querySelector(options.spinnerSelector) : null);
        const fetcher = typeof options.fetcher === 'function' ? options.fetcher : null;
        if (!fetcher) {
            throw new Error('fetcher function is required');
        }

        const includeDependencies = options.includeDependencies !== false;

        const controller = {
            state: 'idle',
            lastResult: null,
            elements: { button, select, status, spinner },
            options,
            updateStatus(state, message, payload) {
                this.state = state;
                if (status.dataset) {
                    status.dataset.state = state;
                }
                if (typeof message === 'string') {
                    status.textContent = message;
                }
                applyStatusClass(status, state);
                if (typeof options.onStatusChange === 'function') {
                    options.onStatusChange({ state, message, payload, controller: this });
                }
            },
            getSelectedService() {
                return select.value || '';
            },
            getSelectedLabel(service) {
                return getSelectedLabel(select, service || this.getSelectedService(), options.describeService);
            },
            async restart(serviceName) {
                const targetService = serviceName || this.getSelectedService();
                if (!targetService) {
                    this.updateStatus('error', options.missingServiceMessage || '请选择需要重启的服务');
                    return null;
                }
                if (this.state === 'running') {
                    return null;
                }

                button.disabled = true;
                if (spinner?.classList) {
                    spinner.classList.remove('d-none');
                }

                const label = this.getSelectedLabel(targetService) || targetService;
                const runningMessage = options.runningMessage
                    ? options.runningMessage.replace('{service}', label)
                    : `正在重启 ${label}...`;
                this.updateStatus('running', runningMessage);

                try {
                    const payload = await fetcher({
                        service: targetService,
                        include_dependencies: includeDependencies,
                    });
                    this.lastResult = payload;
                    let message;
                    if (payload && typeof payload.message === 'string' && !options.ignoreResponseMessage) {
                        message = payload.message;
                    } else if (payload && Array.isArray(payload?.restarted) && payload.restarted.length) {
                        const joined = payload.restarted.join('、');
                        message = options.successMessage
                            ? options.successMessage.replace('{services}', joined).replace('{service}', label)
                            : `已重启 ${joined}`;
                    } else {
                        message = options.successMessage
                            ? options.successMessage.replace('{service}', label)
                            : `已重启 ${label}`;
                    }
                    this.updateStatus('success', message, payload);
                    return payload;
                } catch (error) {
                    const friendly = error?.isReadOnly
                        ? options.readOnlyMessage || '访客模式无法执行重启操作'
                        : (error?.message || options.errorMessage || '重启失败');
                    this.updateStatus('error', friendly, { error });
                    throw error;
                } finally {
                    button.disabled = false;
                    if (spinner?.classList) {
                        spinner.classList.add('d-none');
                    }
                }
            },
        };

        button.addEventListener('click', () => {
            controller.restart().catch(() => {});
        });

        if (spinner?.classList) {
            spinner.classList.add('d-none');
        }

        controller.updateStatus('idle', options.initialMessage || '选择服务后执行重启。');
        return controller;
    }

    module.createController = createController;
    module.applyStatusClass = applyStatusClass;
})(typeof window !== 'undefined' ? window : globalThis);
