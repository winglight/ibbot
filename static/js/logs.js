/**
 * 日志管理模块
 * 负责日志的加载、显示、统计等功能
 */
class LogsManager {
    constructor(apiCall, showAlert, formatNumber) {
        this.apiCall = apiCall;
        this.showAlert = showAlert;
        this.formatNumber = formatNumber;
        this.pageSize = 200;
        this.cursor = null;
        this.forwardCursor = null;
        this.autoTail = true;
        this.tailTimer = null;
        this.tailInterval = 4000;
        this.isLoading = false;
        this.lastParams = {};
        this.statusEl = document.getElementById('logsStatusText');
        this.prevBtn = document.getElementById('logsPrevPageBtn');
        this.nextBtn = document.getElementById('logsNextPageBtn');
        this.tailToggle = document.getElementById('logsTailToggle');
        this.contentEl = document.getElementById('logsContent');
    }

    /**
     * 加载日志模块列表
     */
    async loadLogModules() {
        try {
            const range = document.getElementById('logRange')?.value || 'today';
            const res = await this.apiCall(`/logs/modules?range=${range}`);
            const moduleSelect = document.getElementById('logModule');
            if (moduleSelect && res?.modules) {
                const currentValue = moduleSelect.value;
                const presetOptions = [
                    { value: 'all', label: 'All Modules' },
                    { value: 'internal_strategy,queue_manager', label: '策略信号链路' },
                    { value: 'internal_strategy', label: '内部策略' },
                    { value: 'queue_manager', label: '队列管理器' },
                    { value: 'external_strategy', label: '外部策略' },
                    { value: 'risk_rule', label: 'risk rule' },
                    { value: 'orders', label: 'orders' },
                    { value: 'portfolio', label: 'portfolio' },
                    { value: 'connection', label: 'connection' },
                ];

                moduleSelect.innerHTML = '';
                const existingValues = new Set();

                const appendOption = ({ value, label }) => {
                    if (existingValues.has(value)) return;
                    const option = document.createElement('option');
                    option.value = value;
                    option.textContent = label;
                    moduleSelect.appendChild(option);
                    existingValues.add(value);
                };

                presetOptions.forEach(appendOption);

                // Add dynamic modules from logs, avoid duplicates with presets
                res.modules.forEach(moduleName => {
                    appendOption({ value: moduleName, label: moduleName });
                });

                const availableValues = [...moduleSelect.options].map(opt => opt.value);
                if (currentValue && availableValues.includes(currentValue)) {
                    moduleSelect.value = currentValue;
                } else if (availableValues.includes('internal_strategy,queue_manager')) {
                    moduleSelect.value = 'internal_strategy,queue_manager';
                }
            }
        } catch (e) {
            console.error('Load log modules error:', e);
            // Fallback to static options if API fails
        }
    }

    /**
     * 加载日志内容
     */
    async loadLogs(options = {}) {
        if (this.isLoading) {
            return;
        }

        const reset = options.reset === true;
        const direction = options.direction || (reset ? 'backward' : (options.direction || 'backward'));

        if (reset) {
            this.cursor = null;
            this.forwardCursor = null;
        }

        try {
            this.isLoading = true;
            const module = document.getElementById('logModule')?.value || 'all';
            const range = document.getElementById('logRange')?.value || 'today';
            const level = document.getElementById('logLevel')?.value || 'all';
            const requestId = document.getElementById('logRequestId')?.value.trim();
            const searchText = document.getElementById('logSearchText')?.value.trim();

            const params = new URLSearchParams();
            params.set('range', range);
            params.set('page_size', String(this.pageSize));
            params.set('direction', direction);
            params.set('lines', String(this.pageSize)); // backward compatibility
            if (level !== 'all') params.set('level', level);
            if (module !== 'all') params.set('module', module);
            if (requestId) params.set('request_id', requestId);
            if (searchText) params.set('search', searchText);

            let cursor = options.cursor;
            if (cursor === undefined || cursor === null) {
                cursor = direction === 'forward' ? this.forwardCursor : this.cursor;
            }
            if (cursor !== undefined && cursor !== null) {
                params.set('cursor', String(cursor));
            }

            this.setStatus('加载中…');
            const res = await this.apiCall(`/logs?${params.toString()}`, { useCache: false, cacheTime: 0 });
            if (res?.logs) {
                if (direction === 'forward' && !reset && options.append !== false) {
                    this.appendLogs(res.logs);
                } else {
                    this.setLogs(res.logs);
                }
            }
            if (res?.pagination) {
                this.applyPagination(res.pagination, direction);
            } else if (reset) {
                this.cursor = null;
                this.forwardCursor = null;
            }
        } catch (e) {
            console.error('Load logs error:', e);
            this.showAlert('danger', '加载日志失败');
            this.setStatus(e?.message || '加载失败', true);
        } finally {
            this.isLoading = false;
            if (this.autoTail) {
                this.scheduleTail();
            }
        }
    }

