(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const monitoring = appModules.monitoring = appModules.monitoring || {};
    const monitoringConstants = monitoring.constants || {};
    const monitoringUtils = monitoring.utils || {};

    const {
        MONITOR_DURATION_OPTIONS = {},
        INTERVAL_MINUTES_MAP = {},
        MONITOR_EVENT_SIDE_COLORS = {
            buy: '#22c55e',
            sell: '#ef4444',
            neutral: '#64748b'
        },
        MONITOR_CHART_MAX_MINUTES = 30,
        MONITOR_CHART_MAX_POINTS = 3000,
        MONITOR_SOURCE_TZ = 'America/New_York',
        MONITOR_FALLBACK_TZ = 'Asia/Shanghai'
    } = monitoringConstants;

    const sharedUtils = appModules.utils || {};

    const requireMonitoringUtil = (fn, name) => {
        if (typeof fn !== 'function') {
            throw new Error(`monitoring.utils.${name} is required`);
        }
        return fn;
    };

    const {
        normalizeIntervalKey,
        getMonitorDurationOptions,
        getMonitorDurationMinutes,
        refreshMonitorDurationSelect
    } = monitoringUtils;

    requireMonitoringUtil(normalizeIntervalKey, 'normalizeIntervalKey');
    requireMonitoringUtil(getMonitorDurationOptions, 'getMonitorDurationOptions');
    requireMonitoringUtil(getMonitorDurationMinutes, 'getMonitorDurationMinutes');
    requireMonitoringUtil(refreshMonitorDurationSelect, 'refreshMonitorDurationSelect');

    const {
        formatNumber: sharedFormatNumber,
        escapeHtml: sharedEscapeHtml,
        formatDuration: sharedFormatDuration,
        isSameDay: sharedIsSameDay,
        TrendSignalLabelPlugin: sharedTrendSignalLabelPlugin,
        slugifyStrategyName: sharedSlugifyStrategyName,
    } = sharedUtils;

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
    if (!sharedTrendSignalLabelPlugin || typeof sharedTrendSignalLabelPlugin.afterDatasetsDraw !== 'function') {
        throw new Error('utils.TrendSignalLabelPlugin is required');
    }
    if (typeof sharedSlugifyStrategyName !== 'function') {
        throw new Error('utils.slugifyStrategyName is required');
    }
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
            this.monitorChart = null;
            this.monitorTrendData = [];
            this.monitorTrendDatasetMap = { trend: 0, events: 1, price: 2 };
            this.monitorDomHistoryList = null;
            this.monitorDomHistoryMeta = null;
            this.currentMonitor = null;
            this.chartDataCache = {};
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
            this.pendingMonitorCandlestickUpgradeToken = null;
            this._syncingCharts = false;
            this.monitorModalFullscreen = false;
            this._monitorFullscreenChangeHandler = null;
        }

        ensureApp(methodName = 'method') {
            if (!this.app) {
                throw new Error(`MonitoringManager requires app context for ${methodName}`);
            }
            return this.app;
        }

        callAppMethod(methodName, ...args) {
            const app = this.ensureApp(methodName);
            const fn = app[methodName];
            if (typeof fn !== 'function') {
                throw new Error(`TradingApp.${methodName} is not available`);
            }
            return fn.apply(app, args);
        }

        tryCallAppMethod(methodName, ...args) {
            const app = this.app;
            if (!app) return undefined;
            const fn = app[methodName];
            if (typeof fn !== 'function') {
                return undefined;
            }
            return fn.apply(app, args);
        }

        get dataManager() {
            return this.app?.dataManager || null;
        }

        get domManager() {
            return this.app?.domManager || null;
        }

        get orderManager() {
            return this.app?.orderManager || null;
        }

        get orders() {
            return Array.isArray(this.app?.orders) ? this.app.orders : [];
        }

        get riskRulesManager() {
            return this.app?.riskRulesManager || null;
        }

        formatNumber(value) {
            return sharedFormatNumber(value);
        }

        escapeHtml(value) {
            return sharedEscapeHtml(String(value ?? ''));
        }

        formatDuration(seconds) {
            return sharedFormatDuration(seconds);
        }

        isSameDay(date, reference) {
            return sharedIsSameDay(date, reference);
        }

        getContractMultiplier(symbol) {
            const result = this.tryCallAppMethod('getContractMultiplier', symbol);
            return Number.isFinite(result) ? result : 1;
        }

        loadOrders(...args) {
            return this.callAppMethod('loadOrders', ...args);
        }

        apiCall(...args) {
            return this.callAppMethod('apiCall', ...args);
        }

        showAlert(...args) {
            return this.callAppMethod('showAlert', ...args);
        }

        correctPriceStep(value, symbol) {
            const result = this.tryCallAppMethod('correctPriceStep', value, symbol);
            if (result === undefined || result === null) {
                return value;
            }
            return result;
        }

        closePosition(symbol, options = {}) {
            return this.callAppMethod('closePosition', symbol, options);
        }

        guardChartScriptableDescriptors() {
            if (this.chartScriptableGuardApplied) {
                return;
            }
            const chart = root.Chart || (typeof Chart !== 'undefined' ? Chart : null);
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
            const traverse = (node, depth = 0) => {
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
                    node.forEach((item) => traverse(item, depth + 1));
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
                    traverse(value, depth + 1);
                }
            };

            traverse(defaults);
            this.chartScriptableGuardApplied = true;
        }

        registerCandlestickControllers() {
            this.guardChartScriptableDescriptors();
            try {
                const chart = root.Chart || (typeof Chart !== 'undefined' ? Chart : null);
                if (!chart || typeof chart.register !== 'function') {
                    return false;
                }

                const financialCandidates = [
                    root.ChartFinancial,
                    root['chartjs-chart-financial'],
                    root.chartFinancial,
                    root.chartjsChartFinancial,
                    root.financialChart
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
                        chart.register(...components);
                        this.candlestickSupported = true;
                        return true;
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

                const existing = Array.from(document.getElementsByTagName('script')).find((script) => {
                    return script.src && script.src.includes('chartjs-chart-financial');
                });

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
            return typeof root.luxon !== 'undefined'
                && root.luxon
                && typeof root.luxon.DateTime === 'function';
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
            const dynamicSelector = 'script[data-dynamic="luxon"]';
            const existingDynamic = typeof document !== 'undefined'
                ? document.querySelector(dynamicSelector)
                : null;

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
            } else if (typeof document !== 'undefined') {
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
            const chart = root.Chart || (typeof Chart !== 'undefined' ? Chart : null);
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
                    return await this.dateAdapterLoader;
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
            const dynamicSelector = 'script[data-dynamic="chartjs-adapter-luxon"]';
            const existingDynamic = typeof document !== 'undefined'
                ? document.querySelector(dynamicSelector)
                : null;

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
            } else if (typeof document !== 'undefined') {
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

        updateExpectedPnL(...args) {
            return this.tryCallAppMethod('updateExpectedPnL', ...args);
        }

        showActiveRiskRule(rule) {
            if (this.riskRulesManager?.showActiveRiskRule) {
                return this.riskRulesManager.showActiveRiskRule(rule);
            }
            return undefined;
        }

        hideActiveRiskRule() {
            if (this.riskRulesManager?.hideActiveRiskRule) {
                return this.riskRulesManager.hideActiveRiskRule();
            }
            return undefined;
        }

        toggleRiskRule(symbol, enabled) {
            if (this.riskRulesManager?.toggleRiskRule) {
                return this.riskRulesManager.toggleRiskRule(symbol, enabled);
            }
            return this.tryCallAppMethod('toggleRiskRule', symbol, enabled);
        }

        updateRiskPrices(symbol, prices) {
            if (this.riskRulesManager?.updateRiskPrices) {
                return this.riskRulesManager.updateRiskPrices(symbol, prices);
            }
            return this.tryCallAppMethod('updateRiskPrices', symbol, prices);
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
                'monitorChart',
                'monitorTrendData',
                'monitorTrendDatasetMap',
                'monitorDomHistoryList',
                'monitorDomHistoryMeta',
                'currentMonitor',
                'chartDataCache',
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
                'pendingMonitorCandlestickUpgradeToken',
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

        /**
         * @param {Array} points
         */
        setMonitorTrendHistory(points = []) {
            if (Array.isArray(points)) {
                this.monitorTrendData = points.map(point => ({ ...point }));
            } else {
                this.monitorTrendData = [];
            }
            if (this.monitorTrendChart?.data?.datasets?.length) {
                const baseData = this.getFormattedTrendChartData().filter(item => Number.isFinite(item?.y));
                const trendIndex = this.monitorTrendDatasetMap?.trend ?? 0;
                if (this.monitorTrendChart.data.datasets[trendIndex]) {
                    this.monitorTrendChart.data.datasets[trendIndex].data = baseData;
                }
                this.updateMonitorSignalOverlayDatasets();
                this.monitorTrendChart.update('none');
            }
            this.renderMonitorSignalHistory();
        }

        resetMonitorTrendData() {
            this.setMonitorTrendHistory([]);
        }

        addDomSignal(signal) {
            if (!signal || typeof signal !== 'object') return;
            if (!Array.isArray(this.domSignals)) {
                this.domSignals = [];
            }
            this.domSignals.push(signal);
            if (this.domSignals.length > 1000) {
                this.domSignals.shift();
            }
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
            if (!this.monitorChart || this.monitorChart.config?.type !== 'candlestick') return;
            const annotations = this.ensureMonitorAnnotations();
            if (!annotations) return;

            Object.keys(annotations)
                .filter((key) => key.startsWith('domSignal'))
                .forEach((key) => delete annotations[key]);

            const sizeMap = { 1: 12, 2: 16, 3: 20, 4: 24 };
            const signals = Array.isArray(this.domSignals) ? this.domSignals : [];
            signals.forEach((sig, idx) => {
                if (!sig) return;
                const content = sig.score > 0 ? '↑' : '↓';
                const color = sig.score > 0 ? 'green' : 'red';
                annotations[`domSignal${idx}`] = {
                    type: 'label',
                    xValue: sig.time,
                    yValue: sig.price,
                    backgroundColor: 'rgba(0,0,0,0)',
                    color,
                    font: { size: sizeMap[sig.level] || 12, weight: 'bold' },
                    content,
                    yAdjust: sig.score > 0 ? 10 : -10,
                };
            });

            this.monitorChart.update('none');
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

                    if (typeof raw === 'number' && !Number.isNaN(raw)) {
                        const value = raw > 1e10 ? raw : raw * 1000;
                        dt = DateTime.fromMillis(value, { zone: targetZone || MONITOR_SOURCE_TZ });
                    } else if (typeof raw === 'string') {
                        const trimmed = raw.trim();
                        if (!trimmed) {
                            return buildFallbackDate();
                        }

                        const numeric = Number(trimmed);
                        if (!Number.isNaN(numeric)) {
                            const value = numeric > 1e10 ? numeric : numeric * 1000;
                            dt = DateTime.fromMillis(value, { zone: targetZone || MONITOR_SOURCE_TZ });
                        } else {
                            const isoParsed = DateTime.fromISO(trimmed, { zone: targetZone || MONITOR_SOURCE_TZ });
                            if (isoParsed.isValid) {
                                dt = isoParsed;
                            } else {
                                const knownFormats = [
                                    'yyyy-MM-dd HH:mm:ss',
                                    'yyyy/MM/dd HH:mm:ss',
                                    'yyyy-MM-dd HH:mm',
                                    'yyyy/MM/dd HH:mm',
                                    "yyyy-MM-dd'T'HH:mm:ss",
                                    "yyyy-MM-dd'T'HH:mm:ssZZ",
                                    "yyyy-MM-dd'T'HH:mmZZ",
                                    'yyyy-MM-dd HH:mm:ss.SSS',
                                    'yyyy-MM-dd HH:mm:ss.SSSZZ',
                                    'yyyy-MM-dd HH:mm:ssZZ',
                                    'yyyy/MM/dd HH:mm:ssZZ',
                                    'MM/dd/yyyy HH:mm:ss',
                                    'MM/dd/yyyy HH:mm',
                                    'MM-dd-yyyy HH:mm:ss',
                                    'MM-dd-yyyy HH:mm',
                                ];
                                for (const fmt of knownFormats) {
                                    const parsed = DateTime.fromFormat(trimmed, fmt, { zone: targetZone || MONITOR_SOURCE_TZ });
                                    if (parsed.isValid) {
                                        dt = parsed;
                                        break;
                                    }
                                }
                            }
                        }
                    }

                    if (dt && dt.isValid) {
                        if (skipDstAdjustment) {
                            return dt.toJSDate();
                        }
                        try {
                            const utcMillis = dt.toUTC().toMillis();
                            const resolved = DateTime.fromMillis(utcMillis, { zone: targetZone || MONITOR_SOURCE_TZ });
                            if (resolved.isValid) {
                                return resolved.toJSDate();
                            }
                        } catch (resolveErr) {
                            console.warn('Failed to adjust timezone with Luxon, using original value.', resolveErr);
                        }
                        return dt.toJSDate();
                    }
                }
            } catch (err) {
                console.warn('Luxon parse failed, falling back to native Date.', err);
            }

            return buildFallbackDate();
        }

        formatTrendPointForChart(point) {
            if (!point || typeof point !== 'object') return null;
            const rawTime = point.timestamp ?? point.x ?? point.time ?? point.date;
            const normalized = this.normalizeMonitorTime(rawTime);
            if (normalized == null) return null;

            const safeNumber = (value) => {
                if (value === null || value === undefined || value === '') return null;
                if (typeof value === 'number') return Number.isFinite(value) ? value : null;
                const parsed = Number(value);
                return Number.isFinite(parsed) ? parsed : null;
            };

            const trendScore = safeNumber(point.score ?? point.trend_score ?? point.y);
            const obi = safeNumber(point.obi);
            const ofi = safeNumber(point.ofi);
            const thresholds = point.thresholds && typeof point.thresholds === 'object' ? point.thresholds : {};
            const longCfg = thresholds.long && typeof thresholds.long === 'object' ? thresholds.long : {};
            const shortCfg = thresholds.short && typeof thresholds.short === 'object' ? thresholds.short : {};
            const longObi = safeNumber(longCfg.obi ?? longCfg.obi5);
            const shortObi = safeNumber(shortCfg.obi ?? shortCfg.obi5);
            const longOfi = safeNumber(longCfg.ofi);
            const shortOfi = safeNumber(shortCfg.ofi);

            let signal = safeNumber(point.signal);
            if (!Number.isFinite(signal)) {
                if (Number.isFinite(obi) && Number.isFinite(ofi)) {
                    if (Number.isFinite(longObi) && Number.isFinite(longOfi) && obi >= longObi && ofi >= longOfi) {
                        signal = 1;
                    } else if (Number.isFinite(shortObi) && Number.isFinite(shortOfi) && obi <= shortObi && ofi <= shortOfi) {
                        signal = -1;
                    }
                }
            }
            if (!Number.isFinite(signal)) {
                signal = 0;
            }
            signal = Math.max(-1, Math.min(1, Number.isFinite(signal) ? signal : 0));

            const price = safeNumber(point.price);
            const suggestion = typeof point.suggestion === 'string' ? point.suggestion.toLowerCase() : null;
            const normalizedSuggestion = suggestion === 'buy' ? 'buy' : suggestion === 'sell' ? 'sell' : null;
            const events = Array.isArray(point.events) ? point.events.map(evt => ({ ...evt })) : [];
            const featureFlagSource = (point.featureFlags && typeof point.featureFlags === 'object')
                ? point.featureFlags
                : (point.feature_flags && typeof point.feature_flags === 'object' ? point.feature_flags : null);
            const featureFlags = featureFlagSource ? { ...featureFlagSource } : {};
            const fakeBreakoutProb = safeNumber(point.fake_breakout_prob ?? point.fakeBreakoutProb);

            return {
                ...point,
                timestamp: normalized,
                x: normalized,
                displayTime: new Date(normalized),
                score: trendScore,
                trend_score: trendScore,
                signal,
                y: Number.isFinite(trendScore) ? trendScore : null,
                obi,
                ofi,
                price,
                suggestion: normalizedSuggestion ?? point.suggestion ?? null,
                thresholds: {
                    long: { obi: longObi, ofi: longOfi },
                    short: { obi: shortObi, ofi: shortOfi },
                },
                events,
                featureFlags,
                feature_flags: featureFlags,
                fake_breakout_prob: fakeBreakoutProb,
            };
        }

        getFormattedTrendChartData() {
            if (!Array.isArray(this.monitorTrendData)) return [];
            return this.monitorTrendData
                .map(point => this.formatTrendPointForChart(point))
                .filter(point => point !== null);
        }

        updateMonitorSignalOverlayDatasets() {
            if (!this.monitorTrendChart) return;
            const chart = this.monitorTrendChart;
            const map = this.monitorTrendDatasetMap || { trend: 0, events: 1, price: 2 };
            const baseData = this.getFormattedTrendChartData();

            const resolveEventColor = (event) => {
                if (!event || typeof event !== 'object') return MONITOR_EVENT_SIDE_COLORS.neutral;
                if (typeof event.color === 'string' && event.color.trim()) {
                    return event.color.trim();
                }
                const side = typeof event.side === 'string' ? event.side.toLowerCase() : '';
                if (side === 'buy') return MONITOR_EVENT_SIDE_COLORS.buy;
                if (side === 'sell') return MONITOR_EVENT_SIDE_COLORS.sell;
                return MONITOR_EVENT_SIDE_COLORS.neutral;
            };

            const trendDataset = chart.data?.datasets?.[map.trend];
            if (trendDataset) {
                const entries = baseData
                    .filter(point => Number.isFinite(point.score))
                    .map(point => ({
                        x: point.x,
                        y: point.score,
                        score: point.score,
                        signal: point.signal,
                        suggestion: point.suggestion,
                        reason: point.reason,
                        price: point.price,
                        obi: point.obi,
                        ofi: point.ofi,
                        thresholds: point.thresholds,
                        events: Array.isArray(point.events) ? point.events : [],
                        level: point.level,
                        fake_breakout_prob: point.fake_breakout_prob,
                        featureFlags: point.featureFlags,
                        feature_flags: point.feature_flags ?? point.featureFlags,
                    }));
                trendDataset.data = entries;
            }

            const eventDataset = chart.data?.datasets?.[map.events];
            if (eventDataset) {
                const entries = [];
                baseData.forEach((point) => {
                    if (!Number.isFinite(point.score)) return;
                    const events = Array.isArray(point.events) ? point.events : [];
                    events.forEach((event) => {
                        if (!event || typeof event !== 'object') return;
                        const labelSource = event.label || event.name || event.description || event.code || event.key || '';
                        const labelText = labelSource ? String(labelSource).trim() : '';
                        const rawCode = event.code || event.key || labelText;
                        const code = rawCode ? String(rawCode).trim().toUpperCase().slice(0, 2) : '';
                        if (!code) return;
                        const side = typeof event.side === 'string' ? event.side : null;
                        entries.push({
                            x: point.x,
                            y: point.score,
                            code,
                            label: labelText || code,
                            side,
                            color: resolveEventColor(event),
                            price: point.price,
                            score: point.score,
                            thresholds: point.thresholds,
                            reason: point.reason,
                        });
                    });
                });
                eventDataset.data = entries;
            }

            const priceDataset = chart.data?.datasets?.[map.price];
            if (priceDataset) {
                priceDataset.data = baseData
                    .map(point => (Number.isFinite(point.price) ? { x: point.x, y: point.price } : null))
                    .filter(Boolean);
            }

            const trendValues = Array.isArray(trendDataset?.data)
                ? trendDataset.data.map(item => (item && Number.isFinite(item.y) ? item.y : null)).filter(Number.isFinite)
                : [];
            const trendAxis = chart.options?.scales?.trendAxis;
            if (trendAxis) {
                if (trendValues.length) {
                    const min = Math.min(...trendValues);
                    const max = Math.max(...trendValues);
                    const span = max - min || 1;
                    const padding = span * 0.1;
                    trendAxis.min = min - padding;
                    trendAxis.max = max + padding;
                } else {
                    trendAxis.min = -1;
                    trendAxis.max = 1;
                }
            }

            const priceAxis = chart.options?.scales?.priceAxis;
            if (priceAxis) {
                const priceValues = Array.isArray(priceDataset?.data)
                    ? priceDataset.data.map(item => (item && Number.isFinite(item.y) ? item.y : null)).filter(Number.isFinite)
                    : [];
                if (priceValues.length) {
                    const min = Math.min(...priceValues);
                    const max = Math.max(...priceValues);
                    const span = max - min || (Math.abs(min) * 0.05) || 1;
                    priceAxis.min = min - span * 0.05;
                    priceAxis.max = max + span * 0.05;
                    priceAxis.display = true;
                } else {
                    priceAxis.display = false;
                }
            }
        }

        renderMonitorSignalHistory() {
            if (!this.monitorDomHistoryList) {
                this.monitorDomHistoryList = document.getElementById('monitorDomHistoryList');
            }
            if (!this.monitorDomHistoryList) return;
            if (!this.monitorDomHistoryMeta) {
                this.monitorDomHistoryMeta = document.getElementById('monitorDomHistoryMeta');
            }
            const metaEl = this.monitorDomHistoryMeta;
            const formatted = this.getFormattedTrendChartData();
            const escapeHtml = (text) => String(text ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
            const now = new Date();
            const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            const startMs = startOfDay.getTime();
            const endMs = startMs + 24 * 60 * 60 * 1000;

            const rawEntries = formatted
                .filter(point => Number.isFinite(point.timestamp)
                    && Math.abs(point.signal) === 1
                    && point.timestamp >= startMs
                    && point.timestamp < endMs)
                .sort((a, b) => a.timestamp - b.timestamp);

            const MERGE_WINDOW_MS = 60 * 1000;
            const mergedGroups = [];
            const appendEventsToGroup = (group, eventList) => {
                if (!Array.isArray(eventList) || !eventList.length) return;
                eventList.forEach((evt) => {
                    if (!evt || typeof evt !== 'object') return;
                    const rawCode = evt.code || evt.key || evt.label || '';
                    const codeText = rawCode ? String(rawCode).trim().toUpperCase() : '';
                    const labelSource = evt.label || evt.name || evt.description || evt.key || rawCode;
                    const labelText = labelSource ? String(labelSource).trim() : '';
                    if (!codeText && !labelText) return;
                    const side = typeof evt.side === 'string' ? evt.side.toLowerCase() : '';
                    const key = `${codeText}||${labelText}||${side}`;
                    if (group.eventKeySet.has(key)) return;
                    group.eventKeySet.add(key);
                    group.events.push({ ...evt });
                });
            };

            rawEntries.forEach((entry) => {
                const reason = entry.reason ? String(entry.reason).split('\n')[0] : '';
                const events = Array.isArray(entry.events) ? entry.events : [];
                const lastGroup = mergedGroups[mergedGroups.length - 1];
                const withinWindow = lastGroup
                    && entry.signal === lastGroup.signal
                    && (entry.timestamp - lastGroup.endTimestamp) <= MERGE_WINDOW_MS;

                if (withinWindow) {
                    lastGroup.endTimestamp = entry.timestamp;
                    lastGroup.count += 1;
                    if (Number.isFinite(entry.price)) lastGroup.price = entry.price;
                    if (Number.isFinite(entry.obi)) lastGroup.obi = entry.obi;
                    if (Number.isFinite(entry.ofi)) lastGroup.ofi = entry.ofi;
                    if (reason) lastGroup.reasonSet.add(reason);
                    appendEventsToGroup(lastGroup, events);
                } else {
                    const group = {
                        signal: entry.signal,
                        startTimestamp: entry.timestamp,
                        endTimestamp: entry.timestamp,
                        price: Number.isFinite(entry.price) ? entry.price : null,
                        obi: Number.isFinite(entry.obi) ? entry.obi : null,
                        ofi: Number.isFinite(entry.ofi) ? entry.ofi : null,
                        reasonSet: new Set(reason ? [reason] : []),
                        events: [],
                        eventKeySet: new Set(),
                        count: 1,
                    };
                    appendEventsToGroup(group, events);
                    mergedGroups.push(group);
                }
            });

            const aggregatedEntries = mergedGroups.map(group => ({
                signal: group.signal,
                startTimestamp: group.startTimestamp,
                endTimestamp: group.endTimestamp,
                price: group.price,
                obi: group.obi,
                ofi: group.ofi,
                reasons: Array.from(group.reasonSet),
                events: group.events.map(evt => ({ ...evt })),
                count: group.count,
            }));

            if (metaEl) {
                metaEl.textContent = `今日信号：${aggregatedEntries.length} 条`;
            }

            if (!aggregatedEntries.length) {
                this.monitorDomHistoryList.innerHTML = '<div class="list-group-item text-center text-muted py-2">暂无信号</div>';
                return;
            }

            const formatNumber = (value) => {
                if (this.app && typeof this.app.formatNumber === 'function') {
                    return this.app.formatNumber(value);
                }
                const numeric = Number(value);
                if (!Number.isFinite(numeric)) return 'N/A';
                return numeric.toFixed(2);
            };

            const html = aggregatedEntries.map((entry) => {
                const startTimeText = new Date(entry.startTimestamp).toLocaleTimeString(undefined, { hour12: false });
                const endTimeText = entry.count > 1 && entry.endTimestamp && entry.endTimestamp !== entry.startTimestamp
                    ? new Date(entry.endTimestamp).toLocaleTimeString(undefined, { hour12: false })
                    : null;
                const timeText = endTimeText ? `${startTimeText} ~ ${endTimeText}` : startTimeText;
                const direction = entry.signal > 0 ? '买入' : '卖出';
                const badgeClass = entry.signal > 0 ? 'bg-success' : 'bg-danger';
                const countBadge = entry.count > 1 ? `<span class="badge bg-secondary me-2">x${entry.count}</span>` : '';
                const priceText = Number.isFinite(entry.price) ? `$${formatNumber(entry.price)}` : '—';
                const obiText = Number.isFinite(entry.obi) ? formatNumber(entry.obi) : '—';
                const ofiText = Number.isFinite(entry.ofi) ? formatNumber(entry.ofi) : '—';
                const reasons = Array.isArray(entry.reasons) ? entry.reasons : [];
                const reasonText = reasons
                    .map(item => escapeHtml(item))
                    .filter(Boolean)
                    .join('；');
                const reasonLine = reasonText
                    ? `<div class="text-muted small">${reasonText}</div>`
                    : '';
                const events = Array.isArray(entry.events) ? entry.events : [];
                const eventBadges = events
                    .map((evt) => {
                        if (!evt || typeof evt !== 'object') return '';
                        const rawCode = evt.code || evt.label || evt.key;
                        const codeText = rawCode ? String(rawCode).trim() : '';
                        if (!codeText) return '';
                        const displayCode = codeText.slice(0, 2).toUpperCase();
                        const labelSource = evt.label || evt.name || evt.description || evt.key || rawCode;
                        const labelText = labelSource ? String(labelSource).trim() : displayCode;
                        const side = typeof evt.side === 'string' ? evt.side.toLowerCase() : '';
                        const colorClass = side === 'buy' ? 'bg-success' : side === 'sell' ? 'bg-danger' : 'bg-secondary';
                        const title = escapeHtml(labelText);
                        const inlineStyle = typeof evt.color === 'string' && evt.color.trim()
                            ? ` style="background-color:${escapeHtml(evt.color.trim())};color:#fff;"`
                            : '';
                        return `<span class="d-inline-flex align-items-center me-2">`
                            + `<span class="badge ${colorClass} me-1"${inlineStyle} title="${title}">${escapeHtml(displayCode)}</span>`
                            + `<span class="ms-1">${escapeHtml(labelText)}</span>`
                            + '</span>';
                    })
                    .filter(Boolean)
                    .join('');
                const eventLine = eventBadges
                    ? `<div class="text-muted small d-flex flex-wrap align-items-center gap-2">${eventBadges}</div>`
                    : '';
                return `
                    <div class="list-group-item d-flex justify-content-between align-items-start">
                        <div>
                            <div class="fw-semibold"><span class="badge ${badgeClass} me-2">${direction}</span>${countBadge}${priceText}</div>
                            <div class="text-muted small">OBI ${obiText} · OFI ${ofiText}</div>
                            ${reasonLine}
                            ${eventLine}
                        </div>
                        <small class="text-muted ms-3">${timeText}</small>
                    </div>
                `;
            }).join('');

            this.monitorDomHistoryList.innerHTML = html;
        }

        getTrendValues(chart) {
            const trendIndex = this.monitorTrendDatasetMap?.trend ?? 0;
            const dataset = chart.data?.datasets?.[trendIndex];
            const arr = Array.isArray(dataset?.data) ? dataset.data : [];
            const ys = arr.map(p => (typeof p === 'number' ? p : p?.y)).filter(Number.isFinite);
            if (!ys.length) return null;
            return { latest: ys[ys.length - 1], min: Math.min(...ys), max: Math.max(...ys) };
        }

        setupTrendLegendAndAnnotations(chart) {
            if (!chart) return;
            const opts = chart.options || (chart.options = {});
            const plugins = opts.plugins || (opts.plugins = {});
            if (!plugins.legend) {
                plugins.legend = {
                    display: true,
                    position: 'top',
                };
            } else {
                plugins.legend.display = true;
                plugins.legend.position = plugins.legend.position || 'top';
            }
        }

        refreshTrend() {
            this.updateMonitorSignalOverlayDatasets();
        }

        syncTrendReferenceLines() {
            this.updateMonitorSignalOverlayDatasets();
        }

        deriveTrendLevel(score) {
            const value = Math.abs(Number(score));
            if (!Number.isFinite(value)) return 0;
            if (value >= 0.95) return 4;
            if (value >= 0.85) return 3;
            if (value >= 0.7) return 2;
            if (value >= 0.5) return 1;
            return 0;
        }

        addMonitorTrendPoint(time, score, options = {}) {
            const safeNumber = (value) => {
                if (value === null || value === undefined || value === '') return null;
                if (typeof value === 'number') return Number.isFinite(value) ? value : null;
                const parsed = Number(value);
                return Number.isFinite(parsed) ? parsed : null;
            };

            const normalizeEvents = (payload) => (Array.isArray(payload) ? payload.map(evt => ({ ...evt })) : []);
            const mergeEvents = (existingEvents, incomingEvents) => {
                const combined = [
                    ...(Array.isArray(existingEvents) ? existingEvents : []),
                    ...(Array.isArray(incomingEvents) ? incomingEvents : []),
                ];
                if (!combined.length) return [];
                const seen = new Set();
                const result = [];
                combined.forEach((evt) => {
                    if (!evt || typeof evt !== 'object') return;
                    const code = typeof evt.code === 'string' ? evt.code : '';
                    const key = `${evt.key || code}-${evt.side || ''}`;
                    if (seen.has(key)) return;
                    seen.add(key);
                    result.push({ ...evt });
                });
                return result;
            };

            const normalizedTime = this.normalizeMonitorTime(time ?? Date.now());
            const x = normalizedTime == null ? Date.now() : normalizedTime;
            const numericScore = safeNumber(score);

            const providedReason = typeof options.reason === 'string' ? options.reason : '';
            const reasonLines = [];
            const providedLineSet = new Set();
            if (providedReason) {
                providedReason.split('\n').forEach((line) => {
                    const trimmed = line.trim();
                    if (trimmed) {
                        providedLineSet.add(trimmed.toLowerCase());
                        reasonLines.push(trimmed);
                    }
                });
            }

            if (Number.isFinite(numericScore)) {
                const trendLine = `Trend Score ${numericScore.toFixed(2)}`;
                if (!providedLineSet.has(trendLine.toLowerCase())) {
                    reasonLines.unshift(trendLine);
                }
            }

            const normalizeSuggestion = (value) => {
                if (typeof value === 'string') {
                    const lower = value.trim().toLowerCase();
                    if (lower === 'buy' || lower === 'sell') return lower;
                }
                return null;
            };

            let suggestion = normalizeSuggestion(options.suggestion);
            if (!suggestion) {
                if (options.longBias === true && options.shortBias !== true) {
                    suggestion = 'buy';
                } else if (options.shortBias === true && options.longBias !== true) {
                    suggestion = 'sell';
                }
            }

            const thresholdValue = typeof options.threshold === 'number' && options.threshold > 0
                ? options.threshold
                : 0.5;
            if (!suggestion && Number.isFinite(numericScore)) {
                if (numericScore >= thresholdValue) suggestion = 'buy';
                else if (numericScore <= -thresholdValue) suggestion = 'sell';
            }

            const obi = safeNumber(options.obi);
            const ofi = safeNumber(options.ofi);
            const thresholds = options.thresholds && typeof options.thresholds === 'object' ? options.thresholds : {};
            const longCfg = thresholds.long && typeof thresholds.long === 'object' ? thresholds.long : {};
            const shortCfg = thresholds.short && typeof thresholds.short === 'object' ? thresholds.short : {};
            const longObi = safeNumber(longCfg.obi ?? longCfg.obi5);
            const shortObi = safeNumber(shortCfg.obi ?? shortCfg.obi5);
            const longOfi = safeNumber(longCfg.ofi);
            const shortOfi = safeNumber(shortCfg.ofi);

            let signal = safeNumber(options.signal);
            if (!Number.isFinite(signal)) {
                if (Number.isFinite(obi) && Number.isFinite(ofi)) {
                    if (Number.isFinite(longObi) && Number.isFinite(longOfi) && obi >= longObi && ofi >= longOfi) {
                        signal = 1;
                    } else if (Number.isFinite(shortObi) && Number.isFinite(shortOfi) && obi <= shortObi && ofi <= shortOfi) {
                        signal = -1;
                    }
                }
            }
            if (!Number.isFinite(signal)) {
                signal = 0;
            }
            signal = Math.max(-1, Math.min(1, Number.isFinite(signal) ? signal : 0));

            if (!suggestion) {
                suggestion = signal > 0 ? 'buy' : signal < 0 ? 'sell' : null;
            }

            const level = typeof options.level === 'number'
                ? options.level
                : this.deriveTrendLevel(numericScore);
            const price = safeNumber(options.price);
            const events = normalizeEvents(options.events);
            const featureFlags = options.featureFlags && typeof options.featureFlags === 'object'
                ? { ...options.featureFlags }
                : {};
            const fakeBreakoutProb = safeNumber(options.fakeBreakoutProb ?? options.fake_breakout_prob);

            if (!Array.isArray(this.monitorTrendData)) {
                this.monitorTrendData = [];
            }

            const reasonText = reasonLines.length ? reasonLines.join('\n') : providedReason || '指标变化有限，保持观察';

            const newPoint = {
                x,
                timestamp: x,
                signal,
                score: numericScore,
                trend_score: numericScore,
                y: Number.isFinite(numericScore) ? numericScore : null,
                reason: reasonText,
                suggestion,
                level,
                price,
                obi,
                ofi,
                thresholds: {
                    long: { obi: longObi, ofi: longOfi },
                    short: { obi: shortObi, ofi: shortOfi },
                },
                events,
                featureFlags,
                feature_flags: featureFlags,
                fake_breakout_prob: fakeBreakoutProb,
            };

            let inserted = false;
            for (let i = 0; i < this.monitorTrendData.length; i++) {
                const existing = this.monitorTrendData[i];
                if (!existing) continue;
                if (existing.x === x) {
                    const mergedEvents = mergeEvents(existing.events, newPoint.events);
                    const mergedFlags = {
                        ...(existing.featureFlags || {}),
                        ...(newPoint.featureFlags || {}),
                    };
                    this.monitorTrendData[i] = {
                        ...existing,
                        ...newPoint,
                        events: mergedEvents,
                        featureFlags: mergedFlags,
                        feature_flags: mergedFlags,
                    };
                    inserted = true;
                    break;
                }
                if (existing.x > x) {
                    this.monitorTrendData.splice(i, 0, newPoint);
                    inserted = true;
                    break;
                }
            }
            if (!inserted) {
                this.monitorTrendData.push(newPoint);
            }

            const windowMs = typeof options.windowMs === 'number' && options.windowMs > 0
                ? options.windowMs
                : null;
            const maxPoints = typeof options.maxPoints === 'number' && options.maxPoints > 0
                ? Math.floor(options.maxPoints)
                : 1200;

            if (windowMs) {
                const cutoff = x - windowMs;
                while (this.monitorTrendData.length && this.monitorTrendData[0].x < cutoff) {
                    this.monitorTrendData.shift();
                }
            }

            while (this.monitorTrendData.length > maxPoints) {
                this.monitorTrendData.shift();
            }

            if (this.monitorTrendChart?.data?.datasets?.length) {
                const baseData = this.getFormattedTrendChartData().filter(item => Number.isFinite(item?.y));
                const trendIndex = this.monitorTrendDatasetMap?.trend ?? 0;
                if (this.monitorTrendChart.data.datasets[trendIndex]) {
                    this.monitorTrendChart.data.datasets[trendIndex].data = baseData;
                }
                this.updateMonitorSignalOverlayDatasets();
                this.monitorTrendChart.update('none');
            }

            this.renderMonitorSignalHistory();
        }

        normalizeMonitorTime(value) {
            try {
                if (value == null) return null;
                if (value instanceof Date) return value.getTime();
                if (typeof value === 'number') {
                    return Number.isFinite(value) ? value : null;
                }
                if (typeof value === 'string' && !value.trim()) {
                    return null;
                }
                const parsed = this.parseMonitorChartTime(value);
                if (parsed && !Number.isNaN(parsed.getTime())) {
                    return parsed.getTime();
                }
                const fallback = new Date(value);
                return Number.isNaN(fallback.getTime()) ? null : fallback.getTime();
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

        syncCharts(source, target) {
            if (!source || !target) return;
            if (this._syncingCharts) return;

            this._syncingCharts = true;

            try {
                const isMonitorTrendPair =
                    (source === this.monitorChart && target === this.monitorTrendChart)
                    || (source === this.monitorTrendChart && target === this.monitorChart);

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
                const ws = this.app?.ibkrManager?.ws;
                ws?.send?.(JSON.stringify({ type: 'subscribe_realtime', symbol: target }));
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
                    const ws = this.app?.ibkrManager?.ws;
                    ws?.send?.(JSON.stringify({ type: 'unsubscribe_realtime', symbol: activeSymbol }));
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

        updateMonitorStopLossLine() {
            if (!this.monitorChart) return;

            const stopLossInput = document.getElementById('monitorStopLoss');
            const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;

            const annotations = this.ensureMonitorAnnotations();
            if (!annotations) return;

            if (stopLossPrice && !Number.isNaN(stopLossPrice)) {
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

            const annotations = this.ensureMonitorAnnotations();
            if (!annotations) return;

            if (takeProfitPrice && !Number.isNaN(takeProfitPrice)) {
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

        async loadMonitorOrdersList(symbol, status = 'working') {
            if (this.app?.orderManager?.loadMonitorOrdersList) {
                return this.app.orderManager.loadMonitorOrdersList(symbol, status);
            }
            return null;
        }

        async _loadMonitorOrdersList(symbol, status = 'working') {
            if (this.app?.orderManager?._loadMonitorOrdersList) {
                return this.app.orderManager._loadMonitorOrdersList(symbol, status);
            }
            return null;
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

        async loadMonitorChart(symbol, options = {}) {
            try {
                this.pendingMonitorCandlestickUpgradeToken = null;
                const { useCache = true, forceRefresh = false } = options || {};
                const chartContainer = document.getElementById('monitorChartContainer');
                if (!chartContainer) return;

                // 显示加载状态
                chartContainer.innerHTML = `<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">加载图表数据中...</p></div>`;

                await this.ensureDateAdapter();

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
                        chartContainer.innerHTML = `<div class=\"alert alert-danger\">获取图表数据失败: ${this.escapeHtml(msg)}${extra}</div>`;
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

                        const volumeData = ohlcData.map(item => ({ x: item.x, y: item.volume }));
                        maxVolume = Math.max(...volumeData.map(v => v.y), 0);
                        chartConfig = {
                            type: 'candlestick',
                            data: {
                                labels: labels,
                                datasets: [
                                    {
                                        type: 'candlestick',
                                        label: `${symbol} OHLC`,
                                        data: ohlcData,
                                        yAxisID: 'y',
                                        // 添加hover配置以确保事件能正确触发
                                        hoverBackgroundColor: 'rgba(255, 99, 132, 0.8)',
                                        hoverBorderColor: 'rgba(255, 99, 132, 1)',
                                        hoverBorderWidth: 2
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
                    chartContainer.innerHTML = `<div class=\"alert alert-danger\">Failed to load chart: ${this.escapeHtml(error?.message || error)}</div>`;
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

    }

    monitoring.manager = monitoring.manager || {};
    monitoring.manager.MonitoringManager = MonitoringManager;
    root.MonitoringManager = MonitoringManager;
})(typeof window !== 'undefined' ? window : globalThis);
