(function (root) {
    const globalScope = root || (typeof window !== 'undefined' ? window : globalThis);
    const appModules = globalScope.appModules = globalScope.appModules || {};
    const module = appModules.serviceStatus = appModules.serviceStatus || {};

    function createServiceItem(doc, entry) {
        const item = doc.createElement('div');
        item.className = 'service-status-item list-group-item d-flex flex-wrap justify-content-between align-items-center gap-3';
        item.dataset.service = entry.service;
        const datasetLabel = entry.display_name
            ? `${entry.display_name} (${entry.service})`
            : entry.service;
        item.dataset.serviceLabel = datasetLabel;
        if (entry.internal !== undefined) {
            item.dataset.serviceInternal = entry.internal ? 'true' : 'false';
        }
        if (entry.restart_service) {
            item.dataset.restartService = entry.restart_service;
        }

        const meta = doc.createElement('div');
        meta.className = 'service-status-meta';

        const name = doc.createElement('div');
        name.className = 'fw-semibold service-name';
        name.textContent = entry.display_name || entry.service;
        meta.appendChild(name);

        const description = doc.createElement('div');
        description.className = 'service-description small text-muted';
        description.textContent = entry.description || entry.display_name || entry.service;
        meta.appendChild(description);

        const statusText = doc.createElement('div');
        statusText.className = 'service-status-text small text-muted';
        statusText.dataset.role = 'status-text';
        statusText.textContent = '等待状态更新';
        meta.appendChild(statusText);

        const actions = doc.createElement('div');
        actions.className = 'service-status-actions d-flex align-items-center gap-2';

        const badge = doc.createElement('span');
        badge.className = 'badge bg-secondary';
        badge.dataset.role = 'status-badge';
        badge.textContent = '已下线';
        actions.appendChild(badge);

        const button = doc.createElement('button');
        button.className = 'btn btn-outline-warning btn-sm d-flex align-items-center gap-2';
        button.type = 'button';
        button.dataset.action = 'restart';

        const spinner = doc.createElement('span');
        spinner.className = 'spinner-border spinner-border-sm d-none';
        spinner.setAttribute('role', 'status');
        spinner.setAttribute('aria-hidden', 'true');
        button.appendChild(spinner);

        const icon = doc.createElement('i');
        icon.className = 'fas fa-rotate-right';
        button.appendChild(icon);

        const labelSpan = doc.createElement('span');
        labelSpan.className = 'restart-label';
        labelSpan.textContent = '重启';
        button.appendChild(labelSpan);

        actions.appendChild(button);

        item.appendChild(meta);
        item.appendChild(actions);

        return item;
    }

    function renderList(options = {}) {
        const doc = options.document || globalScope.document;
        const container = options.container;
        const registry = Array.isArray(options.registry)
            ? options.registry.filter((entry) => entry && entry.service)
            : [];

        if (!doc || !container) {
            return { rendered: 0 };
        }

        container.innerHTML = '';
        registry.forEach((entry) => {
            container.appendChild(createServiceItem(doc, entry));
        });

        return { rendered: registry.length };
    }

    module.renderList = renderList;
})(typeof window !== 'undefined' ? window : globalThis);
