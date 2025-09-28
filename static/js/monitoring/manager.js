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
        }
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
            if (this.app && typeof this.app.normalizeMonitorTime === 'function') {
                return this.app.normalizeMonitorTime(value);
            }
            try {
                if (value == null) return null;
                if (value instanceof Date) return value.getTime();
                if (typeof value === 'number') {
                    return Number.isFinite(value) ? value : null;
                }
                if (this.app && typeof this.app.parseMonitorChartTime === 'function') {
                    const parsedFromApp = this.app.parseMonitorChartTime(value);
                    if (parsedFromApp && !Number.isNaN(parsedFromApp.getTime())) {
                        return parsedFromApp.getTime();
                    }
                }
                const parsed = new Date(value);
                return Number.isNaN(parsed.getTime()) ? null : parsed.getTime();
            } catch (e) {
                return null;
            }
        }
    }

    monitoring.manager = monitoring.manager || {};
    monitoring.manager.MonitoringManager = MonitoringManager;
    root.MonitoringManager = MonitoringManager;
})(typeof window !== 'undefined' ? window : globalThis);
