(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const utils = appModules.utils = appModules.utils || {};

    function slugifyStrategyName(name) {
        return (name || '')
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '_')
            .replace(/_+/g, '_')
            .replace(/^_+|_+$/g, '');
    }

    const TrendSignalLabelPlugin = {
        id: 'trendSignalLabel',
        afterDatasetsDraw(chart, args, opts) {
            const dataset = chart.data?.datasets?.[0];
            if (!dataset) return;
            const meta = chart.getDatasetMeta(0);
            if (!meta || !meta.data) return;

            const monitoringUtils = appModules.monitoring?.utils || {};
            const getTrendLevelStyle = monitoringUtils.getTrendLevelStyle
                || (() => ({}));
            const getTrendSuggestionLabel = monitoringUtils.getTrendSuggestionLabel
                || (() => '');

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
                const style = getTrendLevelStyle(level) || {};
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

    function formatNumber(num) {
        if (num === null || num === undefined || num === '') {
            return 'N/A';
        }
        const numeric = Number(num);
        if (!Number.isFinite(numeric)) {
            return 'N/A';
        }
        return numeric.toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
    }

    function escapeHtml(value) {
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

    function formatDuration(seconds) {
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

    function isSameDay(dateInput, referenceDate) {
        if (!dateInput) return false;
        const date = new Date(dateInput);
        if (Number.isNaN(date.getTime())) return false;

        const compareTarget = referenceDate instanceof Date ? referenceDate : new Date();
        return date.getFullYear() === compareTarget.getFullYear()
            && date.getMonth() === compareTarget.getMonth()
            && date.getDate() === compareTarget.getDate();
    }

    utils.slugifyStrategyName = slugifyStrategyName;
    utils.TrendSignalLabelPlugin = TrendSignalLabelPlugin;
    utils.formatNumber = formatNumber;
    utils.escapeHtml = escapeHtml;
    utils.formatDuration = formatDuration;
    utils.isSameDay = isSameDay;

    if (root && typeof root === 'object') {
        root.slugifyStrategyName = root.slugifyStrategyName || slugifyStrategyName;
        root.formatNumber = root.formatNumber || formatNumber;
    }
})(typeof window !== 'undefined' ? window : globalThis);
