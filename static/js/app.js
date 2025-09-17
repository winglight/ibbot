// 全局常量定义
const INTERVAL_MINUTES_MAP = {
    '1min': 1,
    '5min': 5,
    '5mins': 5,
    '15min': 15,
    '15mins': 15,
    '30mins': 30,
    '1hour': 60
};

const MONITOR_SOURCE_TZ = 'America/New_York';
const MONITOR_FALLBACK_TZ = 'Asia/Shanghai';

const TREND_LEVEL_STYLES = {
    0: {
        buyColor: '#60a5fa',
        sellColor: '#f87171',
        neutralColor: '#94a3b8',
        barBuyColor: 'rgba(96,165,250,0.3)',
        barSellColor: 'rgba(248,113,113,0.3)',
        barNeutralColor: 'rgba(148,163,184,0.25)',
        pointRadius: 2.5,
        hoverRadius: 4,
        borderWidth: 1,
        borderColor: '#ffffff',
        labelFont: '600 11px sans-serif',
        labelOffset: 16,
        labelBuyColor: '#60a5fa',
        labelSellColor: '#f87171'
    },
    1: {
        buyColor: 'rgba(134,239,172,0.85)',
        sellColor: 'rgba(252,165,165,0.85)',
        neutralColor: '#94a3b8',
        barBuyColor: 'rgba(134,239,172,0.35)',
        barSellColor: 'rgba(252,165,165,0.35)',
        barNeutralColor: 'rgba(148,163,184,0.25)',
        pointRadius: 3.5,
        hoverRadius: 5,
        borderWidth: 1,
        borderColor: '#ffffff',
        labelFont: '600 12px sans-serif',
        labelOffset: 18,
        labelBuyColor: '#4ade80',
        labelSellColor: '#f87171'
    },
    2: {
        buyColor: 'rgba(74,222,128,0.9)',
        sellColor: 'rgba(248,113,113,0.9)',
        neutralColor: '#94a3b8',
        barBuyColor: 'rgba(74,222,128,0.4)',
        barSellColor: 'rgba(248,113,113,0.4)',
        barNeutralColor: 'rgba(148,163,184,0.25)',
        pointRadius: 4.5,
        hoverRadius: 6,
        borderWidth: 1.5,
        borderColor: '#f0fdf4',
        labelFont: '600 13px sans-serif',
        labelOffset: 20,
        labelBuyColor: '#22c55e',
        labelSellColor: '#ef4444'
    },
    3: {
        buyColor: 'rgba(34,197,94,0.95)',
        sellColor: 'rgba(239,68,68,0.95)',
        neutralColor: '#94a3b8',
        barBuyColor: 'rgba(34,197,94,0.45)',
        barSellColor: 'rgba(239,68,68,0.45)',
        barNeutralColor: 'rgba(148,163,184,0.25)',
        pointRadius: 5.5,
        hoverRadius: 7,
        borderWidth: 2,
        borderColor: '#dcfce7',
        labelFont: '700 14px sans-serif',
        labelOffset: 22,
        labelBuyColor: '#16a34a',
        labelSellColor: '#dc2626'
    },
    4: {
        buyColor: '#16a34a',
        sellColor: '#b91c1c',
        neutralColor: '#94a3b8',
        barBuyColor: 'rgba(22,163,74,0.55)',
        barSellColor: 'rgba(185,28,28,0.55)',
        barNeutralColor: 'rgba(148,163,184,0.25)',
        pointRadius: 6.5,
        hoverRadius: 8,
        borderWidth: 2.5,
        borderColor: '#fef9c3',
        labelFont: '700 15px sans-serif',
        labelOffset: 24,
        labelBuyColor: '#15803d',
        labelSellColor: '#b91c1c',
        shadowBlur: 10,
        shadowColor: 'rgba(250,204,21,0.55)',
        outlineColor: '#fef08a',
        outlineWidth: 1.5
    }
};

function getTrendLevelStyle(level) {
    const key = Number.isFinite(level) ? Number(level) : 0;
    return TREND_LEVEL_STYLES[key] || TREND_LEVEL_STYLES[0];
}

function getTrendSuggestionLabel(suggestion, level) {
    if (!suggestion) return '';
    const numericLevel = Number(level);
    const resolvedLevel = Number.isFinite(numericLevel) ? Math.min(4, Math.max(1, Math.round(numericLevel))) : 1;
    if (suggestion === 'buy') return `B${resolvedLevel}`;
    if (suggestion === 'sell') return `S${resolvedLevel}`;
    return '';
}

if (typeof window !== 'undefined') {
    window.getTrendLevelStyle = getTrendLevelStyle;
    window.getTrendSuggestionLabel = getTrendSuggestionLabel;
}

