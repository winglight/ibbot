(function () {
    const DEFAULT_RESULT_HTML = '<div class="text-muted small">点击“立即优化”后将在此展示最新结果、精度统计以及阈值建议。</div>';
    const STORAGE_KEYS = {
        symbol: 'domOptimizer.symbol',
        lookback: 'domOptimizer.lookback',
        limit: 'domOptimizer.limit',
        pageSize: 'domOptimizer.pageSize',
        baseThresholds: 'domOptimizer.baseThresholds',
        featureThresholds: 'domOptimizer.featureThresholds'
    };

    const LOOKBACK_MINUTES_PRESETS = [10, 30, 60, 120, 180, 240, 360, 720, 1440, 2880, 10080, -1];
    const DOM_STATS_MAX_POINTS = 3000;

    const SIGNAL_TYPES = new Set(['long', 'short']);

    const STATUS_BADGE_MAP = {
        running: 'bg-info',
        succeeded: 'bg-success',
        failed: 'bg-danger',
        scheduled: 'bg-primary',
        idle: 'bg-secondary'
    };

    const EVENT_SIDE_COLORS = {
        buy: '#22c55e',
        sell: '#ef4444',
        neutral: '#f97316',
    };

    const DEFAULT_BASE_THRESHOLD_TEMPLATE = {
        baseThresholds: {
            long: { obi5: 0.6, ofi: 0.0 },
            short: { obi5: 0.4, ofi: 0.0 },
            absorb: { obi_buy: 0.4, obi_sell: 0.6, ofi_sign: 0.0 },
            sweep: { ofi_multiplier: 2.0, min_abs_ofi: 0.0 },
        },
        obiCandidates: [0.55, 0.6, 0.65, 0.7],
        ofiCandidates: [0, 5, 10, 15],
        trendCandidates: [0.5, 0.7, 0.85, 0.95],
    };

    const DEFAULT_FEATURE_THRESHOLD_TEMPLATE = {
        stacking: { intensity: 0.35, min_depth: 50 },
        pulling: { intensity: 0.3, min_depth: 30 },
        wall: { strength: 0.35, min_size: 80 },
        cancel_rate: { rate: 1.2 },
        absorption: { index: 0.6 },
        sweep: { intensity: 0.7 },
        fake_breakout: { prob: 0.65 },
        wall_pull_no_follow: { score: 0.6 },
        obi_ofi_reversal: { score: 0.75 },
        spread: { delta_ticks: 2 },
    };

    const domOptimizerEventPlugin = {
        id: 'domOptimizerEventMarkers',
        afterDatasetsDraw(chart) {
            const { ctx } = chart;
            if (!ctx) return;
            chart.data.datasets.forEach((dataset, datasetIndex) => {
                if (!dataset || !dataset.domEvents) return;
                const meta = chart.getDatasetMeta(datasetIndex);
                if (!meta || !meta.data) return;
                meta.data.forEach((element, index) => {
                    const raw = dataset.data?.[index];
                    if (!element || !raw) return;
                    const { x, y } = element.getProps(['x', 'y'], true);
                    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
                    const code = typeof raw.code === 'string' ? raw.code : '?';
                    const color = raw.color || EVENT_SIDE_COLORS.neutral;
                    ctx.save();
                    ctx.fillStyle = color;
                    ctx.font = 'bold 12px "Inter", "Helvetica", sans-serif';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText(code, x, y);
                    ctx.restore();
                });
            });
        },
    };

    function cloneBaseThresholds(value) {
        if (!value || typeof value !== 'object') return null;
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (error) {
            console.warn('Failed to clone base thresholds', error);
            return null;
        }
    }

    function cloneFeatureThresholds(value) {
        if (!value || typeof value !== 'object') return null;
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (error) {
            console.warn('Failed to clone feature thresholds', error);
            return null;
        }
    }

    function normalizeCandidateArray(value) {
        if (value === undefined || value === null) return null;
        let candidates = [];
        if (Array.isArray(value)) {
            candidates = value;
        } else if (typeof value === 'number') {
            candidates = [value];
        } else if (typeof value === 'string') {
            candidates = value.split(/[\s,]+/).map((token) => Number(token.trim()));
        } else {
            return null;
        }
        const numeric = candidates
            .map((item) => Number(item))
            .filter((item) => Number.isFinite(item));
        if (!numeric.length) return null;
        const unique = [];
        numeric.forEach((item) => {
            if (!unique.some((existing) => Math.abs(existing - item) < 1e-9)) {
                unique.push(item);
            }
        });
        unique.sort((a, b) => a - b);
        return unique;
    }

    function parseBaseThresholdsInput(rawValue) {
        if (!rawValue) return null;
        let parsed = rawValue;
        if (typeof rawValue === 'string') {
            const trimmed = rawValue.trim();
            if (!trimmed) return null;
            try {
                parsed = JSON.parse(trimmed);
            } catch (error) {
                throw new Error(`基础阈值 JSON 解析失败: ${error.message}`);
            }
        } else if (typeof rawValue === 'object' && !Array.isArray(rawValue)) {
            parsed = cloneBaseThresholds(rawValue) || rawValue;
        } else {
            throw new Error('基础阈值 JSON 必须是对象');
        }

        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            throw new Error('基础阈值 JSON 必须是对象');
        }

        const config = {
            baseThresholds: null,
            obiCandidates: null,
            ofiCandidates: null,
            trendCandidates: null,
        };

        const baseSource = (parsed.base_thresholds && typeof parsed.base_thresholds === 'object' && !Array.isArray(parsed.base_thresholds))
            ? parsed.base_thresholds
            : (parsed.baseThresholds && typeof parsed.baseThresholds === 'object' && !Array.isArray(parsed.baseThresholds))
                ? parsed.baseThresholds
                : null;
        const source = baseSource || parsed;
        if (source && typeof source === 'object') {
            const entries = {};
            Object.keys(source).forEach((key) => {
                if (key === 'base_thresholds' || key === 'baseThresholds') return;
                const value = source[key];
                if (value && typeof value === 'object' && !Array.isArray(value)) {
                    entries[key] = cloneBaseThresholds(value) || value;
                }
            });
            if (Object.keys(entries).length) {
                config.baseThresholds = entries;
            } else if (baseSource) {
                config.baseThresholds = cloneBaseThresholds(baseSource) || baseSource;
            }
        }

        config.obiCandidates = normalizeCandidateArray(parsed.obi_candidates ?? parsed.obiCandidates);
        config.ofiCandidates = normalizeCandidateArray(parsed.ofi_candidates ?? parsed.ofiCandidates);
        config.trendCandidates = normalizeCandidateArray(
            parsed.trend_candidates
            ?? parsed.trendCandidates
            ?? parsed.trend_thresholds
            ?? parsed.trendThresholds,
        );

        if (
            !config.baseThresholds
            && !config.obiCandidates
            && !config.ofiCandidates
            && !config.trendCandidates
        ) {
            throw new Error('基础阈值 JSON 需包含 base_thresholds 或候选数组');
        }

        return config;
    }

    function stringifyBaseThresholds(config) {
        if (!config || typeof config !== 'object') return '';
        const payload = {};
        if (Object.prototype.hasOwnProperty.call(config, 'baseThresholds')) {
            const base = config.baseThresholds && typeof config.baseThresholds === 'object'
                ? cloneBaseThresholds(config.baseThresholds) || config.baseThresholds
                : null;
            if (base && Object.keys(base).length) {
                payload.base_thresholds = base;
            }
        }
        if (Object.prototype.hasOwnProperty.call(config, 'obiCandidates')) {
            const obi = normalizeCandidateArray(config.obiCandidates);
            if (obi && obi.length) {
                payload.obi_candidates = obi;
            }
        }
        if (Object.prototype.hasOwnProperty.call(config, 'ofiCandidates')) {
            const ofi = normalizeCandidateArray(config.ofiCandidates);
            if (ofi && ofi.length) {
                payload.ofi_candidates = ofi;
            }
        }
        if (Object.prototype.hasOwnProperty.call(config, 'trendCandidates')) {
            const trend = normalizeCandidateArray(config.trendCandidates);
            if (trend && trend.length) {
                payload.trend_candidates = trend;
            }
        }
        if (!Object.keys(payload).length) return '';
        try {
            return JSON.stringify(payload, null, 2);
        } catch (error) {
            console.warn('Failed to stringify base thresholds', error);
            return '';
        }
    }

    function parseFeatureThresholdsInput(rawValue) {
        if (!rawValue) return null;
        if (typeof rawValue === 'object') {
            return cloneFeatureThresholds(rawValue);
        }
        if (typeof rawValue !== 'string') return null;
        const trimmed = rawValue.trim();
        if (!trimmed) return null;
        try {
            const parsed = JSON.parse(trimmed);
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                throw new Error('特征阈值必须是对象');
            }
            return parsed;
        } catch (error) {
            throw new Error(`特征阈值 JSON 解析失败: ${error.message}`);
        }
    }

    function stringifyFeatureThresholds(value) {
        if (!value || typeof value !== 'object') return '';
        try {
            return JSON.stringify(value, null, 2);
        } catch (error) {
            console.warn('Failed to stringify feature thresholds', error);
            return '';
        }
    }

    function resolveEventColor(event) {
        if (!event || typeof event !== 'object') return EVENT_SIDE_COLORS.neutral;
        if (typeof event.color === 'string') return event.color;
        const side = typeof event.side === 'string' ? event.side.toLowerCase() : '';
        if (side === 'buy') return EVENT_SIDE_COLORS.buy;
        if (side === 'sell') return EVENT_SIDE_COLORS.sell;
        if (typeof event.key === 'string') {
            if (event.key.includes('buy')) return EVENT_SIDE_COLORS.buy;
            if (event.key.includes('sell')) return EVENT_SIDE_COLORS.sell;
        }
        return EVENT_SIDE_COLORS.neutral;
    }

    function normaliseEventCode(event) {
        if (!event || typeof event !== 'object') return '?';
        if (typeof event.code === 'string' && event.code.trim()) {
            return event.code.trim().slice(0, 2);
        }
        if (typeof event.key === 'string' && event.key.trim()) {
            return event.key.trim().charAt(0).toUpperCase();
        }
        return '?';
    }

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

    function formatLookbackPreset(minutes) {
        if (minutes === -1 || minutes === null || minutes === undefined) {
            return '全量历史';
        }
        const value = Number(minutes);
        if (!Number.isFinite(value) || value <= 0) {
            return '全量历史';
        }
        if (value < 60) {
            return `${Math.round(value)} 分钟`;
        }
        if (value % 1440 === 0) {
            const days = value / 1440;
            return `${days >= 10 ? days.toFixed(0) : days.toFixed(days >= 3 ? 1 : 2)} 天`;
        }
        if (value % 60 === 0) {
            const hours = value / 60;
            return `${hours >= 10 ? hours.toFixed(0) : hours.toFixed(1)} 小时`;
        }
        const hours = value / 60;
        return `${hours.toFixed(hours >= 10 ? 0 : 1)} 小时`;
    }

    function sliderIndexToMinutes(index) {
        const i = Number.parseInt(index, 10);
        if (!Number.isFinite(i) || i < 0 || i >= LOOKBACK_MINUTES_PRESETS.length) {
            return LOOKBACK_MINUTES_PRESETS[LOOKBACK_MINUTES_PRESETS.length - 1];
        }
        return LOOKBACK_MINUTES_PRESETS[i];
    }

    function minutesToSliderIndex(minutes) {
        if (minutes === null || minutes === undefined) {
            return LOOKBACK_MINUTES_PRESETS.length - 1;
        }
        const value = Number(minutes);
        if (!Number.isFinite(value) || value <= 0) {
            return LOOKBACK_MINUTES_PRESETS.length - 1;
        }
        let bestIndex = 0;
        let smallestDiff = Number.POSITIVE_INFINITY;
        LOOKBACK_MINUTES_PRESETS.forEach((preset, idx) => {
            if (preset === -1) return;
            const diff = Math.abs(preset - value);
            if (diff < smallestDiff) {
                smallestDiff = diff;
                bestIndex = idx;
            }
        });
        return bestIndex;
    }

    function isFullLookback(minutes) {
        if (minutes === -1) return true;
        if (minutes === null || minutes === undefined) return true;
        const value = Number(minutes);
        return !Number.isFinite(value) || value <= 0;
    }

    function coerceMinutesFromStorage(value) {
        if (!value) return null;
        if (value === 'full') return -1;
        const parsed = Number.parseInt(value, 10);
        if (Number.isFinite(parsed) && parsed > 0) {
            return parsed;
        }
        return null;
    }

    function formatSecondsBrief(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) return '-';
        if (seconds < 60) {
            return `${seconds >= 10 ? seconds.toFixed(0) : seconds.toFixed(1)} 秒`;
        }
        if (seconds < 3600) {
            if (seconds % 60 === 0) {
                return `${(seconds / 60).toFixed(0)} 分钟`;
            }
            return `${(seconds / 60).toFixed(1)} 分钟`;
        }
        if (seconds % 3600 === 0) {
            return `${(seconds / 3600).toFixed(0)} 小时`;
        }
        return `${(seconds / 3600).toFixed(1)} 小时`;
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
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
                statsWindow: null,
                statsMetadata: null,
                latestSignal: null,
                trendThresholds: [0.5, 0.7, 0.85, 0.95],
                baseThresholds: null,
                baseThresholdCandidates: {
                    obi: null,
                    ofi: null,
                    trend: null,
                },
                featureThresholds: null,
                orderCache: {
                    symbol: null,
                    fetchedAt: 0,
                    items: []
                },
                activeStreamSymbol: null,
                streamEnsuredAt: 0,
                priceHistoryCache: {},
                activePriceHistory: [],
                lowTrendFilteredCount: 0,
                activeSignal: null,
                lastQueryRange: null,
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
                threshold: null,
            };
            this._polling = {
                status: null,
                stats: null,
                history: null,
            };
            this._readOnlyWrapped = false;
            this._wsUnsubscribeFns = [];
            this._pendingStatusInitial = false;
            this._pendingStatsRequest = null;
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
                lookbackSlider: document.getElementById('domOptimizerLookbackSlider'),
                lookbackDisplay: document.getElementById('domOptimizerLookbackDisplay'),
                statsLimitInput: document.getElementById('domOptimizerStatsLimitInput'),
                minSignalsInput: document.getElementById('domOptimizerMinSignalsInput'),
                baseThresholdsInput: document.getElementById('domOptimizerBaseThresholdsInput'),
                featureThresholdsInput: document.getElementById('domOptimizerFeatureThresholdsInput'),
                featurePresetBtn: document.getElementById('domOptimizerFeaturePresetBtn'),
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
                thresholdChart: document.getElementById('domOptimizerThresholdChart'),
                trendRefreshBtn: document.getElementById('domOptimizerTrendRefreshBtn'),
                indicatorRefreshBtn: document.getElementById('domOptimizerIndicatorRefreshBtn'),
                thresholdRefreshBtn: document.getElementById('domOptimizerThresholdRefreshBtn'),
                trendLiveToggle: document.getElementById('domOptimizerTrendLiveToggle'),
                startInput: document.getElementById('domOptimizerStartInput'),
                endInput: document.getElementById('domOptimizerEndInput'),
                pageSizeInput: document.getElementById('domOptimizerPageSizeInput'),
                outcomeFilter: document.getElementById('domOptimizerOutcomeFilter'),
                querySignalsBtn: document.getElementById('domOptimizerQuerySignalsBtn'),
                signalsTable: document.getElementById('domOptimizerSignalsTable'),
                signalsSummary: document.getElementById('domOptimizerSignalsSummary'),
                signalsRange: document.getElementById('domOptimizerSignalsRange'),
                pagination: document.getElementById('domOptimizerPagination'),
                currentPage: document.getElementById('domOptimizerCurrentPage'),
                totalPages: document.getElementById('domOptimizerTotalPages'),
                optimizeBtn: document.getElementById('domOptimizerOptimizeBtn'),
                clearResultBtn: document.getElementById('domOptimizerClearResultBtn'),
                applySwitch: document.getElementById('domOptimizerApplySwitch'),
                optimizeResult: document.getElementById('domOptimizerOptimizeResult'),
                outcomeModal: document.getElementById('domOutcomeModal'),
                outcomeModalTitle: document.getElementById('domOutcomeModalTitle'),
                outcomeModalAlert: document.getElementById('domOutcomeModalAlert'),
                outcomeSignalMetrics: document.getElementById('domOutcomeSignalMetrics'),
                outcomeSignalLevel: document.getElementById('domOutcomeSignalLevel'),
                outcomeModalMeta: document.getElementById('domOutcomeModalMeta'),
                outcomeOrdersTbody: document.getElementById('domOutcomeOrdersTbody'),
                outcomeOrdersLoading: document.getElementById('domOutcomeOrdersLoading'),
                outcomePriceTbody: document.getElementById('domOutcomePriceTbody'),
                outcomePriceMeta: document.getElementById('domOutcomePriceMeta')
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
                lookbackSlider,
                statsLimitInput,
                pageSizeInput,
                trendRefreshBtn,
                indicatorRefreshBtn,
                thresholdRefreshBtn,
                trendLiveToggle,
                symbolInput,
                signalsTable,
                featurePresetBtn,
                baseThresholdsInput,
                featureThresholdsInput,
            } = this.elements;

            refreshStatusBtn?.addEventListener('click', () => this.refreshAutomationStatus({ force: true }));
            loadConfigBtn?.addEventListener('click', () => this.loadCurrentConfig());
            useMonitorBtn?.addEventListener('click', () => this.fillSymbolFromMonitor());
            refreshStatsBtn?.addEventListener('click', () => this.refreshStatistics({ forceEnsure: true }));
            querySignalsBtn?.addEventListener('click', () => this.querySignals());
            optimizeBtn?.addEventListener('click', () => this.handleOptimize());
            clearResultBtn?.addEventListener('click', () => this.resetOptimizeResult());
            trendRefreshBtn?.addEventListener('click', () => {
                if (this.state.historyMetrics.length) {
                    this.updateTrendChart(this.state.historyMetrics);
                } else {
                    this.refreshStatistics({ silent: true, forceEnsure: true });
                }
            });
            thresholdRefreshBtn?.addEventListener('click', () => {
                if (this.state.historyMetrics.length) {
                    this.updateThresholdChart(this.state.historyMetrics);
                } else {
                    this.refreshStatistics({ silent: true, forceEnsure: true });
                }
            });
            indicatorRefreshBtn?.addEventListener('click', () => {
                if (this.state.historyMetrics.length) {
                    this.updateIndicatorChart(this.state.historyMetrics);
                    this.updateThresholdChart(this.state.historyMetrics);
                } else {
                    this.refreshStatistics({ silent: true, forceEnsure: true });
                }
            });
            trendLiveToggle?.addEventListener('click', () => {
                const current = trendLiveToggle.dataset.active === '1';
                this.setLiveMode(!current);
            });

            featurePresetBtn?.addEventListener('click', () => {
                this.fillFeatureThresholdPreset().catch(() => {});
            });
            baseThresholdsInput?.addEventListener('input', () => {
                this.handleBaseThresholdInputChange();
            });
            baseThresholdsInput?.addEventListener('blur', () => {
                this.handleBaseThresholdBlur();
            });
            featureThresholdsInput?.addEventListener('input', () => {
                this.handleFeatureThresholdInputChange();
            });
            featureThresholdsInput?.addEventListener('blur', () => {
                this.handleFeatureThresholdBlur();
            });

            pagination?.addEventListener('click', (event) => {
                const button = event.target instanceof Element ? event.target.closest('button[data-page]') : null;
                if (!button || button.disabled) return;
                const direction = button.dataset.page;
                this.turnPage(direction);
            });

            symbolInput?.addEventListener('change', () => {
                const value = symbolInput.value.trim();
                this.persistPreference(STORAGE_KEYS.symbol, value);
                if (!value) {
                    this.stopDomStream().catch(() => {});
                } else if (value.toUpperCase() !== this.state.activeStreamSymbol) {
                    this.state.streamEnsuredAt = 0;
                }
            });
            lookbackInput?.addEventListener('change', () => this.handleLookbackInputChange());
            if (lookbackSlider) {
                lookbackSlider.addEventListener('input', () => {
                    const minutes = sliderIndexToMinutes(lookbackSlider.value);
                    this.syncLookbackControls({ minutes, persist: false });
                });
                lookbackSlider.addEventListener('change', () => this.handleLookbackSliderCommit());
            }
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

            signalsTable?.addEventListener('click', (event) => {
                const target = event.target instanceof Element ? event.target : null;
                if (!target) return;
                const badge = target.closest('[data-role="dom-outcome-badge"]');
                if (!badge) return;
                const index = parseInt(badge.getAttribute('data-index') || '', 10);
                if (!Number.isFinite(index)) return;
                const signal = this.state.filteredSignals[index];
                if (!signal) return;
                event.preventDefault();
                this.openOutcomeDetail(signal);
            });

            if (typeof window !== 'undefined' && typeof navigator !== 'undefined' && navigator.sendBeacon) {
                window.addEventListener('beforeunload', () => {
                    if (!this.state.activeStreamSymbol) return;
                    try {
                        const payload = JSON.stringify({ symbol: this.state.activeStreamSymbol });
                        navigator.sendBeacon('/api/dom/stop', new Blob([payload], { type: 'application/json' }));
                    } catch (error) {
                        // ignore transport errors during unload
                    }
                });
            }
        }

        restorePreferences() {
            if (!this.elements.section) return;
            const { symbolInput, lookbackInput, statsLimitInput, pageSizeInput, baseThresholdsInput, featureThresholdsInput } = this.elements;
            const storedSymbol = localStorage.getItem(STORAGE_KEYS.symbol);
            const storedLookbackRaw = localStorage.getItem(STORAGE_KEYS.lookback);
            const storedLookbackMinutes = coerceMinutesFromStorage(storedLookbackRaw);
            const storedLimit = localStorage.getItem(STORAGE_KEYS.limit);
            const storedPageSize = localStorage.getItem(STORAGE_KEYS.pageSize);
            const storedBaseThresholds = localStorage.getItem(STORAGE_KEYS.baseThresholds);
            const storedFeatureThresholds = localStorage.getItem(STORAGE_KEYS.featureThresholds);

            if (storedSymbol && symbolInput) symbolInput.value = storedSymbol;
            if (storedLimit && statsLimitInput) statsLimitInput.value = storedLimit;
            if (storedPageSize && pageSizeInput) pageSizeInput.value = storedPageSize;

            if (pageSizeInput) {
                const parsed = parseInt(pageSizeInput.value, 10);
                if (Number.isFinite(parsed) && parsed > 0) {
                    this.state.pageSize = parsed;
                }
            }

            if (storedBaseThresholds && baseThresholdsInput) {
                baseThresholdsInput.value = storedBaseThresholds;
                const parsedBase = this.safeParseBaseThresholds(storedBaseThresholds);
                if (parsedBase) {
                    this.applyBaseThresholdConfig(parsedBase);
                }
            }

            if (storedFeatureThresholds && featureThresholdsInput) {
                featureThresholdsInput.value = storedFeatureThresholds;
                const parsedFeature = this.safeParseFeatureThresholds(storedFeatureThresholds);
                if (parsedFeature) {
                    this.state.featureThresholds = cloneFeatureThresholds(parsedFeature) || parsedFeature;
                }
            }

            const fallbackMinutes = Number.parseInt(lookbackInput?.value || '', 10) || 240;
            const preferredMinutes = storedLookbackMinutes !== null && storedLookbackMinutes !== undefined
                ? storedLookbackMinutes
                : fallbackMinutes;
            this.syncLookbackControls({ minutes: preferredMinutes, persist: false });
        }

        handleLookbackInputChange() {
            const { lookbackInput } = this.elements;
            if (!lookbackInput) return;
            const parsed = Number.parseInt(lookbackInput.value || '', 10);
            const minutes = Number.isFinite(parsed) && parsed > 0 ? parsed : null;
            this.syncLookbackControls({ minutes, persist: true });
        }

        handleLookbackSliderCommit() {
            const { lookbackSlider } = this.elements;
            if (!lookbackSlider) return;
            const minutes = sliderIndexToMinutes(lookbackSlider.value);
            this.syncLookbackControls({ minutes, persist: true });
        }

        syncLookbackControls({ minutes, persist = false } = {}) {
            const { lookbackInput, lookbackSlider, lookbackDisplay } = this.elements;
            let resolved = minutes;
            if (resolved === undefined) {
                const parsed = Number.parseInt(lookbackInput?.value || '', 10);
                resolved = Number.isFinite(parsed) && parsed > 0 ? parsed : null;
            }

            if (lookbackInput) {
                if (isFullLookback(resolved)) {
                    lookbackInput.value = '';
                } else {
                    const rounded = Math.max(1, Math.round(Number(resolved)));
                    lookbackInput.value = String(rounded);
                    resolved = rounded;
                }
            }

            if (lookbackSlider) {
                const index = minutesToSliderIndex(isFullLookback(resolved) ? null : resolved);
                lookbackSlider.value = String(index);
            }

            if (lookbackDisplay) {
                lookbackDisplay.textContent = formatLookbackPreset(isFullLookback(resolved) ? -1 : resolved);
            }

            if (persist) {
                if (isFullLookback(resolved)) {
                    this.persistPreference(STORAGE_KEYS.lookback, 'full');
                } else {
                    this.persistPreference(STORAGE_KEYS.lookback, Math.max(1, Math.round(Number(resolved))));
                }
            }
        }

        resolveLookbackMinutes({ allowFull = true, defaultMinutes = 240 } = {}) {
            const { lookbackSlider, lookbackInput } = this.elements;
            let minutes = null;
            if (lookbackSlider) {
                minutes = sliderIndexToMinutes(lookbackSlider.value);
            }
            if (minutes === null || minutes === undefined) {
                const parsed = Number.parseInt(lookbackInput?.value || '', 10);
                if (Number.isFinite(parsed) && parsed > 0) {
                    minutes = parsed;
                }
            }
            if (!allowFull && isFullLookback(minutes)) {
                return defaultMinutes;
            }
            if (isFullLookback(minutes)) {
                return null;
            }
            return Math.max(1, Math.round(Number(minutes)));
        }

        resolveLookbackSeconds() {
            const minutes = this.resolveLookbackMinutes({ allowFull: true });
            if (minutes === null) return null;
            return Math.max(1, Math.round(Number(minutes))) * 60;
        }

        resolveLatestSignal(metrics) {
            if (!Array.isArray(metrics) || !metrics.length) return null;
            for (let i = metrics.length - 1; i >= 0; i -= 1) {
                const item = metrics[i];
                if (item && SIGNAL_TYPES.has(item.signal_type)) {
                    return item;
                }
            }
            return null;
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
            if (typeof this.app.registerWebSocketHandler === 'function') {
                this._wsUnsubscribeFns.forEach(unsub => { try { if (typeof unsub === 'function') unsub(); } catch (e) { console.warn('Failed to unsubscribe DOM optimizer handler', e); } });
                this._wsUnsubscribeFns = [];
                this._wsUnsubscribeFns.push(
                    this.app.registerWebSocketHandler('dom_automation_status', (payload) => this.onAutomationStatusMessage(payload)),
                );
                this._wsUnsubscribeFns.push(
                    this.app.registerWebSocketHandler('dom_stats', (payload) => this.onDomStatsMessage(payload)),
                );
            }
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
                this.refreshStatistics({ forceEnsure: true }).catch(() => {});
            }
            this.loadCurrentConfig({ auto: true }).catch(() => {});
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

        setTrendThresholds(values) {
            const parsed = Array.isArray(values)
                ? values
                    .map((value) => {
                        const num = Number(value);
                        return Number.isFinite(num) && num > 0 ? Math.abs(num) : null;
                    })
                    .filter((value) => Number.isFinite(value))
                    .sort((a, b) => a - b)
                : [];
            this.state.trendThresholds = parsed.length ? parsed.slice(0, 4) : [0.5, 0.7, 0.85, 0.95];
        }

        resolveMinTrendThreshold() {
            const thresholds = Array.isArray(this.state.trendThresholds)
                ? this.state.trendThresholds
                    .map((value) => Number(value))
                    .filter((value) => Number.isFinite(value) && value > 0)
                : [];
            if (!thresholds.length) {
                return 0.5;
            }
            return Math.min(...thresholds);
        }

        extractTrendThresholds(metrics) {
            if (!Array.isArray(metrics)) return null;
            for (const item of metrics) {
                if (!item) continue;
                if (Array.isArray(item.trend_thresholds)) {
                    const parsed = item.trend_thresholds
                        .map((value) => Number(value))
                        .filter((value) => Number.isFinite(value) && value > 0);
                    if (parsed.length) return parsed;
                }
                const nested = item.thresholds;
                if (nested && typeof nested === 'object') {
                    const direct = nested.trend_thresholds || nested.trendThresholds || nested.trend;
                    if (Array.isArray(direct)) {
                        const parsed = direct
                            .map((value) => Number(value))
                            .filter((value) => Number.isFinite(value) && value > 0);
                        if (parsed.length) return parsed;
                    }
                }
            }
            return null;
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

        async ensureDomStream(symbol, options = {}) {
            const { force = false } = options || {};
            const targetSymbol = (symbol || '').toString().trim().toUpperCase();
            if (!targetSymbol) return;
            if (this.app?.isReadOnly) return;
            const now = Date.now();
            const ensuredAt = this.state.streamEnsuredAt || 0;
            if (
                !force
                && this.state.activeStreamSymbol === targetSymbol
                && ensuredAt > 0
            ) {
                return;
            }
            if (
                this.state.activeStreamSymbol
                && this.state.activeStreamSymbol !== targetSymbol
            ) {
                await this.stopDomStream(this.state.activeStreamSymbol).catch(() => {});
            }
            try {
                await this.apiCall('/dom/start', {
                    method: 'POST',
                    body: JSON.stringify({
                        symbol: targetSymbol,
                        num_rows: 10
                    }),
                    useCache: false
                });
                this.state.activeStreamSymbol = targetSymbol;
                this.state.streamEnsuredAt = now;
            } catch (error) {
                this.notify('warning', `无法启动 ${targetSymbol} DOM 订阅: ${error?.message || error}`);
            }
        }

        async stopDomStream(symbol = null) {
            const currentSymbol = symbol
                ? symbol
                : this.state.activeStreamSymbol;
            const targetSymbol = (currentSymbol || '').toString().trim().toUpperCase();
            if (!targetSymbol) return;
            if (this.app?.isReadOnly) {
                this.state.activeStreamSymbol = null;
                this.state.streamEnsuredAt = 0;
                return;
            }
            try {
                await this.apiCall('/dom/stop', {
                    method: 'POST',
                    body: JSON.stringify({ symbol: targetSymbol }),
                    useCache: false
                });
            } catch (error) {
                if (String(error?.message || '').toLowerCase().includes('no active')) {
                    // Ignore "No active depth stream" noise
                } else {
                    this.notify('warning', `停止 ${targetSymbol} DOM 订阅失败: ${error?.message || error}`);
                }
            } finally {
                if (!symbol || symbol === this.state.activeStreamSymbol) {
                    this.state.activeStreamSymbol = null;
                    this.state.streamEnsuredAt = 0;
                }
            }
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
            this.setLoading('status', true);
            this.setButtonLoading(this.elements.refreshStatusBtn, true, '刷新中');
            this._pendingStatusInitial = Boolean(initial);
            const result = this.app?.ibkrManager && typeof this.app.ibkrManager.requestDomAutomationStatus === 'function'
                ? this.app.ibkrManager.requestDomAutomationStatus({ force })
                : 'failed';
            if (result === 'failed') {
                this.setLoading('status', false);
                this.setButtonLoading(this.elements.refreshStatusBtn, false);
                this._pendingStatusInitial = false;
                this.notify('error', '无法请求自动化状态，请检查WebSocket连接');
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
            const planFeatures = statusPayload?.plan?.feature_thresholds;
            if (planFeatures) {
                this.state.featureThresholds = cloneFeatureThresholds(planFeatures) || planFeatures;
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
            if (result?.updated_thresholds) {
                const baseThresholds = cloneBaseThresholds(result.updated_thresholds) || result.updated_thresholds;
                this.applyBaseThresholdConfig({
                    baseThresholds,
                    obiCandidates: this.state.baseThresholdCandidates?.obi,
                    ofiCandidates: this.state.baseThresholdCandidates?.ofi,
                    trendCandidates: this.state.baseThresholdCandidates?.trend,
                }, {
                    updateInput: Boolean(result?.applied),
                    persist: Boolean(result?.applied),
                });
            }
            if (result?.feature_thresholds) {
                const cloned = cloneFeatureThresholds(result.feature_thresholds) || result.feature_thresholds;
                const featureText = stringifyFeatureThresholds(cloned);
                if (featureText) {
                    parts.push(`<pre class="bg-light border rounded small px-2 py-1 text-break mt-2 mb-0">${escapeHtml(featureText)}</pre>`);
                }
                this.state.featureThresholds = cloned;
                if (result?.applied && featureText && this.elements.featureThresholdsInput) {
                    this.elements.featureThresholdsInput.value = featureText;
                    this.persistPreference(STORAGE_KEYS.featureThresholds, featureText);
                }
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

        async refreshStatistics({ silent = false, forceEnsure = false } = {}) {
            if (!this.elements.section) return;
            const rawSymbol = this.elements.symbolInput?.value?.trim();
            if (!rawSymbol) {
                if (!silent) this.notify('warning', '请先输入交易标的后再刷新统计');
                return;
            }
            const symbol = rawSymbol.toUpperCase();
            this.setLoading('stats', true);
            if (!silent) this.setButtonLoading(this.elements.refreshStatsBtn, true, '刷新中');
            try {
                await this.ensureDomStream(symbol, { force: forceEnsure });
            } catch (error) {
                if (!silent) {
                    this.notify('warning', `尝试启动 DOM 流失败: ${error?.message || error}`);
                }
            }
            const lookbackSeconds = this.resolveLookbackSeconds();
            const payload = {
                symbol,
                lookbackSeconds: Number.isFinite(lookbackSeconds) && lookbackSeconds > 0
                    ? Math.round(Number(lookbackSeconds))
                    : undefined,
                maxPoints: DOM_STATS_MAX_POINTS,
            };
            const result = this.app?.ibkrManager && typeof this.app.ibkrManager.requestDomStats === 'function'
                ? this.app.ibkrManager.requestDomStats(payload)
                : 'failed';
            if (result !== 'failed') {
                this._pendingStatsRequest = { symbol, silent: Boolean(silent) };
            } else {
                this.setLoading('stats', false);
                this.setButtonLoading(this.elements.refreshStatsBtn, false);
                if (!silent) {
                    this.notify('error', '无法请求统计数据，请检查WebSocket连接');
                }
            }
        }

        applyStatisticsPayload(payload) {
            if (!payload || payload.success === false) return;
            const stats = payload.stats || {};
            const metrics = Array.isArray(payload.chart_metrics) ? payload.chart_metrics : [];
            this.state.lastStats = stats;
            this.state.statsWindow = payload.window || null;
            this.state.statsMetadata = payload.metadata || null;
            this.state.latestSignal = payload.latest_signal || null;
            this.updateStatsIndicators(stats);
            this.updateSummary(stats, metrics, {
                latestSignal: payload.latest_signal,
                avgDelaySeconds: stats?.avg_delay_seconds ?? null,
                window: payload.window || null,
            });
            this.setHistoryMetrics(metrics);
        }

        onAutomationStatusMessage(message) {
            if (!this.elements.section) return true;
            this.setLoading('status', false);
            this.setButtonLoading(this.elements.refreshStatusBtn, false);
            const initial = this._pendingStatusInitial;
            this._pendingStatusInitial = false;
            if (!message || message.success === false) {
                const errorText = message?.error || message?.detail || '无法获取自动化状态';
                if (typeof errorText === 'string' && errorText.toLowerCase().includes('rate')) {
                    console.debug('DOM automation status request rate limited');
                } else {
                    this.notify('error', errorText);
                }
                return true;
            }
            const statusPayload = message.status && typeof message.status === 'object'
                ? message.status
                : message;
            this.renderAutomationStatus(statusPayload || {});
            if (initial) {
                this.notify('success', '已加载 DOM 自动化状态');
            }
            return true;
        }

        onDomStatsMessage(message) {
            if (!this.elements.section) return true;
            const currentSymbol = this.elements.symbolInput?.value?.trim()?.toUpperCase() || '';
            const payloadSymbol = (message?.symbol || '').toUpperCase();
            if (payloadSymbol && currentSymbol && payloadSymbol !== currentSymbol) {
                return true;
            }
            const pending = this._pendingStatsRequest;
            const silent = pending?.silent ?? true;
            if (pending && pending.symbol && payloadSymbol && pending.symbol !== payloadSymbol) {
                return true;
            }
            this._pendingStatsRequest = null;
            this.setLoading('stats', false);
            this.setButtonLoading(this.elements.refreshStatsBtn, false);
            if (!message || message.success === false) {
                if (!silent) {
                    const errorText = message?.error || message?.detail || '获取统计数据失败';
                    if (typeof errorText === 'string' && errorText.toLowerCase().includes('rate')) {
                        console.debug('DOM stats request rate limited');
                    } else {
                        this.notify('error', errorText);
                    }
                }
                return true;
            }
            this.applyStatisticsPayload(message);
            return true;
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
            const totalCount = Number.isFinite(totals.total) ? Math.round(totals.total) : '-';
            const longCount = Number.isFinite(totals.long) ? Math.round(totals.long) : '-';
            const shortCount = Number.isFinite(totals.short) ? Math.round(totals.short) : '-';
            const pendingCount = Number.isFinite(totals.pending) ? Math.round(totals.pending) : '-';

            if (this.elements.statsTotal) this.elements.statsTotal.textContent = totalCount;
            if (this.elements.statsBySide) this.elements.statsBySide.textContent = `${longCount} / ${shortCount}`;
            if (this.elements.statsSuccessRate) this.elements.statsSuccessRate.textContent = totals.precision != null ? formatPercent(totals.precision) : '-';
            if (this.elements.statsPending) this.elements.statsPending.textContent = pendingCount;
        }

        updateSummary(stats, metrics, extras = {}) {
            if (!this.elements.summaryPrecision) return;
            const totals = stats || {};
            this.elements.summaryPrecision.textContent = totals.precision != null
                ? formatPercent(totals.precision)
                : '-';
            const avgDelaySeconds = Number.isFinite(extras?.avgDelaySeconds)
                ? extras.avgDelaySeconds
                : totals.avg_delay_seconds;
            if (this.elements.summaryDelay) {
                this.elements.summaryDelay.textContent = formatSecondsBrief(avgDelaySeconds);
            }

            const latestSignal = extras?.latestSignal || this.resolveLatestSignal(metrics);
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
                const evaluated = Number.isFinite(totals?.evaluated) ? Math.round(totals.evaluated) : 0;
                const pending = Number.isFinite(totals?.pending) ? Math.round(totals.pending) : 0;
                if (evaluated || pending) {
                    this.elements.summaryNotes.textContent = `已评估 ${evaluated} 条信号，待评估 ${pending} 条。`;
                } else {
                    this.elements.summaryNotes.textContent = '暂无可用信号，请刷新统计或查询历史。';
                }
                if (extras?.window?.start && extras?.window?.end) {
                    const startLabel = formatIsoDatetime(extras.window.start);
                    const endLabel = formatIsoDatetime(extras.window.end);
                    this.elements.summaryNotes.title = `统计窗口：${startLabel} → ${endLabel}`;
                } else {
                    this.elements.summaryNotes.title = '';
                }
            }
        }

        setHistoryMetrics(metrics) {
            if (!Array.isArray(metrics) || !metrics.length) {
                this.state.historyMetrics = [];
                this.updateTrendChart([]);
                this.updateIndicatorChart([]);
                this.updateThresholdChart([]);
                return;
            }
            const ordered = metrics
                .filter((item) => item && resolveTimestampMs(item))
                .sort((a, b) => (resolveTimestampMs(a) || 0) - (resolveTimestampMs(b) || 0));
            this.state.historyMetrics = ordered;
            const trendThresholds = this.extractTrendThresholds(ordered);
            if (trendThresholds) {
                this.setTrendThresholds(trendThresholds);
            }
            this.ensureTrendChart();
            this.ensureIndicatorChart();
            this.updateTrendChart(ordered);
            this.updateIndicatorChart(ordered);
            this.updateThresholdChart(ordered);
        }

        safeParseBaseThresholds(rawValue) {
            try {
                return parseBaseThresholdsInput(rawValue);
            } catch (error) {
                if (error && error.message) {
                    this.notify('warning', error.message);
                }
                return null;
            }
        }

        applyBaseThresholdConfig(config, options = {}) {
            const { updateInput = false, persist = false } = options || {};
            const sanitized = config || {};
            const hasBase = Object.prototype.hasOwnProperty.call(sanitized, 'baseThresholds');
            const hasObi = Object.prototype.hasOwnProperty.call(sanitized, 'obiCandidates');
            const hasOfi = Object.prototype.hasOwnProperty.call(sanitized, 'ofiCandidates');
            const hasTrend = Object.prototype.hasOwnProperty.call(sanitized, 'trendCandidates');

            const resolvedBase = hasBase
                ? cloneBaseThresholds(sanitized.baseThresholds) || sanitized.baseThresholds || null
                : this.state.baseThresholds;
            const previousCandidates = this.state.baseThresholdCandidates || { obi: null, ofi: null, trend: null };
            const resolvedObi = hasObi
                ? normalizeCandidateArray(sanitized.obiCandidates)
                : normalizeCandidateArray(previousCandidates.obi);
            const resolvedOfi = hasOfi
                ? normalizeCandidateArray(sanitized.ofiCandidates)
                : normalizeCandidateArray(previousCandidates.ofi);
            const resolvedTrend = hasTrend
                ? normalizeCandidateArray(sanitized.trendCandidates)
                : normalizeCandidateArray(previousCandidates.trend);

            this.state.baseThresholds = resolvedBase
                ? cloneBaseThresholds(resolvedBase) || resolvedBase
                : null;
            this.state.baseThresholdCandidates = {
                obi: resolvedObi,
                ofi: resolvedOfi,
                trend: resolvedTrend,
            };

            if (updateInput && this.elements.baseThresholdsInput) {
                const text = stringifyBaseThresholds({
                    baseThresholds: this.state.baseThresholds,
                    obiCandidates: this.state.baseThresholdCandidates.obi,
                    ofiCandidates: this.state.baseThresholdCandidates.ofi,
                    trendCandidates: this.state.baseThresholdCandidates.trend,
                }) || '';
                this.elements.baseThresholdsInput.value = text;
                if (persist) {
                    this.persistPreference(STORAGE_KEYS.baseThresholds, text);
                }
            }
        }

        handleBaseThresholdInputChange() {
            const input = this.elements.baseThresholdsInput;
            if (!input) return;
            this.persistPreference(STORAGE_KEYS.baseThresholds, input.value ?? '');
        }

        handleBaseThresholdBlur() {
            const input = this.elements.baseThresholdsInput;
            if (!input) return;
            const text = typeof input.value === 'string' ? input.value.trim() : '';
            if (!text) {
                this.applyBaseThresholdConfig({
                    baseThresholds: null,
                    obiCandidates: null,
                    ofiCandidates: null,
                    trendCandidates: null,
                }, { updateInput: false, persist: true });
                return;
            }
            const parsed = this.safeParseBaseThresholds(text);
            if (parsed) {
                this.applyBaseThresholdConfig(parsed, { updateInput: true, persist: true });
            }
        }

        handleFeatureThresholdInputChange() {
            const input = this.elements.featureThresholdsInput;
            if (!input) return;
            this.persistPreference(STORAGE_KEYS.featureThresholds, input.value ?? '');
        }

        handleFeatureThresholdBlur() {
            const input = this.elements.featureThresholdsInput;
            if (!input) return;
            const text = typeof input.value === 'string' ? input.value.trim() : '';
            if (!text) {
                this.state.featureThresholds = null;
                this.persistPreference(STORAGE_KEYS.featureThresholds, '');
                return;
            }
            const parsed = this.safeParseFeatureThresholds(text);
            if (parsed) {
                const cloned = cloneFeatureThresholds(parsed) || parsed;
                this.state.featureThresholds = cloned;
                const normalized = stringifyFeatureThresholds(cloned);
                if (normalized) {
                    input.value = normalized;
                    this.persistPreference(STORAGE_KEYS.featureThresholds, normalized);
                }
            }
        }

        safeParseFeatureThresholds(rawValue) {
            try {
                return parseFeatureThresholdsInput(rawValue);
            } catch (error) {
                if (error && error.message) {
                    this.notify('warning', error.message);
                }
                return null;
            }
        }

        async fillFeatureThresholdPreset() {
            const button = this.elements.featurePresetBtn;
            try {
                this.setButtonLoading(button, true, '同步中');
                const response = await this.apiCall('/config/dom', {
                    allowReadOnly: true,
                    useCache: false,
                });
                if (!response || response.success === false) {
                    throw new Error(response?.detail || '获取配置失败');
                }
                const baseThresholds = cloneBaseThresholds(response?.tuner_base_thresholds)
                    || cloneBaseThresholds(DEFAULT_BASE_THRESHOLD_TEMPLATE.baseThresholds)
                    || DEFAULT_BASE_THRESHOLD_TEMPLATE.baseThresholds;
                const trendCandidates = Array.isArray(response?.trend_thresholds)
                    ? normalizeCandidateArray(response.trend_thresholds)
                    : null;
                const baseConfig = {
                    baseThresholds,
                    obiCandidates: this.state.baseThresholdCandidates?.obi
                        ?? DEFAULT_BASE_THRESHOLD_TEMPLATE.obiCandidates,
                    ofiCandidates: this.state.baseThresholdCandidates?.ofi
                        ?? DEFAULT_BASE_THRESHOLD_TEMPLATE.ofiCandidates,
                    trendCandidates: trendCandidates
                        ?? this.state.baseThresholdCandidates?.trend
                        ?? DEFAULT_BASE_THRESHOLD_TEMPLATE.trendCandidates,
                };
                this.applyBaseThresholdConfig(baseConfig, { updateInput: true, persist: true });

                const featureThresholds = cloneFeatureThresholds(response?.tuner_feature_thresholds)
                    || cloneFeatureThresholds(DEFAULT_FEATURE_THRESHOLD_TEMPLATE)
                    || DEFAULT_FEATURE_THRESHOLD_TEMPLATE;
                this.state.featureThresholds = featureThresholds;
                const featureText = stringifyFeatureThresholds(featureThresholds);
                if (this.elements.featureThresholdsInput) {
                    this.elements.featureThresholdsInput.value = featureText;
                }
                this.persistPreference(STORAGE_KEYS.featureThresholds, featureText);
                this.notify('success', '已填充当前 DOM 配置');
            } catch (error) {
                this.notify('error', `获取配置失败: ${error?.message || error}`);
            } finally {
                this.setButtonLoading(button, false);
            }
        }

        getBaseThresholdPayload() {
            const input = this.elements.baseThresholdsInput;
            if (!input) return null;
            const text = typeof input.value === 'string' ? input.value.trim() : '';
            if (!text) return null;
            const parsed = this.safeParseBaseThresholds(text);
            if (!parsed) {
                throw new Error('基础阈值 JSON 解析失败');
            }
            this.applyBaseThresholdConfig(parsed, { updateInput: true, persist: true });
            const candidates = this.state.baseThresholdCandidates || {};
            const cloneArray = (value) => (Array.isArray(value) && value.length ? value.map((num) => Number(num)).filter((num) => Number.isFinite(num)) : null);
            return {
                baseThresholds: this.state.baseThresholds
                    ? cloneBaseThresholds(this.state.baseThresholds) || this.state.baseThresholds
                    : null,
                obiCandidates: cloneArray(candidates.obi),
                ofiCandidates: cloneArray(candidates.ofi),
                trendCandidates: cloneArray(candidates.trend),
            };
        }

        getFeatureThresholdPayload() {
            const input = this.elements.featureThresholdsInput;
            if (!input) return null;
            const text = typeof input.value === 'string' ? input.value.trim() : '';
            if (!text) return null;
            const parsed = this.safeParseFeatureThresholds(text);
            if (!parsed) {
                throw new Error('特征阈值 JSON 解析失败');
            }
            const cloned = cloneFeatureThresholds(parsed) || parsed;
            this.state.featureThresholds = cloned;
            const normalized = stringifyFeatureThresholds(cloned);
            if (normalized && this.elements.featureThresholdsInput) {
                this.elements.featureThresholdsInput.value = normalized;
                this.persistPreference(STORAGE_KEYS.featureThresholds, normalized);
            }
            return cloned;
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
                        {
                            label: 'Events',
                            type: 'scatter',
                            data: [],
                            yAxisID: 'trendAxis',
                            parsing: false,
                            showLine: false,
                            pointRadius: 0,
                            pointHitRadius: 12,
                            domEvents: true,
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
                                    if (context.dataset?.domEvents) {
                                        const raw = context.raw || {};
                                        const label = raw.label || raw.code || '事件';
                                        if (raw.side) {
                                            const sideLabel = raw.side === 'buy' ? '买方' : raw.side === 'sell' ? '卖方' : raw.side;
                                            return `${label} (${sideLabel})`;
                                        }
                                        return label;
                                    }
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
                plugins: [domOptimizerEventPlugin],
            });
            chart._datasetMap = { price: 0, trend: 1, events: 2 };
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

        ensureThresholdChart() {
            if (this.charts.threshold) return this.charts.threshold;
            const canvas = this.elements.thresholdChart;
            if (!(canvas && canvas.getContext)) return null;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;

            const chart = new Chart(ctx, {
                type: 'line',
                data: {
                    datasets: [
                        {
                            label: '价格',
                            data: [],
                            yAxisID: 'priceAxis',
                            borderColor: '#2563eb',
                            backgroundColor: 'rgba(37,99,235,0.15)',
                            tension: 0.2,
                            pointRadius: 0,
                            spanGaps: true,
                        },
                        {
                            label: 'Long OBI 阈值',
                            data: [],
                            yAxisID: 'thresholdAxis',
                            borderColor: 'rgba(34,197,94,0.7)',
                            borderDash: [6, 3],
                            pointRadius: 0,
                            spanGaps: true,
                        },
                        {
                            label: 'Short OBI 阈值',
                            data: [],
                            yAxisID: 'thresholdAxis',
                            borderColor: 'rgba(239,68,68,0.7)',
                            borderDash: [6, 3],
                            pointRadius: 0,
                            spanGaps: true,
                        },
                        {
                            label: 'Long OFI 阈值',
                            data: [],
                            yAxisID: 'thresholdAxis',
                            borderColor: 'rgba(16,185,129,0.45)',
                            borderDash: [4, 4],
                            pointRadius: 0,
                            spanGaps: true,
                        },
                        {
                            label: 'Short OFI 阈值',
                            data: [],
                            yAxisID: 'thresholdAxis',
                            borderColor: 'rgba(239,68,68,0.45)',
                            borderDash: [4, 4],
                            pointRadius: 0,
                            spanGaps: true,
                        },
                    ],
                },
                options: {
                    parsing: false,
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: {
                            type: 'time',
                            time: { tooltipFormat: 'MM-dd HH:mm', displayFormats: { hour: 'MM-dd HH:mm', day: 'MM-dd' } },
                            grid: { color: 'rgba(148,163,184,0.1)' },
                        },
                        priceAxis: {
                            position: 'left',
                            grid: { drawOnChartArea: false },
                            title: { display: true, text: '价格' },
                        },
                        thresholdAxis: {
                            position: 'right',
                            grid: { drawOnChartArea: true, color: 'rgba(148,163,184,0.12)' },
                            title: { display: true, text: '阈值' },
                        },
                    },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => {
                                    const value = Number(context.parsed.y);
                                    if (!Number.isFinite(value)) return context.dataset.label || '';
                                    if (context.dataset.yAxisID === 'priceAxis') {
                                        return `${context.dataset.label}: $${formatNumber(value, 2)}`;
                                    }
                                    return `${context.dataset.label}: ${formatNumber(value, 2)}`;
                                },
                            },
                        },
                    },
                },
            });
            chart._datasetMap = { price: 0, longObi: 1, shortObi: 2, longOfi: 3, shortOfi: 4 };
            this.charts.threshold = chart;
            return chart;
        }

        updateTrendChart(metrics) {
            const chart = this.ensureTrendChart();
            if (!chart) return;
            const priceData = [];
            const trendData = [];
            const eventData = [];
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
                const events = Array.isArray(item?.events) ? item.events : [];
                if (events.length) {
                    events.forEach((evt) => {
                        const code = normaliseEventCode(evt);
                        eventData.push({
                            x: ts,
                            y: Number.isFinite(trend) ? trend : 0,
                            code,
                            label: typeof evt?.label === 'string' ? evt.label : code,
                            side: typeof evt?.side === 'string' ? evt.side : null,
                            color: resolveEventColor(evt),
                        });
                    });
                }
            }
            const priceDataset = chart.data.datasets[chart._datasetMap.price];
            const trendDataset = chart.data.datasets[chart._datasetMap.trend];
            const eventDataset = chart.data.datasets[chart._datasetMap.events];
            priceDataset.data = priceData;
            trendDataset.data = trendData;
            if (eventDataset) {
                eventDataset.data = eventData;
            }
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

        updateThresholdChart(metrics) {
            const chart = this.ensureThresholdChart();
            if (!chart) return;
            const map = chart._datasetMap || { price: 0, longObi: 1, shortObi: 2, longOfi: 3, shortOfi: 4 };

            const priceData = [];
            const longObiData = [];
            const shortObiData = [];
            const longOfiData = [];
            const shortOfiData = [];
            const thresholdValues = [];
            const priceValues = [];

            for (const item of metrics) {
                const ts = resolveTimestampMs(item);
                if (!ts) continue;
                const price = resolvePrice(item);
                if (Number.isFinite(price)) {
                    priceData.push({ x: ts, y: price });
                    priceValues.push(price);
                }
                const thresholds = item?.thresholds;
                if (thresholds && typeof thresholds === 'object') {
                    const longObi = toNumber(thresholds?.long?.obi ?? thresholds?.long?.obi5);
                    const shortObi = toNumber(thresholds?.short?.obi ?? thresholds?.short?.obi5);
                    const longOfi = toNumber(thresholds?.long?.ofi);
                    const shortOfi = toNumber(thresholds?.short?.ofi);
                    if (Number.isFinite(longObi)) {
                        longObiData.push({ x: ts, y: longObi });
                        thresholdValues.push(longObi);
                    }
                    if (Number.isFinite(shortObi)) {
                        shortObiData.push({ x: ts, y: shortObi });
                        thresholdValues.push(shortObi);
                    }
                    if (Number.isFinite(longOfi)) {
                        longOfiData.push({ x: ts, y: longOfi });
                        thresholdValues.push(longOfi);
                    }
                    if (Number.isFinite(shortOfi)) {
                        shortOfiData.push({ x: ts, y: shortOfi });
                        thresholdValues.push(shortOfi);
                    }
                }
            }

            const priceDataset = chart.data.datasets[map.price];
            if (priceDataset) priceDataset.data = priceData;
            const longObiDataset = chart.data.datasets[map.longObi];
            if (longObiDataset) longObiDataset.data = longObiData;
            const shortObiDataset = chart.data.datasets[map.shortObi];
            if (shortObiDataset) shortObiDataset.data = shortObiData;
            const longOfiDataset = chart.data.datasets[map.longOfi];
            if (longOfiDataset) longOfiDataset.data = longOfiData;
            const shortOfiDataset = chart.data.datasets[map.shortOfi];
            if (shortOfiDataset) shortOfiDataset.data = shortOfiData;

            const thresholdAxis = chart.options?.scales?.thresholdAxis;
            if (thresholdAxis && thresholdValues.length) {
                const min = Math.min(...thresholdValues);
                const max = Math.max(...thresholdValues);
                const span = max - min || 1;
                thresholdAxis.min = min - span * 0.15;
                thresholdAxis.max = max + span * 0.15;
            }

            const priceAxis = chart.options?.scales?.priceAxis;
            if (priceAxis && priceValues.length) {
                const min = Math.min(...priceValues);
                const max = Math.max(...priceValues);
                const span = max - min || (min * 0.05) || 1;
                priceAxis.min = min - span * 0.05;
                priceAxis.max = max + span * 0.05;
            }

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
            const thresholds = Array.isArray(this.state.trendThresholds)
                ? this.state.trendThresholds
                : [0.5, 0.7, 0.85, 0.95];
            let level = 0;
            thresholds.forEach((value, index) => {
                const num = Number(value);
                if (Number.isFinite(num) && abs >= num) {
                    level = index + 1;
                }
            });
            return Math.min(level, 4);
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
                    this.refreshStatistics({ silent: true, forceEnsure: true }).catch(() => {});
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
                await this.ensureDomStream(symbol);
                const lookbackMinutes = this.resolveLookbackMinutes({ allowFull: false, defaultMinutes: 240 }) || 240;
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
                const startDerived = !startValue || Number.isNaN(new Date(startValue).getTime());
                this.state.lastQueryRange = {
                    start: start.toISOString(),
                    end: end.toISOString(),
                    derived: startDerived,
                };
                this.applySignalFilter();
                this.renderSignalsSummary();
            } catch (error) {
                this.notify('error', `查询信号失败: ${error?.message || error}`);
                this.state.signals = [];
                this.state.filteredSignals = [];
                this.state.lowTrendFilteredCount = 0;
                this.state.lastQueryRange = null;
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

            let lowTrendFiltered = 0;
            const minTrend = this.resolveMinTrendThreshold();
            if (filter === 'pending' && Number.isFinite(minTrend)) {
                const before = filtered.length;
                filtered = filtered.filter((item) => {
                    const trendScore = toNumber(item?.trend_score);
                    if (!Number.isFinite(trendScore)) return false;
                    return Math.abs(trendScore) >= minTrend;
                });
                lowTrendFiltered = Math.max(0, before - filtered.length);
            }

            this.state.filteredSignals = filtered;
            this.state.lowTrendFilteredCount = lowTrendFiltered;
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
                pageItems.forEach((item, offset) => {
                    const absoluteIndex = startIndex + offset;
                    const row = document.createElement('tr');
                    const ts = resolveTimestampMs(item);
                    const dateLabel = ts ? formatIsoDatetime(ts) : '-';
                    const price = resolvePrice(item);
                    const obi = toNumber(item?.obi5);
                    const ofi = toNumber(item?.ofi);
                    const trend = toNumber(item?.trend_score);
                    const outcomeBadge = this.renderOutcomeBadge(item, absoluteIndex);
                    row.dataset.signalIndex = String(absoluteIndex);
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
                });
            }
            this.updatePaginationControls(totalPages);
            this.renderSignalsSummary();
            this.renderSignalsRange();
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

        renderOutcomeBadge(item, index) {
            const outcome = item?.outcome;
            let label = '待评估';
            let cls = 'bg-secondary';
            if (outcome === 1) {
                label = '成功';
                cls = 'bg-success';
            } else if (outcome === 0) {
                label = '失败';
                cls = 'bg-danger';
            }
            const attributes = [
                `class="badge ${cls}"`,
                'data-role="dom-outcome-badge"',
                `data-index="${Number.isFinite(index) ? index : ''}"`
            ];
            if (item?.signal_id) {
                attributes.push(`data-signal-id="${escapeHtml(item.signal_id)}"`);
            }
            return `<span ${attributes.join(' ')}>${label}</span>`;
        }

        renderSignalsSummary() {
            if (!this.elements.signalsSummary) return;
            const total = this.state.filteredSignals.length;
            const successes = this.state.filteredSignals.filter((item) => item?.outcome === 1).length;
            const failures = this.state.filteredSignals.filter((item) => item?.outcome === 0).length;
            const pending = total - successes - failures;
            const filterValue = this.elements.outcomeFilter?.value || '';
            const minTrend = this.resolveMinTrendThreshold();
            const trendNote = filterValue === 'pending' && Number.isFinite(minTrend)
                ? ` · Trend≥${minTrend.toFixed(2)}`
                : '';
            const filteredNote = filterValue === 'pending' && (this.state.lowTrendFilteredCount || 0) > 0
                ? ` · 已过滤低置信度 ${this.state.lowTrendFilteredCount} 条`
                : '';
            this.elements.signalsSummary.textContent = `${total} 条记录${trendNote} · 成功 ${successes} / 失败 ${failures} / 待评估 ${pending}${filteredNote}`;
        }

        renderSignalsRange() {
            if (!this.elements.signalsRange) return;
            const range = this.state.lastQueryRange;
            if (!range || !range.start || !range.end) {
                this.elements.signalsRange.textContent = '实际时间范围：-';
                return;
            }
            const startLabel = formatIsoDatetime(range.start);
            const endLabel = formatIsoDatetime(range.end);
            const suffix = range.derived ? '（自动回溯）' : '';
            this.elements.signalsRange.textContent = `实际时间范围：${startLabel} → ${endLabel}${suffix}`;
        }

        ensureOutcomeModal() {
            if (this._outcomeModalInstance) return this._outcomeModalInstance;
            const modalEl = this.elements.outcomeModal;
            if (!modalEl || typeof bootstrap === 'undefined' || typeof bootstrap.Modal !== 'function') {
                return null;
            }
            this._outcomeModalInstance = new bootstrap.Modal(modalEl, {
                backdrop: 'static',
                keyboard: true
            });
            return this._outcomeModalInstance;
        }

        async openOutcomeDetail(signal) {
            if (!signal) return;
            this.state.activeSignal = signal;
            this.state.activePriceHistory = [];
            const modal = this.ensureOutcomeModal();
            this.renderOutcomeSignal(signal);
            this.renderOutcomeOrders(signal, []);
            this.renderOutcomePricePlaceholder('加载价格数据…');
            if (this.elements.outcomePriceMeta) {
                this.elements.outcomePriceMeta.textContent = '';
            }
            if (modal) {
                modal.show();
            }

            let orders = [];
            try {
                orders = await this.fetchOrdersForSignal(signal);
                this.renderOutcomeOrders(signal, orders);
            } catch (error) {
                orders = [];
                this.renderOutcomeOrders(signal, []);
                this.showOutcomeAlert('danger', `加载关联订单失败：${error?.message || error}`);
            }

            try {
                const history = await this.fetchPriceHistoryForSignal(signal, orders);
                this.state.activePriceHistory = Array.isArray(history) ? history : [];
                this.renderOutcomePriceTrajectory(signal, orders, this.state.activePriceHistory);
            } catch (error) {
                const message = `加载价格数据失败：${error?.message || error}`;
                const previous = (this.elements.outcomeModalAlert?.textContent || '').trim();
                this.state.activePriceHistory = [];
                this.renderOutcomePricePlaceholder('加载价格数据失败');
                this.showOutcomeAlert('danger', previous ? `${previous} · ${message}` : message);
            }
        }

        showOutcomeAlert(level, message) {
            const el = this.elements.outcomeModalAlert;
            if (!el) return;
            if (!message) {
                el.classList.add('d-none');
                return;
            }
            el.textContent = message;
            el.classList.remove('d-none', 'alert-info', 'alert-warning', 'alert-danger', 'alert-success');
            if (level === 'success') el.classList.add('alert-success');
            else if (level === 'danger' || level === 'error') el.classList.add('alert-danger');
            else if (level === 'warning') el.classList.add('alert-warning');
            else el.classList.add('alert-info');
        }

        renderOutcomeSignal(signal) {
            const {
                outcomeModalTitle,
                outcomeSignalMetrics,
                outcomeSignalLevel,
                outcomeModalMeta
            } = this.elements;
            const symbol = (signal?.symbol || this.elements.symbolInput?.value || '').toString().trim().toUpperCase();
            const ts = resolveTimestampMs(signal);
            const timeLabel = ts ? formatIsoDatetime(ts) : '-';
            const direction = signal?.signal_type === 'short' ? '空头' : signal?.signal_type === 'long' ? '多头' : '—';
            const outcome = signal?.outcome;
            let statusLabel = '待评估';
            let alertLevel = null;
            if (outcome === 1) {
                statusLabel = '成功';
                alertLevel = null;
            } else if (outcome === 0) {
                statusLabel = '失败';
                alertLevel = null;
            } else {
                alertLevel = 'warning';
            }

            if (outcomeModalTitle) {
                const title = `${symbol || '-'} · ${direction} · ${statusLabel}`;
                outcomeModalTitle.innerHTML = `<i class="fas fa-clipboard-list me-2"></i>${escapeHtml(title)}`;
            }

            if (alertLevel) {
                this.showOutcomeAlert(alertLevel, '该信号仍在评估窗口内，Outcome 暂不可用。');
            } else {
                this.showOutcomeAlert(null, '');
            }

            if (outcomeSignalMetrics) {
                outcomeSignalMetrics.innerHTML = '';
                const trendScore = toNumber(signal?.trend_score);
                const signalPrice = this.coercePriceValue(signal?.signal_price ?? resolvePrice(signal));
                const obi = toNumber(signal?.obi5);
                const ofi = toNumber(signal?.ofi);
                const outcomePrice = this.coercePriceValue(signal?.outcome_price);
                const outcomeTs = toNumber(signal?.outcome_timestamp ?? signal?.outcome_ts);
                const priceDelta = Number.isFinite(signalPrice) && Number.isFinite(outcomePrice)
                    ? outcomePrice - signalPrice
                    : null;
                const pctDelta = Number.isFinite(signalPrice) && signalPrice !== 0 && Number.isFinite(priceDelta)
                    ? priceDelta / signalPrice
                    : null;
                const delaySeconds = Number.isFinite(ts) && Number.isFinite(outcomeTs)
                    ? (outcomeTs - ts) / 1000
                    : null;
                const outcomeThresholdPct = toNumber(signal?.outcome_threshold_pct);
                const outcomeTargetCurrency = toNumber(signal?.outcome_target_currency);
                const outcomePnlCurrency = toNumber(signal?.outcome_pnl_currency ?? signal?.outcome_pnl);
                const outcomeTierRaw = signal?.outcome_tier;
                const tierLabelMap = {
                    small_win: '小赢',
                    medium_win: '中赢',
                    big_win: '大赢',
                    loss: '亏损',
                    flat: '持平'
                };
                const outcomeTierLabel = outcomeTierRaw && tierLabelMap[outcomeTierRaw]
                    ? tierLabelMap[outcomeTierRaw]
                    : outcomeTierRaw;

                const entries = [
                    { label: '信号时间', value: timeLabel },
                    { label: '信号方向', value: direction },
                    { label: 'Trend Score', value: Number.isFinite(trendScore) ? trendScore.toFixed(2) : '-' },
                    { label: 'Signal Price', value: Number.isFinite(signalPrice) ? formatNumber(signalPrice, 2) : '-' },
                    { label: 'OBI', value: Number.isFinite(obi) ? formatNumber(obi, 2) : '-' },
                    { label: 'OFI', value: Number.isFinite(ofi) ? formatNumber(ofi, 0) : '-' },
                    { label: 'Outcome 状态', value: statusLabel },
                    { label: 'Outcome 价格', value: Number.isFinite(outcomePrice) ? formatNumber(outcomePrice, 2) : '-' },
                    { label: 'Outcome 时间', value: Number.isFinite(outcomeTs) ? formatIsoDatetime(outcomeTs) : '-' },
                    { label: '价格变化', value: Number.isFinite(priceDelta) ? `${priceDelta >= 0 ? '+' : ''}${formatNumber(priceDelta, 2)}` : '-' },
                    { label: '变化幅度', value: Number.isFinite(pctDelta) ? formatPercent(pctDelta) : '-' },
                    { label: '评估耗时', value: Number.isFinite(delaySeconds) ? `${delaySeconds.toFixed(delaySeconds >= 10 ? 0 : 1)} s` : '-' }
                ];
                if (Number.isFinite(outcomeThresholdPct)) {
                    entries.push({ label: '成功阈值', value: formatPercent(outcomeThresholdPct) });
                }
                if (Number.isFinite(outcomeTargetCurrency)) {
                    entries.push({ label: '目标收益($)', value: `${outcomeTargetCurrency >= 0 ? '+' : ''}${formatNumber(outcomeTargetCurrency, 2)}` });
                }
                if (Number.isFinite(outcomePnlCurrency)) {
                    entries.push({ label: '实际收益($)', value: `${outcomePnlCurrency >= 0 ? '+' : ''}${formatNumber(outcomePnlCurrency, 2)}` });
                }
                if (outcomeTierLabel) {
                    entries.push({ label: 'Outcome 分类', value: outcomeTierLabel });
                }

                for (const entry of entries) {
                    const dt = document.createElement('dt');
                    dt.className = 'col-md-4 text-muted mb-0';
                    dt.textContent = entry.label;
                    const dd = document.createElement('dd');
                    dd.className = 'col-md-8 mb-0';
                    dd.textContent = entry.value;
                    outcomeSignalMetrics.appendChild(dt);
                    outcomeSignalMetrics.appendChild(dd);
                }
            }

            if (outcomeSignalLevel) {
                const level = this.resolveSignalLevel(signal);
                if (level > 0) {
                    outcomeSignalLevel.textContent = `Level ${level}`;
                    outcomeSignalLevel.classList.remove('d-none');
                } else {
                    outcomeSignalLevel.classList.add('d-none');
                }
            }

            if (outcomeModalMeta) {
                const segments = [];
                if (signal?.signal_id) {
                    segments.push(`Signal ID ${signal.signal_id}`);
                }
                const outcomeTs = toNumber(signal?.outcome_timestamp ?? signal?.outcome_ts);
                if (Number.isFinite(ts) && Number.isFinite(outcomeTs)) {
                    const elapsed = (outcomeTs - ts) / 1000;
                    segments.push(`评估耗时 ${elapsed.toFixed(elapsed >= 10 ? 0 : 1)} s`);
                }
                outcomeModalMeta.textContent = segments.join(' · ');
            }
        }

        toggleOrdersLoading(isLoading) {
            const spinner = this.elements.outcomeOrdersLoading;
            if (!spinner) return;
            spinner.classList.toggle('d-none', !isLoading);
        }

        async fetchOrdersForSignal(signal) {
            const symbol = (signal?.symbol || this.elements.symbolInput?.value || '').toString().trim().toUpperCase();
            const timestamp = resolveTimestampMs(signal);
            const now = Date.now();
            const cache = this.state.orderCache;
            if (
                cache
                && cache.symbol === symbol
                && Array.isArray(cache.items)
                && now - (cache.fetchedAt || 0) < 30_000
            ) {
                return this.filterOrdersForSignal(signal, cache.items);
            }

            const params = new URLSearchParams();
            params.set('limit', '200');
            if (Number.isFinite(timestamp)) {
                const start = new Date(timestamp - 60 * 60 * 1000);
                params.set('start_date', start.toISOString());
            }

            this.toggleOrdersLoading(true);
            try {
                const response = await this.apiCall(`/orders?${params.toString()}`, {
                    method: 'GET',
                    allowReadOnly: true
                });
                const orders = Array.isArray(response?.orders) ? response.orders : [];
                this.state.orderCache = {
                    symbol,
                    fetchedAt: now,
                    items: orders
                };
                return this.filterOrdersForSignal(signal, orders);
            } finally {
                this.toggleOrdersLoading(false);
            }
        }

        filterOrdersForSignal(signal, orders) {
            if (!Array.isArray(orders) || !orders.length) {
                return [];
            }
            const symbol = (signal?.symbol || this.elements.symbolInput?.value || '').toString().trim().toUpperCase();
            const signalId = signal?.order_id || signal?.signal_id;
            const timestamp = resolveTimestampMs(signal);
            const windowMs = 15 * 60 * 1000;

            let matches = [];
            if (signalId) {
                const idText = String(signalId);
                matches = orders.filter((order) => {
                    const orderId = order?.order_id != null ? String(order.order_id) : '';
                    const parentId = order?.parent_order_id != null ? String(order.parent_order_id) : '';
                    return orderId === idText || parentId === idText;
                });
            }
            if (!matches.length) {
                matches = orders.filter((order) => {
                    if (symbol && String(order?.symbol || '').toUpperCase() !== symbol) return false;
                    const submitTs = this.parseOrderTimestamp(order?.submit_time);
                    const executedTs = this.parseOrderTimestamp(order?.executed_at);
                    const reference = Number.isFinite(executedTs) ? executedTs : submitTs;
                    if (!Number.isFinite(reference)) return false;
                    if (!Number.isFinite(timestamp)) return true;
                    return Math.abs(reference - timestamp) <= windowMs;
                });
            }
            return matches.slice(0, 10);
        }

        async fetchPriceHistoryForSignal(signal, orders) {
            const symbol = (signal?.symbol || this.elements.symbolInput?.value || '').toString().trim().toUpperCase();
            if (!symbol) return [];
            const reference = this.resolveOutcomeReference(signal, orders);
            const baseTs = reference?.timestamp;
            if (!Number.isFinite(baseTs)) return [];

            const offsets = [0, 300, 600, 1800, 3600, 7200, 14400];
            const maxOffset = Math.max(...offsets);
            const offsetMs = Math.max(maxOffset, 1) * 1000;
            const start = new Date(baseTs - offsetMs);
            const end = new Date(baseTs + offsetMs);
            const params = new URLSearchParams();
            params.set('start', start.toISOString());
            params.set('end', end.toISOString());
            params.set('limit', '0');
            params.set('include_snapshots', 'true');

            if (!this.state.priceHistoryCache) {
                this.state.priceHistoryCache = {};
            }
            const cacheKey = `${symbol}:${Math.floor(baseTs / 1000)}:${maxOffset}`;
            const cached = this.state.priceHistoryCache[cacheKey];
            const now = Date.now();
            if (cached && now - (cached.fetchedAt || 0) < 30_000) {
                return cached.items;
            }

            const response = await this.apiCall(`/dom/history/${encodeURIComponent(symbol)}?${params.toString()}`, {
                allowReadOnly: true,
                useCache: false
            });
            const snapshots = Array.isArray(response?.snapshots) ? response.snapshots : [];
            const metrics = Array.isArray(response?.metrics) ? response.metrics : [];
            const merged = [...snapshots, ...metrics]
                .filter(Boolean)
                .sort((a, b) => (resolveTimestampMs(a) || 0) - (resolveTimestampMs(b) || 0));

            this.state.priceHistoryCache[cacheKey] = {
                items: merged,
                fetchedAt: now
            };
            return merged;
        }

        parseOrderTimestamp(value) {
            if (!value) return null;
            const date = value instanceof Date ? value : new Date(value);
            const time = date.getTime();
            return Number.isNaN(time) ? null : time;
        }

        renderOutcomeOrders(signal, orders) {
            const tbody = this.elements.outcomeOrdersTbody;
            if (!tbody) return;
            tbody.innerHTML = '';
            if (!orders || !orders.length) {
                const row = document.createElement('tr');
                row.innerHTML = '<td colspan="7" class="text-center text-muted py-3">暂无关联订单</td>';
                tbody.appendChild(row);
                return;
            }
            for (const order of orders) {
                const row = document.createElement('tr');
                const qty = Number.isFinite(Number(order?.quantity)) ? Number(order.quantity) : null;
                const avgPrice = this.coercePriceValue(order?.avg_fill_price);
                const submitLabel = order?.submit_time ? formatIsoDatetime(order.submit_time) : '-';
                const execLabel = order?.executed_at ? formatIsoDatetime(order.executed_at) : '-';
                const statusBadge = this.renderOrderStatus(order?.status);
                row.innerHTML = `
                    <td class="text-nowrap">${escapeHtml(order?.order_id || '-')}</td>
                    <td>${escapeHtml(order?.action || '-')}</td>
                    <td>${qty != null ? qty : '-'}</td>
                    <td>${Number.isFinite(avgPrice) ? formatNumber(avgPrice, 2) : '-'}</td>
                    <td>${statusBadge}</td>
                    <td class="text-nowrap">${submitLabel}</td>
                    <td class="text-nowrap">${execLabel}</td>
                `;
                tbody.appendChild(row);
            }
        }

        renderOutcomePricePlaceholder(message) {
            const tbody = this.elements.outcomePriceTbody;
            if (!tbody) return;
            const text = message || '暂无可用价格数据';
            tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-3">${escapeHtml(text)}</td></tr>`;
        }

        renderOrderStatus(status) {
            const text = status ? String(status) : '-';
            const normalized = text.toLowerCase();
            let cls = 'bg-secondary';
            if (normalized.includes('filled')) cls = 'bg-success';
            else if (normalized.includes('cancel')) cls = 'bg-warning text-dark';
            else if (normalized.includes('reject')) cls = 'bg-danger';
            else if (normalized.includes('submit') || normalized.includes('pre')) cls = 'bg-info';
            return `<span class="badge ${cls}">${escapeHtml(text)}</span>`;
        }

        resolveOutcomeReference(signal, orders) {
            const signalTs = resolveTimestampMs(signal);
            let bestTimestamp = Number.isFinite(signalTs) ? signalTs : null;
            let bestDiff = Number.isFinite(signalTs) ? 0 : Infinity;
            let bestOrder = null;

            if (Array.isArray(orders)) {
                for (const order of orders) {
                    const executedTs = this.parseOrderTimestamp(order?.executed_at);
                    if (Number.isFinite(executedTs)) {
                        const diff = Number.isFinite(signalTs) ? Math.abs(executedTs - signalTs) : 0;
                        if (!Number.isFinite(bestTimestamp) || diff < bestDiff) {
                            bestTimestamp = executedTs;
                            bestDiff = diff;
                            bestOrder = order;
                        }
                    }
                    const submitTs = this.parseOrderTimestamp(order?.submit_time);
                    if (Number.isFinite(submitTs)) {
                        const diff = Number.isFinite(signalTs) ? Math.abs(submitTs - signalTs) : 0;
                        if (!Number.isFinite(bestTimestamp) || diff < bestDiff) {
                            bestTimestamp = submitTs;
                            bestDiff = diff;
                            bestOrder = order;
                        }
                    }
                }
            }

            if (!Number.isFinite(bestTimestamp)) {
                return {
                    order: null,
                    timestamp: Number.isFinite(signalTs) ? signalTs : null
                };
            }
            return {
                order: bestOrder,
                timestamp: bestTimestamp
            };
        }

        renderOutcomePriceTrajectory(signal, orders, historyOverride = null) {
            const tbody = this.elements.outcomePriceTbody;
            if (!tbody) return;
            tbody.innerHTML = '';
            const history = Array.isArray(historyOverride)
                ? historyOverride
                : Array.isArray(this.state.activePriceHistory)
                    ? this.state.activePriceHistory
                    : Array.isArray(this.state.historyMetrics)
                        ? this.state.historyMetrics
                        : [];
            if (!history.length) {
                this.renderOutcomePricePlaceholder('暂无可用价格数据');
                if (this.elements.outcomePriceMeta) this.elements.outcomePriceMeta.textContent = '';
                return;
            }

            const reference = this.resolveOutcomeReference(signal, orders);
            const baseTs = Number.isFinite(reference?.timestamp)
                ? reference.timestamp
                : resolveTimestampMs(signal);

            if (!Number.isFinite(baseTs)) {
                this.renderOutcomePricePlaceholder('缺少参考时间，无法计算价格变化');
                if (this.elements.outcomePriceMeta) this.elements.outcomePriceMeta.textContent = '';
                return;
            }

            const filledOrder = Array.isArray(orders)
                ? orders.find((order) => String(order?.status || '').toLowerCase().includes('filled'))
                : null;
            const baselineOrder = filledOrder || reference?.order || null;

            let basePrice = this.coercePriceValue(baselineOrder?.avg_fill_price);
            if (!Number.isFinite(basePrice)) basePrice = this.coercePriceValue(baselineOrder?.limit_price);
            if (!Number.isFinite(basePrice)) basePrice = this.coercePriceValue(signal?.signal_price ?? resolvePrice(signal));
            if (!Number.isFinite(basePrice)) {
                const sample = this.findNearestPrice(baseTs, history);
                if (sample) basePrice = sample.price;
            }

            const offsets = [0, 300, 600, 1800, 3600, 7200, 14400];

            const rows = [];
            let anchorPrice = Number.isFinite(basePrice) ? basePrice : null;
            for (const offset of offsets) {
                const sample = this.findNearestPrice(baseTs + offset * 1000, history);
                if (!sample) continue;
                if (!Number.isFinite(anchorPrice)) {
                    anchorPrice = sample.price;
                }
                const delta = Number.isFinite(anchorPrice) ? sample.price - anchorPrice : null;
                const pct = Number.isFinite(anchorPrice) && anchorPrice !== 0 && Number.isFinite(delta)
                    ? delta / anchorPrice
                    : null;
                rows.push({
                    offset,
                    price: sample.price,
                    delta,
                    pct,
                    timestamp: sample.timestamp
                });
            }

            if (!rows.length) {
                this.renderOutcomePricePlaceholder('未找到相邻时间段的价格数据');
            } else {
                for (const rowData of rows) {
                    const tr = document.createElement('tr');
                    const offsetLabel = this.formatOffsetLabel(rowData.offset);
                    const deltaClass = rowData.delta == null
                        ? 'text-muted'
                        : rowData.delta > 0
                            ? 'text-success'
                            : rowData.delta < 0
                                ? 'text-danger'
                                : 'text-muted';
                    const deltaLabel = rowData.delta == null
                        ? '-'
                        : `${rowData.delta > 0 ? '+' : ''}${formatNumber(rowData.delta, 2)}`;
                    const pctLabel = rowData.pct == null
                        ? '-'
                        : `${rowData.pct > 0 ? '+' : ''}${formatPercent(rowData.pct)}`;
                    tr.innerHTML = `
                        <td>${offsetLabel}</td>
                        <td>${formatNumber(rowData.price, 2)}</td>
                        <td class="${deltaClass}">${deltaLabel}</td>
                        <td class="${deltaClass}">${pctLabel}</td>
                        <td class="text-nowrap">${formatIsoDatetime(rowData.timestamp)}</td>
                    `;
                    tbody.appendChild(tr);
                }
            }

            if (this.elements.outcomePriceMeta) {
                let baseSource = '信号';
                if (baselineOrder?.order_id) {
                    baseSource = `订单 ${baselineOrder.order_id}`;
                } else if (signal?.signal_id) {
                    baseSource = `信号 ${signal.signal_id}`;
                }
                const basePriceLabel = Number.isFinite(anchorPrice)
                    ? ` · 价格 ${formatNumber(anchorPrice, 2)}`
                    : '';
                this.elements.outcomePriceMeta.textContent = `基准：${baseSource} @ ${formatIsoDatetime(baseTs)}${basePriceLabel}`;
            }
        }

        findNearestPrice(targetTs, historyOverride = null) {
            if (!Number.isFinite(targetTs)) return null;
            const history = Array.isArray(historyOverride)
                ? historyOverride
                : Array.isArray(this.state.activePriceHistory)
                    ? this.state.activePriceHistory
                    : Array.isArray(this.state.historyMetrics)
                        ? this.state.historyMetrics
                        : [];
            let closest = null;
            let minDiff = Infinity;
            for (const item of history) {
                const ts = resolveTimestampMs(item);
                if (!Number.isFinite(ts)) continue;
                const diff = Math.abs(ts - targetTs);
                if (diff < minDiff) {
                    const price = this.coercePriceValue(resolvePrice(item));
                    if (!Number.isFinite(price)) continue;
                    minDiff = diff;
                    closest = { price, timestamp: ts };
                }
            }
            return closest;
        }

        coercePriceValue(value) {
            if (value === null || value === undefined) return null;
            const num = Number(value);
            return Number.isFinite(num) ? num : null;
        }

        formatOffsetLabel(seconds) {
            if (!Number.isFinite(seconds)) return '-';
            if (seconds === 0) return '成交时刻';
            if (seconds < 60) return `+${seconds} 秒`;
            if (seconds % 3600 === 0) {
                const hours = seconds / 3600;
                return `+${hours} 小时`;
            }
            if (seconds % 60 === 0) return `+${seconds / 60} 分钟`;
            return `+${(seconds / 60).toFixed(1)} 分钟`;
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
                const lookbackMinutes = this.resolveLookbackMinutes({ allowFull: false, defaultMinutes: 240 }) || 240;
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
                const baseConfig = this.getBaseThresholdPayload();
                if (baseConfig) {
                    if (baseConfig.baseThresholds) {
                        payload.base_thresholds = baseConfig.baseThresholds;
                    }
                    if (baseConfig.obiCandidates) {
                        payload.obi_candidates = baseConfig.obiCandidates;
                    }
                    if (baseConfig.ofiCandidates) {
                        payload.ofi_candidates = baseConfig.ofiCandidates;
                    }
                    if (baseConfig.trendCandidates) {
                        payload.trend_candidates = baseConfig.trendCandidates;
                    }
                }
                const featureThresholds = this.getFeatureThresholdPayload();
                if (featureThresholds) payload.feature_thresholds = featureThresholds;
                const response = await this.apiCall('/dom/optimize', {
                    method: 'POST',
                    body: JSON.stringify(payload),
                    useCache: false
                });
                if (!response || response.success === false) {
                    throw new Error(response?.detail || '优化失败');
                }
                this.renderOptimizeResult(response);
                this.refreshAutomationStatus({ force: true }).catch(() => {});
                this.refreshStatistics({ silent: true, forceEnsure: true }).catch(() => {});
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
            const trendList = Array.isArray(result?.trend_thresholds)
                ? result.trend_thresholds
                : null;
            if (trendList && result?.applied) {
                this.setTrendThresholds(trendList);
            }
            if (result?.updated_thresholds) {
                const baseThresholds = cloneBaseThresholds(result.updated_thresholds) || result.updated_thresholds;
                const config = {
                    baseThresholds,
                    obiCandidates: this.state.baseThresholdCandidates?.obi,
                    ofiCandidates: this.state.baseThresholdCandidates?.ofi,
                    trendCandidates: trendList || this.state.baseThresholdCandidates?.trend,
                };
                this.applyBaseThresholdConfig(config, {
                    updateInput: Boolean(result?.applied),
                    persist: Boolean(result?.applied),
                });
            }
            let featureText = '';
            if (result?.feature_thresholds) {
                const cloned = cloneFeatureThresholds(result.feature_thresholds) || result.feature_thresholds;
                featureText = stringifyFeatureThresholds(cloned) || '';
                this.state.featureThresholds = cloned;
                if (result?.applied && this.elements.featureThresholdsInput) {
                    this.elements.featureThresholdsInput.value = featureText;
                    this.persistPreference(STORAGE_KEYS.featureThresholds, featureText);
                }
            }
            const rows = [
                `<div class="mb-2">状态: ${outcome} · 完成时间 ${completedAt}</div>`,
                `<div>精度: <strong>${precision}</strong> · 评估 ${evaluated} 次 (成功 ${positives} / 失败 ${negatives} / 待评估 ${pending})</div>`
            ];
            if (thresholds) rows.push(`<div class="text-muted">阈值: ${thresholds}</div>`);
            const trendLabel = trendList
                ? trendList.map((value) => formatNumber(Number(value), 2)).join(', ')
                : Array.isArray(this.state.trendThresholds)
                    ? this.state.trendThresholds.map((value) => formatNumber(Number(value), 2)).join(', ')
                    : null;
            if (trendLabel) {
                rows.push(`<div class="text-muted">Trend 阈值: ${trendLabel}</div>`);
            }
            if (featureText) {
                rows.push(`<pre class="bg-light border rounded small px-2 py-1 text-break">${escapeHtml(featureText)}</pre>`);
            }
            this.elements.optimizeResult.innerHTML = rows.join('');
        }

        async loadCurrentConfig(options = {}) {
            const { auto = false } = options || {};
            try {
                if (this.app && typeof this.app.isAuthenticated === 'function' && !this.app.isAuthenticated()) {
                    if (!auto) {
                        this.notify('warning', '请先登录后再加载配置');
                    } else {
                        console.info('Skipping DOM config load: user is not authenticated yet.');
                    }
                    return null;
                }

                if (!auto) {
                    this.setButtonLoading(this.elements.loadConfigBtn, true, '载入中');
                }
                const response = await this.apiCall('/config/dom', {
                    allowReadOnly: true,
                    useCache: false
                });
                if (!response || response.success === false) {
                    throw new Error(response?.detail || '获取配置失败');
                }
                const thresholds = this.describeThresholds(response?.tuner_base_thresholds);
                this.setTrendThresholds(response?.trend_thresholds);
                const baseThresholds = cloneBaseThresholds(response?.tuner_base_thresholds)
                    || cloneBaseThresholds(DEFAULT_BASE_THRESHOLD_TEMPLATE.baseThresholds)
                    || DEFAULT_BASE_THRESHOLD_TEMPLATE.baseThresholds;
                const trendCandidates = Array.isArray(response?.trend_thresholds)
                    ? normalizeCandidateArray(response.trend_thresholds)
                    : null;
                const baseConfig = {
                    baseThresholds,
                    obiCandidates: this.state.baseThresholdCandidates?.obi
                        ?? DEFAULT_BASE_THRESHOLD_TEMPLATE.obiCandidates,
                    ofiCandidates: this.state.baseThresholdCandidates?.ofi
                        ?? DEFAULT_BASE_THRESHOLD_TEMPLATE.ofiCandidates,
                    trendCandidates: trendCandidates
                        ?? this.state.baseThresholdCandidates?.trend
                        ?? DEFAULT_BASE_THRESHOLD_TEMPLATE.trendCandidates,
                };
                this.applyBaseThresholdConfig(baseConfig, { updateInput: true, persist: true });
                const trendThresholds = Array.isArray(this.state.trendThresholds)
                    ? this.state.trendThresholds.map((v) => formatNumber(Number(v), 2)).join(', ')
                    : '-';
                const html = [
                    `<div>当前基础阈值: ${thresholds || '未定义'}</div>`,
                    `<div class="text-muted">Trend 阈值: ${trendThresholds}</div>`
                ].join('');
                this.elements.optimizeResult.innerHTML = html;
                const featureThresholds = cloneFeatureThresholds(response?.tuner_feature_thresholds)
                    || cloneFeatureThresholds(DEFAULT_FEATURE_THRESHOLD_TEMPLATE)
                    || DEFAULT_FEATURE_THRESHOLD_TEMPLATE;
                this.state.featureThresholds = featureThresholds;
                const featureText = stringifyFeatureThresholds(featureThresholds);
                if (this.elements.featureThresholdsInput) {
                    this.elements.featureThresholdsInput.value = featureText;
                }
                this.persistPreference(STORAGE_KEYS.featureThresholds, featureText);
            } catch (error) {
                if (!auto) {
                    this.notify('error', `加载配置失败: ${error?.message || error}`);
                } else {
                    console.warn('自动加载 DOM 配置失败:', error);
                }
            } finally {
                if (!auto) {
                    this.setButtonLoading(this.elements.loadConfigBtn, false);
                }
            }
        }

        fillSymbolFromMonitor() {
            const monitorSymbol = this.app?.currentMonitor?.symbol
                || this.app?.monitorRealtimeSymbol
                || this.app?.domManager?.symbol
                || '';
            const inputSymbol = this.elements.symbolInput?.value || '';
            const storedSymbol = localStorage.getItem(STORAGE_KEYS.symbol) || '';

            const candidate = (monitorSymbol || inputSymbol || storedSymbol || '').toString().trim().toUpperCase();

            if (!candidate) {
                this.notify('warning', '请输入交易标的后再点击“监控”。');
                this.elements.symbolInput?.focus?.();
                return;
            }

            if (this.elements.symbolInput) {
                this.elements.symbolInput.value = candidate;
                this.persistPreference(STORAGE_KEYS.symbol, candidate);
                this.state.streamEnsuredAt = 0;
                this.ensureDomStream(candidate).catch(() => {});
            }
        }
    }

    const domOptimizer = new DOMOptimizerUI();
    if (typeof window !== 'undefined') {
        window.domOptimizerUI = domOptimizer;
    }
}());
