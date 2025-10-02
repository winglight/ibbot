const globalScope = typeof window !== 'undefined' ? window : globalThis;
const appModules = globalScope.appModules = globalScope.appModules || {};
const monitoringModules = appModules.monitoring || {};
const MonitoringConstants = monitoringModules.constants || {};
const MonitoringUtils = monitoringModules.utils || {};
const MonitoringManagerModule = monitoringModules.manager || {};
const MonitoringManagerClass = MonitoringManagerModule.MonitoringManager || globalScope.MonitoringManager;
const MonitoringDashboardModule = monitoringModules.dashboard || {};
const MonitoringDashboardClass = MonitoringDashboardModule.MonitoringDashboard || globalScope.MonitoringDashboard;
const SharedUtils = appModules.utils || {};
const strategiesModules = appModules.strategies || {};
const StrategiesManagerModule = strategiesModules.manager || {};
const StrategiesManagerClass = StrategiesManagerModule.StrategiesManager || globalScope.StrategiesManager;
const StrategiesDetailsModule = strategiesModules.details || {};
const StrategyDetailsControllerClass = StrategiesDetailsModule.StrategyDetailsController || globalScope.StrategyDetailsController;
const uiModules = appModules.ui || {};
const UIControllerModule = uiModules.controller || {};
const UIControllerClass = UIControllerModule.UIController || globalScope.UIController;
const ordersModules = appModules.orders || {};
const OrdersManagerModule = ordersModules.manager || {};
const OrdersManagerClass = OrdersManagerModule.OrderManager || globalScope.OrderManager;
const notificationsModules = appModules.notifications || {};
const NotificationsManagerModule = notificationsModules.manager || {};
const NotificationsManagerClass = NotificationsManagerModule.NotificationsManager || globalScope.NotificationsManager;
const serviceRestartModules = appModules.serviceRestart || {};

const getConfigCenterModules = () => {
    const modules = globalScope.appModules?.configCenter;
    return modules && typeof modules === 'object' ? modules : null;
};

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

const fallbackNormalizeRuleStatus = (status) => {
    if (!status || typeof status !== 'object') {
        return { passed: true, message: 'Normal' };
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
        message = passed ? 'Normal' : 'Violation';
    }
    return { passed, message };
};

const requireMonitoringUtil = (fn, name) => {
    if (typeof fn !== 'function') {
        throw new Error(`monitoring.utils.${name} is required`);
    }
    return fn;
};

const {
    normalizeIntervalKey,
    normalizeDurationKey,
    getMonitorDurationMinutes,
    getMonitorDurationOptions,
    refreshMonitorDurationSelect,
    getTrendLevelStyle,
    getTrendSuggestionLabel
} = MonitoringUtils;

requireMonitoringUtil(normalizeIntervalKey, 'normalizeIntervalKey');
requireMonitoringUtil(normalizeDurationKey, 'normalizeDurationKey');
requireMonitoringUtil(getMonitorDurationMinutes, 'getMonitorDurationMinutes');
requireMonitoringUtil(getMonitorDurationOptions, 'getMonitorDurationOptions');
requireMonitoringUtil(refreshMonitorDurationSelect, 'refreshMonitorDurationSelect');
requireMonitoringUtil(getTrendLevelStyle, 'getTrendLevelStyle');
requireMonitoringUtil(getTrendSuggestionLabel, 'getTrendSuggestionLabel');

const {
    TrendSignalLabelPlugin,
    slugifyStrategyName,
    formatNumber: sharedFormatNumber,
    escapeHtml: sharedEscapeHtml,
    formatDuration: sharedFormatDuration,
    isSameDay: sharedIsSameDay,
} = SharedUtils;

