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

    utils.slugifyStrategyName = slugifyStrategyName;
    utils.TrendSignalLabelPlugin = TrendSignalLabelPlugin;

    if (root && typeof root === 'object') {
        root.slugifyStrategyName = slugifyStrategyName;
    }
})(typeof window !== 'undefined' ? window : globalThis);