const TrendSignalLabelPlugin = {
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
            const style = getTrendLevelStyle(level);
            const text = getTrendSuggestionLabel(raw.suggestion, raw.level)
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

function slugifyStrategyName(name) {
    return (name || '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '_')
        .replace(/_+/g, '_')
        .replace(/^_+|_+$/g, '');
}

class TradingApp {
    constructor() {
        this.ws = null;
        this.connected = false;
        this.charts = {}; // Store chart instances
        this.executionsPagination = {
            currentPage: 1,
            pageSize: 25,
            totalItems: 0,
            allExecutions: []
        };
        this.cancellingOrders = new Set();
        this.strategiesLoaded = false;
        this.startReminderTimeout = null;
        this.endReminderTimeout = null;
        this.currentStrategyDetails = null;
        this.internalStrategyOrders = [];
        // 当前订阅实时行情的合约符号，null 表示未订阅
        this.monitorRealtimeSymbol = null;
        // 缓存当日订单列表，供监控时查找最新建仓/加仓时间
        this.orders = [];
        // 通知列表
        this.notifications = [];
        this.monitorChartTimezone = null;
        // 价格缓存机制
        this.priceCache = new Map();
        this.priceCacheTimeout = 30000; // 30秒缓存
        this.domSignals = [];
        this.monitorTrendChart = null;
        this.monitorTrendData = [];
        // 渲染监控图表时使用的定时器句柄，防止重复渲染导致死循环
        this.monitorChartRenderTimeout = null;
        // 缓存待渲染的图表回调，确保快速连续请求只触发一次绘制
        this.pendingMonitorChartRender = null;
        // 图表联动时的递归保护开关
        this._syncingCharts = false;

        // 初始化登录管理器
        this.loginManager = new LoginManager(this);
        this.ibkrManager = new IBKRConnectionManager(this);
        this.dataManager = new IBKRDataManager(this);
        this.orderManager = new OrderManager(this);
        this.portfolioManager = new PortfolioManager(this);
        this.riskRulesManager = new RiskRulesManager(this);
        this.strategiesManager = new StrategiesManager(this);
        this.logsManager = new LogsManager(
            this.apiCall.bind(this),
            this.showAlert.bind(this),
            this.formatNumber.bind(this)
        );
        this.notificationsManager = new NotificationsManager(this);
        this.domManager = new DOMManager(this);

        // Internal strategy summary helpers
        this.summaryControlsInitialized = false;
        this.summaryPrimarySaving = false;
        this.summaryPrimaryCurrent = null;
        this.summaryPushUpdating = false;
        this.summaryPushAwaitingData = false;
        this.summaryPushAwaitingTimer = null;
        this.summaryStatusLoading = false;
        this.summaryDataFeedCurrent = null;
        this.data_push_state = {};

        this.init();
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
        if (Array.isArray(points)) {
            this.monitorTrendData = points.map(point => ({ ...point }));
        } else {
            this.monitorTrendData = [];
        }
        if (this.monitorTrendChart?.data?.datasets?.[0]) {
            this.monitorTrendChart.data.datasets[0].data = this.getFormattedTrendChartData();
            this.syncTrendReferenceLines();
            this.monitorTrendChart.update('none');
        }
    }

    resetMonitorTrendData() {
        this.setMonitorTrendHistory([]);
    }

    formatTrendPointForChart(point) {
        if (!point || typeof point !== 'object') return null;
        const rawTime = point.timestamp ?? point.x ?? point.time ?? point.date;
        const normalized = this.normalizeMonitorTime(rawTime);
        if (normalized == null) return null;
        return {
            ...point,
            timestamp: normalized,
            x: new Date(normalized)
        };
    }

    getFormattedTrendChartData() {
        if (!Array.isArray(this.monitorTrendData)) return [];
        return this.monitorTrendData
            .map(point => this.formatTrendPointForChart(point))
            .filter(point => point !== null);
    }

    getTrendValues(chart) {
        const ds0 = chart.data?.datasets?.[0];
        const arr = Array.isArray(ds0?.data) ? ds0.data : [];
        const ys = arr.map(p => (typeof p === 'number' ? p : p?.y)).filter(Number.isFinite);
        if (!ys.length) return null;
        return { latest: ys[ys.length - 1], min: Math.min(...ys), max: Math.max(...ys) };
    }

    fmt(v, digits = 2) {
        return Number.isFinite(v) ? v.toFixed(digits) : String(v);
    }


    // 仅在创建 chart 后、首轮配置时调用一次
    setupTrendLegendAndAnnotations(chart) {
        const that = this;
        const opts = chart.options || (chart.options = {});
        const plugins = opts.plugins || (opts.plugins = {});

        const hasLegend = Object.prototype.hasOwnProperty.call(plugins, 'legend');
        const legend = hasLegend ? plugins.legend : null;

        const legendLabels = legend?.labels || {};
        const newLegend = {
            ...(legend || {}),
            display: true,
            position: 'top',
            align: 'end',
            labels: {
            ...legendLabels,
            generateLabels(chart) {
                const v = that.getTrendValues(chart);
                if (!v) return [];
                return [
                { text: `Latest: ${that.fmt(v.latest)}`, fillStyle: 'yellow', strokeStyle: 'yellow', lineWidth: 2 },
                { text: `Min: ${that.fmt(v.min)}`,       fillStyle: 'red',    strokeStyle: 'red',    lineWidth: 2 },
                { text: `Max: ${that.fmt(v.max)}`,       fillStyle: 'green',  strokeStyle: 'green',  lineWidth: 2 },
                ];
            },
            // 若还想显示原 datasets 的图例，把这行删掉
            filter() { return false; }
            },
            onClick: () => {}
        };

        // —— 关键：如果 legend 不存在，不要立刻写入，延后到微任务 —— //
        if (!hasLegend) {
            queueMicrotask(() => {
            // 这里已经不在脚本化解析栈里，安全赋值
            plugins.legend = newLegend;
            chart.update('none');
            });
        } else {
            // 已存在时，仅就地合并，不换引用
            Object.assign(legend, newLegend);
            legend.labels = newLegend.labels; // 覆盖 labels（对象内仍为就地写入）
        }

        // annotation：同理，先判断是否存在，缺失则延后创建；存在则就地改
        // const hasAnno = Object.prototype.hasOwnProperty.call(plugins, 'annotation');
        // if (!hasAnno) {
        //     queueMicrotask(() => {
        //     plugins.annotation = { annotations: {
        //         latestLine: { type: 'line', borderColor: 'yellow', borderWidth: 1 },
        //         minLine:    { type: 'line', borderColor: 'red',    borderWidth: 1 },
        //         maxLine:    { type: 'line', borderColor: 'green',  borderWidth: 1 },
        //     }};
        //     chart.update('none');
        //     });
        // } else {
        //     const anns = plugins.annotation.annotations || (plugins.annotation.annotations = {});
        //     anns.latestLine = anns.latestLine || { type: 'line', borderColor: 'yellow', borderWidth: 1 };
        //     anns.minLine    = anns.minLine    || { type: 'line', borderColor: 'red',    borderWidth: 1 };
        //     anns.maxLine    = anns.maxLine    || { type: 'line', borderColor: 'green',  borderWidth: 1 };
        // }
    }

    // 需要时手动调用；只在数值变化时更新 yMin/yMax，避免触发外层 watch 循环
    refreshTrend(chart) {
        const v = this.getTrendValues(chart);
        const anns = chart.options.plugins?.annotation?.annotations;
        if (!anns) return; // 还没初始化

        let changed = false;
        const setLine = (line, y) => {
            if (!line) return;
            if (line.yMin !== y || line.yMax !== y) {
            line.yMin = y; line.yMax = y;
            changed = true;
            }
        };

        if (!v) {
            ['latestLine','minLine','maxLine'].forEach(k => {
            if (anns[k] && ('yMin' in anns[k])) {
                delete anns[k].yMin; delete anns[k].yMax; changed = true;
            }
            });
        } else {
            setLine(anns.latestLine, v.latest);
            setLine(anns.minLine,    v.min);
            setLine(anns.maxLine,    v.max);
        }
        if (changed) chart.update('none');
    }


    syncTrendReferenceLines() {
        if (!this.monitorTrendChart || !Array.isArray(this.monitorTrendChart.data?.datasets)) return;

        const chart = this.monitorTrendChart;
        const baseDataset = chart.data.datasets[0];
        if (!baseDataset) return;

        // Hide primary dataset from legend so that legend focuses on reference lines
        baseDataset._excludeFromLegend = true;

        const parsedPoints = Array.isArray(baseDataset.data)
            ? baseDataset.data.map((point) => {
                if (!point) return null;
                const xValue = point.x instanceof Date
                    ? point.x
                    : (Number.isFinite(point.timestamp) ? new Date(point.timestamp) : (Number.isFinite(point.x) ? new Date(point.x) : null));
                const rawY = typeof point.y === 'number' ? point.y
                    : (typeof point.value === 'number' ? point.value
                        : (typeof point.score === 'number' ? point.score : null));
                if (!xValue || !Number.isFinite(rawY)) return null;
                return { x: xValue, y: rawY };
            }).filter(Boolean)
            : [];

        if (!parsedPoints.length) {
            chart.data.datasets = chart.data.datasets.filter((_, index) => index === 0);
            return;
        }

        const values = parsedPoints.map(point => point.y);
        if (!values.length) {
            chart.data.datasets = chart.data.datasets.filter((_, index) => index === 0);
            return;
        }

        const latestValue = values[values.length - 1];
        const minValue = Math.min(...values);
        const maxValue = Math.max(...values);

        const lineTypes = [
            { key: 'latest', label: '最新值', color: 'rgba(255, 205, 86, 0.9)', value: latestValue, dash: [6, 4], order: 4 },
            { key: 'min', label: '最小值', color: 'rgba(220, 53, 69, 0.85)', value: minValue, dash: [], order: 3 },
            { key: 'max', label: '最大值', color: 'rgba(25, 135, 84, 0.85)', value: maxValue, dash: [], order: 3 }
        ];

        const lineDatasets = lineTypes.map((descriptor) => {
            const dataset = {
                type: 'line',
                label: `${descriptor.label}: ${this.formatNumber(descriptor.value)}`,
                data: parsedPoints.map(point => ({ x: point.x, y: descriptor.value })),
                borderColor: descriptor.color,
                backgroundColor: descriptor.color,
                borderWidth: 1.5,
                pointRadius: 0,
                pointHoverRadius: 0,
                pointHitRadius: 0,
                fill: false,
                tension: 0,
                parsing: false,
                spanGaps: true,
                stepped: false,
                borderDash: descriptor.dash,
                segment: descriptor.dash.length ? { borderDash: descriptor.dash } : undefined,
                yAxisID: 'y',
                xAxisID: 'x',
                order: descriptor.order,
                _refLineType: descriptor.key
            };
            return dataset;
        });

        chart.data.datasets = [baseDataset, ...lineDatasets];
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
        console.log('[App] addMonitorTrendPoint 开始 - score:', score, 'time:', time, 'options:', options);
        
        if (typeof score !== 'number' || Number.isNaN(score)) {
            console.warn('[App] addMonitorTrendPoint - 无效的score值:', score);
            return;
        }

        let x = this.normalizeMonitorTime(time ?? Date.now());
        if (x == null) {
            x = Date.now();
        }
        
        console.log('[App] addMonitorTrendPoint - 标准化时间:', x);

        const providedReason = typeof options.reason === 'string' ? options.reason : '';
        const reasonLines = [];
        const trendLine = `Trend Score ${score.toFixed(2)}`;

        const providedLineSet = new Set();
        if (providedReason) {
            providedReason.split('\n').forEach((line) => {
                const trimmed = line.trim();
                if (trimmed) {
                    const lower = trimmed.toLowerCase();
                    providedLineSet.add(lower);
                    reasonLines.push(trimmed);
                }
            });
        }

        if (!providedLineSet.has(trendLine.toLowerCase())) {
            reasonLines.unshift(trendLine);
        }

        if (reasonLines.length === 0) {
            reasonLines.push(trendLine);
            reasonLines.push('指标变化有限，保持观察');
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
        if (!suggestion) {
            const threshold = typeof options.threshold === 'number' && options.threshold > 0
                ? options.threshold
                : 0.5;
            if (score >= threshold) suggestion = 'buy';
            else if (score <= -threshold) suggestion = 'sell';
        }

        const level = typeof options.level === 'number'
            ? options.level
            : this.deriveTrendLevel(score);
        const price = typeof options.price === 'number' && Number.isFinite(options.price)
            ? options.price
            : undefined;

        if (!Array.isArray(this.monitorTrendData)) {
            this.monitorTrendData = [];
            console.log('[App] addMonitorTrendPoint - 初始化monitorTrendData数组');
        }

        const newPoint = {
            x,
            timestamp: x,
            y: score,
            reason: reasonLines.join('\n'),
            suggestion,
            level,
            price
        };
        
        console.log('[App] addMonitorTrendPoint - 创建新数据点:', newPoint);
        console.log('[App] addMonitorTrendPoint - 当前monitorTrendData长度:', this.monitorTrendData.length);

        let inserted = false;
        for (let i = 0; i < this.monitorTrendData.length; i++) {
            const existing = this.monitorTrendData[i];
            if (!existing) continue;
            if (existing.x === x) {
                this.monitorTrendData[i] = { ...existing, ...newPoint };
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
        
        console.log('[App] addMonitorTrendPoint - 数据插入后monitorTrendData长度:', this.monitorTrendData.length);
        console.log('[App] addMonitorTrendPoint - 最新5个数据点:', this.monitorTrendData.slice(-5));

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

        if (this.monitorTrendChart?.data?.datasets?.[0]) {
            console.log('[App] addMonitorTrendPoint - 更新monitorTrendChart，数据点数量:', this.monitorTrendData.length);
            this.monitorTrendChart.data.datasets[0].data = this.getFormattedTrendChartData();
            this.syncTrendReferenceLines();
            this.monitorTrendChart.update('none');
            console.log('[App] addMonitorTrendPoint - 图表更新完成');
        } else {
            console.warn('[App] addMonitorTrendPoint - monitorTrendChart不存在或未初始化');
            console.log('[App] addMonitorTrendPoint - monitorTrendChart状态:', {
                exists: !!this.monitorTrendChart,
                hasData: !!this.monitorTrendChart?.data,
                hasDatasets: !!this.monitorTrendChart?.data?.datasets,
                hasFirstDataset: !!this.monitorTrendChart?.data?.datasets?.[0]
            });
        }
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
            const FUT_ROOTS = new Set(['MNQ','NQ','ES','MES','YM','MYM','RTY','M2K','CL','NG','GC','SI','HG','ZB','ZN','ZF','ZT','6E','6J','6B','6A','6C','6S']);
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

    async init() {
        try {
            if (window.Chart && window.ChartAnnotation && window.Chart.register) {
                window.Chart.register(window.ChartAnnotation);
            }
            if (window.Chart && window.zoomPlugin && window.Chart.register) {
                window.Chart.register(window.zoomPlugin);
            }
            if (window.Chart && window.Chart.register) {
                const fin = window.ChartFinancial || window["chartjs-chart-financial"]; 
                if (fin && fin.CandlestickController && fin.OhlcElement) {
                    window.Chart.register(
                        fin.CandlestickController,
                        fin.OhlcController,
                        fin.CandlestickElement,
                        fin.OhlcElement
                    );
                }
            }
        } catch (e) {}
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
            console.log('Refresh suggestion button found and event listener attached');
            refreshSuggestionBtn.addEventListener('click', () => {
                console.log('Refresh suggestion button clicked!');
                this.refreshPositionSuggestion();
            });
        } else {
            console.log('Refresh suggestion button not found!');
        }
        
        // Auto-refresh suggestion when order parameters change
        const orderSymbol = document.getElementById('orderSymbol');
        const orderAction = document.getElementById('orderAction');
        const orderPrice = document.getElementById('orderPrice');
        if (orderSymbol) {
            orderSymbol.addEventListener('input', () => {
                const symbol = orderSymbol.value.toUpperCase().trim();
                
                // 获取当前价格（如果输入的标的长度>=2）
                if (symbol.length >= 2) {
                    // 使用防抖避免频繁调用
                    clearTimeout(this.priceUpdateTimeout);
                    this.priceUpdateTimeout = setTimeout(() => {
                        this.fetchCurrentPrice(symbol);
                    }, 500);
                } else {
                    // 隐藏价格显示
                    const priceDisplay = document.getElementById('currentPriceDisplay');
                    if (priceDisplay) {
                        priceDisplay.style.display = 'none';
                    }
                }
                
                // Recalculate risk prices whenever symbol changes
                this.updateRiskPrices().catch(console.error);
                
                // Refresh position suggestion with debounce
                this.debounceRefreshSuggestion();
            });
        }
        if (orderAction) {
            orderAction.addEventListener('change', () => {
                // Update risk prices when action changes
                this.updateRiskPrices().catch(console.error);
                // Refresh position suggestion with debounce
                this.debounceRefreshSuggestion();
            });
        }
        if (orderPrice) {
            orderPrice.addEventListener('input', () => {
                // Update risk prices when price changes
                this.updateRiskPrices().catch(console.error);
                // Refresh position suggestion with debounce
                this.debounceRefreshSuggestion();
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
        
        // Order price change - auto-calculate risk prices
        document.getElementById('orderPrice').addEventListener('input', () => {
            this.updateRiskPrices().catch(console.error);
        });
        
        // Stop loss and take profit price changes - calculate expected PnL only
        document.getElementById('stopLossPrice').addEventListener('input', () => {
            this.updateExpectedPnL();
            // 移除自动刷新position suggestion，避免频繁API调用
            // this.debounceRefreshSuggestion();
        });
        
        document.getElementById('takeProfitPrice').addEventListener('input', () => {
            this.updateExpectedPnL();
        });
        
        // Order quantity change - update expected PnL only
        document.getElementById('orderQuantity').addEventListener('input', () => {
            this.updateExpectedPnL();
            // 只有在数量变化时才刷新position suggestion
            this.debounceRefreshSuggestion();
        });
        
        // Note: Order action and symbol event listeners are already handled above in the unified section
        
        
    }

    // WebSocket functionality moved to IBKRConnectionManager

    scheduleTradingReminders() {
        if (typeof window === 'undefined' || !('Notification' in window)) {
            console.log('浏览器不支持通知功能');
            return;
        }

        const startTime = localStorage.getItem('startTradingTime') || '20:30';
        const endTime = localStorage.getItem('endTradingTime') || '22:30';

        const now = new Date();
        console.log(`当前时间: ${now.toLocaleString()}, 开市提醒: ${startTime}, 收市提醒: ${endTime}`);

        const schedule = (timeStr, type) => {
            const target = this.getNextReminderDate(timeStr, now);
            const delay = target.getTime() - now.getTime();
            console.log(`${type === 'start' ? '开市' : '收市'}提醒计划时间: ${target.toLocaleString()}, 延迟: ${Math.round(delay/1000/60)}分钟`);
            
            if (delay <= 0) {
                console.warn(`${type === 'start' ? '开市' : '收市'}提醒时间已过，跳过`);
                return null;
            }
            
            return setTimeout(() => {
                console.log(`触发${type === 'start' ? '开市' : '收市'}提醒`);
                this.fireTradingReminder(type);
                this.scheduleTradingReminders();
            }, delay);
        };

        // 请求通知权限
        if (Notification.permission === 'default') {
            try { 
                Notification.requestPermission().then(permission => {
                    console.log(`通知权限状态: ${permission}`);
                });
            } catch (e) {
                console.error('请求通知权限失败:', e);
            }
        } else {
            console.log(`当前通知权限状态: ${Notification.permission}`);
        }

        // 清除之前的定时器
        if (this.startReminderTimeout) {
            clearTimeout(this.startReminderTimeout);
            console.log('清除之前的开市提醒定时器');
        }
        if (this.endReminderTimeout) {
            clearTimeout(this.endReminderTimeout);
            console.log('清除之前的收市提醒定时器');
        }

        // 设置新的定时器
        this.startReminderTimeout = schedule(startTime, 'start');
        this.endReminderTimeout = schedule(endTime, 'end');
        
        console.log('定时提醒已重新设置');
    }

    fireTradingReminder(type) {
        const message = type === 'start' ? '🔔 开始交易提醒' : '🔔 结束交易提醒';
        console.log(`发送提醒: ${message}`);
        
        // 浏览器通知
        try {
            if ('Notification' in window && Notification.permission === 'granted') {
                const notification = new Notification(message, {
                    icon: '/favicon.ico',
                    badge: '/favicon.ico',
                    tag: 'trading-reminder',
                    requireInteraction: true
                });
                console.log('浏览器通知已发送');
                
                // 自动关闭通知
                setTimeout(() => {
                    notification.close();
                }, 10000);
            } else {
                console.warn(`无法发送浏览器通知，权限状态: ${Notification.permission}`);
            }
        } catch (e) {
            console.error('发送浏览器通知失败:', e);
        }
        
        // Telegram通知
        this.apiCall('/notify', { 
            method: 'POST', 
            body: JSON.stringify({ message }) 
        }).then(response => {
            console.log('Telegram通知发送成功:', response);
        }).catch(error => {
            console.error('Telegram通知发送失败:', error);
        });
    }
    

    


    getNextReminderDate(timeStr, reference) {
        const [hour, minute] = timeStr.split(':').map(n => parseInt(n, 10));
        let date = new Date(reference);
        date.setHours(hour, minute, 0, 0);
        while (date <= reference || !this.isWorkingDay(date) || this.isNasdaqHoliday(date)) {
            date.setDate(date.getDate() + 1);
            date.setHours(hour, minute, 0, 0);
        }
        return date;
    }

    isWorkingDay(date) {
        const day = date.getDay();
        return day >= 1 && day <= 5;
    }

    isNasdaqHoliday(date) {
        const holidays = [
            // 2024年假期
            '2024-01-01', '2024-01-15', '2024-02-19', '2024-03-29',
            '2024-05-27', '2024-06-19', '2024-07-04', '2024-09-02',
            '2024-11-28', '2024-12-25',
            // 2025年假期
            '2025-01-01', '2025-01-20', '2025-02-17', '2025-04-18',
            '2025-05-26', '2025-06-19', '2025-07-04', '2025-09-01',
            '2025-11-27', '2025-12-25'
        ];
        const d = date.toISOString().split('T')[0];
        return holidays.includes(d);
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
        const useCache = options.useCache !== false && method === 'GET';
        const cacheTime = options.cacheTime || 10000; // 默认缓存10秒
        
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
                    ...options.headers
                };
                
                // 添加JWT令牌到Authorization头
                const token = localStorage.getItem('access_token');
                if (token) {
                    headers['Authorization'] = `Bearer ${token}`;
                }
                
                // 使用带超时的fetch
                const response = await this.fetchWithTimeout(`/api${endpoint}`, {
                    headers,
                    ...options,
                    timeout: options.timeout ?? 15000 // 默认15秒超时
                });
                
                // 处理401未授权 - 重定向到登录
                if (response.status === 401) {
                    localStorage.removeItem('access_token');
                    this.loginManager.toggleAuthButtons(false);
                    this.loginManager.showLoginModal();
                    throw new Error('Authentication required');
                }
                
                const data = await response.json();
                const reqId = response.headers.get('x-request-id');
                if (reqId) data.request_id = reqId;
                
                // 特殊处理/api/notify接口，即使返回success:false也不抛出错误
                if (endpoint === '/notify' && typeof data === 'object' && 'success' in data) {
                    if (!data.success) {
                        console.warn('Notification failed:', data.message || 'Unknown reason');
                    }
                    return data;
                }
                
                if (!response.ok) {
                    throw new Error(data.detail || 'API call failed');
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

    openRiskRuleModal(symbol = '', stopLoss = '', takeProfit = '', maxTime = '', position = 0, currentPrice = 0, avgCost = 0, isEditMode = false) {
        document.getElementById('riskSymbol').value = symbol;
        document.getElementById('riskStopLossPrice').value = stopLoss;
        document.getElementById('riskTakeProfitPrice').value = takeProfit;
        document.getElementById('riskMaxTime').value = maxTime;
        
        // Store position data for PnL calculations
        this.currentRiskRuleData = {
            symbol: symbol,
            position: position,
            currentPrice: currentPrice,
            avgCost: avgCost
        };
        
        // Always show PnL preview section
        const pnlSection = document.getElementById('pnlPreviewSection');
        pnlSection.style.display = 'block';
        this.updatePnLPreview();
        
        // Set button text based on mode
        const submitBtn = document.getElementById('submitRiskRuleBtn');
        if (isEditMode) {
            submitBtn.innerHTML = '<i class="fas fa-save"></i> Save Changes';
        } else {
            submitBtn.innerHTML = '<i class="fas fa-plus"></i> Add Risk Rule';
        }
        
        // Add event listeners for real-time updates
        this.addPnLUpdateListeners();
        
        const modal = new bootstrap.Modal(document.getElementById('riskRuleModal'));
        
        // Clean up listeners when modal is closed
        document.getElementById('riskRuleModal').addEventListener('hidden.bs.modal', () => {
            this.removePnLUpdateListeners();
        }, { once: true });
        
        modal.show();
    }

    updatePnLPreview() {
        if (!this.currentRiskRuleData) return;
        
        const { position, currentPrice, avgCost } = this.currentRiskRuleData;
        const stopLossPrice = parseFloat(document.getElementById('riskStopLossPrice').value) || 0;
        const takeProfitPrice = parseFloat(document.getElementById('riskTakeProfitPrice').value) || 0;
        
        // Check if we have valid position data
        if (position === 0 || !avgCost) {
            // Show placeholder when no position data
            document.getElementById('stopLossPnL').textContent = 'N/A';
            document.getElementById('takeProfitPnL').textContent = 'N/A';
            
            // Set neutral colors
            document.getElementById('stopLossPnL').className = 'fw-bold text-muted';
            document.getElementById('takeProfitPnL').className = 'fw-bold text-muted';
            return;
        }
        
        // Calculate PnL based on actual position size
        const stopLossPnL = (stopLossPrice - avgCost) * position;
        const takeProfitPnL = (takeProfitPrice - avgCost) * position;
        
        // Update display
        document.getElementById('stopLossPnL').textContent = `$${stopLossPnL.toFixed(2)}`;
        document.getElementById('takeProfitPnL').textContent = `$${takeProfitPnL.toFixed(2)}`;
        
        // Update colors based on positive/negative values
        const stopLossElement = document.getElementById('stopLossPnL');
        const takeProfitElement = document.getElementById('takeProfitPnL');
        
        stopLossElement.className = stopLossPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
        takeProfitElement.className = takeProfitPnL >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
    }

    addPnLUpdateListeners() {
        // Remove existing listeners to avoid duplicates
        this.removePnLUpdateListeners();
        
        const symbol = document.getElementById('riskSymbol').value;
        
        // Add new listeners with price correction
        this.stopLossListener = (e) => {
            const correctedPrice = this.correctPriceStep(e.target.value, symbol);
            if (correctedPrice !== parseFloat(e.target.value)) {
                e.target.value = correctedPrice;
            }
            this.updatePnLPreview();
        };
        
        this.takeProfitListener = (e) => {
            const correctedPrice = this.correctPriceStep(e.target.value, symbol);
            if (correctedPrice !== parseFloat(e.target.value)) {
                e.target.value = correctedPrice;
            }
            this.updatePnLPreview();
        };
        
        document.getElementById('riskStopLossPrice').addEventListener('input', this.stopLossListener);
        document.getElementById('riskTakeProfitPrice').addEventListener('input', this.takeProfitListener);
    }

    removePnLUpdateListeners() {
        if (this.stopLossListener) {
            document.getElementById('riskStopLossPrice').removeEventListener('input', this.stopLossListener);
        }
        if (this.takeProfitListener) {
            document.getElementById('riskTakeProfitPrice').removeEventListener('input', this.takeProfitListener);
        }
    }

    async editPositionRiskRule(symbol, currentPrice, avgCost, position = 0, presetStopLoss = '', presetTakeProfit = '') {
        try {
            // Get risk configuration from API
            const config = await this.apiCall('/risk-config');
            
            const stopLossRatio = config.stop_loss_ratio || 0.025;
            const takeProfitRatio = config.take_profit_ratio || 0.025;
            
            // Determine if this is a short position (negative position)
            const isShortPosition = position < 0;
            
            // Determine base price for SL/TP calculation
            let basePrice = avgCost; // Default to average cost
            
            // Apply leverage adjustment for leveraged assets
            let adjustedStopLossRatio = stopLossRatio;
            let adjustedTakeProfitRatio = takeProfitRatio;
            
            const leverageMap = (config && config.leverage) ? config.leverage : {};
            const lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || (symbol === 'MNQ' ? (leverageMap['MNQ'] || 20) : null);
            if (lev && !isNaN(lev) && lev > 0) {
                adjustedStopLossRatio = stopLossRatio / lev;
                adjustedTakeProfitRatio = takeProfitRatio / lev;
            }
            
            let stopLoss, takeProfit;
            
            // Use preset values if provided, otherwise calculate suggested values
            if (presetStopLoss !== '' && presetTakeProfit !== '') {
                stopLoss = presetStopLoss;
                takeProfit = presetTakeProfit;
            } else {
                // Calculate suggested values based on position direction
                if (isShortPosition) {
                    // For short positions: stop loss is above avg cost, take profit is below avg cost
                    stopLoss = (basePrice * (1 + adjustedStopLossRatio)).toFixed(2);
                    takeProfit = (basePrice * (1 - adjustedTakeProfitRatio)).toFixed(2);
                } else {
                    // For long positions: stop loss is below avg cost, take profit is above avg cost
                    stopLoss = (basePrice * (1 - adjustedStopLossRatio)).toFixed(2);
                    takeProfit = (basePrice * (1 + adjustedTakeProfitRatio)).toFixed(2);
                }
            }
            
            this.openRiskRuleModal(symbol, stopLoss, takeProfit, '', position, currentPrice, avgCost, true);
        } catch (error) {
            console.error('Failed to get risk config:', error);
            // Fallback to preset values or default calculated values with leverage consideration
            let stopLoss, takeProfit;
            const isShortPosition = position < 0;
            
            if (presetStopLoss !== '' && presetTakeProfit !== '') {
                stopLoss = presetStopLoss;
                takeProfit = presetTakeProfit;
            } else {
                // Use default ratios with leverage adjustment
                let stopLossRatio = 0.025; // Default 2.5%
                let takeProfitRatio = 0.025; // Default 2.5%
                
                // Try to get leverage info even if risk config failed
                try {
                    const config = await this.apiCall('/risk-config');
                    const leverageMap = (config && config.leverage) ? config.leverage : {};
                    const lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || (symbol === 'MNQ' ? (leverageMap['MNQ'] || 20) : null);
                    if (lev && !isNaN(lev) && lev > 0) {
                        stopLossRatio = stopLossRatio / lev;
                        takeProfitRatio = takeProfitRatio / lev;
                    }
                } catch (leverageError) {
                    console.warn('Failed to get leverage info, using default ratios:', leverageError);
                }
                
                if (isShortPosition) {
                    // For short positions: stop loss above avg cost, take profit below avg cost
                    stopLoss = presetStopLoss || (avgCost * (1 + stopLossRatio)).toFixed(2);
                    takeProfit = presetTakeProfit || (avgCost * (1 - takeProfitRatio)).toFixed(2);
                } else {
                    // For long positions: stop loss below avg cost, take profit above avg cost
                    stopLoss = presetStopLoss || (avgCost * (1 - stopLossRatio)).toFixed(2);
                    takeProfit = presetTakeProfit || (avgCost * (1 + takeProfitRatio)).toFixed(2);
                }
            }
            
            this.openRiskRuleModal(symbol, stopLoss, takeProfit, '', position, currentPrice, avgCost, true);
        }
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
            if (combinedEl) combinedEl.textContent = `${(positionQty || 0)} ($${this.formatNumber((Math.abs(positionQty) || 0) * (currentPrice || 0))})`;
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
            this.currentMonitor = {
                symbol,
                positionQty: Number(positionQty) || 0,
                currentPrice: Number(currentPrice) || 0,
                avgCost: Number(avgCost) || 0,
                pnl: Number(pnl) || 0,
                pnlPercent: Number(pnlPercent) || 0,
                maxTimeSpan: (this.riskRules && this.riskRules[symbol] && this.riskRules[symbol].max_time_span) || 30,
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
                        this.currentMonitor.maxTimeSpan = rule.max_time_span || this.currentMonitor.maxTimeSpan;
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
                    };
                    // 如果之前订阅了其他合约，先取消订阅
                    if (this.monitorRealtimeSymbol && this.monitorRealtimeSymbol !== symbol) {
                        try {
                            console.log('[monitor] unsubscribe realtime', this.monitorRealtimeSymbol);
            this.ibkrManager.ws?.send(JSON.stringify({ type: 'unsubscribe_realtime', symbol: this.monitorRealtimeSymbol }));
                        } catch (e) {}
                        this.monitorRealtimeSymbol = null;
                        setActive(false);
                    }
                    realtimeBtn.onclick = () => {
                        if (this.monitorRealtimeSymbol) {
                            try {
                                console.log('[monitor] unsubscribe realtime', this.monitorRealtimeSymbol);
            this.ibkrManager.ws?.send(JSON.stringify({ type: 'unsubscribe_realtime', symbol: this.monitorRealtimeSymbol }));
                            } catch (e) {}
                            this.monitorRealtimeSymbol = null;
                            setActive(false);
                        } else {
                            try {
                                console.log('[monitor] subscribe realtime', { symbol });
            this.ibkrManager.ws?.send(JSON.stringify({ type: 'subscribe_realtime', symbol }));
                                this.monitorRealtimeSymbol = symbol;
                                setActive(true);
                                // 开启实时后强制刷新图表，绕过缓存
                                this.loadMonitorChart(symbol, { useCache: false, forceRefresh: true });
                            } catch (e) {
                                console.error('subscribe realtime failed', e);
                            }
                        }
                    };
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
                    closePositionBtn.onclick = async () => {
                        if (!this.currentMonitor || this.currentMonitor.positionQty === 0) {
                            this.showAlert('warning', '当前无持仓，无法平仓');
                            return;
                        }
                        
                        const confirmed = confirm(`确认平仓 ${symbol}？\n持仓量: ${this.currentMonitor.positionQty}\n将以市价单全部平仓`);
                        if (!confirmed) return;
                        
                        try {
                            const response = await this.apiCall('/close_position', {
                                method: 'POST',
                                body: JSON.stringify({ symbol })
                            });
                            
                            if (response.success) {
                                this.showAlert('success', `平仓订单已提交: ${response.action} ${response.quantity} ${symbol} (订单ID: ${response.order_id})`);
                                // 刷新持仓和订单数据
                                this.loadPortfolio();
                                this.loadOrders();
                                // 刷新监控弹窗数据
                                await this.syncMonitorModalData(symbol);
                            } else {
                                this.showAlert('danger', `平仓失败: ${response.message || '未知错误'}`);
                            }
                        } catch (err) {
                            console.error('平仓失败:', err);
                            this.showAlert('danger', `平仓失败: ${err.message || err}`);
                        }
                    };
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
                                this.loadPortfolio();
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

                // 关闭时取消实时订阅
                modalEl.addEventListener('hidden.bs.modal', () => {
                    if (this.monitorRealtimeSymbol) {
                        try {
                            console.log('[monitor] unsubscribe realtime', this.monitorRealtimeSymbol);
            this.ibkrManager.ws?.send(JSON.stringify({ type: 'unsubscribe_realtime', symbol: this.monitorRealtimeSymbol }));
                        } catch (e) {}
                        this.monitorRealtimeSymbol = null;
                    }
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
                
                // 初次载入图表
                this.loadMonitorChart(symbol);
                
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

    // 订单数据缓存
    ordersCache = {};
    
    // 创建防抖版本的loadMonitorOrdersList方法
    debouncedLoadMonitorOrdersList = null;
    
    // 初始化防抖函数
    initDebounce() {
        if (!this.debouncedLoadMonitorOrdersList) {
            this.debouncedLoadMonitorOrdersList = this.debounce(this._loadMonitorOrdersList.bind(this), 300);
        }
    }
    
    // 防抖包装方法
    async loadMonitorOrdersList(symbol, status = 'working') {
        // 确保防抖函数已初始化
        this.initDebounce();
        
        const ordersList = document.getElementById('monitorOrdersList');
        if (!ordersList) return;
        
        // 显示加载状态
        ordersList.innerHTML = '<div class="text-center text-muted py-2"><div class="spinner-border spinner-border-sm text-primary me-2" role="status"></div><small>加载订单数据中...</small></div>';
        
        // 调用防抖版本的方法
        this.debouncedLoadMonitorOrdersList(symbol, status);
    }
    
    // 实际加载订单的方法
    async _loadMonitorOrdersList(symbol, status = 'working') {
        try {
            const ordersList = document.getElementById('monitorOrdersList');
            if (!ordersList) return;
            
            // 检查缓存
            const cacheKey = `orders_${symbol}_${status}`;
            const now = Date.now();
            const cachedData = this.ordersCache[cacheKey];
            
            let result;
            if (cachedData && (now - cachedData.timestamp) < 10 * 1000) { // 10秒缓存
                console.log(`Using cached orders data for ${symbol} (${status})`);
                result = cachedData.data;
            } else {
                console.log(`Fetching fresh orders data for ${symbol} (${status})`);
                // 获取订单数据，使用缓存机制
                result = await this.apiCall('/orders', {
                    useCache: true,
                    cacheTime: 10000, // 10秒缓存
                    timeout: 20000 // 20秒超时
                });
                
                // 更新缓存
                this.ordersCache[cacheKey] = {
                    data: result,
                    timestamp: now
                };
            }
            
            let orders = result.orders || [];
            // 仅保留当天创建的订单
            orders = orders.filter(order => this.isSameDay(order.submit_time || order.created_at));
            
            // 过滤当前资产的订单
            const filteredOrders = orders.filter(order => {
                const matchSymbol = order.symbol === symbol;
                
                // 市价单应该总是显示在filled中，因为它们会立即成交
                if (order.order_type === 'MKT') {
                    return matchSymbol && status === 'filled';
                }
                
                // 限价单根据状态分类
                const matchStatus = status === 'working' ? 
                    ['PendingSubmit', 'Submitted', 'PreSubmitted', 'ApiPending'].includes(order.status) :
                    ['Filled'].includes(order.status);
                return matchSymbol && matchStatus;
            });
            
            if (filteredOrders.length === 0) {
                ordersList.innerHTML = '<div class="text-center text-muted py-2"><small>暂无订单</small></div>';
                return;
            }
            
            // 构建订单列表HTML
            let html = '';
            filteredOrders.forEach(order => {
                const side = order.action === 'BUY' ? '买入' : '卖出';
                const sideClass = order.action === 'BUY' ? 'text-success' : 'text-danger';
                const quantity = Math.abs(parseFloat(order.quantity || 0));
                // 检查订单类型和价格
                const orderType = order.order_type || 'MKT';
                const limitPrice = parseFloat(order.limit_price || 0);
                const avgFillPrice = parseFloat(order.avg_fill_price || 0);
                
                let priceText;
                if (orderType === 'LMT' && limitPrice > 0) {
                    priceText = `$${limitPrice.toFixed(2)} (限价)`;
                } else if (avgFillPrice > 0) {
                    priceText = `$${avgFillPrice.toFixed(2)} (成交)`;
                } else {
                    priceText = '市价';
                }
                
                // 判断是否需要显示同步按钮（限价单且状态为Submitted）
                const needSyncButton = orderType === 'LMT' && order.status === 'Submitted';
                
                html += `
                    <div class="border-bottom py-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <div>
                                <span class="badge ${sideClass === 'text-success' ? 'bg-success' : 'bg-danger'} me-1">${side}</span>
                                <small class="text-muted">${quantity}</small>
                            </div>
                            <div class="text-end d-flex align-items-center">
                                <small class="${sideClass} me-2">${priceText}</small>
                                ${needSyncButton ? `<button class="btn btn-outline-primary btn-sm" onclick="app.syncLimitOrder('${order.order_id}', null, event)" title="同步订单状态"><i class="fas fa-sync-alt"></i></button>` : ''}
                            </div>
                        </div>
                    </div>
                `;
            });
            
            ordersList.innerHTML = html;
            
        } catch (error) {
            console.error('加载订单列表失败:', error);
            const ordersList = document.getElementById('monitorOrdersList');
            if (ordersList) {
                ordersList.innerHTML = '<div class="text-center text-muted py-2"><small>加载失败</small></div>';
            }
        }
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
                                <p><strong>提交时间:</strong> ${formatDateTime(orderData.submit_time)}</p>
                                <p><strong>执行时间:</strong> ${formatDateTime(orderData.executed_at)}</p>
                                <p><strong>订单来源:</strong> ${orderData.order_source || '—'}</p>
                                <p><strong>备注:</strong> ${orderData.notes || '—'}</p>
                                <p><strong>策略:</strong> ${orderData.strategy || '—'}</p>
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

    parseMonitorChartTime(raw) {
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
                    // 转换到本地时区而不是targetZone，确保显示本地时间
                    const localTime = dt.setZone('local');
                    console.log(`[时间转换] 原始: ${raw} -> 本地时间: ${localTime.toFormat('yyyy-MM-dd HH:mm:ss')}`);
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
            const { useCache = true, forceRefresh = false } = options || {};
            const chartContainer = document.getElementById('monitorChartContainer');
            if (!chartContainer) return;

            // 显示加载状态
            chartContainer.innerHTML = `<div class="text-center py-3"><div class="spinner-border text-primary" role="status"></div><p class="mt-2">加载图表数据中...</p></div>`;
            

            // 获取图表参数
            const intervalSelect = document.getElementById('monitorInterval');
            const durationSelect = document.getElementById('monitorDuration');
            const barSize = intervalSelect?.value || '1 min';
            const duration = durationSelect?.value || '1 D';
            
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
                        // 增加超时时间，略高于后端65秒
                        timeout: 6000
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

            // Wait a bit for DOM to update
            this.pendingMonitorChartRender = () => {
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
                const intervalKey = intervalValueRaw.replace(/\s+/g, '');
                const padIntervalMinutes = INTERVAL_MINUTES_MAP[intervalKey] || 1;
                const padSpanMinutes = this.currentMonitor?.maxTimeSpan || 30;
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

                // 计算时间跨度相关参数
                const intervalValue = intervalSelect ? intervalSelect.value : '1min';
                const chartIntervalMinutes = INTERVAL_MINUTES_MAP[intervalValue] || 1;
                const chartSpanMinutes = this.currentMonitor?.maxTimeSpan || 30;
                const spanBars = Math.max(1, Math.round(chartSpanMinutes / chartIntervalMinutes));

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
                    const containerWidth = chartContainer ? chartContainer.offsetWidth : 800;
                    
                    // 根据图表宽度计算合适的显示数据量
                    // 蜡烛图和折线图使用相同的像素间距，保持一致的显示数据量
                    const pixelsPerDataPoint = 10; // 统一像素间距
                    const optimalDataCount = Math.floor(containerWidth / pixelsPerDataPoint);
                    // 减少显示数据量，为右边数据点留出空间
                    const maxDisplayCount = Math.min(Math.floor(optimalDataCount * 0.9), Math.max(15, Math.min(80, dataLength)));
                    
                    // 始终计算显示范围，确保蜡烛图和折线图使用相同的数据量
                    const actualDisplayCount = Math.min(maxDisplayCount, dataLength);
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
                            this.monitorTrendChart = new Chart(trendCtx, {
                                type: 'line',
                                data: {
                                    datasets: [{
                                        label: 'Trend Score',
                                        data: this.getFormattedTrendChartData(),
                                        borderColor: 'rgba(255,165,0,0.9)',
                                        backgroundColor: 'rgba(255,165,0,0.1)',
                                        pointRadius: (ctx) => {
                                            const level = ctx.raw?.level ?? 0;
                                            return getTrendLevelStyle(level).pointRadius ?? 3;
                                        },
                                        pointHoverRadius: (ctx) => {
                                            const level = ctx.raw?.level ?? 0;
                                            return getTrendLevelStyle(level).hoverRadius ?? 5;
                                        },
                                        pointHitRadius: (ctx) => {
                                            const level = ctx.raw?.level ?? 0;
                                            const style = getTrendLevelStyle(level);
                                            const base = style.hoverRadius ?? 5;
                                            return Math.max(10, base + 6);
                                        },
                                        pointBackgroundColor: (ctx) => {
                                            const raw = ctx.raw || {};
                                            const style = getTrendLevelStyle(raw.level ?? 0);
                                            if (raw.suggestion === 'buy') return style.buyColor || '#16a34a';
                                            if (raw.suggestion === 'sell') return style.sellColor || '#dc2626';
                                            return style.neutralColor || '#94a3b8';
                                        },
                                        pointBorderColor: (ctx) => {
                                            const style = getTrendLevelStyle(ctx.raw?.level ?? 0);
                                            return style.borderColor || '#ffffff';
                                        },
                                        pointBorderWidth: (ctx) => {
                                            const style = getTrendLevelStyle(ctx.raw?.level ?? 0);
                                            return style.borderWidth ?? 1;
                                        },
                                        tension: 0.2,
                                        spanGaps: true,
                                        yAxisID: 'y'
                                    }]
                                },
                                options: {
                                    responsive: true,
                                    maintainAspectRatio: false,
                                    interaction: { intersect: false, mode: 'index' },
                                    plugins: {
                                        legend: {
                                            display: true,
                                            position: 'top',
                                            align: 'end',
                                            labels: {
                                                padding: 12
                                            }
                                        },
                                        zoom: {
                                            pan: { enabled: true, mode: 'x' },
                                            zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' },
                                            onZoom: ({chart}) => this.syncCharts(chart, this.monitorChart),
                                            onPan: ({chart}) => this.syncCharts(chart, this.monitorChart)
                                        },
                                        tooltip: {
                                            callbacks: {
                                                label: (context) => {
                                                    const lines = [];
                                                    const value = context.parsed?.y;
                                                    if (typeof value === 'number') {
                                                        lines.push(`Trend Score: ${value.toFixed(2)}`);
                                                    }
                                                    const rawReason = context.raw?.reason;
                                                    if (rawReason) {
                                                        String(rawReason).split('\n').forEach((line) => {
                                                            if (line) lines.push(line);
                                                        });
                                                    }
                                                    if (context.raw?.suggestion) {
                                                        const label = getTrendSuggestionLabel(context.raw.suggestion, context.raw.level);
                                                        if (label) {
                                                            lines.push(`建议：${label}`);
                                                        } else {
                                                            lines.push(context.raw.suggestion === 'buy' ? '建议：Buy' : '建议：Sell');
                                                        }
                                                    }
                                                    return lines;
                                                }
                                            }
                                        },
                                        trendSignalLabel: { offset: 18 }
                                    },
                                    scales: {
                                        x: {
                                            type: 'time',
                                            time: { unit: 'minute', displayFormats: { minute: 'HH:mm', hour: 'HH:mm', day: 'MMM dd' } }
                                        },
                                        y: {
                                            position: 'left',
                                            min: -1,
                                            max: 1,
                                            title: { display: true, text: 'Trend Score' }
                                        }
                                    }
                                },
                                plugins: [TrendSignalLabelPlugin]
                            });
                            console.log('[loadMonitorChart] MonitorTrendChart created successfully');
                            console.log('[loadMonitorChart] Chart data after creation:', this.monitorTrendChart.data.datasets[0].data?.length || 0);
                            // 创建好 chart 后：
                            this.setupTrendLegendAndAnnotations(this.monitorTrendChart);

                            this.syncTrendReferenceLines();
                            this.monitorTrendChart.update('none');
                        } else {
                            console.log('[loadMonitorChart] Updating existing monitorTrendChart');
                            console.log('[loadMonitorChart] Current chart data length:', this.monitorTrendChart.data.datasets[0].data?.length || 0);
                            this.monitorTrendChart.data.datasets[0].data = this.getFormattedTrendChartData();
                            const trendYScale = this.monitorTrendChart.options?.scales?.y;
                            if (trendYScale) {
                                trendYScale.min = -1;
                                trendYScale.max = 1;
                                if ('beginAtZero' in trendYScale) delete trendYScale.beginAtZero;
                                if ('suggestedMin' in trendYScale) delete trendYScale.suggestedMin;
                                if ('suggestedMax' in trendYScale) delete trendYScale.suggestedMax;
                                if ('grace' in trendYScale) delete trendYScale.grace;
                            }
                            this.syncTrendReferenceLines();
                            // 每次你数据有变更（或初始化数据之后），显式调用：
                            this.refreshTrend(this.monitorTrendChart);
                            this.monitorTrendChart.update('none');
                            console.log('[loadMonitorChart] MonitorTrendChart updated');
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
                            const intervalValue = document.getElementById('monitorInterval')?.value || '1min';
                            const intervalMinutes = INTERVAL_MINUTES_MAP[intervalValue] || 1;
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
                            this.currentMonitor.maxTimeSpan = (nearestIndex - (this.currentMonitor?.riskStartIndex ?? 0)) * intervalMinutes;
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
                            const intervalValue = document.getElementById('monitorInterval')?.value || '1min';
                            const intervalMinutes = INTERVAL_MINUTES_MAP[intervalValue] || 1;
                            const newSpan = (index - this.currentMonitor.riskStartIndex) * intervalMinutes;
                            this.currentMonitor.maxTimeSpan = newSpan;
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
                            renderFn();
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
            if (typeof payload.last === 'number') price = payload.last;
            else if (typeof payload.price === 'number') price = payload.price;
            else if (typeof payload.close === 'number') price = payload.close;
            if (price == null) return;

            const volume = payload.volume || payload.vol || 0;

            const qty = Number(this.currentMonitor.positionQty || 0);
            const avgCost = Number(this.currentMonitor.avgCost || 0);
            
            // 更新当前价格到监控上下文
            this.currentMonitor.currentPrice = price;

            const curEl = document.getElementById('monitorCurrentPrice');
            if (curEl) curEl.textContent = `$${this.formatNumber(price)}`;

            const combinedEl = document.getElementById('monitorPositionCombined');
            if (combinedEl) combinedEl.textContent = `${qty} ($${this.formatNumber(Math.abs(qty) * price)})`;

            // 计算未实现盈亏，区分多头和空头持仓
            let pnl = 0;
            if (qty > 0) {  // 多头
                // 多头盈亏 = (当前价格 - 成本价) * 持仓数量
                pnl = (price - avgCost) * qty;
            } else {  // 空头
                // 空头盈亏 = (成本价 - 当前价格) * 持仓数量的绝对值
                pnl = (avgCost - price) * Math.abs(qty);
            }
            
            const pnlEl = document.getElementById('monitorPnL');
            if (pnlEl) {
                pnlEl.textContent = `$${this.formatNumber(pnl)}`;
                pnlEl.classList.toggle('text-success', pnl >= 0);
                pnlEl.classList.toggle('text-danger', pnl < 0);
            }

            const denom = Math.max(1e-9, Math.abs(avgCost) * Math.max(1, Math.abs(qty)));
            const pnlPct = (pnl / denom) * 100;
            const pnlPctEl = document.getElementById('monitorPnLPercent');
            if (pnlPctEl) {
                pnlPctEl.textContent = `(${this.formatNumber(pnlPct)}%)`;
                pnlPctEl.classList.toggle('text-success', pnl >= 0);
                pnlPctEl.classList.toggle('text-danger', pnl < 0);
            }

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

                // 获取当前图表的interval设置
                const intervalSelect = document.getElementById('monitorInterval');
                const intervalRaw = intervalSelect ? intervalSelect.value : '1 min';

                // 标准化interval格式，移除空格，与loadMonitorChart方法保持一致
                const interval = intervalRaw.replace(/\s+/g, '');

                // 根据interval计算正确的时间点
                const now = new Date();
                const intervalTime = new Date(now);

                switch(interval) {
                    case '1min':
                        intervalTime.setSeconds(0, 0);
                        break;
                    case '5min':
                        const minutes5 = Math.floor(intervalTime.getMinutes() / 5) * 5;
                        intervalTime.setMinutes(minutes5, 0, 0);
                        break;
                    case '15min':
                        const minutes15 = Math.floor(intervalTime.getMinutes() / 15) * 15;
                        intervalTime.setMinutes(minutes15, 0, 0);
                        break;
                    case '1hour':
                        intervalTime.setMinutes(0, 0, 0);
                        break;
                    default:
                        break;
                }

                const durationSelect = document.getElementById('monitorDuration');
                const duration = durationSelect?.value || '1 D';

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

                const timeValue = intervalTime.getTime();

                if (chartType === 'candlestick') {
                    const ohlcData = Array.isArray(dataset.data) ? dataset.data : [];
                    candlestickData = ohlcData;
                    const volumeDataset = this.monitorChart.data.datasets[1];
                    const vData = Array.isArray(volumeDataset.data) ? volumeDataset.data : [];
                    if (ohlcData.length > 0) {
                        const lastBar = ohlcData[ohlcData.length - 1];
                        if (lastBar.x === timeValue) {
                            lastBar.c = price;
                            lastBar.h = Math.max(lastBar.h || price, price);
                            lastBar.l = Math.min(lastBar.l || price, price);
                            lastBar.volume = volume;
                            if (vData.length > 0 && vData[vData.length - 1].x === timeValue) {
                                vData[vData.length - 1].y = volume;
                            } else {
                                vData.push({ x: timeValue, y: volume });
                            }
                        } else {
                            ohlcData.push({ x: timeValue, o: price, h: price, l: price, c: price, volume });
                            vData.push({ x: timeValue, y: volume });
                        }
                    } else {
                        ohlcData.push({ x: timeValue, o: price, h: price, l: price, c: price, volume });
                        vData.push({ x: timeValue, y: volume });
                    }

                    const MAX_POINTS = 300;
                    if (ohlcData.length > MAX_POINTS) {
                        ohlcData.shift();
                        vData.shift();
                    }

                    if (this.currentMonitor) {
                        this.currentMonitor.displayTimes = ohlcData.map(item => new Date(item.x));
                        if (Array.isArray(this.monitorChart.data.labels)) {
                            this.currentMonitor.displayLabels = Array.from(this.monitorChart.data.labels);
                        }
                    }

                    // 重新分配新的数据引用，确保Chart.js能检测到变更
                    dataset.data = ohlcData.map(bar => ({ ...bar }));
                    volumeDataset.data = vData.map(entry => ({ ...entry }));

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
                    const labels = this.monitorChart.data.labels || [];
                    const volumeDataset = this.monitorChart.data.datasets[1];
                    let shouldUpdateLast = false;

                    if (labels.length > 0) {
                        const lastLabel = labels[labels.length - 1];
                        console.log(`[实时更新] 比较: 最后标签="${lastLabel}", 当前标签="${timeLabel}"`);
                        console.log(`[实时更新] 使用的interval="${intervalRaw}", duration="${duration}"`);
                        console.log(`[实时更新] 时间值: ${intervalTime.toISOString()}, 本地时间: ${new Date().toISOString()}`);
                        console.log(`[实时更新] 接收到的数据 - 价格: ${price}, 成交量: ${volume}`);

                        if (lastLabel === timeLabel) {
                            dataset.data[dataset.data.length - 1] = price;
                            volumeDataset.data[volumeDataset.data.length - 1] = volume;
                            shouldUpdateLast = true;
                            console.log(`[实时更新] 更新最后一个数据点: ${timeLabel}, 价格: ${price}`);
                        } else {
                            labels.push(timeLabel);
                            dataset.data.push(price);
                            volumeDataset.data.push(volume);
                            shouldUpdateLast = false;
                            console.log(`[实时更新] 添加新数据点: ${timeLabel}, 价格: ${price}`);
                        }
                    } else {
                        labels.push(timeLabel);
                        dataset.data.push(price);
                        volumeDataset.data.push(volume);
                        shouldUpdateLast = false;
                        console.log(`[实时更新] 添加首个数据点: ${timeLabel}, 价格: ${price}`);
                        console.log(`[实时更新] 使用的interval="${intervalRaw}", duration="${duration}"`);
                        console.log(`[实时更新] 时间值: ${intervalTime.toISOString()}, 本地时间: ${new Date().toISOString()}`);
                    }

                    const MAX_POINTS = 300;
                    if (labels.length > MAX_POINTS) {
                        labels.shift();
                        dataset.data.shift();
                        volumeDataset.data.shift();
                    }

                    // 重新分配引用，避免Chart.js跳过数据变更
                    this.monitorChart.data.labels = labels.slice();
                    dataset.data = dataset.data.slice();
                    volumeDataset.data = volumeDataset.data.slice();

                    if (this.currentMonitor) {
                        const normalizedTime = new Date(timeValue);
                        let displayTimes = Array.isArray(this.currentMonitor.displayTimes) ? [...this.currentMonitor.displayTimes] : [];
                        if (shouldUpdateLast && displayTimes.length) {
                            displayTimes[displayTimes.length - 1] = normalizedTime;
                        } else {
                            displayTimes.push(normalizedTime);
                        }
                        while (displayTimes.length > labels.length) {
                            displayTimes.shift();
                        }
                        if (displayTimes.length < labels.length) {
                            const filler = displayTimes.length ? new Date(displayTimes[0].getTime()) : normalizedTime;
                            while (displayTimes.length < labels.length) {
                                displayTimes.unshift(new Date(filler.getTime()));
                            }
                        }
                        this.currentMonitor.displayTimes = displayTimes;
                        this.currentMonitor.displayLabels = labels.slice();
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
                const volDataset = this.monitorChart.data.datasets[1];
                const volumes = volDataset.data
                    .map(v => (typeof v === 'object' && v !== null) ? v.y : v)
                    .filter(v => typeof v === 'number' && !isNaN(v));
                const maxVol = volumes.length ? Math.max(...volumes) : 0;
                this.monitorChart.options.scales.y1.max = (maxVol * 3) || 1;

                console.log(`[图表更新] 准备更新图表，数据点数量: ${dataset.data.length}, 成交量数据点: ${volDataset.data.length}`);
                console.log(`[图表更新] 价格数据: ${JSON.stringify(dataset.data.slice(-3))}`);
                console.log(`[图表更新] 成交量数据: ${JSON.stringify(volDataset.data.slice(-3))}`);
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
                        this.apiCall('/notify', { method: 'POST', body: JSON.stringify({ message: msg }) })
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
            
            let stopLossInput, takeProfitInput, positionQty, avgCost;
            
            if (isMonitorMode) {
                // 监控模式：使用监控面板的数据
                stopLossInput = document.getElementById('monitorStopLoss');
                takeProfitInput = document.getElementById('monitorTakeProfit');
                positionQty = this.currentMonitor.positionQty || 0;
                avgCost = this.currentMonitor.avgCost || 0;
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
            }
            
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
                        // 多头盈亏 = (止损价格 - 成本价) * 持仓数量
                        stopLossPnL = deltaSL * positionQty;
                    } else {  // 空头
                        // 空头盈亏 = (成本价 - 止损价格) * 持仓数量的绝对值
                        stopLossPnL = (avgCost - stopLossPrice) * Math.abs(positionQty);
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
                        // 多头盈亏 = (止盈价格 - 成本价) * 持仓数量
                        takeProfitPnL = deltaTP * positionQty;
                    } else {  // 空头
                        // 空头盈亏 = (成本价 - 止盈价格) * 持仓数量的绝对值
                        takeProfitPnL = (avgCost - takeProfitPrice) * Math.abs(positionQty);
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
        try {
            const activeRiskRuleDisplay = document.getElementById('activeRiskRuleDisplay');
            const activeStopLossPrice = document.getElementById('activeStopLossPrice');
            const activeTakeProfitPrice = document.getElementById('activeTakeProfitPrice');
            
            if (activeRiskRuleDisplay && activeStopLossPrice && activeTakeProfitPrice) {
                // 显示止损价格
                if (rule.stop_loss_price) {
                    activeStopLossPrice.textContent = `$${this.formatNumber(rule.stop_loss_price)}`;
                } else {
                    activeStopLossPrice.textContent = '未设置';
                }
                
                // 显示止盈价格
                if (rule.take_profit_price) {
                    activeTakeProfitPrice.textContent = `$${this.formatNumber(rule.take_profit_price)}`;
                } else {
                    activeTakeProfitPrice.textContent = '未设置';
                }
                
                // 显示激活风险规则区域
                activeRiskRuleDisplay.style.display = 'block';
            }
        } catch (err) {
            console.error('showActiveRiskRule failed:', err);
        }
    }

    hideActiveRiskRule() {
        try {
            const activeRiskRuleDisplay = document.getElementById('activeRiskRuleDisplay');
            if (activeRiskRuleDisplay) {
                activeRiskRuleDisplay.style.display = 'none';
            }
        } catch (err) {
            console.error('hideActiveRiskRule failed:', err);
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



    async addRiskRule() {
        const rule = {
            symbol: document.getElementById('riskSymbol').value.toUpperCase(),
            stop_loss_price: document.getElementById('riskStopLossPrice').value ? parseFloat(document.getElementById('riskStopLossPrice').value) : null,
            take_profit_price: document.getElementById('riskTakeProfitPrice').value ? parseFloat(document.getElementById('riskTakeProfitPrice').value) : null,
            max_time_span: document.getElementById('riskMaxTime').value || null
        };
        
        if (!rule.symbol) {
            this.showAlert('warning', 'Please enter a symbol');
            return;
        }
        
        if (!rule.stop_loss_price && !rule.take_profit_price) {
            this.showAlert('warning', 'Please enter at least one price level');
            return;
        }
        
        try {
            await this.apiCall('/risk_rules', {
                method: 'POST',
                body: JSON.stringify(rule)
            });
            
            this.showAlert('success', 'Risk rule added successfully');
            document.getElementById('riskRuleForm').reset();
            this.riskRulesManager.loadRiskRules();
            
            // Close modal
            const modal = bootstrap.Modal.getInstance(document.getElementById('riskRuleModal'));
            if (modal) {
                modal.hide();
                // Force remove backdrop if it exists
                setTimeout(() => {
                    const backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) {
                        backdrop.remove();
                    }
                    document.body.classList.remove('modal-open');
                    document.body.style.removeProperty('overflow');
                    document.body.style.removeProperty('padding-right');
                }, 300);
            }
        } catch (error) {
            console.error('Failed to add risk rule:', error);
        }
    }

    // 创建防抖版本的loadOrders方法
    debouncedLoadOrders = null;
    
    // 初始化loadOrders防抖函数
    initOrdersDebounce() {
        if (!this.debouncedLoadOrders) {
            this.debouncedLoadOrders = this.debounce(this._loadOrders.bind(this), 300);
        }
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
        const tbody = document.querySelector('#internalOrdersTable tbody');
        if (!tbody) return;

        const strategyName = name || this.currentStrategyDetails;
        const sourceOrders = Array.isArray(orders) ? orders : this.internalStrategyOrders;
        const data = Array.isArray(sourceOrders) ? sourceOrders : [];
        if (!strategyName || data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No strategy orders</td></tr>';
            return;
        }

        const frag = document.createDocumentFragment();
        data.forEach(order => {
            if (!order) return;
            const normalized = {
                order_id: order.order_id,
                symbol: order.symbol,
                action: order.action,
                quantity: order.quantity,
                order_type: order.order_type || 'MKT',
                limit_price: order.price ?? order.limit_price ?? null,
                price: order.price ?? null,
                avg_fill_price: order.fill_price ?? order.price ?? 0,
                status: order.status || 'Submitted',
                pnl: Number(order.pnl ?? 0),
                commission: Number(order.commission ?? 0),
                order_source: order.order_source || 'strategy',
                notes: order.notes || ''
            };
            const row = this.orderManager.generateOrderRowHtml(normalized);
            if (row) {
                frag.appendChild(row);
            }
        });

        if (!frag.childNodes.length) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No strategy orders</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        tbody.appendChild(frag);
    }

    async loadInternalStrategyOrders(name, limit = 200) {
        try {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/executions?limit=${limit}`);
            if (!res.success) {
                this.internalStrategyOrders = [];
                this.renderInternalOrders(name, []);
                return [];
            }
            const orders = Array.isArray(res.executions) ? res.executions.slice() : [];
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

    async loadPortfolio() {
        try {
            return this.portfolioManager.loadPortfolio();
        } catch (error) {
            console.error('Failed to load portfolio:', error);
        }
    }

    async loadRiskRules() { return this.riskRulesManager.loadRiskRules(); }



    async loadStrategySettings() {
        try {
            const data = await this.apiCall('/strategy/settings');
            const s = data.settings || {};
            const el1 = document.getElementById('strategyApiEndpoint');
            if (!el1) return;
            document.getElementById('strategyApiEndpoint').value = s.api_endpoint || '/api/strategy';
            document.getElementById('strategyApiMethod').value = s.api_method || 'POST';
            document.getElementById('strategyApiPayload').value = JSON.stringify(s.api_payload_template || {
                strategy: '{{strategy_name}}', symbol: '{{symbol}}', action: '{{action}}', qty: '{{quantity}}', type: '{{order_type}}', price: '{{price}}'
            }, null, 2);
            document.getElementById('strategyPath').value = s.strategy_path || 'src/strategies/';
            document.getElementById('enableStrategyAuth').checked = !!s.enable_auth;
            document.getElementById('strategyAuthToken').value = s.auth_token || '';
        } catch (error) {
            console.error('Failed to load strategy settings:', error);
        }
    }

    async saveStrategySettings() {
        try {
            const api_endpoint = document.getElementById('strategyApiEndpoint').value.trim() || '/api/strategy';
            const api_method = document.getElementById('strategyApiMethod').value || 'POST';
            const payloadText = document.getElementById('strategyApiPayload').value || '{}';
            let api_payload_template = {};
            try { api_payload_template = JSON.parse(payloadText); } catch (e) { this.showAlert('danger', 'Invalid JSON for payload template'); return; }
            const strategy_path = document.getElementById('strategyPath').value.trim() || 'src/strategies/';
            const enable_auth = document.getElementById('enableStrategyAuth').checked;
            const auth_token = document.getElementById('strategyAuthToken').value || null;
            await this.apiCall('/strategy/settings', {
                method: 'POST',
                body: JSON.stringify({ api_endpoint, api_method, api_payload_template, strategy_path, enable_auth, auth_token })
            });
            this.showAlert('success', 'Strategy settings saved');
            const modalEl = document.getElementById('strategySettingsModal');
            const modal = bootstrap.Modal.getInstance(modalEl);
            if (modal) modal.hide();
        } catch (error) {
            console.error('Failed to save strategy settings:', error);
            this.showAlert('danger', 'Failed to save strategy settings');
        }
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
            this.summaryPushAwaitingData = awaitingData;
            if (switchEl) switchEl.checked = !!state.active;
            if (statusEl) {
                if (state.active) {
                    statusEl.textContent = awaitingData ? '等待DOM数据…' : '已推送';
                } else {
                    statusEl.textContent = '未推送';
                }
            }
            this.data_push_state = {
                enabled: !!state.active,
                active: !!state.active,
                subscription: state.subscription,
                symbol: state.symbol,
                awaiting_data: awaitingData,
                last_data_timestamp: state.last_data_timestamp || null,
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
        const runtimeEl = document.getElementById('summaryStatusRuntime');
        const processedEl = document.getElementById('summaryStatusProcessed');
        const thresholdEl = document.getElementById('summaryStatusThresholdHits');
        const buyEl = document.getElementById('summaryStatusBuySignals');
        const sellEl = document.getElementById('summaryStatusSellSignals');
        const statusPush = document.getElementById('summaryDataPushStatus');
        const pushSwitch = document.getElementById('summaryDataPushSwitch');
        try {
            receivingEl.textContent = '…';
            runtimeEl && (runtimeEl.textContent = '…');
            processedEl && (processedEl.textContent = '…');
            thresholdEl && (thresholdEl.textContent = '…');
            buyEl && (buyEl.textContent = '…');
            sellEl && (sellEl.textContent = '…');
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/status`);
            if (!res?.success) throw new Error('Failed to fetch status');
            const status = res.status || {};
            receivingEl.textContent = status.is_receiving_data ? 'Yes' : 'No';
            runtimeEl && (runtimeEl.textContent = typeof status.runtime_seconds === 'number' ? status.runtime_seconds.toFixed(1) : '-');
            processedEl && (processedEl.textContent = status.processed_count ?? '-');
            thresholdEl && (thresholdEl.textContent = status.threshold_hits ?? '-');
            buyEl && (buyEl.textContent = status.buy_signals ?? '-');
            sellEl && (sellEl.textContent = status.sell_signals ?? '-');
            const pushState = status.data_push || {};
            let awaitingData = !!pushState.awaiting_data;
            if (status.is_receiving_data) {
                awaitingData = false;
            }
            if (!pushState.active) {
                awaitingData = false;
            }
            this.summaryPushAwaitingData = awaitingData;
            if (awaitingData) {
                if (!this.summaryPushAwaitingTimer) {
                    this.summaryPushAwaitingTimer = setTimeout(() => {
                        this.summaryPushAwaitingTimer = null;
                        if (this.currentStrategyDetails === name) {
                            this.loadStrategyStatus(name, { force: true });
                        }
                    }, 1500);
                }
            } else if (this.summaryPushAwaitingTimer) {
                clearTimeout(this.summaryPushAwaitingTimer);
                this.summaryPushAwaitingTimer = null;
            }
            if (pushSwitch && !this.summaryPushUpdating) {
                pushSwitch.checked = !!pushState.active;
            }
            if (statusPush) {
                if (pushState.active) {
                    statusPush.textContent = awaitingData ? '等待DOM数据…' : '已推送';
                } else {
                    statusPush.textContent = '未推送';
                }
            }
            this.data_push_state = {
                enabled: !!pushState.active,
                active: !!pushState.active,
                subscription: pushState.subscription,
                symbol: pushState.symbol,
                awaiting_data: awaitingData,
                last_data_timestamp: pushState.last_data_timestamp || null,
            };
        } catch (error) {
            console.error('Failed to load strategy status', error);
            receivingEl.textContent = '-';
            runtimeEl && (runtimeEl.textContent = '-');
            processedEl && (processedEl.textContent = '-');
            thresholdEl && (thresholdEl.textContent = '-');
            buyEl && (buyEl.textContent = '-');
            sellEl && (sellEl.textContent = '-');
            if (statusPush) statusPush.textContent = '未推送';
            this.summaryPushAwaitingData = false;
            this.data_push_state = {};
            if (this.summaryPushAwaitingTimer) {
                clearTimeout(this.summaryPushAwaitingTimer);
                this.summaryPushAwaitingTimer = null;
            }
        } finally {
            this.summaryStatusLoading = false;
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
        }
        
        // 修正价格到最近的有效单位
        const correctedPrice = Math.round(numPrice / step) * step;
        return parseFloat(correctedPrice.toFixed(step < 1 ? 2 : 0));
    }

    showAlert(type, message) {
        const alertContainer = document.getElementById('alertContainer');
        const alertId = 'alert-' + Date.now();
        
        // 提升容器层级，确保不被模态遮挡
        if (alertContainer) {
            alertContainer.style.position = 'fixed';
            alertContainer.style.top = document.querySelector('.modal.show') ? '10px' : '20px';
            alertContainer.style.right = '20px';
            alertContainer.style.zIndex = '20000';
            alertContainer.style.maxWidth = '420px';
        }
        
        const iconClass = type === 'success' ? 'fa-check-circle text-success'
            : (type === 'danger' || type === 'error') ? 'fa-times-circle text-danger'
            : type === 'warning' ? 'fa-exclamation-triangle text-warning'
            : 'fa-info-circle text-info';
        
        const alertHtml = `
            <div id="${alertId}" class="alert alert-${type} alert-dismissible fade show" role="alert" style="background:#ffffff; color:#212529; border:1px solid rgba(0,0,0,.125); box-shadow:0 0.5rem 1rem rgba(0,0,0,.15); opacity:1;">
                <div style="display:flex; align-items:center; gap:.5rem;">
                    <i class="fas ${iconClass}"></i>
                    <span>${message}</span>
                </div>
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            </div>
        `;
        
        alertContainer.insertAdjacentHTML('beforeend', alertHtml);
        
        // 同时添加到通知面板
        this.notificationsManager.addNotification(type, message);
        
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            const alert = document.getElementById(alertId);
            if (alert) {
                const bsAlert = new bootstrap.Alert(alert);
                bsAlert.close();
            }
        }, 5000);
    }

    // Symbol combobox functionality
    // Duplicate initSymbolCombobox method removed

    setupSymbolInput(inputId, dropdownId) {
        const input = document.getElementById(inputId);
        const dropdown = document.getElementById(dropdownId);
        if (!input || !dropdown) return;
        dropdown.dataset.inputId = inputId;
        let searchTimeout;

        // Load historical symbols on focus
        input.addEventListener('focus', () => {
            this.showHistoricalSymbols(dropdownId);
            dropdown.classList.add('show');
        });

        // Hide dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!input.contains(e.target) && !dropdown.contains(e.target)) {
                dropdown.classList.remove('show');
            }
        });

        // Search symbols on input
        input.addEventListener('input', (e) => {
            const query = e.target.value.trim();
            
            clearTimeout(searchTimeout);
            
            if (query.length === 0) {
                this.showHistoricalSymbols(dropdownId);
                // 隐藏价格显示
                if (inputId === 'orderSymbol') {
                    const priceDisplay = document.getElementById('currentPriceDisplay');
                    if (priceDisplay) {
                        priceDisplay.style.display = 'none';
                    }
                }
                return;
            }

            if (query.length >= 1) {
                searchTimeout = setTimeout(() => {
                    this.searchSymbols(query, dropdownId);
                }, 300);
            }
            
            // 如果是下单弹窗且输入长度>=2，尝试获取价格
            if (inputId === 'orderSymbol' && query.length >= 2) {
                clearTimeout(this.priceTimeout);
                this.priceTimeout = setTimeout(() => {
                    this.fetchCurrentPrice(query);
                }, 1000); // 1秒后获取价格
            }
        });

        // Handle keyboard navigation
        input.addEventListener('keydown', (e) => {
            const items = dropdown.querySelectorAll('.dropdown-item');
            const activeItem = dropdown.querySelector('.dropdown-item.active');
            let activeIndex = Array.from(items).indexOf(activeItem);

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeIndex = activeIndex < items.length - 1 ? activeIndex + 1 : 0;
                this.setActiveDropdownItem(items, activeIndex);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeIndex = activeIndex > 0 ? activeIndex - 1 : items.length - 1;
                this.setActiveDropdownItem(items, activeIndex);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (activeItem) {
                    activeItem.click();
                }
            } else if (e.key === 'Escape') {
                dropdown.classList.remove('show');
                input.blur();
            }
        });
    }

    setActiveDropdownItem(items, activeIndex) {
        items.forEach((item, index) => {
            item.classList.toggle('active', index === activeIndex);
        });
    }

    showHistoricalSymbols(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        if (!dropdown) return;
        const historicalSymbols = this.getHistoricalSymbols();

        let html = '';

        if (historicalSymbols.length > 0) {
            html += '<h6 class="dropdown-header">Recent Symbols</h6>';
            historicalSymbols.forEach(symbol => {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            });
            html += '<div class="dropdown-divider"></div>';
        }
        
        html += '<h6 class="dropdown-header">Popular Symbols</h6>';
        const popularSymbols = ['MNQ', 'NQ', 'ES', 'YM', 'AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA', 'AMZN'];
        popularSymbols.forEach(symbol => {
            if (!historicalSymbols.includes(symbol)) {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            }
        });
        
        dropdown.innerHTML = html;
        
        // Add click handlers
        dropdown.querySelectorAll('.dropdown-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const symbol = e.target.getAttribute('data-symbol');
                const targetId = dropdown.dataset.inputId || (dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol');
                const targetInput = document.getElementById(targetId);
                if (targetInput) {
                    targetInput.value = symbol;
                    const changeEvent = new Event('change', { bubbles: true });
                    targetInput.dispatchEvent(changeEvent);
                }
                dropdown.classList.remove('show');
                this.addToHistoricalSymbols(symbol);

                // 如果是下单弹窗的symbol选择，获取当前价格
                if (targetId === 'orderSymbol') {
                    this.fetchCurrentPrice(symbol);
                }
            });
        });
    }

    // 编辑订单
    editOrder(orderId, symbol, action, quantity, orderType, limitPrice) {
        // 填充下单表单
        document.getElementById('orderSymbol').value = symbol;
        document.getElementById('orderAction').value = action;
        document.getElementById('orderQuantity').value = quantity;
        // 正确转换订单类型到HTML选项值
        let normalizedType;
        if (orderType === 'LMT') {
            normalizedType = 'LIMIT';
        } else if (orderType === 'MKT') {
            normalizedType = 'MARKET';
        } else {
            normalizedType = orderType; // 保持原值
        }
        document.getElementById('orderType').value = normalizedType;
        
        // 获取当前价格（这会更新价格显示）
        this.fetchCurrentPrice(symbol);
        
        // 在fetchCurrentPrice之后设置价格输入框状态，确保限价单的设置不被覆盖
        setTimeout(() => {
            const priceInput = document.getElementById('orderPrice');
            if ((normalizedType === 'LIMIT') && limitPrice) {
                priceInput.value = limitPrice;
                priceInput.disabled = false;
            } else if (normalizedType === 'LIMIT') {
                // 限价单但没有预设价格，启用输入框让用户输入
                priceInput.disabled = false;
                if (!priceInput.value) {
                    priceInput.value = '';
                }
            }
            // 触发价格变化事件以更新SL/TP计算
            if (priceInput.value) {
                priceInput.dispatchEvent(new Event('input'));
            }
        }, 100);
        
        // 显示下单模态框
        this.setupOrderFormListeners();
        const modal = new bootstrap.Modal(document.getElementById('orderModal'));
        modal.show();
        
        // 存储原订单ID，用于后续处理
        this.editingOrderId = orderId;
        
        this.showAlert('info', `正在编辑订单 ${orderId}，请修改后重新提交`);
    }
    async loadStrategies() {
        try {
            const data = await this.apiCall('/strategies');
            if (data.success) {
                this.strategiesManager.displayStrategies(data.strategies);
                this.strategiesLoaded = true;
            }
        } catch (error) {
            console.error('Failed to load strategies:', error);
            this.strategiesLoaded = true; // Mark as loaded even on error to avoid repeated calls
        }
    }

    displayStrategies(strategies) {
        const tbody = document.querySelector('#strategiesTable tbody');
        if (!tbody) return;
        
        tbody.innerHTML = '';
        const requests = [];
        strategies.forEach(strategy => {
            const row = document.createElement('tr');
            
            // Name
            const nameCell = document.createElement('td');
            nameCell.textContent = strategy.name;
            nameCell.style.cursor = 'pointer';
            nameCell.style.color = '#0066cc';
            nameCell.style.textDecoration = 'underline';
            nameCell.addEventListener('click', () => {
                this.openStrategyDetails(strategy.name);
            });
            
            // Symbol（暂无字段，先占位）
            const symbolCell = document.createElement('td');
            symbolCell.textContent = '-';
            
            // Status（开关）
            const statusCell = document.createElement('td');
            const toggleSwitch = document.createElement('div');
            toggleSwitch.className = 'form-check form-switch';
            toggleSwitch.innerHTML = `
                <input class="form-check-input" type="checkbox" ${strategy.enabled ? 'checked' : ''} 
                       onchange="app.strategiesManager.toggleStrategy('${strategy.name}', this.checked)">
            `;
            statusCell.appendChild(toggleSwitch);
            
            // P&L（最近1个月）与 Trades（executions/2 取整），先占位，后异步回填
            const pnlCell = document.createElement('td');
            pnlCell.textContent = '...';
            const tradesCell = document.createElement('td');
            tradesCell.textContent = '...';
            
            row.appendChild(nameCell);
            row.appendChild(symbolCell);
            row.appendChild(statusCell);
            row.appendChild(pnlCell);
            row.appendChild(tradesCell);
            
            tbody.appendChild(row);
            
            // 异步获取 1m 性能数据
            const req = this.apiCall(`/strategies/${encodeURIComponent(strategy.name)}/performance?range=1m`)
                .then(res => {
                    if (!res || !res.success) {
                        pnlCell.textContent = '-';
                        tradesCell.textContent = '-';
                        return;
                    }
                    const m = res.metrics || {};
                    const totalPnL = Number(m.total_pnl ?? 0);
                    const totalExec = Number(m.total_executions ?? 0);
                    pnlCell.textContent = `$${(isFinite(totalPnL) ? totalPnL : 0).toFixed(2)}`;
                    tradesCell.textContent = `${Math.max(0, Math.floor((isFinite(totalExec) ? totalExec : 0) / 2))}`;
                })
                .catch(() => {
                    pnlCell.textContent = '-';
                    tradesCell.textContent = '-';
                });
            requests.push(req);
        });
        
        Promise.allSettled(requests).catch(() => {});
    }

    async toggleStrategy(name, enabled) {
        try {
            await this.apiCall(`/strategies/${encodeURIComponent(name)}/toggle?enabled=${enabled}`, { method: 'POST' });
            this.showAlert('success', `Strategy ${name} ${enabled ? 'enabled' : 'disabled'}`);
        } catch (error) {
            console.error('Failed to toggle strategy:', error);
            this.showAlert('danger', 'Failed to toggle strategy');
            // Reload to revert the switch
            this.strategiesManager.loadStrategies();
        }
    }

    async editStrategy(name) {
        try {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}`);
            if (!res.success) throw new Error('Failed to load strategy');
            const s = res.strategy;
            const nameEl = document.getElementById('editStrategyName');
            if (nameEl) nameEl.textContent = s.name;
            const desc = document.getElementById('editStrategyDescriptionInput');
            const api = document.getElementById('editStrategyApiInput');
            if (desc) desc.value = s.description || '';
            if (api) api.value = s.api_endpoint || '';

            // SL/TP: 仅对内部策略显示并填充
            const sltpGroup = document.getElementById('editSlTpGroup');
            const subscriptionGroup = document.getElementById('editSubscriptionGroup');
            const positionTimingGroup = document.getElementById('editPositionTimingGroup');
            if (sltpGroup) sltpGroup.style.display = s.strategy_type === 'internal' ? '' : 'none';
            if (subscriptionGroup) subscriptionGroup.style.display = s.strategy_type === 'internal' ? '' : 'none';
            if (positionTimingGroup) positionTimingGroup.style.display = s.strategy_type === 'internal' ? '' : 'none';
            const slEnabled = document.getElementById('editSlEnabledSwitch');
            const tpEnabled = document.getElementById('editTpEnabledSwitch');
            const slPrice = document.getElementById('editSlPriceInput');
            const tpPrice = document.getElementById('editTpPriceInput');
            const slType = document.getElementById('editSlTypeSelect');
            const tpType = document.getElementById('editTpTypeSelect');
            if (slEnabled) slEnabled.checked = !!s.sl_enabled;
            if (tpEnabled) tpEnabled.checked = !!s.tp_enabled;
            if (slPrice) slPrice.value = s.sl_price ?? '';
            if (tpPrice) tpPrice.value = s.tp_price ?? '';
            if (slType) slType.value = s.sl_type || 'amount';
            if (tpType) tpType.value = s.tp_type || 'amount';
            const editDataSubscription = document.getElementById('editDataSubscriptionSelect');
            if (editDataSubscription) editDataSubscription.value = s.data_subscription || '';
            const editCooldownInput = document.getElementById('editPositionAddCooldownInput');
            if (editCooldownInput) editCooldownInput.value = s.position_add_cooldown_minutes ?? '';
            const editCloseInput = document.getElementById('editCloseBeforeCloseInput');
            if (editCloseInput) editCloseInput.value = s.close_before_close_minutes ?? '';

            const modal = new bootstrap.Modal(document.getElementById('editStrategyModal'));
            modal.show();

            const updateBtn = document.getElementById('updateStrategyBtn');
            if (updateBtn) {
                const newBtn = updateBtn.cloneNode(true);
                updateBtn.parentNode.replaceChild(newBtn, updateBtn);
                newBtn.addEventListener('click', async () => {
                    const updates = {
                        description: document.getElementById('editStrategyDescriptionInput')?.value || '',
                        api_endpoint: document.getElementById('editStrategyApiInput')?.value || undefined,
                    };
                    if (s.strategy_type === 'internal') {
                        updates.sl_enabled = !!document.getElementById('editSlEnabledSwitch')?.checked;
                        updates.tp_enabled = !!document.getElementById('editTpEnabledSwitch')?.checked;
                        const slv = document.getElementById('editSlPriceInput')?.value;
                        const tpv = document.getElementById('editTpPriceInput')?.value;
                        updates.sl_price = slv ? Number(slv) : null;
                        updates.tp_price = tpv ? Number(tpv) : null;
                        updates.sl_type = document.getElementById('editSlTypeSelect')?.value || 'amount';
                        updates.tp_type = document.getElementById('editTpTypeSelect')?.value || 'amount';
                        const dataSub = document.getElementById('editDataSubscriptionSelect')?.value || '';
                        updates.data_subscription = dataSub || null;
                        const coolRaw = document.getElementById('editPositionAddCooldownInput')?.value;
                        const closeRaw = document.getElementById('editCloseBeforeCloseInput')?.value;
                        const coolVal = coolRaw !== undefined && coolRaw !== '' ? parseInt(coolRaw, 10) : null;
                        const closeVal = closeRaw !== undefined && closeRaw !== '' ? parseInt(closeRaw, 10) : null;
                        updates.position_add_cooldown_minutes = Number.isFinite(coolVal) ? coolVal : null;
                        updates.close_before_close_minutes = Number.isFinite(closeVal) ? closeVal : null;
                    }
                    try {
                        await this.apiCall(`/strategies/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(updates) });
                        this.showAlert('success', `Strategy ${name} updated`);
                        const modalEl = document.getElementById('editStrategyModal');
                        const inst = bootstrap.Modal.getInstance(modalEl);
                        if (inst) inst.hide();
                        this.strategiesManager.loadStrategies();
                    } catch (e) {
                        console.error(e);
                        this.showAlert('danger', 'Failed to update strategy');
                    }
                });
            }
        } catch (e) {
            console.error('Edit strategy error:', e);
            this.showAlert('danger', 'Failed to open edit modal');
        }
    }

    async openStrategyDetails(name) {
        try {
            this.currentStrategyDetails = name;
            const nameEl = document.getElementById('detailsStrategyName');
            if (nameEl) nameEl.textContent = name;
            let s = null;
            try {
                const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}`);
                if (res.success) s = res.strategy;
            } catch {}
            const typeEl = document.getElementById('detailsStrategyType');
            const trigEl = document.getElementById('detailsTriggerCount');
            const lastEl = document.getElementById('detailsLastTriggered');
            if (typeEl) typeEl.textContent = s?.strategy_type || '-';
            if (trigEl) trigEl.textContent = s?.trigger_count ?? 0;
            if (lastEl) lastEl.textContent = s?.last_triggered ? new Date(s.last_triggered).toLocaleString() : '-';

            const isInternal = s?.strategy_type === 'internal';
            const summaryWrap = document.getElementById('summary-tab-wrap');
            const ordersWrap = document.getElementById('internal-orders-tab-wrap');
            const execWrap = document.getElementById('executions-tab-wrap');
            const apiWrap = document.getElementById('api-examples-tab-wrap');
            const summaryPane = document.getElementById('summary');
            const ordersPane = document.getElementById('internal-orders');
            const chartsTabText = document.getElementById('charts-tab-text');
            if (isInternal) {
                this.ensureSummaryControls();
                summaryWrap?.classList.remove('d-none');
                ordersWrap?.classList.remove('d-none');
                execWrap?.classList.add('d-none');
                apiWrap?.classList.add('d-none');
                summaryPane?.classList.remove('d-none');
                ordersPane?.classList.remove('d-none');
                if (chartsTabText) chartsTabText.textContent = 'Visual Metrics';
                try { new bootstrap.Tab(document.getElementById('summary-tab')).show(); } catch (e) { console.warn('Tab show failed', e); }

                const symbolInput = document.getElementById('summaryPrimarySymbolInput');
                const dataSelect = document.getElementById('summaryDataFeedSelect');
                const pushStatusEl = document.getElementById('summaryDataPushStatus');
                const pushSwitch = document.getElementById('summaryDataPushSwitch');
                if (pushStatusEl) pushStatusEl.textContent = '未推送';
                if (pushSwitch) pushSwitch.checked = false;
                this.data_push_state = {};
                this.summaryPushAwaitingData = false;
                if (this.summaryPushAwaitingTimer) {
                    clearTimeout(this.summaryPushAwaitingTimer);
                    this.summaryPushAwaitingTimer = null;
                }
                const fallbackSymbol = s?.primary_symbol || '';
                if (symbolInput) {
                    symbolInput.value = fallbackSymbol || '';
                    this.summaryPrimaryCurrent = fallbackSymbol ? fallbackSymbol.toUpperCase() : null;
                    this.updateSummaryPrimaryStatus(fallbackSymbol ? 'saved' : 'empty');
                }
                const fallbackSub = s?.data_subscription || '';
                this.summaryDataFeedCurrent = fallbackSub || null;
                if (dataSelect) {
                    dataSelect.value = fallbackSub || 'market_data';
                }

                try {
                    const infoRes = await this.apiCall(`/strategies/${encodeURIComponent(name)}/internal-info`);
                    if (infoRes.success) {
                        const info = infoRes.strategy || {};
                        const descEl = document.getElementById('summaryDescription');
                        const pathEl = document.getElementById('summaryPath');
                        const highlightsEl = document.getElementById('summaryHighlights');
                        const riskParamEl = document.getElementById('summaryRiskParams');
                        if (descEl) descEl.textContent = info.description || '';
                        if (pathEl) pathEl.textContent = info.file_path || '';
                        const resolvedSymbol = info.primary_symbol || s?.primary_symbol || '';
                        if (symbolInput) {
                            symbolInput.value = resolvedSymbol || '';
                            this.summaryPrimaryCurrent = resolvedSymbol ? resolvedSymbol.toUpperCase() : null;
                            this.updateSummaryPrimaryStatus(resolvedSymbol ? 'saved' : 'empty');
                        }
                        if (highlightsEl) {
                            const points = Array.isArray(info.summary_points) ? info.summary_points : [];
                            if (points.length) {
                                highlightsEl.innerHTML = '';
                                points.forEach(text => {
                                    const li = document.createElement('li');
                                    li.textContent = text;
                                    highlightsEl.appendChild(li);
                                });
                            } else {
                                highlightsEl.innerHTML = '<li class="text-muted">No additional highlights</li>';
                            }
                        }
                        if (riskParamEl) {
                            const params = info.risk_parameters || {};
                            const mappings = {
                                trend_threshold: 'Trend Threshold',
                                cooldown_seconds: 'Cooldown',
                                max_loss_streak: 'Breaker (Max Loss Streak)',
                                default_quantity: 'Base Quantity'
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
                        const subKey = info.data_subscription || s?.data_subscription || '';
                        this.summaryDataFeedCurrent = subKey || null;
                        if (dataSelect) {
                            dataSelect.value = subKey || 'market_data';
                        }
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
                            (info.schedule || []).forEach(item => {
                                const li = document.createElement('li');
                                li.textContent = `${item.time || ''} ${item.action || ''} ${item.qty || ''} ${item.symbol || ''}`.trim();
                                schedList.appendChild(li);
                            });
                        }
                        const riskBody = document.querySelector('#summaryRiskTable tbody');
                        if (riskBody) {
                            riskBody.innerHTML = '';
                            const rr = info.risk_rules || {};
                            Object.keys(rr).forEach(sym => {
                                const rule = rr[sym] || {};
                                const tr = document.createElement('tr');
                                tr.innerHTML = `<td>${sym}</td><td>${rule.stop_loss_price ?? ''}</td><td>${rule.take_profit_price ?? ''}</td>`;
                                riskBody.appendChild(tr);
                            });
                        }
                    }
                } catch (err) {
                    console.error('Failed to load internal strategy info:', err);
                }
                await this.loadStrategyStatus(name, { force: true });
            } else {
                summaryWrap?.classList.add('d-none');
                ordersWrap?.classList.add('d-none');
                execWrap?.classList.remove('d-none');
                apiWrap?.classList.remove('d-none');
                summaryPane?.classList.add('d-none');
                ordersPane?.classList.add('d-none');
                if (chartsTabText) chartsTabText.textContent = 'Charts';
                this.summaryPushAwaitingData = false;
                this.data_push_state = {};
                if (this.summaryPushAwaitingTimer) {
                    clearTimeout(this.summaryPushAwaitingTimer);
                    this.summaryPushAwaitingTimer = null;
                }
                try { new bootstrap.Tab(document.getElementById('charts-tab')).show(); } catch (e) { console.warn('Tab show failed', e); }
            }

            // API Examples
            const container = document.getElementById('apiExampleContent');
            if (container) {
                if (s?.strategy_type === 'external') {
                    const endpoint = s.api_endpoint || '/api/strategy';
                    const samplePayload = { symbol: 'AAPL', action: 'BUY', quantity: 10, price: 190.12, meta: { note: 'optional' } };
                    const payloadStr = JSON.stringify(samplePayload).replace(/\"/g, '\\\"');
                    const curl = `curl -X POST \"${endpoint}\" \\\n  -H \"Content-Type: application/json\" \\\n  -d \"${payloadStr}\"`;
                    container.innerHTML = `
                      <div class="mb-2"><span class="badge bg-primary">external</span> endpoint: <code>${endpoint}</code></div>
                      <pre class="bg-light p-3"><code>${curl}</code></pre>
                      <p class="text-muted">如果在策略设置中开启鉴权，需要在请求体中加入 <code>{ token: &lt;TOKEN&gt; }</code> 字段，其中 TOKEN 来自策略设置</p>`;
                } else {
                    container.innerHTML = '<p class="text-muted">API examples are only available for external strategies.</p>';
                }
            }

            const modalElForDetails = document.getElementById('strategyDetailsModal');
            if (modalElForDetails) {
                const modal = (bootstrap.Modal.getOrCreateInstance ? bootstrap.Modal.getOrCreateInstance(modalElForDetails) : new bootstrap.Modal(modalElForDetails));
                try { modal.show(); } catch (e) { console.warn('Failed to show strategy details modal:', e); }
            }

            // 标题内联重命名：双击编辑，回车提交
            const titleNameEl = document.getElementById('detailsStrategyName');
            if (titleNameEl) {
                // 解除旧事件（克隆替换避免重复绑定）
                const newTitle = titleNameEl.cloneNode(true);
                titleNameEl.parentNode.replaceChild(newTitle, titleNameEl);
                newTitle.title = '双击可重命名';
                newTitle.style.cursor = 'text';
                newTitle.ondblclick = () => {
                    const oldName = newTitle.textContent || name;
                    const input = document.createElement('input');
                    input.type = 'text';
                    input.className = 'form-control form-control-sm d-inline-block';
                    input.style.width = Math.max(120, oldName.length * 9) + 'px';
                    input.value = oldName;
                    newTitle.replaceWith(input);
                    input.focus();
                    const commit = async () => {
                        const newName = input.value.trim();
                        if (!newName || newName === oldName) {
                            input.replaceWith(newTitle);
                            newTitle.textContent = oldName;
                            return;
                        }
                        try {
                            await this.apiCall(`/strategies/${encodeURIComponent(oldName)}/rename`, {
                                method: 'POST',
                                body: JSON.stringify({ new_name: newName, rename_file: false })
                            });
                            this.showAlert('success', `策略已重命名为 ${newName}`);
                            newTitle.textContent = newName;
                            input.replaceWith(newTitle);
                            // 刷新策略列表
                            this.strategiesManager.loadStrategies();
                        } catch (e) {
                            console.error('Rename strategy error:', e);
                            this.showAlert('danger', '重命名失败');
                            input.replaceWith(newTitle);
                            newTitle.textContent = oldName;
                        }
                    };
                    input.addEventListener('keydown', (ev) => {
                        if (ev.key === 'Enter') commit();
                        if (ev.key === 'Escape') {
                            input.replaceWith(newTitle);
                            newTitle.textContent = oldName;
                        }
                    });
                    input.addEventListener('blur', commit);
                };
            }

            const rangeSel = document.getElementById('performanceRange');
            const customSection = document.getElementById('customRangeSection');
            if (rangeSel && customSection) {
                rangeSel.value = '1d';
                rangeSel.onchange = () => {
                    customSection.style.display = rangeSel.value === 'custom' ? '' : 'none';
                    if (rangeSel.value !== 'custom') {
                        this.loadStrategyPerformance(name, { range: rangeSel.value });
                    }
                };
            }
            const applyBtn = document.getElementById('applyCustomRangeBtn');
            if (applyBtn) {
                applyBtn.onclick = () => {
                    const start = document.getElementById('perfStart').value;
                    const end = document.getElementById('perfEnd').value;
                    if (start && end) {
                        this.loadStrategyPerformance(name, { range: 'custom', start, end });
                    }
                };
            }

            // Pagination controls and export
            const pageSizeSelect = document.getElementById('executionsPageSize');
            if (pageSizeSelect) {
                pageSizeSelect.value = String(this.executionsPagination.pageSize);
                pageSizeSelect.onchange = () => {
                    this.executionsPagination.pageSize = parseInt(pageSizeSelect.value, 10) || 25;
                    this.executionsPagination.currentPage = 1;
                    this.renderExecutionsTable();
                    this.renderExecutionsPagination();
                };
            }
            const exportBtn = document.getElementById('exportExecutionsBtn');
            if (exportBtn) {
                exportBtn.onclick = () => this.exportExecutionsCsv(name);
            }

            // 删除策略按钮
            const delBtn = document.getElementById('deleteStrategyFromDetails');
            if (delBtn) {
                // 解绑旧事件
                const cloned = delBtn.cloneNode(true);
                delBtn.parentNode.replaceChild(cloned, delBtn);
                cloned.addEventListener('click', async () => {
                    const curName = document.getElementById('detailsStrategyName')?.textContent || name;
                    if (!curName) return;
                    await this.deleteStrategy(curName);
                    // 删除成功后关闭弹窗
                    const modalEl = document.getElementById('strategyDetailsModal');
                    const inst = bootstrap.Modal.getInstance(modalEl);
                    inst && inst.hide();
                });
            }
            
            // WebSocket updates executions automatically
            const refreshBtn = document.getElementById('refreshExecutionsBtn');
            if (refreshBtn) {
                refreshBtn.disabled = true;
                refreshBtn.title = 'WebSocket updates executions automatically';
            }

            const clearBtn = document.getElementById('clearExecutionsBtn');
            if (clearBtn) {
                clearBtn.onclick = async () => {
                    try {
                        const strategy = document.getElementById('detailsStrategyName')?.textContent || name;
                        if (!strategy) return;
                        if (!confirm(`确定要清空策略 "${strategy}" 的全部执行记录吗？此操作不可撤销！`)) {
                            return;
                        }
                        await this.apiCall(`/strategies/${encodeURIComponent(strategy)}/executions`, { method: 'DELETE' });
                        this.executionsPagination.allExecutions = [];
                        this.executionsPagination.totalItems = 0;
                        this.executionsPagination.currentPage = 1;
                        this.renderExecutionsTable();
                        this.renderExecutionsPagination();
                        this.showAlert('success', '已清空该策略的执行记录');
                    } catch (e) {
                        console.error('Clear executions error:', e);
                        this.showAlert('danger', '清空执行记录失败');
                    }
                };
            }

            const loaders = [
                this.loadStrategyPerformance(name, { range: '1d' }),
                this.loadStrategyExecutions(name),
                this.loadPnLCalendar(name)
            ];
            if (isInternal) {
                loaders.push(this.loadInternalStrategyOrders(name));
            }
            await Promise.all(loaders);
        } catch (e) {
            console.error('Open details error:', e);
            this.showAlert('danger', 'Failed to open strategy details');
        }
    }

    async loadStrategyPerformance(name, { range = '1d', start = null, end = null } = {}) {
        try {
            const params = new URLSearchParams();
            if (range) params.set('range', range);
            if (start) params.set('start', start);
            if (end) params.set('end', end);
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/performance?${params.toString()}`);
            if (!res.success) return;
            const m = res.metrics || {};
            const el = (id) => document.getElementById(id);
            el('kpiPnL').textContent = `$${(m.total_pnl ?? 0).toFixed(2)}`;
            el('kpiWinRate').textContent = `${(m.win_rate ?? 0).toFixed(2)}%`;
            el('kpiMaxDD').textContent = `$${(m.max_drawdown ?? 0).toFixed(2)}`;
            el('kpiSharpe').textContent = `${(m.sharpe_ratio ?? 0).toFixed(2)}`;
            el('kpiTrades').textContent = `${m.total_executions ?? 0}`;
            el('kpiAvgDur').textContent = `${m.avg_trade_duration ?? 0}`;

            // Charts from executions over selected range
            const execRes = await this.apiCall(`/strategies/${encodeURIComponent(name)}/executions?limit=1000`);
            const executions = (execRes.success ? (execRes.executions || []) : []).slice().sort((a,b)=> new Date(a.execution_time) - new Date(b.execution_time));
            const labels = [];
            const cumData = [];
            let cum = 0;
            executions.forEach(ex => {
                const pnl = Number(ex.pnl || 0);
                cum += pnl;
                labels.push(ex.execution_time ? new Date(ex.execution_time).toLocaleString() : '');
                cumData.push(cum);
            });
            this.renderLineChart('cumulativePnLChart', 'Cumulative PnL', labels, cumData, 'rgba(54, 162, 235, 1)');
            const ddData = [];
            let peak = -Infinity;
            cum = 0;
            executions.forEach(ex => {
                cum += Number(ex.pnl || 0);
                peak = Math.max(peak, cum);
                ddData.push(cum - peak);
            });
            this.renderLineChart('drawdownChart', 'Drawdown', labels, ddData, 'rgba(255, 99, 132, 1)');
            const pnls = executions.map(ex => Number(ex.pnl || 0));
            const bins = this.makeHistogram(pnls, 20);
            this.renderBarChart('pnlDistributionChart', 'PnL Distribution', bins.labels, bins.counts, 'rgba(75, 192, 192, 0.7)');
            const wins = pnls.filter(v => v > 0).length;
            const losses = pnls.filter(v => v <= 0).length;
            this.renderPieChart('winLossChart', ['Win','Loss'], [wins, losses], ['#2ecc71', '#e74c3c']);
        } catch (e) {
            console.error('Load performance error:', e);
        }
    }

    async loadStrategyExecutions(name, limit = 100) {
        try {
            const res = await this.apiCall(`/strategies/${encodeURIComponent(name)}/executions?limit=${limit}`);
            if (!res.success) return;
            const all = res.executions || [];
            const filled = all.filter(ex => String(ex.status || '').toLowerCase() === 'filled');
            this.executionsPagination.allExecutions = filled;
            this.executionsPagination.totalItems = filled.length;
            this.executionsPagination.currentPage = 1;
            this.renderExecutionsTable();
            this.renderExecutionsPagination();
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
        const tbody = document.querySelector('#executionsTable tbody');
        if (!tbody) return;
        // 同步表头为 9 列
        const theadRow = document.querySelector('#executionsTable thead tr');
        if (theadRow) {
            theadRow.innerHTML = `
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
        tbody.innerHTML = '';
        const { currentPage, pageSize, allExecutions } = this.executionsPagination;
        const start = (currentPage - 1) * pageSize;
        const end = start + pageSize;
        const pageItems = allExecutions.slice(start, end);
        pageItems.forEach(exe => {
            const tr = document.createElement('tr');
            const created = exe.created_at ? new Date(exe.created_at).toLocaleString() : (exe.execution_time ? new Date(exe.execution_time).toLocaleString() : '-');
            const commission = exe.commission != null ? Number(exe.commission).toFixed(2) : '-';
            const pnl = exe.pnl != null ? Number(exe.pnl).toFixed(2) : '-';
            const pnlClass = exe.pnl != null ? (Number(exe.pnl) > 0 ? 'text-success' : Number(exe.pnl) < 0 ? 'text-danger' : '') : '';
            tr.innerHTML = `
              <td>${exe.id ?? exe.order_id ?? '-'}</td>
              <td>${exe.symbol ?? '-'}</td>
              <td>${created}</td>
              <td>${exe.action ?? '-'}</td>
              <td>${exe.quantity ?? '-'}</td>
              <td>${exe.price != null ? exe.price : '-'}</td>
              <td><span class="badge bg-${this.getStatusColor(exe.status || '')}">${exe.status}</span></td>
              <td class="${pnlClass}">${pnl}</td>
              <td>${commission}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    renderExecutionsPagination() {
        const ul = document.getElementById('executionsPagination');
        if (!ul) return;
        const { currentPage, pageSize, totalItems } = this.executionsPagination;
        const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
        const makeLi = (label, page, disabled = false, active = false) => {
            const li = document.createElement('li');
            li.className = `page-item ${disabled ? 'disabled' : ''} ${active ? 'active' : ''}`;
            const a = document.createElement('a');
            a.className = 'page-link';
            a.href = '#';
            a.textContent = label;
            a.onclick = (e) => {
                e.preventDefault();
                if (disabled || page === currentPage) return;
                this.executionsPagination.currentPage = page;
                this.renderExecutionsTable();
                this.renderExecutionsPagination();
            };
            li.appendChild(a);
            return li;
        };

        ul.innerHTML = '';
        ul.appendChild(makeLi('«', Math.max(1, currentPage - 1), currentPage === 1));

        const windowSize = 5;
        const start = Math.max(1, currentPage - Math.floor(windowSize / 2));
        const end = Math.min(totalPages, start + windowSize - 1);
        for (let p = start; p <= end; p++) {
            ul.appendChild(makeLi(String(p), p, false, p === currentPage));
        }

        ul.appendChild(makeLi('»', Math.min(totalPages, currentPage + 1), currentPage === totalPages));
    }

    exportExecutionsCsv(name) {
        const rows = [
            ['ID','Symbol','Created At','Action','Qty','Price','Status','PnL','Commission']
        ];
        const all = this.executionsPagination.allExecutions || [];
        all.forEach(exe => {
            rows.push([
                exe.id ?? '',
                exe.symbol ?? '',
                exe.created_at ? new Date(exe.created_at).toISOString() : (exe.execution_time ? new Date(exe.execution_time).toISOString() : ''),
                exe.action ?? '',
                exe.quantity ?? '',
                exe.price ?? '',
                exe.status ?? '',
                exe.pnl ?? '',
                exe.commission ?? ''
            ]);
        });
        const csv = rows.map(r => r.map(v => typeof v === 'string' && v.includes(',') ? `"${v.replace(/"/g,'""')}"` : v).join(',')).join('\n');
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${(document.getElementById('detailsStrategyName')?.textContent || name)}_executions.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    // Chart helpers
    ensureChartDestroyed(id) {
        if (this.charts[id]) {
            try { this.charts[id].destroy(); } catch {}
            delete this.charts[id];
        }
    }

    renderLineChart(canvasId, label, labels, data, color) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        this.ensureChartDestroyed(canvasId);
        this.charts[canvasId] = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets: [{ label, data, borderColor: color, backgroundColor: color, fill: false, tension: 0.2, pointRadius: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, scales: { x: { display: true }, y: { display: true } } }
        });
    }

    renderBarChart(canvasId, label, labels, data, color) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        this.ensureChartDestroyed(canvasId);
        this.charts[canvasId] = new Chart(ctx, {
            type: 'bar',
            data: { labels, datasets: [{ label, data, backgroundColor: color }] },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    renderPieChart(canvasId, labels, data, colors) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        this.ensureChartDestroyed(canvasId);
        this.charts[canvasId] = new Chart(ctx, {
            type: 'pie',
            data: { labels, datasets: [{ data, backgroundColor: colors }] },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    makeHistogram(values, bins) {
        const min = Math.min(...values);
        const max = Math.max(...values);
        const step = (max - min) / bins;
        const labels = [];
        const counts = new Array(bins).fill(0);
        for (let i = 0; i < bins; i++) {
            labels.push(`${(min + i * step).toFixed(2)}`);
        }
        values.forEach(v => {
            const idx = Math.min(bins - 1, Math.floor((v - min) / step));
            counts[idx]++;
        });
        return { labels, counts };
    }

    async deleteStrategy(name) {
        if (!confirm(`Are you sure you want to delete strategy "${name}"?`)) {
            return;
        }
        
        try {
            await this.apiCall(`/strategies/${encodeURIComponent(name)}`, { method: 'DELETE' });
            this.showAlert('success', `Strategy ${name} deleted`);
            this.strategiesManager.loadStrategies();
        } catch (error) {
            console.error('Failed to delete strategy:', error);
            this.showAlert('danger', 'Failed to delete strategy');
        }
    }

    // Logs functionality removed - panel no longer exists

    clearData() {
        const accountInfo = document.getElementById('accountInfo');
        if (accountInfo) {
            accountInfo.innerHTML = '<p class="text-muted">Connect to view account information</p>';
        }
        
        const ordersTableBody = document.querySelector('#ordersTable tbody');
        if (ordersTableBody) {
            ordersTableBody.innerHTML = '';
        }
        
        const positionsTableBody = document.querySelector('#positionsTable tbody');
        if (positionsTableBody) {
            positionsTableBody.innerHTML = '';
        }
        
        const autoOrdersTableBody = document.querySelector('#autoOrdersTable tbody');
        if (autoOrdersTableBody) {
            autoOrdersTableBody.innerHTML = '';
        }
        
        const riskRulesTableBody = document.querySelector('#riskRulesTable2 tbody');
        if (riskRulesTableBody) {
            riskRulesTableBody.innerHTML = '';
        }
    }

    getStatusColor(status) {
        switch ((status || '').toLowerCase()) {
            // Order statuses
            case 'filled': return 'success';
            case 'cancelled':
            case 'canceled': return 'danger';
            case 'submitted':
            case 'presubmitted': return 'primary';
            case 'pendingsubmit': return 'warning';
            case 'pendingcancel': return 'info';
            case 'inactive': return 'secondary';
            case 'apiwaiting':
            case 'apipending': return 'warning';
            // Executions/general statuses
            case 'executed':
            case 'success': return 'success';
            case 'failed':
            case 'error':
            case 'rejected': return 'danger';
            case 'pending': return 'warning';
            default: return 'secondary';
        }
    }

    // 判断传入日期是否为“今天”（按本地时区）
    isSameDay(dateInput) {
        if (!dateInput) return false;
        const d = new Date(dateInput);
        if (isNaN(d.getTime())) return false;
        const now = new Date();
        return d.getFullYear() === now.getFullYear() &&
               d.getMonth() === now.getMonth() &&
               d.getDate() === now.getDate();
    }

    // Symbol combobox functionality
    initSymbolCombobox() {
        this.setupSymbolInput('orderSymbol', 'orderSymbolDropdown');
    }

    // Duplicate setupSymbolInput method removed

    setActiveDropdownItem(items, activeIndex) {
        items.forEach((item, index) => {
            item.classList.toggle('active', index === activeIndex);
        });
    }

    showHistoricalSymbols(dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        const historicalSymbols = this.getHistoricalSymbols();
        
        let html = '';
        
        if (historicalSymbols.length > 0) {
            html += '<h6 class="dropdown-header">Recent Symbols</h6>';
            historicalSymbols.forEach(symbol => {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            });
            html += '<div class="dropdown-divider"></div>';
        }
        
        html += '<h6 class="dropdown-header">Popular Symbols</h6>';
        const popularSymbols = ['MNQ', 'NQ', 'ES', 'YM', 'AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA', 'AMZN'];
        popularSymbols.forEach(symbol => {
            if (!historicalSymbols.includes(symbol)) {
                html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
            }
        });
        
        dropdown.innerHTML = html;
        
        // Add click handlers
        dropdown.querySelectorAll('.dropdown-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const symbol = e.target.getAttribute('data-symbol');
                const inputId = dropdown.dataset.inputId || (dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol');
                const inputEl = document.getElementById(inputId);
                if (inputEl) {
                    inputEl.value = symbol;
                    const changeEvent = new Event('change', { bubbles: true });
                    inputEl.dispatchEvent(changeEvent);
                }
                dropdown.classList.remove('show');
                this.addToHistoricalSymbols(symbol);

                // 如果是下单弹窗的symbol选择，获取当前价格
                if (inputId === 'orderSymbol') {
                    this.fetchCurrentPrice(symbol);
                }
            });
        });
    }

    async searchSymbols(query, dropdownId) {
        const dropdown = document.getElementById(dropdownId);
        if (!dropdown) return;

        try {
            // Show loading
            dropdown.innerHTML = '<div class="dropdown-item-text">Searching...</div>';
            dropdown.classList.add('show');
            
            // For now, we'll use a simple client-side filter of common symbols
            // In a real implementation, this would call an API
            const allSymbols = [
                'MNQ', 'NQ', 'ES', 'YM', 'RTY', 'CL', 'GC', 'SI',
                'AAPL', 'MSFT', 'GOOGL', 'GOOG', 'TSLA', 'NVDA', 'AMZN', 'META',
                'NFLX', 'AMD', 'INTC', 'CRM', 'ORCL', 'ADBE', 'PYPL', 'UBER',
                'SPOT', 'ZM', 'DOCU', 'SNOW', 'PLTR', 'COIN', 'RBLX', 'HOOD'
            ];
            
            const filteredSymbols = allSymbols.filter(symbol => 
                symbol.toLowerCase().includes(query.toLowerCase())
            );
            
            let html = '';
            
            if (filteredSymbols.length > 0) {
                html += '<h6 class="dropdown-header">Search Results</h6>';
                filteredSymbols.slice(0, 10).forEach(symbol => {
                    html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
                });
            } else {
                html += '<div class="dropdown-item-text text-muted">No symbols found</div>';
            }
            
            dropdown.innerHTML = html;
            
            // Add click handlers
            dropdown.querySelectorAll('.dropdown-item').forEach(item => {
                item.addEventListener('click', (e) => {
                    e.preventDefault();
                    const symbol = e.target.getAttribute('data-symbol');
                    const inputId = dropdown.dataset.inputId || (dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol');
                    const inputEl = document.getElementById(inputId);
                    if (inputEl) {
                        inputEl.value = symbol;
                        const changeEvent = new Event('change', { bubbles: true });
                        inputEl.dispatchEvent(changeEvent);
                    }
                    dropdown.classList.remove('show');
                    this.addToHistoricalSymbols(symbol);

                    // 如果是下单弹窗的symbol选择，获取当前价格
                    if (inputId === 'orderSymbol') {
                        this.fetchCurrentPrice(symbol);
                    }
                });
            });
            
        } catch (error) {
            console.error('Error searching symbols:', error);
            dropdown.innerHTML = '<div class="dropdown-item-text text-danger">Search failed</div>';
        }
    }

    getHistoricalSymbols() {
        const stored = localStorage.getItem('historicalSymbols');
        return stored ? JSON.parse(stored) : [];
    }

    addToHistoricalSymbols(symbol) {
        let historical = this.getHistoricalSymbols();
        
        // Remove if already exists
        historical = historical.filter(s => s !== symbol);
        
        // Add to beginning
        historical.unshift(symbol);
        
        // Keep only last 10
        historical = historical.slice(0, 10);
        
        localStorage.setItem('historicalSymbols', JSON.stringify(historical));
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
        try {
            // 获取全部订单数据
            const ordersData = await this.apiCall('/orders');

            // 构建持仓映射，用于计算PnL
            const positions = (this.dataManager.getAllPositions && this.dataManager.getAllPositions()) || [];
            const posMap = {};
            (positions || []).forEach(p => { if (p && p.symbol) posMap[String(p.symbol).toUpperCase()] = p; });

            const target = String(symbol || '').toUpperCase();
            
            // 过滤：来源为风险规则（sl/tp 或 trigger_type=risk_rule），且 symbol 或 rule_id 与目标匹配；不再限制“仅今日”
            const riskRuleOrders = (ordersData.orders || []).filter(order => {
                const os = String(order.order_source || '').toLowerCase();
                const isRisk = os === 'sl' || os === 'tp' || String(order.trigger_type || '').toLowerCase() === 'risk_rule';
                const symU = String(order.symbol || '').toUpperCase();
                const ridU = String(order.rule_id || '').toUpperCase();
                const isFilled = String(order.status || '').toLowerCase() === 'filled';
                return isRisk && (symU === target || ridU === target) && isFilled;
            }).sort((a,b) => new Date(b.submit_time || b.created_at || 0) - new Date(a.submit_time || a.created_at || 0));

            // 计算统计数据
            let totalOrders = riskRuleOrders.length;
            let totalProfit = 0;
            let totalLoss = 0;

            riskRuleOrders.forEach(order => {
                const pos = posMap[String(order.symbol || '').toUpperCase()];
                const avgCost = pos ? pos.avg_cost : 0;
                if (String(order.status || '').toLowerCase() === 'filled' && order.avg_fill_price && avgCost > 0) {
                    let pnlPerShare = 0;
                    if (order.action === 'SELL') pnlPerShare = order.avg_fill_price - avgCost; else if (order.action === 'BUY') pnlPerShare = avgCost - order.avg_fill_price;
                    const totalPnl = pnlPerShare * (order.quantity || 0);
                    if (totalPnl > 0) totalProfit += totalPnl; else totalLoss += Math.abs(totalPnl);
                }
            });

            // 更新规则信息
            document.getElementById('ruleInfoText').textContent = `${symbol} 风险规则触发的订单`;
            
            // 更新统计卡片
            document.getElementById('totalOrdersCount').textContent = totalOrders;
            document.getElementById('totalProfit').textContent = '$' + this.formatNumber(totalProfit);
            document.getElementById('totalLoss').textContent = '$' + this.formatNumber(totalLoss);

            // 渲染订单表格
            const tbody = document.querySelector('#riskRuleOrdersTable tbody');
            tbody.innerHTML = (riskRuleOrders || []).map(order => {
                // 确定触发类型显示文本
                let triggerTypeText = '风险规则';
                let triggerTypeClass = 'bg-warning';
                if (order.order_source === 'sl') { triggerTypeText = '止损'; triggerTypeClass = 'bg-danger'; }
                else if (order.order_source === 'tp') { triggerTypeText = '止盈'; triggerTypeClass = 'bg-success'; }
                else if (order.order_source === 'strategy') { triggerTypeText = '策略'; triggerTypeClass = 'bg-info'; }
                else if (order.order_source === 'auto' || order.order_source === '机器人') { triggerTypeText = '自动'; triggerTypeClass = 'bg-secondary'; }
                else if (String(order.trigger_type || '').toLowerCase() === 'risk_rule') { triggerTypeText = '风险规则'; triggerTypeClass = 'bg-warning'; }
                else { triggerTypeText = order.order_source || '手动'; triggerTypeClass = 'bg-info'; }

                // 价格显示
                let priceDisplay = '';
                if (order.order_type === 'MKT' || order.order_type === 'MARKET') {
                    priceDisplay = order.avg_fill_price && order.avg_fill_price > 0 ? this.formatNumber(order.avg_fill_price) : 'Market';
                } else {
                    priceDisplay = order.limit_price ? this.formatNumber(order.limit_price) : 'N/A';
                }

                const pos = posMap[String(order.symbol || '').toUpperCase()];
                const avgCost = pos ? pos.avg_cost : 0;
                let orderPnl = 0;
                if (String(order.status || '').toLowerCase() === 'filled' && order.avg_fill_price && avgCost > 0) {
                    let pnlPerShare = 0;
                    if (order.action === 'SELL') pnlPerShare = order.avg_fill_price - avgCost; else if (order.action === 'BUY') pnlPerShare = avgCost - order.avg_fill_price;
                    orderPnl = pnlPerShare * (order.quantity || 0);
                }
                const pnlClass = orderPnl > 0 ? 'text-success' : orderPnl < 0 ? 'text-danger' : 'text-muted';
                const pnlText = `$${this.formatNumber(orderPnl)}`;
                const executionPrice = order.avg_fill_price || 0;

                return `
                <tr>
                    <td>${order.order_id}</td>
                    <td>${new Date(order.submit_time || order.created_at).toLocaleString()}</td>
                    <td>${order.symbol}</td>
                    <td><span class="badge bg-${order.action === 'BUY' ? 'success' : 'danger'}">${order.action}</span></td>
                    <td>${order.quantity}</td>
                    <td>$${priceDisplay}</td>
                    <td><span class="badge bg-${this.getStatusColor(order.status)}">${order.status}</span></td>
                    <td>$${this.formatNumber(executionPrice)}</td>
                    <td class="${pnlClass}">${pnlText}</td>
                    <td><span class="badge ${triggerTypeClass}">${triggerTypeText}</span></td>
                </tr>
                `;
            }).join('');

            if ((riskRuleOrders || []).length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="10" class="text-center text-muted">暂无该风险规则关联的订单</td>
                    </tr>
                `;
            }

            const modalEl = document.getElementById('riskRuleOrdersModal');
            const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
            modal.show();

        } catch (error) {
            console.error('Failed to load risk rule orders:', error);
            this.showAlert('danger', '加载风险规则订单详情时发生错误');
        }
    }

    // S Rules (Sizing Policy) related functions


    async loadSizingHistory() {
        try {
            const data = await this.apiCall('/sizing/history?limit=50');
            const history = data.data || [];
            
            const tbody = document.querySelector('#sizingHistoryTable tbody');
            if (!tbody) return;
            
            tbody.innerHTML = history.map(item => {
                const timeValue = item.sent_at || item.created_at || item.timestamp;
            const timestamp = timeValue ? new Date(timeValue).toLocaleString() : '-';
                return `
                <tr>
                    <td>${timestamp}</td>
                    <td>${item.symbol}</td>
                    <td><span class="badge bg-${item.action === 'BUY' ? 'success' : 'danger'}">${item.action}</span></td>
                    <td>${item.suggested_quantity}</td>
                    <td>${item.sizing_method}</td>
                    <td>${item.current_price !== null && item.current_price !== undefined ? '$' + this.formatNumber(item.current_price) : 'N/A'}</td>
                </tr>
                `;
            }).join('');
            
            if (history.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="6" class="text-center text-muted">
                            No sizing history available
                        </td>
                    </tr>
                `;
            }
        } catch (error) {
            console.error('Failed to load sizing history:', error);
        }
    }
    
    // Suggested Position Size functions
    async refreshPositionSuggestion() {
        const symbol = document.getElementById('orderSymbol')?.value?.trim();
        const action = document.getElementById('orderAction')?.value;
        const priceInput = document.getElementById('orderPrice')?.value?.trim();
        const price = parseFloat(priceInput) || 0;
        const stopLoss = parseFloat(document.getElementById('stopLossPrice')?.value) || 0;
    
    console.log('refreshPositionSuggestion called with:', { symbol, action, price, stopLoss });
    console.log('stopLossPrice element value:', document.getElementById('stopLossPrice')?.value);
        
        // Note: Position sizing calculation doesn't require IB connection
        // Only actual order placement requires connection
        
        if (!symbol || !action) {
            console.log('Missing symbol or action, clearing suggestion');
            this.clearPositionSuggestion();
            return;
        }
        
        // If no price is provided, try to get current market price
        let effectivePrice = price;
        if (!effectivePrice || effectivePrice <= 0) {
            console.log('No price provided, attempting to get current market price for', symbol);
            try {
                const position = this.dataManager.getPosition(symbol);
                if (position && position.current_price > 0) {
                    effectivePrice = position.current_price;
                    console.log('Using current market price:', effectivePrice);
                }
            } catch (error) {
                console.log('Could not get market price:', error);
            }

            // If still no price, show error and return
            if (!effectivePrice || effectivePrice <= 0) {
                console.log('No valid price available, clearing suggestion');
                this.showAlert('warning', '请输入价格或确保该股票有当前市场价格');
                this.clearPositionSuggestion();
                return;
            }
        }
        
        // Calculate stop_distance based on action and stop loss
        let stopDistance = 0;
        console.log('Calculating stopDistance - stopLoss:', stopLoss, 'effectivePrice:', effectivePrice, 'action:', action);
        
        if (stopLoss > 0) {
            if (action === 'BUY') {
                stopDistance = Math.abs(effectivePrice - stopLoss);
            } else if (action === 'SELL') {
                stopDistance = Math.abs(stopLoss - effectivePrice);
            }
            console.log('Calculated stopDistance from SL price:', stopDistance);
        } else {
            // Default stop distance based on symbol type
            if (symbol.includes('ES') || symbol.includes('NQ') || symbol.includes('YM') || symbol.includes('RTY')) {
                // For futures, use a smaller default stop distance (e.g., 10 points)
                stopDistance = 10;
                console.log('Using default futures stopDistance:', stopDistance);
            } else {
                // For stocks, use 2% of price as default
                stopDistance = effectivePrice * 0.02;
                console.log('Using default stock stopDistance:', stopDistance);
            }
        }
        
        if (stopDistance <= 0) {
            this.clearPositionSuggestion();
            return;
        }
        
        try {
            const data = await this.apiCall('/position-sizing', {
                method: 'POST',
                body: JSON.stringify({
                    symbol: symbol,
                    stop_distance: stopDistance
                })
            });
            
            this.updatePositionSuggestionUI(data);
            
            // Refresh sizing history table after successful calculation
            this.loadSizingHistory();
        } catch (error) {
            console.error('Failed to get position suggestion:', error);
            this.clearPositionSuggestion();
        }
    }
    
    debounceRefreshSuggestion() {
        clearTimeout(this.suggestionTimeout);
        this.suggestionTimeout = setTimeout(() => {
            this.refreshPositionSuggestion();
        }, 500);
    }
    
    updatePositionSuggestionUI(data) {
        const suggestedQuantity = document.getElementById('suggestedQuantity');
        const sizingMethod = document.getElementById('sizingMethod');
        const estimatedInvestment = document.getElementById('estimatedInvestment');
        const maxRisk = document.getElementById('maxRisk');
        const stopDistance = document.getElementById('stopDistance');
        const pointValue = document.getElementById('pointValue');
        const scaleTotal = document.getElementById('scaleTotal');
        const scaleVol = document.getElementById('scaleVol');
        const scaleDd = document.getElementById('scaleDd');
        const scaleSig = document.getElementById('scaleSig');
        const scaleExec = document.getElementById('scaleExec');
        const sizingExplanation = document.getElementById('sizingExplanation');
        
        console.log('Updating position suggestion UI with data:', data);
        
        // Extract data from the response - data structure: {N: number, explain: {...}}
        const contracts = data.N || 0;
        const explain = data.explain || {};
        
        if (suggestedQuantity) suggestedQuantity.textContent = contracts || '--';
        if (sizingMethod) sizingMethod.textContent = 'Position Sizing';
        if (estimatedInvestment) estimatedInvestment.textContent = data.estimatedInvestment ? `$${data.estimatedInvestment.toFixed(2)}` : '--';
        if (maxRisk) maxRisk.textContent = data.maxRisk ? `$${data.maxRisk.toFixed(2)}` : '--';
        if (stopDistance) stopDistance.textContent = data.stopDistance ? data.stopDistance.toFixed(4) : '--';
        if (pointValue) pointValue.textContent = data.pointValue ? data.pointValue.toFixed(2) : '--';
        if (scaleTotal) scaleTotal.textContent = data.scaleTotal ? data.scaleTotal.toFixed(4) : '--';
        if (scaleVol) scaleVol.textContent = data.scaleVol ? data.scaleVol.toFixed(4) : '--';
        if (scaleDd) scaleDd.textContent = data.scaleDd ? data.scaleDd.toFixed(4) : '--';
        if (scaleSig) scaleSig.textContent = data.scaleSig ? data.scaleSig.toFixed(4) : '--';
        if (scaleExec) scaleExec.textContent = data.scaleExec ? data.scaleExec.toFixed(4) : '--';
        if (sizingExplanation) sizingExplanation.textContent = explain || '--';
        
        // Store suggested quantity for apply button
        this.lastSuggestedQuantity = contracts;
    }
    
    clearPositionSuggestion() {
        const elements = [
            'suggestedQuantity', 'sizingMethod', 'estimatedInvestment', 'maxRisk',
            'stopDistance', 'pointValue', 'scaleTotal', 'scaleVol', 'scaleDd', 
            'scaleSig', 'scaleExec', 'sizingExplanation'
        ];
        elements.forEach(id => {
            const element = document.getElementById(id);
            if (element) element.textContent = '--';
        });
        this.lastSuggestedQuantity = null;
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
        try {
            const response = await this.apiCall('/sizing/history?limit=1');
            if (response.success && response.data && response.data.length > 0) {
                const latest = response.data[0];
                const gStatus = latest.g_rules_status || {};
                const sStatus = latest.s_rules_status || {};
                const lastCheck = latest.timestamp;
                
                // 更新G Rules状态表格
                this.updateRuleStatus('g0', gStatus.g0 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('g1', gStatus.g1 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('g2', gStatus.g2 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('g3', gStatus.g3 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('g4', gStatus.g4 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('g5', gStatus.g5 || { passed: true, message: '正常' }, lastCheck);
                
                // 更新S Rules状态表格
                this.updateRuleStatus('s0', sStatus.s0 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('s1', sStatus.s1 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('s2', sStatus.s2 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('s3', sStatus.s3 || { passed: true, message: '正常' }, lastCheck);
                this.updateRuleStatus('s4', sStatus.s4 || { passed: true, message: '正常' }, lastCheck);
            }
        } catch (error) {
            console.error('加载 G&S Rules 状态失败:', error);
        }
    }

    updateRuleStatus(ruleId, status, lastCheck) {
        const statusElement = document.getElementById(`${ruleId}_status`);
        const messageElement = document.getElementById(`${ruleId}_message`);
        const lastElement = document.getElementById(`${ruleId}_last`);
        
        if (statusElement) {
            statusElement.className = `badge ${status.passed ? 'bg-success' : 'bg-danger'}`;
            statusElement.textContent = status.passed ? '通过' : '违规';
        }
        
        if (messageElement) {
            messageElement.textContent = status.message || (status.passed ? '正常' : '违规');
        }
        
        if (lastElement) {
            const ts = lastCheck || status?.timestamp || status?.lastCheck;
            if (ts) {
                try {
                    lastElement.textContent = new Date(ts).toLocaleString();
                } catch (_) {
                    lastElement.textContent = ts;
                }
            }
        }
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
        const positionMethod = document.getElementById('s0PositionSizing')?.value;
        const positionValueLabel = document.querySelector('label[for="s1PositionValue"]');
        const positionValueInput = document.getElementById('s1PositionValue');
        const positionValueHelp = document.getElementById('s1PositionValue')?.parentNode?.querySelector('.form-text');
        
        if (!positionValueLabel || !positionValueInput) return;
        
        switch (positionMethod) {
            case 'percentage':
                positionValueLabel.textContent = 'Position Percentage (%)';
                positionValueInput.placeholder = '例如: 2.5 (表示2.5%)';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '每个仓位占总资金的百分比';
                }
                break;
            case 'fixed':
                positionValueLabel.textContent = 'Fixed Amount ($)';
                positionValueInput.placeholder = '例如: 1000 (表示$1000)';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '每个仓位的固定金额';
                }
                break;
            case 'shares':
                positionValueLabel.textContent = 'Number of Shares';
                positionValueInput.placeholder = '例如: 100 (表示100股)';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '每个仓位的固定股数';
                }
                break;
            default:
                positionValueLabel.textContent = 'Position Value';
                positionValueInput.placeholder = '';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '';
                }
        }
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
        // 每30秒自动刷新订单状态
        this.ordersRefreshInterval = setInterval(() => {
            // 只有在Orders标签页激活时才刷新
            const ordersTab = document.querySelector('#orders-tab');
            if (ordersTab && ordersTab.classList.contains('active')) {
                console.log('Auto-refreshing orders status...');
                this.loadOrders();
            }
        }, 30000); // 30秒间隔
        
        console.log('Orders auto-refresh started (30s interval)');
    }

    // 停止订单状态自动刷新
    stopOrdersAutoRefresh() {
        if (this.ordersRefreshInterval) {
            clearInterval(this.ordersRefreshInterval);
            this.ordersRefreshInterval = null;
            console.log('Orders auto-refresh stopped');
        }
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