if (!TrendSignalLabelPlugin || typeof TrendSignalLabelPlugin.afterDatasetsDraw !== 'function') {
    throw new Error('utils.TrendSignalLabelPlugin is required');
}
if (typeof slugifyStrategyName !== 'function') {
    throw new Error('utils.slugifyStrategyName is required');
}
if (typeof sharedFormatNumber !== 'function') {
    throw new Error('utils.formatNumber is required');
}
if (typeof sharedEscapeHtml !== 'function') {
    throw new Error('utils.escapeHtml is required');
}
if (typeof sharedFormatDuration !== 'function') {
    throw new Error('utils.formatDuration is required');
}
if (typeof sharedIsSameDay !== 'function') {
    throw new Error('utils.isSameDay is required');
}

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
        // 缓存当日订单列表，供监控时查找最新建仓/加仓时间
        this.orders = [];
        // 通知列表
        this.notifications = [];
        this.serviceRestartController = null;
        this.configCenterManager = null;
        this.configCenterInitRetry = null;

        if (typeof MonitoringManagerClass !== 'function') {
            throw new Error('MonitoringManager module is required');
        }
        this.monitoringManager = new MonitoringManagerClass(this);
        this.monitoring = this.monitoringManager;
        this.wsHandlers = new Map();
        this.monitoringDashboard = typeof MonitoringDashboardClass === 'function'
            ? new MonitoringDashboardClass(this)
            : null;
        this.bindMonitoringStateProxies();
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
        this.riskRulesManager = new RiskRulesManager(this);
        this.strategiesManager = typeof StrategiesManagerClass === 'function'
            ? new StrategiesManagerClass(this)
            : null;
        if (this.strategiesManager) {
            this.bindStrategiesStateProxies();
        }
        this.strategyDetailsController = typeof StrategyDetailsControllerClass === 'function'
            ? new StrategyDetailsControllerClass(this)
            : null;
        if (this.strategyDetailsController) {
            this.bindStrategyDetailsStateProxies();
        } else {
            this.strategyDetailsState = {
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
            this.strategyDetailsTabListenerBound = false;
            this.currentStrategyDetails = null;
        }
        this.registerWebSocketHandler('strategy_performance', (payload) => this.handleStrategyPerformanceMessage(payload));
        this.registerWebSocketHandler('strategies_snapshot', (payload) => this.handleStrategiesSnapshot(payload));
        this.registerWebSocketHandler('strategy_status', (payload) => this.handleStrategyStatusMessage(payload));
        this.registerWebSocketHandler('sizing_history', (payload) => this.handleSizingHistorySnapshot(payload));
        this.registerWebSocketHandler('service_statuses', (payload) => this.handleServiceStatusesMessage(payload));

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
            this,
            this.apiCall.bind(this),
            this.showAlert.bind(this),
            this.formatNumber.bind(this)
        );
        this.notificationsManager = typeof NotificationsManagerClass === 'function'
            ? new NotificationsManagerClass(this)
            : new NotificationsManager(this);
        this.logsManagerInitialized = false;
        this.notificationsManagerInitialized = false;
        this.authenticatedModulesInitialized = false;
        this.uiController = typeof UIControllerClass === 'function'
            ? new UIControllerClass(this)
            : null;
        this.domManager = new DOMManager(this);

        this.callMonitoringManager('guardChartScriptableDescriptors');
        this.init();
    }

    getMonitoringManager(methodName = null) {
        const manager = this.monitoringManager;
        if (!manager) {
            throw new Error('MonitoringManager is not initialized');
        }
        if (methodName && typeof manager[methodName] !== 'function') {
            throw new Error(`MonitoringManager.${methodName} is not available`);
        }
        return manager;
    }

    callMonitoringManager(methodName, ...args) {
        const manager = this.getMonitoringManager(methodName);
        return manager[methodName](...args);
    }

    bindMonitoringStateProxies() {
        const manager = this.getMonitoringManager('getManagedProperties');
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

    bindStrategyDetailsStateProxies() {
        const controller = this.strategyDetailsController;
        if (!controller || typeof controller.getManagedProperties !== 'function') {
            return;
        }
        const properties = controller.getManagedProperties();
        properties.forEach((prop) => {
            if (Object.prototype.hasOwnProperty.call(this, prop)) {
                delete this[prop];
            }
            Object.defineProperty(this, prop, {
                configurable: true,
                enumerable: false,
                get: () => controller[prop],
                set: (value) => {
                    controller[prop] = value;
                }
            });
        });
    }

    callStrategyDetails(methodName, ...args) {
        const controller = this.strategyDetailsController;
        if (!controller || typeof controller[methodName] !== 'function') {
            console.warn(`Strategy details controller unavailable: ${methodName}`);
            return undefined;
        }
        return controller[methodName](...args);
    }

    async openStrategyDetails(name) {
        return this.callStrategyDetails('openStrategyDetails', name);
    }

    initializeStrategyDetailsState(name) {
        return this.callStrategyDetails('initializeStrategyDetailsState', name);
    }

    prepareStrategyDetailsModal(name) {
        return this.callStrategyDetails('prepareStrategyDetailsModal', name);
    }

    ensureStrategyDetailsTabListener() {
        return this.callStrategyDetails('ensureStrategyDetailsTabListener');
    }

    showStrategyDetailsModal() {
        return this.callStrategyDetails('showStrategyDetailsModal');
    }

    async initializeStrategyDetailsContent(name) {
        return this.callStrategyDetails('initializeStrategyDetailsContent', name);
    }

    activateStrategyDetailsDefaultTab() {
        return this.callStrategyDetails('activateStrategyDetailsDefaultTab');
    }

    async requestActiveStrategyDetailsTabLoad(options = {}) {
        return this.callStrategyDetails('requestActiveStrategyDetailsTabLoad', options);
    }

    async triggerStrategyDetailsTabLoad(targetSelector, options = {}) {
        return this.callStrategyDetails('triggerStrategyDetailsTabLoad', targetSelector, options);
    }

    async fetchStrategyBasicInfo(name) {
        return this.callStrategyDetails('fetchStrategyBasicInfo', name);
    }

    async fetchStrategyInternalInfo(name) {
        return this.callStrategyDetails('fetchStrategyInternalInfo', name);
    }

    async loadStrategySummaryTab(options) {
        return this.callStrategyDetails('loadStrategySummaryTab', options);
    }

    async loadStrategyRiskTab(options) {
        return this.callStrategyDetails('loadStrategyRiskTab', options);
    }

    async loadStrategyInternalOrdersTab(options) {
        return this.callStrategyDetails('loadStrategyInternalOrdersTab', options);
    }

    async loadStrategyChartsTab(options) {
        return this.callStrategyDetails('loadStrategyChartsTab', options);
    }

    async loadStrategyStrategyChartTab(options) {
        return this.callStrategyDetails('loadStrategyStrategyChartTab', options);
    }

    async loadStrategyExecutionsTab(options) {
        return this.callStrategyDetails('loadStrategyExecutionsTab', options);
    }

    async loadStrategyPnlCalendarTab(options) {
        return this.callStrategyDetails('loadStrategyPnlCalendarTab', options);
    }

    loadStrategyApiExamplesTab() {
        return this.callStrategyDetails('loadStrategyApiExamplesTab');
    }

    updateStrategyDetailsHeader(strategy) {
        return this.callStrategyDetails('updateStrategyDetailsHeader', strategy);
    }

    configureStrategyDetailsTabs(strategy) {
        return this.callStrategyDetails('configureStrategyDetailsTabs', strategy);
    }

    clearStrategyExecutionsCache(name) {
        return this.strategiesManager?.clearStrategyExecutionsCache?.(name);
    }

    async fetchStrategyExecutions(name, options = {}) {
        if (!this.strategiesManager?.fetchStrategyExecutions) {
            return [];
        }
        return this.strategiesManager.fetchStrategyExecutions(name, options);
    }

    async loadInternalStrategyOrders(name, options = {}) {
        if (!this.strategiesManager?.loadInternalStrategyOrders) {
            return [];
        }
        return this.strategiesManager.loadInternalStrategyOrders(name, options);
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
        if (!this.wsHandlers) {
            this.wsHandlers = new Map();
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
        if (this.riskRulesManager?.normalizeRuleStatus) {
            return this.riskRulesManager.normalizeRuleStatus(status);
        }
        return fallbackNormalizeRuleStatus(status);
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
        return this.callMonitoringManager('addDomSignal', signal);
    }

    ensureMonitorAnnotations() {
        return this.callMonitoringManager('ensureMonitorAnnotations');
    }

    updateDomSignalAnnotations() {
        return this.callMonitoringManager('updateDomSignalAnnotations');
    }

    setMonitorTrendHistory(points = []) {
        return this.callMonitoringManager('setMonitorTrendHistory', points);
    }

    resetMonitorTrendData() {
        return this.callMonitoringManager('resetMonitorTrendData');
    }

    formatTrendPointForChart(point) {
        return this.callMonitoringManager('formatTrendPointForChart', point);
    }

    getFormattedTrendChartData() {
        return this.callMonitoringManager('getFormattedTrendChartData');
    }

    updateMonitorSignalOverlayDatasets() {
        return this.callMonitoringManager('updateMonitorSignalOverlayDatasets');
    }

    renderMonitorSignalHistory() {
        return this.callMonitoringManager('renderMonitorSignalHistory');
    }

    getTrendValues(chart) {
        return this.callMonitoringManager('getTrendValues', chart);
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
        return this.callMonitoringManager('setupTrendLegendAndAnnotations', chart);
    }

    // 需要时手动调用；只在数值变化时更新 yMin/yMax，避免触发外层 watch 循环
    refreshTrend(chart) {
        return this.callMonitoringManager('refreshTrend', chart);
    }


    syncTrendReferenceLines() {
        return this.callMonitoringManager('syncTrendReferenceLines');
    }

    deriveTrendLevel(score) {
        return this.callMonitoringManager('deriveTrendLevel', score);
    }

    addMonitorTrendPoint(time, score, options = {}) {
        return this.callMonitoringManager('addMonitorTrendPoint', time, score, options);
    }

    syncCharts(source, target) {
        return this.callMonitoringManager('syncCharts', source, target);
    }

    normalizeMonitorTime(value) {
        return this.callMonitoringManager('normalizeMonitorTime', value);
    }

    getMonitorDisplayTimes() {
        return this.callMonitoringManager('getMonitorDisplayTimes');
    }

    getMonitorDisplayLabels() {
        return this.callMonitoringManager('getMonitorDisplayLabels');
    }

    extractRangeFromMonitorChart() {
        return this.callMonitoringManager('extractRangeFromMonitorChart');
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

    isAuthenticated() {
        try {
            if (this.loginManager && typeof this.loginManager.isLoggedIn === 'function') {
                return this.loginManager.isLoggedIn();
            }
        } catch (error) {
            console.warn('Failed to determine authentication state from login manager:', error);
        }
        try {
            return !!localStorage.getItem('access_token');
        } catch (error) {
            console.warn('Failed to read authentication token from storage:', error);
        }
        return false;
    }

    initializeAuthenticatedModules(options = {}) {
        if (!this.isAuthenticated()) {
            return false;
        }

        const { force = false } = options || {};

        if (!this.logsManagerInitialized && this.logsManager?.init) {
            try {
                const maybePromise = this.logsManager.init();
                if (maybePromise?.catch) {
                    maybePromise.catch((error) => {
                        console.error('Failed to initialize logs manager:', error);
                    });
                }
            } catch (error) {
                console.error('Failed to initialize logs manager:', error);
            }
            this.logsManagerInitialized = true;
        } else if (force) {
            try {
                this.loadLogModules();
                this.loadLogs();
            } catch (error) {
                console.error('Failed to refresh logs after authentication:', error);
            }
        }

        if (!this.notificationsManagerInitialized && this.notificationsManager?.init) {
            try {
                this.notificationsManager.init();
            } catch (error) {
                console.error('Failed to initialize notifications manager:', error);
            }
            this.notificationsManagerInitialized = true;
        } else if (force && this.notificationsManager?.filterNotificationHistory) {
            try {
                this.notificationsManager.filterNotificationHistory();
            } catch (error) {
                console.error('Failed to refresh notification history after authentication:', error);
            }
        }

        try {
            this.strategiesManager?.loadStrategies?.();
        } catch (error) {
            console.error('Failed to load strategies after authentication:', error);
        }

        try {
            this.loadGSRulesStatus();
        } catch (error) {
            console.error('Failed to load G/S rules status after authentication:', error);
        }

        try {
            this.refreshHolidayDataPushSetting();
        } catch (error) {
            console.error('Failed to refresh holiday data push setting after authentication:', error);
        }

        try {
            this.riskRulesManager?.loadRiskRules?.();
        } catch (error) {
            console.error('Failed to load risk rules after authentication:', error);
        }

        try {
            this.configCenterManager?.refreshModules?.();
        } catch (error) {
            console.error('Failed to refresh config modules after authentication:', error);
        }

        this.authenticatedModulesInitialized = true;
        return true;
    }

    handleLogout() {
        this.authenticatedModulesInitialized = false;
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

                const candlestickRegistered = this.callMonitoringManager('registerCandlestickControllers');
                if (!candlestickRegistered) {
                    console.warn('[Monitor] candlestick plugin unavailable during initialization; candlestick charts will fall back to line charts.');
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
            const initialized = this.initializeAuthenticatedModules();
            if (!initialized) {
                console.info('Authentication not available yet, deferring protected requests until login.');
            }
            // 已改为WebSocket推送更新订单，取消轮询刷新
            await cfgPromise.catch(() => {});
        }, 50);

        if (this.monitoringDashboard && typeof this.monitoringDashboard.init === 'function') {
            try {
                this.monitoringDashboard.init();
            } catch (error) {
                console.error('Failed to initialise monitoring dashboard', error);
            }
        }
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
        this.initHolidayDataPushToggle();
        this.initConfigCenter();

        this.monitorDomHistoryList = document.getElementById('monitorDomHistoryList');
        this.monitorDomHistoryMeta = document.getElementById('monitorDomHistoryMeta');

        // DOM 已就绪时同步访客模式下的界面元素状态，避免初始渲染遗漏
        this.updateReadOnlyMode(this.isReadOnly, this.authRole, { persist: false });

        if (!this.serviceRestartController && serviceRestartModules?.createListController) {
            const listContainer = document.getElementById('serviceStatusList');
            if (listContainer) {
                const controller = serviceRestartModules.createListController({
                    document,
                    container: listContainer,
                    fetcher: (payload) => this.apiCall('/services/restart', {
                        method: 'POST',
                        body: JSON.stringify(payload),
                    }),
                    describeService: (serviceName, element) => {
                        if (element?.dataset?.serviceLabel) {
                            return element.dataset.serviceLabel;
                        }
                        const title = element?.querySelector('.fw-semibold');
                        return title?.textContent?.trim() || serviceName;
                    },
                    readOnlyMessage: '访客模式无法执行重启操作',
                    runningMessage: '正在重启 {service}...',
                    successMessage: '已重启 {services}',
                    dependencySuccessMessage: '{service} 已随依赖重启',
                    errorMessage: '重启失败，请稍后重试',
                    initialMessage: '点击重启以重新启动 {service}。',
                    badgeLabels: {
                        idle: '已下线',
                        running: '重启中',
                        success: '运行中',
                        error: '不健康',
                    },
                    onStatusChange: (info) => {
                        if (info.meta?.trigger !== 'restart') {
                            return;
                        }
                        if (info.state === 'success') {
                            this.showAlert('success', info.message);
                        } else if (info.state === 'error') {
                            const errorObj = info.payload?.error;
                            const level = errorObj?.isReadOnly ? 'warning' : 'danger';
                            this.showAlert(level, info.message);
                        }
                    },
                });
                if (controller) {
                    this.serviceRestartController = controller;
                    if (this.isReadOnly && typeof controller.setReadOnly === 'function') {
                        controller.setReadOnly(true);
                    }
                    const wsResult = this.ibkrManager?.requestServiceStatuses
                        ? this.ibkrManager.requestServiceStatuses({ force: true })
                        : 'failed';
                    if (wsResult === 'failed') {
                        this.loadServiceStatuses({ silent: true, preferSocket: false }).catch((error) => {
                            console.warn('Failed to load initial service statuses', error);
                        });
                    }
                }
            }
        }

        const refreshButton = document.getElementById('btn-refresh');
        if (refreshButton) {
            refreshButton.addEventListener('click', () => {
                const wsResult = this.ibkrManager?.requestServiceStatuses
                    ? this.ibkrManager.requestServiceStatuses({ force: true })
                    : 'failed';
                if (wsResult === 'failed') {
                    this.loadServiceStatuses().catch((error) => {
                        console.warn('Failed to refresh service statuses', error);
                    });
                }
            });
        }

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
                throw new Error('Request timed out, please try again later');
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
                    this.showAlert('warning', 'The guest account can only view data');
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
                    let detailPayload = (data && typeof data === 'object') ? data.detail : null;
                    if (detailPayload && typeof detailPayload === 'object') {
                        const message = detailPayload.message || detailPayload.detail || 'Forbidden';
                        const lowered = String(message).toLowerCase();
                        if (lowered.includes('read-only')) {
                            const now = Date.now();
                            if (now - this.lastReadOnlyWarningAt > 1500) {
                                this.showAlert('warning', message);
                                this.lastReadOnlyWarningAt = now;
                            }
                            const err = new Error(message);
                            err.isReadOnly = true;
                            err.code = 'READ_ONLY';
                            throw err;
                        }
                        if (detailPayload.error === 'RBAC_FORBIDDEN') {
                            const requiredRoles = Array.isArray(detailPayload.required_roles)
                                ? detailPayload.required_roles
                                : [];
                            const friendly = requiredRoles.length
                                ? `You do not have permission to perform this action. Required role: ${requiredRoles.join(', ')}`
                                : message;
                            this.showAlert('danger', friendly);
                            const err = new Error(friendly);
                            err.code = detailPayload.error;
                            err.requiredRoles = requiredRoles;
                            err.status = 403;
                            throw err;
                        }
                        const err = new Error(message);
                        err.status = 403;
                        this.showAlert('danger', message);
                        throw err;
                    }

                    const rawDetail = (data && typeof data === 'object' && data.detail !== undefined)
                        ? data.detail
                        : detailPayload;
                    const detail = rawDetail ?? 'Forbidden';
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
                    const err = new Error(typeof detail === 'string' ? detail : 'Forbidden');
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

        if (this.serviceRestartController) {
            const restartCtrl = this.serviceRestartController;
            if (typeof restartCtrl.setReadOnly === 'function') {
                restartCtrl.setReadOnly(this.isReadOnly);
            } else {
                const statusEl = restartCtrl.elements?.status;
                if (this.isReadOnly) {
                    if (restartCtrl.elements?.button) {
                        restartCtrl.elements.button.disabled = true;
                    }
                    restartCtrl.updateStatus('error', '访客模式无法执行重启操作');
                } else {
                    if (restartCtrl.elements?.button) {
                        restartCtrl.elements.button.disabled = false;
                    }
                    const statusText = statusEl?.textContent?.trim();
                    if (statusText === '访客模式无法执行重启操作' || !statusText) {
                        restartCtrl.updateStatus('idle', '选择服务后执行重启。');
                    }
                }
            }
        }

        if (this.configCenterManager?.setReadOnly) {
            try {
                this.configCenterManager.setReadOnly(this.isReadOnly);
            } catch (error) {
                console.error('Failed to update config center read-only state', error);
            }
        }

        const holidayToggle = document.getElementById('holidayDataPushSwitch');
        if (holidayToggle) {
            const storedState = holidayToggle.dataset.currentState;
            const currentState = storedState === 'on'
                ? true
                : storedState === 'off'
                    ? false
                    : !!holidayToggle.checked;
            this.setHolidayDataPushState(currentState);
        }
    }

    applyServiceStatusesPayload(payload, options = {}) {
        const controller = this.serviceRestartController;
        if (!controller || !controller.items) {
            return false;
        }

        const statusMap = {
            running: { state: 'success', fallback: '运行中' },
            offline: { state: 'idle', fallback: '服务已下线' },
            unhealthy: { state: 'error', fallback: '服务状态异常' },
        };

        const services = Array.isArray(payload?.services) ? payload.services : [];
        const seen = new Set();
        const metaDefaults = {
            skipStateUpdate: true,
            source: options.source || payload?.source || 'websocket',
        };
        const effectiveSilent = options.silent !== false;

        services.forEach((entry) => {
            if (!entry || typeof entry !== 'object') {
                return;
            }
            const serviceName = entry.service;
            if (!serviceName || !controller.items[serviceName]) {
                return;
            }
            const rawStatus = String(entry.status || 'running').toLowerCase();
            const statusInfo = statusMap[rawStatus] || statusMap.running;
            const message = entry.detail || entry.message || statusInfo.fallback;
            seen.add(serviceName);
            controller.updateStatus(
                serviceName,
                statusInfo.state,
                message,
                entry,
                { ...metaDefaults, silent: effectiveSilent }
            );
        });

        Object.entries(controller.items).forEach(([serviceName, item]) => {
            if (seen.has(serviceName)) {
                return;
            }
            const currentMessage = item?.status?.textContent?.trim() || '';
            if (!currentMessage || currentMessage === item?.defaultIdleMessage) {
                controller.updateStatus(
                    serviceName,
                    'success',
                    '运行中',
                    null,
                    { ...metaDefaults, silent: effectiveSilent }
                );
            }
        });

        return true;
    }

    handleServiceStatusesMessage(payload) {
        const applied = this.applyServiceStatusesPayload(payload, {
            source: payload?.source || 'websocket',
            silent: payload?.cached ? true : undefined,
        });
        if (payload?.success === false && payload?.error && !payload?.cached) {
            console.warn('Service status request failed via websocket:', payload.error);
        }
        return applied;
    }

    async loadServiceStatuses(options = {}) {
        const controller = this.serviceRestartController;
        if (!controller || !controller.items) {
            return null;
        }

        const { silent = false, force = false, preferSocket = true, minInterval } = options || {};
        const socketAvailable = preferSocket !== false
            && typeof this.ibkrManager?.requestServiceStatuses === 'function';
        let attemptedSocket = false;
        if (socketAvailable) {
            const result = this.ibkrManager.requestServiceStatuses({ force, minInterval });
            if (result !== 'failed') {
                return result;
            }
            attemptedSocket = true;
        }

        let payload;
        try {
            payload = await this.apiCall('/services/status', { method: 'GET', useCache: false });
        } catch (error) {
            if (!silent) {
                const message = error?.message || '无法加载服务状态';
                this.showAlert('danger', message);
            }
            throw error;
        }

        const enrichedPayload = {
            type: 'service_statuses',
            success: true,
            source: attemptedSocket ? 'http-fallback' : 'http',
            ...(payload && typeof payload === 'object' ? payload : {}),
        };
        this.applyServiceStatusesPayload(enrichedPayload, { source: enrichedPayload.source });
        return enrichedPayload;
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
        return this.callMonitoringManager('shouldResumeMonitorRealtime', symbol);
    }

    async subscribeMonitorRealtime(symbol, options = {}) {
        return this.callMonitoringManager('subscribeMonitorRealtime', symbol, options);
    }

    unsubscribeMonitorRealtime(options = {}) {
        return this.callMonitoringManager('unsubscribeMonitorRealtime', options);
    }

    async exitMonitorFullscreen(options = {}) {
        return this.callMonitoringManager('exitMonitorFullscreen', options);
    }

    async closePosition(symbol, options = {}) {
        return this.orderManager.closePosition(symbol, options);
    }

    async openMonitorModal(...args) {
        return this.callMonitoringManager('openMonitorModal', ...args);
    }



    async syncMonitorModalData(...args) {
        return this.callMonitoringManager('syncMonitorModalData', ...args);
    }



    initOrdersDebounce() {
        return this.orderManager.initOrdersDebounce?.();
    }

    initMonitorOrdersDebounce() {
        return this.orderManager.initMonitorOrdersDebounce?.();
    }

    async loadMonitorOrdersList(symbol, status = 'working') {
        return this.callMonitoringManager('loadMonitorOrdersList', symbol, status);
    }



    async openOrderDetails(orderId) {
        try {
            // 验证订单ID
            if (!orderId || orderId.trim() === '') {
                this.showAlert('warning', 'Order ID cannot be empty');
                return;
            }
            
            // 获取订单详情
            let result = await this.apiCall(`/orders/${orderId}`);
            
            if (!result || !result.order) {
                this.showAlert('warning', `Order ${orderId} does not exist or has been deleted`);
                return;
            }
            
            const orderData = result.order;
            
            // 填充模态框内容
            const modalBody = document.getElementById('orderDetailsModalBody');
            if (!modalBody) {
                this.showAlert('danger', 'Order details modal not found');
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
                        return 'Strategy';
                    case 'risk_rule':
                        return 'Risk Control';
                    case 'manual':
                        return 'Manual';
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
                            <div class="card-header"><strong>Basic Information</strong></div>
                            <div class="card-body">
                                <p><strong>Order ID:</strong> ${orderData.order_id}</p>
                                <p><strong>Symbol:</strong> ${orderData.symbol}</p>
                                <p><strong>Action:</strong> ${orderData.action}</p>
                                <p><strong>Quantity:</strong> ${orderData.quantity}</p>
                                <p><strong>Order Type:</strong> ${orderData.order_type}</p>
                                <p><strong>Status:</strong> <span class="badge bg-${this.getStatusColor(orderData.status)}">${orderData.status}</span></p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-6">
                        <div class="card">
                            <div class="card-header"><strong>Price Information</strong></div>
                            <div class="card-body">
                                <p><strong>Limit Price:</strong> ${formatPrice(orderData.limit_price)}</p>
                                <p><strong>Average Fill Price:</strong> ${formatPrice(orderData.avg_fill_price)}</p>
                                <p><strong>Filled:</strong> ${orderData.filled || 0}</p>
                                <p><strong>Remaining:</strong> ${orderData.remaining || 0}</p>
                                <p><strong>Commission:</strong> ${formatPrice(orderData.commission)}</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-12">
                        <div class="card">
                            <div class="card-header"><strong>Additional Information</strong></div>
                            <div class="card-body">
                                <div class="row row-cols-1 row-cols-md-2 g-2">
                                    <div class="col">
                                        <p><strong>Submitted At:</strong> ${formatDateTime(orderData.submit_time)}</p>
                                        <p><strong>Updated At:</strong> ${formatDateTime(orderData.updated_at)}</p>
                                        <p><strong>Executed At:</strong> ${formatDateTime(orderData.executed_at)}</p>
                                        <p><strong>Order Source:</strong> ${sourceHtml}</p>
                                        <p><strong>Trigger Type:</strong> ${formatTriggerType(orderData.trigger_type)}</p>
                                    </div>
                                    <div class="col">
                                        <p><strong>Strategy:</strong> ${strategyHtml}</p>
                                        <p><strong>Rule ID:</strong> ${formatText(orderData.rule_id)}</p>
                                        <p><strong>Parent Order ID:</strong> ${formatText(orderData.parent_order_id)}</p>
                                        <p><strong>Exchange:</strong> ${formatText(orderData.exchange)}</p>
                                        <p><strong>Security Type:</strong> ${formatText(orderData.sec_type)}</p>
                                    </div>
                                </div>
                                <p class="mt-3"><strong>Notes:</strong> ${notesHtml}</p>
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
            let errorMessage = 'Failed to get order details';
            if (error.message) {
                if (error.message.includes('404') || error.message.includes('not found')) {
                    errorMessage = `Order ${orderId} does not exist`;
                } else if (error.message.includes('422') || error.message.includes('Unprocessable Entity')) {
                    errorMessage = `Invalid order ID format: ${orderId}`;
                } else if (error.message.includes('401') || error.message.includes('Authentication')) {
                    errorMessage = 'Please log in first';
                } else {
                    errorMessage = `Failed to get order details: ${error.message}`;
                }
            }
            
            this.showAlert('danger', errorMessage);
        }
    }

    getMonitorTimezone() {
        return this.callMonitoringManager('getMonitorTimezone');
    }

    parseMonitorChartTime(raw, options = {}) {
        return this.callMonitoringManager('parseMonitorChartTime', raw, options);
    }

    async loadMonitorChart(...args) {
        return this.callMonitoringManager('loadMonitorChart', ...args);
    }

    displayChart(...args) {
        return this.callMonitoringManager('displayChart', ...args);
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
            statusEl.textContent = 'Loading risk parameters...';
        } else if (state === 'saving') {
            statusEl.textContent = 'Saving risk settings...';
        } else if (state === 'error') {
            statusEl.textContent = message || 'Save failed, please try again later.';
            statusEl.classList.add('text-danger');
        } else if (state === 'reset') {
            statusEl.textContent = 'Reverted to the most recently saved settings.';
        } else if (state === 'loaded') {
            statusEl.textContent = 'Risk settings loaded.';
        } else if (state === 'saved') {
            statusEl.textContent = 'Risk settings saved.';
        } else {
            statusEl.textContent = message || 'Risk settings';
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
            this.updateRiskSettingsStatus('error', 'Unable to load risk settings.');
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
                    this.showAlert('danger', 'Position limit must be a non-negative integer');
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
                    this.showAlert('danger', 'Floating loss duration must be a non-negative integer');
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
                    this.showAlert('danger', 'Invalid format for floating loss threshold');
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
            this.showAlert('success', 'Risk settings saved');
        } catch (error) {
            console.error('Failed to save strategy risk settings', error);
            this.updateRiskSettingsStatus('error', error?.message);
            this.showAlert('danger', 'Failed to save risk settings, please try again later.');
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
        if (!this.strategiesManager?.handleSummaryDataFeedChange) return;
        return this.strategiesManager.handleSummaryDataFeedChange();
    }

    async handleSummaryPushToggle() {
        if (!this.strategiesManager?.handleSummaryPushToggle) return;
        return this.strategiesManager.handleSummaryPushToggle();
    }

    async setStrategyDataPush({ enabled, subscription, symbol } = {}) {
        if (!this.strategiesManager?.setStrategyDataPush) return null;
        return this.strategiesManager.setStrategyDataPush({ enabled, subscription, symbol });
    }

    async loadStrategyStatus(name, { force = false } = {}) {
        if (!this.strategiesManager?.loadStrategyStatus) return null;
        return this.strategiesManager.loadStrategyStatus(name, { force });
    }

    // cancelAllOrders和syncLimitOrder方法已迁移到OrderManager

    async removeRiskRule(symbol) { return this.riskRulesManager.removeRiskRule(symbol); }

    async toggleRiskRule(symbol, enabled) { return this.riskRulesManager.toggleRiskRule(symbol, enabled); }



    formatNumber(num) {
        return sharedFormatNumber(num);
    }

    escapeHtml(value) {
        return sharedEscapeHtml(value);
    }

    formatDuration(seconds) {
        return sharedFormatDuration(seconds);
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
        return this.callMonitoringManager('createDefaultStrategyChartState');
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
        return this.callMonitoringManager('getStrategyChartState');
    }

    getStrategyChartRefreshIntervalMs(intervalValue) {
        return this.callMonitoringManager('getStrategyChartRefreshIntervalMs', intervalValue);
    }

    getStrategyChartTimeUnit(intervalValue) {
        return this.callMonitoringManager('getStrategyChartTimeUnit', intervalValue);
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
                    this.updateStrategyChartStatus('Missing underlying symbol');
                    return;
                }

                const detailsState = this.strategyDetailsState || {};
                const tabLoaded = detailsState.tabLoaded?.['strategy-chart'];

                if (!tabLoaded) {
                    if (chartState.loading) {
                        this.updateStrategyChartStatus('Loading...');
                    }
                    return;
                }

                if (chartState.loading) {
                    this.updateStrategyChartStatus('Loading...');
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
        return this.callMonitoringManager('refreshStrategyChartDuration', intervalValue, options);
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
                container.innerHTML = '<div class="text-center text-muted py-5">The candlestick chart will appear after loading a strategy.</div>';
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
            container.innerHTML = '<div class="alert alert-warning mb-0">No candlestick data available</div>';
            this.updateStrategyChartStatus(options.extraStatus || 'No candlestick data available');
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
            extraParts.push(`Filled ${visibleOrders.length} orders`);
        }
        if (options.extraStatus) {
            extraParts.push(options.extraStatus);
        }

        const useCandles = state.useCandlestick !== false && this.candlestickSupported;
        const desiredType = useCandles ? 'candlestick' : 'line';
        if (!useCandles) {
            extraParts.push('Line chart');
        }
        const lineSeries = ohlc.map((bar) => ({ x: bar.x, y: bar.c }));
        if (this.strategyChartInstance && this.strategyChartInstance.config?.type !== desiredType) {
            this.destroyStrategyChart();
        }

        let baseDatasetConfigForUpdate;
        let baseDatasetForCreation;

        if (useCandles) {
            const candlestickConfig = this.callMonitoringManager('createCandlestickChartConfig', {
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
                                            return `Buy ${raw.price.toFixed(2)} @ ${when}`;
                                        }
                                        return `Buy ${context.parsed.y}`;
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
                                            return `Sell ${raw.price.toFixed(2)} @ ${when}`;
                                        }
                                        return `Sell ${context.parsed.y}`;
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
                                    label: (context) => `Stop Loss ${this.formatNumber(context.parsed.y)}`,
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
                                    label: (context) => `Take Profit ${this.formatNumber(context.parsed.y)}`,
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
        summaryEl.textContent = parts.length ? parts.join(' · ') : 'Chart not loaded yet';
    }

    updateStrategyChartButtons() {
        const state = this.getStrategyChartState();
        const realtimeBtn = document.getElementById('strategyChartRealtimeBtn');
        if (realtimeBtn) {
            const active = state.isRealtime;
            realtimeBtn.classList.toggle('btn-success', active);
            realtimeBtn.classList.toggle('btn-outline-success', !active);
            realtimeBtn.innerHTML = active ? '<i class="fas fa-pause"></i> Stop' : '<i class="fas fa-play"></i> Live';
            realtimeBtn.disabled = !active && state.loading;
        }
        const replayBtn = document.getElementById('strategyChartReplayBtn');
        if (replayBtn) {
            const playing = state.isReplaying;
            replayBtn.classList.toggle('btn-primary', playing);
            replayBtn.classList.toggle('btn-outline-secondary', !playing);
            replayBtn.innerHTML = playing ? '<i class="fas fa-stop"></i> Stop Replay' : '<i class="fas fa-history"></i> Replay';
            replayBtn.disabled = !playing && state.loading;
        }
    }

    updateStrategyChartStatus(extraText = '') {
        const statusEl = document.getElementById('strategyChartStatus');
        if (!statusEl) return;
        const state = this.getStrategyChartState();
        if (state.loading) {
            statusEl.textContent = 'Loading...';
            return;
        }
        if (!state.fullBars || !state.fullBars.length) {
            statusEl.textContent = extraText || '';
            return;
        }
        const total = state.fullBars.length;
        const visible = Math.max(1, Math.min(state.visibleCount || total, total));
        let text = `Showing ${visible}/${total} candlesticks`;
        if (extraText) {
            text += ` · ${extraText}`;
        }
        if (state.isRealtime) {
            text = `Live update · ${text}`;
        } else if (state.isReplaying) {
            text = `Replaying · ${text}`;
        }
        statusEl.textContent = text;
    }

    async loadStrategyChart(name, options = {}) {
        const state = this.getStrategyChartState();
        if (!name) return;
        state.currentStrategy = name;
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
                container.innerHTML = '<div class="alert alert-warning mb-0">Strategy has no symbol configured, unable to load chart.</div>';
            }
            state.fullBars = [];
            state.visibleCount = 0;
            this.updateStrategyChartButtons();
            this.updateStrategyChartStatus('Missing underlying symbol');
            return;
        }

        const loadToken = Symbol('strategy-chart-load');
        state.loadToken = loadToken;
        state.loading = true;
        this.updateStrategyChartButtons();
        this.updateStrategyChartStatus('Loading...');
        console.log('[StrategyChart] 开始加载策略K线数据', {
            name,
            symbol: state.symbol,
            interval: state.interval,
            duration: state.duration,
            options
        });
        if (container && !this.strategyChartInstance) {
            container.innerHTML = '<div class="text-center text-muted py-5"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">Loading chart data...</p></div>';
        }

        const interval = state.interval || '5 mins';
        const duration = state.duration || '1 D';
        const useCache = options.useCache !== false;
        const forceRefresh = options.forceRefresh === true;
        const forceExecutions = options.forceExecutions === true;

        try {
            const dateAdapterReady = this.callMonitoringManager('isDateAdapterReady');
            if (!dateAdapterReady) {
                console.warn('[StrategyChart] Date adapter not detected; time axes may not format correctly.');
            }

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
                    container.innerHTML = '<div class="alert alert-warning mb-0">No candlestick data available</div>';
                }
                state.fullBars = [];
                state.visibleCount = 0;
                this.updateStrategyChartStatus('No candlestick data available');
                return;
            }

            const pluginReady = this.callMonitoringManager('registerCandlestickControllers');
            console.log('[StrategyChart] 蜡烛图插件状态', { pluginReady });
            state.useCandlestick = Boolean(pluginReady);
            if (!pluginReady) {
                if (!this.strategyChartFallbackNotified) {
                    console.warn('[StrategyChart] Candlestick plugin unavailable, falling back to line chart.');
                    this.strategyChartFallbackNotified = true;
                }
            } else {
                this.strategyChartFallbackNotified = false;
            }

            const bars = this.normalizeStrategyChartBars(response.data);
            console.log('[StrategyChart] 归一化后的K线数量', { count: bars.length });
            if (!bars.length) {
                this.destroyStrategyChart();
                if (container) {
                    container.innerHTML = '<div class="alert alert-warning mb-0">No valid candlestick data</div>';
                }
                state.fullBars = [];
                state.visibleCount = 0;
                this.updateStrategyChartStatus('No valid candlestick data');
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
                const message = error?.message || 'Failed to load strategy chart';
                container.innerHTML = `<div class="alert alert-danger mb-0">${message}</div>`;
            }
            state.fullBars = [];
            state.visibleCount = 0;
            this.updateStrategyChartStatus('Load failed');
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
            this.updateStrategyChartStatus('Missing symbol, cannot refresh in real-time');
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
            this.updateStrategyChartStatus('No trades today');
            return;
        }

        const firstOrderTime = todaysOrders[0].time;
        const startIndex = state.fullBars.findIndex((bar) => bar.x >= firstOrderTime);
        if (startIndex === -1) {
            this.updateStrategyChartStatus("No candlestick matches today's trades in the current range");
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
            this.renderStrategyChart({ extraStatus: complete ? 'Replay complete' : '' });
        } else if (!silent) {
            this.updateStrategyChartStatus(complete ? 'Replay complete' : '');
        }
        this.updateStrategyChartButtons();
        if (complete && silent) {
            this.updateStrategyChartStatus('Replay complete');
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

    setupStrategyChartState(strategy, name) {
        const initialSymbol = this.resolveStrategySymbol(strategy) || '';
        if (this.strategyDetailsState) {
            this.strategyDetailsState.initialSymbol = initialSymbol;
        }
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
    isSameDay(dateInput, referenceDate) {
        if (this.uiController?.isSameDay) {
            return this.uiController.isSameDay(dateInput, referenceDate);
        }
        return sharedIsSameDay(dateInput, referenceDate);
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
            priceValue.textContent = 'Connecting...';
            priceValue.className = 'fw-bold text-warning';
            return;
        }
        
        try {
            // 显示加载状态
            priceDisplay.style.display = 'block';
            priceValue.textContent = 'Loading...';
            priceValue.className = 'fw-bold text-muted';
            
            // 使用缓存的当前价格接口
            const currentPrice = await this.dataManager.getCachedPrice(symbol);
            
            if (currentPrice && currentPrice > 0) {
                priceValue.textContent = `$${this.formatNumber(currentPrice)}`;
                priceValue.className = 'fw-bold text-primary';
                
                // 显示数据源信息
                priceValue.title = `Data source: cache`;
                
                // 如果是市价单，自动填入价格输入框（即使是disabled状态）
                const orderTypeSelect = document.getElementById('orderType');
                const orderPriceInput = document.getElementById('orderPrice');
                if (orderTypeSelect && orderPriceInput && orderTypeSelect.value === 'MARKET') {
                    orderPriceInput.value = currentPrice.toString();
                    // 触发价格变化事件以更新相关计算
                    orderPriceInput.dispatchEvent(new Event('input'));
                }
            } else {
                priceValue.textContent = 'Failed to fetch';
                priceValue.className = 'fw-bold text-warning';
            }
        } catch (error) {
            console.error('获取当前价格失败:', error);
            priceValue.textContent = 'Failed to fetch';
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

    setHolidayDataPushState(enabled, options = {}) {
        const toggle = document.getElementById('holidayDataPushSwitch');
        const statusText = document.getElementById('holidayDataPushStatusText');
        const pending = Boolean(options.pending);
        const effective = enabled !== false;

        if (toggle) {
            toggle.checked = effective;
            toggle.dataset.currentState = effective ? 'on' : 'off';
            toggle.disabled = pending || this.isReadOnly;
        }

        if (statusText) {
            statusText.classList.remove('text-success', 'text-warning', 'text-muted');
            if (pending) {
                statusText.textContent = 'Saving…';
                statusText.classList.add('text-muted');
            } else if (effective) {
                statusText.textContent = 'Current status: Enabled. Data pushes pause automatically on US market holidays.';
                statusText.classList.add('text-success');
            } else {
                statusText.textContent = 'Current status: Disabled. Data pushes continue during holidays.';
                statusText.classList.add('text-warning');
            }
        }
    }

    async refreshHolidayDataPushSetting(options = {}) {
        const toggle = document.getElementById('holidayDataPushSwitch');
        if (!toggle) {
            return;
        }

        const resolveState = () => {
            const stored = toggle.dataset.currentState;
            if (stored === 'on') return true;
            if (stored === 'off') return false;
            return !!toggle.checked;
        };

        if (options.pending) {
            this.setHolidayDataPushState(resolveState(), { pending: true });
        }

        if (!this.isAuthenticated()) {
            if (options.pending) {
                this.setHolidayDataPushState(resolveState());
            }
            return;
        }

        try {
            const data = await this.apiCall('/strategy/settings');
            const settings = data?.settings || {};
            const enabled = settings.pause_data_push_on_holidays;
            this.setHolidayDataPushState(enabled !== false);
        } catch (error) {
            console.error('Failed to load holiday data push setting:', error);
            if (options.pending) {
                this.setHolidayDataPushState(resolveState());
            }
        }
    }

    initConfigCenter() {
        if (this.configCenterManager) {
            return;
        }

        const configCenterModules = getConfigCenterModules();
        if (!configCenterModules?.createManager) {
            if (!this.configCenterInitRetry) {
                let timerId = null;
                const attemptInit = () => {
                    if (timerId) {
                        clearTimeout(timerId);
                        timerId = null;
                    }
                    this.configCenterInitRetry = null;
                    this.initConfigCenter();
                };

                if (typeof globalScope.addEventListener === 'function') {
                    const handler = () => {
                        globalScope.removeEventListener?.('configCenter:ready', handler);
                        attemptInit();
                    };
                    try {
                        globalScope.addEventListener('configCenter:ready', handler, { once: true });
                    } catch (error) {
                        try {
                            globalScope.addEventListener('configCenter:ready', handler);
                        } catch (err) {
                            console.warn('Failed to listen for config center readiness', err);
                        }
                    }
                }

                timerId = setTimeout(attemptInit, 250);
                this.configCenterInitRetry = timerId;
            }
            return;
        }

        if (this.configCenterInitRetry) {
            clearTimeout(this.configCenterInitRetry);
            this.configCenterInitRetry = null;
        }

        const manager = configCenterModules.createManager({
            document,
            moduleSelectId: 'configCenterModuleSelect',
            textareaId: 'configCenterTextarea',
            saveButtonId: 'configCenterSaveButton',
            resetButtonId: 'configCenterResetButton',
            reloadButtonId: 'configCenterReloadButton',
            editorId: 'configCenterEditor',
            messageInputId: 'configCenterMessageInput',
            statusTextId: 'configCenterStatusText',
            historyListId: 'configCenterHistoryList',
            historyReloadId: 'configCenterHistoryRefresh',
            loadModules: () => this.apiCall('/config/modules', { allowReadOnly: true }),
            loadConfig: (module) => this.apiCall(`/config/${encodeURIComponent(module)}`, { allowReadOnly: true }),
            saveConfig: (module, payload) => this.apiCall(`/config/${encodeURIComponent(module)}`, {
                method: 'PUT',
                body: JSON.stringify(payload),
            }),
            loadHistory: (module) => this.apiCall(`/config/${encodeURIComponent(module)}/history`, { allowReadOnly: true }),
            rollback: (module, payload) => this.apiCall(`/config/${encodeURIComponent(module)}/rollback`, {
                method: 'POST',
                body: JSON.stringify(payload),
            }),
            onStatus: (level, message) => {
                if (!message) return;
                const tone = level === 'success' ? 'success' : level === 'error' ? 'danger' : 'info';
                this.showAlert(tone, message);
            },
        });

        if (manager) {
            this.configCenterManager = manager;
            if (this.isReadOnly) {
                manager.setReadOnly(true);
            }
        }
    }

    initHolidayDataPushToggle() {
        const toggle = document.getElementById('holidayDataPushSwitch');
        if (!toggle || toggle.dataset.initialized === 'true') {
            return;
        }
        toggle.dataset.initialized = 'true';

        const resolveState = () => {
            const stored = toggle.dataset.currentState;
            if (stored === 'on') return true;
            if (stored === 'off') return false;
            return !!toggle.checked;
        };

        this.setHolidayDataPushState(resolveState());

        toggle.addEventListener('change', async (event) => {
            if (this.isReadOnly) {
                event.preventDefault();
                this.setHolidayDataPushState(resolveState());
                return;
            }

            if (!this.isAuthenticated()) {
                event.preventDefault();
                this.setHolidayDataPushState(resolveState());
                this.showAlert('warning', 'Please log in before changing holiday push settings');
                return;
            }

            const desired = !!event.target.checked;
            const previous = resolveState();
            if (desired === previous) {
                this.setHolidayDataPushState(desired);
                return;
            }

            this.setHolidayDataPushState(desired, { pending: true });
            const card = document.getElementById('dataPushSettingsCard');

            try {
                await this.apiCall('/strategy/settings', {
                    method: 'POST',
                    body: JSON.stringify({ pause_data_push_on_holidays: desired }),
                });
                this.setHolidayDataPushState(desired);
                if (card) {
                    card.classList.add('panel-focus-highlight');
                    setTimeout(() => card.classList.remove('panel-focus-highlight'), 1400);
                }
                this.showAlert(
                    'success',
                    desired
                        ? 'Holiday data pause enabled'
                        : 'Holiday data pause disabled'
                );
            } catch (error) {
                console.error('Failed to update holiday data push setting:', error);
                this.showAlert('danger', 'Failed to save holiday push settings');
                this.setHolidayDataPushState(previous);
            }
        });
    }

    applySuggestedQuantity() {
        if (this.lastSuggestedQuantity) {
            const quantityInput = document.getElementById('orderQuantity');
            if (quantityInput) {
                quantityInput.value = this.lastSuggestedQuantity;
                this.showAlert('success', 'Suggested quantity applied');
            }
        } else {
            this.showAlert('warning', 'No suggested quantity available');
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
        if (!this.riskRulesManager?.showConfigModal) {
            console.warn('RiskRulesManager.showConfigModal unavailable');
            return undefined;
        }
        return this.riskRulesManager.showConfigModal(activeTab);
    }

    async saveConfigFromModal() {
        if (!this.riskRulesManager?.saveConfigRules) {
            console.warn('RiskRulesManager.saveConfigRules unavailable');
            return undefined;
        }
        return this.riskRulesManager.saveConfigRules();
    }

    resetConfigFromModal() {
        if (!this.riskRulesManager?.resetConfigRules) {
            console.warn('RiskRulesManager.resetConfigRules unavailable');
            return undefined;
        }
        return this.riskRulesManager.resetConfigRules();
    }

    updatePositionValueField() {
        return this.orderManager.updatePositionValueField();
    }

    async loadConfigData() {
        if (!this.riskRulesManager?.loadConfigData) {
            console.warn('RiskRulesManager.loadConfigData unavailable');
            return null;
        }
        return this.riskRulesManager.loadConfigData();
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
