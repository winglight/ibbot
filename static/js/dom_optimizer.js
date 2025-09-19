(function () {
    const DEFAULT_RESULT_HTML = '<div class="text-muted small">点击“立即优化”后将在此展示最新结果、精度统计以及阈值建议。</div>';
    const STORAGE_KEYS = {
        symbol: 'domOptimizer.symbol',
        lookback: 'domOptimizer.lookback',
        limit: 'domOptimizer.limit',
        pageSize: 'domOptimizer.pageSize'
    };

    const SIGNAL_TYPES = new Set(['long', 'short']);

    const STATUS_BADGE_MAP = {
        running: 'bg-info',
        succeeded: 'bg-success',
        failed: 'bg-danger',
        scheduled: 'bg-primary',
        idle: 'bg-secondary'
    };

    function toNumber(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric : null;
    }

    function formatPercent(value, digits = 1) {
        if (!Number.isFinite(value)) return '-';
        return `${(value * 100).toFixed(digits)}%`;
    }

    function formatNumber(value, digits = 2) {
        if (!Number.isFinite(value)) return '-';
        return value.toFixed(digits);
    }

    function parseCandidates(input) {
        if (!input) return null;
        const parts = String(input)
            .split(/[,\s]+/)
            .map((token) => token.trim())
            .filter(Boolean)
            .map((token) => Number(token))
            .filter((value) => Number.isFinite(value));
        return parts.length ? parts : null;
    }

    function formatIsoDatetime(value) {
        if (!value) return '-';
        const date = value instanceof Date ? value : new Date(value);
        if (Number.isNaN(date.getTime())) return '-';
        return date.toLocaleString(undefined, { hour12: false });
    }

    function formatDurationMs(durationMs) {
        if (!Number.isFinite(durationMs)) return '-';
        if (durationMs >= 60_000) {
            const minutes = durationMs / 60_000;
            return `${minutes.toFixed(minutes >= 10 ? 0 : 1)} min`;
        }
        if (durationMs >= 1_000) {
            const seconds = durationMs / 1_000;
            return `${seconds.toFixed(seconds >= 10 ? 0 : 1)} s`;
        }
        return `${durationMs.toFixed(0)} ms`;
    }

    function resolveTimestampMs(payload) {
        if (!payload || typeof payload !== 'object') return null;
        const candidates = [
            payload.timestamp,
            payload.timestamp_ms,
            payload.timestampMs,
            payload.timestamp_ns ? payload.timestamp_ns / 1_000_000 : null,
            payload.timestampNs ? payload.timestampNs / 1_000_000 : null,
            payload.ts_ns ? payload.ts_ns / 1_000_000 : null
        ];
        for (const value of candidates) {
            if (typeof value === 'number' && Number.isFinite(value)) {
                if (value > 9_999_999_999_999) return value / 1_000;
                if (value > 9_999_999_999) return value;
                if (value > 9_999_999) return value * 1_000;
                return value * 1_000;
            }
            if (typeof value === 'string' && value) {
                const parsed = Number(value);
                if (Number.isFinite(parsed)) {
                    if (parsed > 9_999_999_999_999) return parsed / 1_000;
                    if (parsed > 9_999_999_999) return parsed;
                    return parsed * 1_000;
                }
                const date = new Date(value);
                if (!Number.isNaN(date.getTime())) {
                    return date.getTime();
                }
            }
        }
        return null;
    }

    function resolvePrice(payload) {
        if (!payload || typeof payload !== 'object') return null;
        const fields = ['signal_price', 'mid_price', 'price'];
        for (const key of fields) {
            const value = payload[key];
            const num = Number(value);
            if (Number.isFinite(num)) return num;
        }
        return null;
    }

    function computeStats(metrics) {
        if (!Array.isArray(metrics) || !metrics.length) {
            return {
                total: 0,
                long: 0,
                short: 0,
                evaluated: 0,
                success: 0,
                failure: 0,
                pending: 0,
                precision: null
            };
        }
        let long = 0;
        let short = 0;
        let success = 0;
        let failure = 0;
        let evaluated = 0;
        let pending = 0;

        for (const item of metrics) {
            if (!item || !SIGNAL_TYPES.has(item.signal_type)) continue;
            if (item.signal_type === 'long') long += 1;
            if (item.signal_type === 'short') short += 1;
            if (item.outcome === 1) {
                evaluated += 1;
                success += 1;
            } else if (item.outcome === 0) {
                evaluated += 1;
                failure += 1;
            } else {
                pending += 1;
            }
        }

        const total = long + short;
        const precision = evaluated > 0 ? success / evaluated : null;
        return {
            total,
            long,
            short,
            success,
            failure,
            evaluated,
            pending,
            precision
        };
    }

    class DOMOptimizerUI {
        constructor() {
            this.app = null;
            this.elements = {};
            this.state = {
                signals: [],
                filteredSignals: [],
                currentPage: 1,
                pageSize: 25,
                loading: {
                    stats: false,
                    signals: false,
                    status: false,
                    optimize: false
                }
            };
            this._polling = {
                status: null,
                stats: null
            };
            this._readOnlyWrapped = false;
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', () => this.init(), { once: true });
            } else {
                this.init();
            }
        }

        init() {
            this.cacheElements();
            if (!this.elements.section) {
                return;
            }
            this.bindDomEvents();
            this.restorePreferences();
            this.resetOptimizeResult();
            this.updateStatsIndicators(null);
            this.attachAppWhenReady();
        }

        cacheElements() {
            this.elements = {
                section: document.getElementById('domOptimizerSection'),
                statusBadge: document.getElementById('domOptimizerStatusBadge'),
                refreshStatusBtn: document.getElementById('domOptimizerRefreshStatusBtn'),
                loadConfigBtn: document.getElementById('domOptimizerLoadConfigBtn'),
                symbolInput: document.getElementById('domOptimizerSymbolInput'),
                useMonitorBtn: document.getElementById('domOptimizerUseMonitorBtn'),
                lookbackInput: document.getElementById('domOptimizerLookbackInput'),
                statsLimitInput: document.getElementById('domOptimizerStatsLimitInput'),
                minSignalsInput: document.getElementById('domOptimizerMinSignalsInput'),
                outcomeWindowInput: document.getElementById('domOptimizerOutcomeWindowInput'),
                obiCandidatesInput: document.getElementById('domOptimizerObiCandidatesInput'),
                ofiCandidatesInput: document.getElementById('domOptimizerOfiCandidatesInput'),
                refreshStatsBtn: document.getElementById('domOptimizerRefreshStatsBtn'),
                statsTotal: document.getElementById('domOptimizerStatsTotal'),
                statsBySide: document.getElementById('domOptimizerStatsBySide'),
                statsSuccessRate: document.getElementById('domOptimizerStatsSuccessRate'),
                statsPending: document.getElementById('domOptimizerStatsPending'),
                statusText: document.getElementById('domOptimizerStatusText'),
                lastRun: document.getElementById('domOptimizerLastRun'),
                lastDuration: document.getElementById('domOptimizerLastDuration'),
                nextRun: document.getElementById('domOptimizerNextRun'),
                lastResultPanel: document.getElementById('domOptimizerLastResultPanel'),
                lastResult: document.getElementById('domOptimizerLastResult'),
                startInput: document.getElementById('domOptimizerStartInput'),
                endInput: document.getElementById('domOptimizerEndInput'),
                pageSizeInput: document.getElementById('domOptimizerPageSizeInput'),
                outcomeFilter: document.getElementById('domOptimizerOutcomeFilter'),
                querySignalsBtn: document.getElementById('domOptimizerQuerySignalsBtn'),
                signalsTable: document.getElementById('domOptimizerSignalsTable'),
                signalsSummary: document.getElementById('domOptimizerSignalsSummary'),
                pagination: document.getElementById('domOptimizerPagination'),
                currentPage: document.getElementById('domOptimizerCurrentPage'),
                totalPages: document.getElementById('domOptimizerTotalPages'),
                optimizeBtn: document.getElementById('domOptimizerOptimizeBtn'),
                clearResultBtn: document.getElementById('domOptimizerClearResultBtn'),
                applySwitch: document.getElementById('domOptimizerApplySwitch'),
                optimizeResult: document.getElementById('domOptimizerOptimizeResult')
            };
        }

        bindDomEvents() {
            const {
                refreshStatusBtn,
                loadConfigBtn,
                useMonitorBtn,
                refreshStatsBtn,
                querySignalsBtn,
                pagination,
                optimizeBtn,
                clearResultBtn,
                lookbackInput,
                statsLimitInput,
                pageSizeInput,
                symbolInput
            } = this.elements;

            refreshStatusBtn?.addEventListener('click', () => this.refreshAutomationStatus({ force: true }));
            loadConfigBtn?.addEventListener('click', () => this.loadCurrentConfig());
            useMonitorBtn?.addEventListener('click', () => this.fillSymbolFromMonitor());
            refreshStatsBtn?.addEventListener('click', () => this.refreshStatistics());
            querySignalsBtn?.addEventListener('click', () => this.querySignals());
            optimizeBtn?.addEventListener('click', () => this.handleOptimize());
            clearResultBtn?.addEventListener('click', () => this.resetOptimizeResult());

            pagination?.addEventListener('click', (event) => {
                const button = event.target instanceof Element ? event.target.closest('button[data-page]') : null;
                if (!button || button.disabled) return;
                const direction = button.dataset.page;
                this.turnPage(direction);
            });

            symbolInput?.addEventListener('change', () => this.persistPreference(STORAGE_KEYS.symbol, symbolInput.value.trim()));
            lookbackInput?.addEventListener('change', () => this.persistPreference(STORAGE_KEYS.lookback, lookbackInput.value));
            statsLimitInput?.addEventListener('change', () => this.persistPreference(STORAGE_KEYS.limit, statsLimitInput.value));
            if (pageSizeInput) {
                pageSizeInput.addEventListener('change', () => {
                    const parsed = parseInt(pageSizeInput.value, 10);
                    if (Number.isFinite(parsed) && parsed > 0) {
                        this.state.pageSize = parsed;
                        this.persistPreference(STORAGE_KEYS.pageSize, parsed);
                        this.applySignalFilter();
                    } else {
                        this.persistPreference(STORAGE_KEYS.pageSize, '');
                    }
                });
            }
            this.elements.outcomeFilter?.addEventListener('change', () => this.applySignalFilter());
        }

        restorePreferences() {
            if (!this.elements.section) return;
            const { symbolInput, lookbackInput, statsLimitInput, pageSizeInput } = this.elements;
            const storedSymbol = localStorage.getItem(STORAGE_KEYS.symbol);
            const storedLookback = localStorage.getItem(STORAGE_KEYS.lookback);
            const storedLimit = localStorage.getItem(STORAGE_KEYS.limit);
            const storedPageSize = localStorage.getItem(STORAGE_KEYS.pageSize);

            if (storedSymbol && symbolInput) symbolInput.value = storedSymbol;
            if (storedLookback && lookbackInput) lookbackInput.value = storedLookback;
            if (storedLimit && statsLimitInput) statsLimitInput.value = storedLimit;
            if (storedPageSize && pageSizeInput) pageSizeInput.value = storedPageSize;

            if (pageSizeInput) {
                const parsed = parseInt(pageSizeInput.value, 10);
                if (Number.isFinite(parsed) && parsed > 0) {
                    this.state.pageSize = parsed;
                }
            }
        }

        attachAppWhenReady() {
            if (this.app || !this.elements.section) return;
            if (typeof window !== 'undefined' && window.app) {
                this.attachApp(window.app);
                return;
            }
            setTimeout(() => this.attachAppWhenReady(), 200);
        }

        attachApp(appInstance) {
            if (!appInstance || this.app) return;
            this.app = appInstance;
            this.wrapReadOnlyHandler();
            this.syncReadOnlyState(this.app?.isReadOnly);
            this.bootstrap();
        }

        wrapReadOnlyHandler() {
            if (!this.app || this._readOnlyWrapped) return;
            const original = typeof this.app.updateReadOnlyMode === 'function'
                ? this.app.updateReadOnlyMode.bind(this.app)
                : null;
            if (original) {
                this.app.updateReadOnlyMode = (...args) => {
                    const result = original(...args);
                    this.syncReadOnlyState(this.app.isReadOnly);
                    return result;
                };
            }
            this._readOnlyWrapped = true;
        }

        bootstrap() {
            this.refreshAutomationStatus({ initial: true }).catch(() => {});
            const symbol = this.elements.symbolInput?.value?.trim();
            if (symbol) {
                this.refreshStatistics().catch(() => {});
            }
            this.startPolling();
        }

        startPolling() {
            this.stopPolling();
            this._polling.status = window.setInterval(() => {
                this.refreshAutomationStatus().catch(() => {});
            }, 60_000);
            this._polling.stats = window.setInterval(() => {
                const symbol = this.elements.symbolInput?.value?.trim();
                if (symbol) {
                    this.refreshStatistics({ silent: true }).catch(() => {});
                }
            }, 120_000);
        }

        stopPolling() {
            if (this._polling.status) {
                clearInterval(this._polling.status);
                this._polling.status = null;
            }
            if (this._polling.stats) {
                clearInterval(this._polling.stats);
                this._polling.stats = null;
            }
        }

        syncReadOnlyState(isReadOnly) {
            const { optimizeBtn, applySwitch } = this.elements;
            const disabled = Boolean(isReadOnly);
            if (optimizeBtn) optimizeBtn.disabled = disabled;
            if (applySwitch) applySwitch.disabled = disabled;
        }

        persistPreference(key, value) {
            if (!key) return;
            if (value === undefined || value === null || value === '') {
                localStorage.removeItem(key);
            } else {
                localStorage.setItem(key, String(value));
            }
        }

        notify(level, message) {
            if (this.app && typeof this.app.showAlert === 'function') {
                this.app.showAlert(level, message);
                return;
            }
            if (level === 'error' || level === 'danger') {
                console.error(message);
            } else if (level === 'warning') {
                console.warn(message);
            } else {
                console.log(message);
            }
        }

        async apiCall(endpoint, options = {}) {
            if (this.app && typeof this.app.apiCall === 'function') {
                return this.app.apiCall(endpoint, options);
            }
            const requestOptions = {
                method: options.method || 'GET',
                headers: {
                    'Content-Type': 'application/json',
                    ...(options.headers || {})
                }
            };
            if (options.body) {
                requestOptions.body = typeof options.body === 'string'
                    ? options.body
                    : JSON.stringify(options.body);
            }
            const response = await fetch(`/api${endpoint}`, requestOptions);
            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || `Request failed (${response.status})`);
            }
            const raw = await response.text();
            return raw ? JSON.parse(raw) : {};
        }

        setLoading(key, isLoading) {
            if (!this.state.loading[key]) this.state.loading[key] = false;
            this.state.loading[key] = Boolean(isLoading);
        }

        setButtonLoading(button, isLoading, loadingLabel) {
            if (!button) return;
            if (isLoading) {
                button.dataset.originalLabel = button.dataset.originalLabel || button.innerHTML;
                button.disabled = true;
                const label = loadingLabel || '处理中...';
                button.innerHTML = `<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>${label}`;
            } else if (button.dataset.originalLabel) {
                button.disabled = false;
                button.innerHTML = button.dataset.originalLabel;
                delete button.dataset.originalLabel;
            } else {
                button.disabled = false;
            }
        }

        async refreshAutomationStatus({ initial = false, force = false } = {}) {
            if (!this.elements.section) return;
            if (!force && this.state.loading.status) return;
            try {
                this.setLoading('status', true);
                this.setButtonLoading(this.elements.refreshStatusBtn, true, '刷新中');
                const response = await this.apiCall('/dom/automation/status', {
                    allowReadOnly: true,
                    useCache: false
                });
                if (response && response.success === false) {
                    throw new Error(response.detail || '获取自动化状态失败');
                }
                this.renderAutomationStatus(response || {});
                if (initial) {
                    this.notify('success', '已加载 DOM 自动化状态');
                }
            } catch (error) {
                this.notify('error', `无法获取自动化状态: ${error?.message || error}`);
            } finally {
                this.setLoading('status', false);
                this.setButtonLoading(this.elements.refreshStatusBtn, false);
            }
        }

        renderAutomationStatus(statusPayload) {
            const status = statusPayload?.status || 'idle';
            const badgeClass = STATUS_BADGE_MAP[status] || STATUS_BADGE_MAP.idle;
            if (this.elements.statusBadge) {
                this.elements.statusBadge.className = `badge ${badgeClass} text-uppercase`;
                this.elements.statusBadge.textContent = status.toUpperCase();
            }
            if (this.elements.statusText) {
                const running = statusPayload?.is_running ? '运行中' : '空闲';
                this.elements.statusText.textContent = `${status.toUpperCase()} · ${running}`;
            }
            if (this.elements.lastRun) {
                const lastRun = statusPayload?.last_run_at || statusPayload?.last_started_at;
                this.elements.lastRun.textContent = formatIsoDatetime(lastRun);
            }
            if (this.elements.lastDuration) {
                this.elements.lastDuration.textContent = formatDurationMs(statusPayload?.last_duration_ms);
            }
            if (this.elements.nextRun) {
                const nextRun = statusPayload?.next_run_at || statusPayload?.schedule?.next_run_at;
                this.elements.nextRun.textContent = formatIsoDatetime(nextRun);
            }
            this.renderLastResult(statusPayload?.last_result);
        }

        renderLastResult(result) {
            if (!this.elements.lastResult || !this.elements.lastResultPanel) return;
            if (!result) {
                this.elements.lastResultPanel.classList.remove('border-success', 'border-danger');
                this.elements.lastResult.innerHTML = '<span class="text-muted">暂无结果</span>';
                return;
            }
            const precision = formatPercent(result.precision);
            const evaluated = Number(result.evaluated_records) || 0;
            const positives = Number(result.positive_signals) || 0;
            const negatives = Number(result.negative_signals) || 0;
            const pending = Number(result.pending_signals) || 0;
            const windowStart = formatIsoDatetime(result.history_window?.start);
            const windowEnd = formatIsoDatetime(result.history_window?.end);
            const thresholds = this.describeThresholds(result.updated_thresholds);
            const parts = [
                `<div>精度: <strong>${precision}</strong> · 评估 ${evaluated} 次 (成功 ${positives} / 失败 ${negatives} / 待评估 ${pending})</div>`,
                `<div class="text-muted">窗口: ${windowStart} → ${windowEnd}</div>`
            ];
            if (thresholds) {
                parts.push(`<div class="text-muted">阈值: ${thresholds}</div>`);
            }
            this.elements.lastResult.innerHTML = parts.join('');
            this.elements.lastResultPanel.classList.remove('border-danger', 'border-success');
            if (result.applied) {
                this.elements.lastResultPanel.classList.add('border-success');
            } else if (result.insufficient_data) {
                this.elements.lastResultPanel.classList.add('border-danger');
            }
        }

        describeThresholds(thresholds) {
            if (!thresholds || typeof thresholds !== 'object') return '';
            const longObi = toNumber(thresholds?.long?.obi5);
            const shortObi = toNumber(thresholds?.short?.obi5);
            const longOfi = toNumber(thresholds?.long?.ofi);
            const shortOfi = toNumber(thresholds?.short?.ofi);
            const chunks = [];
            if (Number.isFinite(longObi) || Number.isFinite(longOfi)) {
                chunks.push(`多头 OBI ${formatNumber(longObi ?? NaN, 2)} / OFI ${formatNumber(longOfi ?? NaN, 2)}`);
            }
            if (Number.isFinite(shortObi) || Number.isFinite(shortOfi)) {
                chunks.push(`空头 OBI ${formatNumber(shortObi ?? NaN, 2)} / OFI ${formatNumber(shortOfi ?? NaN, 2)}`);
            }
            return chunks.join(' · ');
        }

        async refreshStatistics({ silent = false } = {}) {
            if (!this.elements.section) return;
            const symbol = this.elements.symbolInput?.value?.trim();
            if (!symbol) {
                if (!silent) this.notify('warning', '请先输入交易标的后再刷新统计');
                return;
            }
            try {
                this.setLoading('stats', true);
                if (!silent) this.setButtonLoading(this.elements.refreshStatsBtn, true, '刷新中');
                const lookbackMinutes = parseInt(this.elements.lookbackInput?.value || '0', 10) || 240;
                const limit = parseInt(this.elements.statsLimitInput?.value || '0', 10) || 500;
                const now = new Date();
                const start = new Date(now.getTime() - lookbackMinutes * 60_000);
                const query = new URLSearchParams();
                query.set('start', start.toISOString());
                query.set('end', now.toISOString());
                query.set('include_snapshots', 'false');
                if (limit > 0) query.set('limit', String(limit));
                const endpoint = `/dom/history/${encodeURIComponent(symbol)}?${query.toString()}`;
                const response = await this.apiCall(endpoint, {
                    allowReadOnly: true,
                    useCache: false
                });
                if (!response || response.success === false) {
                    throw new Error(response?.detail || '获取历史数据失败');
                }
                const metrics = Array.isArray(response.metrics) ? response.metrics : [];
                const stats = computeStats(metrics);
                this.updateStatsIndicators(stats);
            } catch (error) {
                this.notify('error', `刷新统计失败: ${error?.message || error}`);
            } finally {
                this.setLoading('stats', false);
                this.setButtonLoading(this.elements.refreshStatsBtn, false);
            }
        }

        updateStatsIndicators(stats) {
            const totals = stats || {
                total: '-',
                long: '-',
                short: '-',
                evaluated: '-',
                pending: '-',
                precision: null
            };
            if (this.elements.statsTotal) this.elements.statsTotal.textContent = totals.total ?? '-';
            if (this.elements.statsBySide) this.elements.statsBySide.textContent = `${totals.long ?? '-'} / ${totals.short ?? '-'}`;
            if (this.elements.statsSuccessRate) this.elements.statsSuccessRate.textContent = totals.precision != null ? formatPercent(totals.precision) : '-';
            if (this.elements.statsPending) this.elements.statsPending.textContent = totals.pending ?? '-';
        }

        async querySignals() {
            if (!this.elements.section) return;
            const symbol = this.elements.symbolInput?.value?.trim();
            if (!symbol) {
                this.notify('warning', '请输入交易标的后再查询信号');
                return;
            }
            if (this.state.loading.signals) return;
            try {
                this.setLoading('signals', true);
                this.setButtonLoading(this.elements.querySignalsBtn, true, '查询中');
                const lookbackMinutes = parseInt(this.elements.lookbackInput?.value || '0', 10) || 240;
                const limit = parseInt(this.elements.statsLimitInput?.value || '0', 10) || 500;
                const pageSize = parseInt(this.elements.pageSizeInput?.value || '0', 10) || 25;
                this.state.pageSize = pageSize;
                this.persistPreference(STORAGE_KEYS.pageSize, pageSize);

                const startValue = this.elements.startInput?.value;
                const endValue = this.elements.endInput?.value;
                let start = startValue ? new Date(startValue) : null;
                let end = endValue ? new Date(endValue) : null;
                const now = new Date();
                if (!end || Number.isNaN(end.getTime())) end = now;
                if (!start || Number.isNaN(start.getTime())) {
                    start = new Date(end.getTime() - lookbackMinutes * 60_000);
                }
                const query = new URLSearchParams();
                query.set('start', start.toISOString());
                query.set('end', end.toISOString());
                query.set('include_snapshots', 'false');
                const signalLimit = Math.max(limit, pageSize * 5);
                query.set('limit', String(signalLimit));
                const endpoint = `/dom/history/${encodeURIComponent(symbol)}?${query.toString()}`;
                const response = await this.apiCall(endpoint, {
                    allowReadOnly: true,
                    useCache: false
                });
                if (!response || response.success === false) {
                    throw new Error(response?.detail || '查询失败');
                }
                const metrics = Array.isArray(response.metrics) ? response.metrics : [];
                const signals = metrics.filter((item) => item && SIGNAL_TYPES.has(item.signal_type));
                signals.sort((a, b) => {
                    const tsA = resolveTimestampMs(a) || 0;
                    const tsB = resolveTimestampMs(b) || 0;
                    return tsB - tsA;
                });
                this.state.signals = signals;
                this.applySignalFilter();
                this.renderSignalsSummary();
            } catch (error) {
                this.notify('error', `查询信号失败: ${error?.message || error}`);
                this.state.signals = [];
                this.state.filteredSignals = [];
                this.renderSignalsTable();
                this.renderSignalsSummary();
            } finally {
                this.setLoading('signals', false);
                this.setButtonLoading(this.elements.querySignalsBtn, false);
            }
        }

        applySignalFilter() {
            const filter = this.elements.outcomeFilter?.value || '';
            let filtered = this.state.signals.slice();
            if (filter === 'success') {
                filtered = filtered.filter((item) => item?.outcome === 1);
            } else if (filter === 'failed') {
                filtered = filtered.filter((item) => item?.outcome === 0);
            } else if (filter === 'pending') {
                filtered = filtered.filter((item) => item?.outcome !== 0 && item?.outcome !== 1);
            }
            this.state.filteredSignals = filtered;
            this.state.currentPage = 1;
            this.renderSignalsTable();
        }

        renderSignalsTable() {
            const tbody = this.elements.signalsTable?.querySelector('tbody');
            if (!tbody) return;
            tbody.innerHTML = '';
            const pageSize = this.state.pageSize || 25;
            const total = this.state.filteredSignals.length;
            const totalPages = Math.max(1, Math.ceil(total / pageSize));
            const currentPage = Math.min(this.state.currentPage, totalPages);
            this.state.currentPage = currentPage;
            const startIndex = (currentPage - 1) * pageSize;
            const pageItems = this.state.filteredSignals.slice(startIndex, startIndex + pageSize);

            if (!pageItems.length) {
                const row = document.createElement('tr');
                row.className = 'text-center text-muted';
                const cell = document.createElement('td');
                cell.colSpan = 7;
                cell.textContent = '暂无信号';
                row.appendChild(cell);
                tbody.appendChild(row);
            } else {
                for (const item of pageItems) {
                    const row = document.createElement('tr');
                    const ts = resolveTimestampMs(item);
                    const dateLabel = ts ? formatIsoDatetime(ts) : '-';
                    const price = resolvePrice(item);
                    const obi = toNumber(item?.obi5);
                    const ofi = toNumber(item?.ofi);
                    const trend = toNumber(item?.trend_score);
                    const outcomeBadge = this.renderOutcomeBadge(item?.outcome);
                    row.innerHTML = `
                        <td class="text-nowrap">${dateLabel}</td>
                        <td>${this.renderSignalType(item?.signal_type)}</td>
                        <td>${formatNumber(price ?? NaN, 2)}</td>
                        <td>${formatNumber(obi ?? NaN, 2)}</td>
                        <td>${formatNumber(ofi ?? NaN, 0)}</td>
                        <td>${formatNumber(trend ?? NaN, 2)}</td>
                        <td>${outcomeBadge}</td>
                    `;
                    tbody.appendChild(row);
                }
            }
            this.updatePaginationControls(totalPages);
            this.renderSignalsSummary();
        }

        renderSignalType(type) {
            if (type === 'long') {
                return '<span class="badge bg-success">Long</span>';
            }
            if (type === 'short') {
                return '<span class="badge bg-danger">Short</span>';
            }
            return '<span class="badge bg-secondary">N/A</span>';
        }

        renderOutcomeBadge(outcome) {
            if (outcome === 1) {
                return '<span class="badge bg-success">成功</span>';
            }
            if (outcome === 0) {
                return '<span class="badge bg-danger">失败</span>';
            }
            return '<span class="badge bg-secondary">待评估</span>';
        }

        renderSignalsSummary() {
            if (!this.elements.signalsSummary) return;
            const total = this.state.filteredSignals.length;
            const successes = this.state.filteredSignals.filter((item) => item?.outcome === 1).length;
            const failures = this.state.filteredSignals.filter((item) => item?.outcome === 0).length;
            const pending = total - successes - failures;
            this.elements.signalsSummary.textContent = `${total} 条记录 · 成功 ${successes} / 失败 ${failures} / 待评估 ${pending}`;
        }

        updatePaginationControls(totalPages) {
            const { pagination, currentPage, totalPages: totalPagesEl } = this.elements;
            if (!pagination || !currentPage || !totalPagesEl) return;
            const prevButton = pagination.querySelector('button[data-page="prev"]');
            const nextButton = pagination.querySelector('button[data-page="next"]');
            currentPage.textContent = String(this.state.currentPage || 1);
            totalPagesEl.textContent = String(totalPages || 1);
            if (prevButton) prevButton.disabled = this.state.currentPage <= 1;
            if (nextButton) nextButton.disabled = this.state.currentPage >= totalPages;
        }

        turnPage(direction) {
            const totalPages = Math.max(1, Math.ceil(this.state.filteredSignals.length / (this.state.pageSize || 25)));
            if (direction === 'prev' && this.state.currentPage > 1) {
                this.state.currentPage -= 1;
                this.renderSignalsTable();
            } else if (direction === 'next' && this.state.currentPage < totalPages) {
                this.state.currentPage += 1;
                this.renderSignalsTable();
            }
        }

        resetOptimizeResult() {
            if (this.elements.optimizeResult) {
                this.elements.optimizeResult.innerHTML = DEFAULT_RESULT_HTML;
            }
        }

        async handleOptimize() {
            if (!this.elements.section) return;
            const symbol = this.elements.symbolInput?.value?.trim();
            if (!symbol) {
                this.notify('warning', '请输入交易标的后再发起优化');
                return;
            }
            if (this.app?.isReadOnly) {
                this.notify('warning', '当前处于只读模式，无法执行优化');
                return;
            }
            try {
                this.setLoading('optimize', true);
                this.setButtonLoading(this.elements.optimizeBtn, true, '优化中');
                const lookbackMinutes = parseInt(this.elements.lookbackInput?.value || '0', 10) || 240;
                const limit = parseInt(this.elements.statsLimitInput?.value || '0', 10) || 0;
                const minSignals = parseInt(this.elements.minSignalsInput?.value || '0', 10) || 0;
                const applyUpdates = this.elements.applySwitch?.checked !== false;
                const now = new Date();
                const start = new Date(now.getTime() - lookbackMinutes * 60_000);
                const payload = {
                    symbol,
                    start: start.toISOString(),
                    end: now.toISOString(),
                    limit: limit > 0 ? limit : null,
                    apply: applyUpdates,
                    min_signals: Math.max(minSignals, 0)
                };
                const obiCandidates = parseCandidates(this.elements.obiCandidatesInput?.value);
                if (obiCandidates) payload.obi_candidates = obiCandidates;
                const ofiCandidates = parseCandidates(this.elements.ofiCandidatesInput?.value);
                if (ofiCandidates) payload.ofi_candidates = ofiCandidates;
                const response = await this.apiCall('/dom/optimize', {
                    method: 'POST',
                    body: payload,
                    useCache: false
                });
                if (!response || response.success === false) {
                    throw new Error(response?.detail || '优化失败');
                }
                this.renderOptimizeResult(response);
                this.refreshAutomationStatus({ force: true }).catch(() => {});
                this.refreshStatistics({ silent: true }).catch(() => {});
                this.notify('success', 'DOM 参数优化完成');
            } catch (error) {
                this.notify('error', `优化失败: ${error?.message || error}`);
            } finally {
                this.setLoading('optimize', false);
                this.setButtonLoading(this.elements.optimizeBtn, false);
            }
        }

        renderOptimizeResult(result) {
            if (!this.elements.optimizeResult) return;
            const precision = formatPercent(result?.precision);
            const evaluated = Number(result?.evaluated_records) || 0;
            const positives = Number(result?.positive_signals) || 0;
            const negatives = Number(result?.negative_signals) || 0;
            const pending = Number(result?.pending_signals) || 0;
            const thresholds = this.describeThresholds(result?.updated_thresholds);
            const applied = result?.applied ? '<span class="badge bg-success">已应用</span>' : '<span class="badge bg-warning text-dark">未应用</span>';
            const outcome = result?.insufficient_data ? '<span class="badge bg-warning text-dark">数据不足</span>' : applied;
            const completedAt = formatIsoDatetime(result?.completed_at);
            const rows = [
                `<div class="mb-2">状态: ${outcome} · 完成时间 ${completedAt}</div>`,
                `<div>精度: <strong>${precision}</strong> · 评估 ${evaluated} 次 (成功 ${positives} / 失败 ${negatives} / 待评估 ${pending})</div>`
            ];
            if (thresholds) rows.push(`<div class="text-muted">阈值: ${thresholds}</div>`);
            this.elements.optimizeResult.innerHTML = rows.join('');
        }

        async loadCurrentConfig() {
            try {
                this.setButtonLoading(this.elements.loadConfigBtn, true, '载入中');
                const response = await this.apiCall('/config/dom', {
                    allowReadOnly: true,
                    useCache: false
                });
                if (!response || response.success === false) {
                    throw new Error(response?.detail || '获取配置失败');
                }
                const thresholds = this.describeThresholds(response?.tuner_base_thresholds);
                const trendThresholds = Array.isArray(response?.trend_thresholds)
                    ? response.trend_thresholds.map((v) => formatNumber(Number(v), 2)).join(', ')
                    : '-';
                const html = [
                    `<div>当前基础阈值: ${thresholds || '未定义'}</div>`,
                    `<div class="text-muted">Trend 阈值: ${trendThresholds}</div>`
                ].join('');
                this.elements.optimizeResult.innerHTML = html;
                if (!this.elements.obiCandidatesInput?.value) {
                    const longObi = toNumber(response?.tuner_base_thresholds?.long?.obi5);
                    if (Number.isFinite(longObi)) {
                        this.elements.obiCandidatesInput.value = String(longObi);
                    }
                }
                if (!this.elements.ofiCandidatesInput?.value) {
                    const longOfi = toNumber(response?.tuner_base_thresholds?.long?.ofi);
                    if (Number.isFinite(longOfi)) {
                        this.elements.ofiCandidatesInput.value = String(longOfi);
                    }
                }
            } catch (error) {
                this.notify('error', `加载配置失败: ${error?.message || error}`);
            } finally {
                this.setButtonLoading(this.elements.loadConfigBtn, false);
            }
        }

        fillSymbolFromMonitor() {
            const monitorSymbol = this.app?.currentMonitor?.symbol
                || this.app?.monitorRealtimeSymbol
                || this.app?.domManager?.symbol
                || '';
            if (!monitorSymbol) {
                this.notify('warning', '当前没有可用的监控标的');
                return;
            }
            if (this.elements.symbolInput) {
                this.elements.symbolInput.value = monitorSymbol;
                this.persistPreference(STORAGE_KEYS.symbol, monitorSymbol);
            }
        }
    }

    const domOptimizer = new DOMOptimizerUI();
    if (typeof window !== 'undefined') {
        window.domOptimizerUI = domOptimizer;
    }
}());
