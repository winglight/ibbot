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

    const BADGE_CLASS_MAP = {
        idle: ['bg-secondary'],
        running: ['bg-info'],
        success: ['bg-success'],
        error: ['bg-danger'],
    };

    const BADGE_LABELS = {
        idle: '已下线',
        running: '重启中',
        success: '运行中',
        error: '不健康',
    };

    function formatMessage(template, replacements = {}) {
        if (typeof template !== 'string') {
            return '';
        }
        return template.replace(/\{([^}]+)\}/g, (match, key) => {
            if (Object.prototype.hasOwnProperty.call(replacements, key)) {
                const value = replacements[key];
                return value === undefined || value === null ? '' : String(value);
            }
            return '';
        });
    }

    function normaliseMode(value, fallback = 'docker') {
        if (typeof value === 'string') {
            const text = value.trim().toLowerCase();
            if (text === 'docker' || text === 'local') {
                return text;
            }
        }
        return fallback;
    }

    function resolveEndpoints(options = {}, initialPayload = null) {
        const endpoints = {
            docker: options.dockerEndpoint
                || options?.endpoints?.docker
                || '/services/restart',
            local: options.localEndpoint
                || options?.endpoints?.local
                || '/services/restart/local',
        };

        if (initialPayload && typeof initialPayload.restart_endpoint === 'string') {
            const mode = normaliseMode(initialPayload.restart_mode);
            if (mode === 'local') {
                endpoints.local = initialPayload.restart_endpoint;
            } else if (mode === 'docker') {
                endpoints.docker = initialPayload.restart_endpoint;
            }
        }

        return endpoints;
    }

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

    function applyStateClasses(element, state, classMap, fallback = ['bg-secondary']) {
        if (!element || !element.classList) {
            return;
        }
        const knownClasses = new Set(fallback);
        Object.values(classMap || {}).forEach((classes) => {
            (classes || []).forEach((cls) => knownClasses.add(cls));
        });
        knownClasses.forEach((cls) => element.classList.remove(cls));
        const target = (classMap && classMap[state]) || fallback;
        if (target && target.length) {
            target.forEach((cls) => element.classList.add(cls));
        }
    }

    function createListController(options = {}) {
        const doc = options.document || root.document;
        if (!doc) {
            throw new Error('document is required to bind service restart controls');
        }

        const container = options.container
            || (options.containerId ? doc.getElementById(options.containerId) : null);
        if (!container) {
            return null;
        }

        const fetcher = typeof options.fetcher === 'function' ? options.fetcher : null;
        if (!fetcher) {
            throw new Error('fetcher function is required');
        }

        const initialPayload = options.initialPayload || null;
        const endpoints = resolveEndpoints(options, initialPayload);
        const initialMode = normaliseMode(options.mode || initialPayload?.restart_mode || 'docker');

        const itemSelector = options.itemSelector || '[data-service]';
        const serviceElements = Array.from(container.querySelectorAll(itemSelector));
        if (!serviceElements.length) {
            return null;
        }

        const includeDependencies = options.includeDependencies !== false;
        const badgeLabels = { ...BADGE_LABELS, ...(options.badgeLabels || {}) };
        const badgeClassMap = { ...BADGE_CLASS_MAP, ...(options.badgeClassMap || {}) };
        const readOnlyMessage = options.readOnlyMessage || '访客模式无法执行重启操作';
        const initialTemplate = typeof options.initialMessage === 'string'
            ? options.initialMessage
            : '点击重启以重新启动 {service}。';

        const controller = {
            state: 'idle',
            lastResult: null,
            includeDependencies,
            options,
            container,
            items: {},
            isReadOnly: false,
            mode: initialMode,
            endpoints,
            getLabel(serviceName) {
                const item = this.items[serviceName];
                if (item?.label) {
                    return item.label;
                }
                if (typeof options.describeService === 'function') {
                    const described = options.describeService(serviceName, item?.element || null);
                    if (described) {
                        return described;
                    }
                }
                const element = item?.element || container.querySelector(`${itemSelector}[data-service="${serviceName}"]`);
                const label = element?.dataset?.serviceLabel
                    || element?.querySelector('.fw-semibold')?.textContent
                    || serviceName;
                return (label || serviceName).trim();
            },
            updateStatus(serviceName, state, message, payload, meta = {}) {
                const item = this.items[serviceName];
                if (!item) {
                    return;
                }
                item.state = state;
                if (item.status) {
                    if (item.status.dataset) {
                        item.status.dataset.state = state;
                    }
                    if (typeof message === 'string') {
                        item.status.textContent = message;
                        item.lastMessage = message;
                    }
                    applyStatusClass(item.status, state);
                } else if (typeof message === 'string') {
                    item.lastMessage = message;
                }
                if (item.badge) {
                    if (item.badge.dataset) {
                        item.badge.dataset.state = state;
                    }
                    const badgeText = badgeLabels[state];
                    if (badgeText) {
                        item.badge.textContent = formatMessage(badgeText, {
                            service: this.getLabel(serviceName),
                            state,
                        });
                    }
                    applyStateClasses(item.badge, state, badgeClassMap);
                }
                if (meta.skipStateUpdate !== true) {
                    this.state = state;
                }
                if (meta.silent !== true && typeof options.onStatusChange === 'function') {
                    options.onStatusChange({
                        state,
                        message,
                        payload,
                        controller: this,
                        service: serviceName,
                        meta,
                    });
                }
            },
            setMode(value) {
                this.mode = normaliseMode(value, this.mode);
                return this.mode;
            },
            getEndpoint() {
                if (this.mode === 'local') {
                    return this.endpoints.local || this.endpoints.docker;
                }
                return this.endpoints.docker;
            },
            setItemBusy(serviceName, busy) {
                const item = this.items[serviceName];
                if (!item) {
                    return;
                }
                if (item.button) {
                    if (busy) {
                        item.button.disabled = true;
                    } else {
                        item.button.disabled = this.isReadOnly;
                    }
                }
                if (item.spinner?.classList) {
                    item.spinner.classList.toggle('d-none', !busy);
                }
            },
            async restart(serviceName) {
                const targetService = serviceName;
                if (!targetService || !this.items[targetService]) {
                    return null;
                }
                if (this.isReadOnly) {
                    this.updateStatus(targetService, 'error', readOnlyMessage, { error: { isReadOnly: true } }, {
                        trigger: 'restart',
                    });
                    return null;
                }
                const item = this.items[targetService];
                if (item.state === 'running') {
                    return null;
                }

                this.setItemBusy(targetService, true);

                const label = this.getLabel(targetService) || targetService;
                const runningMessage = formatMessage(options.runningMessage, { service: label })
                    || `正在重启 ${label}...`;
                this.updateStatus(targetService, 'running', runningMessage, null, { trigger: 'restart' });

                try {
                    const request = {
                        endpoint: this.getEndpoint(),
                        payload: {
                            service: targetService,
                            include_dependencies: includeDependencies,
                        },
                        mode: this.mode,
                    };
                    const payload = await fetcher(request);
                    this.lastResult = payload;
                    if (payload && typeof payload.mode === 'string') {
                        this.setMode(payload.mode);
                    }
                    if (payload && typeof payload.restart_endpoint === 'string') {
                        if (this.mode === 'local') {
                            this.endpoints.local = payload.restart_endpoint;
                        } else {
                            this.endpoints.docker = payload.restart_endpoint;
                        }
                    }
                    const restarted = Array.isArray(payload?.restarted) && payload.restarted.length
                        ? payload.restarted
                        : [targetService];
                    const joined = restarted.join('、');
                    let message;
                    if (payload && typeof payload.message === 'string' && !options.ignoreResponseMessage) {
                        message = payload.message;
                    } else if (options.successMessage) {
                        message = formatMessage(options.successMessage, {
                            service: label,
                            services: joined,
                        });
                    } else {
                        message = `已重启 ${joined}`;
                    }
                    this.updateStatus(targetService, 'success', message, payload, { trigger: 'restart' });
                    restarted.forEach((name) => {
                        if (name === targetService || !this.items[name]) {
                            return;
                        }
                        const dependencyLabel = this.getLabel(name);
                        const depMessage = options.dependencySuccessMessage
                            ? formatMessage(options.dependencySuccessMessage, {
                                service: dependencyLabel,
                                source: label,
                            })
                            : `已重启 ${dependencyLabel}`;
                        this.updateStatus(name, 'success', depMessage, payload, {
                            trigger: 'dependency',
                            silent: true,
                            skipStateUpdate: true,
                        });
                    });
                    return payload;
                } catch (error) {
                    const friendly = error?.isReadOnly
                        ? readOnlyMessage
                        : (error?.message || options.errorMessage || '重启失败');
                    this.updateStatus(targetService, 'error', friendly, { error }, { trigger: 'restart' });
                    throw error;
                } finally {
                    this.setItemBusy(targetService, false);
                }
            },
            setReadOnly(value) {
                this.isReadOnly = !!value;
                Object.entries(this.items).forEach(([serviceName, item]) => {
                    if (item.button) {
                        item.button.disabled = this.isReadOnly;
                    }
                    if (item.spinner?.classList) {
                        item.spinner.classList.add('d-none');
                    }
                    if (this.isReadOnly) {
                        this.updateStatus(serviceName, 'error', readOnlyMessage, null, {
                            trigger: 'readOnly',
                            silent: true,
                            skipStateUpdate: true,
                        });
                    } else if (item.lastMessage === readOnlyMessage || !item.lastMessage) {
                        this.updateStatus(serviceName, 'idle', item.defaultIdleMessage, null, {
                            trigger: 'readOnly',
                            silent: true,
                            skipStateUpdate: true,
                        });
                    }
                });
            },
        };

        serviceElements.forEach((element) => {
            const serviceName = element?.dataset?.service;
            if (!serviceName || controller.items[serviceName]) {
                return;
            }
            const button = element.querySelector(options.buttonSelector || '[data-action="restart"]');
            if (!button) {
                return;
            }
            const spinner = options.spinnerSelector
                ? element.querySelector(options.spinnerSelector)
                : button.querySelector('.spinner-border');
            const statusEl = element.querySelector(options.statusSelector || '[data-role="status-text"]');
            const badgeEl = element.querySelector(options.badgeSelector || '[data-role="status-badge"]');
            const label = typeof options.describeService === 'function'
                ? (options.describeService(serviceName, element) || element.dataset?.serviceLabel)
                : element.dataset?.serviceLabel;
            const resolvedLabel = (label || element.querySelector('.fw-semibold')?.textContent || serviceName).trim();
            const defaultIdleMessage = formatMessage(initialTemplate, { service: resolvedLabel }) || initialTemplate;

            controller.items[serviceName] = {
                element,
                button,
                spinner,
                status: statusEl,
                badge: badgeEl,
                state: 'idle',
                lastMessage: defaultIdleMessage,
                defaultIdleMessage,
                label: resolvedLabel,
            };

            if (spinner?.classList) {
                spinner.classList.add('d-none');
            }

            button.addEventListener('click', () => {
                controller.restart(serviceName).catch(() => {});
            });

            controller.updateStatus(serviceName, 'idle', defaultIdleMessage, null, {
                trigger: 'init',
                silent: true,
                skipStateUpdate: true,
            });
        });

        if (!Object.keys(controller.items).length) {
            return null;
        }

        return controller;
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
        const initialPayload = options.initialPayload || null;
        const endpoints = resolveEndpoints(options, initialPayload);
        const initialMode = normaliseMode(options.mode || initialPayload?.restart_mode || 'docker');

        const controller = {
            state: 'idle',
            lastResult: null,
            elements: { button, select, status, spinner },
            options,
            mode: initialMode,
            endpoints,
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
            setMode(value) {
                this.mode = normaliseMode(value, this.mode);
                return this.mode;
            },
            getEndpoint() {
                if (this.mode === 'local') {
                    return this.endpoints.local || this.endpoints.docker;
                }
                return this.endpoints.docker;
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
                    const request = {
                        endpoint: this.getEndpoint(),
                        payload: {
                            service: targetService,
                            include_dependencies: includeDependencies,
                        },
                        mode: this.mode,
                    };
                    const payload = await fetcher(request);
                    this.lastResult = payload;
                    if (payload && typeof payload.mode === 'string') {
                        this.setMode(payload.mode);
                    }
                    if (payload && typeof payload.restart_endpoint === 'string') {
                        if (this.mode === 'local') {
                            this.endpoints.local = payload.restart_endpoint;
                        } else {
                            this.endpoints.docker = payload.restart_endpoint;
                        }
                    }
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
    module.createListController = createListController;
    module.applyStatusClass = applyStatusClass;
})(typeof window !== 'undefined' ? window : globalThis);
