(function (global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const module = appModules.serviceStatus = appModules.serviceStatus || {};

    let buildPromise = null;

    const DEFAULT_PLACEHOLDER = '<div class="list-group-item text-center text-muted py-3" data-role="placeholder">' +
        '<i class="fas fa-spinner fa-spin me-2"></i>加载服务列表...</div>';

    function createElement(tag, className, text) {
        const element = document.createElement(tag);
        if (className) {
            element.className = className;
        }
        if (typeof text === 'string') {
            element.textContent = text;
        }
        return element;
    }

    function createButton() {
        const button = createElement('button', 'btn btn-outline-warning btn-sm d-flex align-items-center gap-2');
        button.type = 'button';
        button.dataset.action = 'restart';

        const spinner = createElement('span', 'spinner-border spinner-border-sm d-none');
        spinner.setAttribute('role', 'status');
        spinner.setAttribute('aria-hidden', 'true');
        button.appendChild(spinner);

        button.appendChild(createElement('i', 'fas fa-rotate-right'));
        button.appendChild(createElement('span', 'restart-label', '重启'));
        return { button, spinner };
    }

    function createServiceItem(entry) {
        const item = createElement(
            'div',
            'service-status-item list-group-item d-flex flex-wrap justify-content-between align-items-center gap-3'
        );
        item.dataset.service = entry.service;
        item.dataset.serviceLabel = entry.display_name || entry.service;
        if (typeof entry.internal === 'boolean') {
            item.dataset.internal = entry.internal ? 'true' : 'false';
        }
        if (entry.display_name) {
            item.dataset.displayName = entry.display_name;
        }
        if (entry.description) {
            item.dataset.description = entry.description;
        }
        if (entry.port) {
            item.dataset.port = entry.port;
        }
        if (entry.health_key) {
            item.dataset.healthKey = entry.health_key;
        }
        if (entry.default_health_key) {
            item.dataset.defaultHealthKey = entry.default_health_key;
        }
        if (entry.implementation_status) {
            item.dataset.implementationStatus = entry.implementation_status;
        }

        const meta = createElement('div', 'service-status-meta');
        meta.appendChild(createElement('div', 'fw-semibold service-name', entry.display_name || entry.service));
        meta.appendChild(createElement('div', 'service-description small text-muted', entry.description || ''));

        const statusText = createElement('div', 'service-status-text small text-muted', '等待状态更新');
        statusText.dataset.role = 'status-text';
        meta.appendChild(statusText);

        const actions = createElement('div', 'service-status-actions d-flex align-items-center gap-2');
        const badge = createElement('span', 'badge bg-secondary', '已下线');
        badge.dataset.role = 'status-badge';
        actions.appendChild(badge);

        const { button, spinner } = createButton();
        actions.appendChild(button);

        item.appendChild(meta);
        item.appendChild(actions);

        item._statusElements = {
            status: statusText,
            badge,
            button,
            spinner,
        };

        return item;
    }

    async function defaultFetcher() {
        const response = await fetch('/api/services/status', {
            method: 'GET',
            credentials: 'include',
        });
        if (!response.ok) {
            throw new Error(`Failed to load service registry: ${response.status}`);
        }
        return response.json();
    }

    module.ensureList = function ensureList(options = {}) {
        if (buildPromise) {
            return buildPromise;
        }

        const doc = options.document || root.document;
        if (!doc) {
            return Promise.reject(new Error('document is required to build service list'));
        }

        const container = options.container
            || (options.containerId ? doc.getElementById(options.containerId) : doc.getElementById('serviceStatusList'));
        if (!container) {
            return Promise.resolve(null);
        }

        const fetcher = typeof options.fetcher === 'function' ? options.fetcher : defaultFetcher;

        buildPromise = (async () => {
            let payload;
            try {
                payload = await fetcher('/services/status', { method: 'GET', useCache: false });
            } catch (error) {
                console.warn('Failed to fetch service registry', error);
                container.innerHTML = DEFAULT_PLACEHOLDER;
                buildPromise = null;
                throw error;
            }

            const services = Array.isArray(payload?.services) ? payload.services : [];
            if (!services.length) {
                container.innerHTML = DEFAULT_PLACEHOLDER;
                return payload;
            }

            const fragment = doc.createDocumentFragment();
            services.forEach((entry) => {
                if (!entry || typeof entry !== 'object') {
                    return;
                }
                fragment.appendChild(createServiceItem(entry));
            });

            container.innerHTML = '';
            container.appendChild(fragment);
            return payload;
        })();

        return buildPromise;
    };
})(typeof window !== 'undefined' ? window : this);

