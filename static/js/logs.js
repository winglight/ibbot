/**
 * 日志管理模块
 * 负责日志的加载、显示、统计等功能
 */
class LogsManager {
    constructor(apiCall, showAlert, formatNumber) {
        this.apiCall = apiCall;
        this.showAlert = showAlert;
        this.formatNumber = formatNumber;
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
                // Clear existing options except "All Modules"
                moduleSelect.innerHTML = '<option value="all">All Modules</option>';
                // Add dynamic modules from logs
                res.modules.forEach(module => {
                    const option = document.createElement('option');
                    option.value = module;
                    option.textContent = module;
                    moduleSelect.appendChild(option);
                });
                // Restore previous selection if it still exists
                if (currentValue && [...moduleSelect.options].some(opt => opt.value === currentValue)) {
                    moduleSelect.value = currentValue;
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
    async loadLogs() {
        try {
            const module = document.getElementById('logModule')?.value || 'all';
            const range = document.getElementById('logRange')?.value || 'today';
            const level = document.getElementById('logLevel')?.value || 'all';
            const requestId = document.getElementById('logRequestId')?.value.trim();
            let url = `/logs?lines=200&range=${range}`;
            if (level !== 'all') url += `&level=${encodeURIComponent(level)}`;
            if (module !== 'all') url += `&module=${module}`;
            if (requestId) url += `&request_id=${encodeURIComponent(requestId)}`;
            const res = await this.apiCall(url);
            if (res?.logs) {
                const content = document.getElementById('logsContent');
                if (content) content.textContent = res.logs.join('');
            }
        } catch (e) {
            console.error('Load logs error:', e);
            this.showAlert('danger', '加载日志失败');
        }
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
            logModule.addEventListener('change', () => this.loadLogs());
        }

        // 日志范围选择变化时重新加载模块和日志
        const logRange = document.getElementById('logRange');
        if (logRange) {
            logRange.addEventListener('change', () => {
                this.loadLogModules();
                this.loadLogs();
            });
        }

        // 日志级别选择变化时重新加载日志
        const logLevel = document.getElementById('logLevel');
        if (logLevel) {
            logLevel.addEventListener('change', () => this.loadLogs());
        }

        // 请求ID输入变化时重新加载日志
        const logRequestId = document.getElementById('logRequestId');
        if (logRequestId) {
            logRequestId.addEventListener('input', () => {
                clearTimeout(this.logSearchTimeout);
                this.logSearchTimeout = setTimeout(() => this.loadLogs(), 500);
            });
        }

        // 刷新日志按钮
        const refreshLogsBtn = document.getElementById('refreshLogsBtn');
        if (refreshLogsBtn) {
            refreshLogsBtn.addEventListener('click', () => this.loadLogs());
        }

        // 日志统计按钮
        const logStatsBtn = document.getElementById('logStatsBtn');
        if (logStatsBtn) {
            logStatsBtn.addEventListener('click', () => this.showLogStats());
        }
    }

    /**
     * 初始化日志管理模块
     */
    async init() {
        this.initEventListeners();
        await this.loadLogModules();
        await this.loadLogs();
    }
}

// 导出类以供其他模块使用
if (typeof module !== 'undefined' && module.exports) {
    module.exports = LogsManager;
} else {
    window.LogsManager = LogsManager;
}