    setLogs(logs) {
        if (!this.contentEl) return;
        const joined = Array.isArray(logs) ? logs.join('') : '';
        this.contentEl.textContent = joined;
        this.scrollToBottom();
    }

    appendLogs(logs) {
        if (!this.contentEl || !Array.isArray(logs) || !logs.length) return;
        const current = this.contentEl.textContent || '';
        const combined = (current + logs.join('')).split('\n');
        const maxLines = this.pageSize * 2;
        const trimmed = combined.slice(-maxLines);
        const text = trimmed.join('\n');
        this.contentEl.textContent = text.endsWith('\n') ? text : `${text}\n`;
        this.scrollToBottom();
    }

    scrollToBottom() {
        if (!this.contentEl) return;
        this.contentEl.scrollTop = this.contentEl.scrollHeight;
    }

    applyPagination(pagination = {}, direction = 'backward') {
        const { start = 0, end = 0, total = 0 } = pagination;
        this.cursor = pagination.next_backward_cursor ?? pagination.end ?? this.cursor;
        this.forwardCursor = pagination.next_forward_cursor ?? pagination.tail_cursor ?? this.forwardCursor;
        this.lastParams = { pagination, direction };

        if (this.statusEl) {
            if (!total) {
                this.statusEl.textContent = '暂无日志';
            } else {
                this.statusEl.textContent = `显示 ${start + 1}-${end} / ${total}`;
            }
        }

        if (this.prevBtn) {
            this.prevBtn.disabled = !pagination.has_more_backward;
        }
        if (this.nextBtn) {
            this.nextBtn.disabled = this.autoTail ? true : !pagination.has_more_forward;
        }
    }

    scheduleTail() {
        if (!this.autoTail || document.hidden) {
            this.stopTail();
            return;
        }
        if (this.tailTimer) {
            return;
        }
        this.tailTimer = setTimeout(() => {
            this.tailTimer = null;
            const cursor = this.forwardCursor ?? this.lastParams?.pagination?.tail_cursor ?? null;
            this.loadLogs({ direction: 'forward', cursor, append: true }).catch(() => {});
        }, this.tailInterval);
    }

    stopTail() {
        if (this.tailTimer) {
            clearTimeout(this.tailTimer);
            this.tailTimer = null;
        }
    }

    setStatus(message, isError = false) {
        if (!this.statusEl) return;
        this.statusEl.textContent = message || '';
        this.statusEl.classList.toggle('text-danger', !!isError);
    }

    /**
     * 显示日志统计信息
     */
    async showLogStats() {
        try {
            const module = document.getElementById('logModule')?.value || 'all';
            const range = document.getElementById('logRange')?.value || 'today';
            let url = `/logs/stats?range=${range}`;
            if (module !== 'all') url += `&module=${module}`;
            const res = await this.apiCall(url);
            const tbody = document.querySelector('#logStatsTable tbody');
            if (tbody) {
                tbody.innerHTML = '';
                for (const [mod, paths] of Object.entries(res.stats || {})) {
                    for (const [path, s] of Object.entries(paths)) {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `<td>${mod}</td><td>${path}</td><td>${s.count}</td><td>${s.avg_time}</td><td>${(s.success_rate*100).toFixed(1)}%</td>`;
                        tbody.appendChild(tr);
                    }
                }
            }
            const modalEl = document.getElementById('logStatsModal');
            if (modalEl) {
                const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
                modal.show();
            }
        } catch (e) {
            console.error('Load log stats error:', e);
            this.showAlert('danger', '加载日志统计失败');
        }
    }

