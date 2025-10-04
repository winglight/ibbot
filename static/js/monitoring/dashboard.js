(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const monitoring = appModules.monitoring = appModules.monitoring || {};

    const REFRESH_STATE = {
        idle: 'idle',
        loading: 'loading',
        error: 'error',
    };

    const COLORS = [
        '#60a5fa', '#34d399', '#fbbf24', '#f97316', '#f87171', '#a855f7', '#22d3ee',
        '#2dd4bf', '#c084fc', '#f472b6', '#eab308', '#818cf8', '#ef4444', '#0ea5e9'
    ];

    const DATA_FIELD_GROUPS = [
        { key: 'configs', title: '配置 (hash:config:*)', emptyText: '暂无配置数据' },
        { key: 'orders', title: '订单结果 (hash:ib:result:*)', emptyText: '暂无订单结果数据' },
        { key: 'subscriptions', title: '订阅快照 (hash:market:snapshot:*)', emptyText: '暂无订阅数据' },
    ];

    class MonitoringDashboard {
        constructor(app) {
            this.app = app;
            this.queueRefreshInterval = 15000;
            this.dbRefreshInterval = 60000;
            this.queueTimer = null;
            this.dbTimer = null;
            this.dbChart = null;
            this.dbData = [];
            this.dbPending = null;
            this.queuePending = null;
            this.dbWsUnsubscribe = null;
            this.queueWsUnsubscribe = null;
            this.state = {
                queue: REFRESH_STATE.idle,
                db: REFRESH_STATE.idle,
            };
            this.clearingRedisCache = false;

            this.elements = {
                queueSourceBadge: document.getElementById('queueMetricsSource'),
                queueUpdatedAt: document.getElementById('queueMetricsUpdatedAt'),
                queueError: document.getElementById('queueMetricsError'),
                queueTableBody: document.querySelector('#queueMetricsTable tbody'),
                streamTableBody: document.querySelector('#streamMetricsTable tbody'),
                dataFieldsContainer: document.getElementById('queueDataFieldsContainer'),
                dataFieldsStatus: document.getElementById('queueDataFieldsStatus'),
                dbSummary: document.getElementById('dbSummaryText'),
                dbWindow: document.getElementById('dbWindowSelect'),
                dbService: document.getElementById('dbServiceSelect'),
                dbMetric: document.getElementById('dbMetricInput'),
                queueClearBtn: document.getElementById('clearRedisCacheBtn'),
                queueRefreshBtn: document.getElementById('refreshQueueMetricsBtn'),
                dbRefreshBtn: document.getElementById('refreshDbMetricsBtn'),
            };

            if (this.app && typeof this.app.registerWebSocketHandler === 'function') {
                this.dbWsUnsubscribe = this.app.registerWebSocketHandler(
                    'metrics_write_rate',
                    (payload) => this.handleDbWriteRate(payload)
                );
                this.queueWsUnsubscribe = this.app.registerWebSocketHandler(
                    'monitoring_queues',
                    (payload) => this.handleQueueMetrics(payload)
                );
            }
        }

        init() {
            this.bindEvents();
            this.refreshQueueMetrics({ immediate: true }).catch(() => {});
            this.refreshDbMetrics({ immediate: true }).catch(() => {});
        }

        bindEvents() {
            if (this.elements.queueRefreshBtn) {
                this.elements.queueRefreshBtn.addEventListener('click', () => {
                    this.refreshQueueMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.queueClearBtn) {
                this.elements.queueClearBtn.addEventListener('click', () => {
                    this.clearRedisCache().catch(() => {});
                });
            }
            if (this.elements.dbRefreshBtn) {
                this.elements.dbRefreshBtn.addEventListener('click', () => {
                    this.refreshDbMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.dbWindow) {
                this.elements.dbWindow.addEventListener('change', () => {
                    this.refreshDbMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.dbService) {
                this.elements.dbService.addEventListener('change', () => {
                    this.refreshDbMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.dbMetric) {
                this.elements.dbMetric.addEventListener('change', () => {
                    this.refreshDbMetrics({ manual: true }).catch(() => {});
                });
                this.elements.dbMetric.addEventListener('keyup', (event) => {
                    if (event.key === 'Enter') {
                        this.refreshDbMetrics({ manual: true }).catch(() => {});
                    }
                });
            }
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) {
                    this.clearTimers();
                } else {
                    this.refreshQueueMetrics().catch(() => {});
                    this.refreshDbMetrics().catch(() => {});
                }
            });
        }

        async clearRedisCache() {
            if (this.clearingRedisCache) {
                return;
            }

            this.clearingRedisCache = true;
            const button = this.elements.queueClearBtn;
            const originalHtml = button ? button.innerHTML : null;

            if (button) {
                button.disabled = true;
                button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>清理中';
            }

            try {
                await this.app.apiCall('/monitoring/queues/clear-cache', {
                    method: 'POST',
                    body: JSON.stringify({}),
                    timeout: 20000,
                });
                this.app?.showAlert?.('success', 'Redis 缓存已清理');
                await this.refreshQueueMetrics({ manual: true });
            } catch (error) {
                console.error('Failed to clear Redis cache', error);
                const isReadOnly = error?.code === 'READ_ONLY' || error?.isReadOnly;
                const message = isReadOnly ? '访客模式无法执行清理操作' : (error?.message || '清理 Redis 缓存失败');
                this.app?.showAlert?.('danger', message);
            } finally {
                if (button) {
                    button.disabled = false;
                    button.innerHTML = originalHtml;
                }
                this.clearingRedisCache = false;
            }
        }

        destroy() {
            this.clearTimers();
            if (typeof this.dbWsUnsubscribe === 'function') {
                try {
                    this.dbWsUnsubscribe();
                } catch (error) {
                    console.warn('Failed to unsubscribe metrics write rate websocket handler', error);
                }
                this.dbWsUnsubscribe = null;
            }
            if (typeof this.queueWsUnsubscribe === 'function') {
                try {
                    this.queueWsUnsubscribe();
                } catch (error) {
                    console.warn('Failed to unsubscribe queue websocket handler', error);
                }
                this.queueWsUnsubscribe = null;
            }
            this.dbPending = null;
            this.queuePending = null;
        }

        shouldUseWebSocket() {
            const ws = this.app?.ibkrManager?.ws;
            return !!ws && ws.readyState === WebSocket.OPEN;
        }

        clearTimers() {
            if (this.queueTimer) {
                clearTimeout(this.queueTimer);
                this.queueTimer = null;
            }
            if (this.dbTimer) {
                clearTimeout(this.dbTimer);
                this.dbTimer = null;
            }
        }

        scheduleQueueRefresh() {
            if (this.queueTimer || document.hidden) return;
            this.queueTimer = setTimeout(() => {
                this.queueTimer = null;
                this.refreshQueueMetrics().catch(() => {});
            }, this.queueRefreshInterval);
        }

        scheduleDbRefresh() {
            if (this.dbTimer || document.hidden) return;
            this.dbTimer = setTimeout(() => {
                this.dbTimer = null;
                this.refreshDbMetrics().catch(() => {});
            }, this.dbRefreshInterval);
        }

        handleDbError(message, { scheduleNext = true } = {}) {
            const summary = this.elements.dbSummary;
            if (summary) {
                summary.textContent = message || '无法加载写入速率';
            }
            this.state.db = REFRESH_STATE.error;
            if (scheduleNext) {
                this.scheduleDbRefresh();
            }
        }

        handleDbWriteRate(payload) {
            if (!payload || payload.type !== 'metrics_write_rate') {
                return false;
            }

            const context = this.dbPending && payload.request_id === this.dbPending.requestId
                ? this.dbPending
                : null;
            if (context) {
                if (context.timer) {
                    clearTimeout(context.timer);
                }
                this.dbPending = null;
            }

            if (!payload.success) {
                const message = payload?.error || '无法加载写入速率';
                this.handleDbError(message, { scheduleNext: !context?.immediate });
                if (context && typeof context.reject === 'function') {
                    context.reject(new Error(message));
                }
                return true;
            }

            try {
                this.renderDbMetrics(payload || {});
                this.state.db = REFRESH_STATE.idle;
                if (context && typeof context.resolve === 'function') {
                    context.resolve(payload);
                }
            } catch (error) {
                const message = error?.message || '渲染写入速率失败';
                this.handleDbError(message, { scheduleNext: !context?.immediate });
                if (context && typeof context.reject === 'function') {
                    context.reject(error);
                }
                return true;
            }

            if (!context?.immediate) {
                this.scheduleDbRefresh();
            }
            return true;
        }

        async refreshQueueMetrics({ manual = false, immediate = false } = {}) {
            if (this.state.queue === REFRESH_STATE.loading && !manual) {
                this.scheduleQueueRefresh();
                return;
            }

            this.state.queue = REFRESH_STATE.loading;
            if (this.elements.queueError) {
                this.elements.queueError.textContent = '';
            }
            if (this.elements.dataFieldsContainer) {
                this.elements.dataFieldsContainer.innerHTML = '<div class="text-muted">加载中...</div>';
            }
            if (this.elements.dataFieldsStatus) {
                this.elements.dataFieldsStatus.textContent = '加载中...';
            }

            const params = new URLSearchParams();
            // Allow custom queue targets via configuration in the future.

            const useWebSocket = this.shouldUseWebSocket() && this.app?.ibkrManager?.requestMonitoringQueues;
            if (useWebSocket) {
                return new Promise((resolve, reject) => {
                    if (this.queuePending && this.queuePending.timer) {
                        clearTimeout(this.queuePending.timer);
                        this.queuePending = null;
                    }

                    const requestId = `queues:${Date.now()}:${Math.random().toString(16).slice(2)}`;
                    const context = { requestId, resolve, reject, immediate };
                    this.queuePending = context;
                    context.timer = setTimeout(() => {
                        if (this.queuePending === context) {
                            this.queuePending = null;
                            const error = new Error('队列指标请求超时');
                            this.handleQueueError(error.message, { scheduleNext: !immediate });
                            reject(error);
                        }
                    }, manual ? 15000 : 20000);

                    const result = this.app.ibkrManager.requestMonitoringQueues({
                        requestId,
                        force: manual,
                        minInterval: manual ? 1000 : undefined,
                    });

                    if (result === 'failed') {
                        if (context.timer) {
                            clearTimeout(context.timer);
                        }
                        if (this.queuePending === context) {
                            this.queuePending = null;
                        }
                        const error = new Error('队列指标请求发送失败');
                        this.handleQueueError(error.message, { scheduleNext: !immediate });
                        reject(error);
                    }
                }).finally(() => {
                    if (!immediate) {
                        this.scheduleQueueRefresh();
                    }
                });
            }

            try {
                const data = await this.app.apiCall(`/monitoring/queues?${params.toString()}`, {
                    useCache: false,
                    cacheTime: 0,
                    timeout: manual ? 10000 : 15000,
                });
                this.renderQueueMetrics(data || {});
                this.state.queue = REFRESH_STATE.idle;
            } catch (error) {
                console.error('Failed to load queue metrics', error);
                this.handleQueueError(error?.message || '加载失败');
            } finally {
                if (!immediate) {
                    this.scheduleQueueRefresh();
                }
            }
        }

        handleQueueError(message, { scheduleNext = true } = {}) {
            if (this.elements.queueError) {
                this.elements.queueError.textContent = message || '加载失败';
            }
            this.renderDataFields(null, message || '加载失败');
            this.state.queue = REFRESH_STATE.error;
            if (scheduleNext) {
                this.scheduleQueueRefresh();
            }
        }

        handleQueueMetrics(payload) {
            if (!payload || payload.type !== 'monitoring_queues') {
                return false;
            }

            const context = this.queuePending && payload.request_id === this.queuePending.requestId
                ? this.queuePending
                : null;
            if (context) {
                if (context.timer) {
                    clearTimeout(context.timer);
                }
                this.queuePending = null;
            }

            if (payload.success === false) {
                const message = payload?.error || '加载失败';
                this.handleQueueError(message, { scheduleNext: !context?.immediate });
                if (context && typeof context.reject === 'function') {
                    context.reject(new Error(message));
                }
                return true;
            }

            try {
                this.renderQueueMetrics(payload || {});
                this.state.queue = REFRESH_STATE.idle;
                if (context && typeof context.resolve === 'function') {
                    context.resolve(payload);
                }
            } catch (error) {
                const message = error?.message || '渲染队列指标失败';
                this.handleQueueError(message, { scheduleNext: !context?.immediate });
                if (context && typeof context.reject === 'function') {
                    context.reject(error);
                }
                return true;
            }

            if (!context?.immediate) {
                this.scheduleQueueRefresh();
            }
            return true;
        }

        formatNumber(value, options = {}) {
            const numeric = Number(value);
            if (!Number.isFinite(numeric)) {
                return '—';
            }
            if (options.maximumFractionDigits !== undefined || options.minimumFractionDigits !== undefined) {
                try {
                    return numeric.toLocaleString('en-US', options);
                } catch (err) {
                    return numeric.toFixed(options.maximumFractionDigits ?? 2);
                }
            }
            return this.app?.formatNumber?.(numeric) ?? numeric.toString();
        }

        formatDuration(seconds) {
            if (!Number.isFinite(seconds) || seconds <= 0) {
                return '—';
            }
            if (this.app?.formatDuration) {
                return this.app.formatDuration(seconds);
            }
            if (seconds < 60) {
                return `${seconds.toFixed(0)} 秒`;
            }
            const minutes = seconds / 60;
            if (minutes < 60) {
                return `${minutes.toFixed(1)} 分钟`;
            }
            const hours = minutes / 60;
            return `${hours.toFixed(1)} 小时`;
        }

        renderQueueMetrics(data) {
            const queues = Array.isArray(data?.queues) ? data.queues : [];
            const streams = Array.isArray(data?.streams) ? data.streams : [];

            if (this.elements.queueSourceBadge) {
                const source = data?.source === 'snapshot' ? 'Snapshot' : 'Live';
                this.elements.queueSourceBadge.textContent = source;
                this.elements.queueSourceBadge.className = `badge ${data?.source === 'snapshot' ? 'bg-info' : 'bg-success'}`;
            }

            if (this.elements.queueUpdatedAt) {
                this.elements.queueUpdatedAt.textContent = data?.timestamp
                    ? new Date(data.timestamp).toLocaleString()
                    : '—';
            }

            if (this.elements.queueTableBody) {
                if (!queues.length) {
                    this.elements.queueTableBody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-3">暂无队列数据</td></tr>';
                } else {
                    this.elements.queueTableBody.innerHTML = queues.map((entry) => {
                        const consumption = Number.isFinite(entry?.consumption_rate)
                            ? `${this.formatNumber(entry.consumption_rate, { maximumFractionDigits: 2 })}/s`
                            : '—';
                        const stale = this.formatDuration(Number(entry?.stale_seconds ?? 0));
                        return `
                            <tr>
                                <td>${this.escapeHtml(entry?.name || entry?.queue_key || '-')}</td>
                                <td><code>${this.escapeHtml(entry?.queue_key || '-')}</code></td>
                                <td class="text-end">${this.formatNumber(entry?.length ?? 0, { maximumFractionDigits: 0 })}</td>
                                <td class="text-end">${consumption}</td>
                                <td class="text-end">${stale}</td>
                            </tr>
                        `;
                    }).join('');
                }
            }

            if (this.elements.streamTableBody) {
                if (!streams.length) {
                    this.elements.streamTableBody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">暂无 Stream 数据</td></tr>';
                } else {
                    this.elements.streamTableBody.innerHTML = streams.map((entry) => {
                        const consumption = Number.isFinite(entry?.consumption_rate)
                            ? `${this.formatNumber(entry.consumption_rate, { maximumFractionDigits: 2 })}/s`
                            : '—';
                        const stale = this.formatDuration(Number(entry?.stale_seconds ?? 0));
                        return `
                            <tr>
                                <td>${this.escapeHtml(entry?.name || entry?.stream_key || '-')}</td>
                                <td><code>${this.escapeHtml(entry?.stream_key || '-')}</code></td>
                                <td><code>${this.escapeHtml(entry?.group || '-')}</code></td>
                                <td class="text-end">${this.formatNumber(entry?.pending ?? 0, { maximumFractionDigits: 0 })}</td>
                                <td class="text-end">${consumption}</td>
                                <td class="text-end">${stale}</td>
                            </tr>
                        `;
                    }).join('');
                }
            }

            this.renderDataFields(data?.data_fields);
        }

        renderDataFields(dataFields, errorMessage) {
            const container = this.elements.dataFieldsContainer;
            if (!container) {
                return;
            }

            const status = this.elements.dataFieldsStatus;

            if (errorMessage) {
                container.innerHTML = `<div class="text-danger small">${this.escapeHtml(errorMessage)}</div>`;
                if (status) {
                    status.textContent = '加载失败';
                }
                return;
            }

            const groups = Array.isArray(DATA_FIELD_GROUPS) ? DATA_FIELD_GROUPS : [];
            if (!dataFields || typeof dataFields !== 'object') {
                container.innerHTML = '<div class="text-muted small">暂无 Redis 数据概览</div>';
                if (status) {
                    status.textContent = '';
                }
                return;
            }

            const sections = [];
            const statusParts = [];
            groups.forEach((group) => {
                const info = dataFields[group.key];
                sections.push(this.renderDataFieldGroup(group, info));
                if (info?.error) {
                    statusParts.push(`${group.title.split(' ')[0]}异常`);
                } else if (Array.isArray(info?.samples) && info.samples.length) {
                    statusParts.push(`${group.title.split(' ')[0]}${info.samples.length}项`);
                }
            });

            container.innerHTML = sections.join('') || '<div class="text-muted small">暂无 Redis 数据概览</div>';
            if (status) {
                status.textContent = statusParts.length ? statusParts.join(' · ') : '已刷新';
            }
        }

        renderDataFieldGroup(group, info) {
            const title = group?.title || group?.key || '';
            const emptyText = group?.emptyText || '暂无数据';
            const header = `
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <span class="text-uppercase text-muted small">${this.escapeHtml(title)}</span>
                    ${info?.has_more ? '<span class="badge bg-secondary">更多...</span>' : ''}
                </div>
            `;

            if (!info) {
                return `<div class="redis-data-group mb-3">${header}<div class="text-muted small">${this.escapeHtml(emptyText)}</div></div>`;
            }

            if (info.error) {
                return `<div class="redis-data-group mb-3">${header}<div class="text-danger small">${this.escapeHtml(info.error)}</div></div>`;
            }

            const samples = Array.isArray(info.samples) ? info.samples : [];
            if (!samples.length) {
                return `<div class="redis-data-group mb-3">${header}<div class="text-muted small">${this.escapeHtml(emptyText)}</div></div>`;
            }

            const list = samples.map((sample) => this.renderDataFieldSample(sample)).join('');
            return `<div class="redis-data-group mb-3">${header}<div class="d-flex flex-column gap-2">${list}</div></div>`;
        }

        renderDataFieldSample(sample) {
            const label = sample?.label || sample?.key || '未命名';
            const key = sample?.key ? `<code class="small text-muted">${this.escapeHtml(sample.key)}</code>` : '';
            const fields = Array.isArray(sample?.fields) ? sample.fields : [];
            let content = '';
            if (sample?.error) {
                content = `<span class="text-danger">${this.escapeHtml(sample.error)}</span>`;
            } else if (fields.length) {
                content = fields.map((field) => {
                    const name = field?.field ? this.escapeHtml(field.field) : '';
                    const value = field?.value ? this.escapeHtml(this.truncateValue(field.value, 60)) : '';
                    const suffix = value ? `=${value}` : '';
                    return `<code class="me-1">${name}${suffix}</code>`;
                }).join('');
                if (sample.has_more_fields) {
                    content += '<span class="badge bg-secondary ms-1">更多...</span>';
                }
            } else {
                content = '<span class="text-muted">暂无字段</span>';
            }

            return `
                <div class="border rounded px-2 py-2">
                    <div class="d-flex flex-wrap justify-content-between align-items-start gap-2">
                        <span class="fw-semibold">${this.escapeHtml(label)}</span>
                        ${key}
                    </div>
                    <div class="small mt-2">${content}</div>
                </div>
            `;
        }

        truncateValue(value, maxLength = 60) {
            if (value === null || value === undefined) {
                return '';
            }
            const text = String(value);
            if (text.length <= maxLength) {
                return text;
            }
            const sliceEnd = Math.max(0, maxLength - 1);
            return `${text.slice(0, sliceEnd)}…`;
        }

        escapeHtml(value) {
            if (!value && value !== 0) return '';
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        async refreshDbMetrics({ manual = false, immediate = false } = {}) {
            if (this.state.db === REFRESH_STATE.loading && !manual) {
                this.scheduleDbRefresh();
                return;
            }

            this.state.db = REFRESH_STATE.loading;
            if (this.elements.dbSummary) {
                this.elements.dbSummary.textContent = '加载中...';
            }

            const windowValue = this.elements.dbWindow?.value || '1h';
            const serviceValue = this.elements.dbService?.value || '';
            const metricValue = this.elements.dbMetric?.value?.trim();
            const useWebSocket = this.shouldUseWebSocket() && this.app?.ibkrManager?.requestMetricsWriteRate;
            if (useWebSocket) {
                return new Promise((resolve, reject) => {
                    if (this.dbPending && this.dbPending.timer) {
                        clearTimeout(this.dbPending.timer);
                        this.dbPending = null;
                    }

                    const requestId = `db:${Date.now()}:${Math.random().toString(16).slice(2)}`;
                    const context = { requestId, resolve, reject, immediate };
                    this.dbPending = context;
                    context.timer = setTimeout(() => {
                        if (this.dbPending === context) {
                            this.dbPending = null;
                            const error = new Error('写入速率请求超时');
                            this.handleDbError(error.message, { scheduleNext: !immediate });
                            reject(error);
                        }
                    }, manual ? 20000 : 25000);

                    const result = this.app.ibkrManager.requestMetricsWriteRate({
                        window: windowValue,
                        service: serviceValue || undefined,
                        metric: metricValue || undefined,
                        aggregate: 'sum',
                        requestId,
                        force: manual,
                        minInterval: manual ? 1000 : undefined,
                    });

                    if (result === 'failed') {
                        if (context.timer) {
                            clearTimeout(context.timer);
                        }
                        if (this.dbPending === context) {
                            this.dbPending = null;
                        }
                        const error = new Error('写入速率请求发送失败');
                        this.handleDbError(error.message, { scheduleNext: !immediate });
                        reject(error);
                    }
                });
            }

            try {
                const params = new URLSearchParams();
                params.set('window', windowValue);
                if (serviceValue) params.set('service', serviceValue);
                if (metricValue) params.set('metric', metricValue);
                const data = await this.app.apiCall(`/monitoring/metrics/write-rate?${params.toString()}`, {
                    useCache: false,
                    cacheTime: 0,
                    timeout: manual ? 15000 : 20000,
                });
                if (data && data.success === false) {
                    const message = data.error || '无法加载写入速率';
                    this.handleDbError(message, { scheduleNext: !immediate });
                    return data;
                }
                this.renderDbMetrics(data || {});
                this.state.db = REFRESH_STATE.idle;
            } catch (error) {
                console.error('Failed to load metrics write rate', error);
                this.handleDbError(error?.message || '无法加载写入速率', { scheduleNext: !immediate });
                throw error;
            } finally {
                if (!immediate) {
                    this.scheduleDbRefresh();
                }
            }
        }

        ensureDbChart() {
            if (this.dbChart) {
                return this.dbChart;
            }
            const canvas = document.getElementById('dbWriteRateChart');
            if (!canvas || !canvas.getContext) return null;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;
            if (!root.Chart || typeof root.Chart !== 'function') return null;

            this.dbChart = new root.Chart(ctx, {
                type: 'line',
                data: { datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'nearest', intersect: false },
                    scales: {
                        x: {
                            type: 'time',
                            adapters: {
                                date: window.luxon || undefined,
                            },
                            time: { unit: 'minute' },
                        },
                        y: {
                            title: { display: true, text: 'Points / 1m' },
                            ticks: { callback: (value) => this.formatNumber(value, { maximumFractionDigits: 2 }) },
                        },
                    },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => {
                                    const value = context.parsed?.y;
                                    return `${context.dataset?.label || 'Series'}: ${this.formatNumber(value, { maximumFractionDigits: 2 })}`;
                                },
                            },
                        },
                    },
                },
            });
            return this.dbChart;
        }

        renderDbMetrics(data) {
            const chart = this.ensureDbChart();
            if (!chart) {
                if (this.elements.dbSummary) {
                    this.elements.dbSummary.textContent = 'Chart.js 未加载，无法显示图表';
                }
                return;
            }

            if (data && data.success === false) {
                const message = data.error || '无法加载写入速率';
                this.handleDbError(message, { scheduleNext: true });
                return;
            }

            const series = Array.isArray(data?.series) ? data.series : [];
            const grouped = {};
            series.forEach((entry) => {
                const label = entry?.metric || entry?.queue_key || entry?.service || 'unknown';
                if (!grouped[label]) {
                    grouped[label] = [];
                }
                if (entry?.time && Number.isFinite(entry?.value)) {
                    grouped[label].push({ x: entry.time, y: Number(entry.value) });
                }
            });

            const datasetEntries = Object.entries(grouped);
            if (!datasetEntries.length) {
                chart.data.datasets = [];
                chart.update('none');
                if (this.elements.dbSummary) {
                    this.elements.dbSummary.textContent = '暂无写入数据';
                }
                return;
            }

            chart.data.datasets = datasetEntries.map(([label, points], index) => {
                const color = COLORS[index % COLORS.length];
                return {
                    label,
                    data: points.sort((a, b) => new Date(a.x).getTime() - new Date(b.x).getTime()),
                    borderColor: color,
                    backgroundColor: color,
                    tension: 0.25,
                    pointRadius: 0,
                    fill: false,
                };
            });
            chart.update('none');

            if (this.elements.dbSummary) {
                const summary = data?.summary || {};
                const text = Object.entries(summary)
                    .map(([label, stats]) => {
                        const avg = Number(stats?.average || 0);
                        const points = Number(stats?.points || 0);
                        return `${label}: 平均 ${this.formatNumber(avg, { maximumFractionDigits: 2 })}/m (${points} 点)`;
                    })
                    .join(' ・ ');
                this.elements.dbSummary.textContent = text || '暂无统计数据';
            }
        }
    }

    monitoring.dashboard = monitoring.dashboard || {};
    monitoring.dashboard.MonitoringDashboard = MonitoringDashboard;

    if (typeof root.MonitoringDashboard === 'undefined') {
        root.MonitoringDashboard = MonitoringDashboard;
    }
})(typeof window !== 'undefined' ? window : globalThis);

