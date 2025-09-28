(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const monitoring = appModules.monitoring = appModules.monitoring || {};

    const INTERVAL_MINUTES_MAP = {
        '1min': 1,
        '5min': 5,
        '5mins': 5,
        '15min': 15,
        '15mins': 15,
        '30mins': 30,
        '1hour': 60,
        '1day': 1440
    };

    const MONITOR_SOURCE_TZ = 'America/New_York';
    const MONITOR_FALLBACK_TZ = 'Asia/Shanghai';
    const MONITOR_CHART_MAX_MINUTES = 30;
    const MONITOR_CHART_MAX_POINTS = 3000;

    const MONITOR_DURATION_MINUTES_MAP = {
        '30M': 30,
        '1H': 60,
        '4H': 240,
        '1D': 1440,
        '2D': 2880,
        '5D': 7200,
        '1W': 10080,
        '1M': 43200,
        '3M': 129600,
        '1Y': 525600
    };

    const baseDurationOptions = {
        '1 min': [
            { value: '30 M', label: '30 分钟' },
            { value: '1 H', label: '1 小时' },
            { value: '4 H', label: '4 小时' },
            { value: '1 D', label: '1 天' }
        ],
        '5 mins': [
            { value: '30 M', label: '30 分钟' },
            { value: '1 H', label: '1 小时' },
            { value: '4 H', label: '4 小时' },
            { value: '1 D', label: '1 天' },
            { value: '1 W', label: '1 周' }
        ],
        '15 mins': [
            { value: '1 H', label: '1 小时' },
            { value: '4 H', label: '4 小时' },
            { value: '1 D', label: '1 天' },
            { value: '1 W', label: '1 周' },
            { value: '1 M', label: '1 个月' }
        ],
        '30 mins': [
            { value: '4 H', label: '4 小时' },
            { value: '1 D', label: '1 天' },
            { value: '1 W', label: '1 周' },
            { value: '1 M', label: '1 个月' },
            { value: '3 M', label: '3 个月' }
        ],
        '1 hour': [
            { value: '1 D', label: '1 天' },
            { value: '1 W', label: '1 周' },
            { value: '1 M', label: '1 个月' },
            { value: '3 M', label: '3 个月' },
            { value: '1 Y', label: '1 年' }
        ],
        '1 day': [
            { value: '1 W', label: '1 周' },
            { value: '1 M', label: '1 个月' },
            { value: '3 M', label: '3 个月' },
            { value: '1 Y', label: '1 年' }
        ]
    };

    const MONITOR_DURATION_OPTIONS = Object.assign({}, baseDurationOptions, {
        '1min': baseDurationOptions['1 min'],
        '5min': baseDurationOptions['5 mins'],
        '5 mins': baseDurationOptions['5 mins'],
        '15min': baseDurationOptions['15 mins'],
        '15 mins': baseDurationOptions['15 mins'],
        '30min': baseDurationOptions['30 mins'],
        '30 mins': baseDurationOptions['30 mins'],
        '1hour': baseDurationOptions['1 hour'],
        '1 hour': baseDurationOptions['1 hour'],
        '1day': baseDurationOptions['1 day'],
        '1 day': baseDurationOptions['1 day']
    });

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

    const MONITOR_EVENT_SIDE_COLORS = {
        buy: '#22c55e',
        sell: '#ef4444',
        neutral: '#64748b'
    };

    monitoring.constants = {
        INTERVAL_MINUTES_MAP,
        MONITOR_SOURCE_TZ,
        MONITOR_FALLBACK_TZ,
        MONITOR_CHART_MAX_MINUTES,
        MONITOR_CHART_MAX_POINTS,
        MONITOR_DURATION_MINUTES_MAP,
        MONITOR_DURATION_OPTIONS,
        TREND_LEVEL_STYLES,
        MONITOR_EVENT_SIDE_COLORS
    };
})(typeof window !== 'undefined' ? window : globalThis);
