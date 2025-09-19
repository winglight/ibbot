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
                historyMetrics: [],
                liveEnabled: false,
                lastStats: null,
                loading: {
                    stats: false,
                    signals: false,
                    status: false,
                    optimize: false
                }
            };
            this.charts = {
                trend: null,
                indicator: null,
            };
            this._polling = {
                status: null,
                stats: null,
                history: null,
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
                summaryPrecision: document.getElementById('domOptimizerSummaryPrecision'),
                summaryDelay: document.getElementById('domOptimizerSummaryDelay'),
                summaryLatest: document.getElementById('domOptimizerSummaryLatest'),
                summaryNotes: document.getElementById('domOptimizerSummaryNotes'),
                statusText: document.getElementById('domOptimizerStatusText'),
                lastRun: document.getElementById('domOptimizerLastRun'),
                lastDuration: document.getElementById('domOptimizerLastDuration'),
                nextRun: document.getElementById('domOptimizerNextRun'),
                lastResultPanel: document.getElementById('domOptimizerLastResultPanel'),
                lastResult: document.getElementById('domOptimizerLastResult'),
                trendChart: document.getElementById('domOptimizerTrendChart'),
                indicatorChart: document.getElementById('domOptimizerIndicatorChart'),
                trendRefreshBtn: document.getElementById('domOptimizerTrendRefreshBtn'),
                indicatorRefreshBtn: document.getElementById('domOptimizerIndicatorRefreshBtn'),
                trendLiveToggle: document.getElementById('domOptimizerTrendLiveToggle'),
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
                trendRefreshBtn,
                indicatorRefreshBtn,
                trendLiveToggle,
                symbolInput
            } = this.elements;

            refreshStatusBtn?.addEventListener('click', () => this.refreshAutomationStatus({ force: true }));
            loadConfigBtn?.addEventListener('click', () => this.loadCurrentConfig());
            useMonitorBtn?.addEventListener('click', () => this.fillSymbolFromMonitor());
            refreshStatsBtn?.addEventListener('click', () => this.refreshStatistics());
            querySignalsBtn?.addEventListener('click', () => this.querySignals());
            optimizeBtn?.addEventListener('click', () => this.handleOptimize());
            clearResultBtn?.addEventListener('click', () => this.resetOptimizeResult());
            trendRefreshBtn?.addEventListener('click', () => {
                if (this.state.historyMetrics.length) {
                    this.updateTrendChart(this.state.historyMetrics);
                } else {
                    this.refreshStatistics({ silent: true });
                }
            });
            indicatorRefreshBtn?.addEventListener('click', () => {
                if (this.state.historyMetrics.length) {
                    this.updateIndicatorChart(this.state.historyMetrics);
                } else {
                    this.refreshStatistics({ silent: true });
                }
            });
            trendLiveToggle?.addEventListener('click', () => {
                const current = trendLiveToggle.dataset.active === '1';
                this.setLiveMode(!current);
            });

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
            this.ensureTrendChart();
            this.ensureIndicatorChart();
            this.updateLiveToggleButton();
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
            this.scheduleHistoryPolling();
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
            if (this._polling.history) {
                clearInterval(this._polling.history);
                this._polling.history = null;
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
                let response = null;
                try {
                    response = await this.apiCall('/dom/automation/status', {
                        allowReadOnly: true,
                        useCache: false
                    });
                } catch (error) {
                    const message = String(error?.message || '').toLowerCase();
                    if (message.includes('not found')) {
                        this.renderAutomationStatus(null);
                        return;
                    }
                    throw error;
                }

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
            if (!statusPayload) {
                if (this.elements.statusBadge) {
                    this.elements.statusBadge.className = 'badge bg-secondary text-uppercase';
                    this.elements.statusBadge.textContent = 'N/A';
                }
                if (this.elements.statusText) {
                    this.elements.statusText.textContent = '未启用';
                }
                if (this.elements.lastRun) {
                    this.elements.lastRun.textContent = '-';
                }
                if (this.elements.lastDuration) {
                    this.elements.lastDuration.textContent = '-';
                }
                if (this.elements.nextRun) {
                    this.elements.nextRun.textContent = '-';
                }
                this.renderLastResult(null);
                return;
            }

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
                this.state.lastStats = stats;
                this.updateStatsIndicators(stats);
                this.updateSummary(stats, metrics);
                this.setHistoryMetrics(metrics);
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

        updateSummary(stats, metrics) {
            if (!this.elements.summaryPrecision) return;
            const totals = stats || {};
            this.elements.summaryPrecision.textContent = totals.precision != null
                ? formatPercent(totals.precision)
                : '-';

            const delays = [];
            let latestSignal = null;
            for (const item of Array.isArray(metrics) ? metrics : []) {
                if (!item || !SIGNAL_TYPES.has(item.signal_type)) continue;
                if (!latestSignal) {
                    latestSignal = item;
                } else {
                    const latestTs = resolveTimestampMs(latestSignal) || 0;
                    const candidateTs = resolveTimestampMs(item) || 0;
                    if (candidateTs >= latestTs) {
                        latestSignal = item;
                    }
                }
                const baseTs = Number(item.timestamp ?? item.timestamp_ms);
                const outcomeTs = Number(item.outcome_timestamp ?? item.outcome_ts);
                if (Number.isFinite(baseTs) && Number.isFinite(outcomeTs) && outcomeTs >= baseTs) {
                    delays.push((outcomeTs - baseTs) / 1000);
                }
            }

            const avgDelay = delays.length
                ? delays.reduce((acc, value) => acc + value, 0) / delays.length
                : null;
            if (this.elements.summaryDelay) {
                this.elements.summaryDelay.textContent = avgDelay != null
                    ? `${avgDelay.toFixed(avgDelay >= 10 ? 0 : 1)} s`
                    : '-';
            }

            if (this.elements.summaryLatest) {
                if (latestSignal) {
                    const ts = resolveTimestampMs(latestSignal);
                    const level = this.resolveSignalLevel(latestSignal);
                    const label = latestSignal.signal_type === 'long' ? '多头' : '空头';
                    const outcomeLabel = latestSignal.outcome === 1
                        ? '成功'
                        : latestSignal.outcome === 0
                            ? '失败'
                            : '待评估';
                    const timeLabel = ts ? formatIsoDatetime(ts) : '-';
                    const trendScore = toNumber(latestSignal.trend_score);
                    const trendLabel = Number.isFinite(trendScore) ? trendScore.toFixed(2) : '-';
                    this.elements.summaryLatest.textContent = `${label} L${level || 0} · ${outcomeLabel} · Trend ${trendLabel} @ ${timeLabel}`;
                } else {
                    this.elements.summaryLatest.textContent = '-';
                }
            }

            if (this.elements.summaryNotes) {
                const evaluated = totals?.evaluated ?? 0;
                const pending = totals?.pending ?? 0;
                if (evaluated || pending) {
                    this.elements.summaryNotes.textContent = `已评估 ${evaluated} 条信号，待评估 ${pending} 条。`;
                } else {
                    this.elements.summaryNotes.textContent = '暂无可用信号，请刷新统计或查询历史。';
                }
            }
        }

        setHistoryMetrics(metrics) {
            if (!Array.isArray(metrics) || !metrics.length) {
                this.state.historyMetrics = [];
                this.updateTrendChart([]);
                this.updateIndicatorChart([]);
                return;
            }
            const ordered = metrics
                .filter((item) => item && resolveTimestampMs(item))
                .sort((a, b) => (resolveTimestampMs(a) || 0) - (resolveTimestampMs(b) || 0));
            this.state.historyMetrics = ordered;
            this.ensureTrendChart();
            this.ensureIndicatorChart();
            this.updateTrendChart(ordered);
            this.updateIndicatorChart(ordered);
        }

        ensureTrendChart() {
            if (this.charts.trend) return this.charts.trend;
            const canvas = this.elements.trendChart;
            if (!(canvas && canvas.getContext)) return null;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;

            const chart = new Chart(ctx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: 'Mid Price',
                            data: [],
                            yAxisID: 'priceAxis',
                            borderColor: '#1d4ed8',
                            backgroundColor: 'rgba(59,130,246,0.1)',
                            borderWidth: 2,
                            fill: false,
                            tension: 0.2,
                            spanGaps: true,
                            pointRadius: 0,
                        },
                        {
                            label: 'Trend Score',
                            data: [],
                            yAxisID: 'trendAxis',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.25,
                            spanGaps: true,
                            pointRadius: 0,
                            segment: {
                                borderColor: (ctx) => (ctx.p0.parsed.y >= 0 ? '#16a34a' : '#dc2626'),
                                backgroundColor: (ctx) => (ctx.p0.parsed.y >= 0 ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)'),
                            },
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: {
                            type: 'time',
                            time: { tooltipFormat: 'HH:mm:ss' },
                            grid: { color: 'rgba(148,163,184,0.1)' },
                        },
                        priceAxis: {
                            position: 'left',
                            title: { display: true, text: '价格' },
                            ticks: {
                                callback(value) {
                                    return Number.isFinite(value) ? Number(value).toFixed(2) : value;
                                },
                            },
                            grid: { drawOnChartArea: false },
                        },
                        trendAxis: {
                            position: 'right',
                            title: { display: true, text: 'Trend Score' },
                            suggestedMin: -1,
                            suggestedMax: 1,
                            ticks: {
                                callback: (value) => Number(value).toFixed(1),
                            },
                            grid: { color: 'rgba(148,163,184,0.1)' },
                        },
                    },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label(context) {
                                    const label = context.dataset.label || '';
                                    const value = Number(context.parsed.y);
                                    if (!Number.isFinite(value)) return label;
                                    if (context.dataset.yAxisID === 'priceAxis') {
                                        return `${label}: ${value.toFixed(2)}`;
                                    }
                                    return `${label}: ${value.toFixed(2)}`;
                                },
                            },
                        },
                    },
                },
            });
            chart._datasetMap = { price: 0, trend: 1 };
            this.charts.trend = chart;
            return chart;
        }

        ensureIndicatorChart() {
            if (this.charts.indicator) return this.charts.indicator;
            const canvas = this.elements.indicatorChart;
            if (!(canvas && canvas.getContext)) return null;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;

            const chart = new Chart(ctx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: 'OBI5',
                            data: [],
                            yAxisID: 'obiAxis',
                            borderColor: '#6366f1',
                            borderWidth: 2,
                            tension: 0.25,
                            spanGaps: true,
                            pointRadius: 0,
                            fill: false,
                        },
                        {
                            label: 'OFI',
                            data: [],
                            yAxisID: 'ofiAxis',
                            borderColor: '#f97316',
                            borderWidth: 2,
                            tension: 0.25,
                            spanGaps: true,
                            pointRadius: 0,
                            fill: false,
                        },
                        {
                            label: 'Signals',
                            data: [],
                            type: 'scatter',
                            parsing: false,
                            yAxisID: 'obiAxis',
                            showLine: false,
                            pointRadius: (ctx) => {
                                const level = ctx?.raw?.level || 0;
                                return 4 + Math.min(level * 1.5, 6);
                            },
                            pointBackgroundColor: (ctx) => {
                                const raw = ctx.raw || {};
                                if (raw.outcome === 1) return '#16a34a';
                                if (raw.outcome === 0) return '#dc2626';
                                return '#64748b';
                            },
                            pointBorderColor: (ctx) => {
                                const raw = ctx.raw || {};
                                if (raw.outcome === 1) return '#15803d';
                                if (raw.outcome === 0) return '#b91c1c';
                                return '#475569';
                            },
                            pointStyle: (ctx) => (ctx?.raw?.signalType === 'short' ? 'rectRot' : 'triangle'),
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'nearest', intersect: false },
                    spanGaps: true,
                    scales: {
                        x: {
                            type: 'time',
                            time: { tooltipFormat: 'HH:mm:ss' },
                            grid: { color: 'rgba(148,163,184,0.1)' },
                        },
                        obiAxis: {
                            position: 'left',
                            suggestedMin: 0,
                            suggestedMax: 1,
                            ticks: {
                                callback: (value) => Number(value).toFixed(2),
                            },
                            title: { display: true, text: 'OBI5' },
                        },
                        ofiAxis: {
                            position: 'right',
                            grid: { drawOnChartArea: false },
                            title: { display: true, text: 'OFI' },
                        },
                    },
                    plugins: {
                        legend: { position: 'bottom' },
                        annotation: { annotations: {} },
                        tooltip: {
                            callbacks: {
                                label(context) {
                                    const raw = context.raw || {};
                                    if (context.dataset.type === 'scatter') {
                                        const label = raw.signalType === 'short' ? '空头' : '多头';
                                        const outcome = raw.outcome === 1 ? '成功'
                                            : raw.outcome === 0 ? '失败' : '待评估';
                                        const price = Number.isFinite(raw.price) ? raw.price.toFixed(2) : '-';
                                        const trend = Number.isFinite(raw.trend) ? raw.trend.toFixed(2) : '-';
                                        return `${label} (${outcome}) · Trend ${trend} · Price ${price}`;
                                    }
                                    const value = Number(context.parsed.y);
                                    if (!Number.isFinite(value)) return context.dataset.label || '';
                                    return `${context.dataset.label}: ${value.toFixed(2)}`;
                                },
                            },
                        },
                    },
                },
            });
            chart._datasetMap = { obi: 0, ofi: 1, signals: 2 };
            this.charts.indicator = chart;
            return chart;
        }

        updateTrendChart(metrics) {
            const chart = this.ensureTrendChart();
            if (!chart) return;
            const priceData = [];
            const trendData = [];
            for (const item of metrics) {
                const ts = resolveTimestampMs(item);
                if (!ts) continue;
                const price = toNumber(item?.mid_price ?? item?.price);
                if (Number.isFinite(price)) {
                    priceData.push({ x: ts, y: price });
                }
                const trend = toNumber(item?.trend_score);
                if (Number.isFinite(trend)) {
                    trendData.push({ x: ts, y: trend });
                }
            }
            const priceDataset = chart.data.datasets[chart._datasetMap.price];
            const trendDataset = chart.data.datasets[chart._datasetMap.trend];
            priceDataset.data = priceData;
            trendDataset.data = trendData;
            chart.update('none');
        }

        updateIndicatorChart(metrics) {
            const chart = this.ensureIndicatorChart();
            if (!chart) return;
            const obiData = [];
            const ofiData = [];
            const signalData = [];
            let thresholds = null;

            for (const item of metrics) {
                const ts = resolveTimestampMs(item);
                if (!ts) continue;
                const obi = toNumber(item?.obi5 ?? item?.obi);
                if (Number.isFinite(obi)) {
                    obiData.push({ x: ts, y: obi });
                }
                const ofi = toNumber(item?.ofi);
                if (Number.isFinite(ofi)) {
                    ofiData.push({ x: ts, y: ofi });
                }
                if (SIGNAL_TYPES.has(item?.signal_type)) {
                    const level = this.resolveSignalLevel(item);
                    let yValue = Number.isFinite(obi) ? obi : null;
                    if (!Number.isFinite(yValue)) {
                        yValue = item.signal_type === 'long' ? 0.85 : 0.15;
                    }
                    signalData.push({
                        x: ts,
                        y: yValue,
                        outcome: item?.outcome,
                        level,
                        signalType: item?.signal_type,
                        price: resolvePrice(item),
                        trend: toNumber(item?.trend_score),
                    });
                }
                if (item?.thresholds) {
                    thresholds = item.thresholds;
                }
            }

            const obiDataset = chart.data.datasets[chart._datasetMap.obi];
            const ofiDataset = chart.data.datasets[chart._datasetMap.ofi];
            const signalDataset = chart.data.datasets[chart._datasetMap.signals];
            obiDataset.data = obiData;
            ofiDataset.data = ofiData;
            signalDataset.data = signalData;
            this.updateIndicatorAnnotations(chart, thresholds);
            chart.update('none');
        }

        updateIndicatorAnnotations(chart, thresholds) {
            if (!chart) return;
            const annotations = {};
            const longObi = toNumber(thresholds?.long?.obi);
            const shortObi = toNumber(thresholds?.short?.obi);
            const longOfi = toNumber(thresholds?.long?.ofi);
            const shortOfi = toNumber(thresholds?.short?.ofi);
            if (Number.isFinite(longObi)) {
                annotations.longObi = {
                    type: 'line',
                    yMin: longObi,
                    yMax: longObi,
                    borderColor: 'rgba(34,197,94,0.45)',
                    borderDash: [6, 6],
                    borderWidth: 1.5,
                    yScaleID: 'obiAxis',
                    label: {
                        enabled: true,
                        position: 'start',
                        content: `Long OBI ${longObi.toFixed(2)}`,
                    },
                };
            }
            if (Number.isFinite(shortObi)) {
                annotations.shortObi = {
                    type: 'line',
                    yMin: shortObi,
                    yMax: shortObi,
                    borderColor: 'rgba(239,68,68,0.45)',
                    borderDash: [6, 6],
                    borderWidth: 1.5,
                    yScaleID: 'obiAxis',
                    label: {
                        enabled: true,
                        position: 'end',
                        content: `Short OBI ${shortObi.toFixed(2)}`,
                    },
                };
            }
            if (Number.isFinite(longOfi)) {
                annotations.longOfi = {
                    type: 'line',
                    yMin: longOfi,
                    yMax: longOfi,
                    borderColor: 'rgba(34,197,94,0.35)',
                    borderDash: [4, 4],
                    borderWidth: 1,
                    yScaleID: 'ofiAxis',
                    label: {
                        enabled: true,
                        position: 'start',
                        content: `Long OFI ${longOfi.toFixed(1)}`,
                    },
                };
            }
            if (Number.isFinite(shortOfi)) {
                annotations.shortOfi = {
                    type: 'line',
                    yMin: shortOfi,
                    yMax: shortOfi,
                    borderColor: 'rgba(239,68,68,0.35)',
                    borderDash: [4, 4],
                    borderWidth: 1,
                    yScaleID: 'ofiAxis',
                    label: {
                        enabled: true,
                        position: 'end',
                        content: `Short OFI ${shortOfi.toFixed(1)}`,
                    },
                };
            }
            if (!chart.options.plugins) chart.options.plugins = {};
            if (!chart.options.plugins.annotation) chart.options.plugins.annotation = {};
            chart.options.plugins.annotation.annotations = annotations;
        }

        resolveSignalLevel(item) {
            const score = toNumber(item?.trend_score);
            if (!Number.isFinite(score)) return 0;
            const abs = Math.abs(score);
            if (abs >= 0.95) return 4;
            if (abs >= 0.85) return 3;
            if (abs >= 0.7) return 2;
            if (abs >= 0.5) return 1;
            return 0;
        }

        scheduleHistoryPolling() {
            if (!this.state.liveEnabled) {
                if (this._polling.history) {
                    clearInterval(this._polling.history);
                    this._polling.history = null;
                }
                return;
            }
            if (this._polling.history) return;
            this._polling.history = window.setInterval(() => {
                const symbol = this.elements.symbolInput?.value?.trim();
                if (symbol) {
                    this.refreshStatistics({ silent: true }).catch(() => {});
                }
            }, 30_000);
        }

        setLiveMode(enabled) {
            const target = Boolean(enabled);
            if (this.state.liveEnabled === target) {
                this.updateLiveToggleButton();
                return;
            }
            this.state.liveEnabled = target;
            this.updateLiveToggleButton();
            this.scheduleHistoryPolling();
            if (target) {
                const symbol = this.elements.symbolInput?.value?.trim();
                if (symbol) {
                    this.refreshStatistics({ silent: true }).catch(() => {});
                }
                this.notify('info', '已开启 DOM Optimizer 实时刷新（30 秒频率）。');
            } else {
                this.notify('info', '已关闭 DOM Optimizer 实时刷新。');
            }
        }

        updateLiveToggleButton() {
            const button = this.elements.trendLiveToggle;
            if (!button) return;
            if (this.state.liveEnabled) {
                button.dataset.active = '1';
                button.classList.remove('btn-outline-secondary');
                button.classList.add('btn-outline-success');
                button.setAttribute('title', '实时刷新已开启');
            } else {
                button.dataset.active = '0';
                button.classList.add('btn-outline-secondary');
                button.classList.remove('btn-outline-success');
                button.setAttribute('title', '实时刷新已关闭');
            }
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
                const stats = computeStats(metrics);
                this.state.lastStats = stats;
                this.updateStatsIndicators(stats);
                this.updateSummary(stats, metrics);
                this.setHistoryMetrics(metrics);
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
