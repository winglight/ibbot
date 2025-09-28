(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const monitoring = appModules.monitoring = appModules.monitoring || {};
    const constants = monitoring.constants || {};

    const {
        INTERVAL_MINUTES_MAP,
        MONITOR_DURATION_MINUTES_MAP,
        MONITOR_DURATION_OPTIONS,
        TREND_LEVEL_STYLES,
        MONITOR_EVENT_SIDE_COLORS
    } = constants;

    function normalizeIntervalKey(value) {
        if (typeof value === 'string') {
            return value.replace(/\s+/g, '').toLowerCase();
        }
        if (typeof value === 'number') {
            return String(value);
        }
        return '';
    }

    function normalizeDurationKey(value) {
        if (typeof value === 'string') {
            return value.replace(/\s+/g, '').toUpperCase();
        }
        if (typeof value === 'number') {
            return String(value);
        }
        return '';
    }

    function getMonitorDurationMinutes(durationValue) {
        const normalized = normalizeDurationKey(durationValue);
        if (!normalized) return null;

        if (Object.prototype.hasOwnProperty.call(MONITOR_DURATION_MINUTES_MAP || {}, normalized)) {
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
    }

    function getMonitorDurationOptions(intervalValue) {
        const normalizedKey = normalizeIntervalKey(intervalValue) || '';
        const trimmedKey = typeof intervalValue === 'string'
            ? intervalValue.trim().toLowerCase()
            : '';
        return MONITOR_DURATION_OPTIONS?.[normalizedKey]
            || MONITOR_DURATION_OPTIONS?.[trimmedKey]
            || MONITOR_DURATION_OPTIONS?.['1 min']
            || [];
    }

    function refreshMonitorDurationSelect(intervalValue, options = {}) {
        const selectEl = options.selectEl || document.getElementById('monitorDuration');
        if (!selectEl) return [];

        const availableOptions = getMonitorDurationOptions(intervalValue);
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

        const hasPreferred = preferredValue
            && availableOptions.some((option) => option.value === preferredValue);
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
    }

    function getTrendLevelStyle(level) {
        const key = Number.isFinite(level) ? Number(level) : 0;
        return TREND_LEVEL_STYLES?.[key] || TREND_LEVEL_STYLES?.[0] || {};
    }

    function getTrendSuggestionLabel(suggestion, level) {
        if (!suggestion) return '';
        const numericLevel = Number(level);
        const resolvedLevel = Number.isFinite(numericLevel)
            ? Math.min(4, Math.max(1, Math.round(numericLevel)))
            : 1;
        if (suggestion === 'buy') return `B${resolvedLevel}`;
        if (suggestion === 'sell') return `S${resolvedLevel}`;
        return '';
    }

    monitoring.utils = {
        normalizeIntervalKey,
        normalizeDurationKey,
        getMonitorDurationMinutes,
        getMonitorDurationOptions,
        refreshMonitorDurationSelect,
        getTrendLevelStyle,
        getTrendSuggestionLabel,
        INTERVAL_MINUTES_MAP,
        MONITOR_EVENT_SIDE_COLORS
    };

    if (root && typeof root === 'object') {
        root.getTrendLevelStyle = getTrendLevelStyle;
        root.getTrendSuggestionLabel = getTrendSuggestionLabel;
    }
})(typeof window !== 'undefined' ? window : globalThis);
