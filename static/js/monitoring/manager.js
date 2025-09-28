(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const monitoring = appModules.monitoring = appModules.monitoring || {};
    const monitoringConstants = monitoring.constants || {};
    const monitoringUtils = monitoring.utils || {};

    const {
        MONITOR_DURATION_OPTIONS = {},
        INTERVAL_MINUTES_MAP = {}
    } = monitoringConstants;

    const fallbackNormalizeIntervalKey = (value) => {
        if (typeof value === 'string') {
            return value.replace(/\s+/g, '').toLowerCase();
        }
        if (typeof value === 'number') {
            return String(value);
        }
        return '';
    };

    const fallbackGetMonitorDurationOptions = (intervalValue) => {
        const intervalKey = typeof intervalValue === 'string'
            ? intervalValue.trim().toLowerCase()
            : '';
        return MONITOR_DURATION_OPTIONS[intervalKey] || MONITOR_DURATION_OPTIONS['1 min'] || [];
    };

    const {
        normalizeIntervalKey = fallbackNormalizeIntervalKey,
        getMonitorDurationOptions = fallbackGetMonitorDurationOptions
    } = monitoringUtils;

    class MonitoringManager {
        constructor(app) {
            this.app = app;
            this.monitorRealtimeSymbol = null;
            this.monitorRealtimeShouldResume = false;
            this.monitorRealtimeResumeSymbol = null;
            this.monitorChartTimezone = null;
            this.priceCache = new Map();
            this.priceCacheTimeout = 30000;
            this.domSignals = [];
            this.monitorTrendChart = null;
            this.monitorTrendData = [];
            this.monitorTrendDatasetMap = { trend: 0, events: 1, price: 2 };
            this.monitorDomHistoryList = null;
            this.monitorDomHistoryMeta = null;
            this.candlestickSupported = false;
            this.candlestickLoader = null;
            this.luxonScriptUrl = 'https://cdn.jsdelivr.net/npm/luxon@3.4.4/build/global/luxon.min.js';
            this.luxonLoader = null;
            this.chartDateAdapterUrl = 'https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.3.1/dist/chartjs-adapter-luxon.umd.min.js';
            this.dateAdapterReady = false;
            this.dateAdapterLoader = null;
            this.financialScriptUrl = 'https://cdn.jsdelivr.net/npm/chartjs-chart-financial@4/dist/chartjs-chart-financial.umd.min.js';
            this.chartScriptableGuardApplied = false;
            this.strategyChartInstance = null;
            this.strategyChartFallbackNotified = false;
            this.strategyChartState = this.createDefaultStrategyChartState();
            this.monitorChartRenderTimeout = null;
            this.pendingMonitorChartRender = null;
            this._syncingCharts = false;
            this.monitorModalFullscreen = false;
            this._monitorFullscreenChangeHandler = null;
        }

        getManagedProperties() {
            return [
                'monitorRealtimeSymbol',
                'monitorRealtimeShouldResume',
                'monitorRealtimeResumeSymbol',
                'monitorChartTimezone',
                'priceCache',
                'priceCacheTimeout',
                'domSignals',
                'monitorTrendChart',
                'monitorTrendData',
                'monitorTrendDatasetMap',
                'monitorDomHistoryList',
                'monitorDomHistoryMeta',
                'candlestickSupported',
                'candlestickLoader',
                'luxonScriptUrl',
                'luxonLoader',
                'chartDateAdapterUrl',
                'dateAdapterReady',
                'dateAdapterLoader',
                'financialScriptUrl',
                'chartScriptableGuardApplied',
                'strategyChartInstance',
                'strategyChartFallbackNotified',
                'strategyChartState',
                'monitorChartRenderTimeout',
                'pendingMonitorChartRender',
                '_syncingCharts',
                'monitorModalFullscreen',
                '_monitorFullscreenChangeHandler'
            ];
        }

        createDefaultStrategyChartState() {
            return {
                initialized: false,
                currentStrategy: null,
                symbol: '',
                interval: '5 mins',
                duration: '1 D',
                fullBars: [],
                orders: [],
                visibleCount: 0,
                isRealtime: false,
                realtimeTimer: null,
                isReplaying: false,
                replayTimer: null,
                loadToken: null,
                loading: false,
                slEnabled: false,
                tpEnabled: false,
                currentSlPrice: null,
                currentTpPrice: null,
                useCandlestick: true,
            };
        }

        getStrategyChartState() {
            if (!this.strategyChartState) {
                this.strategyChartState = this.createDefaultStrategyChartState();
            }
            return this.strategyChartState;
        }

        getStrategyChartRefreshIntervalMs(intervalValue) {
            const key = normalizeIntervalKey(intervalValue || '');
            const minutes = INTERVAL_MINUTES_MAP[key] || 5;
            return Math.max(15000, minutes * 60000);
        }

        getStrategyChartTimeUnit(intervalValue) {
            const value = (intervalValue || '').toLowerCase();
            if (value.includes('day')) return 'day';
            if (value.includes('hour')) return 'hour';
            return 'minute';
        }

        refreshStrategyChartDuration(intervalValue, options = {}) {
            const selectEl = options.selectEl || document.getElementById('strategyChartDuration');
            if (!selectEl) return;
            const available = getMonitorDurationOptions(intervalValue);
            const preferred = options.preferredValue || selectEl.value || '1 D';
            selectEl.innerHTML = '';
            available.forEach((item, index) => {
                const option = document.createElement('option');
                option.value = item.value;
                option.textContent = item.label;
                if (index === 0 && !preferred) {
                    option.selected = true;
                }
                selectEl.appendChild(option);
            });
            const hasPreferred = preferred && available.some((item) => item.value === preferred);
            const fallback = available[0]?.value || '1 D';
            selectEl.value = hasPreferred ? preferred : fallback;
        }
    }

    monitoring.manager = monitoring.manager || {};
    monitoring.manager.MonitoringManager = MonitoringManager;
    root.MonitoringManager = MonitoringManager;
})(typeof window !== 'undefined' ? window : globalThis);
