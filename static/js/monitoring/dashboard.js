(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const monitoring = appModules.monitoring = appModules.monitoring || {};

    const REFRESH_STATE = {
        idle: 'idle',
        loading: 'loading',
        error: 'error',
    };

    const COLORS = [
        '#60a5fa', '#34d399', '#fbbf24', '#f97316', '#f87171', '#a855f7', '#22d3ee',
        '#2dd4bf', '#c084fc', '#f472b6', '#eab308', '#818cf8', '#ef4444', '#0ea5e9'
    ];

    class MonitoringDashboard {
        constructor(app) {
            this.app = app;
            this.queueRefreshInterval = 15000;
            this.influxRefreshInterval = 60000;
            this.queueTimer = null;
            this.influxTimer = null;
            this.influxChart = null;
            this.influxData = [];
            this.state = {
                queue: REFRESH_STATE.idle,
                influx: REFRESH_STATE.idle,
            };

            this.elements = {
                queueSourceBadge: document.getElementById('queueMetricsSource'),
                queueUpdatedAt: document.getElementById('queueMetricsUpdatedAt'),
                queueError: document.getElementById('queueMetricsError'),
                queueTableBody: document.querySelector('#queueMetricsTable tbody'),
                streamTableBody: document.querySelector('#streamMetricsTable tbody'),
                influxSummary: document.getElementById('influxSummaryText'),
                influxWindow: document.getElementById('influxWindowSelect'),
                influxService: document.getElementById('influxServiceSelect'),
                influxMetric: document.getElementById('influxMetricSelect'),
                queueRefreshBtn: document.getElementById('refreshQueueMetricsBtn'),
                influxRefreshBtn: document.getElementById('refreshInfluxMetricsBtn'),
            };
        }

        init() {
            this.bindEvents();
            this.refreshQueueMetrics({ immediate: true }).catch(() => {});
            this.refreshInfluxMetrics({ immediate: true }).catch(() => {});
        }

        bindEvents() {
            if (this.elements.queueRefreshBtn) {
                this.elements.queueRefreshBtn.addEventListener('click', () => {
                    this.refreshQueueMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.influxRefreshBtn) {
                this.elements.influxRefreshBtn.addEventListener('click', () => {
                    this.refreshInfluxMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.influxWindow) {
                this.elements.influxWindow.addEventListener('change', () => {
                    this.refreshInfluxMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.influxService) {
                this.elements.influxService.addEventListener('change', () => {
                    this.refreshInfluxMetrics({ manual: true }).catch(() => {});
                });
            }
            if (this.elements.influxMetric) {
                this.elements.influxMetric.addEventListener('change', () => {
                    this.refreshInfluxMetrics({ manual: true }).catch(() => {});
                });
                this.elements.influxMetric.addEventListener('keyup', (event) => {
                    if (event.key === 'Enter') {
                        this.refreshInfluxMetrics({ manual: true }).catch(() => {});
                    }
                });
            }
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) {
                    this.clearTimers();
                } else {
                    this.refreshQueueMetrics().catch(() => {});
                    this.refreshInfluxMetrics().catch(() => {});
                }
            });
        }

        clearTimers() {
            if (this.queueTimer) {
                clearTimeout(this.queueTimer);
                this.queueTimer = null;
            }
            if (this.influxTimer) {
                clearTimeout(this.influxTimer);
                this.influxTimer = null;
            }
        }

        scheduleQueueRefresh() {
            if (this.queueTimer || document.hidden) return;
            this.queueTimer = setTimeout(() => {
                this.queueTimer = null;
                this.refreshQueueMetrics().catch(() => {});
            }, this.queueRefreshInterval);
        }

        scheduleInfluxRefresh() {
            if (this.influxTimer || document.hidden) return;
            this.influxTimer = setTimeout(() => {
                this.influxTimer = null;
                this.refreshInfluxMetrics().catch(() => {});
            }, this.influxRefreshInterval);
        }

        async refreshQueueMetrics({ manual = false, immediate = false } = {}) {
            if (this.state.queue === REFRESH_STATE.loading && !manual) {
                this.scheduleQueueRefresh();
                return;
            }

            this.state.queue = REFRESH_STATE.loading;
            if (this.elements.queueError) {
                this.elements.queueError.textContent = '';
            }

            const params = new URLSearchParams();
            // Allow custom queue targets via configuration in the future.

            try {
                const data = await this.app.apiCall(`/monitoring/queues?${params.toString()}`, {
                    useCache: false,
                    cacheTime: 0,
                    timeout: manual ? 10000 : 15000,
                });
                this.renderQueueMetrics(data || {});
                this.state.queue = REFRESH_STATE.idle;
            } catch (error) {
                console.error('Failed to load queue metrics', error);
                this.state.queue = REFRESH_STATE.error;
                if (this.elements.queueError) {
                    this.elements.queueError.textContent = error?.message || '加载失败';
                }
            } finally {
                if (!immediate) {
                    this.scheduleQueueRefresh();
                }
            }
        }

        formatNumber(value, options = {}) {
            const numeric = Number(value);
            if (!Number.isFinite(numeric)) {
                return '—';
            }
            if (options.maximumFractionDigits !== undefined || options.minimumFractionDigits !== undefined) {
                try {
                    return numeric.toLocaleString('en-US', options);
                } catch (err) {
                    return numeric.toFixed(options.maximumFractionDigits ?? 2);
                }
            }
            return this.app?.formatNumber?.(numeric) ?? numeric.toString();
        }

        formatDuration(seconds) {
            if (!Number.isFinite(seconds) || seconds <= 0) {
                return '—';
            }
            if (this.app?.formatDuration) {
                return this.app.formatDuration(seconds);
            }
            if (seconds < 60) {
                return `${seconds.toFixed(0)} 秒`;
            }
            const minutes = seconds / 60;
            if (minutes < 60) {
                return `${minutes.toFixed(1)} 分钟`;
            }
            const hours = minutes / 60;
            return `${hours.toFixed(1)} 小时`;
        }

        renderQueueMetrics(data) {
            const queues = Array.isArray(data?.queues) ? data.queues : [];
            const streams = Array.isArray(data?.streams) ? data.streams : [];

            if (this.elements.queueSourceBadge) {
                const source = data?.source === 'snapshot' ? 'Snapshot' : 'Live';
                this.elements.queueSourceBadge.textContent = source;
                this.elements.queueSourceBadge.className = `badge ${data?.source === 'snapshot' ? 'bg-info' : 'bg-success'}`;
            }

            if (this.elements.queueUpdatedAt) {
                this.elements.queueUpdatedAt.textContent = data?.timestamp
                    ? new Date(data.timestamp).toLocaleString()
                    : '—';
            }

            if (this.elements.queueTableBody) {
                if (!queues.length) {
                    this.elements.queueTableBody.innerHTML = '<tr><td colspan="5" class="text-center text-muted py-3">暂无队列数据</td></tr>';
                } else {
                    this.elements.queueTableBody.innerHTML = queues.map((entry) => {
                        const consumption = Number.isFinite(entry?.consumption_rate)
                            ? `${this.formatNumber(entry.consumption_rate, { maximumFractionDigits: 2 })}/s`
                            : '—';
                        const stale = this.formatDuration(Number(entry?.stale_seconds ?? 0));
                        return `
                            <tr>
                                <td>${this.escapeHtml(entry?.name || entry?.queue_key || '-')}</td>
                                <td><code>${this.escapeHtml(entry?.queue_key || '-')}</code></td>
                                <td class="text-end">${this.formatNumber(entry?.length ?? 0, { maximumFractionDigits: 0 })}</td>
                                <td class="text-end">${consumption}</td>
                                <td class="text-end">${stale}</td>
                            </tr>
                        `;
                    }).join('');
                }
            }

            if (this.elements.streamTableBody) {
                if (!streams.length) {
                    this.elements.streamTableBody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">暂无 Stream 数据</td></tr>';
                } else {
                    this.elements.streamTableBody.innerHTML = streams.map((entry) => {
                        const consumption = Number.isFinite(entry?.consumption_rate)
                            ? `${this.formatNumber(entry.consumption_rate, { maximumFractionDigits: 2 })}/s`
                            : '—';
                        const stale = this.formatDuration(Number(entry?.stale_seconds ?? 0));
                        return `
                            <tr>
                                <td>${this.escapeHtml(entry?.name || entry?.stream_key || '-')}</td>
                                <td><code>${this.escapeHtml(entry?.stream_key || '-')}</code></td>
                                <td><code>${this.escapeHtml(entry?.group || '-')}</code></td>
                                <td class="text-end">${this.formatNumber(entry?.pending ?? 0, { maximumFractionDigits: 0 })}</td>
                                <td class="text-end">${consumption}</td>
                                <td class="text-end">${stale}</td>
                            </tr>
                        `;
                    }).join('');
                }
            }
        }

        escapeHtml(value) {
            if (!value && value !== 0) return '';
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        async refreshInfluxMetrics({ manual = false, immediate = false } = {}) {
            if (this.state.influx === REFRESH_STATE.loading && !manual) {
                this.scheduleInfluxRefresh();
                return;
            }

            this.state.influx = REFRESH_STATE.loading;
            if (this.elements.influxSummary) {
                this.elements.influxSummary.textContent = '加载中...';
            }

            const params = new URLSearchParams();
            const windowValue = this.elements.influxWindow?.value || '1h';
            params.set('window', windowValue);
            const serviceValue = this.elements.influxService?.value || '';
            if (serviceValue) {
                params.set('service', serviceValue);
            }
            const metricValue = this.elements.influxMetric?.value?.trim();
            if (metricValue) {
                params.set('metric', metricValue);
            }

            try {
                const data = await this.app.apiCall(`/monitoring/influx/write-rate?${params.toString()}`, {
                    useCache: false,
                    cacheTime: 0,
                    timeout: manual ? 15000 : 20000,
                });
                this.renderInfluxMetrics(data || {});
                this.state.influx = REFRESH_STATE.idle;
            } catch (error) {
                console.error('Failed to load Influx write metrics', error);
                this.state.influx = REFRESH_STATE.error;
                if (this.elements.influxSummary) {
                    this.elements.influxSummary.textContent = error?.message || '无法加载写入速率';
                }
            } finally {
                if (!immediate) {
                    this.scheduleInfluxRefresh();
                }
            }
        }

        ensureInfluxChart() {
            if (this.influxChart) {
                return this.influxChart;
            }
            const canvas = document.getElementById('influxWriteRateChart');
            if (!canvas || !canvas.getContext) return null;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;
            if (!root.Chart || typeof root.Chart !== 'function') return null;

            this.influxChart = new root.Chart(ctx, {
                type: 'line',
                data: { datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'nearest', intersect: false },
                    scales: {
                        x: {
                            type: 'time',
                            adapters: {
                                date: window.luxon || undefined,
                            },
                            time: { unit: 'minute' },
                        },
                        y: {
                            title: { display: true, text: 'Points / 1m' },
                            ticks: { callback: (value) => this.formatNumber(value, { maximumFractionDigits: 2 }) },
                        },
                    },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => {
                                    const value = context.parsed?.y;
                                    return `${context.dataset?.label || 'Series'}: ${this.formatNumber(value, { maximumFractionDigits: 2 })}`;
                                },
                            },
                        },
                    },
                },
            });
            return this.influxChart;
        }

        renderInfluxMetrics(data) {
            const chart = this.ensureInfluxChart();
            if (!chart) {
                if (this.elements.influxSummary) {
                    this.elements.influxSummary.textContent = 'Chart.js 未加载，无法显示图表';
                }
                return;
            }

            const series = Array.isArray(data?.series) ? data.series : [];
            const grouped = {};
            series.forEach((entry) => {
                const label = entry?.metric || entry?.queue_key || entry?.service || 'unknown';
                if (!grouped[label]) {
                    grouped[label] = [];
                }
                if (entry?.time && Number.isFinite(entry?.value)) {
                    grouped[label].push({ x: entry.time, y: Number(entry.value) });
                }
            });

            const datasetEntries = Object.entries(grouped);
            if (!datasetEntries.length) {
                chart.data.datasets = [];
                chart.update('none');
                if (this.elements.influxSummary) {
                    this.elements.influxSummary.textContent = '暂无写入数据';
                }
                return;
            }

            chart.data.datasets = datasetEntries.map(([label, points], index) => {
                const color = COLORS[index % COLORS.length];
                return {
                    label,
                    data: points.sort((a, b) => new Date(a.x).getTime() - new Date(b.x).getTime()),
                    borderColor: color,
                    backgroundColor: color,
                    tension: 0.25,
                    pointRadius: 0,
                    fill: false,
                };
            });
            chart.update('none');

            if (this.elements.influxSummary) {
                const summary = data?.summary || {};
                const text = Object.entries(summary)
                    .map(([label, stats]) => {
                        const avg = Number(stats?.average || 0);
                        const points = Number(stats?.points || 0);
                        return `${label}: 平均 ${this.formatNumber(avg, { maximumFractionDigits: 2 })}/m (${points} 点)`;
                    })
                    .join(' ・ ');
                this.elements.influxSummary.textContent = text || '暂无统计数据';
            }
        }
    }

    monitoring.dashboard = monitoring.dashboard || {};
    monitoring.dashboard.MonitoringDashboard = MonitoringDashboard;

    if (typeof root.MonitoringDashboard === 'undefined') {
        root.MonitoringDashboard = MonitoringDashboard;
    }
})(typeof window !== 'undefined' ? window : globalThis);

