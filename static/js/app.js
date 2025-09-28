const globalScope = typeof window !== 'undefined' ? window : globalThis;
const appModules = globalScope.appModules = globalScope.appModules || {};
const monitoringModules = appModules.monitoring || {};
const MonitoringConstants = monitoringModules.constants || {};
const MonitoringUtils = monitoringModules.utils || {};
const MonitoringManagerModule = monitoringModules.manager || {};
const MonitoringManagerClass = MonitoringManagerModule.MonitoringManager;
const SharedUtils = appModules.utils || {};
const uiModules = appModules.ui || {};
const UIControllerModule = uiModules.controller || {};
const UIControllerClass = UIControllerModule.UIController || globalScope.UIController;
const ordersModules = appModules.orders || {};
const OrdersManagerModule = ordersModules.manager || {};
const OrdersManagerClass = OrdersManagerModule.OrderManager || globalScope.OrderManager;
const notificationsModules = appModules.notifications || {};
const NotificationsManagerModule = notificationsModules.manager || {};
const NotificationsManagerClass = NotificationsManagerModule.NotificationsManager || globalScope.NotificationsManager;

const {
    INTERVAL_MINUTES_MAP = {},
    MONITOR_SOURCE_TZ = 'America/New_York',
    MONITOR_FALLBACK_TZ = 'Asia/Shanghai',
    MONITOR_CHART_MAX_MINUTES = 30,
    MONITOR_CHART_MAX_POINTS = 3000,
    MONITOR_DURATION_MINUTES_MAP = {},
    MONITOR_DURATION_OPTIONS = {},
    TREND_LEVEL_STYLES = {}
} = MonitoringConstants;

const fallbackNormalizeIntervalKey = (value) => {
    if (typeof value === 'string') {
        return value.replace(/\s+/g, '').toLowerCase();
    }
    if (typeof value === 'number') {
        return String(value);
    }
    return '';
};

const fallbackNormalizeDurationKey = (value) => {
    if (typeof value === 'string') {
        return value.replace(/\s+/g, '').toUpperCase();
    }
    if (typeof value === 'number') {
        return String(value);
    }
    return '';
};

const fallbackGetMonitorDurationMinutes = (durationValue) => {
    const normalized = fallbackNormalizeDurationKey(durationValue);
    if (!normalized) return null;

    if (Object.prototype.hasOwnProperty.call(MONITOR_DURATION_MINUTES_MAP, normalized)) {
        return MONITOR_DURATION_MINUTES_MAP[normalized];
    }

    const match = normalized.match(/^(\d+)([A-Z]+)$/);
    if (!match) {
        const numericValue = Number(normalized);
        return Number.isFinite(numericValue) && numericValue > 0 ? numericValue : null;
    }

    const value = Number(match[1]);
    const unit = match[2];
    if (!Number.isFinite(value) || value <= 0) {
        return null;
    }

    switch (unit) {
        case 'MIN':
        case 'MINS':
        case 'MINUTE':
        case 'MINUTES':
            return value;
        case 'M':
            return value <= 10 ? value * 43200 : value;
        case 'H':
        case 'HR':
        case 'HRS':
        case 'HOUR':
        case 'HOURS':
            return value * 60;
        case 'D':
        case 'DAY':
        case 'DAYS':
            return value * 1440;
        case 'W':
        case 'WK':
        case 'WKS':
        case 'WEEK':
        case 'WEEKS':
            return value * 10080;
        case 'MON':
        case 'MONTH':
        case 'MONTHS':
            return value * 43200;
        case 'Y':
        case 'YR':
        case 'YRS':
        case 'YEAR':
        case 'YEARS':
            return value * 525600;
        default:
            return null;
    }
};

const fallbackGetMonitorDurationOptions = (intervalValue) => {
    const intervalKey = typeof intervalValue === 'string'
        ? intervalValue.trim().toLowerCase()
        : '';
    return MONITOR_DURATION_OPTIONS[intervalKey] || MONITOR_DURATION_OPTIONS['1 min'] || [];
};

const fallbackRefreshMonitorDurationSelect = (intervalValue, options = {}) => {
    const selectEl = options.selectEl || document.getElementById('monitorDuration');
    if (!selectEl) return [];

    const availableOptions = fallbackGetMonitorDurationOptions(intervalValue);
    const previousValue = selectEl.value;
    const preferredValue = options.preferredValue;

    selectEl.innerHTML = '';
    availableOptions.forEach((item, index) => {
        const optionEl = document.createElement('option');
        optionEl.value = item.value;
        optionEl.textContent = item.label;
        if (index === 0 && !preferredValue && !previousValue) {
            optionEl.selected = true;
        }
        selectEl.appendChild(optionEl);
    });

    const hasPreferred = preferredValue && availableOptions.some((option) => option.value === preferredValue);
    const hasPrevious = availableOptions.some((option) => option.value === previousValue);
    const fallbackValue = availableOptions[0]?.value || '';

    if (hasPreferred) {
        selectEl.value = preferredValue;
    } else if (hasPrevious) {
        selectEl.value = previousValue;
    } else if (fallbackValue) {
        selectEl.value = fallbackValue;
    }

    const changed = selectEl.value !== previousValue;
    if (options.triggerChange && changed) {
        selectEl.dispatchEvent(new Event('change'));
    }

    return availableOptions;
};

const fallbackGetTrendLevelStyle = (level) => {
    const key = Number.isFinite(level) ? Number(level) : 0;
    return TREND_LEVEL_STYLES[key] || TREND_LEVEL_STYLES[0] || {};
};

const fallbackGetTrendSuggestionLabel = (suggestion, level) => {
    if (!suggestion) return '';
    const numericLevel = Number(level);
    const resolvedLevel = Number.isFinite(numericLevel)
        ? Math.min(4, Math.max(1, Math.round(numericLevel)))
        : 1;
    if (suggestion === 'buy') return `B${resolvedLevel}`;
    if (suggestion === 'sell') return `S${resolvedLevel}`;
    return '';
};

const fallbackTrendSignalLabelPlugin = {
    id: 'trendSignalLabel',
    afterDatasetsDraw(chart, args, opts) {
        const dataset = chart.data?.datasets?.[0];
        if (!dataset) return;
        const meta = chart.getDatasetMeta(0);
        if (!meta || !meta.data) return;
        const ctx = chart.ctx;
        const offset = typeof opts?.offset === 'number' ? opts.offset : 16;
        const font = opts?.font || 'bold 12px sans-serif';
        const buyColor = opts?.buyColor || '#16a34a';
        const sellColor = opts?.sellColor || '#dc2626';
        meta.data.forEach((element, index) => {
            if (!element) return;
            const raw = dataset.data?.[index];
            if (!raw || !raw.suggestion) return;
            const { x, y } = element.tooltipPosition();
            const level = raw.level ?? 0;
            const style = fallbackGetTrendLevelStyle(level);
            const text = fallbackGetTrendSuggestionLabel(raw.suggestion, raw.level)
                || (raw.suggestion === 'buy' ? 'Buy' : 'Sell');
            const resolvedOffset = style.labelOffset ?? offset;
            const yOffset = raw.suggestion === 'buy' ? -resolvedOffset : resolvedOffset;
            ctx.save();
            ctx.font = style.labelFont || font;
            const fillColor = raw.suggestion === 'buy'
                ? (style.labelBuyColor || style.buyColor || buyColor)
                : (style.labelSellColor || style.sellColor || sellColor);
            ctx.fillStyle = fillColor;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            if (style.shadowBlur) {
                ctx.shadowColor = style.shadowColor || fillColor;
                ctx.shadowBlur = style.shadowBlur;
            }
            if (style.outlineColor) {
                ctx.strokeStyle = style.outlineColor;
                ctx.lineWidth = style.outlineWidth || 1;
                ctx.strokeText(text, x, y + yOffset);
            }
            ctx.fillText(text, x, y + yOffset);
            ctx.restore();
        });
    }
};

const fallbackSlugifyStrategyName = (name) => (name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');

const {
    normalizeIntervalKey = fallbackNormalizeIntervalKey,
    normalizeDurationKey = fallbackNormalizeDurationKey,
    getMonitorDurationMinutes = fallbackGetMonitorDurationMinutes,
    getMonitorDurationOptions = fallbackGetMonitorDurationOptions,
    refreshMonitorDurationSelect = fallbackRefreshMonitorDurationSelect,
    getTrendLevelStyle = fallbackGetTrendLevelStyle,
    getTrendSuggestionLabel = fallbackGetTrendSuggestionLabel
} = MonitoringUtils;

const {
    TrendSignalLabelPlugin = fallbackTrendSignalLabelPlugin,
    slugifyStrategyName = fallbackSlugifyStrategyName
} = SharedUtils;

class TradingApp {
    constructor() {
        this.ws = null;
        this.connected = false;
        this.charts = {}; // Store chart instances
        this.executionsPagination = {
            currentPage: 1,
            pageSize: 20,
            totalItems: 0,
            allExecutions: []
        };
        this.cancellingOrders = new Set();
        this.currentStrategyDetails = null;
        this.internalStrategyOrders = [];
        this.strategyExecutionsCache = new Map();
        this.strategyExecutionsDefaultLimit = 20;
        // 缓存当日订单列表，供监控时查找最新建仓/加仓时间
        this.orders = [];
        // 通知列表
        this.notifications = [];

        this.monitoringManager = typeof MonitoringManagerClass === 'function'
            ? new MonitoringManagerClass(this)
            : null;
        this.monitoring = this.monitoringManager;
        if (this.monitoringManager) {
            this.bindMonitoringStateProxies();
        } else {
            this.initializeMonitoringStateFallback();
        }
        this.strategyDetailsState = this.createEmptyStrategyDetailsState();
        this.strategyDetailsTabListenerBound = false;
        this.authRole = localStorage.getItem('user_role') || 'admin';
        this.isReadOnly = localStorage.getItem('is_read_only') === '1';
        this.lastReadOnlyWarningAt = 0;
        this.riskSettingsInitialized = false;
        this.currentRiskSettings = null;
        this.riskSettingsSaving = false;

        // Internal strategy summary helpers
        this.summaryControlsInitialized = false;
        this.summaryPrimarySaving = false;
        this.summaryPrimaryCurrent = null;
        this.summaryDataFeedCurrent = null;
        this.wsHandlers = new Map();
        this.riskRulesManager = new RiskRulesManager(this);
        this.strategiesManager = new StrategiesManager(this);
        this.bindStrategiesStateProxies();
        this.registerWebSocketHandler('strategy_performance', (payload) => this.handleStrategyPerformanceMessage(payload));
        this.registerWebSocketHandler('strategies_snapshot', (payload) => this.handleStrategiesSnapshot(payload));
        this.registerWebSocketHandler('strategy_status', (payload) => this.handleStrategyStatusMessage(payload));
        this.registerWebSocketHandler('sizing_history', (payload) => this.handleSizingHistorySnapshot(payload));

        this.sidePanelManagerInitialized = false;
        this.panelLoadingStates = new Map();

        // 初始化登录管理器
        this.loginManager = new LoginManager(this);
        this.ibkrManager = new IBKRConnectionManager(this);
        this.dataManager = new IBKRDataManager(this);
        this.orderManager = typeof OrdersManagerClass === 'function'
            ? new OrdersManagerClass(this)
            : new OrderManager(this);
        this.portfolioManager = new PortfolioManager(this);
        this.logsManager = new LogsManager(
            this.apiCall.bind(this),
            this.showAlert.bind(this),
            this.formatNumber.bind(this)
        );
        this.notificationsManager = typeof NotificationsManagerClass === 'function'
            ? new NotificationsManagerClass(this)
            : new NotificationsManager(this);
        this.uiController = typeof UIControllerClass === 'function'
            ? new UIControllerClass(this)
            : null;
        this.domManager = new DOMManager(this);

        this.guardChartScriptableDescriptors();
        this.init();
    }

    bindMonitoringStateProxies() {
        const manager = this.monitoringManager;
        if (!manager || typeof manager.getManagedProperties !== 'function') {
            return;
        }
        const properties = manager.getManagedProperties();
        properties.forEach((prop) => {
            if (Object.prototype.hasOwnProperty.call(this, prop)) {
                delete this[prop];
            }
            Object.defineProperty(this, prop, {
                configurable: true,
                enumerable: true,
                get: () => manager[prop],
                set: (value) => {
                    manager[prop] = value;
                }
            });
        });
    }

    bindStrategiesStateProxies() {
        const manager = this.strategiesManager;
        if (!manager || typeof manager.getManagedProperties !== 'function') {
            return;
        }
        const properties = manager.getManagedProperties();
        properties.forEach((prop) => {
            if (Object.prototype.hasOwnProperty.call(this, prop)) {
                delete this[prop];
            }
            Object.defineProperty(this, prop, {
                configurable: true,
                enumerable: false,
                get: () => manager[prop],
                set: (value) => {
                    manager[prop] = value;
                }
            });
        });
    }

    initializeMonitoringStateFallback() {
        // 当前订阅实时行情的合约符号，null 表示未订阅
        this.monitorRealtimeSymbol = null;
        // 记录用户是否希望在重新打开时自动恢复实时行情
        this.monitorRealtimeShouldResume = false;
        this.monitorRealtimeResumeSymbol = null;
        this.monitorChartTimezone = null;
        // 价格缓存机制
        this.priceCache = new Map();
        this.priceCacheTimeout = 30000; // 30秒缓存
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
        // 渲染监控图表时使用的定时器句柄，防止重复渲染导致死循环
        this.monitorChartRenderTimeout = null;
        // 缓存待渲染的图表回调，确保快速连续请求只触发一次绘制
        this.pendingMonitorChartRender = null;
        // 图表联动时的递归保护开关
        this._syncingCharts = false;
        // 监控弹窗全屏状态
        this.monitorModalFullscreen = false;
        this._monitorFullscreenChangeHandler = null;
    }

    /**
     * 向后兼容：保持原有的日志加载接口
     * 迁移到 LogsManager 后仍然暴露同名方法，避免未更新的调用出现异常
     */
    loadLogs(...args) {
        if (this.logsManager && typeof this.logsManager.loadLogs === 'function') {
            return this.logsManager.loadLogs(...args);
        }
        return Promise.resolve();
    }

    loadLogModules(...args) {
        if (this.logsManager && typeof this.logsManager.loadLogModules === 'function') {
            return this.logsManager.loadLogModules(...args);
        }
        return Promise.resolve();
    }

    showLogStats(...args) {
        if (this.logsManager && typeof this.logsManager.showLogStats === 'function') {
            return this.logsManager.showLogStats(...args);
        }
        return Promise.resolve();
    }

    async ensureDefaultConnectionConfig(options = {}) {
        if (this.ibkrManager && typeof this.ibkrManager.ensureDefaultConnectionConfig === 'function') {
            return this.ibkrManager.ensureDefaultConnectionConfig(options);
        }
        return undefined;
    }

    registerWebSocketHandler(type, handler) {
        if (!type || typeof handler !== 'function') {
            return () => {};
        }
        if (!this.wsHandlers.has(type)) {
            this.wsHandlers.set(type, new Set());
        }
        const handlers = this.wsHandlers.get(type);
        handlers.add(handler);
        return () => {
            this.unregisterWebSocketHandler(type, handler);
        };
    }

    unregisterWebSocketHandler(type, handler) {
        const handlers = this.wsHandlers.get(type);
        if (!handlers) return;
        handlers.delete(handler);
        if (handlers.size === 0) {
            this.wsHandlers.delete(type);
        }
    }

    updatePanelLoadingIndicator(panelKey) {
        const key = String(panelKey);
        const indicator = document.querySelector(`[data-panel-spinner="${key}"]`);
        if (!indicator) return;
        const count = this.panelLoadingStates.get(key) || 0;
        if (count > 0) {
            indicator.classList.remove('d-none');
            indicator.setAttribute('aria-hidden', 'false');
        } else {
            indicator.classList.add('d-none');
            indicator.setAttribute('aria-hidden', 'true');
        }
    }

    setPanelLoading(panelKey, isLoading, options = {}) {
        if (!panelKey) return 0;
        const key = String(panelKey);
        const force = Boolean(options.force);
        let count = this.panelLoadingStates.get(key) || 0;
        if (force) {
            count = 0;
        }
        if (isLoading) {
            count += 1;
        } else if (count > 0) {
            count -= 1;
        } else {
            count = 0;
        }
        this.panelLoadingStates.set(key, count);
        this.updatePanelLoadingIndicator(key);
        return count;
    }

    resetPanelLoading(panelKey) {
        return this.setPanelLoading(panelKey, false, { force: true });
    }

    handleWebSocketPayload(payload) {
        if (!payload || !payload.type) return false;
        const handlers = this.wsHandlers.get(payload.type);
        if (!handlers || handlers.size === 0) {
            return false;
        }
        let handled = false;
        [...handlers].forEach(handler => {
            try {
                const result = handler(payload);
                if (result) handled = true;
            } catch (error) {
                console.error('WebSocket handler error:', payload.type, error);
            }
        });
        return handled;
    }

    handleStrategyPerformanceMessage(message) {
        if (!this.strategiesManager || typeof this.strategiesManager.handleStrategyPerformanceMessage !== 'function') {
            return false;
        }
        return this.strategiesManager.handleStrategyPerformanceMessage(message);
    }

    handleRiskRulesMessage(payload) {
        if (!payload || payload.type !== 'risk_rules') return false;
        if (this.riskRulesManager && typeof this.riskRulesManager.applySnapshot === 'function') {
            return this.riskRulesManager.applySnapshot(payload);
        }
        return false;
    }

    handleStrategiesSnapshot(payload) {
        if (!this.strategiesManager?.handleStrategiesSnapshot) return false;
        return this.strategiesManager.handleStrategiesSnapshot(payload);
    }

    handleSizingHistorySnapshot(payload) {
        if (!this.strategiesManager?.handleSizingHistorySnapshot) return false;
        return this.strategiesManager.handleSizingHistorySnapshot(payload);
    }

    renderSizingHistory(history = []) {
        if (!this.strategiesManager?.renderSizingHistory) return;
        this.strategiesManager.renderSizingHistory(history);
    }

    normalizeRuleStatus(status) {
        if (!status || typeof status !== 'object') {
            return { passed: true, message: '正常' };
        }
        const passed = status.passed !== undefined ? !!status.passed : true;
        let message = status.message;
        if (!message) {
            const errors = Array.isArray(status.errors) ? status.errors : [];
            if (errors.length > 0) {
                message = errors.map((err) => {
                    if (typeof err === 'string') return err;
                    try {
                        return JSON.stringify(err);
                    } catch (_) {
                        return String(err);
                    }
                }).join(' | ');
            }
        }
        if (!message) {
            message = passed ? '正常' : '违规';
        }
        return { passed, message };
    }

    applySizingRuleStatusesFromHistory(latestRecord) {
        if (!this.strategiesManager?.applySizingRuleStatusesFromHistory) return;
        this.strategiesManager.applySizingRuleStatusesFromHistory(latestRecord);
    }

    handleStrategyStatusMessage(payload) {
        if (!this.strategiesManager?.handleStrategyStatusMessage) return false;
        return this.strategiesManager.handleStrategyStatusMessage(payload);
    }

    applyStrategyStatusPayload(payload) {
        if (!this.strategiesManager?.applyStrategyStatusPayload) return false;
        return this.strategiesManager.applyStrategyStatusPayload(payload);
    }

    addDomSignal(signal) {
        this.domSignals.push(signal);
        if (this.domSignals.length > 1000) this.domSignals.shift();
        this.updateDomSignalAnnotations();
    }

    ensureMonitorAnnotations() {
        if (!this.monitorChart || !this.monitorChart.options) return null;

        const options = this.monitorChart.options;
        let plugins = options.plugins;
        if (!plugins) {
            options.plugins = {};
            plugins = options.plugins;
        }

        let annotationConfig = plugins.annotation;
        if (!annotationConfig) {
            plugins.annotation = { annotations: {} };
            annotationConfig = plugins.annotation;
        }

        if (!annotationConfig.annotations) {
            annotationConfig.annotations = {};
        }

        return annotationConfig.annotations;
    }

    updateDomSignalAnnotations() {
        if (!this.monitorChart || this.monitorChart.config.type !== 'candlestick') return;
        const annotations = this.ensureMonitorAnnotations();
        if (!annotations) return;
        Object.keys(annotations).filter(k => k.startsWith('domSignal')).forEach(k => delete annotations[k]);
        const sizeMap = {1: 12, 2: 16, 3: 20, 4: 24};
        this.domSignals.forEach((sig, idx) => {
            const content = sig.score > 0 ? '↑' : '↓';
            const color = sig.score > 0 ? 'green' : 'red';
            annotations[`domSignal${idx}`] = {
                type: 'label',
                xValue: sig.time,
                yValue: sig.price,
                backgroundColor: 'rgba(0,0,0,0)',
                color,
                font: {size: sizeMap[sig.level] || 12, weight: 'bold'},
                content,
                yAdjust: sig.score > 0 ? 10 : -10
            };
        });
        this.monitorChart.update('none');
    }

    setMonitorTrendHistory(points = []) {
        if (this.monitoringManager?.setMonitorTrendHistory) {
            return this.monitoringManager.setMonitorTrendHistory(points);
        }
        return undefined;
    }

    resetMonitorTrendData() {
        if (this.monitoringManager?.resetMonitorTrendData) {
            return this.monitoringManager.resetMonitorTrendData();
        }
        return undefined;
    }

    formatTrendPointForChart(point) {
        if (this.monitoringManager?.formatTrendPointForChart) {
            return this.monitoringManager.formatTrendPointForChart(point);
        }
        return null;
    }

    getFormattedTrendChartData() {
        if (this.monitoringManager?.getFormattedTrendChartData) {
            return this.monitoringManager.getFormattedTrendChartData();
        }
        return [];
    }

    updateMonitorSignalOverlayDatasets() {
        if (this.monitoringManager?.updateMonitorSignalOverlayDatasets) {
            return this.monitoringManager.updateMonitorSignalOverlayDatasets();
        }
    }

    renderMonitorSignalHistory() {
        if (this.monitoringManager?.renderMonitorSignalHistory) {
            return this.monitoringManager.renderMonitorSignalHistory();
        }
    }

    getTrendValues(chart) {
        if (this.monitoringManager?.getTrendValues) {
            return this.monitoringManager.getTrendValues(chart);
        }
        return null;
    }

    fmt(v, digits = 2) {
        return Number.isFinite(v) ? v.toFixed(digits) : String(v);
    }

    formatRuntimeDuration(seconds) {
        if (!Number.isFinite(seconds)) return '-';
        const safeSeconds = Math.max(0, seconds);
        let value = safeSeconds;
        let unit = 's';
        if (safeSeconds >= 86400) {
            value = safeSeconds / 86400;
            unit = 'd';
        } else if (safeSeconds >= 3600) {
            value = safeSeconds / 3600;
            unit = 'h';
        } else if (safeSeconds >= 60) {
            value = safeSeconds / 60;
            unit = 'min';
        }
        const decimals = value >= 100 ? 0 : value >= 10 ? 1 : 2;
        const formattedValue = parseFloat(value.toFixed(decimals));
        return `${formattedValue} ${unit}`;
    }


    // 仅在创建 chart 后、首轮配置时调用一次
    setupTrendLegendAndAnnotations(chart) {
        if (this.monitoringManager?.setupTrendLegendAndAnnotations) {
            return this.monitoringManager.setupTrendLegendAndAnnotations(chart);
        }
    }

    // 需要时手动调用；只在数值变化时更新 yMin/yMax，避免触发外层 watch 循环
    refreshTrend(chart) {
        if (this.monitoringManager?.refreshTrend) {
            return this.monitoringManager.refreshTrend(chart);
        }
    }


    syncTrendReferenceLines() {
        if (this.monitoringManager?.syncTrendReferenceLines) {
            return this.monitoringManager.syncTrendReferenceLines();
        }
    }

    deriveTrendLevel(score) {
        if (this.monitoringManager?.deriveTrendLevel) {
            return this.monitoringManager.deriveTrendLevel(score);
        }
        return 0;
    }

    addMonitorTrendPoint(time, score, options = {}) {
        if (this.monitoringManager?.addMonitorTrendPoint) {
            return this.monitoringManager.addMonitorTrendPoint(time, score, options);
        }
        return undefined;
    }

    syncCharts(source, target) {
        if (!source || !target) return;
        if (this._syncingCharts) return;

        this._syncingCharts = true;

        try {
            const isMonitorTrendPair =
                (source === this.monitorChart && target === this.monitorTrendChart) ||
                (source === this.monitorTrendChart && target === this.monitorChart);

            if (!isMonitorTrendPair) {
                const srcScale = source.scales?.x;
                const targetScale = target.options?.scales?.x;
                if (srcScale && targetScale) {
                    targetScale.min = srcScale.min;
                    targetScale.max = srcScale.max;
                }
                target.update('none');
            }
            if (source === this.monitorChart || target === this.monitorChart) {
                this.updateDomSignalAnnotations();
            }
        } finally {
            this._syncingCharts = false;
        }
    }

    normalizeMonitorTime(value) {
        try {
            if (value == null) return null;
            if (value instanceof Date) return value.getTime();
            if (typeof value === 'number') {
                return Number.isFinite(value) ? value : null;
            }
            const parsed = this.parseMonitorChartTime(value);
            if (parsed && !Number.isNaN(parsed.getTime())) {
                return parsed.getTime();
            }
            return null;
        } catch (e) {
            return null;
        }
    }

    getMonitorDisplayTimes() {
        const times = this.currentMonitor?.displayTimes;
        return Array.isArray(times) ? times : [];
    }

    getMonitorDisplayLabels() {
        const labels = this.currentMonitor?.displayLabels;
        return Array.isArray(labels) ? labels : [];
    }

    extractRangeFromMonitorChart() {
        if (!this.monitorChart || !this.monitorChart.scales?.x) return null;
        const chartType = this.currentMonitor?.chartType || this.monitorChart.config?.type;
        const times = this.getMonitorDisplayTimes();
        if (!times.length) return null;

        if (chartType === 'candlestick') {
            const min = this.normalizeMonitorTime(this.monitorChart.scales.x.min);
            const max = this.normalizeMonitorTime(this.monitorChart.scales.x.max);
            if (min == null || max == null) return null;
            return { min, max };
        }

        const labels = this.getMonitorDisplayLabels();
        if (!labels.length || labels.length !== times.length) return null;

        const toIndex = (value, fallback) => {
            if (typeof value === 'number' && Number.isFinite(value)) {
                return Math.round(value);
            }
            if (typeof value === 'string') {
                const idx = labels.indexOf(value);
                if (idx !== -1) return idx;
            }
            return fallback;
        };

        const minIdx = Math.max(0, Math.min(times.length - 1, toIndex(this.monitorChart.scales.x.min, 0)));
        const maxIdx = Math.max(minIdx, Math.min(times.length - 1, toIndex(this.monitorChart.scales.x.max, times.length - 1)));

        const minTime = this.normalizeMonitorTime(times[minIdx]);
        const maxTime = this.normalizeMonitorTime(times[maxIdx]);
        if (minTime == null || maxTime == null) return null;
        return { min: minTime, max: maxTime };
    }

    // Detect security type based on symbol (simple heuristic for common cases)
    detectSecurityType(symbol) {
        try {
            if (!symbol) return null;
            const s = String(symbol).trim().toUpperCase();
            // Common futures roots and FX futures roots
            const FUT_ROOTS = new Set(['MNQ','NQ','ES','MES','YM','MYM','RTY','M2K','VX','CL','NG','GC','SI','HG','ZB','ZN','ZF','ZT','6E','6J','6B','6A','6C','6S']);
            // Root only (e.g., MNQ), or root+month code like MNQZ4 / MNQZ24
            if (FUT_ROOTS.has(s) || /^[A-Z]{1,3}[FGHJKMNQUVXZ][0-9]$/.test(s) || /^[A-Z]{1,3}[FGHJKMNQUVXZ][0-9]{2}$/.test(s)) {
                return 'FUT';
            }
            // Default to stock
            return 'STK';
        } catch (e) {
            return null;
        }
    }

    getContractMultiplier(symbol) {
        try {
            if (!symbol) return 1;
            const raw = String(symbol).trim().toUpperCase();
            if (!raw) return 1;

            const FUTURES_MULTIPLIERS = {
                MNQ: 2,
                NQ: 20,
                MES: 5,
                ES: 50,
                MYM: 0.5,
                YM: 5,
                M2K: 5,
                RTY: 50,
                MCL: 100,
                CL: 1000,
                MGC: 10,
                GC: 100,
            };

            if (FUTURES_MULTIPLIERS[raw] != null) {
                return FUTURES_MULTIPLIERS[raw];
            }

            const futMatch = raw.match(/^([A-Z]{1,3})([FGHJKMNQUVXZ])([0-9]{1,2})$/);
            const base = futMatch ? futMatch[1] : raw;
            return FUTURES_MULTIPLIERS[base] != null ? FUTURES_MULTIPLIERS[base] : 1;
        } catch (e) {
            return 1;
        }
    }

    async init() {
        try {
            if (window.Chart && window.Chart.register) {
                if (!window.ChartAnnotation && window['chartjs-plugin-annotation']) {
                    window.ChartAnnotation = window['chartjs-plugin-annotation'];
                }
                if (!window.zoomPlugin && window['chartjs-plugin-zoom']) {
                    window.zoomPlugin = window['chartjs-plugin-zoom'];
                }
                if (window.ChartAnnotation) {
                    window.Chart.register(window.ChartAnnotation);
                }
                if (window.zoomPlugin) {
                    window.Chart.register(window.zoomPlugin);
                }

                this.registerCandlestickControllers();
                if (!this.candlestickSupported) {
                    this.ensureCandlestickPlugin().catch((err) => {
                        console.warn('[Monitor] candlestick plugin load deferred:', err?.message || err);
                    });
                }
            }
        } catch (e) {
            console.error('Failed to register Chart.js plugins', e);
        }
        this.setupEventListeners();
        this.initSymbolCombobox();
        this.ibkrManager.connectWebSocket();
        this.scheduleTradingReminders();
        
        // 延迟执行所有网络请求，避免阻塞首页渲染
        setTimeout(async () => {
            // 优化：并行初始化，降低关键接口超时与重试，避免串行等待叠加
            const cfgPromise = this.ibkrManager.ensureDefaultConnectionConfig({ timeout: 3000, maxRetries: 0 });
            // 不阻塞首屏：自动登录与自动连接在后台进行
            void this.loginManager.tryAutoLogin({ timeout: 7000, maxRetries: 0, connectTimeout: 10000, connectRetries: 0 });
            await this.ibkrManager.checkConnectionStatus({ timeout: 4000, maxRetries: 0 });
            // 加载账户和投资组合数据
            // this.dataManager.loadAccountInfo(); // 改为由 WebSocket account_update 推送
            // this.portfolioManager.loadPortfolio(); // 移到登录成功后调用
            this.strategiesManager.loadStrategies();
            this.logsManager.init();
            // 初始化通知管理器
            this.notificationsManager.init();
            // 加载配置和状态
            this.loadGSRulesStatus();
            // 加载风险规则
            this.riskRulesManager.loadRiskRules();
            // 已改为WebSocket推送更新订单，取消轮询刷新
            await cfgPromise.catch(() => {});
        }, 50);
    }

    setupEventListeners() {
        // Wait for DOM to be ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.bindEventListeners());
        } else {
            this.bindEventListeners();
        }
    }

    bindEventListeners() {
        // Connection events are now handled by IBKRConnectionManager

        // Login events are now handled by LoginManager

        this.initSidePanels();

        this.monitorDomHistoryList = document.getElementById('monitorDomHistoryList');
        this.monitorDomHistoryMeta = document.getElementById('monitorDomHistoryMeta');

        // DOM 已就绪时同步访客模式下的界面元素状态，避免初始渲染遗漏
        this.updateReadOnlyMode(this.isReadOnly, this.authRole, { persist: false });

        const summaryRange = document.getElementById('dailySummaryRange');
        if (summaryRange) {
            summaryRange.addEventListener('change', () => {
                const days = parseInt(summaryRange.value, 10) || 30;
                this.dataManager.loadDailyAccountSummary({ days, showLoading: true });
            });
        }
        const refreshDailySummaryBtn = document.getElementById('refreshDailySummaryBtn');
        if (refreshDailySummaryBtn) {
            refreshDailySummaryBtn.addEventListener('click', () => {
                const rangeInput = document.getElementById('dailySummaryRange');
                const days = parseInt(rangeInput?.value || '30', 10) || 30;
                this.dataManager.loadDailyAccountSummary({ days, force: true, showLoading: true });
            });
        }
        const dailySummaryModal = document.getElementById('dailyAccountSummaryModal');
        if (dailySummaryModal) {
            dailySummaryModal.addEventListener('shown.bs.modal', () => {
                const rangeInput = document.getElementById('dailySummaryRange');
                const days = parseInt(rangeInput?.value || '30', 10) || 30;
                this.dataManager.loadDailyAccountSummary({ days, force: true, showLoading: true });
            });
        }

        // Config Rules Settings buttons
        const gRulesSettingsBtn = document.getElementById('gRulesSettingsBtn');
        if (gRulesSettingsBtn) {
            gRulesSettingsBtn.addEventListener('click', () => this.showConfigModal('g-rules'));
        }
        const sRulesSettingsBtn = document.getElementById('sRulesSettingsBtn');
        if (sRulesSettingsBtn) {
            sRulesSettingsBtn.addEventListener('click', () => this.showConfigModal('s-rules'));
        }

        // Position Sizing Method change event
        const positionMethodSelect = document.getElementById('s0PositionSizing');
        if (positionMethodSelect) {
            positionMethodSelect.addEventListener('change', () => this.updatePositionValueField());
        }

        // Strategy and Logs buttons
        const strategyBtn = document.getElementById('strategyBtn');
        if (strategyBtn) {
            strategyBtn.addEventListener('click', () => {
                this.strategiesManager.loadStrategies();
                const panel = document.getElementById('strategiesTable');
                if (panel) {
                    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            });
        }
        // Logs search and filter controls
        const searchLogsBtn = document.getElementById('searchLogsBtn');
        if (searchLogsBtn) searchLogsBtn.addEventListener('click', () => this.logsManager.loadLogs());

        const logModule = document.getElementById('logModule');
        if (logModule) logModule.addEventListener('change', () => this.logsManager.loadLogs());

        const logRange = document.getElementById('logRange');
        if (logRange) logRange.addEventListener('change', () => {
            this.loadLogModules();
            this.logsManager.loadLogs();
        });

        const logLevel = document.getElementById('logLevel');
        if (logLevel) logLevel.addEventListener('change', () => this.logsManager.loadLogs());

        const logStatsBtn = document.getElementById('logStatsBtn');
        if (logStatsBtn) logStatsBtn.addEventListener('click', () => this.showLogStats());
        // Tab switching event listeners
        const sRulesTab = document.getElementById('s-rules-tab');
        if (sRulesTab) {
            sRulesTab.addEventListener('click', () => {
                this.riskRulesManager.loadRiskRules();
                this.loadSizingHistory();
            });
        }
        
        // Suggested Position Size event listeners
        const applySuggestedQuantityBtn = document.getElementById('applySuggestedQuantity');
        if (applySuggestedQuantityBtn) {
            applySuggestedQuantityBtn.addEventListener('click', () => this.applySuggestedQuantity());
        }
        
        const refreshSuggestionBtn = document.getElementById('refreshSuggestion');
        if (refreshSuggestionBtn) {
            refreshSuggestionBtn.addEventListener('click', async () => {
                const icon = refreshSuggestionBtn.querySelector('i');
                refreshSuggestionBtn.disabled = true;
                if (icon) {
                    icon.classList.add('fa-spin');
                }
                try {
                    await this.refreshPositionSuggestion();
                } finally {
                    refreshSuggestionBtn.disabled = false;
                    if (icon) {
                        icon.classList.remove('fa-spin');
                    }
                }
            });
        }

        const orderSymbol = document.getElementById('orderSymbol');
        const orderAction = document.getElementById('orderAction');
        const orderPrice = document.getElementById('orderPrice');
        if (orderSymbol) {
            orderSymbol.addEventListener('input', () => {
                const symbol = orderSymbol.value.toUpperCase().trim();
                // Recalculate risk prices whenever symbol changes
                this.updateRiskPrices().catch(console.error);

                if (!symbol) {
                    const priceDisplay = document.getElementById('currentPriceDisplay');
                    if (priceDisplay) {
                        priceDisplay.style.display = 'none';
                    }
                }

                this.clearPositionSuggestion();
            });
        }
        if (orderAction) {
            orderAction.addEventListener('change', () => {
                // Update risk prices when action changes
                this.updateRiskPrices().catch(console.error);

                this.clearPositionSuggestion();
            });
        }
        if (orderPrice) {
            orderPrice.addEventListener('input', () => {
                // Update risk prices when price changes
                this.updateRiskPrices().catch(console.error);

                this.clearPositionSuggestion();
            });
        }

        // WebSocket updates logs and strategies automatically
        const addStrategyBtn = document.getElementById('addStrategyBtn');
        if (addStrategyBtn) {
            addStrategyBtn.addEventListener('click', async () => {
                // Open Add Strategy modal instead of prompts
                const nameInput = document.getElementById('strategyNameInput');
                const typeSelect = document.getElementById('strategyTypeSelect');
                const descInput = document.getElementById('strategyDescriptionInput');
                const useTemplateGroup = document.getElementById('useTemplateGroup');
                const useTemplateCheckbox = document.getElementById('strategyUseTemplate');
                const fileGroup = document.getElementById('strategyFileGroup');
                const fileSelect = document.getElementById('strategyFileSelect');
                const slTpGroup = document.getElementById('slTpGroup');
                const subscriptionGroup = document.getElementById('subscriptionGroup');
                const positionTimingGroup = document.getElementById('positionTimingGroup');
                const dataSubscriptionSelect = document.getElementById('dataSubscriptionSelect');
                const addCooldownInput = document.getElementById('positionAddCooldownInput');
                const closeBeforeInput = document.getElementById('closeBeforeCloseInput');
                const filenamePreview = document.getElementById('strategyFilenamePreview');
                if (!nameInput || !typeSelect || !descInput) return;
                // reset form
                nameInput.value = '';
                if (filenamePreview) filenamePreview.textContent = '';
                typeSelect.value = 'external';
                descInput.value = '';
                if (useTemplateCheckbox) useTemplateCheckbox.checked = true;
                if (useTemplateGroup) useTemplateGroup.style.display = 'none';
                if (fileGroup) fileGroup.style.display = 'none';
                if (slTpGroup) slTpGroup.style.display = 'none';
                if (subscriptionGroup) subscriptionGroup.style.display = 'none';
                if (positionTimingGroup) positionTimingGroup.style.display = 'none';
                if (dataSubscriptionSelect) dataSubscriptionSelect.value = 'market_data';
                if (addCooldownInput) addCooldownInput.value = '';
                if (closeBeforeInput) closeBeforeInput.value = '';
                if (fileSelect) fileSelect.innerHTML = '';
                const modal = new bootstrap.Modal(document.getElementById('addStrategyModal'));
                modal.show();
            });
        }

        // Add Strategy type toggle and submit
        const strategyTypeSelect = document.getElementById('strategyTypeSelect');
        if (strategyTypeSelect) {
            strategyTypeSelect.addEventListener('change', async (e) => {
                const useTemplateGroup = document.getElementById('useTemplateGroup');
                const fileGroup = document.getElementById('strategyFileGroup');
                const fileSelect = document.getElementById('strategyFileSelect');
                const slTpGroup = document.getElementById('slTpGroup');
                const subscriptionGroup = document.getElementById('subscriptionGroup');
                const positionTimingGroup = document.getElementById('positionTimingGroup');
                const isInternal = e.target.value === 'internal';
                if (useTemplateGroup) useTemplateGroup.style.display = isInternal ? '' : 'none';
                if (fileGroup) fileGroup.style.display = isInternal ? '' : 'none';
                if (slTpGroup) slTpGroup.style.display = isInternal ? '' : 'none';
                if (subscriptionGroup) subscriptionGroup.style.display = isInternal ? '' : 'none';
                if (positionTimingGroup) positionTimingGroup.style.display = isInternal ? '' : 'none';
                if (isInternal && fileSelect) {
                    try {
                        const data = await this.apiCall('/strategy/files');
                        if (data && data.success) {
                            const files = data.files || [];
                            let html = '<option value="new">[New File]</option>';
                            for (const f of files) { html += `<option value="${f}">${f}</option>`; }
                            fileSelect.innerHTML = html;
                        }
                    } catch (err) { console.warn('Failed to load strategy files', err); }
                }
            });
        }
        const createStrategyBtn = document.getElementById('createStrategyBtn');
        if (createStrategyBtn) {
            createStrategyBtn.addEventListener('click', async () => {
                const nameInput = document.getElementById('strategyNameInput');
                const nameRaw = (nameInput?.value || '').trim();
                const strategy_type = document.getElementById('strategyTypeSelect')?.value || 'external';
                const description = document.getElementById('strategyDescriptionInput')?.value || '';
                const use_template = strategy_type === 'internal' ? !!document.getElementById('strategyUseTemplate')?.checked : false;
                const selected_file = strategy_type === 'internal' ? (document.getElementById('strategyFileSelect')?.value || 'new') : undefined;
                const sl_enabled = strategy_type === 'internal' ? !!document.getElementById('slEnabledSwitch')?.checked : false;
                const tp_enabled = strategy_type === 'internal' ? !!document.getElementById('tpEnabledSwitch')?.checked : false;
                const sl_price_val = document.getElementById('slPriceInput')?.value;
                const tp_price_val = document.getElementById('tpPriceInput')?.value;
                const sl_price = sl_enabled && sl_price_val !== '' ? parseFloat(sl_price_val) : undefined;
                const tp_price = tp_enabled && tp_price_val !== '' ? parseFloat(tp_price_val) : undefined;
                const sl_type = document.getElementById('slTypeSelect')?.value || 'amount';
                const tp_type = document.getElementById('tpTypeSelect')?.value || 'amount';
                const data_subscription = strategy_type === 'internal' ? (document.getElementById('dataSubscriptionSelect')?.value || '') : undefined;
                const cooldown_raw = document.getElementById('positionAddCooldownInput')?.value;
                const close_raw = document.getElementById('closeBeforeCloseInput')?.value;
                const cooldown_minutes = cooldown_raw !== undefined && cooldown_raw !== '' ? parseInt(cooldown_raw, 10) : null;
                const close_minutes = close_raw !== undefined && close_raw !== '' ? parseInt(close_raw, 10) : null;
                let name = nameRaw;
                if (strategy_type === 'internal' && (!name || name.length === 0)) {
                    const sel = selected_file || 'new';
                    if (sel && sel !== 'new') {
                        name = sel.replace(/\.py$/i, '');
                        if (nameInput) nameInput.value = name;
                    }
                }
                if (!name) { this.showAlert('danger', 'Please enter strategy name'); return; }
                try {
                    const payload = { name, strategy_type, description };
                    if (strategy_type === 'internal') {
                        Object.assign(payload, { use_template, selected_file, sl_enabled, tp_enabled, sl_price, tp_price, sl_type, tp_type });
                        if (data_subscription) payload.data_subscription = data_subscription;
                        if (Number.isFinite(cooldown_minutes) && cooldown_minutes > 0) payload.position_add_cooldown_minutes = cooldown_minutes;
                        if (Number.isFinite(close_minutes) && close_minutes > 0) payload.close_before_close_minutes = close_minutes;
                    }
                    await this.apiCall('/strategies', { method: 'POST', body: JSON.stringify(payload) });
                    this.showAlert('success', `Strategy ${name} created`);
                    const modalEl = document.getElementById('addStrategyModal');
                    const modal = bootstrap.Modal.getInstance(modalEl);
                    if (modal) modal.hide();
                    this.strategiesManager.loadStrategies();
                } catch (e) {}
            });
        }
        
        // Strategy Settings button
        const strategySettingsBtn = document.getElementById('strategySettingsBtn');
        if (strategySettingsBtn) {
            strategySettingsBtn.addEventListener('click', async () => {
                await this.strategiesManager.loadStrategySettings();
                const modal = new bootstrap.Modal(document.getElementById('strategySettingsModal'));
                modal.show();
            });
        }
        const saveStrategySettingsBtn = document.getElementById('saveStrategySettingsBtn');
        if (saveStrategySettingsBtn) {
            saveStrategySettingsBtn.addEventListener('click', async () => {
                await this.strategiesManager.saveStrategySettings();
            });
        }
        const generateStrategyTokenBtn = document.getElementById('generateStrategyTokenBtn');
        if (generateStrategyTokenBtn) {
            generateStrategyTokenBtn.addEventListener('click', async () => {
                try {
                    const data = await this.apiCall('/strategy/settings/generate-token', { method: 'POST' });
                    if (data.success && data.token) {
                        document.getElementById('strategyAuthToken').value = data.token;
                        this.showAlert('success', 'Token generated');
                    } else {
                        this.showAlert('danger', 'Failed to generate token');
                    }
                } catch (err) {
                    this.showAlert('danger', 'Failed to generate token');
                }
            });
        }

        const nameInput = document.getElementById('strategyNameInput');
        const filenamePreview = document.getElementById('strategyFilenamePreview');
        if (nameInput && filenamePreview) {
            nameInput.addEventListener('input', () => {
                const sanitized = slugifyStrategyName(nameInput.value);
                filenamePreview.textContent = sanitized ? `${sanitized}.py` : '';
            });
        }

        // Place order button
        document.getElementById('placeOrderBtn').addEventListener('click', () => {
            // Reset order form to default state
            document.getElementById('orderType').value = 'MARKET';
            document.getElementById('orderPrice').value = '';
            document.getElementById('orderPrice').disabled = true; // Market order by default
            document.getElementById('riskRuleSection').style.display = 'block'; // Always show SL/TP section
            
            // Clear SL/TP inputs
            document.getElementById('stopLossPrice').value = '';
            document.getElementById('takeProfitPrice').value = '';
            document.getElementById('expectedLoss').value = '';
            document.getElementById('expectedProfit').value = '';
            
            // 隐藏价格显示
            const priceDisplay = document.getElementById('currentPriceDisplay');
            if (priceDisplay) {
                priceDisplay.style.display = 'none';
            }
            
            const lastQuantity = localStorage.getItem('lastOrderQuantity');
            if (lastQuantity) {
                document.getElementById('orderQuantity').value = lastQuantity;
            }
            
            // 设置上次交易的资产符号
            const lastSymbol = localStorage.getItem('lastOrderSymbol');
            if (lastSymbol) {
                const symbolInput = document.querySelector('#orderSymbol');
                if (symbolInput) {
                    // 设置输入框的值
                    symbolInput.value = lastSymbol;
                    
                    // 延迟触发 input 事件，避免在弹窗刚打开时立即获取价格导致连接问题
                    setTimeout(() => {
                        const event = new Event('input');
                        symbolInput.dispatchEvent(event);
                    }, 500); // 延迟500ms
                }
            }
            

            this.setupOrderFormListeners();
            const modal = new bootstrap.Modal(document.getElementById('orderModal'));
            modal.show();
        });
        
        // Add risk rule button
        document.getElementById('addRiskRuleBtn').addEventListener('click', () => {
            this.openRiskRuleModal();
        });
        
        // Add risk rule button 2 (for independent panel)
        document.getElementById('addRiskRuleBtn2').addEventListener('click', () => {
            this.openRiskRuleModal();
        });
        
        // Submit order button
        document.getElementById('submitOrderBtn').addEventListener('click', () => {
            this.placeOrder();
        });
        
        // Submit risk rule button
        document.getElementById('submitRiskRuleBtn').addEventListener('click', () => {
            this.addRiskRule();
        });

        // Market data

        
        // Risk management
        document.getElementById('riskRuleForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.addRiskRule();
        });
        
        // WebSocket updates orders, positions and risk rules automatically
        // Auto Orders panel removed - no longer needed

        
        // Cancel all orders
        document.getElementById('cancelAllOrdersBtn').addEventListener('click', () => this.cancelAllOrders());
        
        // Test reminder buttons

        
        // 通知面板事件监听器
        const clearNotificationsBtn = document.getElementById('clearNotificationsBtn');
        if (clearNotificationsBtn) {
            clearNotificationsBtn.addEventListener('click', () => this.notificationsManager.clearNotifications());
        }
        

        
        // 加载通知历史记录到面板
        this.notificationsManager.filterNotificationHistory();
        
        console.log('All event listeners bound successfully');
    }
    
    // Refresh position suggestion automatically
    // Removed duplicate refreshPositionSuggestion method - using the one at line 6543
    
    setupOrderFormListeners() {
        // Order form
        document.getElementById('orderForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.placeOrder();
        });
        
        // Order type change
        document.getElementById('orderType').addEventListener('change', (e) => {
            const priceInput = document.getElementById('orderPrice');

            priceInput.disabled = e.target.value === 'MARKET';
            if (e.target.value === 'MARKET') {
                // 市价单时获取当前价格并填入
                this.fetchCurrentPrice();
            } else {
                // 限价单时清空价格，让用户手动输入
                priceInput.value = '';
            }

            // Recalculate SL/TP and suggestion whenever order type changes
            this.updateRiskPrices().catch(console.error);
        });
        
        // Stop loss and take profit price changes - calculate expected PnL only
        document.getElementById('stopLossPrice').addEventListener('input', () => {
            this.updateExpectedPnL();
            this.clearPositionSuggestion();
        });

        document.getElementById('takeProfitPrice').addEventListener('input', () => {
            this.updateExpectedPnL();
            this.clearPositionSuggestion();
        });
        
        // Order quantity change - update expected PnL only
        document.getElementById('orderQuantity').addEventListener('input', () => {
            this.updateExpectedPnL();
            this.clearPositionSuggestion();
        });
        
        // Note: Order action and symbol event listeners are already handled above in the unified section
        
        
    }

    // WebSocket functionality moved to IBKRConnectionManager

    scheduleTradingReminders() {
        return this.notificationsManager.scheduleTradingReminders();
    }

    fireTradingReminder(type) {
        return this.notificationsManager.fireTradingReminder(type);
    }
    

    


    getNextReminderDate(timeStr, reference) {
        if (typeof this.notificationsManager.getNextReminderDate === 'function') {
            return this.notificationsManager.getNextReminderDate(timeStr, reference);
        }
        return new Date(reference);
    }

    // API请求缓存
    apiCache = {};
    
    // 防抖函数
    debounce(func, wait) {
        let timeout;
        return function(...args) {
            const context = this;
            clearTimeout(timeout);
            timeout = setTimeout(() => func.apply(context, args), wait);
        };
    }
    
    // 带超时的fetch
    async fetchWithTimeout(url, options = {}) {
        const { timeout = 15000, ...fetchOptions } = options;
        
        const controller = new AbortController();
        const id = setTimeout(() => controller.abort(), timeout);
        
        try {
            const response = await fetch(url, {
                ...fetchOptions,
                signal: controller.signal
            });
            clearTimeout(id);
            return response;
        } catch (error) {
            clearTimeout(id);
            if (error.name === 'AbortError') {
                throw new Error('请求超时，请稍后再试');
            }
            throw error;
        }
    }
    
    // 带重试的API调用
    async apiCall(endpoint, options = {}) {
        // 检查是否为GET请求且需要缓存
        const method = options.method || 'GET';
        const normalizedMethod = method.toUpperCase();
        const useCache = options.useCache !== false && normalizedMethod === 'GET';
        const cacheTime = options.cacheTime || 10000; // 默认缓存10秒
        const endpointPath = (endpoint || '').split('?')[0];
        const {
            allowReadOnly = false,
            skipReadOnlyCheck = false,
            useCache: _ignoreCacheFlag,
            cacheTime: _ignoreCacheTime,
            maxRetries: _ignoreMaxRetries,
            ...fetchOptions
        } = options;

        const safeReadOnlyMethods = ['GET', 'HEAD', 'OPTIONS'];
        const readOnlyBypassEndpoints = ['/login'];
        if (this.isReadOnly && !allowReadOnly && !skipReadOnlyCheck) {
            if (!safeReadOnlyMethods.includes(normalizedMethod) && !readOnlyBypassEndpoints.includes(endpointPath)) {
                const now = Date.now();
                if (now - this.lastReadOnlyWarningAt > 1500) {
                    this.showAlert('warning', '当前访客账号仅支持查看数据');
                    this.lastReadOnlyWarningAt = now;
                }
                const readOnlyError = new Error('Read-only mode');
                readOnlyError.code = 'READ_ONLY';
                readOnlyError.isReadOnly = true;
                throw readOnlyError;
            }
        }
        
        // 生成缓存键
        const cacheKey = useCache ? `${endpoint}_${JSON.stringify(options.body || '')}` : null;
        
        // 检查缓存
        if (useCache && this.apiCache[cacheKey]) {
            const cachedData = this.apiCache[cacheKey];
            if ((Date.now() - cachedData.timestamp) < cacheTime) {
                console.log(`Using cached data for ${endpoint}`);
                return cachedData.data;
            } else {
                // 缓存已过期，删除过期缓存
                delete this.apiCache[cacheKey];
            }
        }
        
        // 设置重试参数
        const maxRetries = options.maxRetries ?? 2;
        let retries = 0;

        while (true) {
            try {
                const headers = {
                    'Content-Type': 'application/json',
                    ...fetchOptions.headers
                };

                // 添加JWT令牌到Authorization头
                const token = localStorage.getItem('access_token');
                if (token) {
                    headers['Authorization'] = `Bearer ${token}`;
                }

                // 使用带超时的fetch
                const response = await this.fetchWithTimeout(`/api${endpoint}`, {
                    ...fetchOptions,
                    headers,
                    method: normalizedMethod,
                    timeout: fetchOptions.timeout ?? 15000 // 默认15秒超时
                });

                const rawBody = await response.text();
                let data = {};
                if (rawBody) {
                    try {
                        data = JSON.parse(rawBody);
                    } catch (parseErr) {
                        data = { detail: rawBody };
                    }
                }

                const reqId = response.headers.get('x-request-id');
                if (reqId && data && typeof data === 'object') {
                    data.request_id = reqId;
                }

                // 处理401未授权 - 重定向到登录
                if (response.status === 401) {
                    localStorage.removeItem('access_token');
                    this.loginManager.toggleAuthButtons(false);
                    this.loginManager.showLoginModal();
                    throw new Error('Authentication required');
                }

                if (response.status === 403) {
                    const detail = (data && typeof data === 'object' && data.detail) ? data.detail : 'Forbidden';
                    const lowered = String(detail).toLowerCase();
                    if (lowered.includes('read-only')) {
                        const now = Date.now();
                        if (now - this.lastReadOnlyWarningAt > 1500) {
                            this.showAlert('warning', detail);
                            this.lastReadOnlyWarningAt = now;
                        }
                        const err = new Error(detail);
                        err.isReadOnly = true;
                        err.code = 'READ_ONLY';
                        throw err;
                    }
                    const err = new Error(detail);
                    err.status = 403;
                    throw err;
                }

                // 特殊处理/api/notify接口，即使返回success:false也不抛出错误
                if (endpoint === '/notify' && typeof data === 'object' && 'success' in data) {
                    if (!data.success) {
                        console.warn('Notification failed:', data.message || 'Unknown reason');
                    }
                    return data;
                }
                
                if (!response.ok) {
                    const detail = (data && typeof data === 'object' && data.detail)
                        ? data.detail
                        : 'API call failed';
                    throw new Error(detail);
                }

                // 更新缓存
                if (useCache) {
                    this.apiCache[cacheKey] = {
                        data: data,
                        timestamp: Date.now()
                    };
                }
                
                return data;
            } catch (error) {
                if (error.isReadOnly || error.code === 'READ_ONLY') {
                    throw error;
                }
                // 如果还有重试次数，则重试
                if (retries < maxRetries && error.message !== 'Authentication required') {
                    retries++;
                    console.warn(`Retrying API call to ${endpoint} (${retries}/${maxRetries})`);
                    await new Promise(resolve => setTimeout(resolve, 1000 * retries)); // 指数退避
                    continue;
                }
                
                // 特殊处理IBKR连接错误 - 直接打开设置窗口而不显示错误提示（带防抖，避免重复弹窗）
                if (error.message === 'Not connected to IBKR') {
                    console.warn('IBKR not connected, opening settings modal (debounced)');
                    const connectionModalEl = document.getElementById('connectionModal');
                    if (connectionModalEl) {
                        const alreadyOpen = connectionModalEl.classList.contains('show') || connectionModalEl.dataset.opening === 'true';
                        if (!alreadyOpen) {
                            connectionModalEl.dataset.opening = 'true';
                            const settingsModal = bootstrap.Modal.getOrCreateInstance(connectionModalEl);
                            settingsModal.show();
                            const clearFlag = () => { delete connectionModalEl.dataset.opening; };
                            connectionModalEl.addEventListener('hidden.bs.modal', clearFlag, { once: true });
                            // 双保险：3秒后确保解除opening标记，避免异常状态导致的卡死
                            setTimeout(clearFlag, 3000);
                        }
                    }
                    throw error;
                }
                
                if (error.message !== 'Authentication required') {
                    this.showAlert('danger', error.message);
                }
                throw error;
            }
        }
    }

    guardChartScriptableDescriptors() {
        if (this.chartScriptableGuardApplied) {
            return;
        }
        const chart = window.Chart;
        const defaults = chart?.defaults;
        if (!defaults || typeof defaults !== 'object') {
            return;
        }

        const guardFn = (fn) => {
            if (typeof fn !== 'function') {
                return fn;
            }
            if (fn.__ibbotGuarded) {
                return fn;
            }
            const wrapped = (key, ...args) => {
                if (typeof key !== 'string') {
                    if (key != null) {
                        console.warn('[Chart] Non-string descriptor key encountered', { key });
                    }
                    return false;
                }
                try {
                    return fn(key, ...args);
                } catch (err) {
                    console.warn('[Chart] Descriptor check failed, suppressing error', err);
                    return false;
                }
            };
            wrapped.__ibbotGuarded = true;
            return wrapped;
        };

        const visited = new WeakSet();
        const traverseDescriptors = (node, depth = 0) => {
            if (!node || typeof node !== 'object' || depth > 8) {
                return;
            }
            if (visited.has(node)) {
                return;
            }
            visited.add(node);

            if (Object.prototype.hasOwnProperty.call(node, '_scriptable')) {
                node._scriptable = guardFn(node._scriptable);
            }
            if (Object.prototype.hasOwnProperty.call(node, '_indexable')) {
                node._indexable = guardFn(node._indexable);
            }

            if (Array.isArray(node)) {
                node.forEach((item) => traverseDescriptors(item, depth + 1));
                return;
            }

            for (const key of Reflect.ownKeys(node)) {
                if (key === '_scriptable' || key === '_indexable') {
                    continue;
                }
                const value = node[key];
                if (!value || typeof value !== 'object') {
                    continue;
                }
                traverseDescriptors(value, depth + 1);
            }
        };

        traverseDescriptors(defaults);
        this.chartScriptableGuardApplied = true;
    }

    registerCandlestickControllers() {
        try {
            this.guardChartScriptableDescriptors();
            const chartLib = window.Chart;
            if (!chartLib) {
                return false;
            }

            const registry = chartLib.registry;
            const controllers = registry?.controllers;
            const elements = registry?.elements;
            const hasEntry = (collection, key) => typeof collection?.has === 'function' && collection.has(key);
            const keys = ['candlestick', 'financial', 'ohlc'];
            const controllerReady = keys.some((key) => hasEntry(controllers, key));
            const elementReady = !elements || keys.some((key) => hasEntry(elements, key));

            if (controllerReady && elementReady) {
                this.candlestickSupported = true;
                return true;
            }

            if (typeof chartLib.register !== 'function') {
                return false;
            }
            const financialCandidates = [
                window.ChartFinancial,
                window['chartjs-chart-financial'],
                window.chartFinancial,
                window.chartjsChartFinancial,
                window.financialChart
            ].filter(Boolean);

            for (const fin of financialCandidates) {
                const components = [
                    fin?.CandlestickController,
                    fin?.OhlcController,
                    fin?.CandlestickElement,
                    fin?.OhlcElement,
                    fin?.FinancialController,
                    fin?.FinancialElement
                ].filter(Boolean);
                if (components.length) {
                    chartLib.register(...components);
                    if (!registry) {
                        this.candlestickSupported = true;
                        return true;
                    }
                    if (keys.some((key) => hasEntry(controllers, key)) && (!elements || keys.some((key) => hasEntry(elements, key)))) {
                        this.candlestickSupported = true;
                        return true;
                    }
                }
            }
        } catch (error) {
            console.error('Failed to register candlestick controllers', error);
        }
        return this.candlestickSupported;
    }

    loadExternalScript(url) {
        return new Promise((resolve, reject) => {
            const normalizedUrl = (url || '').trim();
            if (!normalizedUrl) {
                resolve(true);
                return;
            }

            const scripts = Array.from(document.getElementsByTagName('script'));
            const existing = scripts.find((script) => script.src && script.src.includes('chartjs-chart-financial'));

            if (existing) {
                const markLoaded = () => {
                    existing.dataset.loaded = 'true';
                    resolve(true);
                };

                const checkReady = () => {
                    if (this.candlestickSupported) {
                        return true;
                    }
                    try {
                        if (this.registerCandlestickControllers()) {
                            this.candlestickSupported = true;
                            return true;
                        }
                    } catch (err) {
                        console.warn('Candlestick controller registration check failed:', err);
                    }
                    if (existing.dataset.loaded === 'true') {
                        return true;
                    }
                    const readyState = existing.readyState;
                    return readyState === 'complete' || readyState === 'loaded';
                };

                if (checkReady()) {
                    markLoaded();
                    return;
                }

                existing.addEventListener('load', () => {
                    console.log('[Chart] Existing candlestick script load event triggered');
                    markLoaded();
                }, { once: true });
                existing.addEventListener('error', (err) => reject(err || new Error(`Failed to load ${normalizedUrl}`)), { once: true });

                setTimeout(() => {
                    if (checkReady()) {
                        console.log('[Chart] Candlestick plugin already available on existing script');
                        markLoaded();
                    }
                }, 0);
                return;
            }

            const script = document.createElement('script');
            script.src = normalizedUrl;
            script.async = true;
            script.defer = true;
            script.crossOrigin = 'anonymous';
            script.dataset.dynamic = 'chartjs-financial';
            script.addEventListener('load', () => {
                script.dataset.loaded = 'true';
                resolve(true);
            }, { once: true });
            script.addEventListener('error', (err) => {
                reject(err || new Error(`Failed to load ${normalizedUrl}`));
            }, { once: true });
            document.head.appendChild(script);
        });
    }

    async ensureCandlestickPlugin() {
        if (this.candlestickSupported) {
            return true;
        }
        if (this.registerCandlestickControllers()) {
            return true;
        }
        if (this.candlestickLoader) {
            return this.candlestickLoader;
        }

        this.candlestickLoader = this.loadExternalScript(this.financialScriptUrl)
            .then(() => {
                const registered = this.registerCandlestickControllers();
                if (!registered) {
                    throw new Error('chartjs-chart-financial loaded but controllers unavailable');
                }
                return true;
            })
            .catch((error) => {
                console.error('Failed to load chartjs-chart-financial plugin', error);
                return false;
            })
            .finally(() => {
                this.candlestickLoader = null;
            });

        return this.candlestickLoader;
    }

    async waitForCandlestickPlugin(options = {}) {
        const { promise = null, timeoutMs = 2500 } = options || {};
        const loaderPromise = Promise.resolve(promise ?? this.ensureCandlestickPlugin());
        const timeoutToken = Symbol('candlestick-timeout');
        try {
            const result = await Promise.race([
                loaderPromise,
                new Promise((resolve) => setTimeout(() => resolve(timeoutToken), Math.max(0, timeoutMs)))
            ]);
            if (result === timeoutToken) {
                console.warn(`[Chart] Candlestick plugin load timed out after ${timeoutMs}ms; proceeding without it.`);
                return { ready: false, timedOut: true, promise: loaderPromise };
            }
            return { ready: !!result, timedOut: false, promise: loaderPromise };
        } catch (error) {
            console.warn('[Chart] Candlestick plugin load failed:', error);
            return { ready: false, timedOut: false, promise: loaderPromise };
        }
    }

    createCandlestickChartConfig(options = {}) {
        const {
            symbol = '',
            ohlcData = [],
            labels = [],
            priceAxisId = 'y',
            volumeAxisId = 'volume',
            includeLabels = true,
            datasetLabel = null,
            volumeLabel = 'Volume',
            priceDatasetOptions = {},
            volumeDatasetOptions = {}
        } = options || {};

        const safeBars = Array.isArray(ohlcData)
            ? ohlcData.filter((bar) => bar && Number.isFinite(bar.x))
            : [];

        const volumeData = safeBars.map((bar) => {
            const rawVolume = Number.isFinite(bar.volume)
                ? bar.volume
                : Number.isFinite(bar.v)
                    ? bar.v
                    : 0;
            return {
                x: bar.x,
                y: Number.isFinite(rawVolume) ? rawVolume : 0
            };
        });

        const maxVolume = volumeData.reduce((acc, point) => {
            const value = Number.isFinite(point?.y) ? point.y : 0;
            return value > acc ? value : acc;
        }, 0);

        const resolvedLabel = datasetLabel
            ? String(datasetLabel)
            : `${symbol ? `${symbol} ` : ''}OHLC`.trim();

        const candlestickDataset = {
            type: 'candlestick',
            label: resolvedLabel,
            data: safeBars,
            yAxisID: priceAxisId,
            parsing: false,
            ...priceDatasetOptions
        };

        const volumeDataset = {
            type: 'bar',
            label: String(volumeLabel || 'Volume'),
            data: volumeData,
            yAxisID: volumeAxisId,
            backgroundColor: 'rgba(75, 192, 192, 0.6)',
            borderWidth: 0,
            parsing: false,
            ...volumeDatasetOptions
        };

        const shouldIncludeLabels = includeLabels !== false;
        const data = {
            datasets: [candlestickDataset, volumeDataset]
        };
        if (shouldIncludeLabels && Array.isArray(labels) && labels.length) {
            data.labels = labels;
        }

        return {
            type: 'candlestick',
            data,
            datasets: {
                price: candlestickDataset,
                volume: volumeDataset
            },
            meta: {
                volumeData,
                maxVolume
            }
        };
    }

    isLuxonReady() {
        return typeof luxon !== 'undefined' && luxon && typeof luxon.DateTime === 'function';
    }

    async ensureLuxon(options = {}) {
        if (this.isLuxonReady()) {
            return true;
        }

        if (this.luxonLoader) {
            try {
                return await this.luxonLoader;
            } catch (error) {
                console.warn('Luxon loading promise rejected:', error);
                return this.isLuxonReady();
            }
        }

        const targetUrl = (options && options.url) || this.luxonScriptUrl;
        if (!targetUrl) {
            return this.isLuxonReady();
        }

        const normalizedUrl = targetUrl.trim();
        const dynamicScriptSelector = 'script[data-dynamic="luxon"]';
        const existingDynamic = document.querySelector(dynamicScriptSelector);

        const buildLoaderPromise = (scriptElement, newlyCreated = false) => new Promise((resolve, reject) => {
            const cleanup = () => {
                scriptElement.removeEventListener('load', handleLoad);
                scriptElement.removeEventListener('error', handleError);
            };
            const finalize = () => {
                setTimeout(() => {
                    resolve(this.isLuxonReady());
                }, 0);
            };
            const handleLoad = () => {
                if (newlyCreated) {
                    scriptElement.dataset.loaded = 'true';
                }
                cleanup();
                finalize();
            };
            const handleError = (err) => {
                cleanup();
                reject(err || new Error(`Failed to load Luxon script ${normalizedUrl}`));
            };

            scriptElement.addEventListener('load', handleLoad, { once: true });
            scriptElement.addEventListener('error', handleError, { once: true });

            if (scriptElement.dataset && scriptElement.dataset.loaded === 'true') {
                finalize();
            }
        });

        if (existingDynamic) {
            if (existingDynamic.dataset.loading !== 'true' && existingDynamic.dataset.loaded === 'true') {
                return this.isLuxonReady();
            }
            this.luxonLoader = buildLoaderPromise(existingDynamic);
        } else {
            const script = document.createElement('script');
            script.src = normalizedUrl;
            script.crossOrigin = 'anonymous';
            script.async = true;
            script.dataset.loading = 'true';
            script.dataset.dynamic = 'luxon';
            this.luxonLoader = buildLoaderPromise(script, true);
            script.addEventListener('load', () => { delete script.dataset.loading; }, { once: true });
            script.addEventListener('error', () => { delete script.dataset.loading; }, { once: true });
            document.head.appendChild(script);
        }

        try {
            const ready = await this.luxonLoader;
            return ready;
        } catch (error) {
            console.warn('Failed to load Luxon:', error);
            return this.isLuxonReady();
        } finally {
            this.luxonLoader = null;
        }
    }

    isDateAdapterReady() {
        const chart = window.Chart;
        const adapterClass = chart?._adapters?._date;
        if (!adapterClass) {
            return false;
        }
        const proto = typeof adapterClass === 'function'
            ? adapterClass.prototype
            : adapterClass;
        if (!proto || typeof proto.formats !== 'function') {
            return false;
        }
        const source = Function.prototype.toString.call(proto.formats);
        if (source.includes('This method is not implemented')) {
            return false;
        }
        return true;
    }

    async ensureDateAdapter(options = {}) {
        if (this.dateAdapterReady && this.isDateAdapterReady()) {
            return true;
        }

        const attemptMarkReady = () => {
            const ready = this.isDateAdapterReady();
            this.dateAdapterReady = ready;
            return ready;
        };

        if (attemptMarkReady()) {
            return true;
        }

        if (this.dateAdapterLoader) {
            try {
                const result = await this.dateAdapterLoader;
                return result;
            } catch (error) {
                console.warn('Date adapter loading promise rejected:', error);
                return false;
            }
        }

        const luxonReady = await this.ensureLuxon();
        if (!luxonReady) {
            console.warn('Luxon library unavailable - date adapter may not initialize correctly.');
        }

        const targetUrl = (options && options.url) || this.chartDateAdapterUrl;
        if (!targetUrl) {
            return attemptMarkReady();
        }

        const normalizedUrl = targetUrl.trim();
        const dynamicScriptSelector = 'script[data-dynamic="chartjs-adapter-luxon"]';
        const existingDynamic = document.querySelector(dynamicScriptSelector);

        const buildLoaderPromise = (scriptElement, newlyCreated = false) => new Promise((resolve, reject) => {
            const cleanup = () => {
                scriptElement.removeEventListener('load', handleLoad);
                scriptElement.removeEventListener('error', handleError);
            };
            const finalize = () => {
                setTimeout(() => {
                    const ready = attemptMarkReady();
                    resolve(ready);
                }, 0);
            };
            const handleLoad = () => {
                if (newlyCreated) {
                    scriptElement.dataset.loaded = 'true';
                }
                cleanup();
                finalize();
            };
            const handleError = (err) => {
                cleanup();
                reject(err || new Error(`Failed to load date adapter script ${normalizedUrl}`));
            };

            scriptElement.addEventListener('load', handleLoad, { once: true });
            scriptElement.addEventListener('error', handleError, { once: true });

            if (scriptElement.dataset && scriptElement.dataset.loaded === 'true') {
                finalize();
            }
        });

        if (existingDynamic) {
            if (existingDynamic.dataset.loading !== 'true' && existingDynamic.dataset.loaded === 'true') {
                return attemptMarkReady();
            }
            this.dateAdapterLoader = buildLoaderPromise(existingDynamic);
        } else {
            const script = document.createElement('script');
            script.src = normalizedUrl;
            script.crossOrigin = 'anonymous';
            script.defer = false;
            script.async = true;
            script.dataset.loading = 'true';
            script.dataset.dynamic = 'chartjs-adapter-luxon';
            this.dateAdapterLoader = buildLoaderPromise(script, true);
            script.addEventListener('load', () => { delete script.dataset.loading; }, { once: true });
            script.addEventListener('error', () => { delete script.dataset.loading; }, { once: true });
            document.head.appendChild(script);
        }

        try {
            const ready = await this.dateAdapterLoader;
            return ready;
        } catch (error) {
            console.warn('Failed to load Chart.js date adapter:', error);
            return false;
        } finally {
            this.dateAdapterLoader = null;
        }
    }

    updateReadOnlyMode(isReadOnly, role = 'admin', options = {}) {
        const persist = options.persist !== false;
        this.isReadOnly = !!isReadOnly;
        this.authRole = role || (this.isReadOnly ? 'visitor' : 'admin');

        if (persist) {
            if (this.isReadOnly) {
                localStorage.setItem('is_read_only', '1');
            } else {
                localStorage.removeItem('is_read_only');
            }
            if (role) {
                localStorage.setItem('user_role', role);
            } else {
                localStorage.removeItem('user_role');
            }
        }

        if (typeof document !== 'undefined' && document.body) {
            document.body.classList.toggle('read-only-mode', this.isReadOnly);
            const badge = document.getElementById('authRoleBadge');
            if (badge) {
                if (this.isReadOnly) {
                    badge.classList.remove('d-none');
                    badge.innerHTML = '<i class="fas fa-user-shield me-1"></i>Visitor Read Only';
                } else if (role && role.toLowerCase() !== 'admin') {
                    badge.classList.remove('d-none');
                    badge.innerHTML = `<i class="fas fa-user me-1"></i>${role}`;
                } else {
                    badge.classList.add('d-none');
                    badge.textContent = '';
                }
            }
            const banner = document.getElementById('readOnlyBanner');
            if (banner) {
                banner.classList.toggle('d-none', !this.isReadOnly);
            }
        }
    }

    // Connection methods moved to IBKRConnectionManager

    // 数据相关方法已移至 IBKRDataManager
    // loadAccountInfo, updateAccountInfoUI, updatePositionsUI, isOpeningPosition, getCachedPrice

    // Get trading price based on order type
    async getTradingPrice(symbol, orderType, limitPrice = null) {
        if (orderType === 'LIMIT' && limitPrice && limitPrice > 0) {
            return limitPrice;
        }
        
        // For market orders, get current market price from cache
        return await this.dataManager.getCachedPrice(symbol);
    }
    
    // Update risk prices based on trading price and order details
    async updateRiskPrices() {
        const orderSymbol = document.getElementById('orderSymbol').value;
        const orderType = document.getElementById('orderType').value;
        const orderAction = document.getElementById('orderAction').value;
        const orderPrice = parseFloat(document.getElementById('orderPrice').value);
        const riskRuleSection = document.getElementById('riskRuleSection');

        // Clear SL/TP if no symbol
        if (!orderSymbol) {
            document.getElementById('stopLossPrice').value = '';
            document.getElementById('takeProfitPrice').value = '';
            // Always keep SL/TP section visible
            if (riskRuleSection) riskRuleSection.style.display = 'block';
            this.updateExpectedPnL();
            return;
        }

        // Check if this is opening or closing position
        const isOpening = this.dataManager.isOpeningPosition(orderSymbol, orderAction);

        // If closing position, don't calculate SL/TP but keep section visible
        if (!isOpening) {
            document.getElementById('stopLossPrice').value = '';
            document.getElementById('takeProfitPrice').value = '';
            // Always keep SL/TP section visible, hide only position suggestions
            if (riskRuleSection) riskRuleSection.style.display = 'block';
            document.getElementById('suggestedPositionSection').style.display = 'none';
            this.updateExpectedPnL();
            return;
        }

        // Show suggestions and risk rule section for opening positions
        document.getElementById('suggestedPositionSection').style.display = 'block';

        // Get trading price
        const tradingPrice = await this.getTradingPrice(orderSymbol, orderType, orderPrice);

        if (!tradingPrice || tradingPrice <= 0) {
            document.getElementById('stopLossPrice').value = '';
            document.getElementById('takeProfitPrice').value = '';
            // Always keep SL/TP section visible even without valid price
            if (riskRuleSection) riskRuleSection.style.display = 'block';
            this.updateExpectedPnL();
            return;
        }

        if (riskRuleSection) riskRuleSection.style.display = 'block';
        
        // Get configuration from backend
        let stopLossRatio = 0.025; // Default 2.5%
        let takeProfitRatio = 0.025; // Default 2.5%
        
        try {
            const config = await this.apiCall('/risk-config');
            stopLossRatio = config.stop_loss_ratio || 0.025;
            takeProfitRatio = config.take_profit_ratio || 0.025;
            
            // Apply leverage adjustment for leveraged assets
            let adjustedStopLossRatio = stopLossRatio;
            let adjustedTakeProfitRatio = takeProfitRatio;
            
            const leverageMap = (config && config.leverage) ? config.leverage : {};
            const lev = leverageMap[orderSymbol] || leverageMap[orderSymbol?.toUpperCase?.()] || (orderSymbol === 'MNQ' ? (leverageMap['MNQ'] || 20) : null);
            if (lev && !isNaN(lev) && lev > 0) {
                adjustedStopLossRatio = stopLossRatio / lev;
                adjustedTakeProfitRatio = takeProfitRatio / lev;
            }
            
            // Calculate stop loss and take profit based on action direction
            let stopLoss, takeProfit;
            if (orderAction === 'BUY') {
                stopLoss = (tradingPrice * (1 - adjustedStopLossRatio)).toFixed(2);
                takeProfit = (tradingPrice * (1 + adjustedTakeProfitRatio)).toFixed(2);
            } else { // SELL
                stopLoss = (tradingPrice * (1 + adjustedStopLossRatio)).toFixed(2);
                takeProfit = (tradingPrice * (1 - adjustedTakeProfitRatio)).toFixed(2);
            }
            
            document.getElementById('stopLossPrice').value = stopLoss;
            document.getElementById('takeProfitPrice').value = takeProfit;
            
            // 移除自动刷新position suggestion，避免频繁API调用
            // this.refreshPositionSuggestion();
            
        } catch (error) {
            console.warn('Failed to get risk config, using defaults:', error);
            // Use default ratios without leverage adjustment
            let stopLoss, takeProfit;
            if (orderAction === 'BUY') {
                stopLoss = (tradingPrice * (1 - stopLossRatio)).toFixed(2);
                takeProfit = (tradingPrice * (1 + takeProfitRatio)).toFixed(2);
            } else { // SELL
                stopLoss = (tradingPrice * (1 + stopLossRatio)).toFixed(2);
                takeProfit = (tradingPrice * (1 - takeProfitRatio)).toFixed(2);
            }
            
            document.getElementById('stopLossPrice').value = stopLoss;
            document.getElementById('takeProfitPrice').value = takeProfit;
            
            // 移除自动刷新position suggestion，避免频繁API调用
            // this.refreshPositionSuggestion();
        }
        
        this.updateExpectedPnL();
    }
    
    // Update expected PnL based on current inputs
    // 旧版本的updateExpectedPnL已删除，使用新版本的方法

    openRiskRuleModal(...args) {
        if (!this.riskRulesManager || typeof this.riskRulesManager.openRiskRuleModal !== 'function') {
            return;
        }
        return this.riskRulesManager.openRiskRuleModal(...args);
    }

    updatePnLPreview() {
        if (!this.riskRulesManager || typeof this.riskRulesManager.updatePnLPreview !== 'function') {
            return;
        }
        return this.riskRulesManager.updatePnLPreview();
    }

    addPnLUpdateListeners() {
        if (!this.riskRulesManager || typeof this.riskRulesManager.addPnLUpdateListeners !== 'function') {
            return;
        }
        return this.riskRulesManager.addPnLUpdateListeners();
    }

    removePnLUpdateListeners() {
        if (!this.riskRulesManager || typeof this.riskRulesManager.removePnLUpdateListeners !== 'function') {
            return;
        }
        return this.riskRulesManager.removePnLUpdateListeners();
    }

    async editPositionRiskRule(...args) {
        if (!this.riskRulesManager || typeof this.riskRulesManager.editPositionRiskRule !== 'function') {
            return;
        }
        return this.riskRulesManager.editPositionRiskRule(...args);
    }

    shouldResumeMonitorRealtime(symbol) {
        if (typeof symbol !== 'string') return false;
        const target = symbol.trim();
        if (!target) return false;
        return !!(this.monitorRealtimeShouldResume && this.monitorRealtimeResumeSymbol === target);
    }

    async subscribeMonitorRealtime(symbol, options = {}) {
        const { skipReload = false, reason = 'manual' } = options || {};
        if (typeof symbol !== 'string') return false;
        const target = symbol.trim();
        if (!target) return false;

        if (this.monitorRealtimeSymbol && this.monitorRealtimeSymbol !== target) {
            this.unsubscribeMonitorRealtime({ keepPreference: false, reason: 'switch-symbol' });
        }
        if (this.monitorRealtimeResumeSymbol && this.monitorRealtimeResumeSymbol !== target) {
            this.monitorRealtimeShouldResume = false;
            this.monitorRealtimeResumeSymbol = null;
        }

        if (this.monitorRealtimeSymbol === target) {
            this.monitorRealtimeShouldResume = true;
            this.monitorRealtimeResumeSymbol = target;
            if (this.currentMonitor) {
                this.currentMonitor.realtimeActive = true;
            }
            if (!skipReload) {
                this.loadMonitorChart(target, { useCache: false, forceRefresh: true });
            }
            return true;
        }

        try {
            console.log('[monitor] subscribe realtime', { symbol: target, reason });
            this.ibkrManager.ws?.send(JSON.stringify({ type: 'subscribe_realtime', symbol: target }));
            this.monitorRealtimeSymbol = target;
            this.monitorRealtimeShouldResume = true;
            this.monitorRealtimeResumeSymbol = target;
            if (this.currentMonitor) {
                this.currentMonitor.realtimeActive = true;
            }
            if (!skipReload) {
                this.loadMonitorChart(target, { useCache: false, forceRefresh: true });
            }
            return true;
        } catch (err) {
            console.error('subscribe realtime failed', err);
            return false;
        }
    }

    unsubscribeMonitorRealtime(options = {}) {
        const { keepPreference = false, reason = 'manual' } = options || {};
        const activeSymbol = this.monitorRealtimeSymbol;
        if (activeSymbol) {
            try {
                console.log('[monitor] unsubscribe realtime', activeSymbol, reason);
                this.ibkrManager.ws?.send(JSON.stringify({ type: 'unsubscribe_realtime', symbol: activeSymbol }));
            } catch (err) {
                console.warn('unsubscribe realtime failed', err);
            }
        }
        this.monitorRealtimeSymbol = null;
        if (this.currentMonitor) {
            this.currentMonitor.realtimeActive = keepPreference ? !!this.currentMonitor.realtimeActive : false;
        }
        if (!keepPreference) {
            this.monitorRealtimeShouldResume = false;
            this.monitorRealtimeResumeSymbol = null;
        } else if (activeSymbol) {
            this.monitorRealtimeShouldResume = true;
            this.monitorRealtimeResumeSymbol = activeSymbol;
        }
        return activeSymbol;
    }

    async exitMonitorFullscreen(options = {}) {
        const { removeListener = true } = options || {};
        const modalEl = document.getElementById('monitorModal');
        if (modalEl) {
            modalEl.classList.remove('monitor-fullscreen');
        }
        this.monitorModalFullscreen = false;
        if (document.fullscreenElement) {
            try {
                if (document.exitFullscreen) {
                    await document.exitFullscreen();
                } else if (document.webkitExitFullscreen) {
                    await document.webkitExitFullscreen();
                }
            } catch (err) {
                console.warn('exitFullscreen failed', err);
            }
        }
        if (removeListener && this._monitorFullscreenChangeHandler) {
            document.removeEventListener('fullscreenchange', this._monitorFullscreenChangeHandler);
            this._monitorFullscreenChangeHandler = null;
        }
    }

    async closePosition(symbol, options = {}) {
        return this.orderManager.closePosition(symbol, options);
    }

    async openMonitorModal(symbol, positionQty = 0, currentPrice = 0, avgCost = 0, pnl = 0, pnlPercent = 0) {
        try {
            // 获取最新订单以确定建仓/加仓时间
            try {
                await this.loadOrders();
            } catch (e) {}

            const args = Array.from(arguments);
            const positionArgProvided = args.length >= 2;
            const priceArgProvided = args.length >= 3;
            const avgArgProvided = args.length >= 4;
            const pnlArgProvided = args.length >= 5;
            const pnlPctArgProvided = args.length >= 6;

            let resolvedPositionQty = positionArgProvided ? Number(positionQty) || 0 : null;
            let resolvedCurrentPrice = priceArgProvided ? Number(currentPrice) || 0 : null;
            let resolvedAvgCost = avgArgProvided ? Number(avgCost) || 0 : null;
            let resolvedPnl = pnlArgProvided ? Number(pnl) || 0 : null;
            let resolvedPnlPercent = pnlPctArgProvided ? Number(pnlPercent) || 0 : null;

            const normalizedSymbol = typeof symbol === 'string' ? symbol.trim() : '';
            const lookupSymbol = normalizedSymbol ? normalizedSymbol.toUpperCase() : '';
            if (lookupSymbol) {
                let positionInfo = this.dataManager?.getPosition?.(lookupSymbol);
                if (!positionInfo && this.dataManager?.loadPortfolio) {
                    try {
                        await this.dataManager.loadPortfolio();
                        positionInfo = this.dataManager.getPosition?.(lookupSymbol);
                    } catch (portfolioErr) {
                        console.warn('Failed to refresh portfolio for monitor modal:', portfolioErr);
                    }
                }
                if (positionInfo) {
                    if (!positionArgProvided && resolvedPositionQty === null) {
                        resolvedPositionQty = Number(positionInfo.position) || 0;
                    }
                    if (!avgArgProvided && resolvedAvgCost === null) {
                        resolvedAvgCost = Number(positionInfo.avg_cost) || 0;
                    }
                    if (!priceArgProvided && resolvedCurrentPrice === null) {
                        resolvedCurrentPrice = Number(positionInfo.current_price) || 0;
                    }
                    if (!pnlArgProvided && resolvedPnl === null && positionInfo.unrealized_pnl !== undefined) {
                        resolvedPnl = Number(positionInfo.unrealized_pnl) || 0;
                    }
                    if (!pnlPctArgProvided && resolvedPnlPercent === null && positionInfo.unrealized_pnl_percent !== undefined) {
                        resolvedPnlPercent = Number(positionInfo.unrealized_pnl_percent) || 0;
                    }
                }
            }

            resolvedPositionQty = resolvedPositionQty ?? 0;
            resolvedCurrentPrice = resolvedCurrentPrice ?? 0;
            resolvedAvgCost = resolvedAvgCost ?? 0;
            resolvedPnl = resolvedPnl ?? 0;
            resolvedPnlPercent = resolvedPnlPercent ?? 0;

            if (normalizedSymbol) {
                symbol = normalizedSymbol;
            }

            const multiplierSymbol = lookupSymbol || (typeof symbol === 'string' ? symbol.toUpperCase() : '');
            const contractMultiplier = this.getContractMultiplier(multiplierSymbol);

            positionQty = resolvedPositionQty;
            currentPrice = resolvedCurrentPrice;
            avgCost = resolvedAvgCost;
            pnl = resolvedPnl;
            pnlPercent = resolvedPnlPercent;

            // 填充基础字段
            const symEl = document.getElementById('monitorSymbol');
            if (!symEl) return;
            symEl.textContent = symbol || '—';
            const combinedEl = document.getElementById('monitorPositionCombined');
            if (combinedEl) {
                const positionValue = (Math.abs(positionQty) || 0) * (currentPrice || 0) * (contractMultiplier || 1);
                combinedEl.textContent = `${(positionQty || 0)} ($${this.formatNumber(positionValue)})`;
            }
            const curEl = document.getElementById('monitorCurrentPrice');
            if (curEl) curEl.textContent = `$${this.formatNumber(currentPrice || 0)}`;
            const avgEl = document.getElementById('monitorAvgCost');
            if (avgEl) avgEl.textContent = `$${this.formatNumber(avgCost || 0)}`;
            const pnlEl = document.getElementById('monitorPnL');
            if (pnlEl) {
                pnlEl.textContent = `$${this.formatNumber(pnl || 0)}`;
                pnlEl.classList.remove('text-success', 'text-danger');
                if ((pnl || 0) > 1e-8) pnlEl.classList.add('text-success');
                else if ((pnl || 0) < -1e-8) pnlEl.classList.add('text-danger');
            }
            const pnlPctEl = document.getElementById('monitorPnLPercent');
            if (pnlPctEl) {
                pnlPctEl.textContent = `(${this.formatNumber(pnlPercent || 0)}%)`;
                pnlPctEl.classList.remove('text-success', 'text-danger');
                if ((pnl || 0) > 1e-8) pnlPctEl.classList.add('text-success');
                else if ((pnl || 0) < -1e-8) pnlPctEl.classList.add('text-danger');
            }

            // 计算建仓/加仓时间：取该标的最新订单时间
            let entryTime = new Date();
            let lastOrderTime = null;
            if (Array.isArray(this.orders)) {
                const latestOrderTime = this.orders
                    .filter(o => o.symbol === symbol && o.submit_time)
                    .map(o => this.parseMonitorChartTime(o.submit_time))
                    .filter(d => !isNaN(d.getTime()))
                    .sort((a, b) => b - a)[0];
                if (latestOrderTime) {
                    entryTime = latestOrderTime;
                }
            }

            const previousSymbol = this.currentMonitor?.symbol;

            // 保存当前监控上下文
            const durationSelectEl = document.getElementById('monitorDuration');
            const selectedDurationMinutes = getMonitorDurationMinutes(durationSelectEl?.value);
            const configuredSpanRaw = (this.riskRules && this.riskRules[symbol] && this.riskRules[symbol].max_time_span) || null;
            const configuredSpanMinutes = getMonitorDurationMinutes(configuredSpanRaw);
            const initialSpan = Number.isFinite(configuredSpanMinutes) && configuredSpanMinutes > 0
                ? configuredSpanMinutes
                : (Number.isFinite(selectedDurationMinutes) && selectedDurationMinutes > 0
                    ? selectedDurationMinutes
                    : MONITOR_CHART_MAX_MINUTES);

            this.currentMonitor = {
                symbol,
                positionQty: Number(positionQty) || 0,
                currentPrice: Number(currentPrice) || 0,
                avgCost: Number(avgCost) || 0,
                pnl: Number(pnl) || 0,
                pnlPercent: Number(pnlPercent) || 0,
                contractMultiplier: Number(contractMultiplier) || 1,
                maxTimeSpan: initialSpan,
                entryTime,
                lastOrderTime, // 添加最近订单时间
                timeAlertSent: false,
            };

            if (!previousSymbol || previousSymbol !== symbol) {
                this.resetMonitorTrendData();
            }
            if (this.domManager && typeof this.domManager.handleMonitorSymbolChange === 'function') {
                await this.domManager.handleMonitorSymbolChange(symbol);
            }

            // 清理输入框
            const slInput = document.getElementById('monitorStopLoss');
            if (slInput) slInput.value = '';
            const tpInput = document.getElementById('monitorTakeProfit');
            if (tpInput) tpInput.value = '';

            // 预填充风险规则与开关状态
            try {
                let rule = (this.riskRules && this.riskRules[symbol]) ? this.riskRules[symbol] : null;
                if (!rule) {
                    const riskResp = await this.apiCall('/risk_rules');
                    this.riskRules = riskResp.rules || {};
                    rule = this.riskRules[symbol];
                }
                const toggle = document.getElementById('monitorRiskToggle');
                if (toggle) toggle.checked = !!(rule && rule.enabled);

                const slInputRef = document.getElementById('monitorStopLoss');
                const tpInputRef = document.getElementById('monitorTakeProfit');

                const posQty = Number(positionQty) || 0;
                const avg = Number(avgCost) || 0;
                const setFromAvg = async () => {
                    if (!slInputRef || !tpInputRef) return;
                    if (avg > 0 && posQty !== 0) {
                        let stopLossRatio = 0.05, takeProfitRatio = 0.05, lev = null;
                        try {
                            const cfg = await this.apiCall('/risk-config');
                            stopLossRatio = cfg.stop_loss_ratio || 0.025;
            takeProfitRatio = cfg.take_profit_ratio || 0.025;
                            const leverageMap = (cfg && cfg.leverage) ? cfg.leverage : {};
                            lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || null;
                        } catch (e) { /* keep defaults */ }
                        const adjSL = lev && lev > 0 ? (stopLossRatio / lev) : stopLossRatio;
                        const adjTP = lev && lev > 0 ? (takeProfitRatio / lev) : takeProfitRatio;
                        if (posQty > 0) {
                            // 多头：SL低于成本、TP高于成本
                            slInputRef.value = this.correctPriceStep(avg * (1 - adjSL), symbol);
                            tpInputRef.value = this.correctPriceStep(avg * (1 + adjTP), symbol);
                        } else {
                            // 空头：SL高于成本、TP低于成本
                            slInputRef.value = this.correctPriceStep(avg * (1 + adjSL), symbol);
                            tpInputRef.value = this.correctPriceStep(avg * (1 - adjTP), symbol);
                        }
                    } else {
                        slInputRef.value = '';
                        tpInputRef.value = '';
                    }
                };

                if (rule && rule.enabled) {
                    if (slInputRef) slInputRef.value = rule.stop_loss_price ?? '';
                    if (tpInputRef) tpInputRef.value = rule.take_profit_price ?? '';
                    if (this.currentMonitor) {
                        const requestedSpan = getMonitorDurationMinutes(rule.max_time_span);
                        const durationLimit = getMonitorDurationMinutes(durationSelectEl?.value);
                        const baseSpan = Number.isFinite(requestedSpan) && requestedSpan > 0
                            ? requestedSpan
                            : this.currentMonitor.maxTimeSpan;
                        const effectiveSpan = Number.isFinite(durationLimit) && durationLimit > 0
                            ? Math.min(baseSpan, durationLimit)
                            : baseSpan;
                        this.currentMonitor.maxTimeSpan = Math.max(1, effectiveSpan);
                    }

                    // 显示激活的风险规则信息
                    this.showActiveRiskRule(rule);
                } else {
                    await setFromAvg();
                    // 隐藏激活风险规则显示
                    this.hideActiveRiskRule();
                }
                
                // 预填充后立即更新差值显示
                this.updateExpectedPnL();
            } catch (prefillErr) {
                console.warn('Prefill risk rule failed:', prefillErr);
                const toggle = document.getElementById('monitorRiskToggle');
                if (toggle) toggle.checked = false;
                // 读取失败时亦按成本价设置默认值
                const slInputRef = document.getElementById('monitorStopLoss');
                const tpInputRef = document.getElementById('monitorTakeProfit');
                const posQty = Number(positionQty) || 0;
                const avg = Number(avgCost) || 0;
                if (slInputRef && tpInputRef && avg > 0 && posQty !== 0) {
                    let stopLossRatio = 0.05, takeProfitRatio = 0.05, lev = null;
                    try {
                        const cfg = await this.apiCall('/risk-config');
                        stopLossRatio = cfg.stop_loss_ratio || 0.025;
                        takeProfitRatio = cfg.take_profit_ratio || 0.025;
                        const leverageMap = (cfg && cfg.leverage) ? cfg.leverage : {};
                        lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || null;
                    } catch (e) { 
                        console.warn('Failed to load risk config for fallback calculation:', e);
                        /* keep defaults */ 
                    }
                    const adjSL = lev && lev > 0 ? (stopLossRatio / lev) : stopLossRatio;
                    const adjTP = lev && lev > 0 ? (takeProfitRatio / lev) : takeProfitRatio;
                    if (posQty > 0) {
                        slInputRef.value = this.correctPriceStep(avg * (1 - adjSL), symbol);
                        tpInputRef.value = this.correctPriceStep(avg * (1 + adjTP), symbol);
                    } else {
                        slInputRef.value = this.correctPriceStep(avg * (1 + adjSL), symbol);
                        tpInputRef.value = this.correctPriceStep(avg * (1 - adjTP), symbol);
                    }
                    
                    // 预填充后立即更新差值显示
                    this.updateExpectedPnL();
                }
            }

            // 显示模态框
            const modalEl = document.getElementById('monitorModal');
            if (modalEl) {
                const modal = new bootstrap.Modal(modalEl);
                modal.show();

                if (this._monitorFullscreenChangeHandler) {
                    document.removeEventListener('fullscreenchange', this._monitorFullscreenChangeHandler);
                    this._monitorFullscreenChangeHandler = null;
                }
                this.monitorModalFullscreen = false;
                modalEl.classList.remove('monitor-fullscreen');

                // 检查monitorTrendChart初始化状态
                console.log('[Monitor Modal] Modal shown, checking monitorTrendChart status:');
                console.log('[Monitor Modal] monitorTrendChart exists:', !!this.monitorTrendChart);
                console.log('[Monitor Modal] monitorTrendData length:', this.monitorTrendData ? this.monitorTrendData.length : 'undefined');
                if (this.monitorTrendChart) {
                    console.log('[Monitor Modal] monitorTrendChart data length:', this.monitorTrendChart.data ? this.monitorTrendChart.data.datasets[0].data.length : 'no data');
                } else {
                    console.warn('[Monitor Modal] monitorTrendChart is not initialized!');
                }

                if (this.domManager && typeof this.domManager.updateToggleButton === 'function') {
                    this.domManager.updateToggleButton();
                }
                if (this.domManager && this.domManager.active && typeof this.domManager.connectWS === 'function') {
                    this.domManager.shouldReconnect = true;
                    this.domManager.connectWS();
                }

                // 重置缩放按钮事件
                const resetZoomBtn = document.getElementById('resetChartZoomBtn');
                if (resetZoomBtn) {
                    resetZoomBtn.onclick = () => {
                        if (this.monitorChart) {
                            this.monitorChart.resetZoom();
                        }
                        if (this.monitorTrendChart) {
                            this.monitorTrendChart.resetZoom();
                        }
                    };
                }

                // 图表类型切换事件
                const chartTypeRadios = document.querySelectorAll('input[name="chartType"]');
                chartTypeRadios.forEach(radio => {
                    radio.onchange = () => {
                        if (radio.checked) {
                            this.loadMonitorChart(symbol);
                        }
                    };
                });

                let updateFullscreenButton = null;
                const fullscreenBtn = document.getElementById('monitorFullscreenBtn');
                if (fullscreenBtn) {
                    updateFullscreenButton = (active) => {
                        fullscreenBtn.classList.toggle('monitor-fullscreen-active', active);
                        fullscreenBtn.classList.toggle('monitor-fullscreen-inactive', !active);
                        const icon = fullscreenBtn.querySelector('i');
                        if (icon) {
                            icon.classList.toggle('fa-expand', !active);
                            icon.classList.toggle('fa-compress', active);
                        }
                        fullscreenBtn.setAttribute('title', active ? '退出全屏' : '全屏');
                    };
                    updateFullscreenButton(false);

                    const enterFullscreen = async () => {
                        modalEl.classList.add('monitor-fullscreen');
                        try {
                            if (modalEl.requestFullscreen) {
                                await modalEl.requestFullscreen();
                            } else if (modalEl.webkitRequestFullscreen) {
                                await modalEl.webkitRequestFullscreen();
                            }
                        } catch (err) {
                            console.warn('requestFullscreen failed', err);
                        }
                        this.monitorModalFullscreen = true;
                        updateFullscreenButton(true);
                    };

                    const exitFullscreen = async () => {
                        await this.exitMonitorFullscreen({ removeListener: false });
                        updateFullscreenButton(false);
                    };

                    fullscreenBtn.onclick = async () => {
                        const isFullscreen = document.fullscreenElement === modalEl
                            || document.webkitFullscreenElement === modalEl;
                        if (this.monitorModalFullscreen || modalEl.classList.contains('monitor-fullscreen') || isFullscreen) {
                            await exitFullscreen();
                        } else {
                            await enterFullscreen();
                        }
                    };

                    this._monitorFullscreenChangeHandler = () => {
                        const activeElement = document.fullscreenElement || document.webkitFullscreenElement;
                        if (activeElement === modalEl) {
                            this.monitorModalFullscreen = true;
                            modalEl.classList.add('monitor-fullscreen');
                            updateFullscreenButton(true);
                        } else {
                            this.monitorModalFullscreen = false;
                            modalEl.classList.remove('monitor-fullscreen');
                            updateFullscreenButton(false);
                        }
                    };
                    document.addEventListener('fullscreenchange', this._monitorFullscreenChangeHandler);
                }

                if (this.monitorRealtimeResumeSymbol && this.monitorRealtimeResumeSymbol !== symbol) {
                    this.monitorRealtimeShouldResume = false;
                    this.monitorRealtimeResumeSymbol = null;
                }
                if (this.monitorRealtimeSymbol && this.monitorRealtimeSymbol !== symbol) {
                    this.unsubscribeMonitorRealtime({ keepPreference: false, reason: 'symbol-change' });
                }
                const shouldResumeRealtime = this.shouldResumeMonitorRealtime(symbol);

                const realtimeBtn = document.getElementById('monitorRealtimeBtn');
                if (realtimeBtn) {
                    const setActive = (active) => {
                        realtimeBtn.classList.toggle('btn-success', active);
                        realtimeBtn.classList.toggle('btn-info', !active);
                        const icon = realtimeBtn.querySelector('i');
                        if (icon) {
                            icon.classList.toggle('fa-play', !active);
                            icon.classList.toggle('fa-pause', active);
                        }
                        realtimeBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
                    };

                    realtimeBtn.onclick = async () => {
                        if (this.monitorRealtimeSymbol) {
                            this.unsubscribeMonitorRealtime({ reason: 'manual' });
                            setActive(false);
                        } else {
                            const subscribed = await this.subscribeMonitorRealtime(symbol);
                            setActive(subscribed && this.monitorRealtimeSymbol === symbol);
                        }
                    };

                    if (shouldResumeRealtime) {
                        const subscribed = await this.subscribeMonitorRealtime(symbol, { skipReload: true, reason: 'auto-resume' });
                        setActive(subscribed && this.monitorRealtimeSymbol === symbol);
                    } else {
                        setActive(this.monitorRealtimeSymbol === symbol);
                    }
                }

                const slInput = document.getElementById('monitorStopLoss');
                if (slInput) {
                    slInput.oninput = (e) => {
                        const correctedPrice = this.correctPriceStep(e.target.value, symbol);
                        if (correctedPrice !== parseFloat(e.target.value)) {
                            e.target.value = correctedPrice;
                        }
                        this.updateMonitorStopLossLine();
                        this.updateExpectedPnL();
                    };
                }
                const tpInput = document.getElementById('monitorTakeProfit');
                if (tpInput) {
                    tpInput.oninput = (e) => {
                        const correctedPrice = this.correctPriceStep(e.target.value, symbol);
                        if (correctedPrice !== parseFloat(e.target.value)) {
                            e.target.value = correctedPrice;
                        }
                        this.updateMonitorTakeProfitLine();
                        this.updateExpectedPnL();
                    };
                }

                const toggle = document.getElementById('monitorRiskToggle');
                if (toggle) {
                    // 切换启用/禁用
                    toggle.onchange = async () => {
                        try {
                            await this.toggleRiskRule(symbol, toggle.checked);
                            if (this.riskRules && this.riskRules[symbol]) {
                                this.riskRules[symbol].enabled = toggle.checked;
                            }
                        } catch (e) {
                            console.error('toggle risk rule failed:', e);
                            // revert UI on failure
                            toggle.checked = !toggle.checked;
                        }
                    };
                }

                const saveBtn = document.getElementById('saveMonitorRiskBtn');
                if (saveBtn) {
                    saveBtn.onclick = async () => {
                        const sl = slInput?.value ? parseFloat(slInput.value) : null;
                        const tp = tpInput?.value ? parseFloat(tpInput.value) : null;

                        if (!sl && !tp) {
                            this.showAlert('warning', '请至少输入止损或止盈价');
                            return;
                        }

                        try {
                            await this.apiCall('/risk_rules', {
                                method: 'POST',
                                body: JSON.stringify({
                                    symbol: symbol,
                                    stop_loss_price: sl,
                                    take_profit_price: tp,
                                    max_time_span: this.currentMonitor?.maxTimeSpan
                                })
                            });
                            // 更新本地缓存
                            if (!this.riskRules) this.riskRules = {};
                            this.riskRules[symbol] = Object.assign({}, this.riskRules[symbol] || {}, {
                                symbol,
                                stop_loss_price: sl,
                                take_profit_price: tp,
                                max_time_span: this.currentMonitor?.maxTimeSpan,
                            });
                            this.showAlert('success', '已保存风险规则');
                        } catch (err) {
                            console.error('保存风险规则失败:', err);
                            this.showAlert('danger', `保存失败: ${err.message || err}`);
                        }
                    };
                }

                // 平仓按钮事件
                const closePositionBtn = document.getElementById('closePositionBtn');
                if (closePositionBtn) {
                    closePositionBtn.onclick = () => this.closePosition(symbol, {
                        positionQty: this.currentMonitor?.positionQty ?? 0
                    });
                }
                
                // 反向下单按钮事件
                const reversePositionBtn = document.getElementById('reversePositionBtn');
                if (reversePositionBtn) {
                    reversePositionBtn.onclick = async () => {
                        if (!this.currentMonitor || this.currentMonitor.positionQty === 0) {
                            this.showAlert('warning', '当前无持仓，无法反向下单');
                            return;
                        }
                        
                        const confirmed = confirm(`确认反向下单 ${symbol}？\n当前持仓: ${this.currentMonitor.positionQty}\n将执行反向操作（2倍持仓量，完全反转头寸）`);
                        if (!confirmed) return;
                        
                        try {
                            const response = await this.apiCall('/reverse_position', {
                                method: 'POST',
                                body: JSON.stringify({ symbol })
                            });
                            
                            if (response.success) {
                                this.showAlert('success', `反向订单已提交: ${response.action} ${response.quantity} ${symbol} (订单ID: ${response.order_id})`);
                                // 刷新持仓和订单数据
                                await this.loadPortfolio();
                                this.loadOrders();

                                // 同步弹窗右侧区域数据
                                await this.syncMonitorModalData(symbol);
                            } else {
                                this.showAlert('danger', `反向下单失败: ${response.message || '未知错误'}`);
                            }
                        } catch (err) {
                            console.error('反向下单失败:', err);
                            this.showAlert('danger', `反向下单失败: ${err.message || err}`);
                        }
                    };
                }

                // 关闭时释放资源并根据用户偏好保留实时订阅状态
                modalEl.addEventListener('hidden.bs.modal', async () => {
                    const keepPreference = this.shouldResumeMonitorRealtime(symbol);
                    this.unsubscribeMonitorRealtime({ keepPreference, reason: 'modal-hidden' });
                    if (typeof updateFullscreenButton === 'function') {
                        updateFullscreenButton(false);
                    }
                    await this.exitMonitorFullscreen({ removeListener: true });
                }, { once: true });

                // 订单状态过滤器事件绑定
                const orderStatusRadios = document.querySelectorAll('input[name="orderStatusFilter"]');
                orderStatusRadios.forEach(radio => {
                    radio.onchange = () => {
                        if (radio.checked) {
                            this.loadMonitorOrdersList(symbol, radio.value);
                        }
                    };
                });
                
                const chartLoadOptions = shouldResumeRealtime ? { useCache: false, forceRefresh: true } : undefined;

                // 初次载入图表
                this.loadMonitorChart(symbol, chartLoadOptions);

                const intervalSelectEl = document.getElementById('monitorInterval');
                const durationSelectEl = document.getElementById('monitorDuration');

                refreshMonitorDurationSelect(intervalSelectEl?.value, {
                    preferredValue: durationSelectEl?.value
                });

                const bindMonitorControl = (element) => {
                    if (!element || element.dataset.monitorBound === '1') return;
                    element.addEventListener('change', () => {
                        if (element.id === 'monitorInterval') {
                            refreshMonitorDurationSelect(element.value, {
                                preferredValue: null,
                                triggerChange: false
                            });
                        }
                        const activeSymbol = (this.currentMonitor?.symbol || symbol || '').trim();
                        if (!activeSymbol) return;
                        const barSize = document.getElementById('monitorInterval')?.value || '1 min';
                        const durationValue = document.getElementById('monitorDuration')?.value || '30 M';
                        if (this.chartDataCache) {
                            const cacheKey = `${activeSymbol}_${barSize}_${durationValue}`;
                            delete this.chartDataCache[cacheKey];
                        }
                        this.loadMonitorChart(activeSymbol, { useCache: false, forceRefresh: true });
                    });
                    element.dataset.monitorBound = '1';
                };

                bindMonitorControl(intervalSelectEl);
                bindMonitorControl(durationSelectEl);

                // 初始化预期PnL显示
                this.updateExpectedPnL();
                
                // 初次加载订单列表
                this.loadMonitorOrdersList(symbol, 'working');
            }
        } catch (e) {
            console.error('openMonitorModal failed:', e);
            this.showAlert('danger', `打开监控失败: ${e.message || e}`);
        }
    }

    async syncMonitorModalData(symbol) {
        try {
            // 从缓存的持仓数据中查找
            const positions = this.dataManager.getAllPositions();
            const position = positions.find(p => p.symbol === symbol);
            
            // 更新持仓信息（无论是否有持仓）
            const positionQtyEl = document.getElementById('monitorPositionQty');
            const avgCostEl = document.getElementById('monitorAvgCost');
            const currentPriceEl = document.getElementById('monitorCurrentPrice');
            const pnlEl = document.getElementById('monitorPnL');
            
            if (position) {
                // 有持仓时更新持仓信息
                if (positionQtyEl) positionQtyEl.textContent = position.position || '0';
                if (avgCostEl) avgCostEl.textContent = `$${parseFloat(position.avg_cost || 0).toFixed(2)}`;
                if (currentPriceEl) currentPriceEl.textContent = `$${parseFloat(position.market_price || 0).toFixed(2)}`;
                
                // 更新PnL
                const pnl = parseFloat(position.unrealized_pnl || 0);
                if (pnlEl) {
                    pnlEl.textContent = `$${pnl.toFixed(2)}`;
                    pnlEl.className = pnl >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
                }
                
                // 更新监控上下文（保持现有对象引用，只更新属性）
                if (!this.currentMonitor) {
                    this.currentMonitor = {};
                }
                this.currentMonitor.symbol = symbol;
                this.currentMonitor.positionQty = parseFloat(position.position || 0);
                this.currentMonitor.avgCost = parseFloat(position.avg_cost || 0);
                this.currentMonitor.currentPrice = parseFloat(position.market_price || 0);
                
                // 保留现有的止损止盈设置，不清空
                // const riskRuleSwitch = document.getElementById('monitorRiskRuleSwitch');
                // const stopLossInput = document.getElementById('monitorStopLoss');
                // const takeProfitInput = document.getElementById('monitorTakeProfit');
                
                // if (riskRuleSwitch) {
                //     riskRuleSwitch.checked = false;
                //     riskRuleSwitch.dispatchEvent(new Event('change'));
                // }
                
                // if (stopLossInput) stopLossInput.value = '';
                // if (takeProfitInput) takeProfitInput.value = '';
                
                // 重新计算推荐的SL/TP价格
                await this.updateRiskPrices();
                
                // 更新预期PnL显示
                this.updateExpectedPnL();
            } else {
                // 平仓后没有持仓时，清零显示
                if (positionQtyEl) positionQtyEl.textContent = '0';
                if (avgCostEl) avgCostEl.textContent = '$0.00';
                if (pnlEl) {
                    pnlEl.textContent = '$0.00';
                    pnlEl.className = 'fw-bold text-muted';
                }
                
                // 更新监控上下文
                if (!this.currentMonitor) {
                    this.currentMonitor = {};
                }
                this.currentMonitor.symbol = symbol;
                this.currentMonitor.positionQty = 0;
                this.currentMonitor.avgCost = 0;
                
                // 保留现有的止损止盈设置，不清空
                // const riskRuleSwitch = document.getElementById('monitorRiskRuleSwitch');
                // const stopLossInput = document.getElementById('monitorStopLoss');
                // const takeProfitInput = document.getElementById('monitorTakeProfit');
                
                // if (riskRuleSwitch) {
                //     riskRuleSwitch.checked = false;
                //     riskRuleSwitch.dispatchEvent(new Event('change'));
                // }
                
                // if (stopLossInput) stopLossInput.value = '';
                // if (takeProfitInput) takeProfitInput.value = '';
                
                // 隐藏激活的风险规则显示
                this.hideActiveRiskRule();
            }
            
            // 刷新订单列表
            const checkedRadio = document.querySelector('input[name="orderStatusFilter"]:checked');
            const currentStatus = checkedRadio ? checkedRadio.value : 'working';
            await this.loadMonitorOrdersList(symbol, currentStatus);
            
        } catch (error) {
            console.error('同步监控弹窗数据失败:', error);
        }
    }

    initOrdersDebounce() {
        return this.orderManager.initOrdersDebounce?.();
    }

    initMonitorOrdersDebounce() {
        return this.orderManager.initMonitorOrdersDebounce?.();
    }

    async loadMonitorOrdersList(symbol, status = 'working') {
        return this.orderManager.loadMonitorOrdersList(symbol, status);
    }

    async openOrderDetails(orderId) {
        try {
            // 验证订单ID
            if (!orderId || orderId.trim() === '') {
                this.showAlert('warning', '订单ID不能为空');
                return;
            }
            
            // 获取订单详情
            let result = await this.apiCall(`/orders/${orderId}`);
            
            if (!result || !result.order) {
                this.showAlert('warning', `订单 ${orderId} 不存在或已被删除`);
                return;
            }
            
            const orderData = result.order;
            
            // 填充模态框内容
            const modalBody = document.getElementById('orderDetailsModalBody');
            if (!modalBody) {
                this.showAlert('danger', '订单详情模态框未找到');
                return;
            }
            
            // 格式化时间显示
            const formatDateTime = (dateStr) => {
                if (!dateStr) return '—';
                try {
                    return new Date(dateStr).toLocaleString('zh-CN');
                } catch {
                    return dateStr;
                }
            };

            // 格式化价格显示
            const formatPrice = (price) => {
                return price && price > 0 ? `$${this.formatNumber(price)}` : '—';
            };

            const escapeText = (value) => {
                if (typeof this.escapeHtml === 'function') {
                    return this.escapeHtml(value);
                }
                if (value === null || value === undefined) return '';
                return String(value)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            };

            const formatText = (value) => {
                if (value === null || value === undefined) return '—';
                const str = String(value).trim();
                return str ? escapeText(str) : '—';
            };

            const formatTriggerType = (value) => {
                if (!value && value !== 0) return '—';
                const str = String(value).trim();
                if (!str) return '—';
                switch (str) {
                    case 'strategy':
                        return '策略';
                    case 'risk_rule':
                        return '风控';
                    case 'manual':
                        return '手动';
                    default:
                        return escapeText(str);
                }
            };

            const sourceHtml = (this.orderManager && typeof this.orderManager.formatOrderSource === 'function')
                ? this.orderManager.formatOrderSource(orderData)
                : (orderData.order_source ? escapeText(orderData.order_source) : '—');

            const rawStrategyName = orderData.strategy_name ?? orderData.strategy;
            const strategyHtml = formatText(rawStrategyName);

            const notesHtml = (() => {
                if (orderData.notes === null || orderData.notes === undefined) return '—';
                const str = String(orderData.notes);
                if (!str.trim()) return '—';
                return escapeText(str).replace(/\n/g, '<br>');
            })();

            // 构建详情HTML
            modalBody.innerHTML = `
                <div class="row g-3">
                    <div class="col-md-6">
                        <div class="card">
                            <div class="card-header"><strong>基本信息</strong></div>
                            <div class="card-body">
                                <p><strong>订单ID:</strong> ${orderData.order_id}</p>
                                <p><strong>股票代码:</strong> ${orderData.symbol}</p>
                                <p><strong>操作:</strong> ${orderData.action}</p>
                                <p><strong>数量:</strong> ${orderData.quantity}</p>
                                <p><strong>订单类型:</strong> ${orderData.order_type}</p>
                                <p><strong>状态:</strong> <span class="badge bg-${this.getStatusColor(orderData.status)}">${orderData.status}</span></p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="card">
                            <div class="card-header"><strong>价格信息</strong></div>
                            <div class="card-body">
                                <p><strong>限价:</strong> ${formatPrice(orderData.limit_price)}</p>
                                <p><strong>平均成交价:</strong> ${formatPrice(orderData.avg_fill_price)}</p>
                                <p><strong>已成交:</strong> ${orderData.filled || 0}</p>
                                <p><strong>剩余:</strong> ${orderData.remaining || 0}</p>
                                <p><strong>佣金:</strong> ${formatPrice(orderData.commission)}</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-12">
                        <div class="card">
                            <div class="card-header"><strong>其他信息</strong></div>
                            <div class="card-body">
                                <div class="row row-cols-1 row-cols-md-2 g-2">
                                    <div class="col">
                                        <p><strong>提交时间:</strong> ${formatDateTime(orderData.submit_time)}</p>
                                        <p><strong>更新时间:</strong> ${formatDateTime(orderData.updated_at)}</p>
                                        <p><strong>执行时间:</strong> ${formatDateTime(orderData.executed_at)}</p>
                                        <p><strong>订单来源:</strong> ${sourceHtml}</p>
                                        <p><strong>触发类型:</strong> ${formatTriggerType(orderData.trigger_type)}</p>
                                    </div>
                                    <div class="col">
                                        <p><strong>策略:</strong> ${strategyHtml}</p>
                                        <p><strong>规则ID:</strong> ${formatText(orderData.rule_id)}</p>
                                        <p><strong>父订单ID:</strong> ${formatText(orderData.parent_order_id)}</p>
                                        <p><strong>交易所:</strong> ${formatText(orderData.exchange)}</p>
                                        <p><strong>证券类型:</strong> ${formatText(orderData.sec_type)}</p>
                                    </div>
                                </div>
                                <p class="mt-3"><strong>备注:</strong> ${notesHtml}</p>
                            </div>
                        </div>
                    </div>
                </div>
            `;
            
            // 显示模态框
            const modal = new bootstrap.Modal(document.getElementById('orderDetailsModal'));
            modal.show();
            
        } catch (error) {
            console.error('Failed to load order details:', error);
            
            // 根据错误类型提供更友好的错误信息
            let errorMessage = '获取订单详情失败';
            if (error.message) {
                if (error.message.includes('404') || error.message.includes('not found')) {
                    errorMessage = `订单 ${orderId} 不存在`;
                } else if (error.message.includes('422') || error.message.includes('Unprocessable Entity')) {
                    errorMessage = `订单ID格式无效: ${orderId}`;
                } else if (error.message.includes('401') || error.message.includes('Authentication')) {
                    errorMessage = '请先登录';
                } else {
                    errorMessage = `获取订单详情失败: ${error.message}`;
                }
            }
            
            this.showAlert('danger', errorMessage);
        }
    }

    getMonitorTimezone() {
        if (!this.monitorChartTimezone) {
            let zone = null;
            if (typeof luxon !== 'undefined' && luxon && luxon.DateTime) {
                try {
                    zone = luxon.DateTime.local().zoneName;
                } catch (err) {
                    console.warn('Failed to resolve timezone from Luxon, falling back to Intl API.', err);
                }
            }
            if (!zone && typeof Intl !== 'undefined' && typeof Intl.DateTimeFormat === 'function') {
                try {
                    zone = new Intl.DateTimeFormat().resolvedOptions().timeZone;
                } catch (err) {
                    console.warn('Failed to resolve timezone from Intl API, using fallback timezone.', err);
                }
            }
            this.monitorChartTimezone = zone || MONITOR_FALLBACK_TZ;
        }
        return this.monitorChartTimezone;
    }

    parseMonitorChartTime(raw, options = {}) {
        const { skipDstAdjustment = false } = options || {};
        const buildFallbackDate = () => {
            if (raw == null || raw === '') {
                return new Date();
            }
            if (raw instanceof Date) {
                return raw;
            }
            if (typeof raw === 'number' && !Number.isNaN(raw)) {
                const value = raw > 1e10 ? raw : raw * 1000;
                const date = new Date(value);
                if (!Number.isNaN(date.getTime())) {
                    return date;
                }
            }
            if (typeof raw === 'string') {
                const trimmed = raw.trim();
                if (trimmed) {
                    const numeric = Number(trimmed);
                    if (!Number.isNaN(numeric)) {
                        const value = numeric > 1e10 ? numeric : numeric * 1000;
                        const date = new Date(value);
                        if (!Number.isNaN(date.getTime())) {
                            return date;
                        }
                    }
                    const parsed = new Date(trimmed);
                    if (!Number.isNaN(parsed.getTime())) {
                        return parsed;
                    }
                }
            }
            const fallback = new Date(raw);
            return Number.isNaN(fallback.getTime()) ? new Date() : fallback;
        };

        try {
            if (typeof luxon !== 'undefined' && luxon && luxon.DateTime) {
                const { DateTime } = luxon;
                const targetZone = this.getMonitorTimezone();
                let dt = null;

                if (raw instanceof Date) {
                    return raw;
                }

                if (typeof raw === 'string') {
                    const value = raw.trim();
                    if (value) {
                        if (/[zZ]|[+\-]\d{2}:?\d{2}$/.test(value)) {
                            dt = DateTime.fromISO(value);
                        }
                        if (!dt || !dt.isValid) {
                            dt = DateTime.fromISO(value, { zone: MONITOR_SOURCE_TZ });
                        }
                        if (!dt || !dt.isValid) {
                            dt = DateTime.fromSQL(value, { zone: MONITOR_SOURCE_TZ });
                        }
                        if (!dt || !dt.isValid) {
                            dt = DateTime.fromFormat(value, 'yyyy-MM-dd HH:mm:ss', { zone: MONITOR_SOURCE_TZ });
                        }
                        if (!dt || !dt.isValid) {
                            dt = DateTime.fromFormat(value, 'yyyyMMdd HH:mm:ss', { zone: MONITOR_SOURCE_TZ });
                        }
                        if (!dt || !dt.isValid) {
                            dt = DateTime.fromFormat(value, "yyyyMMdd'T'HHmmss", { zone: MONITOR_SOURCE_TZ });
                        }
                        if (!dt || !dt.isValid) {
                            dt = DateTime.fromFormat(value, 'yyyy-MM-dd', { zone: MONITOR_SOURCE_TZ });
                        }
                        if (!dt || !dt.isValid) {
                            dt = DateTime.fromFormat(value, 'yyyyMMdd', { zone: MONITOR_SOURCE_TZ });
                        }
                        if ((!dt || !dt.isValid) && !Number.isNaN(Number(value))) {
                            const numeric = Number(value);
                            const isSeconds = numeric < 1e12;
                            dt = isSeconds
                                ? DateTime.fromSeconds(numeric, { zone: 'utc' })
                                : DateTime.fromMillis(numeric, { zone: 'utc' });
                        }
                    }
                } else if (typeof raw === 'number' && !Number.isNaN(raw)) {
                    const isSeconds = raw < 1e12;
                    dt = isSeconds
                        ? DateTime.fromSeconds(raw, { zone: 'utc' })
                        : DateTime.fromMillis(raw, { zone: 'utc' });
                }

                if (dt && dt.isValid) {
                    const sourceTime = dt.setZone(MONITOR_SOURCE_TZ);
                    const isEasternDst = sourceTime.isValid ? sourceTime.isInDST : false;
                    let localTime = dt.setZone('local');
                    if (!skipDstAdjustment && isEasternDst) {
                        localTime = localTime.plus({ hours: 1 });
                    }
                    const dstLabel = isEasternDst ? (skipDstAdjustment ? 'DST' : 'DST +1h') : '标准';
                    // console.log(`[时间转换] 原始: ${raw} -> 本地时间: ${localTime.toFormat('yyyy-MM-dd HH:mm:ss')} (${dstLabel})`);
                    return localTime.toJSDate();
                }
            }
        } catch (error) {
            console.warn('Failed to parse monitor chart time with Luxon, falling back to native Date.', error, raw);
        }

        return buildFallbackDate();
    }

    // 添加图表数据缓存
    chartDataCache = {};
    
    async loadMonitorChart(symbol, options = {}) {
        try {
            this.pendingMonitorCandlestickUpgradeToken = null;
            const { useCache = true, forceRefresh = false } = options || {};
            const chartContainer = document.getElementById('monitorChartContainer');
            if (!chartContainer) return;

            // 显示加载状态
            chartContainer.innerHTML = `<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">加载图表数据中...</p></div>`;
            

            // 获取图表参数
            const intervalSelect = document.getElementById('monitorInterval');
            const durationSelect = document.getElementById('monitorDuration');
            const barSize = intervalSelect?.value || '1 min';
            const duration = durationSelect?.value || '30 M';
            
            // 生成缓存键
            const cacheKey = `${symbol}_${barSize}_${duration}`;
            
            // 检查缓存是否有效（5分钟内有效）
            const now = Date.now();
            const cachedData = this.chartDataCache[cacheKey];
            let response;
            
            if (useCache && !forceRefresh && cachedData && (now - cachedData.timestamp) < 5 * 60 * 1000) {
                console.log(`Using cached chart data for ${cacheKey}`);
                response = cachedData.data;
            } else {
                console.log(`Fetching fresh chart data for ${cacheKey}`);
                // 准备图表容器
                chartContainer.innerHTML = `<canvas id="monitorChart" width="900" height="600"></canvas>`;
                
                // 请求新数据
                try {
                    response = await this.apiCall('/chart_data', {
                        method: 'POST',
                        body: JSON.stringify({
                            symbol: symbol,
                            duration: duration,
                            bar_size: barSize,
                            data_type: 'TRADES',
                            use_cache: useCache,
                            force_refresh: forceRefresh
                        }),
                        allowReadOnly: true,
                        // 增加超时时间，略高于后端65秒
                        timeout: 70000
                    });
                    
                    // 更新缓存（仅当允许使用缓存且非强制刷新）
                    if (useCache && !forceRefresh) {
                        this.chartDataCache[cacheKey] = {
                            data: response,
                            timestamp: now
                        };
                    }
                } catch (error) {
                    console.error('Failed to fetch chart data:', error);
                    const msg = (error && error.message) ? error.message : '请稍后再试';
                    const extra = (/timeout|timed out|请求超时/i.test(String(msg)) || /AbortError/i.test(String(error?.name))) ? ' 可能在其他地方登录 IB，请退出后再试' : '';
                    chartContainer.innerHTML = `<div class=\"alert alert-danger\">获取图表数据失败: ${msg}${extra}</div>`;
                    return;
                }
            }
            
            // 准备图表容器
            chartContainer.innerHTML = `<canvas id="monitorChart" width="900" height="600"></canvas>`;

            if (!response.data || response.data.length === 0) {
                chartContainer.innerHTML = '<div class="alert alert-warning">暂无图表数据</div>';
                return;
            }

            const intervalKey = normalizeIntervalKey(barSize) || '1min';
            const chartIntervalMinutes = INTERVAL_MINUTES_MAP[intervalKey] || 1;
            const durationMinutes = getMonitorDurationMinutes(duration);
            const fallbackSpanMinutes = this.currentMonitor?.maxTimeSpan || MONITOR_CHART_MAX_MINUTES;
            const spanLimitMinutes = Number.isFinite(durationMinutes) && durationMinutes > 0
                ? durationMinutes
                : fallbackSpanMinutes;
            const maxSpanByPoints = MONITOR_CHART_MAX_POINTS * chartIntervalMinutes;
            const effectiveSpanMinutes = Math.max(
                1,
                Math.min(
                    (Number.isFinite(spanLimitMinutes) && spanLimitMinutes > 0) ? spanLimitMinutes : MONITOR_CHART_MAX_MINUTES,
                    maxSpanByPoints
                )
            );

            const chartSpanMinutes = effectiveSpanMinutes;

            const normalizedBars = response.data.map((bar, index) => {
                const raw = bar.time !== undefined && bar.time !== null ? bar.time : bar.date;
                const parsedTime = this.parseMonitorChartTime(raw);
                return {
                    bar,
                    index,
                    parsedTime: parsedTime instanceof Date && !Number.isNaN(parsedTime.getTime())
                        ? parsedTime
                        : null,
                };
            });

            const validBars = normalizedBars.filter(item => item.parsedTime);
            if (validBars.length) {
                const lastTimeMs = validBars[validBars.length - 1].parsedTime.getTime();
                const minAllowedTime = lastTimeMs - effectiveSpanMinutes * 60 * 1000;
                let trimmed = validBars.filter(item => item.parsedTime.getTime() >= minAllowedTime);
                if (!trimmed.length) {
                    trimmed = validBars.slice(-1);
                }
                if (trimmed.length > MONITOR_CHART_MAX_POINTS) {
                    trimmed = trimmed.slice(trimmed.length - MONITOR_CHART_MAX_POINTS);
                }
                response.data = trimmed.map(item => item.bar);
            } else if (response.data.length > MONITOR_CHART_MAX_POINTS) {
                response.data = response.data.slice(response.data.length - MONITOR_CHART_MAX_POINTS);
            }

            // Wait a bit for DOM to update
            this.pendingMonitorChartRender = async () => {
                const ctx = document.getElementById('monitorChart')?.getContext('2d');
                if (!ctx) {
                    return;
                }

                // Prepare data for Chart.js
                // 先解析原始时间并保存Date对象
                const timeValues = response.data.map(bar => {
                    const raw = (bar.time !== undefined && bar.time !== null) ? bar.time : bar.date;
                    return this.parseMonitorChartTime(raw);
                });
                // 根据时间生成显示标签
                const labels = timeValues.map(date => {
                    const interval = intervalSelect?.value || '5 mins';
                    if (interval.includes('day') || duration.includes('M') || duration.includes('Y')) {
                        return date.toLocaleDateString(undefined, {
                            month: 'short',
                            day: 'numeric'
                        });
                    } else if (interval.includes('hour') || duration === '1 W') {
                        return date.toLocaleString(undefined, {
                            month: 'short',
                            day: 'numeric',
                            hour: '2-digit',
                            hour12: false
                        });
                    } else {
                        return date.toLocaleTimeString(undefined, {
                            hour: '2-digit',
                            minute: '2-digit',
                            hour12: false
                        });
                    }
                });
                // 确保数据完整性，过滤掉无效的价格数据，并记录索引
                const validData = [];
                const validIndices = [];
                response.data.forEach((bar, idx) => {
                    if (bar.close != null && !isNaN(bar.close)) {
                        validData.push(bar);
                        validIndices.push(idx);
                    }
                });
                const prices = validData.map(bar => bar.close);

                // 重新生成对应的labels和时间
                const validLabels = validIndices.map(i => labels[i]);
                const validTimes = validIndices.map(i => timeValues[i]);

                // 为了在建仓后显示盈亏方框，右侧额外添加时间空间
                const intervalValueRaw = intervalSelect?.value || '1 min';
                const intervalKeyForPadding = normalizeIntervalKey(intervalValueRaw) || '1min';
                const padIntervalMinutes = INTERVAL_MINUTES_MAP[intervalKeyForPadding] || chartIntervalMinutes;
                const padSpanMinutes = Math.min(chartSpanMinutes, MONITOR_CHART_MAX_MINUTES);
                const padBars = Math.ceil(padSpanMinutes / padIntervalMinutes);
                const lastBar = response.data[response.data.length - 1];
                // 打印chart数据数组中的最后一个对象
                console.log('chart数据数组中的最后一个对象:', {
                    lastBar,
                    dataLength: response.data.length,
                    currentTime: new Date().toISOString()
                });
                const lastRaw = (lastBar.time !== undefined && lastBar.time !== null) ? lastBar.time : lastBar.date;
                const lastDate = this.parseMonitorChartTime(lastRaw);
                // 打印解析后的最后一个数据点时间
                console.log('解析后的最后一个数据点时间:', {
                    lastRaw,
                    lastDate,
                    lastDateISO: lastDate.toISOString(),
                    currentTime: new Date().toISOString()
                });
                const futureTimes = [];
                for (let i = 1; i <= padBars; i++) {
                    const future = new Date(lastDate.getTime() + i * padIntervalMinutes * 60000);
                    futureTimes.push(future);
                    let label;
                    if (intervalValueRaw.includes('day') || duration.includes('M') || duration.includes('Y')) {
                        label = future.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
                    } else if (intervalValueRaw.includes('hour') || duration === '1 W') {
                        label = future.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', hour12: false });
                    } else {
                        label = future.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
                    }
                    labels.push(label);
                    validLabels.push(label);
                    prices.push(null);
                    timeValues.push(future);
                    validTimes.push(future);
                }

                // Get stop loss and take profit values for annotations
                const stopLossInput = document.getElementById('monitorStopLoss');
                const takeProfitInput = document.getElementById('monitorTakeProfit');
                const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;
                const takeProfitPrice = takeProfitInput?.value ? parseFloat(takeProfitInput.value) : null;

                // Get current price from chart data (latest price) and average cost from monitor context
                const latestChartPrice = prices.length > 0 ? prices[prices.length - 1] : 0;
                const currentPrice = latestChartPrice || this.currentMonitor?.currentPrice || 0;
                const avgCost = this.currentMonitor?.avgCost || 0;
                
                // Update monitor context with latest chart price
                if (this.currentMonitor && latestChartPrice > 0) {
                    this.currentMonitor.currentPrice = latestChartPrice;
                    // Update the display element as well
                    const currentPriceEl = document.getElementById('monitorCurrentPrice');
                    if (currentPriceEl) {
                        currentPriceEl.textContent = `$${latestChartPrice.toFixed(2)}`;
                    }
                }

                // Calculate and display asset change (daily change)
                if (validData.length > 0) {
                    const todayOpenPrice = validData[0].open; // First bar's open price
                    const currentClosePrice = latestChartPrice;
                    
                    // Store today's open price in currentMonitor for real-time updates
                    if (this.currentMonitor) {
                        this.currentMonitor.todayOpenPrice = todayOpenPrice;
                    }
                    
                    if (todayOpenPrice && currentClosePrice) {
                        const assetChange = currentClosePrice - todayOpenPrice;
                        const assetChangePercent = (assetChange / todayOpenPrice) * 100;
                        
                        // Update asset change display
                        const assetChangeEl = document.getElementById('monitorAssetChange');
                        const assetChangePercentEl = document.getElementById('monitorAssetChangePercent');
                        
                        if (assetChangeEl) {
                            const changeText = assetChange >= 0 ? `+$${assetChange.toFixed(2)}` : `-$${Math.abs(assetChange).toFixed(2)}`;
                            assetChangeEl.textContent = changeText;
                            assetChangeEl.className = assetChange >= 0 ? 'text-success' : 'text-danger';
                        }
                        
                        if (assetChangePercentEl) {
                            const percentText = assetChangePercent >= 0 ? `+${assetChangePercent.toFixed(2)}%` : `${assetChangePercent.toFixed(2)}%`;
                            assetChangePercentEl.textContent = percentText;
                            assetChangePercentEl.className = assetChangePercent >= 0 ? 'text-success' : 'text-danger';
                        }
                    }
                }

                // 获取标的最小价格单位（根据symbol类型确定）
                const getMinPriceStep = (symbol) => {
                    if (symbol.includes('NQ') || symbol.includes('MNQ')) {
                        return 0.25; // 纳斯达克期货最小单位
                    } else if (symbol.includes('ES') || symbol.includes('MES')) {
                        return 0.25; // 标普500期货最小单位
                    } else {
                        return 0.01; // 股票默认最小单位
                    }
                };
                const minStep = getMinPriceStep(symbol);

                // 检查当前选择的图表类型
                const chartTypeRadio = document.querySelector('input[name="chartType"]:checked');
                const rawChartType = chartTypeRadio ? chartTypeRadio.value : 'line';
                const chartType = typeof rawChartType === 'string'
                    ? rawChartType.trim().toLowerCase()
                    : 'line';
                const isCandlestick = chartType === 'candlestick';

                if (isCandlestick) {
                    const pluginStatus = await this.waitForCandlestickPlugin();
                    if (!pluginStatus.ready) {
                        if (pluginStatus.timedOut && pluginStatus.promise) {
                            const upgradeToken = Symbol('monitor-candlestick-upgrade');
                            this.pendingMonitorCandlestickUpgradeToken = upgradeToken;
                            pluginStatus.promise
                                .then((result) => {
                                    if (!result || this.pendingMonitorCandlestickUpgradeToken !== upgradeToken) {
                                        return;
                                    }
                                    this.pendingMonitorCandlestickUpgradeToken = null;
                                    this.loadMonitorChart(symbol, {
                                        ...options,
                                        useCache: true,
                                        forceRefresh: false,
                                    });
                                })
                                .catch(() => {});
                        }
                        chartContainer.innerHTML = '<div class="alert alert-warning mb-0">蜡烛图插件加载中，将在可用时自动刷新。</div>';
                        return;
                    }
                }

                // 计算时间跨度相关参数
                const spanBars = Math.min(
                    MONITOR_CHART_MAX_POINTS,
                    Math.max(1, Math.round(chartSpanMinutes / chartIntervalMinutes))
                );

                // 根据图表类型选择对应的标签和时间数组
                const displayLabels = isCandlestick ? labels : validLabels;
                const displayTimes = isCandlestick ? timeValues : validTimes;

                const normalizedTimes = displayTimes.map(value => {
                    if (value instanceof Date) {
                        return new Date(value.getTime());
                    }
                    const parsed = this.parseMonitorChartTime(value);
                    return parsed instanceof Date ? parsed : new Date(value);
                });

                let chartTimeRange = this.currentMonitor?.chartTimeRange;

                if (this.currentMonitor) {
                    this.currentMonitor.displayTimes = normalizedTimes;
                    this.currentMonitor.displayLabels = Array.from(displayLabels);
                    this.currentMonitor.chartType = chartType;
                }

                // 打印处理后的时间数组信息
                console.log('处理后的displayTimes数组信息:', {
                    displayTimesLength: normalizedTimes.length,
                    firstTime: normalizedTimes[0]?.toISOString(),
                    lastTime: normalizedTimes[normalizedTimes.length-1]?.toISOString(),
                    chartType
                });

                // 使用最近持仓订单的成交时间作为方框起点
                let startIndex = 0;
                if (this.currentMonitor?.lastOrderTime) {
                    const lastOrderTime = this.parseMonitorChartTime(this.currentMonitor.lastOrderTime);
                    // 打印作为起点时间的订单对象
                    console.log('作为起点时间的订单对象:', {
                        lastOrderTime: this.currentMonitor.lastOrderTime,
                        lastOrderTimeDate: lastOrderTime,
                        orders: this.currentMonitor.orders
                    });
                    startIndex = displayTimes.findIndex(t => t >= lastOrderTime);
                    if (startIndex === -1) startIndex = Math.max(0, displayTimes.length - spanBars - 1);
                } else if (this.currentMonitor?.entryTime) {
                    const entryTime = this.parseMonitorChartTime(this.currentMonitor.entryTime);
                    startIndex = displayTimes.findIndex(t => t >= entryTime);
                    if (startIndex === -1) startIndex = Math.max(0, displayTimes.length - spanBars - 1);
                } else {
                    // 如果没有入场时间，则默认显示最近的数据
                    startIndex = Math.max(0, displayTimes.length - spanBars - 1);
                }
                const endIndex = Math.min(displayLabels.length - 1, startIndex + spanBars);
                this.currentMonitor.riskStartIndex = startIndex;

                // 打印最终确定的起点索引和对应的时间
                console.log('最终确定的起点索引和时间:', {
                    startIndex,
                    endIndex,
                    startTime: normalizedTimes[startIndex]?.toISOString(),
                    endTime: normalizedTimes[endIndex]?.toISOString(),
                    spanBars
                });

                if (this.currentMonitor) {
                    const totalPoints = normalizedTimes.length;
                    const clampIndex = (idx) => {
                        if (!Number.isFinite(idx)) return 0;
                        return Math.max(0, Math.min(totalPoints - 1, idx));
                    };

                    let minIdx;
                    let maxIdx;
                    if (isCandlestick && displayRange && typeof displayRange.minIndex === 'number' && typeof displayRange.maxIndex === 'number') {
                        minIdx = clampIndex(displayRange.minIndex);
                        maxIdx = clampIndex(displayRange.maxIndex);
                    } else {
                        minIdx = clampIndex(startIndex);
                        maxIdx = clampIndex(endIndex);
                    }

                    this.currentMonitor.displayStartIndex = minIdx;
                    this.currentMonitor.displayEndIndex = maxIdx;

                    const minTime = this.normalizeMonitorTime(normalizedTimes[minIdx]);
                    const maxTime = this.normalizeMonitorTime(normalizedTimes[maxIdx]);
                    if (minTime != null && maxTime != null) {
                        this.currentMonitor.chartTimeRange = { min: minTime, max: maxTime };
                    } else {
                        delete this.currentMonitor.chartTimeRange;
                    }
                    chartTimeRange = this.currentMonitor.chartTimeRange;
                }

                const clampLabelIndex = (idx) => {
                    if (!Number.isFinite(idx)) return 0;
                    if (!displayLabels.length) return 0;
                    return Math.max(0, Math.min(displayLabels.length - 1, Math.round(idx)));
                };

                const clampTimeIndex = (idx) => {
                    if (!Number.isFinite(idx)) return 0;
                    if (!normalizedTimes.length) return 0;
                    return Math.max(0, Math.min(normalizedTimes.length - 1, Math.round(idx)));
                };

                const getTimeValueForIndex = (idx) => {
                    if (!normalizedTimes.length) return null;
                    const clamped = clampTimeIndex(idx);
                    const value = normalizedTimes[clamped];
                    if (value instanceof Date) return new Date(value.getTime());
                    if (typeof value === 'number' && Number.isFinite(value)) return new Date(value);
                    return null;
                };

                const getAxisValueForIndex = (idx) => {
                    if (isCandlestick) {
                        return getTimeValueForIndex(idx);
                    }
                    if (!displayLabels.length) return null;
                    return displayLabels[clampLabelIndex(idx)];
                };

                const rangeStartAxisValue = getAxisValueForIndex(startIndex);
                const rangeEndAxisValue = getAxisValueForIndex(endIndex);
                const rangeStartTimeMs = isCandlestick ? this.normalizeMonitorTime(rangeStartAxisValue) : null;
                const rangeEndTimeMs = isCandlestick ? this.normalizeMonitorTime(rangeEndAxisValue) : null;

                // Prepare annotations
                const annotations = {};
                if (stopLossPrice) {
                    annotations.stopLoss = {
                        type: 'line',
                        yMin: stopLossPrice,
                        yMax: stopLossPrice,
                        borderColor: 'rgb(255, 99, 132)',
                        borderWidth: 2,
                        borderDash: [5, 5],
                        // 使线条可拖动
                        adjustScaleRange: false,
                        scaleID: 'y',
                        value: stopLossPrice,
                        endValue: stopLossPrice,
                        label: {
                            content: `Stop Loss: $${stopLossPrice.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(255, 99, 132, 0.8)',
                            color: 'white',
                            padding: 4
                        },
                        // 拖动配置
                        enter: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'ns-resize';
                        },
                        leave: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'default';
                        }
                    };
                }
                if (takeProfitPrice) {
                    annotations.takeProfit = {
                        type: 'line',
                        yMin: takeProfitPrice,
                        yMax: takeProfitPrice,
                        borderColor: 'rgb(75, 192, 192)',
                        borderWidth: 2,
                        borderDash: [5, 5],
                        // 使线条可拖动
                        adjustScaleRange: false,
                        scaleID: 'y',
                        value: takeProfitPrice,
                        endValue: takeProfitPrice,
                        label: {
                            content: `Take Profit: $${takeProfitPrice.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(75, 192, 192, 0.8)',
                            color: 'white',
                            padding: 4
                        },
                        // 拖动配置
                        enter: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'ns-resize';
                        },
                        leave: function(ctx, event) {
                            ctx.chart.canvas.style.cursor = 'default';
                        }
                    };
                }

                // Add current price line
                if (currentPrice > 0) {
                    annotations.currentPrice = {
                        type: 'line',
                        yMin: currentPrice,
                        yMax: currentPrice,
                        borderColor: 'rgb(255, 206, 84)',
                        borderWidth: 2,
                        // 实线（无虚线配置）
                        label: {
                            content: `$${currentPrice.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(255, 206, 84, 0.8)',
                            color: 'black',
                            padding: 4
                        }
                    };
                }

                // Add average cost line
                if (avgCost > 0) {
                    annotations.avgCost = {
                        type: 'line',
                        yMin: avgCost,
                        yMax: avgCost,
                        borderColor: 'rgb(153, 102, 255)',
                        borderWidth: 2,
                        // 实线（无虚线配置）
                        label: {
                            content: `$${avgCost.toFixed(2)}`,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(153, 102, 255, 0.8)',
                            color: 'white',
                            padding: 4
                        }
                    };
                }

                const annotationRangeStart = isCandlestick
                    ? (rangeStartTimeMs ?? this.normalizeMonitorTime(getTimeValueForIndex(startIndex)))
                    : rangeStartAxisValue;
                const annotationRangeEnd = isCandlestick
                    ? (rangeEndTimeMs ?? this.normalizeMonitorTime(getTimeValueForIndex(endIndex)))
                    : rangeEndAxisValue;

                if (stopLossPrice && takeProfitPrice && annotationRangeStart != null && annotationRangeEnd != null) {
                    const isLong = (this.currentMonitor?.positionQty || 0) > 0;
                    const xMin = annotationRangeStart;
                    const xMax = annotationRangeEnd;
                    const profitColor = 'rgba(144,238,144,0.3)';
                    const lossColor = 'rgba(255,182,193,0.3)';

                    // 确保无论是多头还是空头，都能正确绘制盈亏方框
                    if (isLong) {
                        annotations.profitBox = { type: 'box', xMin, xMax, yMin: avgCost, yMax: takeProfitPrice, backgroundColor: profitColor, borderWidth: 0 };
                        annotations.lossBox = { type: 'box', xMin, xMax, yMin: stopLossPrice, yMax: avgCost, backgroundColor: lossColor, borderWidth: 0 };
                    } else {
                        // 空头情况：止盈价格低于成本，止损价格高于成本
                        annotations.profitBox = { type: 'box', xMin, xMax, yMin: takeProfitPrice, yMax: avgCost, backgroundColor: profitColor, borderWidth: 0 };
                        annotations.lossBox = { type: 'box', xMin, xMax, yMin: avgCost, yMax: stopLossPrice, backgroundColor: lossColor, borderWidth: 0 };
                    }

                    // 添加时间限制线
                    annotations.timeLimit = {
                        type: 'line',
                        xMin: xMax,
                        xMax: xMax,
                        value: xMax,
                        endValue: xMax,
                        borderColor: 'rgba(128,128,128,0.8)',
                        borderWidth: 2,
                        enter: (ctx) => { ctx.chart.canvas.style.cursor = 'ew-resize'; },
                        leave: (ctx) => { ctx.chart.canvas.style.cursor = 'default'; }
                    };

                    // 添加调试信息
                    console.log(`绘制${isLong ? '多头' : '空头'}盈亏方框: 成本=${avgCost}, 止损=${stopLossPrice}, 止盈=${takeProfitPrice}`);
                }
                
                // 移除了价格范围线段（最低价线和最高价线），只保留当前价格、平均成本、SL/TP价格线

                // Destroy existing charts if they exist
                if (this.monitorChart) {
                    // 清理键盘事件监听器
                    if (this.monitorChart._keyboardHandler) {
                        document.removeEventListener('keydown', this.monitorChart._keyboardHandler);
                    }
                    this.monitorChart.destroy();
                }

                // 根据图表宽度和数据量智能设置初始显示范围
                const getOptimalDisplayRange = (dataLength, chartType) => {
                    // 获取图表容器宽度
                    const chartContainer = document.getElementById('monitorChart')?.parentElement;
                    const containerWidthRaw = chartContainer ? chartContainer.offsetWidth : 0;
                    // 当容器尚未渲染完成或被隐藏时 offsetWidth 可能为 0，导致后续计算仅展示极少数据点
                    const fallbackWidth = chartContainer?.parentElement?.offsetWidth || window.innerWidth || 800;
                    const containerWidth = containerWidthRaw > 0 ? containerWidthRaw : fallbackWidth;

                    // 根据图表宽度计算合适的显示数据量
                    // 蜡烛图和折线图使用相同的像素间距，保持一致的显示数据量
                    const pixelsPerDataPoint = 10; // 统一像素间距
                    const optimalDataCount = Math.max(1, Math.floor(containerWidth / pixelsPerDataPoint));
                    // 减少显示数据量，为右边数据点留出空间
                    const maxDisplayCount = Math.min(
                        Math.max(1, Math.floor(optimalDataCount * 0.9)),
                        Math.max(15, Math.min(80, dataLength))
                    );

                    // 始终计算显示范围，确保蜡烛图和折线图使用相同的数据量
                    const actualDisplayCount = Math.max(1, Math.min(maxDisplayCount, dataLength));
                    const startIndex = Math.max(0, dataLength - actualDisplayCount);

                    return chartType === 'candlestick' ?
                        { minIndex: startIndex, maxIndex: dataLength - 1 } :
                        {
                            min: labels[startIndex], 
                            max: labels[dataLength - 1] 
                        };
                };
                
                const displayRange = getOptimalDisplayRange(labels.length, chartType);

                // 准备图表数据
                let chartConfig;
                let ohlcData; // 声明在外层作用域
                let maxVolume = 0; // 记录最大成交量以限制柱状图高度
                let candlestickMinIdx = 0;
                let candlestickMaxIdx = 0;
                let candlestickStep = 60000;
                let candlestickVisibleCount = 1;
                let candlestickFirst = null;
                let candlestickLast = null;

                if (isCandlestick) {
                    const toTimestamp = (raw) => {
                        const parsed = this.parseMonitorChartTime(raw);
                        if (parsed && !Number.isNaN(parsed.getTime())) {
                            return parsed.getTime();
                        }
                        return NaN;
                    };

                    ohlcData = validData.map((bar) => {
                        const raw = (bar.time !== undefined && bar.time !== null) ? bar.time : bar.date;
                        const timestamp = toTimestamp(raw);
                        return {
                            x: timestamp,
                            o: parseFloat(bar.open ?? bar.close ?? 0) || 0,
                            h: parseFloat(bar.high ?? bar.close ?? 0) || 0,
                            l: parseFloat(bar.low ?? bar.close ?? 0) || 0,
                            c: parseFloat(bar.close ?? 0) || 0,
                            volume: parseFloat(bar.volume ?? 0) || 0
                        };
                    }).filter(item => Number.isFinite(item.x) && item.c > 0);

                    candlestickMinIdx = Math.max(0, Math.min(ohlcData.length - 1, (displayRange && typeof displayRange.minIndex === 'number') ? displayRange.minIndex : 0));
                    candlestickMaxIdx = Math.max(0, Math.min(ohlcData.length - 1, (displayRange && typeof displayRange.maxIndex === 'number') ? displayRange.maxIndex : (ohlcData.length - 1)));
                    candlestickStep = ohlcData.length > 1 ? Math.max(1, Math.abs(ohlcData[1].x - ohlcData[0].x)) : 60000;
                    candlestickVisibleCount = Math.max(1, candlestickMaxIdx - candlestickMinIdx + 1);
                    const firstDataTime = ohlcData[0]?.x ?? null;
                    const lastDataTime = ohlcData[ohlcData.length - 1]?.x ?? null;
                    const paddedStartTime = Number.isFinite(rangeStartTimeMs) ? rangeStartTimeMs : this.normalizeMonitorTime(getTimeValueForIndex(startIndex));
                    const paddedEndTime = Number.isFinite(rangeEndTimeMs) ? rangeEndTimeMs : this.normalizeMonitorTime(getTimeValueForIndex(endIndex));
                    candlestickFirst = Number.isFinite(paddedStartTime) ? paddedStartTime : firstDataTime;
                    candlestickLast = Number.isFinite(paddedEndTime) ? paddedEndTime : lastDataTime;
                    if (!Number.isFinite(candlestickFirst) && Number.isFinite(firstDataTime)) {
                        candlestickFirst = firstDataTime;
                    }
                    if (!Number.isFinite(candlestickLast) && Number.isFinite(lastDataTime)) {
                        candlestickLast = lastDataTime;
                    }

                    console.log('OHLC data sample:', ohlcData.slice(0, 3));
                    console.log('Total OHLC points:', ohlcData.length);

                    const adapterReady = await this.ensureDateAdapter();
                    if (!adapterReady) {
                        console.warn('[loadMonitorChart] Date adapter unavailable, skipping candlestick render.');
                        chartContainer.innerHTML = '<div class="alert alert-warning mb-0">蜡烛图依赖的时间适配器加载失败，请刷新后重试。</div>';
                        return;
                    }

                    const candlestickConfig = this.createCandlestickChartConfig({
                        symbol,
                        ohlcData,
                        labels,
                        priceAxisId: 'y',
                        volumeAxisId: 'y1',
                        includeLabels: true,
                        priceDatasetOptions: {
                            // 添加hover配置以确保事件能正确触发
                            hoverBackgroundColor: 'rgba(255, 99, 132, 0.8)',
                            hoverBorderColor: 'rgba(255, 99, 132, 1)',
                            hoverBorderWidth: 2
                        },
                        volumeDatasetOptions: {
                            backgroundColor: 'rgba(75, 192, 192, 0.6)'
                        }
                    });
                    chartConfig = {
                        type: candlestickConfig.type,
                        data: candlestickConfig.data
                    };
                    maxVolume = candlestickConfig.meta.maxVolume;
                } else {
                    // 折线图配置
                    const volumeData = validData.map(item => parseFloat(item.volume || 0));
                    for (let i = 0; i < padBars; i++) {
                        volumeData.push(null);
                    }
                    maxVolume = Math.max(...volumeData.filter(v => v != null), 0);
                    chartConfig = {
                        type: 'line',
                        data: {
                            labels: chartType === 'line' ? validLabels : labels,
                            datasets: [
                                {
                                    label: `${symbol} Price`,
                                    data: chartType === 'line' ? prices : prices,
                                    borderColor: 'rgb(54, 162, 235)',
                                    backgroundColor: 'rgba(54, 162, 235, 0.1)',
                                    tension: 0.1,
                                    pointRadius: 3,
                                    pointHoverRadius: 5,
                                    pointBackgroundColor: 'white',
                                    pointBorderColor: 'rgb(54, 162, 235)',
                                    pointBorderWidth: 2,
                                    pointHoverBackgroundColor: 'white',
                                    pointHoverBorderColor: 'rgb(54, 162, 235)',
                                    pointHoverBorderWidth: 3,
                                    yAxisID: 'y'
                                },
                                {
                                    type: 'bar',
                                    label: 'Volume',
                                    data: volumeData,
                                    yAxisID: 'y1',
                                    backgroundColor: 'rgba(75, 192, 192, 0.6)'
                                }
                            ]
                        }
                    };
                }

                const resolvedCandlestickMin = isCandlestick
                    ? (Number.isFinite(chartTimeRange?.min) ? chartTimeRange.min : candlestickFirst)
                    : null;
                const resolvedCandlestickMax = isCandlestick
                    ? (Number.isFinite(chartTimeRange?.max) ? chartTimeRange.max : candlestickLast)
                    : null;
                const fallbackCandlestickMin = isCandlestick
                    ? (resolvedCandlestickMin ?? this.normalizeMonitorTime(getTimeValueForIndex(startIndex)) ?? (ohlcData[candlestickMinIdx]?.x ?? candlestickFirst ?? null))
                    : null;
                const fallbackCandlestickMax = isCandlestick
                    ? (resolvedCandlestickMax ?? this.normalizeMonitorTime(getTimeValueForIndex(endIndex)) ?? (ohlcData[candlestickMaxIdx]?.x ?? candlestickLast ?? null))
                    : null;

                // Create new chart
                this.monitorChart = new Chart(ctx, {
                    ...chartConfig,
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        interaction: {
                            intersect: isCandlestick ? true : false,
                            mode: isCandlestick ? 'nearest' : 'index'
                        },
                        plugins: {
                            title: {
                                display: true,
                                text: `${symbol} - Real-time Price Chart`
                            },
                            legend: {
                                display: isCandlestick
                            },
                            annotation: {
                                annotations: annotations
                            },
                            zoom: {
                                limits: {
                                    x: isCandlestick ?
                                        { min: resolvedCandlestickMin ?? fallbackCandlestickMin, max: resolvedCandlestickMax ?? fallbackCandlestickMax } :
                                        { min: 0, max: Math.max(10, labels.length - 1) },
                                    y: {min: 'original', max: 'original'}
                                },
                                pan: {
                                    enabled: true,
                                    mode: 'x',
                                    modifierKey: null,
                                    threshold: 5,
                                    rangeMin: isCandlestick ?
                                        { x: resolvedCandlestickMin ?? fallbackCandlestickMin } : { x: 0 },
                                    rangeMax: isCandlestick ?
                                        { x: resolvedCandlestickMax ?? fallbackCandlestickMax } : { x: Math.max(10, labels.length - 1) }
                                },
                                zoom: {
                                    wheel: {
                                        enabled: true,
                                        modifierKey: null,
                                        speed: isCandlestick ? 0.05 : 0.1
                                    },
                                    pinch: {
                                        enabled: true
                                    },
                                    mode: 'x',
                                    limits: isCandlestick ? {
                                        x: {
                                            min: candlestickVisibleCount * candlestickStep,
                                            max: (() => {
                                                const minBound = resolvedCandlestickMin ?? fallbackCandlestickMin ?? candlestickFirst ?? 0;
                                                const maxBound = resolvedCandlestickMax ?? fallbackCandlestickMax ?? candlestickLast ?? 0;
                                                const span = maxBound - minBound;
                                                return span > 0 ? span : candlestickVisibleCount * candlestickStep;
                                            })()
                                        }
                                    } : {
                                        x: {
                                            min: Math.max(5, Math.floor(labels.length * 0.05)),
                                            max: Math.max(10, labels.length)
                                        }
                                    }
                                }
                                ,
                                onZoom: ({chart}) => this.syncCharts(chart, this.monitorTrendChart),
                                onPan: ({chart}) => this.syncCharts(chart, this.monitorTrendChart)
                            }
                        },
                        scales: {
                            x: {
                                display: true,
                                title: {
                                    display: true,
                                    text: 'Time'
                                },
                                // 蜡烛图使用时间轴
                                ...(isCandlestick ? {
                                    type: 'time',
                                    adapters: {
                                        date: {
                                            locale: 'zh-CN'
                                        }
                                    },
                                    time: {
                                        unit: 'minute',
                                        displayFormats: {
                                            minute: 'HH:mm',
                                            hour: 'HH:mm',
                                            day: 'MMM dd'
                                        }
                                    },
                                    ticks: {
                                        maxTicksLimit: 20,
                                        autoSkip: true,
                                        maxRotation: 45
                                    },
                                    // 蜡烛图的显示范围控制
                                    min: resolvedCandlestickMin ?? fallbackCandlestickMin,
                                    max: resolvedCandlestickMax ?? fallbackCandlestickMax,
                                    // 为右边数据点添加空间
                                    grace: '10%'
                                } : {
                                    // 折线图使用智能计算的显示范围
                                    min: displayRange.min,
                                    max: displayRange.max,
                                    // 改善标签显示
                                    ticks: {
                                        maxTicksLimit: labels.length > 50 ? 10 : Math.min(labels.length, 20),
                                        autoSkip: true,
                                        maxRotation: 45,
                                        minRotation: 0
                                    },
                                    // 为右边数据点添加空间
                                     grace: '10%'
                                })
                            },
                            y: {
                                display: true,
                                title: {
                                    display: true,
                                    text: 'Price ($)'
                                },
                                beginAtZero: false,
                                // 为蜡烛图设置合适的价格范围
                                ...(isCandlestick && response.data && response.data.length > 0 ? {
                                    min: Math.min(...response.data.map(item => parseFloat(item.low)).filter(p => p > 0)) * 0.998,
                                    max: Math.max(...response.data.map(item => parseFloat(item.high)).filter(p => p > 0)) * 1.002
                                } : {})
                            },
                            y1: {
                                position: 'right',
                                grid: { display: false },
                                ticks: { display: false },
                                title: { display: true, text: 'Vol' },
                                beginAtZero: true,
                                max: (maxVolume * 3) || 1
                            }
                        },
                        // 移除padding以解决数据点显示不完整问题
                        // layout: {
                        //     padding: {
                        //         left: 15,
                        //         right: 15,
                        //         top: 15,
                        //         bottom: 15
                        //     }
                        // },
                        // 添加拖动事件监听器
                        onHover: (event, activeElements, chart) => {
                            // 检查是否悬停在可拖动的annotation线上（只有SL/TP线可拖动）
                            const annotations = chart.options.plugins.annotation.annotations;
                            if (annotations) {
                                let overHLine = false;
                                let overVLine = false;
                                const canvasPosition = Chart.helpers.getRelativePosition(event, chart);
                                const dataY = chart.scales.y.getValueForPixel(canvasPosition.y);
                                const dataX = chart.scales.x.getValueForPixel(canvasPosition.x);
                                Object.keys(annotations).forEach(key => {
                                    if ((key === 'stopLoss' || key === 'takeProfit') && annotations[key].type === 'line') {
                                        const annotation = annotations[key];
                                        const lineY = annotation.yMin || annotation.value;
                                        if (Math.abs(dataY - lineY) < (chart.scales.y.max - chart.scales.y.min) * 0.02) {
                                            overHLine = true;
                                        }
                                    } else if (key === 'timeLimit' && annotations[key].type === 'line') {
                                        const annotation = annotations[key];
                                        if (isCandlestick) {
                                            const annotationValue = annotation.xMin ?? annotation.value ?? annotation.xMax;
                                            const annotationTime = this.normalizeMonitorTime(annotationValue);
                                            const tolerance = Math.max(candlestickStep || 60000, 1) * 0.5;
                                            if (annotationTime != null && Math.abs(dataX - annotationTime) <= tolerance) {
                                                overVLine = true;
                                            }
                                        } else {
                                            const labels = chart.data.labels || [];
                                            const lineIndex = labels.indexOf(annotation.xMin || annotation.value);
                                            if (lineIndex !== -1 && Math.abs(dataX - lineIndex) < 0.5) {
                                                overVLine = true;
                                            }
                                        }
                                    }
                                });
                                if (overHLine) chart.canvas.style.cursor = 'ns-resize';
                                else if (overVLine) chart.canvas.style.cursor = 'ew-resize';
                                else chart.canvas.style.cursor = 'default';
                            }
                        },
                        // 移除onClick事件，改用mousedown处理拖动开始
                    },
                    // 添加鼠标移动事件监听器
                    onResize: function(chart, size) {
                        // 图表大小改变时的处理
                    }
                    });

                    // Initialize or update trend chart with synchronized time range
                    const trendCtx = document.getElementById('monitorTrendChart')?.getContext('2d');
                    console.log('[loadMonitorChart] Trend chart context:', trendCtx ? 'found' : 'not found');
                    console.log('[loadMonitorChart] Current monitorTrendData length:', this.monitorTrendData?.length || 0);
                    console.log('[loadMonitorChart] MonitorTrendData sample:', this.monitorTrendData?.slice(0, 3));
                    if (trendCtx) {

                        if (!this.monitorTrendChart) {
                            console.log('[loadMonitorChart] Creating new monitorTrendChart with data points:', this.monitorTrendData?.length || 0);
                            const baseData = this.getFormattedTrendChartData().filter(item => Number.isFinite(item?.y));
                            const eventMarkerPlugin = (typeof window !== 'undefined' && window.domEventMarkerPlugin)
                                ? window.domEventMarkerPlugin
                                : null;
                            this.monitorTrendDatasetMap = { trend: 0, events: 1, price: 2 };
                            this.monitorTrendChart = new Chart(trendCtx, {
                                type: 'line',
                                data: {
                                    datasets: [
                                        {
                                            type: 'line',
                                            id: 'trend',
                                            label: '趋势分数',
                                            data: baseData,
                                            yAxisID: 'trendAxis',
                                            borderColor: 'rgba(59,130,246,0.85)',
                                            backgroundColor: 'rgba(59,130,246,0.15)',
                                            borderWidth: 2,
                                            tension: 0.25,
                                            pointRadius: 0,
                                            pointHoverRadius: 3,
                                            spanGaps: true,
                                        },
                                        {
                                            type: 'scatter',
                                            id: 'events',
                                            label: '事件',
                                            data: [],
                                            yAxisID: 'trendAxis',
                                            pointRadius: 0,
                                            pointHoverRadius: 6,
                                            pointHitRadius: 12,
                                            borderWidth: 0,
                                            showLine: false,
                                            domEvents: true,
                                        },
                                        {
                                            type: 'line',
                                            id: 'price',
                                            label: '价格',
                                            data: [],
                                            yAxisID: 'priceAxis',
                                            borderColor: 'rgba(234,179,8,0.85)',
                                            backgroundColor: 'rgba(234,179,8,0.12)',
                                            borderWidth: 2,
                                            tension: 0.2,
                                            pointRadius: 0,
                                            spanGaps: true,
                                        },
                                    ],
                                },
                                options: {
                                    parsing: false,
                                    responsive: true,
                                    maintainAspectRatio: false,
                                    interaction: { intersect: false, mode: 'index' },
                                    plugins: {
                                        legend: { display: true, position: 'top' },
                                        zoom: {
                                            pan: { enabled: true, mode: 'x' },
                                            zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' },
                                            onZoom: ({ chart }) => this.syncCharts(chart, this.monitorChart),
                                            onPan: ({ chart }) => this.syncCharts(chart, this.monitorChart),
                                        },
                                        tooltip: {
                                            callbacks: {
                                                title: (items) => {
                                                    if (!items?.length) return '';
                                                    const ts = items[0].parsed?.x;
                                                    if (!Number.isFinite(ts)) return '';
                                                    return new Date(ts).toLocaleString(undefined, { hour12: false });
                                                },
                                                label: (context) => {
                                                    const datasetId = context.dataset?.id;
                                                    const value = context.parsed?.y;
                                                    const raw = context.raw || {};
                                                    if (datasetId === 'trend') {
                                                        const lines = [];
                                                        const scoreValue = Number.isFinite(value) ? value : raw.score;
                                                        if (Number.isFinite(scoreValue)) {
                                                            lines.push(`趋势分数: ${this.formatNumber(scoreValue)}`);
                                                        }
                                                        if (Number.isFinite(raw.signal) && raw.signal !== 0) {
                                                            lines.push(raw.signal > 0 ? '方向: 做多' : '方向: 做空');
                                                        }
                                                        if (raw.suggestion && !lines.some(line => line.startsWith('建议'))) {
                                                            const label = raw.suggestion === 'buy' ? '建议: 做多' : raw.suggestion === 'sell' ? '建议: 做空' : `建议: ${raw.suggestion}`;
                                                            lines.push(label);
                                                        }
                                                        if (Number.isFinite(raw.price)) {
                                                            lines.push(`价格: $${this.formatNumber(raw.price)}`);
                                                        }
                                                        if (Number.isFinite(raw.obi)) {
                                                            lines.push(`OBI: ${this.formatNumber(raw.obi)}`);
                                                        }
                                                        if (Number.isFinite(raw.ofi)) {
                                                            lines.push(`OFI: ${this.formatNumber(raw.ofi)}`);
                                                        }
                                                        const longObi = raw.thresholds?.long?.obi;
                                                        const longOfi = raw.thresholds?.long?.ofi;
                                                        const shortObi = raw.thresholds?.short?.obi;
                                                        const shortOfi = raw.thresholds?.short?.ofi;
                                                        if (Number.isFinite(longObi) || Number.isFinite(longOfi)) {
                                                            const obiText = Number.isFinite(longObi) ? this.formatNumber(longObi) : '—';
                                                            const ofiText = Number.isFinite(longOfi) ? this.formatNumber(longOfi) : '—';
                                                            lines.push(`多头阈值: OBI ${obiText} / OFI ${ofiText}`);
                                                        }
                                                        if (Number.isFinite(shortObi) || Number.isFinite(shortOfi)) {
                                                            const obiText = Number.isFinite(shortObi) ? this.formatNumber(shortObi) : '—';
                                                            const ofiText = Number.isFinite(shortOfi) ? this.formatNumber(shortOfi) : '—';
                                                            lines.push(`空头阈值: OBI ${obiText} / OFI ${ofiText}`);
                                                        }
                                                        if (Number.isFinite(raw.fake_breakout_prob)) {
                                                            const pct = Math.round(raw.fake_breakout_prob * 100);
                                                            lines.push(`假突破概率: ${pct}%`);
                                                        }
                                                        if (raw.reason) {
                                                            String(raw.reason).split('\n').forEach((line) => {
                                                                const trimmed = line.trim();
                                                                if (trimmed) lines.push(trimmed);
                                                            });
                                                        }
                                                        return lines;
                                                    }
                                                    if (datasetId === 'events') {
                                                        const lines = [];
                                                        if (raw.label) {
                                                            lines.push(String(raw.label));
                                                        } else if (raw.code) {
                                                            lines.push(String(raw.code));
                                                        }
                                                        if (raw.side) {
                                                            const side = String(raw.side).toLowerCase();
                                                            if (side === 'buy') lines.push('方向: 做多');
                                                            else if (side === 'sell') lines.push('方向: 做空');
                                                            else lines.push(`方向: ${raw.side}`);
                                                        }
                                                        if (Number.isFinite(raw.score)) {
                                                            lines.push(`趋势分数: ${this.formatNumber(raw.score)}`);
                                                        }
                                                        if (Number.isFinite(raw.price)) {
                                                            lines.push(`价格: $${this.formatNumber(raw.price)}`);
                                                        }
                                                        if (raw.reason) {
                                                            const firstLine = String(raw.reason).split('\n').find(line => line && line.trim());
                                                            if (firstLine) lines.push(firstLine.trim());
                                                        }
                                                        return lines;
                                                    }
                                                    if (datasetId === 'price') {
                                                        return `价格: $${this.formatNumber(value)}`;
                                                    }
                                                    return `${context.dataset.label}: ${this.formatNumber(value)}`;
                                                },
                                            },
                                        },
                                    },
                                    scales: {
                                        x: {
                                            type: 'time',
                                            time: { unit: 'minute', displayFormats: { minute: 'HH:mm', hour: 'HH:mm', day: 'MMM dd' } },
                                        },
                                        trendAxis: {
                                            type: 'linear',
                                            position: 'left',
                                            grid: { drawOnChartArea: true },
                                            title: { display: true, text: '趋势分数' },
                                            suggestedMin: -1,
                                            suggestedMax: 1,
                                        },
                                        priceAxis: {
                                            type: 'linear',
                                            position: 'right',
                                            grid: { drawOnChartArea: false },
                                            title: { display: true, text: '价格' },
                                            display: false,
                                        },
                                    },
                                },
                                plugins: eventMarkerPlugin ? [eventMarkerPlugin] : [],
                            });
                            this.setupTrendLegendAndAnnotations(this.monitorTrendChart);
                            this.updateMonitorSignalOverlayDatasets();
                            this.monitorTrendChart.update('none');
                            this.renderMonitorSignalHistory();
                        } else {
                            if (!this.monitorTrendDatasetMap || this.monitorTrendDatasetMap.trend === undefined) {
                                this.monitorTrendDatasetMap = { trend: 0, events: 1, price: 2 };
                            }
                            const baseData = this.getFormattedTrendChartData().filter(item => Number.isFinite(item?.y));
                            const trendIndex = this.monitorTrendDatasetMap?.trend ?? 0;
                            if (this.monitorTrendChart.data.datasets[trendIndex]) {
                                this.monitorTrendChart.data.datasets[trendIndex].data = baseData;
                            }
                            this.updateMonitorSignalOverlayDatasets();
                            this.monitorTrendChart.update('none');
                            this.renderMonitorSignalHistory();
                        }
                    } else {
                        console.warn('[loadMonitorChart] Trend chart context not found - cannot create/update trend chart');
                    }

                    this.updateDomSignalAnnotations();

                // 添加键盘事件监听器支持左右箭头键平移
                const addKeyboardNavigation = () => {
                    const handleKeyDown = (event) => {
                        if (!this.monitorChart) return;
                        
                        const panAmount = isCandlestick ?
                            candlestickStep * 5 : // 5个数据点的时间跨度
                            5; // 折线图移动5个索引
                        
                        if (event.key === 'ArrowLeft') {
                            event.preventDefault();
                            this.monitorChart.pan({x: panAmount}, undefined, 'default');
                        } else if (event.key === 'ArrowRight') {
                            event.preventDefault();
                            this.monitorChart.pan({x: -panAmount}, undefined, 'default');
                        }
                    };
                    
                    // 移除之前的监听器（如果存在）
                    if (this.monitorChart._keyboardHandler) {
                        document.removeEventListener('keydown', this.monitorChart._keyboardHandler);
                    }
                    
                    // 添加新的监听器
                    this.monitorChart._keyboardHandler = handleKeyDown;
                    document.addEventListener('keydown', handleKeyDown);
                };
                
                // 启用键盘导航
                addKeyboardNavigation();

                // 添加鼠标事件监听器实现正确的拖动逻辑
                const canvas = this.monitorChart.canvas;

                canvas.addEventListener('mousedown', (event) => {
                    // 处理可拖动annotation线的拖动开始（只有SL/TP线可拖动）
                const rect = canvas.getBoundingClientRect();
                const canvasPosition = {
                    x: event.clientX - rect.left,
                    y: event.clientY - rect.top
                };
                const dataY = this.monitorChart.scales.y.getValueForPixel(canvasPosition.y);
                const dataX = this.monitorChart.scales.x.getValueForPixel(canvasPosition.x);

                const annotations = this.ensureMonitorAnnotations();
                if (annotations) {
                    Object.keys(annotations).forEach(key => {
                        if ((key === 'stopLoss' || key === 'takeProfit') && annotations[key].type === 'line') {
                            const annotation = annotations[key];
                            const lineY = annotation.yMin || annotation.value;
                            if (Math.abs(dataY - lineY) < (this.monitorChart.scales.y.max - this.monitorChart.scales.y.min) * 0.02) {
                                this.monitorChart._draggedAnnotation = key;
                                this.monitorChart._isDragging = true;
                                event.preventDefault();
                            }
                        } else if (key === 'timeLimit' && annotations[key].type === 'line') {
                            const annotation = annotations[key];
                            if (isCandlestick) {
                                const annotationValue = annotation.xMin ?? annotation.value ?? annotation.xMax;
                                const annotationTime = this.normalizeMonitorTime(annotationValue);
                                const tolerance = Math.max(candlestickStep || 60000, 1) * 0.5;
                                if (annotationTime != null && Math.abs(dataX - annotationTime) <= tolerance) {
                                    this.monitorChart._draggedAnnotation = key;
                                    this.monitorChart._isDragging = true;
                                    event.preventDefault();
                                }
                            } else {
                                const labels = this.monitorChart.data.labels || [];
                                const lineIndex = labels.indexOf(annotation.xMin || annotation.value);
                                if (lineIndex !== -1 && Math.abs(dataX - lineIndex) < 0.5) {
                                    this.monitorChart._draggedAnnotation = key;
                                    this.monitorChart._isDragging = true;
                                    event.preventDefault();
                                }
                            }
                        }
                    });
                }
            });

                canvas.addEventListener('mousemove', (event) => {
                if (this.monitorChart._isDragging && this.monitorChart._draggedAnnotation) {
                    const rect = canvas.getBoundingClientRect();
                    const canvasPosition = {
                        x: event.clientX - rect.left,
                        y: event.clientY - rect.top
                    };
                    const annotationKey = this.monitorChart._draggedAnnotation;
                    const annotations = this.ensureMonitorAnnotations();
                    const annotation = annotations ? annotations[annotationKey] : null;
                    if (!annotation) return;

                    if (annotationKey === 'timeLimit') {
                        const dataX = this.monitorChart.scales.x.getValueForPixel(canvasPosition.x);
                        if (isCandlestick) {
                            const times = Array.isArray(this.currentMonitor?.displayTimes) ? this.currentMonitor.displayTimes : [];
                            let nearestIndex = Math.max((this.currentMonitor?.riskStartIndex ?? 0) + 1, 0);
                            if (times.length) {
                                let smallestDiff = Number.POSITIVE_INFINITY;
                                for (let i = nearestIndex; i < times.length; i++) {
                                    const ts = this.normalizeMonitorTime(times[i]);
                                    if (ts == null) continue;
                                    const diff = Math.abs(ts - dataX);
                                    if (diff < smallestDiff) {
                                        smallestDiff = diff;
                                        nearestIndex = i;
                                    }
                                }
                                if (nearestIndex >= times.length) nearestIndex = times.length - 1;
                            }
                            if (nearestIndex <= (this.currentMonitor?.riskStartIndex ?? 0)) {
                                nearestIndex = Math.min((this.currentMonitor?.riskStartIndex ?? 0) + 1, Math.max(times.length - 1, 0));
                            }
                            const intervalValueRaw = document.getElementById('monitorInterval')?.value || '1 min';
                            const intervalMinutes = INTERVAL_MINUTES_MAP[normalizeIntervalKey(intervalValueRaw) || '1min'] || 1;
                            const timeValue = times[nearestIndex];
                            const axisDate = timeValue instanceof Date ? new Date(timeValue.getTime())
                                : (typeof timeValue === 'number' && Number.isFinite(timeValue) ? new Date(timeValue) : null);
                            if (axisDate) {
                                annotation.xMin = axisDate;
                                annotation.xMax = axisDate;
                                annotation.value = axisDate;
                                annotation.endValue = axisDate;
                                if (annotations.profitBox) annotations.profitBox.xMax = axisDate;
                                if (annotations.lossBox) annotations.lossBox.xMax = axisDate;
                            }
                            const computedSpan = (nearestIndex - (this.currentMonitor?.riskStartIndex ?? 0)) * intervalMinutes;
                            const durationLimit = getMonitorDurationMinutes(document.getElementById('monitorDuration')?.value);
                            const effectiveSpan = Number.isFinite(durationLimit) && durationLimit > 0
                                ? Math.min(computedSpan, durationLimit)
                                : computedSpan;
                            this.currentMonitor.maxTimeSpan = Math.max(1, effectiveSpan);
                        } else {
                            const labels = this.monitorChart.data.labels || [];
                            let index = Math.round(dataX);
                            if (index <= this.currentMonitor.riskStartIndex) index = this.currentMonitor.riskStartIndex + 1;
                            if (index >= labels.length) index = labels.length - 1;
                            const label = labels[index];
                            annotation.xMin = label;
                            annotation.xMax = label;
                            if (annotations.profitBox) annotations.profitBox.xMax = label;
                            if (annotations.lossBox) annotations.lossBox.xMax = label;
                            const intervalValue = document.getElementById('monitorInterval')?.value || '1 min';
                            const intervalMinutes = INTERVAL_MINUTES_MAP[normalizeIntervalKey(intervalValue) || '1min'] || 1;
                            const newSpan = (index - this.currentMonitor.riskStartIndex) * intervalMinutes;
                            const durationLimit = getMonitorDurationMinutes(document.getElementById('monitorDuration')?.value);
                            const effectiveSpan = Number.isFinite(durationLimit) && durationLimit > 0
                                ? Math.min(newSpan, durationLimit)
                                : newSpan;
                            this.currentMonitor.maxTimeSpan = Math.max(1, effectiveSpan);
                        }
                    } else {
                        const dataY = this.monitorChart.scales.y.getValueForPixel(canvasPosition.y);
                        const adjustedPrice = Math.round(dataY / minStep) * minStep;
                        annotation.yMin = adjustedPrice;
                        annotation.yMax = adjustedPrice;
                        annotation.value = adjustedPrice;
                        annotation.endValue = adjustedPrice;
                        annotation.label.content = `$${adjustedPrice.toFixed(2)}`;

                        if (annotationKey === 'stopLoss') {
                            const stopLossInput = document.getElementById('monitorStopLoss');
                            if (stopLossInput) stopLossInput.value = adjustedPrice.toFixed(2);
                        } else if (annotationKey === 'takeProfit') {
                            const takeProfitInput = document.getElementById('monitorTakeProfit');
                            if (takeProfitInput) takeProfitInput.value = adjustedPrice.toFixed(2);
                        }

                        // 更新预期PnL
                        this.updateExpectedPnL();
                    }

                    // 更新图表
                    this.monitorChart.update('none');
                }
            });

                canvas.addEventListener('mouseup', () => {
                    if (this.monitorChart) {
                        const dragged = this.monitorChart._draggedAnnotation;
                        this.monitorChart._isDragging = false;
                        this.monitorChart._draggedAnnotation = null;
                        if (dragged === 'timeLimit') {
                            const symbol = this.currentMonitor?.symbol;
                            const sl = document.getElementById('monitorStopLoss')?.value;
                            const tp = document.getElementById('monitorTakeProfit')?.value;
                            const payload = {
                                symbol: symbol,
                                stop_loss_price: sl ? parseFloat(sl) : null,
                                take_profit_price: tp ? parseFloat(tp) : null,
                                max_time_span: this.currentMonitor?.maxTimeSpan
                            };
                            this.apiCall('/risk_rules', {
                                method: 'POST',
                                body: JSON.stringify(payload)
                            }).catch(console.error);
                            if (!this.riskRules) this.riskRules = {};
                            this.riskRules[symbol] = Object.assign({}, this.riskRules[symbol] || {}, payload);
                        }
                        // 拖动结束后更新预期PnL
                        this.updateExpectedPnL();
                    }
                });

                canvas.addEventListener('mouseleave', () => {
                    if (this.monitorChart) {
                        this.monitorChart._isDragging = false;
                        this.monitorChart._draggedAnnotation = null;
                        canvas.style.cursor = 'default';
                    }
                });
            };

            if (!this.monitorChartRenderTimeout) {
                this.monitorChartRenderTimeout = setTimeout(() => {
                    const renderFn = this.pendingMonitorChartRender;
                    this.pendingMonitorChartRender = null;
                    this.monitorChartRenderTimeout = null;
                    if (typeof renderFn === 'function') {
                        try {
                            const result = renderFn();
                            if (result && typeof result.then === 'function') {
                                result.catch((renderErr) => {
                                    console.error('Failed to render monitor chart:', renderErr);
                                });
                            }
                        } catch (renderErr) {
                            console.error('Failed to render monitor chart:', renderErr);
                        }
                    }
                }, 100);
            }

            // 渲染OHLC价格分布
            const currentPrice = this.currentMonitor?.currentPrice || 0;
            this.renderOhlcPriceStrip(response.data, currentPrice);

        } catch (error) {
            console.error('Failed to load monitor chart:', error);
            const chartContainer = document.getElementById('monitorChartContainer');
            if (chartContainer) {
                chartContainer.innerHTML = `<div class="alert alert-danger">Failed to load chart: ${error.message}</div>`;
            }
        }
    }

    renderOhlcPriceStrip(data, currentPrice) {
        try {
            const container = document.getElementById('monitorOhlcStrip');
            if (!container) return;

            // 规范化与过滤数据，避免出现 0 或 NaN 影响 L/C
            const records = Array.isArray(data) ? data : [];
            const toNum = (v) => {
                const n = typeof v === 'string' ? parseFloat(v) : (typeof v === 'number' ? v : NaN);
                return Number.isFinite(n) && n > 0 ? n : null;
            };

            let low = Infinity;
            let high = -Infinity;
            let open = null;

            for (const item of records) {
                const l = toNum(item?.low);
                const h = toNum(item?.high);
                const o = toNum(item?.open);

                if (l !== null && l < low) low = l;
                if (h !== null && h > high) high = h;
                if (open === null && o !== null) open = o;
            }

            // 计算收盘/当前价：优先使用有效的 currentPrice；否则回退到最后一个有效 close/price；再不行回退到 open
            const hasValidCur = Number.isFinite(currentPrice) && currentPrice > 0;
            let close = null;
            if (hasValidCur) {
                close = currentPrice;
            } else {
                for (let i = records.length - 1; i >= 0; i--) {
                    const c = toNum(records[i]?.close ?? records[i]?.price);
                    if (c !== null) { close = c; break; }
                }
                if (close === null) close = open;
            }

            // 基本校验
            if (!Number.isFinite(low) || !Number.isFinite(high) || open === null || close === null) {
                container.innerHTML = '<div class="text-muted small">暂无数据</div>';
                return;
            }

            // 扩展范围以包含当前价（如果有效）
            if (hasValidCur) {
                if (close < low) low = close;
                if (close > high) high = close;
            }

            const range = high - low;
            if (!Number.isFinite(range) || range <= 0) {
                container.innerHTML = '<div class="text-muted small">价格无变化</div>';
                return;
            }

            // 计算各价格点的相对位置（百分比）
            const lowPos = 0;
            const openPos = ((open - low) / range) * 100;
            const closePos = ((close - low) / range) * 100;
            const highPos = 100;

            // 确定开盘价和当前价之间的颜色
            const isRising = close >= open;
            const segmentColor = isRising ? '#28a745' : '#dc3545';

            // 创建SVG可视化
            const svgHeight = 14;
            const widthCandidate = container.offsetWidth || container.clientWidth;
            const svgWidth = widthCandidate && widthCandidate > 0 ? widthCandidate : 200;

            const svg = `
                <svg width="${svgWidth}" height="${svgHeight}" style="display: block;">
                    <!-- 背景线段 -->
                    <line x1="0" y1="${svgHeight/2}" x2="${svgWidth}" y2="${svgHeight/2}" 
                          stroke="#dee2e6" stroke-width="2"/>
                    
                    <!-- 开盘价到当前价的彩色线段 -->
                    <line x1="${(openPos/100) * svgWidth}" y1="${svgHeight/2}" 
                          x2="${(closePos/100) * svgWidth}" y2="${svgHeight/2}" 
                          stroke="${segmentColor}" stroke-width="3"/>
                    
                    <!-- 最低价点 -->
                    <circle cx="${(lowPos/100) * svgWidth}" cy="${svgHeight/2}" r="3" 
                            fill="#6c757d" stroke="white" stroke-width="1"/>
                    
                    <!-- 开盘价点 -->
                    <circle cx="${(openPos/100) * svgWidth}" cy="${svgHeight/2}" r="3" 
                            fill="#007bff" stroke="white" stroke-width="1"/>
                    
                    <!-- 当前价点 -->
                    <circle cx="${(closePos/100) * svgWidth}" cy="${svgHeight/2}" r="3" 
                            fill="${segmentColor}" stroke="white" stroke-width="1"/>
                    
                    <!-- 最高价点 -->
                    <circle cx="${(highPos/100) * svgWidth}" cy="${svgHeight/2}" r="3" 
                            fill="#6c757d" stroke="white" stroke-width="1"/>
                </svg>
            `;

            const fmt = (v) => Number.isFinite(v) ? v.toFixed(2) : '-';

            // 生成按价格从低到高排序的标签
            const points = [
                { key: 'L', value: low, style: '' },
                { key: 'O', value: open, style: '' },
                { key: 'C', value: close, style: `color: ${segmentColor};` },
                { key: 'H', value: high, style: '' },
            ].sort((a, b) => a.value - b.value);

            const labels = `
                <div class="d-flex justify-content-between mt-1" style="font-size: 10px; color: #6c757d;">
                    ${points.map(p => `<span style="${p.style}">${p.key}: $${fmt(p.value)}</span>`).join('')}
                </div>
            `;

            container.innerHTML = svg + labels;
        } catch (error) {
            console.error('Failed to render OHLC price strip:', error);
            const container = document.getElementById('monitorOhlcStrip');
            if (container) {
                container.innerHTML = '<div class="text-muted small">渲染失败</div>';
            }
        }
    }

    updateMonitorStopLossLine() {
        if (!this.monitorChart) return;

        const stopLossInput = document.getElementById('monitorStopLoss');
        const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;

        // Update or remove stop loss annotation
        const annotations = this.ensureMonitorAnnotations();
        if (!annotations) return;

        if (stopLossPrice && !isNaN(stopLossPrice)) {
            annotations.stopLoss = {
                type: 'line',
                yMin: stopLossPrice,
                yMax: stopLossPrice,
                borderColor: 'rgb(255, 99, 132)',
                borderWidth: 2,
                borderDash: [5, 5],
                label: {
                    content: `Stop Loss: $${stopLossPrice.toFixed(2)}`,
                    enabled: true,
                    position: 'start'
                }
            };
            if (annotations.lossBox) {
                if ((this.currentMonitor?.positionQty || 0) > 0) {
                    annotations.lossBox.yMin = stopLossPrice;
                } else {
                    annotations.lossBox.yMax = stopLossPrice;
                }
            }
        } else {
            delete annotations.stopLoss;
        }

        this.monitorChart.update('none');
    }

    updateMonitorTakeProfitLine() {
        if (!this.monitorChart) return;

        const takeProfitInput = document.getElementById('monitorTakeProfit');
        const takeProfitPrice = takeProfitInput?.value ? parseFloat(takeProfitInput.value) : null;

        // Update or remove take profit annotation
        const annotations = this.ensureMonitorAnnotations();
        if (!annotations) return;

        if (takeProfitPrice && !isNaN(takeProfitPrice)) {
            annotations.takeProfit = {
                type: 'line',
                yMin: takeProfitPrice,
                yMax: takeProfitPrice,
                borderColor: 'rgb(75, 192, 192)',
                borderWidth: 2,
                borderDash: [5, 5],
                label: {
                    content: `Take Profit: $${takeProfitPrice.toFixed(2)}`,
                    enabled: true,
                    position: 'start'
                }
            };
            if (annotations.profitBox) {
                if ((this.currentMonitor?.positionQty || 0) > 0) {
                    annotations.profitBox.yMax = takeProfitPrice;
                } else {
                    annotations.profitBox.yMin = takeProfitPrice;
                }
            }
        } else {
            delete annotations.takeProfit;
        }

        this.monitorChart.update('none');
    }

    updateMonitorRealtime(data) {
        try {
            if (!this.currentMonitor || !this.currentMonitor.symbol) return;
            const symbol = this.currentMonitor.symbol;

            // 兼容服务器返回结构，确保处理数组形式的数据
            let payload = (data && data.data) ? data.data : data;
            if (Array.isArray(payload)) payload = payload[0] || {};
            if (!payload || (payload.symbol && payload.symbol !== symbol)) return;

            let price = null;
            const priceCandidates = [payload.last, payload.price, payload.close, payload.mark, payload.current];
            for (const candidate of priceCandidates) {
                const numeric = Number(candidate);
                if (Number.isFinite(numeric)) {
                    price = numeric;
                    break;
                }
            }
            if (!Number.isFinite(price)) return;

            const volume = Number(payload.volume ?? payload.vol ?? payload.lastVolume ?? 0) || 0;

            const qty = Number(this.currentMonitor.positionQty || 0);
            const avgCostRaw = Number(this.currentMonitor.avgCost || 0);
            const avgCostForCalc = qty >= 0 ? avgCostRaw : Math.abs(avgCostRaw);
            const storedMultiplier = Number(this.currentMonitor.contractMultiplier || 0);
            const multiplierSymbol = (this.currentMonitor.symbol || symbol || '').toString().toUpperCase();
            const contractMultiplier = (Number.isFinite(storedMultiplier) && storedMultiplier > 0)
                ? storedMultiplier
                : this.getContractMultiplier(multiplierSymbol);
            this.currentMonitor.contractMultiplier = contractMultiplier;

            // 更新当前价格到监控上下文
            this.currentMonitor.currentPrice = price;

            const curEl = document.getElementById('monitorCurrentPrice');
            if (curEl) curEl.textContent = `$${this.formatNumber(price)}`;

            const combinedEl = document.getElementById('monitorPositionCombined');
            if (combinedEl) {
                const positionValue = Math.abs(qty) * price * (contractMultiplier || 1);
                combinedEl.textContent = `${qty} ($${this.formatNumber(positionValue)})`;
            }

            // 计算未实现盈亏，区分多头和空头持仓
            let pnl = 0;
            if (qty > 0) {  // 多头
                // 多头盈亏 = (当前价格 - 成本价) * 持仓数量
                pnl = (price - avgCostForCalc) * qty * (contractMultiplier || 1);
            } else if (qty < 0) {  // 空头
                // 空头盈亏 = (成本价 - 当前价格) * 持仓数量的绝对值
                pnl = (avgCostForCalc - price) * Math.abs(qty) * (contractMultiplier || 1);
            }

            const pnlEl = document.getElementById('monitorPnL');
            if (pnlEl) {
                pnlEl.textContent = `$${this.formatNumber(pnl)}`;
                pnlEl.classList.toggle('text-success', pnl >= 0);
                pnlEl.classList.toggle('text-danger', pnl < 0);
            }

            const denom = Math.max(
                1e-9,
                Math.abs(avgCostForCalc)
                    * Math.max(1, Math.abs(qty))
                    * Math.max(Math.abs(contractMultiplier) || 0, 1e-9)
            );
            const pnlPct = (pnl / denom) * 100;
            const pnlPctEl = document.getElementById('monitorPnLPercent');
            if (pnlPctEl) {
                pnlPctEl.textContent = `(${this.formatNumber(pnlPct)}%)`;
                pnlPctEl.classList.toggle('text-success', pnl >= 0);
                pnlPctEl.classList.toggle('text-danger', pnl < 0);
            }

            this.currentMonitor.pnl = pnl;
            this.currentMonitor.pnlPercent = pnlPct;

            // Update asset change (daily change) in real-time
            if (this.currentMonitor.todayOpenPrice && price) {
                const assetChange = price - this.currentMonitor.todayOpenPrice;
                const assetChangePercent = (assetChange / this.currentMonitor.todayOpenPrice) * 100;
                
                const assetChangeEl = document.getElementById('monitorAssetChange');
                const assetChangePercentEl = document.getElementById('monitorAssetChangePercent');
                
                if (assetChangeEl) {
                    const changeText = assetChange >= 0 ? `+$${assetChange.toFixed(2)}` : `-$${Math.abs(assetChange).toFixed(2)}`;
                    assetChangeEl.textContent = changeText;
                    assetChangeEl.className = assetChange >= 0 ? 'text-success' : 'text-danger';
                }
                
                if (assetChangePercentEl) {
                    const percentText = assetChangePercent >= 0 ? `+${assetChangePercent.toFixed(2)}%` : `${assetChangePercent.toFixed(2)}%`;
                    assetChangePercentEl.textContent = percentText;
                    assetChangePercentEl.className = assetChangePercent >= 0 ? 'text-success' : 'text-danger';
                }
            }

            if (this.monitorChart && this.monitorChart.data && this.monitorChart.data.datasets && this.monitorChart.data.datasets.length > 0) {
                const dataset = this.monitorChart.data.datasets[0];
                const chartType = this.monitorChart.config.type || '';
                let candlestickData = null;

                const intervalSelect = document.getElementById('monitorInterval');
                const intervalRaw = intervalSelect ? intervalSelect.value : '1 min';
                const interval = normalizeIntervalKey(intervalRaw) || '1min';
                const durationSelect = document.getElementById('monitorDuration');
                const duration = durationSelect?.value || '30 M';

                const rawTimestamp = payload.timestamp ?? payload.lastTimestamp ?? payload.lastTime ?? payload.time ?? payload.last_time ?? payload.updateTime ?? payload.datetime ?? null;
                let baseTime = this.parseMonitorChartTime(rawTimestamp, { skipDstAdjustment: true });
                if (!(baseTime instanceof Date) || Number.isNaN(baseTime.getTime())) {
                    baseTime = new Date();
                }

                const alignToInterval = (date) => {
                    const aligned = new Date(date.getTime());
                    const intervalKey = typeof interval === 'string' ? interval : '';
                    switch (intervalKey) {
                        case '1min':
                        case '1mins':
                            aligned.setSeconds(0, 0);
                            break;
                        case '5min':
                        case '5mins': {
                            const minutes5 = Math.floor(aligned.getMinutes() / 5) * 5;
                            aligned.setMinutes(minutes5, 0, 0);
                            break;
                        }
                        case '15min':
                        case '15mins': {
                            const minutes15 = Math.floor(aligned.getMinutes() / 15) * 15;
                            aligned.setMinutes(minutes15, 0, 0);
                            break;
                        }
                        case '30min':
                        case '30mins': {
                            const minutes30 = Math.floor(aligned.getMinutes() / 30) * 30;
                            aligned.setMinutes(minutes30, 0, 0);
                            break;
                        }
                        case '1hour':
                        case '1hours':
                        case '60min':
                        case '60mins':
                            aligned.setMinutes(0, 0, 0);
                            break;
                        case '1day':
                        case '1days':
                            aligned.setHours(0, 0, 0, 0);
                            break;
                        default:
                            break;
                    }
                    return aligned;
                };

                let intervalTime = alignToInterval(baseTime);
                let timeValue = this.normalizeMonitorTime(intervalTime);

                let timeLabel;
                if (intervalRaw.includes('day') || duration.includes('M') || duration.includes('Y')) {
                    timeLabel = intervalTime.toLocaleDateString('zh-CN', {
                        month: 'short',
                        day: 'numeric'
                    });
                } else if (intervalRaw.includes('hour') || duration === '1 W') {
                    timeLabel = intervalTime.toLocaleString('zh-CN', {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        hour12: false
                    });
                } else {
                    timeLabel = intervalTime.toLocaleTimeString('zh-CN', {
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: false
                    });
                }

                if (chartType === 'candlestick') {
                    const sourceOhlc = Array.isArray(dataset.data) ? dataset.data : [];
                    const ohlcData = sourceOhlc.map(bar => ({ ...bar }));
                    candlestickData = ohlcData;
                    const volumeDataset = this.monitorChart.data.datasets?.[1];
                    const sourceVolumes = Array.isArray(volumeDataset?.data) ? volumeDataset.data : [];
                    const vData = sourceVolumes.map((entry) => {
                        if (typeof entry === 'object' && entry !== null) {
                            return { ...entry };
                        }
                        const numericVolume = Number(entry);
                        return {
                            x: timeValue,
                            y: Number.isFinite(numericVolume) ? numericVolume : 0
                        };
                    });
                    const labelArray = Array.isArray(this.monitorChart.data.labels)
                        ? [...this.monitorChart.data.labels]
                        : [];
                    let estimatedStep = null;
                    if (ohlcData.length > 0) {
                        const lastBar = ohlcData[ohlcData.length - 1];
                        const prevBar = ohlcData.length > 1 ? ohlcData[ohlcData.length - 2] : null;
                        if (prevBar && Number.isFinite(lastBar?.x) && Number.isFinite(prevBar.x)) {
                            estimatedStep = Math.max(1, Math.abs(lastBar.x - prevBar.x));
                        }
                        if (Number.isFinite(lastBar?.x)) {
                            const diff = timeValue - lastBar.x;
                            if (estimatedStep) {
                                if (Math.abs(diff) <= estimatedStep * 0.75) {
                                    timeValue = lastBar.x;
                                } else if (diff > estimatedStep * 0.75) {
                                    const multiples = Math.max(1, Math.round(diff / estimatedStep));
                                    timeValue = lastBar.x + multiples * estimatedStep;
                                } else if (diff < -estimatedStep * 0.75) {
                                    timeValue = lastBar.x;
                                }
                            } else if (timeValue <= lastBar.x) {
                                timeValue = lastBar.x;
                            }
                            intervalTime = new Date(timeValue);
                        }
                    }

                    const tolerance = (estimatedStep || 60000) * 0.75;
                    if (ohlcData.length > 0) {
                        const lastBar = ohlcData[ohlcData.length - 1];
                        if (Number.isFinite(lastBar?.x) && Math.abs(lastBar.x - timeValue) <= tolerance) {
                            const updatedBar = {
                                ...lastBar,
                                x: Number.isFinite(lastBar.x) ? lastBar.x : timeValue,
                                c: price,
                                h: Math.max(Number(lastBar.h ?? price), price),
                                l: Math.min(Number(lastBar.l ?? price), price),
                                volume
                            };
                            ohlcData[ohlcData.length - 1] = updatedBar;

                            if (labelArray.length) {
                                labelArray[labelArray.length - 1] = timeLabel;
                            } else {
                                labelArray.push(timeLabel);
                            }

                            if (vData.length > 0) {
                                const lastVolume = vData[vData.length - 1];
                                if (Number.isFinite(lastVolume?.x) && Math.abs(lastVolume.x - timeValue) <= tolerance) {
                                    vData[vData.length - 1] = {
                                        ...lastVolume,
                                        x: Number.isFinite(lastVolume.x) ? lastVolume.x : timeValue,
                                        y: volume
                                    };
                                } else {
                                    vData.push({ x: timeValue, y: volume });
                                }
                            } else {
                                vData.push({ x: timeValue, y: volume });
                            }
                        } else {
                            ohlcData.push({ x: timeValue, o: price, h: price, l: price, c: price, volume });
                            vData.push({ x: timeValue, y: volume });
                            labelArray.push(timeLabel);
                        }
                    } else {
                        ohlcData.push({ x: timeValue, o: price, h: price, l: price, c: price, volume });
                        vData.push({ x: timeValue, y: volume });
                        labelArray.push(timeLabel);
                    }

                    const MAX_POINTS = 300;
                    if (ohlcData.length > MAX_POINTS) {
                        ohlcData.shift();
                        vData.shift();
                        if (labelArray.length > MAX_POINTS) {
                            while (labelArray.length > MAX_POINTS) {
                                labelArray.shift();
                            }
                        } else if (labelArray.length > 0) {
                            labelArray.shift();
                        }
                    }

                    const sanitizedOhlc = ohlcData.map((bar) => {
                        const normalizedX = this.normalizeMonitorTime(bar?.x ?? bar?.t ?? bar?.time ?? timeValue);
                        const openCandidate = Number(bar?.o ?? bar?.open ?? bar?.c ?? bar?.close ?? price);
                        const closeCandidate = Number(bar?.c ?? bar?.close ?? bar?.o ?? bar?.open ?? price);
                        const highCandidate = Number(bar?.h ?? bar?.high ?? closeCandidate ?? openCandidate ?? price);
                        const lowCandidate = Number(bar?.l ?? bar?.low ?? closeCandidate ?? openCandidate ?? price);
                        const volumeCandidate = Number(bar?.volume ?? bar?.v ?? 0);

                        const resolvedX = Number.isFinite(normalizedX) ? normalizedX : timeValue;
                        const resolvedClose = Number.isFinite(closeCandidate) ? closeCandidate : price;
                        const resolvedOpen = Number.isFinite(openCandidate) ? openCandidate : resolvedClose;
                        const resolvedHighBase = Number.isFinite(highCandidate) ? highCandidate : resolvedClose;
                        const resolvedLowBase = Number.isFinite(lowCandidate) ? lowCandidate : resolvedClose;
                        const resolvedVolume = Number.isFinite(volumeCandidate) ? volumeCandidate : 0;

                        const resolvedHigh = Math.max(resolvedHighBase, resolvedOpen, resolvedClose);
                        const resolvedLow = Math.min(resolvedLowBase, resolvedOpen, resolvedClose);

                        return {
                            x: resolvedX,
                            t: resolvedX,
                            o: resolvedOpen,
                            h: resolvedHigh,
                            l: resolvedLow,
                            c: resolvedClose,
                            volume: resolvedVolume,
                            v: resolvedVolume
                        };
                    });
                    dataset.data = sanitizedOhlc;
                    if (this.monitorChart.config?.data?.datasets?.[0]) {
                        this.monitorChart.config.data.datasets[0].data = sanitizedOhlc;
                    }

                    const sanitizedVolumes = vData.map((entry) => {
                        const rawX = entry?.x ?? entry?.t ?? entry?.time ?? entry;
                        const normalizedX = this.normalizeMonitorTime(rawX ?? timeValue);
                        const volumeCandidate = Number(entry?.y ?? entry?.volume ?? entry?.v ?? entry ?? 0);
                        const resolvedX = Number.isFinite(normalizedX) ? normalizedX : timeValue;
                        const resolvedVolume = Number.isFinite(volumeCandidate) ? volumeCandidate : 0;
                        return {
                            x: resolvedX,
                            t: resolvedX,
                            y: resolvedVolume,
                            volume: resolvedVolume
                        };
                    });
                    if (volumeDataset) {
                        volumeDataset.data = sanitizedVolumes;
                        if (this.monitorChart.config?.data?.datasets?.[1]) {
                            this.monitorChart.config.data.datasets[1].data = sanitizedVolumes;
                        }
                    }
                    candlestickData = dataset.data;
                    this.monitorChart.data.labels = labelArray.slice();
                    if (this.monitorChart.config?.data) {
                        this.monitorChart.config.data.labels = labelArray.slice();
                    }
                    if (this.currentMonitor) {
                        this.currentMonitor.displayTimes = sanitizedOhlc.map(item => new Date(item.x));
                        this.currentMonitor.displayLabels = labelArray.slice();
                    }

                    const xOptions = this.monitorChart.options?.scales?.x;
                    if (xOptions) {
                        const firstBar = dataset.data[0];
                        const latestBar = dataset.data[dataset.data.length - 1];
                        const previousBar = dataset.data[dataset.data.length - 2];
                        const step = previousBar && latestBar
                            ? Math.max(1, Math.abs(latestBar.x - previousBar.x))
                            : 60000;
                        if (firstBar && Number.isFinite(firstBar.x) && !Number.isFinite(xOptions.min)) {
                            xOptions.min = firstBar.x;
                        }
                        if (latestBar && Number.isFinite(latestBar.x)) {
                            xOptions.max = latestBar.x + step * 0.1;
                        }
                    }
                } else {
                    const baseLabels = Array.isArray(this.monitorChart.data.labels)
                        ? [...this.monitorChart.data.labels]
                        : [];
                    const volumeDataset = this.monitorChart.data.datasets[1];
                    const priceData = Array.isArray(dataset.data) ? [...dataset.data] : [];
                    const volumeData = Array.isArray(volumeDataset.data) ? [...volumeDataset.data] : [];
                    let displayTimes = Array.isArray(this.currentMonitor?.displayTimes)
                        ? this.currentMonitor.displayTimes.map(value => {
                            if (value instanceof Date) return new Date(value.getTime());
                            const parsed = this.parseMonitorChartTime(value);
                            return parsed instanceof Date ? parsed : new Date(value);
                        })
                        : [];

                    const trimTrailingPlaceholders = () => {
                        let trimmed = false;
                        while (priceData.length > 0) {
                            const lastIdx = priceData.length - 1;
                            const lastPrice = priceData[lastIdx];
                            if (lastPrice == null || Number.isNaN(lastPrice)) {
                                priceData.pop();
                                if (volumeData.length > lastIdx) {
                                    volumeData.splice(lastIdx, 1);
                                } else if (volumeData.length) {
                                    volumeData.pop();
                                }
                                if (baseLabels.length > lastIdx) {
                                    baseLabels.splice(lastIdx, 1);
                                } else if (baseLabels.length) {
                                    baseLabels.pop();
                                }
                                if (displayTimes.length > lastIdx) {
                                    displayTimes.splice(lastIdx, 1);
                                } else if (displayTimes.length) {
                                    displayTimes.pop();
                                }
                                trimmed = true;
                            } else {
                                break;
                            }
                        }
                        return trimmed;
                    };

                    if (trimTrailingPlaceholders()) {
                        console.log('[实时更新] 移除尾部占位空值，准备连接实时数据点');
                    }

                    const intervalMinutes = INTERVAL_MINUTES_MAP[interval] || 1;
                    const fallbackStep = Math.max(60000, intervalMinutes * 60 * 1000);
                    let estimatedStep = fallbackStep;
                    if (displayTimes.length > 1) {
                        const lastTime = this.normalizeMonitorTime(displayTimes[displayTimes.length - 1]);
                        const prevTime = this.normalizeMonitorTime(displayTimes[displayTimes.length - 2]);
                        if (Number.isFinite(lastTime) && Number.isFinite(prevTime) && Math.abs(lastTime - prevTime) > 0) {
                            estimatedStep = Math.max(1, Math.abs(lastTime - prevTime));
                        }
                    }

                    const tolerance = estimatedStep * 0.75;
                    const lastStoredTime = displayTimes.length
                        ? this.normalizeMonitorTime(displayTimes[displayTimes.length - 1])
                        : null;
                    let updatedExisting = false;

                    console.log(`[实时更新] interval="${intervalRaw}" duration="${duration}" 时间值=${intervalTime.toISOString()}`);
                    console.log(`[实时更新] 最新数据 - 价格: ${price}, 成交量: ${volume}`);

                    if (lastStoredTime != null && Math.abs(timeValue - lastStoredTime) <= tolerance) {
                        if (priceData.length) {
                            priceData[priceData.length - 1] = price;
                        } else {
                            priceData.push(price);
                        }
                        if (volumeData.length) {
                            volumeData[volumeData.length - 1] = volume;
                        } else {
                            volumeData.push(volume);
                        }
                        if (baseLabels.length) {
                            baseLabels[baseLabels.length - 1] = timeLabel;
                        } else {
                            baseLabels.push(timeLabel);
                        }
                        displayTimes[displayTimes.length - 1] = new Date(timeValue);
                        updatedExisting = true;
                        console.log(`[实时更新] 更新最后一个数据点: ${timeLabel}, 价格: ${price}`);
                    } else {
                        baseLabels.push(timeLabel);
                        priceData.push(price);
                        volumeData.push(volume);
                        displayTimes.push(new Date(timeValue));
                        console.log(`[实时更新] 添加新数据点: ${timeLabel}, 价格: ${price}`);
                    }

                    const MAX_POINTS = 300;
                    while (priceData.length > MAX_POINTS) {
                        priceData.shift();
                        if (volumeData.length) volumeData.shift();
                        if (baseLabels.length) baseLabels.shift();
                        if (displayTimes.length) displayTimes.shift();
                    }

                    this.monitorChart.data.labels = baseLabels.slice();
                    dataset.data = priceData.slice();
                    volumeDataset.data = volumeData.slice();

                    if (this.currentMonitor) {
                        this.currentMonitor.displayTimes = displayTimes.map(value =>
                            value instanceof Date ? new Date(value.getTime()) : new Date(value)
                        );
                        this.currentMonitor.displayLabels = baseLabels.slice();
                    }

                    const xScale = this.monitorChart.options?.scales?.x;
                    if (xScale && baseLabels.length) {
                        const firstLabel = baseLabels[0];
                        const lastLabel = baseLabels[baseLabels.length - 1];
                        if (typeof xScale.min === 'string' || xScale.min == null) {
                            xScale.min = firstLabel;
                        }
                        if (typeof xScale.max === 'string' || xScale.max == null || !updatedExisting) {
                            xScale.max = lastLabel;
                        }
                    }
                }
                if (chartType === 'candlestick' && Array.isArray(candlestickData)) {
                    const extremes = candlestickData.reduce((acc, bar) => {
                        if (!bar) return acc;
                        const high = Number(bar.h);
                        const low = Number(bar.l);
                        if (Number.isFinite(high)) acc.max = Math.max(acc.max, high);
                        if (Number.isFinite(low)) acc.min = Math.min(acc.min, low);
                        return acc;
                    }, { min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY });
                    if (Number.isFinite(extremes.min) && Number.isFinite(extremes.max)) {
                        const span = extremes.max - extremes.min;
                        const padding = span > 0 ? span * 0.05 : (extremes.max || 0) * 0.002 || 0.5;
                        const yScale = this.monitorChart.options?.scales?.y;
                        if (yScale) {
                            yScale.min = Math.max(0, extremes.min - padding);
                            yScale.max = extremes.max + padding;
                        }
                    }
                }

                const annotations = this.ensureMonitorAnnotations();
                if (annotations) {
                    const currentPriceLabel = `$${this.formatNumber(price)}`;
                    if (annotations.currentPrice) {
                        annotations.currentPrice.yMin = price;
                        annotations.currentPrice.yMax = price;
                        annotations.currentPrice.borderColor = annotations.currentPrice.borderColor || 'rgb(255, 206, 84)';
                        annotations.currentPrice.borderWidth = annotations.currentPrice.borderWidth || 2;
                        if (annotations.currentPrice.label) {
                            annotations.currentPrice.label.content = currentPriceLabel;
                            annotations.currentPrice.label.backgroundColor = 'rgba(255, 206, 84, 0.8)';
                            annotations.currentPrice.label.color = 'black';
                            annotations.currentPrice.label.position = 'end';
                            annotations.currentPrice.label.enabled = true;
                            annotations.currentPrice.label.padding = 4;
                        } else {
                            annotations.currentPrice.label = {
                                content: currentPriceLabel,
                                enabled: true,
                                position: 'end',
                                backgroundColor: 'rgba(255, 206, 84, 0.8)',
                                color: 'black',
                                padding: 4
                            };
                        }
                    } else {
                        annotations.currentPrice = {
                            type: 'line',
                            yMin: price,
                            yMax: price,
                            borderColor: 'rgb(255, 206, 84)',
                            borderWidth: 2,
                            label: {
                                content: currentPriceLabel,
                                enabled: true,
                                position: 'end',
                                backgroundColor: 'rgba(255, 206, 84, 0.8)',
                                color: 'black',
                                padding: 4
                            }
                        };
                    }
                }
                // 调整成交量轴的最大值，使柱状图高度不超过整体三分之一
                const volDataset = this.monitorChart.data.datasets?.[1];
                const volumes = Array.isArray(volDataset?.data)
                    ? volDataset.data
                        .map(v => (typeof v === 'object' && v !== null) ? v.y : v)
                        .filter(v => typeof v === 'number' && !Number.isNaN(v))
                    : [];
                const maxVol = volumes.length ? Math.max(...volumes) : 0;
                if (this.monitorChart.options?.scales?.y1) {
                    this.monitorChart.options.scales.y1.max = (maxVol * 3) || 1;
                }

                const volumeSample = Array.isArray(volDataset?.data) ? volDataset.data : [];
                console.log(`[图表更新] 准备更新图表，数据点数量: ${dataset.data.length}, 成交量数据点: ${volumeSample.length}`);
                console.log(`[图表更新] 价格数据: ${JSON.stringify(dataset.data.slice(-3))}`);
                console.log(`[图表更新] 成交量数据: ${JSON.stringify(volumeSample.slice(-3))}`);
                console.log(`[图表更新] 标签数据: ${JSON.stringify(this.monitorChart.data.labels.slice(-3))}`);

                this.monitorChart.update('none');
                console.log(`[图表更新] 图表更新完成`);

                const updatedRange = this.extractRangeFromMonitorChart();
                if (updatedRange) {
                    if (!this.currentMonitor) this.currentMonitor = {};
                    this.currentMonitor.chartTimeRange = updatedRange;
                }
            }

            // 更新预期PnL显示
            this.updateExpectedPnL();

            // 检查时间跨度达到右边界提醒
            try {
                // 使用实际的时间比较，而不是依赖图表标签数量
                if (this.currentMonitor && this.currentMonitor.entryTime && !this.currentMonitor.timeAlertSent) {
                    const entryTime = new Date(this.currentMonitor.entryTime);
                    const currentTime = new Date();
                    const elapsedMinutes = (currentTime - entryTime) / (60 * 1000);
                    const maxTimeSpan = this.currentMonitor.maxTimeSpan || 30;
                    
                    if (elapsedMinutes >= maxTimeSpan) {
                        const symbol = this.currentMonitor.symbol;
                        const msg = `${symbol} 持仓时间已达预设上限 (${Math.floor(elapsedMinutes)}分钟)`;
                        this.showAlert('warning', msg);
                        // 发送通知，即使失败也不影响标记已发送
                        this.apiCall('/notify', { method: 'POST', body: JSON.stringify({ message: msg, event_type: 'risk_rule' }) })
                            .then(result => {
                                if (!result.success) {
                                    console.warn('Position expiry notification failed:', result.message || 'Unknown reason');
                                }
                            })
                            .catch(err => console.warn('Position expiry notification error:', err));
                        this.currentMonitor.timeAlertSent = true;
                    }
                }
            } catch (e) { console.warn('检查持仓时间失败:', e); }
        } catch (e) {
            console.warn('updateMonitorRealtime failed:', e);
        }
    }

    updateExpectedPnL() {
        try {
            // 支持两种模式：监控模式和建议仓位模式
            const isMonitorMode = !!this.currentMonitor;
            const currentRiskRuleData = this.riskRulesManager?.getCurrentRuleData?.() || null;

            let stopLossInput, takeProfitInput, positionQty, avgCost;
            let symbolForMultiplier = '';

            if (isMonitorMode) {
                // 监控模式：使用监控面板的数据
                stopLossInput = document.getElementById('monitorStopLoss');
                takeProfitInput = document.getElementById('monitorTakeProfit');
                positionQty = this.currentMonitor.positionQty || 0;
                avgCost = this.currentMonitor.avgCost || 0;
                symbolForMultiplier = this.currentMonitor.symbol || '';
            } else {
                // 建议仓位模式：使用订单面板的数据
                stopLossInput = document.getElementById('stopLossPrice');
                takeProfitInput = document.getElementById('takeProfitPrice');
                const orderPrice = parseFloat(document.getElementById('orderPrice')?.value || 0);
                const quantity = parseInt(document.getElementById('orderQuantity')?.value || 0);
                const action = document.getElementById('orderAction')?.value;

                // 在建议仓位模式下，使用订单价格作为成本价，订单数量作为持仓
                avgCost = orderPrice;
                positionQty = action === 'SELL' ? -quantity : quantity;
                symbolForMultiplier = (currentRiskRuleData?.symbol
                    || document.getElementById('orderSymbol')?.value
                    || document.getElementById('summaryPrimarySymbolInput')?.value
                    || '')
                    .toString();
            }

            const normalizedSymbol = symbolForMultiplier ? symbolForMultiplier.trim().toUpperCase() : '';
            const contractMultiplier = this.getContractMultiplier(normalizedSymbol);
            
            const expectedSection = document.getElementById('expectedPnLSection');
            const expectedStopLossPnL = document.getElementById('expectedStopLossPnL');
        const expectedTakeProfitPnL = document.getElementById('expectedTakeProfitPnL');
        const slDeltaEl = document.getElementById('monitorStopLossDelta');
        const tpDeltaEl = document.getElementById('monitorTakeProfitDelta');
        const rrrEl = document.getElementById('monitorRRR');
            
            // 获取下单弹窗中的Expected Loss和Expected Profit字段
            const expectedLossInput = document.getElementById('expectedLoss');
            const expectedProfitInput = document.getElementById('expectedProfit');
            
            if (!expectedSection || !expectedStopLossPnL || !expectedTakeProfitPnL) return;
            
            // 默认小字Δ显示
            if (slDeltaEl) slDeltaEl.textContent = 'Δ — / —%';
            if (tpDeltaEl) tpDeltaEl.textContent = 'Δ — / —%';
            
            const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;
            const takeProfitPrice = takeProfitInput?.value ? parseFloat(takeProfitInput.value) : null;
            
            // 只有当有持仓且设置了止损或止盈价格时才显示预期PnL
            if (positionQty !== 0 && (stopLossPrice || takeProfitPrice)) {
                expectedSection.style.display = 'block';
                
                // 计算止损预期PnL & Δ
                if (stopLossPrice && avgCost > 0) {
                    const deltaSL = stopLossPrice - avgCost;
                    const pctSL = avgCost ? (deltaSL / avgCost) * 100 : 0;
                    
                    // 根据持仓方向计算预期盈亏
                    let stopLossPnL = 0;
                    if (positionQty > 0) {  // 多头
                        // 多头盈亏 = (止损价格 - 成本价) * 持仓数量 * 合约乘数
                        stopLossPnL = deltaSL * positionQty * contractMultiplier;
                    } else {  // 空头
                        // 空头盈亏 = (成本价 - 止损价格) * 持仓数量的绝对值 * 合约乘数
                        stopLossPnL = (avgCost - stopLossPrice) * Math.abs(positionQty) * contractMultiplier;
                    }
                    
                    // 仅显示金额
                    expectedStopLossPnL.textContent = `$${this.formatNumber(stopLossPnL)}`;
                    expectedStopLossPnL.className = stopLossPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
                    
                    // 填充下单弹窗中的Expected Loss字段
                    if (expectedLossInput) {
                        expectedLossInput.value = `$${this.formatNumber(stopLossPnL)}`;
                    }
                    
                    // 小字Δ/百分比（颜色保持 text-muted）
                    if (slDeltaEl) {
                        const sign = deltaSL >= 0 ? '+' : '';
                        slDeltaEl.textContent = `Δ ${sign}${this.formatNumber(deltaSL)} / ${this.formatNumber(pctSL)}%`;
                    }
                } else {
                    expectedStopLossPnL.textContent = '$0.00';
                    expectedStopLossPnL.className = 'fw-bold text-muted';
                    if (expectedLossInput) {
                        expectedLossInput.value = '';
                    }
                    if (slDeltaEl) slDeltaEl.textContent = 'Δ — / —%';
                }
                
                // 计算止盈预期PnL & Δ
                if (takeProfitPrice && avgCost > 0) {
                    const deltaTP = takeProfitPrice - avgCost;
                    const pctTP = avgCost ? (deltaTP / avgCost) * 100 : 0;
                    
                    // 根据持仓方向计算预期盈亏
                    let takeProfitPnL = 0;
                    if (positionQty > 0) {  // 多头
                        // 多头盈亏 = (止盈价格 - 成本价) * 持仓数量 * 合约乘数
                        takeProfitPnL = deltaTP * positionQty * contractMultiplier;
                    } else {  // 空头
                        // 空头盈亏 = (成本价 - 止盈价格) * 持仓数量的绝对值 * 合约乘数
                        takeProfitPnL = (avgCost - takeProfitPrice) * Math.abs(positionQty) * contractMultiplier;
                    }
                    
                    // 仅显示金额
                    expectedTakeProfitPnL.textContent = `$${this.formatNumber(takeProfitPnL)}`;
                    expectedTakeProfitPnL.className = takeProfitPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
                    
                    // 填充下单弹窗中的Expected Profit字段
                    if (expectedProfitInput) {
                        expectedProfitInput.value = `$${this.formatNumber(takeProfitPnL)}`;
                    }
                    
                    // 小字Δ/百分比（颜色保持 text-muted）
                    if (tpDeltaEl) {
                        const sign = deltaTP >= 0 ? '+' : '';
                        tpDeltaEl.textContent = `Δ ${sign}${this.formatNumber(deltaTP)} / ${this.formatNumber(pctTP)}%`;
                    }
                } else {
                    expectedTakeProfitPnL.textContent = '$0.00';
                    expectedTakeProfitPnL.className = 'fw-bold text-muted';
                    if (expectedProfitInput) {
                        expectedProfitInput.value = '';
                    }
                    if (tpDeltaEl) tpDeltaEl.textContent = 'Δ — / —%';
                }
                // 计算RRR
                if (rrrEl) {
                    if (stopLossPrice && takeProfitPrice && avgCost > 0 && positionQty !== 0) {
                        let risk, reward;
                        if (positionQty > 0) {
                            risk = avgCost - stopLossPrice;
                            reward = takeProfitPrice - avgCost;
                        } else {
                            risk = stopLossPrice - avgCost;
                            reward = avgCost - takeProfitPrice;
                        }
                        if (risk > 0) {
                            const ratio = reward / risk;
                            rrrEl.textContent = `${this.formatNumber(ratio)}:1`;
                        } else {
                            rrrEl.textContent = '—';
                        }
                    } else {
                        rrrEl.textContent = '2:1';
                    }
                }
            } else {
                expectedSection.style.display = 'none';
                if (slDeltaEl) slDeltaEl.textContent = 'Δ — / —%';
                if (tpDeltaEl) tpDeltaEl.textContent = 'Δ — / —%';
                if (rrrEl) rrrEl.textContent = '2:1';
            }
        } catch (err) {
            console.error('updateExpectedPnL failed:', err);
        }
    }

    showActiveRiskRule(rule) {
        if (!this.riskRulesManager || typeof this.riskRulesManager.showActiveRiskRule !== 'function') {
            return;
        }
        return this.riskRulesManager.showActiveRiskRule(rule);
    }

    hideActiveRiskRule() {
        if (!this.riskRulesManager || typeof this.riskRulesManager.hideActiveRiskRule !== 'function') {
            return;
        }
        return this.riskRulesManager.hideActiveRiskRule();
    }

    displayChart(symbol, data) {
         // Show chart modal
          const modal = new bootstrap.Modal(document.getElementById('chartModal'));
          document.getElementById('chartSymbol').textContent = `${symbol} Price Chart`;
         
         const chartContainer = document.getElementById('chartContainer');
         if (!chartContainer) return;
         
         // Clear previous chart
         chartContainer.innerHTML = `<canvas id="priceChart" width="900" height="450"></canvas>`; // 增加canvas大小以解决数据点显示不完整问题
         
         modal.show();
         
         // Wait for modal to be shown before creating chart
         setTimeout(() => {
             const ctx = document.getElementById('priceChart').getContext('2d');
             
             // Prepare data for Chart.js
             const labels = data.map(bar => new Date(bar.date).toLocaleDateString());
             const prices = data.map(bar => bar.close);
             
             new Chart(ctx, {
                 type: 'line',
                 data: {
                     labels: labels,
                     datasets: [{
                         label: `${symbol} Price`,
                         data: prices,
                         borderColor: 'rgb(75, 192, 192)',
                         backgroundColor: 'rgba(75, 192, 192, 0.1)',
                         tension: 0.1
                     }]
                 },
                 options: {
                     responsive: true,
                     plugins: {
                         title: {
                             display: true,
                             text: `${symbol} Price Chart`
                         }
                     },
                     scales: {
                         y: {
                             beginAtZero: false
                         }
                     }
                 }
             });
         }, 300);
     }



    async addRiskRule() {
        if (!this.riskRulesManager || typeof this.riskRulesManager.addRiskRule !== 'function') {
            return;
        }
        return this.riskRulesManager.addRiskRule();
    }

    // 初始化loadOrders防抖函数
    initOrdersDebounce() {
        return this.orderManager.initOrdersDebounce?.();
    }
    
    // 委托给OrderManager
    async loadOrders() {
        return this.orderManager.loadOrders();
    }
    
    async placeOrder() {
        return this.orderManager.placeOrder();
    }
    
    async cancelOrder(orderId, button) {
        return this.orderManager.cancelOrder(orderId, button);
    }
    
    async cancelAllOrders() {
        return this.orderManager.cancelAllOrders();
    }
    
    async syncLimitOrder(orderId, button, event) {
        return this.orderManager.syncLimitOrder(orderId, button, event);
    }
    
    // _loadOrders方法已迁移到OrderManager
    
    // 智能更新Orders表格，只更新发生变化的行
    // 订单表格相关方法已迁移到OrderManager

    renderInternalOrders(name, orders = null) {
        return this.orderManager.renderInternalOrders(name, orders);
    }

    async loadInternalStrategyOrders(name, { executions = null, limit = this.strategyExecutionsDefaultLimit, force = false } = {}) {
        try {
            const rawExecutions = Array.isArray(executions)
                ? executions
                : await this.fetchStrategyExecutions(name, { limit, force });
            if (!Array.isArray(rawExecutions)) {
                this.internalStrategyOrders = [];
                this.renderInternalOrders(name, []);
                return [];
            }
            const orders = rawExecutions.slice();
            orders.sort((a, b) => {
                const at = a?.timestamp ? new Date(a.timestamp).getTime() : 0;
                const bt = b?.timestamp ? new Date(b.timestamp).getTime() : 0;
                if (bt !== at) return bt - at;
                const aid = Number(a?.order_id) || 0;
                const bid = Number(b?.order_id) || 0;
                return bid - aid;
            });
            this.internalStrategyOrders = orders;
            this.renderInternalOrders(name, orders);
            return orders;
        } catch (error) {
            console.error('Failed to load internal strategy orders:', error);
            this.internalStrategyOrders = [];
            this.renderInternalOrders(name, []);
            return [];
        }
    }

    async loadPortfolio(options = {}) {
        const { refresh = true } = options || {};
        try {
            let handledByWebsocket = false;
            if (refresh && this.dataManager?.loadPortfolio) {
                handledByWebsocket = await this.dataManager.loadPortfolio(options);
            }
            if (!handledByWebsocket && this.portfolioManager?.loadPortfolio) {
                return this.portfolioManager.loadPortfolio();
            }
        } catch (error) {
            console.error('Failed to load portfolio:', error);
        }
        return [];
    }

    async loadRiskRules(options = {}) { return this.riskRulesManager.loadRiskRules(options); }



    async loadStrategySettings() {
        if (!this.strategiesManager || typeof this.strategiesManager.loadStrategySettings !== 'function') {
            return;
        }
        return this.strategiesManager.loadStrategySettings();
    }

    async saveStrategySettings() {
        if (!this.strategiesManager || typeof this.strategiesManager.saveStrategySettings !== 'function') {
            return;
        }
        return this.strategiesManager.saveStrategySettings();
    }

    ensureSummaryControls() {
        if (this.summaryControlsInitialized) return;
        this.summaryControlsInitialized = true;
        this.setupSymbolInput('summaryPrimarySymbolInput', 'summaryPrimarySymbolDropdown');
        const symbolInput = document.getElementById('summaryPrimarySymbolInput');
        if (symbolInput) {
            symbolInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    symbolInput.blur();
                }
            });
            const handler = () => this.handleSummaryPrimarySymbolChange();
            symbolInput.addEventListener('change', handler);
            symbolInput.addEventListener('blur', handler);
        }
        const dataSelect = document.getElementById('summaryDataFeedSelect');
        if (dataSelect) {
            dataSelect.addEventListener('change', () => this.handleSummaryDataFeedChange());
        }
        const pushSwitch = document.getElementById('summaryDataPushSwitch');
        if (pushSwitch) {
            pushSwitch.addEventListener('change', () => this.handleSummaryPushToggle());
        }
        const refreshBtn = document.getElementById('summaryStatusRefreshBtn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => {
                if (this.currentStrategyDetails) {
                    this.loadStrategyStatus(this.currentStrategyDetails, { force: true });
                }
            });
        }
    }

    ensureRiskSettingsControls() {
        if (this.riskSettingsInitialized) return;
        this.riskSettingsInitialized = true;
        const form = document.getElementById('riskSettingsForm');
        if (form) {
            form.addEventListener('submit', (event) => {
                event.preventDefault();
                this.handleRiskSettingsSave();
            });
        }
        const resetBtn = document.getElementById('riskSettingsResetBtn');
        if (resetBtn) {
            resetBtn.addEventListener('click', () => this.handleRiskSettingsReset());
        }
    }

    async handleSummaryPrimarySymbolChange() {
        if (this.summaryPrimarySaving) return;
        const name = this.currentStrategyDetails;
        if (!name) return;
        const input = document.getElementById('summaryPrimarySymbolInput');
        if (!input) return;
        const raw = (input.value || '').trim().toUpperCase();
        if (!raw) {
            this.updateSummaryPrimaryStatus('empty');
            this.summaryPrimaryCurrent = null;
            return;
        }
        if (raw === this.summaryPrimaryCurrent) {
            this.updateSummaryPrimaryStatus('saved');
            return;
        }
        this.summaryPrimarySaving = true;
        this.updateSummaryPrimaryStatus('saving');
        try {
            await this.apiCall(`/strategies/${encodeURIComponent(name)}`, {
                method: 'PUT',
                body: JSON.stringify({ primary_symbol: raw }),
            });
            this.summaryPrimaryCurrent = raw;
            this.updateSummaryPrimaryStatus('saved');
            if (this.data_push_state?.enabled) {
                this.loadStrategyStatus(name, { force: true });
            }
        } catch (error) {
            console.error('Failed to save primary symbol', error);
            this.updateSummaryPrimaryStatus('error');
            this.showAlert('danger', 'Failed to save primary symbol');
        } finally {
            this.summaryPrimarySaving = false;
        }
    }

    updateSummaryPrimaryStatus(state) {
        const statusEl = document.getElementById('summaryPrimarySymbolStatus');
        if (!statusEl) return;
        if (state === 'saving') {
            statusEl.textContent = 'Saving primary symbol...';
            statusEl.classList.remove('text-danger');
        } else if (state === 'saved') {
            statusEl.textContent = 'Primary symbol saved';
            statusEl.classList.remove('text-danger');
        } else if (state === 'empty') {
            statusEl.textContent = 'Select a symbol to save';
            statusEl.classList.remove('text-danger');
        } else if (state === 'error') {
            statusEl.textContent = 'Save failed. Please retry.';
            statusEl.classList.add('text-danger');
        } else {
            statusEl.textContent = '';
            statusEl.classList.remove('text-danger');
        }
    }

    updateRiskSettingsStatus(state, message) {
        const statusEl = document.getElementById('riskSettingsStatus');
        if (!statusEl) return;
        statusEl.classList.remove('text-danger');
        if (state === 'loading') {
            statusEl.textContent = '正在加载风控参数...';
        } else if (state === 'saving') {
            statusEl.textContent = '正在保存风控设置...';
        } else if (state === 'error') {
            statusEl.textContent = message || '保存失败，请稍后重试。';
            statusEl.classList.add('text-danger');
        } else if (state === 'reset') {
            statusEl.textContent = '已恢复到最近保存的设置。';
        } else if (state === 'loaded') {
            statusEl.textContent = '风控设置已载入。';
        } else if (state === 'saved') {
            statusEl.textContent = '风控设置已保存。';
        } else {
            statusEl.textContent = message || '风控设置';
        }
    }

    renderRiskSettingsForm(settings = {}) {
        const maxInput = document.getElementById('riskMaxPositions');
        const forbidSwitch = document.getElementById('riskForbidAddOnLoss');
        const timeInput = document.getElementById('riskLossTimeThreshold');
        const pnlInput = document.getElementById('riskLossPnlThreshold');
        const notifySwitch = document.getElementById('riskNotifyOnTrigger');
        const maxValue = settings.max_positions;
        if (maxInput) {
            maxInput.value = (maxValue === null || maxValue === undefined) ? '' : maxValue;
        }
        if (forbidSwitch) {
            forbidSwitch.checked = settings.forbid_add_on_loss !== false;
        }
        const timeValue = settings.loss_time_threshold;
        if (timeInput) {
            timeInput.value = (timeValue === null || timeValue === undefined) ? '' : timeValue;
        }
        const pnlValue = settings.loss_pnl_threshold;
        if (pnlInput) {
            pnlInput.value = (pnlValue === null || pnlValue === undefined) ? '' : pnlValue;
        }
        if (notifySwitch) {
            notifySwitch.checked = settings.notify_on_trigger !== false;
        }
    }

    async loadStrategyRiskSettings(name) {
        this.updateRiskSettingsStatus('loading');
        this.currentRiskSettings = null;
        try {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/risk-settings`);
            if (!res?.success) {
                throw new Error(res?.detail || 'Failed to load risk settings');
            }
            this.currentRiskSettings = res.settings || {};
            this.renderRiskSettingsForm(this.currentRiskSettings);
            this.updateRiskSettingsStatus('loaded');
        } catch (error) {
            console.error('Failed to load strategy risk settings', error);
            this.updateRiskSettingsStatus('error', '无法加载风控设置。');
        }
    }

    async handleRiskSettingsSave() {
        if (this.riskSettingsSaving) return;
        const name = this.currentStrategyDetails;
        if (!name) return;
        const maxInput = document.getElementById('riskMaxPositions');
        const forbidSwitch = document.getElementById('riskForbidAddOnLoss');
        const timeInput = document.getElementById('riskLossTimeThreshold');
        const pnlInput = document.getElementById('riskLossPnlThreshold');
        const notifySwitch = document.getElementById('riskNotifyOnTrigger');

        const payload = {};
        if (maxInput) {
            const raw = maxInput.value?.trim();
            if (raw) {
                const parsed = Number.parseInt(raw, 10);
                if (!Number.isNaN(parsed) && parsed >= 0) {
                    payload.max_positions = parsed;
                } else {
                    this.showAlert('danger', '持仓数量上限需为非负整数');
                    return;
                }
            } else {
                payload.max_positions = null;
            }
        }
        if (forbidSwitch) {
            payload.forbid_add_on_loss = !!forbidSwitch.checked;
        }
        if (timeInput) {
            const raw = timeInput.value?.trim();
            if (raw) {
                const parsed = Number.parseInt(raw, 10);
                if (Number.isNaN(parsed) || parsed < 0) {
                    this.showAlert('danger', '浮亏持续时间需为非负整数');
                    return;
                }
                payload.loss_time_threshold = parsed;
            } else {
                payload.loss_time_threshold = null;
            }
        }
        if (pnlInput) {
            const raw = pnlInput.value?.trim();
            if (raw) {
                const parsed = Number.parseFloat(raw);
                if (Number.isNaN(parsed)) {
                    this.showAlert('danger', '浮亏判定阈值格式不正确');
                    return;
                }
                payload.loss_pnl_threshold = parsed;
            } else {
                payload.loss_pnl_threshold = null;
            }
        }
        if (notifySwitch) {
            payload.notify_on_trigger = !!notifySwitch.checked;
        }

        this.riskSettingsSaving = true;
        this.updateRiskSettingsStatus('saving');
        try {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/risk-settings`, {
                method: 'PUT',
                body: JSON.stringify(payload),
            });
            if (!res?.success) {
                throw new Error(res?.detail || 'Failed to save risk settings');
            }
            this.currentRiskSettings = res.settings || payload;
            this.renderRiskSettingsForm(this.currentRiskSettings);
            this.updateRiskSettingsStatus('saved');
            this.showAlert('success', '风控设置已保存');
        } catch (error) {
            console.error('Failed to save strategy risk settings', error);
            this.updateRiskSettingsStatus('error', error?.message);
            this.showAlert('danger', '保存风控设置失败，请稍后重试。');
        } finally {
            this.riskSettingsSaving = false;
        }
    }

    async handleRiskSettingsReset() {
        if (this.riskSettingsSaving) return;
        if (!this.currentStrategyDetails) return;
        if (!this.currentRiskSettings) {
            await this.loadStrategyRiskSettings(this.currentStrategyDetails);
            return;
        }
        this.renderRiskSettingsForm(this.currentRiskSettings);
        this.updateRiskSettingsStatus('reset');
    }

    async handleSummaryDataFeedChange() {
        const name = this.currentStrategyDetails;
        if (!name) return;
        const select = document.getElementById('summaryDataFeedSelect');
        if (!select) return;
        const value = select.value || null;
        if (value === this.summaryDataFeedCurrent) return;
        try {
            await this.apiCall(`/strategies/${encodeURIComponent(name)}`, {
                method: 'PUT',
                body: JSON.stringify({ data_subscription: value || null }),
            });
            this.summaryDataFeedCurrent = value;
            if (this.data_push_state?.enabled) {
                await this.setStrategyDataPush({ enabled: true });
            }
        } catch (error) {
            console.error('Failed to update data subscription', error);
            this.showAlert('danger', 'Failed to update data subscription');
            if (this.summaryDataFeedCurrent !== null) {
                select.value = this.summaryDataFeedCurrent || '';
            }
        }
    }

    async handleSummaryPushToggle() {
        if (this.summaryPushUpdating) return;
        const switchEl = document.getElementById('summaryDataPushSwitch');
        if (!switchEl) return;
        const enabled = !!switchEl.checked;
        const result = await this.setStrategyDataPush({ enabled });
        if (!result || result.error) {
            this.summaryPushUpdating = true;
            switchEl.checked = false;
            this.summaryPushUpdating = false;
        }
    }

    async setStrategyDataPush({ enabled, subscription, symbol } = {}) {
        const name = this.currentStrategyDetails;
        if (!name) return null;
        if (typeof enabled !== 'boolean') enabled = true;
        const select = document.getElementById('summaryDataFeedSelect');
        const symbolInput = document.getElementById('summaryPrimarySymbolInput');
        const switchEl = document.getElementById('summaryDataPushSwitch');
        const statusEl = document.getElementById('summaryDataPushStatus');
        const targetSub = subscription || select?.value || null;
        const targetSymbol = (symbol || symbolInput?.value || '').trim().toUpperCase();
        if (enabled) {
            if (!targetSub) {
                this.showAlert('warning', 'Select a data subscription before enabling push');
                if (switchEl) switchEl.checked = false;
                statusEl && (statusEl.textContent = '未推送');
                this.summaryPushAwaitingData = false;
                return { error: 'missing_subscription' };
            }
            if (!targetSymbol) {
                this.showAlert('warning', 'Primary symbol is required to push data');
                if (switchEl) switchEl.checked = false;
                statusEl && (statusEl.textContent = '未推送');
                this.summaryPushAwaitingData = false;
                return { error: 'missing_symbol' };
            }
        }
        try {
            this.summaryPushUpdating = true;
            if (statusEl) statusEl.textContent = enabled ? '推送中…' : '未推送';
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/data-push`, {
                method: 'POST',
                body: JSON.stringify({
                    enabled,
                    subscription: targetSub || null,
                    symbol: targetSymbol || null,
                }),
            });
            const state = res?.state || {};
            const awaitingData = !!state.awaiting_data && !!state.active;
            const stateReason = state.status_reason || '';
            this.summaryPushAwaitingData = awaitingData;
            if (switchEl) switchEl.checked = !!state.active;
            if (statusEl) {
                if (state.active) {
                    if (!awaitingData && stateReason) {
                        statusEl.textContent = stateReason;
                    } else {
                        statusEl.textContent = awaitingData ? '等待DOM数据…' : '已推送';
                    }
                } else {
                    statusEl.textContent = stateReason || '未推送';
                }
            }
            this.data_push_state = {
                enabled: !!state.active,
                active: !!state.active,
                subscription: state.subscription,
                symbol: state.symbol,
                awaiting_data: awaitingData,
                last_data_timestamp: state.last_data_timestamp || null,
                status_reason: stateReason || null,
            };
            if (!res?.success) {
                const message = res?.error || 'Failed to update data push state';
                this.showAlert('danger', message);
            }
            if (!awaitingData && this.summaryPushAwaitingTimer) {
                clearTimeout(this.summaryPushAwaitingTimer);
                this.summaryPushAwaitingTimer = null;
            }
            await this.loadStrategyStatus(name, { force: true });
            return state;
        } catch (error) {
            console.error('Failed to update data push state', error);
            this.showAlert('danger', 'Failed to update data push state');
            if (switchEl) switchEl.checked = false;
            if (statusEl) statusEl.textContent = '未推送';
            this.summaryPushAwaitingData = false;
            if (this.summaryPushAwaitingTimer) {
                clearTimeout(this.summaryPushAwaitingTimer);
                this.summaryPushAwaitingTimer = null;
            }
            return { error: 'request_failed' };
        } finally {
            this.summaryPushUpdating = false;
        }
    }

    async loadStrategyStatus(name, { force = false } = {}) {
        if (this.summaryStatusLoading && !force) return;
        const receivingEl = document.getElementById('summaryStatusReceiving');
        if (!receivingEl) return;
        this.summaryStatusLoading = true;
        this.summaryStatusTarget = name;
        const runtimeEl = document.getElementById('summaryStatusRuntime');
        const processedEl = document.getElementById('summaryStatusProcessed');
        const thresholdEl = document.getElementById('summaryStatusThresholdHits');
        const buyEl = document.getElementById('summaryStatusBuySignals');
        const sellEl = document.getElementById('summaryStatusSellSignals');
        const slPriceEl = document.getElementById('summaryStatusStopLoss');
        const tpPriceEl = document.getElementById('summaryStatusTakeProfit');

        receivingEl.textContent = '…';
        runtimeEl && (runtimeEl.textContent = '…');
        processedEl && (processedEl.textContent = '…');
        thresholdEl && (thresholdEl.textContent = '…');
        buyEl && (buyEl.textContent = '…');
        sellEl && (sellEl.textContent = '…');
        slPriceEl && (slPriceEl.textContent = '…');
        tpPriceEl && (tpPriceEl.textContent = '…');

        if (this.ibkrManager && typeof this.ibkrManager.requestStrategyStatus === 'function') {
            const result = this.ibkrManager.requestStrategyStatus({
                name,
                force: Boolean(force),
                minInterval: 15000,
            });
            if (result !== 'failed') {
                return result;
            }
        }

        try {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/status`);
            const payload = {
                type: 'strategy_status',
                name,
                success: !!res?.success,
                status: res?.status || {},
                source: 'http',
            };
            if (!payload.success) {
                payload.error = res?.error || 'Failed to fetch status';
            }
            this.applyStrategyStatusPayload(payload);
            return payload;
        } catch (error) {
            console.error('Failed to load strategy status', error);
            this.applyStrategyStatusPayload({
                type: 'strategy_status',
                name,
                success: false,
                error: error?.message || 'request_failed',
            });
            return { error: error?.message || 'request_failed' };
        }
    }

    // cancelAllOrders和syncLimitOrder方法已迁移到OrderManager

    async removeRiskRule(symbol) { return this.riskRulesManager.removeRiskRule(symbol); }

    async toggleRiskRule(symbol, enabled) { return this.riskRulesManager.toggleRiskRule(symbol, enabled); }



    formatNumber(num) {
        if (num === null || num === undefined) return 'N/A';
        return parseFloat(num).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    escapeHtml(value) {
        if (value === null || value === undefined) {
            return '';
        }
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    formatDuration(seconds) {
        const numeric = Number(seconds);
        if (!Number.isFinite(numeric) || numeric <= 0) {
            return '0 秒';
        }

        const absolute = Math.max(0, numeric);
        const unitConfig = [
            { threshold: 60, unit: '秒', divisor: 1 },
            { threshold: 3600, unit: '分钟', divisor: 60 },
            { threshold: 86400, unit: '小时', divisor: 3600 },
            { threshold: Infinity, unit: '天', divisor: 86400 },
        ];

        const selected = unitConfig.find(({ threshold }) => absolute < threshold) || unitConfig[0];
        const rawValue = absolute / selected.divisor;
        const rounded = Math.round(rawValue * 100) / 100;
        const valueStr = Number.isInteger(rounded)
            ? String(rounded)
            : rounded.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');

        return `${valueStr} ${selected.unit}`;
    }

    // 根据标的类型修正价格到允许的最小单位
    correctPriceStep(price, symbol) {
        if (!price || isNaN(price)) return price;
        
        const numPrice = parseFloat(price);
        
        // 根据标的类型确定最小单位
        let step = 0.01; // 默认股票最小单位
        
        if (symbol) {
            const upperSymbol = symbol.toUpperCase();
            // NQ/MNQ 期货最小单位是0.25
            if (upperSymbol.includes('NQ') || upperSymbol.includes('MNQ')) {
                step = 0.25;
            }
            // ES/MES 期货最小单位是0.25
            else if (upperSymbol.includes('ES') || upperSymbol.includes('MES')) {
                step = 0.25;
            }
            // YM/MYM 期货最小单位是1.0
            else if (upperSymbol.includes('YM') || upperSymbol.includes('MYM')) {
                step = 1.0;
            }
            // RTY/M2K 期货最小单位是0.1
            else if (upperSymbol.includes('RTY') || upperSymbol.includes('M2K')) {
                step = 0.1;
            }
            else if (upperSymbol.startsWith('VX')) {
                step = 0.05;
            }
        }
        
        // 修正价格到最近的有效单位
        const correctedPrice = Math.round(numPrice / step) * step;
        return parseFloat(correctedPrice.toFixed(step < 1 ? 2 : 0));
    }

    showAlert(type, message) {
        if (this.uiController?.showAlert) {
            return this.uiController.showAlert(type, message);
        }
        const logMethod = type === 'danger' || type === 'error' ? 'error' : 'log';
        console[logMethod]?.(message);
    }

    // Symbol combobox functionality
    initSymbolCombobox() {
        return this.uiController?.initSymbolCombobox?.();
    }

    setupSymbolInput(inputId, dropdownId) {
        return this.uiController?.setupSymbolInput?.(inputId, dropdownId);
    }

    setActiveDropdownItem(items, activeIndex) {
        return this.uiController?.setActiveDropdownItem?.(items, activeIndex);
    }

    showHistoricalSymbols(dropdownId) {
        return this.uiController?.showHistoricalSymbols?.(dropdownId);
    }

    // 编辑订单
    editOrder(orderId, symbol, action, quantity, orderType, limitPrice) {
        return this.orderManager.editOrder(orderId, symbol, action, quantity, orderType, limitPrice);
    }
    async loadStrategies(options = {}) {
        if (!this.strategiesManager || typeof this.strategiesManager.loadStrategies !== 'function') {
            return 'failed';
        }
        return this.strategiesManager.loadStrategies(options);
    }

    resolveStrategySymbol(strategy) {
        if (!this.strategiesManager || typeof this.strategiesManager.resolveStrategySymbol !== 'function') {
            return '';
        }
        return this.strategiesManager.resolveStrategySymbol(strategy);
    }

    formatStrategyDataReception(strategy) {
        if (!this.strategiesManager || typeof this.strategiesManager.formatStrategyDataReception !== 'function') {
            return {
                text: '-',
                tooltip: '',
                active: false,
                awaitingData: false,
                reason: '',
                isReceiving: false,
                subscription: null,
                symbol: '',
                statusLabel: '-',
                toggleDisabled: false,
            };
        }
        return this.strategiesManager.formatStrategyDataReception(strategy);
    }

    displayStrategies(strategies) {
        if (!this.strategiesManager || typeof this.strategiesManager.displayStrategies !== 'function') {
            return;
        }
        this.strategiesManager.displayStrategies(strategies);
    }

    async toggleStrategy(name, enabled, options = {}) {
        if (!this.strategiesManager || typeof this.strategiesManager.toggleStrategy !== 'function') {
            return;
        }
        return this.strategiesManager.toggleStrategy(name, enabled, options);
    }

    async toggleStrategyDataReception(strategy, args = {}) {
        if (!this.strategiesManager || typeof this.strategiesManager.toggleStrategyDataReception !== 'function') {
            return;
        }
        return this.strategiesManager.toggleStrategyDataReception(strategy, args);
    }

    async editStrategy(name) {
        if (!this.strategiesManager || typeof this.strategiesManager.editStrategy !== 'function') {
            return;
        }
        return this.strategiesManager.editStrategy(name);
    }

    createDefaultStrategyChartState() {
        if (this.monitoringManager?.createDefaultStrategyChartState) {
            return this.monitoringManager.createDefaultStrategyChartState();
        }
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

    createEmptyStrategyDetailsState() {
        return {
            name: null,
            basicInfo: null,
            strategyType: null,
            initialSymbol: '',
            defaultTabTarget: '#charts',
            tabLoaded: {},
            fetches: {},
            internalInfo: null,
            internalInfoCached: false,
            preloadedRiskSettings: null,
        };
    }

    getStrategyFromCache(name) {
        if (!Array.isArray(this.strategies) || !name) {
            return null;
        }
        return this.strategies.find((item) => item && item.name === name) || null;
    }

    getStrategyChartState() {
        if (this.monitoringManager?.getStrategyChartState) {
            return this.monitoringManager.getStrategyChartState();
        }
        if (!this.strategyChartState) {
            this.strategyChartState = this.createDefaultStrategyChartState();
        }
        return this.strategyChartState;
    }

    getStrategyChartRefreshIntervalMs(intervalValue) {
        if (this.monitoringManager?.getStrategyChartRefreshIntervalMs) {
            return this.monitoringManager.getStrategyChartRefreshIntervalMs(intervalValue);
        }
        const key = normalizeIntervalKey(intervalValue || '');
        const minutes = INTERVAL_MINUTES_MAP[key] || 5;
        return Math.max(15000, minutes * 60000);
    }

    getStrategyChartTimeUnit(intervalValue) {
        if (this.monitoringManager?.getStrategyChartTimeUnit) {
            return this.monitoringManager.getStrategyChartTimeUnit(intervalValue);
        }
        const value = (intervalValue || '').toLowerCase();
        if (value.includes('day')) return 'day';
        if (value.includes('hour')) return 'hour';
        return 'minute';
    }

    getLocalDayKey(timestamp) {
        const date = timestamp instanceof Date ? timestamp : new Date(Number(timestamp));
        if (!Number.isFinite(date.getTime())) {
            return '';
        }
        const y = date.getFullYear();
        const m = String(date.getMonth() + 1).padStart(2, '0');
        const d = String(date.getDate()).padStart(2, '0');
        return `${y}-${m}-${d}`;
    }

    resolveStrategySymbol(strategy) {
        if (!strategy) return '';
        const candidates = [];
        const pushCandidate = (value) => {
            if (typeof value === 'string' && value.trim()) {
                candidates.push(value.trim());
            }
        };
        pushCandidate(strategy.primary_symbol);
        pushCandidate(strategy.symbol);
        if (Array.isArray(strategy.symbols) && strategy.symbols.length) {
            pushCandidate(strategy.symbols[0]);
        }
        if (strategy.contract && typeof strategy.contract.symbol === 'string') {
            pushCandidate(strategy.contract.symbol);
        }
        if (Array.isArray(strategy.contracts) && strategy.contracts.length) {
            pushCandidate(strategy.contracts[0]?.symbol);
            pushCandidate(strategy.contracts[0]?.local_symbol);
        }
        if (typeof strategy.asset === 'string') {
            pushCandidate(strategy.asset);
        }
        if (strategy.instrument && typeof strategy.instrument.symbol === 'string') {
            pushCandidate(strategy.instrument.symbol);
        }
        const resolved = candidates.find(Boolean);
        return resolved ? resolved.toUpperCase() : '';
    }

    updateStrategyChartSymbol(symbol, options = {}) {
        const normalized = typeof symbol === 'string' ? symbol.trim() : '';
        if (!normalized) {
            return false;
        }
        const upper = normalized.toUpperCase();
        const state = this.getStrategyChartState();
        if (state.symbol === upper) {
            return false;
        }
        state.symbol = upper;
        this.updateStrategyChartSummary();
        if (options.reload && state.currentStrategy) {
            this.loadStrategyChart(state.currentStrategy, {
                symbol: upper,
                forceRefresh: true,
                forceExecutions: options.forceExecutions
            }).catch((err) => console.warn('Failed to reload strategy chart after symbol update:', err));
        } else if (options.ensureLoaded && state.currentStrategy && !state.fullBars.length) {
            this.loadStrategyChart(state.currentStrategy, {
                symbol: upper,
                forceExecutions: options.forceExecutions
            }).catch((err) => console.warn('Failed to load strategy chart for symbol:', err));
        }
        return true;
    }

    setupStrategyChartTab() {
        const state = this.getStrategyChartState();
        let intervalSelect = document.getElementById('strategyChartInterval');
        if (intervalSelect) {
            const cloned = intervalSelect.cloneNode(true);
            intervalSelect.parentNode.replaceChild(cloned, intervalSelect);
            intervalSelect = cloned;
        }
        let durationSelect = document.getElementById('strategyChartDuration');
        if (durationSelect) {
            const cloned = durationSelect.cloneNode(true);
            durationSelect.parentNode.replaceChild(cloned, durationSelect);
            durationSelect = cloned;
        }

        if (intervalSelect) {
            intervalSelect.value = state.interval || intervalSelect.value || '5 mins';
        }
        this.refreshStrategyChartDuration(intervalSelect ? intervalSelect.value : state.interval, {
            preferredValue: state.duration,
            selectEl: durationSelect
        });
        if (durationSelect) {
            durationSelect.value = state.duration || durationSelect.value || '1 D';
        }

        if (intervalSelect) {
            intervalSelect.addEventListener('change', () => {
                state.interval = intervalSelect.value || '5 mins';
                this.refreshStrategyChartDuration(state.interval, {
                    preferredValue: state.duration,
                    selectEl: document.getElementById('strategyChartDuration')
                });
                const durationEl = document.getElementById('strategyChartDuration');
                if (durationEl) {
                    state.duration = durationEl.value || state.duration;
                }
                this.stopStrategyChartRealtime({ silent: true });
                this.stopStrategyChartPlayback({ restoreFull: false, silent: true });
                if (state.currentStrategy && state.symbol) {
                    this.loadStrategyChart(state.currentStrategy, {
                        symbol: state.symbol,
                        forceRefresh: true
                    }).catch((err) => console.warn('Failed to reload strategy chart after interval change:', err));
                }
            });
        }

        if (durationSelect) {
            durationSelect.addEventListener('change', () => {
                state.duration = durationSelect.value || '1 D';
                this.stopStrategyChartRealtime({ silent: true });
                this.stopStrategyChartPlayback({ restoreFull: false, silent: true });
                if (state.currentStrategy && state.symbol) {
                    this.loadStrategyChart(state.currentStrategy, {
                        symbol: state.symbol,
                        forceRefresh: true
                    }).catch((err) => console.warn('Failed to reload strategy chart after duration change:', err));
                }
            });
        }

        const realtimeBtnEl = document.getElementById('strategyChartRealtimeBtn');
        if (realtimeBtnEl) {
            const cloned = realtimeBtnEl.cloneNode(true);
            realtimeBtnEl.parentNode.replaceChild(cloned, realtimeBtnEl);
            cloned.addEventListener('click', () => this.toggleStrategyChartRealtime());
        }

        const replayBtnEl = document.getElementById('strategyChartReplayBtn');
        if (replayBtnEl) {
            const cloned = replayBtnEl.cloneNode(true);
            replayBtnEl.parentNode.replaceChild(cloned, replayBtnEl);
            cloned.addEventListener('click', () => this.toggleStrategyChartPlayback());
        }

        const tabTriggerEl = document.getElementById('strategy-chart-tab');
        if (tabTriggerEl) {
            const cloned = tabTriggerEl.cloneNode(true);
            tabTriggerEl.parentNode.replaceChild(cloned, tabTriggerEl);
            cloned.addEventListener('shown.bs.tab', () => {
                const chartState = this.getStrategyChartState();
                if (!chartState.currentStrategy) return;
                if (!chartState.symbol) {
                    this.updateStrategyChartStatus('缺少标的');
                    return;
                }

                const detailsState = this.strategyDetailsState || {};
                const tabLoaded = detailsState.tabLoaded?.['strategy-chart'];

                if (!tabLoaded) {
                    if (chartState.loading) {
                        this.updateStrategyChartStatus('加载中...');
                    }
                    return;
                }

                if (chartState.loading) {
                    this.updateStrategyChartStatus('加载中...');
                    return;
                }

                const hasBars = Array.isArray(chartState.fullBars) && chartState.fullBars.length > 0;
                if (!hasBars) {
                    this.loadStrategyChart(chartState.currentStrategy, {
                        symbol: chartState.symbol,
                        forceExecutions: !Array.isArray(chartState.orders) || !chartState.orders.length
                    }).catch((err) => console.warn('Failed to load strategy chart on tab show:', err));
                } else {
                    this.renderStrategyChart({ redraw: true, forceResize: true });
                    this.updateStrategyChartStatus();
                }
            });
        }

        const modalEl = document.getElementById('strategyDetailsModal');
        if (modalEl && modalEl.dataset.strategyChartCleanup !== 'true') {
            modalEl.addEventListener('hidden.bs.modal', () => {
                this.resetStrategyChartState({ keepControls: true });
            });
            modalEl.dataset.strategyChartCleanup = 'true';
        }

        state.initialized = true;
        state.interval = intervalSelect ? intervalSelect.value || '5 mins' : state.interval || '5 mins';
        state.duration = durationSelect ? durationSelect.value || '1 D' : state.duration || '1 D';
        this.updateStrategyChartSummary();
        this.updateStrategyChartButtons();
        this.updateStrategyChartStatus('');
    }

    refreshStrategyChartDuration(intervalValue, options = {}) {
        if (this.monitoringManager?.refreshStrategyChartDuration) {
            return this.monitoringManager.refreshStrategyChartDuration(intervalValue, options);
        }
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

    resetStrategyChartState(options = {}) {
        const state = this.getStrategyChartState();
        this.stopStrategyChartRealtime({ silent: true });
        this.stopStrategyChartPlayback({ restoreFull: false, silent: true });
        if (options.keepControls) {
            const intervalSelect = document.getElementById('strategyChartInterval');
            if (intervalSelect && state.interval) {
                intervalSelect.value = state.interval;
            }
            this.refreshStrategyChartDuration(state.interval, { preferredValue: state.duration });
            const durationSelect = document.getElementById('strategyChartDuration');
            if (durationSelect && state.duration) {
                durationSelect.value = state.duration;
            }
        } else {
            state.interval = '5 mins';
            state.duration = '1 D';
            const intervalSelect = document.getElementById('strategyChartInterval');
            if (intervalSelect) intervalSelect.value = state.interval;
            this.refreshStrategyChartDuration(state.interval, { preferredValue: state.duration });
            const durationSelect = document.getElementById('strategyChartDuration');
            if (durationSelect) durationSelect.value = state.duration;
        }
        state.currentStrategy = options.currentStrategy ?? null;
        state.symbol = options.symbol ? String(options.symbol).trim().toUpperCase() : (options.resetSymbol ? '' : state.symbol || '');
        state.fullBars = [];
        state.orders = [];
        state.visibleCount = 0;
        state.loadToken = null;
        state.loading = false;
        state.useCandlestick = true;
        if (!options.preserveCanvas) {
            this.destroyStrategyChart();
            const container = document.getElementById('strategyChartContainer');
            if (container) {
                container.innerHTML = '<div class="text-center text-muted py-5">加载策略后将显示K线图。</div>';
            }
        }
        this.updateStrategyChartSummary();
        this.updateStrategyChartButtons();
        this.updateStrategyChartStatus('');
    }

    destroyStrategyChart() {
        if (this.strategyChartInstance) {
            try {
                this.strategyChartInstance.destroy();
            } catch (err) {
                console.warn('Failed to destroy strategy chart instance:', err);
            }
            this.strategyChartInstance = null;
        }
    }

    normalizeStrategyChartBars(data) {
        if (!Array.isArray(data)) return [];
        const toNumber = (value, fallback = NaN) => {
            const num = Number(value);
            return Number.isFinite(num) ? num : fallback;
        };
        const bars = data.map((bar) => {
            const rawTime = bar?.time ?? bar?.date;
            const parsed = this.parseMonitorChartTime(rawTime);
            if (!(parsed instanceof Date) || Number.isNaN(parsed.getTime())) {
                return null;
            }
            const open = toNumber(bar?.open ?? bar?.close);
            const high = toNumber(bar?.high ?? bar?.close);
            const low = toNumber(bar?.low ?? bar?.close);
            const close = toNumber(bar?.close);
            if (!Number.isFinite(close)) {
                return null;
            }
            return {
                x: parsed.getTime(),
                o: Number.isFinite(open) ? open : close,
                h: Number.isFinite(high) ? high : close,
                l: Number.isFinite(low) ? low : close,
                c: close,
                volume: toNumber(bar?.volume, 0) || 0
            };
        }).filter(Boolean);
        bars.sort((a, b) => a.x - b.x);
        const maxPoints = MONITOR_CHART_MAX_POINTS || 3000;
        if (bars.length > maxPoints) {
            return bars.slice(bars.length - maxPoints);
        }
        return bars;
    }

    normalizeStrategyOrders(executions) {
        if (!Array.isArray(executions)) return [];
        const normalized = [];
        executions.forEach((exe) => {
            const status = String(exe?.status || '').toLowerCase();
            if (!status.includes('fill')) {
                return;
            }
            const action = String(exe?.action || exe?.side || '').toLowerCase();
            let side = null;
            if (action.includes('buy')) side = 'buy';
            else if (action.includes('sell')) side = 'sell';
            if (!side) return;
            const priceCandidates = [exe?.price, exe?.avg_price, exe?.average_price, exe?.fill_price, exe?.filled_avg_price];
            let price = null;
            for (const candidate of priceCandidates) {
                const num = Number(candidate);
                if (Number.isFinite(num) && num !== 0) {
                    price = num;
                    break;
                }
            }
            if (!Number.isFinite(price)) return;
            const timeSource = exe?.execution_time || exe?.executed_at || exe?.completed_at || exe?.created_at;
            const parsed = this.parseMonitorChartTime(timeSource);
            if (!(parsed instanceof Date) || Number.isNaN(parsed.getTime())) {
                return;
            }
            const timestamp = parsed.getTime();
            normalized.push({
                time: timestamp,
                price,
                side,
                dayKey: this.getLocalDayKey(timestamp)
            });
        });
        normalized.sort((a, b) => a.time - b.time);
        return normalized;
    }

    applyStrategyChartOrders(executions, options = {}) {
        if (!Array.isArray(executions)) return;
        const state = this.getStrategyChartState();
        state.orders = this.normalizeStrategyOrders(executions);
        if (options.redraw !== false && this.strategyChartInstance && state.fullBars.length) {
            this.renderStrategyChart();
        }
    }

    setStrategyChartRiskLevels(levels = {}) {
        const state = this.getStrategyChartState();
        const normalizePrice = (value) => {
            const num = Number(value);
            return Number.isFinite(num) ? num : null;
        };

        let changed = false;

        if (typeof levels.slEnabled === 'boolean') {
            if (state.slEnabled !== levels.slEnabled) {
                state.slEnabled = levels.slEnabled;
                changed = true;
            }
        }
        if (typeof levels.tpEnabled === 'boolean') {
            if (state.tpEnabled !== levels.tpEnabled) {
                state.tpEnabled = levels.tpEnabled;
                changed = true;
            }
        }

        if ('slPrice' in levels || 'sl_price' in levels) {
            const value = 'slPrice' in levels ? levels.slPrice : levels.sl_price;
            const next = state.slEnabled ? normalizePrice(value) : null;
            if (state.currentSlPrice !== next) {
                state.currentSlPrice = next;
                changed = true;
            }
        }

        if ('tpPrice' in levels || 'tp_price' in levels) {
            const value = 'tpPrice' in levels ? levels.tpPrice : levels.tp_price;
            const next = state.tpEnabled ? normalizePrice(value) : null;
            if (state.currentTpPrice !== next) {
                state.currentTpPrice = next;
                changed = true;
            }
        }

        if (changed && this.strategyChartInstance && state.fullBars && state.fullBars.length) {
            this.renderStrategyChart();
        }
    }

    renderStrategyChart(options = {}) {
        const state = this.getStrategyChartState();
        const container = document.getElementById('strategyChartContainer');
        if (!container) return;

        if (!state.fullBars || !state.fullBars.length) {
            this.destroyStrategyChart();
            container.innerHTML = '<div class="alert alert-warning mb-0">暂无K线数据</div>';
            this.updateStrategyChartStatus(options.extraStatus || '暂无K线数据');
            return;
        }

        if (!document.getElementById('strategyChartCanvas')) {
            container.innerHTML = '<canvas id="strategyChartCanvas" style="width:100%;height:100%;"></canvas>';
        }
        const canvas = document.getElementById('strategyChartCanvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        const total = state.fullBars.length;
        const visibleCount = Math.max(1, Math.min(state.visibleCount || total, total));
        const ohlc = state.fullBars.slice(0, visibleCount);
        let volumeData = ohlc.map((bar) => ({ x: bar.x, y: bar.volume }));
        const cutoff = ohlc.length ? ohlc[ohlc.length - 1].x : null;
        const visibleOrders = cutoff == null
            ? []
            : state.orders.filter((order) => order.time <= cutoff);
        const buyOrders = visibleOrders
            .filter((order) => order.side === 'buy')
            .map((order) => ({ x: order.time, y: order.price, order }));
        const sellOrders = visibleOrders
            .filter((order) => order.side === 'sell')
            .map((order) => ({ x: order.time, y: order.price, order }));
        const slPrice = state.slEnabled ? state.currentSlPrice : null;
        const tpPrice = state.tpEnabled ? state.currentTpPrice : null;
        const slLine = Number.isFinite(slPrice)
            ? ohlc.map((bar) => ({ x: bar.x, y: slPrice }))
            : [];
        const tpLine = Number.isFinite(tpPrice)
            ? ohlc.map((bar) => ({ x: bar.x, y: tpPrice }))
            : [];
        let maxVolume = volumeData.reduce((acc, point) => Math.max(acc, point.y || 0), 0);
        let volumeScaleMax = maxVolume ? maxVolume * 3 : undefined;
        let volumeDatasetForCreation = {
            type: 'bar',
            label: 'Volume',
            data: volumeData,
            yAxisID: 'volume',
            backgroundColor: 'rgba(99, 102, 241, 0.35)',
            borderColor: 'rgba(79, 70, 229, 0.7)',
            borderWidth: 1,
            parsing: false,
        };
        let volumeDatasetConfigForUpdate = { ...volumeDatasetForCreation };
        const extraParts = [];
        if (visibleOrders.length) {
            extraParts.push(`成交 ${visibleOrders.length} 笔`);
        }
        if (options.extraStatus) {
            extraParts.push(options.extraStatus);
        }

        const useCandles = state.useCandlestick !== false && this.candlestickSupported;
        const desiredType = useCandles ? 'candlestick' : 'line';
        if (!useCandles) {
            extraParts.push('折线展示');
        }
        const lineSeries = ohlc.map((bar) => ({ x: bar.x, y: bar.c }));
        if (this.strategyChartInstance && this.strategyChartInstance.config?.type !== desiredType) {
            this.destroyStrategyChart();
        }

        let baseDatasetConfigForUpdate;
        let baseDatasetForCreation;

        if (useCandles) {
            const candlestickConfig = this.createCandlestickChartConfig({
                symbol: state.symbol || '',
                ohlcData: ohlc,
                labels: [],
                includeLabels: false,
                priceAxisId: 'y',
                volumeAxisId: 'volume',
                volumeDatasetOptions: {
                    backgroundColor: 'rgba(99, 102, 241, 0.35)',
                    borderColor: 'rgba(79, 70, 229, 0.7)',
                    borderWidth: 1,
                },
            });
            const priceDataset = candlestickConfig.datasets.price;
            const volumeDataset = candlestickConfig.datasets.volume;
            baseDatasetForCreation = { ...priceDataset, data: ohlc };
            baseDatasetConfigForUpdate = { ...priceDataset, data: ohlc };
            volumeData = candlestickConfig.meta.volumeData;
            maxVolume = candlestickConfig.meta.maxVolume;
            volumeScaleMax = maxVolume ? maxVolume * 3 : undefined;
            volumeDatasetForCreation = { ...volumeDataset, data: volumeData };
            volumeDatasetConfigForUpdate = { ...volumeDataset, data: volumeData };
        } else {
            baseDatasetConfigForUpdate = {
                type: 'line',
                label: `${state.symbol || ''} Close`,
                data: lineSeries,
                yAxisID: 'y',
                parsing: false,
                borderColor: 'rgba(54, 162, 235, 0.85)',
                backgroundColor: 'rgba(54, 162, 235, 0.12)',
                borderWidth: 1.5,
                pointRadius: 0,
                pointHoverRadius: 2,
                spanGaps: true,
                tension: 0.2,
                fill: false,
            };
            baseDatasetForCreation = { ...baseDatasetConfigForUpdate };
            volumeDatasetConfigForUpdate = { ...volumeDatasetForCreation };
        }

        if (!this.strategyChartInstance) {
            this.strategyChartInstance = new Chart(ctx, {
                type: desiredType,
                data: {
                    datasets: [
                        baseDatasetForCreation,
                        volumeDatasetForCreation,
                        {
                            type: 'scatter',
                            label: 'Buy Orders',
                            data: buyOrders,
                            yAxisID: 'y',
                            parsing: false,
                            pointStyle: 'triangle',
                            pointRadius: 6,
                            pointHoverRadius: 7,
                            pointBackgroundColor: 'rgba(34, 197, 94, 0.95)',
                            pointBorderColor: '#15803d',
                            pointBorderWidth: 1.5,
                            pointRotation: 0,
                            clip: false,
                            showLine: false,
                            hitRadius: 10,
                            tooltip: {
                                callbacks: {
                                    label: (context) => {
                                        const raw = context.raw?.order;
                                        if (raw) {
                                            const when = new Date(raw.time).toLocaleString();
                                            return `买入 ${raw.price.toFixed(2)} @ ${when}`;
                                        }
                                        return `买入 ${context.parsed.y}`;
                                    },
                                },
                            },
                        },
                        {
                            type: 'scatter',
                            label: 'Sell Orders',
                            data: sellOrders,
                            yAxisID: 'y',
                            parsing: false,
                            pointStyle: 'triangle',
                            pointRadius: 6,
                            pointHoverRadius: 7,
                            pointBackgroundColor: 'rgba(239, 68, 68, 0.95)',
                            pointBorderColor: '#b91c1c',
                            pointBorderWidth: 1.5,
                            pointRotation: 180,
                            clip: false,
                            showLine: false,
                            hitRadius: 10,
                            tooltip: {
                                callbacks: {
                                    label: (context) => {
                                        const raw = context.raw?.order;
                                        if (raw) {
                                            const when = new Date(raw.time).toLocaleString();
                                            return `卖出 ${raw.price.toFixed(2)} @ ${when}`;
                                        }
                                        return `卖出 ${context.parsed.y}`;
                                    },
                                },
                            },
                        },
                        {
                            type: 'line',
                            label: 'Stop Loss',
                            data: slLine,
                            yAxisID: 'y',
                            parsing: false,
                            borderColor: 'rgba(220, 53, 69, 0.75)',
                            borderWidth: 1.5,
                            borderDash: [6, 4],
                            pointRadius: 0,
                            pointHoverRadius: 0,
                            fill: false,
                            stepped: 'after',
                            spanGaps: true,
                            hidden: !slLine.length,
                            tooltip: {
                                callbacks: {
                                    label: (context) => `止损 ${this.formatNumber(context.parsed.y)}`,
                                },
                            },
                        },
                        {
                            type: 'line',
                            label: 'Take Profit',
                            data: tpLine,
                            yAxisID: 'y',
                            parsing: false,
                            borderColor: 'rgba(25, 135, 84, 0.75)',
                            borderWidth: 1.5,
                            borderDash: [6, 4],
                            pointRadius: 0,
                            pointHoverRadius: 0,
                            fill: false,
                            stepped: 'after',
                            spanGaps: true,
                            hidden: !tpLine.length,
                            tooltip: {
                                callbacks: {
                                    label: (context) => `止盈 ${this.formatNumber(context.parsed.y)}`,
                                },
                            },
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    interaction: {
                        intersect: false,
                        mode: 'nearest',
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top',
                        },
                        tooltip: {
                            intersect: false,
                            mode: 'nearest',
                        },
                    },
                    scales: {
                        x: {
                            type: 'time',
                            adapters: {
                                date: { locale: 'zh-CN' },
                            },
                            time: {
                                unit: this.getStrategyChartTimeUnit(state.interval),
                                tooltipFormat: 'MMM dd HH:mm',
                            },
                            ticks: {
                                maxRotation: 45,
                                autoSkip: true,
                            },
                        },
                        y: {
                            position: 'left',
                            title: {
                                display: true,
                                text: 'Price',
                            },
                        },
                        volume: {
                            position: 'right',
                            grid: { display: false },
                            ticks: { display: false },
                            title: { display: true, text: 'Volume' },
                            max: volumeScaleMax,
                        },
                    },
                },
            });
            if (options.forceResize) {
                requestAnimationFrame(() => {
                    try {
                        this.strategyChartInstance?.resize();
                    } catch (err) {
                        console.warn('Strategy chart resize failed after init:', err);
                    }
                });
            }
        } else {
            const datasets = this.strategyChartInstance.data.datasets || [];
            if (datasets[0]) {
                Object.assign(datasets[0], baseDatasetConfigForUpdate);
            }
            if (datasets[1]) {
                Object.assign(datasets[1], volumeDatasetConfigForUpdate);
            }
            if (datasets[2]) datasets[2].data = buyOrders;
            if (datasets[3]) datasets[3].data = sellOrders;
            if (datasets[4]) {
                datasets[4].data = slLine;
                datasets[4].hidden = !slLine.length;
            }
            if (datasets[5]) {
                datasets[5].data = tpLine;
                datasets[5].hidden = !tpLine.length;
            }
            this.strategyChartInstance.config.type = desiredType;
            const xScale = this.strategyChartInstance.options?.scales?.x;
            if (xScale) {
                const nextUnit = this.getStrategyChartTimeUnit(state.interval);
                const currentTimeConfig = xScale.time;
                const normalizedTimeConfig = (currentTimeConfig && typeof currentTimeConfig === 'object' && !Array.isArray(currentTimeConfig))
                    ? { ...currentTimeConfig }
                    : {};
                normalizedTimeConfig.unit = nextUnit;
                xScale.time = normalizedTimeConfig;
            }
            const volumeScale = this.strategyChartInstance.options?.scales?.volume;
            if (volumeScale) {
                if (typeof volumeScaleMax === 'number') {
                    volumeScale.max = volumeScaleMax;
                } else {
                    delete volumeScale.max;
                }
            }
            const updateMode = options.redraw === true ? 'default' : 'none';
            this.strategyChartInstance.update(updateMode);
            if (options.forceResize && typeof this.strategyChartInstance.resize === 'function') {
                try {
                    this.strategyChartInstance.resize();
                } catch (err) {
                    console.warn('Strategy chart resize failed:', err);
                }
            }
        }

        this.updateStrategyChartStatus(extraParts.join(' · '));
    }

    updateStrategyChartSummary() {
        const summaryEl = document.getElementById('strategyChartSummary');
        if (!summaryEl) return;
        const state = this.getStrategyChartState();
        const parts = [];
        if (state.symbol) parts.push(state.symbol);
        if (state.interval) parts.push(state.interval);
        if (state.duration) parts.push(state.duration);
        summaryEl.textContent = parts.length ? parts.join(' · ') : '尚未加载图表';
    }

    updateStrategyChartButtons() {
        const state = this.getStrategyChartState();
        const realtimeBtn = document.getElementById('strategyChartRealtimeBtn');
        if (realtimeBtn) {
            const active = state.isRealtime;
            realtimeBtn.classList.toggle('btn-success', active);
            realtimeBtn.classList.toggle('btn-outline-success', !active);
            realtimeBtn.innerHTML = active ? '<i class="fas fa-pause"></i> 停止' : '<i class="fas fa-play"></i> 实时';
            realtimeBtn.disabled = !active && state.loading;
        }
        const replayBtn = document.getElementById('strategyChartReplayBtn');
        if (replayBtn) {
            const playing = state.isReplaying;
            replayBtn.classList.toggle('btn-primary', playing);
            replayBtn.classList.toggle('btn-outline-secondary', !playing);
            replayBtn.innerHTML = playing ? '<i class="fas fa-stop"></i> 停止回放' : '<i class="fas fa-history"></i> 回放';
            replayBtn.disabled = !playing && state.loading;
        }
    }

    updateStrategyChartStatus(extraText = '') {
        const statusEl = document.getElementById('strategyChartStatus');
        if (!statusEl) return;
        const state = this.getStrategyChartState();
        if (state.loading) {
            statusEl.textContent = '加载中...';
            return;
        }
        if (!state.fullBars || !state.fullBars.length) {
            statusEl.textContent = extraText || '';
            return;
        }
        const total = state.fullBars.length;
        const visible = Math.max(1, Math.min(state.visibleCount || total, total));
        let text = `已显示 ${visible}/${total} 根K线`;
        if (extraText) {
            text += ` · ${extraText}`;
        }
        if (state.isRealtime) {
            text = `实时更新 · ${text}`;
        } else if (state.isReplaying) {
            text = `回放中 · ${text}`;
        }
        statusEl.textContent = text;
    }

    async loadStrategyChart(name, options = {}) {
        const state = this.getStrategyChartState();
        if (!name) return;
        state.currentStrategy = name;
        state.pendingCandlestickUpgradeToken = null;
        if (options.symbol) {
            const normalizedSymbol = String(options.symbol).trim().toUpperCase();
            if (normalizedSymbol) {
                state.symbol = normalizedSymbol;
            }
        }

        const intervalSelect = document.getElementById('strategyChartInterval');
        if (intervalSelect) {
            state.interval = intervalSelect.value || state.interval || '5 mins';
        } else if (!state.interval) {
            state.interval = '5 mins';
        }
        const durationSelect = document.getElementById('strategyChartDuration');
        if (durationSelect) {
            state.duration = durationSelect.value || state.duration || '1 D';
        } else if (!state.duration) {
            state.duration = '1 D';
        }
        this.updateStrategyChartSummary();

        const container = document.getElementById('strategyChartContainer');
        if (!state.symbol) {
            if (container) {
                container.innerHTML = '<div class="alert alert-warning mb-0">策略未配置标的，无法加载K线图。</div>';
            }
            state.fullBars = [];
            state.visibleCount = 0;
            this.updateStrategyChartButtons();
            this.updateStrategyChartStatus('缺少标的');
            return;
        }

        const loadToken = Symbol('strategy-chart-load');
        state.loadToken = loadToken;
        state.loading = true;
        this.updateStrategyChartButtons();
        this.updateStrategyChartStatus('加载中...');
        console.log('[StrategyChart] 开始加载策略K线数据', {
            name,
            symbol: state.symbol,
            interval: state.interval,
            duration: state.duration,
            options
        });
        if (container && !this.strategyChartInstance) {
            container.innerHTML = '<div class="text-center text-muted py-5"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">加载图表数据中...</p></div>';
        }

        const interval = state.interval || '5 mins';
        const duration = state.duration || '1 D';
        const useCache = options.useCache !== false;
        const forceRefresh = options.forceRefresh === true;
        const forceExecutions = options.forceExecutions === true;

        try {
            const pluginPromise = this.ensureCandlestickPlugin();

            let executions = Array.isArray(options.executions) ? options.executions : null;
            if (!executions || forceExecutions) {
                try {
                    executions = await this.fetchStrategyExecutions(name, {
                        limit: options.executionLimit || 500,
                        force: forceExecutions
                    });
                } catch (err) {
                    console.warn('Failed to load strategy executions for chart overlay:', err);
                    executions = executions || [];
                }
            }
            if (Array.isArray(executions)) {
                this.applyStrategyChartOrders(executions, { redraw: false });
            }

            const response = await this.apiCall('/chart_data', {
                method: 'POST',
                body: JSON.stringify({
                    symbol: state.symbol,
                    duration,
                    bar_size: interval,
                    data_type: 'TRADES',
                    use_cache: useCache,
                    force_refresh: forceRefresh
                }),
                allowReadOnly: true,
                timeout: 70000
            });

            console.log('[StrategyChart] 图表数据接口返回', {
                success: !!response,
                points: Array.isArray(response?.data) ? response.data.length : null,
                loadTokenMatches: state.loadToken === loadToken
            });

            if (state.loadToken !== loadToken) {
                console.log('[StrategyChart] loadToken 已变化，放弃当前渲染');
                return;
            }

            if (!response || !Array.isArray(response.data) || !response.data.length) {
                this.destroyStrategyChart();
                if (container) {
                    container.innerHTML = '<div class="alert alert-warning mb-0">暂无K线数据</div>';
                }
                state.fullBars = [];
                state.visibleCount = 0;
                this.updateStrategyChartStatus('暂无K线数据');
                return;
            }

            const pluginStatus = await this.waitForCandlestickPlugin({ promise: pluginPromise });
            const pluginReady = pluginStatus.ready;
            console.log('[StrategyChart] 蜡烛图插件状态', { pluginReady, timedOut: pluginStatus.timedOut });
            state.useCandlestick = Boolean(pluginReady);
            if (!pluginReady) {
                if (!this.strategyChartFallbackNotified) {
                    console.warn('[StrategyChart] Candlestick plugin unavailable, falling back to line chart.');
                    this.strategyChartFallbackNotified = true;
                }
                if (pluginStatus.timedOut && pluginStatus.promise) {
                    const upgradeToken = Symbol('strategy-candlestick-upgrade');
                    state.pendingCandlestickUpgradeToken = upgradeToken;
                    pluginStatus.promise
                        .then((result) => {
                            if (!result || state.pendingCandlestickUpgradeToken !== upgradeToken) {
                                return;
                            }
                            state.pendingCandlestickUpgradeToken = null;
                            state.useCandlestick = true;
                            this.strategyChartFallbackNotified = false;
                            if (state.fullBars && state.fullBars.length) {
                                console.log('[StrategyChart] Candlestick plugin became available after timeout, upgrading chart.');
                                this.renderStrategyChart({ redraw: true });
                            }
                        })
                        .catch(() => {});
                }
            } else {
                state.pendingCandlestickUpgradeToken = null;
                this.strategyChartFallbackNotified = false;
            }

            const bars = this.normalizeStrategyChartBars(response.data);
            console.log('[StrategyChart] 归一化后的K线数量', { count: bars.length });
            if (!bars.length) {
                this.destroyStrategyChart();
                if (container) {
                    container.innerHTML = '<div class="alert alert-warning mb-0">暂无有效K线数据</div>';
                }
                state.fullBars = [];
                state.visibleCount = 0;
                this.updateStrategyChartStatus('暂无有效K线数据');
                return;
            }

            state.fullBars = bars;
            if (options.keepVisibleCount && Number.isFinite(state.visibleCount) && state.visibleCount > 0) {
                state.visibleCount = Math.min(Math.max(1, state.visibleCount), bars.length);
            } else {
                state.visibleCount = bars.length;
            }

            this.renderStrategyChart();
            this.updateStrategyChartSummary();
        } catch (error) {
            if (state.loadToken !== loadToken) {
                return;
            }
            console.error('Failed to load strategy chart:', error);
            if (container) {
                const message = error?.message || '加载策略图表失败';
                container.innerHTML = `<div class="alert alert-danger mb-0">${message}</div>`;
            }
            state.fullBars = [];
            state.visibleCount = 0;
            this.updateStrategyChartStatus('加载失败');
        } finally {
            if (state.loadToken === loadToken) {
                state.loadToken = null;
                state.loading = false;
                this.updateStrategyChartButtons();
                if (state.fullBars && state.fullBars.length) {
                    this.updateStrategyChartStatus();
                }
            }
        }
    }

    startStrategyChartRealtime() {
        const state = this.getStrategyChartState();
        if (!state.currentStrategy || !state.symbol) {
            this.updateStrategyChartStatus('缺少标的，无法实时刷新');
            return;
        }
        this.stopStrategyChartPlayback({ restoreFull: true, silent: true });
        this.stopStrategyChartRealtime({ silent: true });
        state.isRealtime = true;
        this.updateStrategyChartButtons();
        this.updateStrategyChartStatus();
        const refresh = () => {
            if (!state.currentStrategy) return;
            this.loadStrategyChart(state.currentStrategy, {
                symbol: state.symbol,
                keepVisibleCount: true,
                useCache: false,
                forceRefresh: true
            }).catch((err) => console.warn('Strategy realtime refresh failed:', err));
        };
        refresh();
        state.realtimeTimer = setInterval(refresh, this.getStrategyChartRefreshIntervalMs(state.interval));
    }

    stopStrategyChartRealtime(options = {}) {
        const state = this.getStrategyChartState();
        if (state.realtimeTimer) {
            clearInterval(state.realtimeTimer);
            state.realtimeTimer = null;
        }
        const wasRealtime = state.isRealtime;
        state.isRealtime = false;
        if (!options.silent && wasRealtime) {
            this.updateStrategyChartStatus();
        }
        this.updateStrategyChartButtons();
    }

    async startStrategyChartPlayback() {
        const state = this.getStrategyChartState();
        if (!state.currentStrategy) return;
        if (!state.fullBars || !state.fullBars.length) {
            await this.loadStrategyChart(state.currentStrategy, {
                symbol: state.symbol
            }).catch(() => {});
            if (!state.fullBars || !state.fullBars.length) {
                return;
            }
        }

        const todayKey = this.getLocalDayKey(Date.now());
        const todaysOrders = state.orders.filter((order) => order.dayKey === todayKey);
        if (!todaysOrders.length) {
            this.updateStrategyChartStatus('今日暂无成交记录');
            return;
        }

        const firstOrderTime = todaysOrders[0].time;
        const startIndex = state.fullBars.findIndex((bar) => bar.x >= firstOrderTime);
        if (startIndex === -1) {
            this.updateStrategyChartStatus('当前区间无今日成交对应的K线');
            return;
        }

        this.stopStrategyChartRealtime({ silent: true });
        this.stopStrategyChartPlayback({ restoreFull: false, silent: true });

        state.isReplaying = true;
        state.visibleCount = Math.max(1, startIndex + 1);
        this.renderStrategyChart();
        this.updateStrategyChartButtons();
        this.updateStrategyChartStatus();

        state.replayTimer = setInterval(() => {
            if (!state.isReplaying) {
                clearInterval(state.replayTimer);
                state.replayTimer = null;
                return;
            }
            if (state.visibleCount >= state.fullBars.length) {
                this.stopStrategyChartPlayback({ restoreFull: true, complete: true });
                return;
            }
            state.visibleCount = Math.min(state.visibleCount + 1, state.fullBars.length);
            this.renderStrategyChart();
        }, 1000);
    }

    stopStrategyChartPlayback(options = {}) {
        const state = this.getStrategyChartState();
        const { restoreFull = true, silent = false, complete = false } = options || {};
        if (state.replayTimer) {
            clearInterval(state.replayTimer);
            state.replayTimer = null;
        }
        const wasReplaying = state.isReplaying;
        state.isReplaying = false;
        if (restoreFull && state.fullBars && state.fullBars.length) {
            state.visibleCount = state.fullBars.length;
        }
        if (!silent && (wasReplaying || complete)) {
            this.renderStrategyChart({ extraStatus: complete ? '回放完成' : '' });
        } else if (!silent) {
            this.updateStrategyChartStatus(complete ? '回放完成' : '');
        }
        this.updateStrategyChartButtons();
        if (complete && silent) {
            this.updateStrategyChartStatus('回放完成');
        }
    }

    toggleStrategyChartRealtime() {
        const state = this.getStrategyChartState();
        if (state.isRealtime) {
            this.stopStrategyChartRealtime();
        } else {
            this.startStrategyChartRealtime();
        }
    }

    toggleStrategyChartPlayback() {
        const state = this.getStrategyChartState();
        if (state.isReplaying) {
            this.stopStrategyChartPlayback({ restoreFull: true });
        } else {
            this.startStrategyChartPlayback();
        }
    }

    async openStrategyDetails(name) {
        try {
            this.prepareStrategyDetailsModal(name);
            this.ensureStrategyDetailsTabListener();
            this.showStrategyDetailsModal();
            await this.initializeStrategyDetailsContent(name);
            this.activateStrategyDetailsDefaultTab();
            await this.requestActiveStrategyDetailsTabLoad();
        } catch (error) {
            console.error('Open details error:', error);
            this.showAlert('danger', 'Failed to open strategy details');
        }
    }

    initializeStrategyDetailsState(name) {
        this.strategyDetailsState = this.createEmptyStrategyDetailsState();
        this.strategyDetailsState.name = name;
    }

    prepareStrategyDetailsModal(name) {
        this.initializeStrategyDetailsState(name);
        this.currentStrategyDetails = name;

        const typeEl = document.getElementById('detailsStrategyType');
        if (typeEl) typeEl.textContent = '-';
        const trigEl = document.getElementById('detailsTriggerCount');
        if (trigEl) trigEl.textContent = '0';
        const lastEl = document.getElementById('detailsLastTriggered');
        if (lastEl) lastEl.textContent = '-';

        const titleEl = document.getElementById('detailsStrategyName');
        if (titleEl) {
            const newTitle = titleEl.cloneNode(true);
            titleEl.parentNode.replaceChild(newTitle, titleEl);
            newTitle.textContent = name;
            newTitle.title = '双击可重命名';
            newTitle.style.cursor = 'text';
            newTitle.ondblclick = () => {
                const original = newTitle.textContent || name;
                const input = document.createElement('input');
                input.type = 'text';
                input.className = 'form-control form-control-sm d-inline-block';
                input.style.width = Math.max(120, original.length * 9) + 'px';
                input.value = original;
                newTitle.replaceWith(input);
                input.focus();
                const commit = async () => {
                    const newName = input.value.trim();
                    if (!newName || newName === original) {
                        input.replaceWith(newTitle);
                        newTitle.textContent = original;
                        return;
                    }
                    try {
                        await this.apiCall(`/strategies/${encodeURIComponent(original)}/rename`, {
                            method: 'POST',
                            body: JSON.stringify({ new_name: newName, rename_file: false }),
                        });
                        this.showAlert('success', `策略已重命名为 ${newName}`);
                        newTitle.textContent = newName;
                        this.currentStrategyDetails = newName;
                        input.replaceWith(newTitle);
                        if (this.strategiesManager?.loadStrategies) {
                            this.strategiesManager.loadStrategies();
                        }
                    } catch (err) {
                        console.error('Rename strategy error:', err);
                        this.showAlert('danger', '重命名失败');
                        input.replaceWith(newTitle);
                        newTitle.textContent = original;
                    }
                };
                input.addEventListener('keydown', (event) => {
                    if (event.key === 'Enter') {
                        event.preventDefault();
                        commit();
                    } else if (event.key === 'Escape') {
                        event.preventDefault();
                        input.replaceWith(newTitle);
                        newTitle.textContent = original;
                    }
                });
                input.addEventListener('blur', commit);
            };
        }
        const symbolWrap = document.getElementById('detailsPrimarySymbolWrap');
        if (symbolWrap) symbolWrap.classList.add('d-none');
        const symbolEl = document.getElementById('detailsPrimarySymbol');
        if (symbolEl) symbolEl.textContent = '';

        const kpiIds = ['kpiPnL', 'kpiWinRate', 'kpiMaxDD', 'kpiSharpe', 'kpiTrades', 'kpiAvgDur'];
        kpiIds.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.textContent = '--';
        });

        const customSection = document.getElementById('customRangeSection');
        if (customSection) customSection.style.display = 'none';
        const startInput = document.getElementById('perfStart');
        if (startInput) startInput.value = '';
        const endInput = document.getElementById('perfEnd');
        if (endInput) endInput.value = '';

        const rangeSelect = document.getElementById('performanceRange');
        if (rangeSelect) {
            const newSelect = rangeSelect.cloneNode(true);
            rangeSelect.parentNode.replaceChild(newSelect, rangeSelect);
            newSelect.value = '1d';
            newSelect.addEventListener('change', async () => {
                if (customSection) customSection.style.display = newSelect.value === 'custom' ? '' : 'none';
                if (newSelect.value !== 'custom') {
                    await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
                }
            });
        }

        const applyBtn = document.getElementById('applyCustomRangeBtn');
        if (applyBtn) {
            const newBtn = applyBtn.cloneNode(true);
            applyBtn.parentNode.replaceChild(newBtn, applyBtn);
            newBtn.addEventListener('click', async () => {
                await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
            });
        }

        ['cumulativePnLChart', 'drawdownChart', 'pnlDistributionChart', 'winLossChart'].forEach((chartId) => {
            this.ensureChartDestroyed(chartId);
            const canvas = document.getElementById(chartId);
            if (canvas && canvas.getContext) {
                const ctx = canvas.getContext('2d');
                if (ctx) {
                    ctx.clearRect(0, 0, canvas.width || canvas.offsetWidth || 0, canvas.height || canvas.offsetHeight || 0);
                }
            }
        });

        const summaryWrap = document.getElementById('summary-tab-wrap');
        const riskWrap = document.getElementById('risk-settings-tab-wrap');
        const ordersWrap = document.getElementById('internal-orders-tab-wrap');
        const execWrap = document.getElementById('executions-tab-wrap');
        const apiWrap = document.getElementById('api-examples-tab-wrap');
        const summaryPane = document.getElementById('summary');
        const riskPane = document.getElementById('risk-settings');
        const ordersPane = document.getElementById('internal-orders');
        summaryWrap?.classList.add('d-none');
        riskWrap?.classList.add('d-none');
        ordersWrap?.classList.add('d-none');
        execWrap?.classList.remove('d-none');
        apiWrap?.classList.remove('d-none');
        summaryPane?.classList.add('d-none');
        riskPane?.classList.add('d-none');
        ordersPane?.classList.add('d-none');

        const chartsTabText = document.getElementById('charts-tab-text');
        if (chartsTabText) chartsTabText.textContent = 'Charts';

        const summaryLists = [
            ['summaryHighlights', '暂无摘要'],
            ['summaryRiskParams', '暂无数据'],
            ['summaryPositionSettings', '暂无限制'],
            ['summarySchedule', '暂无排程'],
        ];
        summaryLists.forEach(([id, placeholder]) => {
            const el = document.getElementById(id);
            if (el) el.innerHTML = `<li class=\"text-muted\">${placeholder}</li>`;
        });
        const summaryDataLog = document.getElementById('summaryDataLogList');
        if (summaryDataLog) summaryDataLog.innerHTML = '<li class="text-muted">暂无记录</li>';

        const summaryParamBody = document.querySelector('#summaryParameterTable tbody');
        if (summaryParamBody) {
            summaryParamBody.innerHTML = '<tr><td colspan="4" class="text-muted">打开 Summary 标签后加载</td></tr>';
        }
        const summaryRiskBody = document.querySelector('#summaryRiskTable tbody');
        if (summaryRiskBody) {
            summaryRiskBody.innerHTML = '';
        }

        const symbolInput = document.getElementById('summaryPrimarySymbolInput');
        if (symbolInput) {
            symbolInput.value = '';
            this.summaryPrimaryCurrent = null;
            this.updateSummaryPrimaryStatus('empty');
        }
        const dataSelect = document.getElementById('summaryDataFeedSelect');
        if (dataSelect) dataSelect.value = 'market_data';
        const pushSwitch = document.getElementById('summaryDataPushSwitch');
        if (pushSwitch) pushSwitch.checked = false;
        const pushStatusEl = document.getElementById('summaryDataPushStatus');
        if (pushStatusEl) pushStatusEl.textContent = '未推送';

        const summaryStatusIds = [
            'summaryStatusReceiving',
            'summaryStatusRuntime',
            'summaryStatusProcessed',
            'summaryStatusThresholdHits',
            'summaryStatusBuySignals',
            'summaryStatusSellSignals',
        ];
        summaryStatusIds.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.textContent = '-';
        });
        this.summaryStatusTarget = null;

        this.data_push_state = {};
        this.summaryPushAwaitingData = false;
        if (this.summaryPushAwaitingTimer) {
            clearTimeout(this.summaryPushAwaitingTimer);
            this.summaryPushAwaitingTimer = null;
        }
        this.summaryDataFeedCurrent = null;

        const riskForm = document.getElementById('riskSettingsForm');
        if (riskForm) riskForm.reset();
        this.currentRiskSettings = null;
        this.updateRiskSettingsStatus('default', '加载风控设置后显示。');

        const ordersBody = document.querySelector('#internalOrdersTable tbody');
        if (ordersBody) {
            ordersBody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">等待加载</td></tr>';
        }
        this.internalStrategyOrders = [];

        const execBody = document.querySelector('#executionsTable tbody');
        if (execBody) {
            execBody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">等待加载</td></tr>';
        }
        const execHead = document.querySelector('#executionsTable thead tr');
        if (execHead) {
            execHead.innerHTML = `
              <th>ID</th>
              <th>Symbol</th>
              <th>Created At</th>
              <th>Action</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Status</th>
              <th>PnL</th>
              <th>Commission</th>
            `;
        }
        const pagination = document.getElementById('executionsPagination');
        if (pagination) {
            pagination.innerHTML = '<li class="page-item disabled"><span class="page-link">-</span></li>';
        }
        this.executionsPagination.currentPage = 1;
        this.executionsPagination.totalItems = 0;
        this.executionsPagination.allExecutions = [];

        const pageSizeSelect = document.getElementById('executionsPageSize');
        if (pageSizeSelect) {
            const newSelect = pageSizeSelect.cloneNode(true);
            pageSizeSelect.parentNode.replaceChild(newSelect, pageSizeSelect);
            newSelect.value = String(this.executionsPagination.pageSize || this.strategyExecutionsDefaultLimit);
            newSelect.addEventListener('change', async () => {
                const newSize = parseInt(newSelect.value, 10) || this.strategyExecutionsDefaultLimit;
                this.executionsPagination.pageSize = newSize;
                this.executionsPagination.currentPage = 1;
                await this.triggerStrategyDetailsTabLoad('#executions', { force: true }).catch(() => {});
                if (this.strategyDetailsState?.strategyType === 'internal') {
                    await this.triggerStrategyDetailsTabLoad('#internal-orders', { force: true }).catch(() => {});
                }
                await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
            });
        }

        const refreshBtn = document.getElementById('refreshExecutionsBtn');
        if (refreshBtn) {
            refreshBtn.disabled = true;
            refreshBtn.title = 'WebSocket updates executions automatically';
        }

        const clearBtn = document.getElementById('clearExecutionsBtn');
        if (clearBtn) {
            const newClear = clearBtn.cloneNode(true);
            clearBtn.parentNode.replaceChild(newClear, clearBtn);
            newClear.addEventListener('click', async () => {
                try {
                    const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                    if (!currentName) return;
                    if (!confirm(`确定要清空策略 "${currentName}" 的全部执行记录吗？此操作不可撤销！`)) {
                        return;
                    }
                    await this.apiCall(`/strategies/${encodeURIComponent(currentName)}/executions`, { method: 'DELETE' });
                    this.clearStrategyExecutionsCache(currentName);
                    this.executionsPagination.allExecutions = [];
                    this.executionsPagination.totalItems = 0;
                    this.executionsPagination.currentPage = 1;
                    await this.triggerStrategyDetailsTabLoad('#executions', { force: true }).catch(() => {});
                    await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
                    if (this.strategyDetailsState?.strategyType === 'internal') {
                        await this.triggerStrategyDetailsTabLoad('#internal-orders', { force: true }).catch(() => {});
                    }
                    this.showAlert('success', '已清空该策略的执行记录');
                } catch (error) {
                    console.error('Clear executions error:', error);
                    this.showAlert('danger', '清空执行记录失败');
                }
            });
        }

        const exportBtn = document.getElementById('exportExecutionsBtn');
        if (exportBtn) {
            const newExport = exportBtn.cloneNode(true);
            exportBtn.parentNode.replaceChild(newExport, exportBtn);
            newExport.addEventListener('click', () => {
                const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                if (currentName) {
                    this.exportExecutionsCsv(currentName);
                }
            });
        }

        const resetBtn = document.getElementById('resetStrategyDataBtn');
        if (resetBtn) {
            const newReset = resetBtn.cloneNode(true);
            resetBtn.parentNode.replaceChild(newReset, resetBtn);
            newReset.addEventListener('click', async () => {
                const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                if (!currentName) return;
                if (!confirm(`确定要重置策略 "${currentName}" 的全部订单与统计数据吗？此操作不可撤销！`)) {
                    return;
                }
                const originalHtml = newReset.innerHTML;
                newReset.disabled = true;
                newReset.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Resetting…';
                try {
                    const res = await this.apiCall(`/strategies/${encodeURIComponent(currentName)}/reset`, { method: 'POST' });
                    if (!res?.success) {
                        throw new Error(res?.error || res?.detail || '重置策略数据失败');
                    }
                    this.clearStrategyExecutionsCache(currentName);
                    this.strategyDetailsState.tabLoaded = {};
                    await Promise.allSettled([
                        this.triggerStrategyDetailsTabLoad('#executions', { force: true }),
                        this.triggerStrategyDetailsTabLoad('#charts', { force: true }),
                        this.triggerStrategyDetailsTabLoad('#pnl-calendar', { force: true }),
                        this.triggerStrategyDetailsTabLoad('#summary', { force: true }),
                        this.triggerStrategyDetailsTabLoad('#internal-orders', { force: true }),
                    ]);
                    try {
                        const detailRes = await this.apiCall(`/strategies/${encodeURIComponent(currentName)}`);
                        if (detailRes?.success) {
                            this.updateStrategyDetailsHeader(detailRes.strategy || {});
                        }
                    } catch (refreshErr) {
                        console.warn('Failed to refresh strategy info after reset', refreshErr);
                        this.updateStrategyDetailsHeader(null);
                    }
                    if (this.strategiesManager?.loadStrategies) {
                        await this.strategiesManager.loadStrategies({ force: true }).catch((err) => {
                            console.warn('Failed to refresh strategies list after reset', err);
                        });
                    }
                    const successMessage = res?.message || '策略订单与统计数据已重置';
                    this.showAlert('success', successMessage);
                } catch (error) {
                    console.error('Reset strategy data error:', error);
                    this.showAlert('danger', error?.message || '重置策略数据失败');
                } finally {
                    newReset.disabled = false;
                    newReset.innerHTML = originalHtml;
                }
            });
        }

        const deleteBtn = document.getElementById('deleteStrategyFromDetails');
        if (deleteBtn) {
            const newDelete = deleteBtn.cloneNode(true);
            deleteBtn.parentNode.replaceChild(newDelete, deleteBtn);
            newDelete.addEventListener('click', async () => {
                const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                if (!currentName) return;
                await this.deleteStrategy(currentName);
                const modalEl = document.getElementById('strategyDetailsModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();
            });
        }

        const calendarContainer = document.getElementById('pnlCalendarContainer');
        if (calendarContainer) {
            calendarContainer.innerHTML = '<div class="text-muted text-center py-4">打开 PnL Calendar 标签以加载数据</div>';
        }

        const apiContainer = document.getElementById('apiExampleContent');
        if (apiContainer) {
            apiContainer.innerHTML = '<p class="text-muted">打开 API Examples 标签查看示例。</p>';
        }

        this.setupStrategyChartTab();
        this.resetStrategyChartState({ keepControls: false, resetSymbol: true });
    }

    ensureStrategyDetailsTabListener() {
        if (this.strategyDetailsTabListenerBound) return;
        const tabs = document.getElementById('strategyDetailsTabs');
        if (!tabs) return;
        tabs.addEventListener('shown.bs.tab', (event) => {
            const target = event.target?.getAttribute('data-bs-target') || event.target?.dataset?.bsTarget;
            if (target) {
                this.triggerStrategyDetailsTabLoad(target).catch(() => {});
            }
        });
        this.strategyDetailsTabListenerBound = true;
    }

    showStrategyDetailsModal() {
        const modalEl = document.getElementById('strategyDetailsModal');
        if (!modalEl) return;
        const modal = bootstrap.Modal?.getOrCreateInstance
            ? bootstrap.Modal.getOrCreateInstance(modalEl)
            : new bootstrap.Modal(modalEl);
        try {
            modal.show();
        } catch (err) {
            console.warn('Failed to show strategy details modal:', err);
        }
    }

    async initializeStrategyDetailsContent(name) {
        const cached = this.getStrategyFromCache(name);
        if (cached) {
            this.strategyDetailsState.basicInfo = cached;
            this.updateStrategyDetailsHeader(cached);
            this.configureStrategyDetailsTabs(cached);
            this.setupStrategyChartState(cached, name);
        }

        const strategy = await this.fetchStrategyBasicInfo(name);
        if (this.strategyDetailsState.name !== name) {
            return;
        }
        const resolved = strategy || cached || null;
        this.strategyDetailsState.basicInfo = resolved;
        this.updateStrategyDetailsHeader(resolved);
        this.configureStrategyDetailsTabs(resolved);
        this.setupStrategyChartState(resolved, name);
    }

    updateStrategyDetailsHeader(strategy) {
        const typeEl = document.getElementById('detailsStrategyType');
        const trigEl = document.getElementById('detailsTriggerCount');
        const lastEl = document.getElementById('detailsLastTriggered');
        if (typeEl) typeEl.textContent = strategy?.strategy_type || '-';
        if (trigEl) trigEl.textContent = strategy?.trigger_count ?? 0;
        if (lastEl) {
            lastEl.textContent = strategy?.last_triggered
                ? new Date(strategy.last_triggered).toLocaleString()
                : '-';
        }
        const symbolWrap = document.getElementById('detailsPrimarySymbolWrap');
        const symbolEl = document.getElementById('detailsPrimarySymbol');
        if (symbolWrap && symbolEl) {
            const symbol = this.resolveStrategySymbol(strategy);
            if (symbol) {
                symbolEl.textContent = symbol;
                symbolWrap.classList.remove('d-none');
            } else {
                symbolEl.textContent = '';
                symbolWrap.classList.add('d-none');
            }
        }
    }

    configureStrategyDetailsTabs(strategy) {
        const isInternal = (strategy?.strategy_type || '').toLowerCase() === 'internal';
        const summaryWrap = document.getElementById('summary-tab-wrap');
        const riskWrap = document.getElementById('risk-settings-tab-wrap');
        const ordersWrap = document.getElementById('internal-orders-tab-wrap');
        const execWrap = document.getElementById('executions-tab-wrap');
        const apiWrap = document.getElementById('api-examples-tab-wrap');
        const summaryPane = document.getElementById('summary');
        const riskPane = document.getElementById('risk-settings');
        const ordersPane = document.getElementById('internal-orders');
        const chartsTabText = document.getElementById('charts-tab-text');

        if (isInternal) {
            summaryWrap?.classList.remove('d-none');
            riskWrap?.classList.remove('d-none');
            ordersWrap?.classList.remove('d-none');
            execWrap?.classList.add('d-none');
            apiWrap?.classList.add('d-none');
            summaryPane?.classList.remove('d-none');
            riskPane?.classList.remove('d-none');
            ordersPane?.classList.remove('d-none');
            if (chartsTabText) chartsTabText.textContent = 'Visual Metrics';
            this.updateRiskSettingsStatus('loading');
            this.strategyDetailsState.defaultTabTarget = '#summary';
        } else {
            summaryWrap?.classList.add('d-none');
            riskWrap?.classList.add('d-none');
            ordersWrap?.classList.add('d-none');
            execWrap?.classList.remove('d-none');
            apiWrap?.classList.remove('d-none');
            summaryPane?.classList.add('d-none');
            riskPane?.classList.add('d-none');
            ordersPane?.classList.add('d-none');
            if (chartsTabText) chartsTabText.textContent = 'Charts';
            this.currentRiskSettings = null;
            this.updateRiskSettingsStatus('default', '风控设置将在内部策略中显示。');
            this.strategyDetailsState.defaultTabTarget = '#charts';
        }

        this.strategyDetailsState.strategyType = isInternal ? 'internal' : (strategy?.strategy_type || 'external');
    }

    setupStrategyChartState(strategy, name) {
        const initialSymbol = this.resolveStrategySymbol(strategy) || '';
        this.strategyDetailsState.initialSymbol = initialSymbol;
        this.resetStrategyChartState({
            keepControls: true,
            currentStrategy: name,
            symbol: initialSymbol || '',
            resetSymbol: !initialSymbol,
        });
        const state = this.getStrategyChartState();
        if (initialSymbol) {
            state.symbol = initialSymbol;
        }
    }

    activateStrategyDetailsDefaultTab() {
        const state = this.strategyDetailsState;
        const target = state?.defaultTabTarget || '#charts';
        const button = document.querySelector(`#strategyDetailsTabs button[data-bs-target="${target}"]`);
        if (!button) return;
        try {
            const tabInstance = bootstrap.Tab?.getOrCreateInstance
                ? bootstrap.Tab.getOrCreateInstance(button)
                : new bootstrap.Tab(button);
            tabInstance.show();
        } catch (err) {
            console.warn('Tab show failed', err);
        }
    }

    async requestActiveStrategyDetailsTabLoad(options = {}) {
        const active = document.querySelector('#strategyDetailsTabs .nav-link.active');
        if (!active) return;
        const target = active.getAttribute('data-bs-target') || active.dataset?.bsTarget;
        if (target) {
            await this.triggerStrategyDetailsTabLoad(target, options);
        }
    }

    async triggerStrategyDetailsTabLoad(targetSelector, options = {}) {
        const state = this.strategyDetailsState;
        if (!state || !state.name) return;
        const key = (targetSelector || '').replace(/^#/, '');
        if (!key) return;
        if (!options.force && state.tabLoaded[key]) return;
        const loaders = {
            summary: (opts) => this.loadStrategySummaryTab(opts),
            'risk-settings': (opts) => this.loadStrategyRiskTab(opts),
            'internal-orders': (opts) => this.loadStrategyInternalOrdersTab(opts),
            charts: (opts) => this.loadStrategyChartsTab(opts),
            'strategy-chart': (opts) => this.loadStrategyStrategyChartTab(opts),
            executions: (opts) => this.loadStrategyExecutionsTab(opts),
            'pnl-calendar': (opts) => this.loadStrategyPnlCalendarTab(opts),
            'api-examples': () => this.loadStrategyApiExamplesTab(),
        };
        const loader = loaders[key];
        if (!loader) return;
        try {
            await loader(options);
            state.tabLoaded[key] = true;
        } catch (err) {
            console.error(`Failed to load strategy details tab ${targetSelector}:`, err);
            state.tabLoaded[key] = false;
        }
    }

    async fetchStrategyBasicInfo(name) {
        const state = this.strategyDetailsState;
        if (!state || state.name !== name) return null;
        if (!state.fetches.basicInfo) {
            state.fetches.basicInfo = (async () => {
                try {
                    const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}`);
                    if (res?.success) {
                        return res.strategy || null;
                    }
                } catch (err) {
                    console.error('Failed to load strategy info:', err);
                }
                return null;
            })();
        }
        try {
            return await state.fetches.basicInfo;
        } catch (err) {
            state.fetches.basicInfo = null;
            throw err;
        }
    }

    async fetchStrategyInternalInfo(name) {
        const state = this.strategyDetailsState;
        if (!state || state.name !== name) return null;
        if (!state.fetches.internalInfo) {
            state.internalInfoCached = false;
            state.fetches.internalInfo = (async () => {
                try {
                    const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/internal-info`);
                    if (res?.success) {
                        if (!this.strategyDetailsState || this.strategyDetailsState.name !== name) {
                            return null;
                        }
                        const info = res.strategy || {};
                        state.internalInfoCached = Boolean(res.cached || info?.stale);
                        return info;
                    }
                } catch (err) {
                    console.error('Failed to load internal strategy info:', err);
                }
                state.internalInfoCached = false;
                return null;
            })();
        }
        try {
            return await state.fetches.internalInfo;
        } catch (err) {
            state.fetches.internalInfo = null;
            state.internalInfoCached = false;
            throw err;
        }
    }

    async loadStrategySummaryTab() {
        const state = this.strategyDetailsState;
        if (!state || state.strategyType !== 'internal') return;
        const name = state.name;
        if (!name) return;

        this.ensureSummaryControls();

        const infoStatus = document.getElementById('summaryInfoStatus');
        if (infoStatus) {
            infoStatus.textContent = '';
            infoStatus.classList.add('d-none');
            infoStatus.classList.remove('alert-danger');
            infoStatus.classList.add('alert-warning');
        }

        const baseInfo = state.basicInfo || {};
        const fallbackSymbol = baseInfo.primary_symbol || state.initialSymbol || '';
        const symbolInput = document.getElementById('summaryPrimarySymbolInput');
        if (symbolInput) {
            symbolInput.value = fallbackSymbol || '';
            this.summaryPrimaryCurrent = fallbackSymbol ? fallbackSymbol.toUpperCase() : null;
            this.updateSummaryPrimaryStatus(fallbackSymbol ? 'saved' : 'empty');
        }
        const fallbackSub = baseInfo.data_subscription || '';
        const dataSelect = document.getElementById('summaryDataFeedSelect');
        this.summaryDataFeedCurrent = fallbackSub || null;
        if (dataSelect) dataSelect.value = fallbackSub || 'market_data';

        const pushSwitch = document.getElementById('summaryDataPushSwitch');
        if (pushSwitch) pushSwitch.checked = false;
        const pushStatusEl = document.getElementById('summaryDataPushStatus');
        if (pushStatusEl) pushStatusEl.textContent = '未推送';
        this.data_push_state = {};
        this.summaryPushAwaitingData = false;
        if (this.summaryPushAwaitingTimer) {
            clearTimeout(this.summaryPushAwaitingTimer);
            this.summaryPushAwaitingTimer = null;
        }

        try {
            const info = await this.fetchStrategyInternalInfo(name);
            if (info) {
                state.internalInfo = info;
                if (infoStatus) {
                    const usedCached = Boolean(state.internalInfoCached || info?.stale);
                    if (usedCached) {
                        infoStatus.textContent = '显示缓存的策略信息，可能不是最新。';
                        infoStatus.classList.remove('d-none');
                        infoStatus.classList.remove('alert-danger');
                        infoStatus.classList.add('alert-warning');
                    } else {
                        infoStatus.textContent = '';
                        infoStatus.classList.add('d-none');
                        infoStatus.classList.remove('alert-danger');
                        infoStatus.classList.add('alert-warning');
                    }
                }
                const descEl = document.getElementById('summaryDescription');
                if (descEl) descEl.textContent = info.description || '';
                const pathEl = document.getElementById('summaryPath');
                if (pathEl) pathEl.textContent = info.file_path || '';

                const resolvedSymbol = info.primary_symbol || fallbackSymbol || '';
                if (symbolInput) {
                    symbolInput.value = resolvedSymbol || '';
                    this.summaryPrimaryCurrent = resolvedSymbol ? resolvedSymbol.toUpperCase() : null;
                    this.updateSummaryPrimaryStatus(resolvedSymbol ? 'saved' : 'empty');
                }
                if (resolvedSymbol) {
                    this.updateStrategyChartSymbol(resolvedSymbol, { ensureLoaded: false });
                }

                const highlightsEl = document.getElementById('summaryHighlights');
                if (highlightsEl) {
                    const points = Array.isArray(info.summary_points) ? info.summary_points : [];
                    if (points.length) {
                        highlightsEl.innerHTML = '';
                        points.forEach((text) => {
                            const li = document.createElement('li');
                            li.textContent = text;
                            highlightsEl.appendChild(li);
                        });
                    } else {
                        highlightsEl.innerHTML = '<li class="text-muted">No additional highlights</li>';
                    }
                }

                const riskParamEl = document.getElementById('summaryRiskParams');
                if (riskParamEl) {
                    const params = info.risk_parameters || {};
                    const mappings = {
                        trend_threshold: 'Trend Threshold',
                        cooldown_seconds: 'Cooldown',
                        max_loss_streak: 'Breaker (Max Loss Streak)',
                        default_quantity: 'Base Quantity',
                        signal_frequency_seconds: 'Execution Frequency',
                    };
                    const entries = Object.entries(params).filter(([, value]) => value !== null && value !== undefined);
                    if (entries.length) {
                        riskParamEl.innerHTML = '';
                        entries.forEach(([key, value]) => {
                            const label = mappings[key] || key;
                            const displayValue = typeof value === 'number' && !Number.isNaN(value)
                                ? (key === 'trend_threshold' ? value.toFixed(2) : value)
                                : value;
                            const li = document.createElement('li');
                            li.innerHTML = `<span class="text-muted">${label}:</span> <span class="fw-semibold">${displayValue}</span>`;
                            riskParamEl.appendChild(li);
                        });
                    } else {
                        riskParamEl.innerHTML = '<li class="text-muted">No dedicated parameters</li>';
                    }
                }

                state.preloadedRiskSettings = info.strategy_risk_settings || null;

                const paramTableBody = document.querySelector('#summaryParameterTable tbody');
                if (paramTableBody) {
                    paramTableBody.innerHTML = '';
                    const schemaSource = Array.isArray(info.configurable_parameters) && info.configurable_parameters.length
                        ? info.configurable_parameters
                        : (Array.isArray(info.parameters) ? info.parameters : []);
                    const schema = Array.isArray(schemaSource) ? schemaSource : [];
                    const formatValue = (param, key) => {
                        const raw = param?.[key];
                        if (raw === null || raw === undefined) return '-';
                        if (param?.type === 'bool') {
                            return raw ? 'On' : 'Off';
                        }
                        if (typeof raw === 'number') {
                            return Number.isFinite(raw) ? raw : '-';
                        }
                        return raw;
                    };
                    const buildRangeText = (param) => {
                        const parts = [];
                        if (param?.min !== undefined && param.min !== null) parts.push(`≥ ${param.min}`);
                        if (param?.max !== undefined && param.max !== null) parts.push(`≤ ${param.max}`);
                        let range = parts.join(' / ');
                        if (param?.allow_null) {
                            range = range ? `${range}; allow null` : 'Allow null';
                        }
                        return range || '-';
                    };
                    const startInlineEdit = (cell, param) => {
                        if (!param?.name) return;
                        if (cell.dataset.editing === '1') return;
                        cell.dataset.editing = '1';
                        const originalValue = param.value;
                        const originalDisplay = formatValue(param, 'value');
                        let isSaving = false;
                        const cleanup = () => {
                            cell.dataset.editing = '0';
                            cell.classList.remove('opacity-75');
                        };
                        const revert = () => {
                            param.value = originalValue;
                            cell.innerHTML = '';
                            cell.textContent = originalDisplay;
                            cleanup();
                        };

                        let editor;
                        const choices = Array.isArray(param?.choices) ? param.choices : null;
                        if (param?.type === 'bool' || (choices && choices.length)) {
                            const select = document.createElement('select');
                            select.className = 'form-select form-select-sm';
                            if (choices && choices.length) {
                                choices.forEach((choice) => {
                                    const opt = document.createElement('option');
                                    const hasValue = choice && typeof choice === 'object' && 'value' in choice;
                                    const rawValue = hasValue ? choice.value : choice;
                                    const label = choice && typeof choice === 'object' && 'label' in choice
                                        ? (choice.label ?? rawValue)
                                        : rawValue;
                                    const normalizedValue = rawValue === null || rawValue === undefined ? '' : String(rawValue);
                                    opt.value = normalizedValue;
                                    opt.textContent = label === null || label === undefined ? normalizedValue : String(label);
                                    select.appendChild(opt);
                                });
                                if (param.allow_null) {
                                    const opt = document.createElement('option');
                                    opt.value = '';
                                    opt.textContent = '（留空）';
                                    select.appendChild(opt);
                                }
                                const current = param.value;
                                if (current === null || current === undefined) {
                                    select.value = '';
                                } else {
                                    const currentStr = typeof current === 'object' ? current?.value : String(current);
                                    if (Array.from(select.options).some((opt) => opt.value == currentStr)) {
                                        select.value = currentStr;
                                    }
                                }
                            } else {
                                const trueOpt = document.createElement('option');
                                trueOpt.value = 'true';
                                trueOpt.textContent = 'On';
                                const falseOpt = document.createElement('option');
                                falseOpt.value = 'false';
                                falseOpt.textContent = 'Off';
                                select.appendChild(trueOpt);
                                select.appendChild(falseOpt);
                                if (param.allow_null) {
                                    const nullOpt = document.createElement('option');
                                    nullOpt.value = 'null';
                                    nullOpt.textContent = '（留空）';
                                    select.appendChild(nullOpt);
                                }
                                const current = param.value;
                                if (current === null || current === undefined) {
                                    select.value = 'null';
                                } else {
                                    select.value = current ? 'true' : 'false';
                                }
                            }
                            editor = select;
                        } else {
                            const input = document.createElement('input');
                            input.className = 'form-control form-control-sm';
                            input.type = (param?.type === 'int' || param?.type === 'float') ? 'number' : 'text';
                            if (param?.step !== undefined && param.step !== null) input.step = param.step;
                            if (param?.min !== undefined && param.min !== null) input.min = param.min;
                            if (param?.max !== undefined && param.max !== null) input.max = param.max;
                            if (param?.allow_null) {
                                input.placeholder = '（留空）';
                            }
                            if (param.value !== undefined && param.value !== null) {
                                input.value = String(param.value);
                            } else {
                                input.value = '';
                            }
                            editor = input;
                        }

                        cell.innerHTML = '';
                        cell.appendChild(editor);
                        editor.focus();
                        cell.classList.add('opacity-75');

                        const commit = async () => {
                            if (isSaving) return;
                            isSaving = true;
                            let newValue = editor.value;
                            if (editor instanceof HTMLSelectElement) {
                                if (param?.type === 'bool' && editor.options.length <= 3) {
                                    if (newValue === 'true') {
                                        param.value = true;
                                    } else if (newValue === 'false') {
                                        param.value = false;
                                    } else {
                                        param.value = null;
                                    }
                                } else if (param.allow_null && newValue === '') {
                                    param.value = null;
                                } else if (param?.type === 'int') {
                                    const parsed = Number.parseInt(newValue, 10);
                                    param.value = Number.isNaN(parsed) ? param.value : parsed;
                                } else if (param?.type === 'float') {
                                    const parsed = Number.parseFloat(newValue);
                                    param.value = Number.isNaN(parsed) ? param.value : parsed;
                                } else {
                                    param.value = newValue;
                                }
                            } else {
                                if (newValue === '') {
                                    param.value = param.allow_null ? null : param.value;
                                } else if (param?.type === 'int') {
                                    const parsed = Number.parseInt(newValue, 10);
                                    if (Number.isNaN(parsed)) {
                                        revert();
                                        return;
                                    }
                                    param.value = parsed;
                                } else if (param?.type === 'float') {
                                    const parsed = Number.parseFloat(newValue);
                                    if (Number.isNaN(parsed)) {
                                        revert();
                                        return;
                                    }
                                    param.value = parsed;
                                } else {
                                    param.value = newValue;
                                }
                            }

                            try {
                                await this.apiCall(`/strategies/${encodeURIComponent(name)}/internal-info/parameter`, {
                                    method: 'POST',
                                    body: JSON.stringify({
                                        name: param.name,
                                        value: param.value,
                                    }),
                                });
                                cell.textContent = formatValue(param, 'value');
                                cleanup();
                            } catch (error) {
                                console.error('Update parameter error:', error);
                                this.showAlert('danger', '更新参数失败');
                                revert();
                            } finally {
                                isSaving = false;
                            }
                        };

                        const handleBlur = () => {
                            editor.removeEventListener('blur', handleBlur);
                            commit();
                        };
                        editor.addEventListener('blur', handleBlur);

                        if (editor instanceof HTMLSelectElement) {
                            editor.addEventListener('change', () => commit());
                        }

                        editor.addEventListener('keydown', (event) => {
                            if (event.key === 'Enter') {
                                event.preventDefault();
                                commit();
                            } else if (event.key === 'Escape') {
                                event.preventDefault();
                                editor.removeEventListener('blur', handleBlur);
                                revert();
                            }
                        });
                    };

                    if (schema.length) {
                        schema.forEach((param) => {
                            const tr = document.createElement('tr');
                            if (param?.description) tr.title = param.description;
                            const nameCell = document.createElement('td');
                            nameCell.textContent = param?.label || param?.name || '-';
                            const valueCell = document.createElement('td');
                            valueCell.textContent = formatValue(param, 'value');
                            if (param?.name) {
                                valueCell.title = '双击编辑，回车保存';
                                valueCell.style.cursor = 'text';
                                valueCell.classList.add('align-middle');
                                valueCell.addEventListener('dblclick', () => startInlineEdit(valueCell, param));
                            }
                            const defaultCell = document.createElement('td');
                            defaultCell.textContent = formatValue(param, 'default');
                            const rangeCell = document.createElement('td');
                            rangeCell.textContent = buildRangeText(param);
                            tr.appendChild(nameCell);
                            tr.appendChild(valueCell);
                            tr.appendChild(defaultCell);
                            tr.appendChild(rangeCell);
                            paramTableBody.appendChild(tr);
                        });
                    } else {
                        const row = document.createElement('tr');
                        const cell = document.createElement('td');
                        cell.colSpan = 4;
                        cell.className = 'text-muted';
                        cell.textContent = 'No configurable parameters';
                        row.appendChild(cell);
                        paramTableBody.appendChild(row);
                    }
                }

                const subKey = info.data_subscription || fallbackSub || '';
                this.summaryDataFeedCurrent = subKey || null;
                if (dataSelect) dataSelect.value = subKey || 'market_data';

                const positionList = document.getElementById('summaryPositionSettings');
                if (positionList) {
                    const timing = info.position_settings || {};
                    positionList.innerHTML = '';
                    const entries = [];
                    if (timing.position_add_cooldown_minutes) {
                        entries.push(['Add Cooldown', `${timing.position_add_cooldown_minutes} min`]);
                    }
                    if (timing.close_before_close_minutes) {
                        entries.push(['Exit Before Close', `${timing.close_before_close_minutes} min`]);
                    }
                    if (entries.length) {
                        entries.forEach(([label, value]) => {
                            const li = document.createElement('li');
                            li.innerHTML = `<span class="text-muted">${label}:</span> <span class="fw-semibold">${value}</span>`;
                            positionList.appendChild(li);
                        });
                    } else {
                        positionList.innerHTML = '<li class="text-muted">No timing limits</li>';
                    }
                }

                const schedList = document.getElementById('summarySchedule');
                if (schedList) {
                    schedList.innerHTML = '';
                    (info.schedule || []).forEach((item) => {
                        const li = document.createElement('li');
                        li.textContent = `${item.time || ''} ${item.action || ''} ${item.qty || ''} ${item.symbol || ''}`.trim();
                        schedList.appendChild(li);
                    });
                }

                const riskBody = document.querySelector('#summaryRiskTable tbody');
                if (riskBody) {
                    riskBody.innerHTML = '';
                    const rr = info.risk_rules || {};
                    Object.keys(rr).forEach((sym) => {
                        const rule = rr[sym] || {};
                        const tr = document.createElement('tr');
                        tr.innerHTML = `<td>${sym}</td><td>${rule.stop_loss_price ?? ''}</td><td>${rule.take_profit_price ?? ''}</td>`;
                        riskBody.appendChild(tr);
                    });
                }
            } else if (infoStatus) {
                infoStatus.textContent = '暂时无法加载策略内部信息。';
                infoStatus.classList.remove('d-none');
                infoStatus.classList.remove('alert-warning');
                infoStatus.classList.add('alert-danger');
            }
        } catch (err) {
            console.error('Failed to load internal strategy info:', err);
        }

        await this.loadStrategyStatus(name, { force: true });
    }

    async loadStrategyRiskTab() {
        if (this.strategyDetailsState.strategyType !== 'internal') {
            return;
        }
        this.ensureRiskSettingsControls();
        const name = this.strategyDetailsState.name;
        if (!name) return;
        if (this.strategyDetailsState.preloadedRiskSettings) {
            this.currentRiskSettings = this.strategyDetailsState.preloadedRiskSettings;
            this.renderRiskSettingsForm(this.currentRiskSettings);
            this.updateRiskSettingsStatus('loaded');
            return;
        }
        await this.loadStrategyRiskSettings(name);
    }

    async loadStrategyInternalOrdersTab() {
        if (this.strategyDetailsState.strategyType !== 'internal') return;
        const name = this.strategyDetailsState.name;
        if (!name) return;
        await this.loadInternalStrategyOrders(name, { limit: this.executionsPagination.pageSize || this.strategyExecutionsDefaultLimit, force: true });
    }

    async loadStrategyChartsTab() {
        const name = this.strategyDetailsState.name;
        if (!name) return;
        const rangeSelect = document.getElementById('performanceRange');
        let range = rangeSelect ? rangeSelect.value : '1d';
        let start = null;
        let end = null;
        if (range === 'custom') {
            start = document.getElementById('perfStart')?.value || null;
            end = document.getElementById('perfEnd')?.value || null;
            if (!start || !end) {
                range = '1d';
                start = null;
                end = null;
            }
        }
        await this.loadStrategyPerformance(name, { range, start, end });
    }

    async loadStrategyStrategyChartTab() {
        const state = this.getStrategyChartState();
        if (!state.currentStrategy) return;
        if (!state.symbol) {
            this.updateStrategyChartStatus('缺少标的');
            return;
        }
        await this.loadStrategyChart(state.currentStrategy, {
            symbol: state.symbol,
            forceExecutions: true,
        });
    }

    async loadStrategyExecutionsTab() {
        const name = this.strategyDetailsState.name;
        if (!name) return;
        const limit = this.executionsPagination.pageSize || this.strategyExecutionsDefaultLimit;
        await this.loadStrategyExecutions(name, { limit, force: true });
    }

    async loadStrategyPnlCalendarTab() {
        const name = this.strategyDetailsState.name;
        if (!name) return;
        await this.loadPnLCalendar(name);
    }

    loadStrategyApiExamplesTab() {
        const container = document.getElementById('apiExampleContent');
        if (!container) return;
        const strategy = this.strategyDetailsState.basicInfo;
        if (strategy?.strategy_type === 'external') {
            const endpoint = strategy.api_endpoint || '/api/strategy';
            const samplePayload = { symbol: 'AAPL', action: 'BUY', quantity: 10, price: 190.12, meta: { note: 'optional' } };
            const payloadStr = JSON.stringify(samplePayload).replace(/\"/g, '\\"');
            const curl = `curl -X POST \"${endpoint}\" \
  -H \"Content-Type: application/json\" \
  -d \"${payloadStr}\"`;
            container.innerHTML = `
              <div class="mb-2"><span class="badge bg-primary">external</span> endpoint: <code>${endpoint}</code></div>
              <pre class="bg-light p-3"><code>${curl}</code></pre>
              <p class="text-muted">如果在策略设置中开启鉴权，需要在请求体中加入 <code>{ token: &lt;TOKEN&gt; }</code> 字段，其中 TOKEN 来自策略设置</p>`;
        } else {
            container.innerHTML = '<p class="text-muted">API examples are only available for external strategies.</p>';
        }
    }


    getStrategyExecutionsCacheKey(name, limit) {
        return `${name}::${limit}`;
    }

    clearStrategyExecutionsCache(name) {
        if (!this.strategyExecutionsCache) return;
        if (!name) {
            this.strategyExecutionsCache.clear();
            return;
        }
        const prefix = `${name}::`;
        for (const key of Array.from(this.strategyExecutionsCache.keys())) {
            if (key.startsWith(prefix)) {
                this.strategyExecutionsCache.delete(key);
            }
        }
    }

    async fetchStrategyExecutions(name, { limit = this.strategyExecutionsDefaultLimit, force = false } = {}) {
        if (!name) return [];
        const effectiveLimit = Math.max(1, Number(limit) || this.strategyExecutionsDefaultLimit);
        const cacheKey = this.getStrategyExecutionsCacheKey(name, effectiveLimit);
        const cached = !force && this.strategyExecutionsCache.get(cacheKey);
        if (cached) {
            if (cached.data) return cached.data;
            if (cached.promise) return await cached.promise;
        }

        const fetchPromise = (async () => {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/executions?limit=${effectiveLimit}`);
            if (!res || !res.success) {
                throw new Error('Failed to load strategy executions');
            }
            return Array.isArray(res.executions) ? res.executions.slice() : [];
        })();

        this.strategyExecutionsCache.set(cacheKey, { promise: fetchPromise });

        try {
            const data = await fetchPromise;
            this.strategyExecutionsCache.set(cacheKey, { data, timestamp: Date.now() });
            return data;
        } catch (error) {
            this.strategyExecutionsCache.delete(cacheKey);
            throw error;
        }
    }

    async loadStrategyPerformance(name, { range = '1d', start = null, end = null, executions = null } = {}) {
        try {
            const params = new URLSearchParams();
            if (range) params.set('range', range);
            if (start) params.set('start', start);
            if (end) params.set('end', end);
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/performance?${params.toString()}`);
            if (!res.success) return;
            const m = res.metrics || {};
            const el = (id) => document.getElementById(id);
            const toFiniteNumber = (value, fallback = 0) => {
                if (typeof value === 'number' && Number.isFinite(value)) {
                    return value;
                }
                const parsed = Number(value);
                return Number.isFinite(parsed) ? parsed : fallback;
            };
            const totalPnl = toFiniteNumber(m.total_pnl);
            const winRate = toFiniteNumber(m.win_rate);
            const maxDrawdown = toFiniteNumber(m.max_drawdown);
            const sharpe = toFiniteNumber(m.sharpe_ratio);
            el('kpiPnL').textContent = `$${totalPnl.toFixed(2)}`;
            el('kpiWinRate').textContent = `${winRate.toFixed(2)}%`;
            el('kpiMaxDD').textContent = `$${maxDrawdown.toFixed(2)}`;
            el('kpiSharpe').textContent = `${sharpe.toFixed(2)}`;
            const totalExecutions = Number(m.total_executions ?? 0);
            const totalCommission = Number(m.total_commission ?? 0);
            const tradesDisplay = Number.isFinite(totalExecutions) ? totalExecutions : 0;
            const commissionDisplay = Number.isFinite(totalCommission)
                ? ` ($${totalCommission.toFixed(2)})`
                : '';
            el('kpiTrades').textContent = `${tradesDisplay}${commissionDisplay}`;
            const avgDurationRaw = m.avg_trade_duration;
            el('kpiAvgDur').textContent =
                typeof avgDurationRaw === 'string'
                    ? avgDurationRaw
                    : this.formatDuration(avgDurationRaw ?? 0);

            // Charts from executions over selected range
            const executionLimit = Math.max(this.strategyExecutionsDefaultLimit, this.executionsPagination.pageSize || 0);
            const rawExecutions = Array.isArray(executions)
                ? executions
                : await this.fetchStrategyExecutions(name, { limit: executionLimit }).catch(() => []);
            const sortedExecutions = Array.isArray(rawExecutions)
                ? rawExecutions.slice().sort((a, b) => new Date(a.execution_time) - new Date(b.execution_time))
                : [];
            const labels = [];
            const cumData = [];
            let cum = 0;
            sortedExecutions.forEach(ex => {
                const pnl = Number(ex.pnl || 0);
                cum += pnl;
                labels.push(ex.execution_time ? new Date(ex.execution_time).toLocaleString() : '');
                cumData.push(cum);
            });
            this.renderLineChart('cumulativePnLChart', 'Cumulative PnL', labels, cumData, 'rgba(54, 162, 235, 1)');
            const ddData = [];
            let peak = -Infinity;
            cum = 0;
            sortedExecutions.forEach(ex => {
                cum += Number(ex.pnl || 0);
                peak = Math.max(peak, cum);
                ddData.push(cum - peak);
            });
            this.renderLineChart('drawdownChart', 'Drawdown', labels, ddData, 'rgba(255, 99, 132, 1)');
            const pnls = sortedExecutions.map(ex => Number(ex.pnl || 0));
            const bins = this.makeHistogram(pnls, 20);
            this.renderBarChart('pnlDistributionChart', 'PnL Distribution', bins.labels, bins.counts, 'rgba(75, 192, 192, 0.7)');
            const wins = pnls.filter(v => v > 0).length;
            const losses = pnls.filter(v => v <= 0).length;
            this.renderPieChart('winLossChart', ['Win','Loss'], [wins, losses], ['#2ecc71', '#e74c3c']);
        } catch (e) {
            console.error('Load performance error:', e);
        }
    }

    async loadStrategyExecutions(name, { executions = null, limit = this.strategyExecutionsDefaultLimit, force = false } = {}) {
        try {
            const rawExecutions = Array.isArray(executions)
                ? executions
                : await this.fetchStrategyExecutions(name, { limit, force });
            const filled = rawExecutions.filter(ex => String(ex.status || '').toLowerCase() === 'filled');
            this.executionsPagination.allExecutions = filled;
            this.executionsPagination.totalItems = filled.length;
            this.executionsPagination.currentPage = 1;
            this.renderExecutionsTable();
            this.renderExecutionsPagination();
            this.applyStrategyChartOrders(filled);
        } catch (e) {
            console.error('Load executions error:', e);
        }
    }

    async loadPnLCalendar(name, options = {}) {
        try {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/pnl/daily`);
            if (!res.success) return;
            if (window.PnLCalendar) {
                PnLCalendar.init('pnlCalendarContainer', res.pnl || [], options);
            }
        } catch (e) {
            console.error('Load PnL calendar error:', e);
        }
    }

    renderExecutionsTable() {
        return this.orderManager.renderExecutionsTable();
    }

    renderExecutionsPagination() {
        return this.orderManager.renderExecutionsPagination();
    }

    exportExecutionsCsv(name) {
        return this.orderManager.exportExecutionsCsv(name);
    }

    // Chart helpers
    ensureChartDestroyed(id) {
        return this.uiController?.ensureChartDestroyed?.(id);
    }

    renderLineChart(canvasId, label, labels, data, color) {
        return this.uiController?.renderLineChart?.(canvasId, label, labels, data, color);
    }

    renderBarChart(canvasId, label, labels, data, color) {
        return this.uiController?.renderBarChart?.(canvasId, label, labels, data, color);
    }

    renderPieChart(canvasId, labels, data, colors) {
        return this.uiController?.renderPieChart?.(canvasId, labels, data, colors);
    }

    makeHistogram(values, bins) {
        if (this.uiController?.makeHistogram) {
            return this.uiController.makeHistogram(values, bins);
        }
        return { labels: [], counts: [] };
    }

    async deleteStrategy(name) {
        if (!this.strategiesManager || typeof this.strategiesManager.deleteStrategy !== 'function') {
            return;
        }
        return this.strategiesManager.deleteStrategy(name);
    }

    // Logs functionality removed - panel no longer exists

    clearData() {
        return this.uiController?.clearData?.();
    }

    getStatusColor(status) {
        if (this.uiController?.getStatusColor) {
            return this.uiController.getStatusColor(status);
        }
        return 'secondary';
    }

    // 判断传入日期是否为“今天”（按本地时区）
    isSameDay(dateInput) {
        if (this.uiController?.isSameDay) {
            return this.uiController.isSameDay(dateInput);
        }
        if (!dateInput) return false;
        const d = new Date(dateInput);
        if (Number.isNaN(d.getTime())) return false;
        const now = new Date();
        return d.getFullYear() === now.getFullYear()
            && d.getMonth() === now.getMonth()
            && d.getDate() === now.getDate();
    }

    async searchSymbols(query, dropdownId) {
        if (this.uiController?.searchSymbols) {
            return this.uiController.searchSymbols(query, dropdownId);
        }
    }

    getHistoricalSymbols() {
        if (this.uiController?.getHistoricalSymbols) {
            return this.uiController.getHistoricalSymbols();
        }
        return [];
    }

    addToHistoricalSymbols(symbol) {
        if (this.uiController?.addToHistoricalSymbols) {
            this.uiController.addToHistoricalSymbols(symbol);
        }
    }

    async fetchCurrentPrice(symbol) {
        const priceDisplay = document.getElementById('currentPriceDisplay');
        const priceValue = document.getElementById('currentPriceValue');
        
        if (!priceDisplay || !priceValue) return;
        
        // 如果未显式传入symbol，则尝试从表单读取；若仍为空则不请求
        if (!symbol) {
            const symEl = document.getElementById('orderSymbol');
            symbol = (symEl?.value || '').toUpperCase().trim();
        }
        if (!symbol) {
            priceDisplay.style.display = 'none';
            return;
        }
        
        // 检查连接状态，避免在连接不稳定时进行价格获取
        if (!this.connected) {
            priceDisplay.style.display = 'block';
            priceValue.textContent = '连接中...';
            priceValue.className = 'fw-bold text-warning';
            return;
        }
        
        try {
            // 显示加载状态
            priceDisplay.style.display = 'block';
            priceValue.textContent = '加载中...';
            priceValue.className = 'fw-bold text-muted';
            
            // 使用缓存的当前价格接口
            const currentPrice = await this.dataManager.getCachedPrice(symbol);
            
            if (currentPrice && currentPrice > 0) {
                priceValue.textContent = `$${this.formatNumber(currentPrice)}`;
                priceValue.className = 'fw-bold text-primary';
                
                // 显示数据源信息
                priceValue.title = `数据源: 缓存`;
                
                // 如果是市价单，自动填入价格输入框（即使是disabled状态）
                const orderTypeSelect = document.getElementById('orderType');
                const orderPriceInput = document.getElementById('orderPrice');
                if (orderTypeSelect && orderPriceInput && orderTypeSelect.value === 'MARKET') {
                    orderPriceInput.value = currentPrice.toString();
                    // 触发价格变化事件以更新相关计算
                    orderPriceInput.dispatchEvent(new Event('input'));
                }
            } else {
                priceValue.textContent = '获取失败';
                priceValue.className = 'fw-bold text-warning';
            }
        } catch (error) {
            console.error('获取当前价格失败:', error);
            priceValue.textContent = '获取失败';
            priceValue.className = 'fw-bold text-danger';
        }
    }


    // Duplicate editOrder method removed

    // cancelOrder方法已迁移到OrderManager

    // 日志管理方法已迁移到 logs.js

    async showRiskRuleOrders(symbol) {
        if (!this.riskRulesManager || typeof this.riskRulesManager.showRiskRuleOrders !== 'function') {
            return;
        }
        return this.riskRulesManager.showRiskRuleOrders(symbol);
    }

    // S Rules (Sizing Policy) related functions


    async loadSizingHistory(options = {}) {
        const cachedHistory = this.sizingHistoryCache?.table;
        if (Array.isArray(cachedHistory)) {
            this.renderSizingHistory(cachedHistory);
        } else {
            this.renderSizingHistory([]);
        }

        if (this.ibkrManager && typeof this.ibkrManager.requestSizingHistory === 'function') {
            const result = this.ibkrManager.requestSizingHistory({
                limit: Number.isFinite(options.limit) ? Number(options.limit) : 50,
                symbol: options.symbol,
                since: options.since,
                context: options.context || 'table',
                force: Boolean(options.force),
                minInterval: options.minInterval,
            });
            if (result === 'failed') {
                console.warn('Failed to request sizing history via websocket');
            }
            return result;
        }

        console.warn('WebSocket sizing history request unavailable');
        return 'failed';
    }
    
    // Suggested Position Size functions
    async refreshPositionSuggestion() {
        return this.orderManager.refreshPositionSuggestion();
    }

    updatePositionSuggestionUI(data) {
        return this.orderManager.updatePositionSuggestionUI(data);
    }

    clearPositionSuggestion() {
        return this.orderManager.clearPositionSuggestion();
    }

    initSidePanels() {
        if (this.sidePanelManagerInitialized) return;
        const overlay = document.getElementById('panelOverlay');
        const configurationPanel = document.getElementById('configurationPanel');
        const notificationsPanel = document.getElementById('notificationsPanel');
        const openConfigurationBtn = document.getElementById('openConfigurationBtn');
        const openNotificationsBtn = document.getElementById('openNotificationsBtn');

        if (!overlay || !configurationPanel || !notificationsPanel) {
            return;
        }

        const updateButtonState = () => {
            if (openConfigurationBtn) {
                openConfigurationBtn.classList.toggle('active', configurationPanel.classList.contains('open'));
            }
            if (openNotificationsBtn) {
                openNotificationsBtn.classList.toggle('active', notificationsPanel.classList.contains('open'));
            }
        };

        const focusTarget = (focusOption) => {
            if (!focusOption) return;
            let target = focusOption;
            if (typeof focusOption === 'string') {
                target = configurationPanel.querySelector(focusOption);
            }
            if (!(target instanceof Element)) return;
            const collapseParent = target.closest('.collapse');
            if (collapseParent && collapseParent.classList.contains('collapse')) {
                const collapseInstance = bootstrap?.Collapse?.getOrCreateInstance?.(collapseParent, { toggle: false })
                    || (bootstrap?.Collapse ? new bootstrap.Collapse(collapseParent, { toggle: false }) : null);
                collapseInstance?.show();
            }
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            target.classList.add('panel-focus-highlight');
            setTimeout(() => target.classList.remove('panel-focus-highlight'), 1400);
        };

        const closePanels = () => {
            configurationPanel.classList.remove('open');
            notificationsPanel.classList.remove('open');
            configurationPanel.setAttribute('aria-hidden', 'true');
            notificationsPanel.setAttribute('aria-hidden', 'true');
            overlay.classList.remove('active');
            document.body.classList.remove('side-panel-open');
            updateButtonState();
        };

        const openPanel = (panel, options = {}) => {
            if (!panel) return;
            if (panel === configurationPanel) {
                notificationsPanel.classList.remove('open');
                notificationsPanel.setAttribute('aria-hidden', 'true');
            } else {
                configurationPanel.classList.remove('open');
                configurationPanel.setAttribute('aria-hidden', 'true');
            }
            panel.classList.add('open');
            panel.setAttribute('aria-hidden', 'false');
            overlay.classList.add('active');
            document.body.classList.add('side-panel-open');
            updateButtonState();
            if (panel === configurationPanel && options.focus) {
                setTimeout(() => focusTarget(options.focus), 180);
            }
        };

        const togglePanel = (panel) => {
            if (!panel) return;
            if (panel.classList.contains('open')) {
                closePanels();
            } else {
                openPanel(panel);
            }
        };

        const handleButtonClick = (panel) => (event) => {
            event?.preventDefault?.();
            event?.stopPropagation?.();
            togglePanel(panel);
        };

        openConfigurationBtn?.addEventListener('click', handleButtonClick(configurationPanel));
        openNotificationsBtn?.addEventListener('click', handleButtonClick(notificationsPanel));

        overlay.addEventListener('click', () => closePanels());

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && overlay.classList.contains('active')) {
                closePanels();
            }
        });

        window.sidePanelManager = {
            openConfiguration: (options = {}) => openPanel(configurationPanel, options),
            openNotifications: () => openPanel(notificationsPanel),
            close: closePanels,
            isConfigurationOpen: () => configurationPanel.classList.contains('open'),
            isNotificationsOpen: () => notificationsPanel.classList.contains('open'),
        };

        updateButtonState();
        this.sidePanelManagerInitialized = true;
    }

    applySuggestedQuantity() {
        if (this.lastSuggestedQuantity) {
            const quantityInput = document.getElementById('orderQuantity');
            if (quantityInput) {
                quantityInput.value = this.lastSuggestedQuantity;
                this.showAlert('success', '已应用建议数量');
            }
        } else {
            this.showAlert('warning', '没有可用的建议数量');
        }
    }

    // 通知管理方法
    // addNotification 方法已迁移到 notifications.js

    // 通知相关方法已迁移到 notifications.js
    // clearNotifications, updateNotificationPanel, removeNotification 方法已迁移

    // 通知设置功能
    // showNotificationSettings 和 populateBasicSettings 方法已迁移到 notifications.js

    // populateModuleSettings 方法已迁移到 notifications.js

    // updateNotificationInputStates 方法已迁移到 notifications.js

    // getAvailableModules method has been migrated to notifications.js

    // saveNotificationSettings method has been migrated to notifications.js

    // testTradingReminder method has been migrated to notifications.js

    // checkTradingReminderStatus method has been migrated to notifications.js

    // testNotification method has been migrated to notifications.js

    // loadNotificationHistory, renderNotificationHistoryRows, getNotificationStatusBadge, 
    // getChannelIcon, and filterNotificationHistory methods have been migrated to notifications.js


    async loadGSRulesStatus() {
        if (!this.strategiesManager?.loadGSRulesStatus) return 'failed';
        return this.strategiesManager.loadGSRulesStatus();
    }

    updateRuleStatus(ruleId, status, lastCheck) {
        if (!this.strategiesManager?.updateRuleStatus) return;
        this.strategiesManager.updateRuleStatus(ruleId, status, lastCheck);
    }

    async showConfigModal(activeTab = 'g-rules') {
        // 激活指定的标签页
        const gRulesTab = document.getElementById('config-g-rules-tab');
        const sRulesTab = document.getElementById('config-s-rules-tab');
        const gRulesPane = document.getElementById('config-g-rules');
        const sRulesPane = document.getElementById('config-s-rules');
        
        if (activeTab === 'g-rules') {
            gRulesTab.classList.add('active');
            gRulesTab.setAttribute('aria-selected', 'true');
            sRulesTab.classList.remove('active');
            sRulesTab.setAttribute('aria-selected', 'false');
            gRulesPane.classList.add('show', 'active');
            sRulesPane.classList.remove('show', 'active');
        } else {
            sRulesTab.classList.add('active');
            sRulesTab.setAttribute('aria-selected', 'true');
            gRulesTab.classList.remove('active');
            gRulesTab.setAttribute('aria-selected', 'false');
            sRulesPane.classList.add('show', 'active');
            gRulesPane.classList.remove('show', 'active');
        }
        
        // 加载现有配置数据
        await this.loadConfigData();
        
        // 添加保存事件监听器
        const saveBtn = document.getElementById('saveConfigRulesBtn');
        const resetBtn = document.getElementById('resetConfigRulesBtn');
        
        // 移除旧的事件监听器
        const newSaveBtn = saveBtn.cloneNode(true);
        const newResetBtn = resetBtn.cloneNode(true);
        saveBtn.parentNode.replaceChild(newSaveBtn, saveBtn);
        resetBtn.parentNode.replaceChild(newResetBtn, resetBtn);
        
        newSaveBtn.addEventListener('click', () => {
            this.saveConfigFromModal();
        });
        
        newResetBtn.addEventListener('click', () => {
            this.resetConfigFromModal();
        });
        
        // 更新Position Value字段显示
        this.updatePositionValueField();
        
        // 显示模态框
        const modal = document.getElementById('configRulesModal');
        const bootstrapModal = new bootstrap.Modal(modal);
        bootstrapModal.show();
    }

    async saveConfigFromModal() {
        try {
            // 获取G Rules配置
            const gRulesConfig = {
                g0_max_drawdown: parseFloat(document.getElementById('g0MaxDrawdown').value) || 5.0,
                g1_max_loss: parseFloat(document.getElementById('g1MaxLoss').value) || 2.0,
                g2_max_position_per_symbol: parseFloat(document.getElementById('g2MaxPositionPerSymbol').value) || 10.0,
                g3_correlation_check: document.getElementById('g3CorrelationCheck').value === 'enabled',
                g4_liquidity_check: document.getElementById('g4LiquidityCheck').value === 'enabled',
                g5_time_window: document.getElementById('g5TimeWindow').value === 'enabled'
            };
            
            // 获取S Rules配置
            const sRulesConfig = {
                s0_position_sizing: document.getElementById('s0PositionSizing').value || 'fixed',
                s1_risk_percentage: parseFloat(document.getElementById('s1PositionValue').value) || 1.0,
                s2_max_positions: parseInt(document.getElementById('s2MaxPositions').value) || 10,
                s3_volatility_adjustment: document.getElementById('s3Rebalancing').value === 'enabled',
                s4_volatility_adjustment: document.getElementById('s4VolatilityAdjustment').value === 'enabled'
            };
            
            // 保存到后端
            await this.apiCall('/config/rules', {
                method: 'PUT',
                body: JSON.stringify({ 
                    g_rules: gRulesConfig,
                    s_rules: sRulesConfig
                })
            });
            
            this.showAlert('success', '配置已保存');
            
            // 关闭模态框
            const modal = bootstrap.Modal.getInstance(document.getElementById('configRulesModal'));
            if (modal) modal.hide();
            
            // 重新加载状态
            this.loadGSRulesStatus();
            
        } catch (error) {
            console.error('保存配置失败:', error);
            this.showAlert('danger', '保存配置失败');
        }
    }
    
    resetConfigFromModal() {
        // 重置G Rules为默认值
        document.getElementById('g0MaxDrawdown').value = 5.0;
        document.getElementById('g1MaxLoss').value = 2.0;
        document.getElementById('g2MaxPositionPerSymbol').value = 10.0;
        document.getElementById('g3CorrelationCheck').value = 'enabled';
        document.getElementById('g4LiquidityCheck').value = 'enabled';
        document.getElementById('g5TimeWindow').value = 'enabled';
        
        // 重置S Rules为默认值
        document.getElementById('s0PositionSizing').value = 'fixed';
        document.getElementById('s1PositionValue').value = 1.0;
        document.getElementById('s2MaxPositions').value = 10;
        document.getElementById('s3Rebalancing').value = 'enabled';
        document.getElementById('s4VolatilityAdjustment').value = 'enabled';
        
        // 更新Position Value字段显示
        this.updatePositionValueField();
        
        this.showAlert('info', '配置已重置为默认值');
    }

    updatePositionValueField() {
        return this.orderManager.updatePositionValueField();
    }

    async loadConfigData() {
        try {
            const response = await this.apiCall('/config/rules', {
                method: 'GET'
            });
            
            if (response && response.g_rules) {
                // 填充G Rules配置
                const gRules = response.g_rules;
                document.getElementById('g0MaxDrawdown').value = gRules.g0_max_drawdown || 5.0;
                document.getElementById('g1MaxLoss').value = gRules.g1_max_loss || 2.0;
                document.getElementById('g2MaxPositionPerSymbol').value = gRules.g2_max_position_per_symbol || 10.0;
                document.getElementById('g3CorrelationCheck').value = gRules.g3_correlation_check ? 'enabled' : 'disabled';
                document.getElementById('g4LiquidityCheck').value = gRules.g4_liquidity_check ? 'enabled' : 'disabled';
                document.getElementById('g5TimeWindow').value = gRules.g5_time_window ? 'enabled' : 'disabled';
            }
            
            if (response && response.s_rules) {
                // 填充S Rules配置
                const sRules = response.s_rules;
                document.getElementById('s0PositionSizing').value = sRules.s0_position_sizing || 'fixed';
                document.getElementById('s1PositionValue').value = sRules.s1_risk_percentage || 1.0;
                document.getElementById('s2MaxPositions').value = sRules.s2_max_positions || 10;
                document.getElementById('s3Rebalancing').value = sRules.s3_volatility_adjustment ? 'enabled' : 'disabled';
                document.getElementById('s4VolatilityAdjustment').value = sRules.s4_volatility_adjustment ? 'enabled' : 'disabled';
            }
            
        } catch (error) {
            console.error('加载配置数据失败:', error);
            // 如果加载失败，使用默认值
            this.resetConfigFromModal();
        }
    }

    // 显示订单notes的浮动提示
    showNotesTooltip(event, element) {
        const notes = element.getAttribute('data-notes');
        const displayNotes = (!notes || notes.trim() === '') ? 'N/A' : notes;

        // 移除已存在的提示框
        this.hideNotesTooltip();

        // 创建提示框
        const tooltip = document.createElement('div');
        tooltip.id = 'notes-tooltip';
        tooltip.className = 'notes-tooltip';
        tooltip.innerHTML = displayNotes;
        tooltip.style.position = 'fixed';
        tooltip.style.zIndex = '9999';
        tooltip.style.display = 'block';
        document.body.appendChild(tooltip);

        // 计算位置
        const tooltipRect = tooltip.getBoundingClientRect();
        
        let left = event.clientX + 10;
        let top = event.clientY - tooltipRect.height - 10;
        
        // 防止提示框超出屏幕边界
        if (left + tooltipRect.width > window.innerWidth) {
            left = event.clientX - tooltipRect.width - 10;
        }
        if (top < 0) {
            top = event.clientY + 10;
        }
        
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
    }

    // 隐藏订单notes的浮动提示
    hideNotesTooltip() {
        const tooltip = document.getElementById('notes-tooltip');
        if (tooltip) {
            tooltip.remove();
        }
    }

    // 启动订单状态自动刷新
    startOrdersAutoRefresh() {
        return this.orderManager.startOrdersAutoRefresh();
    }

    // 停止订单状态自动刷新
    stopOrdersAutoRefresh() {
        return this.orderManager.stopOrdersAutoRefresh();
    }
}

// Initialize the app
const app = new TradingApp();
// expose for inline onclick handlers
window.app = app;
// Attach NotificationsManager shim if available
if (window.__attachNotifications) {
    window.__attachNotifications(app);
}