    /**
     * 初始化日志管理相关的事件监听器
     */
    initEventListeners() {
        // 日志模块选择变化时重新加载日志
        const logModule = document.getElementById('logModule');
        if (logModule) {
            logModule.addEventListener('change', () => this.loadLogs({ reset: true }));
        }

        // 日志范围选择变化时重新加载模块和日志
        const logRange = document.getElementById('logRange');
        if (logRange) {
            logRange.addEventListener('change', () => {
                this.loadLogModules();
                this.loadLogs({ reset: true });
            });
        }

        // 日志级别选择变化时重新加载日志
        const logLevel = document.getElementById('logLevel');
        if (logLevel) {
            logLevel.addEventListener('change', () => this.loadLogs({ reset: true }));
        }

        // 请求ID输入变化时重新加载日志
        const logRequestId = document.getElementById('logRequestId');
        if (logRequestId) {
            logRequestId.addEventListener('input', () => {
                clearTimeout(this.logSearchTimeout);
                this.logSearchTimeout = setTimeout(() => this.loadLogs({ reset: true }), 500);
            });
        }

        const logSearchText = document.getElementById('logSearchText');
        if (logSearchText) {
            logSearchText.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') {
                    this.loadLogs({ reset: true });
                }
            });
        }

        // 刷新日志按钮
        const searchLogsBtn = document.getElementById('searchLogsBtn');
        if (searchLogsBtn) {
            searchLogsBtn.addEventListener('click', () => this.loadLogs({ reset: true }));
        }

        // 日志统计按钮
        const logStatsBtn = document.getElementById('logStatsBtn');
        if (logStatsBtn) {
            logStatsBtn.addEventListener('click', () => this.showLogStats());
        }

        if (this.prevBtn) {
            this.prevBtn.addEventListener('click', () => {
                const pagination = this.lastParams?.pagination || {};
                if (!pagination.has_more_backward) return;
                const cursor = pagination.next_backward_cursor ?? pagination.start;
                this.autoTail = false;
                if (this.tailToggle) {
                    this.tailToggle.checked = false;
                }
                this.stopTail();
                this.loadLogs({ cursor, direction: 'backward' }).catch(() => {});
            });
        }

        if (this.nextBtn) {
            this.nextBtn.addEventListener('click', () => {
                const pagination = this.lastParams?.pagination || {};
                if (!pagination.has_more_forward) return;
                const cursor = pagination.next_forward_cursor ?? pagination.end;
                this.loadLogs({ cursor, direction: 'forward' }).catch(() => {});
            });
        }

        if (this.tailToggle) {
            this.autoTail = !!this.tailToggle.checked;
            this.tailToggle.addEventListener('change', (event) => {
                const checked = !!event.target.checked;
                this.autoTail = checked;
                if (checked) {
                    this.scheduleTail();
                    if (this.nextBtn) {
                        this.nextBtn.disabled = true;
                    }
                } else {
                    this.stopTail();
                    if (this.nextBtn) {
                        this.nextBtn.disabled = !(this.lastParams?.pagination?.has_more_forward);
                    }
                }
            });
        }
    }

    /**
     * 初始化日志管理模块
     */
    async init() {
        this.initEventListeners();
        await this.loadLogModules();
        await this.loadLogs({ reset: true });
    }
}

// 导出类以供其他模块使用
if (typeof module !== 'undefined' && module.exports) {
    module.exports = LogsManager;
} else {
    window.LogsManager = LogsManager;
}