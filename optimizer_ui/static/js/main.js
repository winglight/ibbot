const DEFAULT_BASE_PARAMETER_TEMPLATE = {
    long_thresholds: [0.75, 0.8, 0.85],
    short_thresholds: [-0.75, -0.8, -0.85],
    persistence_seconds: [2, 3, 4],
    cooldown_threshold: [0, 0.1, 0.2],
    window: [8000, 10000, 12000],
    max_levels: [20, 30, 40],
    depth_rows: [5, 10, 15],
};

const BASE_METRIC_PARAMETER_TEMPLATES = {
    obi5: {
        obi5_threshold: [0.55, 0.6, 0.65],
        obi5_window_seconds: [10, 20, 30],
    },
    ofi: {
        ofi_threshold: [0.0, 0.05, 0.1],
        ofi_window_seconds: [10, 20, 30],
    },
    trend_score: {
        trend_score_long: [0.7, 0.8, 0.9],
        trend_score_short: [-0.7, -0.8, -0.9],
        trend_smoothing_window: [30, 45, 60],
    },
    cvd: {
        cvd_delta: [400, 600, 800],
        cvd_window_seconds: [20, 30, 45],
    },
    mid_price: {
        mid_price_offset_ticks: [0.5, 1, 1.5],
    },
};

const FEATURE_PARAMETER_TEMPLATES = {
    stacking: {
        intensity: [0.3, 0.35, 0.4],
        min_depth: [40, 60, 80],
    },
    pulling: {
        intensity: [0.25, 0.3, 0.35],
        min_depth: [20, 30, 40],
    },
    wall: {
        strength: [0.3, 0.4, 0.5],
        min_size: [60, 80, 100],
    },
    cancel_rate: {
        rate: [1.0, 1.2, 1.4],
    },
    absorption: {
        index: [0.5, 0.6, 0.7],
    },
    sweep: {
        intensity: [0.6, 0.7, 0.8],
    },
    fake_breakout: {
        prob: [0.55, 0.65, 0.75],
    },
    wall_pull_no_follow: {
        score: [0.5, 0.6, 0.7],
    },
    obi_ofi_reversal: {
        score: [0.65, 0.75, 0.85],
    },
    spread: {
        delta_ticks: [1, 2, 3],
    },
};

const METRIC_LABELS = {
    obi5: "订单簿不平衡 (OBI5)",
    ofi: "订单流差 (OFI)",
    trend_score: "趋势评分",
    cvd: "累计成交量差 (CVD)",
    mid_price: "中间价",
    stacking: "堆单强度",
    pulling: "撤单强度",
    wall: "墙单强度",
    cancel_rate: "撤单速率",
    absorption: "吸收指数",
    sweep: "扫单检测",
    fake_breakout: "假突破启发式",
    wall_pull_no_follow: "撤墙+无跟随",
    obi_ofi_reversal: "OBI/OFI 快速反转",
    spread: "价差变化",
};

const FormatUtils = {
    number(value, digits = 2) {
        if (value === null || value === undefined) {
            return '-';
        }
        const num = Number(value);
        if (!Number.isFinite(num)) {
            return '-';
        }
        return num.toFixed(digits);
    },
    duration(seconds) {
        if (seconds === null || seconds === undefined) {
            return '-';
        }
        const value = Number(seconds);
        if (!Number.isFinite(value) || value < 0) {
            return '-';
        }
        if (value < 1) {
            return `${value.toFixed(2)} 秒`;
        }
        if (value < 60) {
            return `${value.toFixed(1)} 秒`;
        }
        const hours = Math.floor(value / 3600);
        const minutes = Math.floor((value % 3600) / 60);
        const secs = Math.round(value % 60);
        const parts = [];
        if (hours) {
            parts.push(`${hours} 小时`);
        }
        if (minutes) {
            parts.push(`${minutes} 分钟`);
        }
        if (secs && parts.length < 2) {
            parts.push(`${secs} 秒`);
        }
        if (!parts.length) {
            parts.push(`${secs || Math.round(value)} 秒`);
        }
        return parts.join(' ');
    },
    percent(value, digits = 1) {
        if (value === null || value === undefined) {
            return '-';
        }
        const num = Number(value);
        if (!Number.isFinite(num)) {
            return '-';
        }
        return `${(num * 100).toFixed(digits)}%`;
    },
    drawdown(value, digits = 2) {
        if (value === null || value === undefined) {
            return '-';
        }
        if (typeof value === 'string') {
            const trimmed = value.trim();
            if (!trimmed) {
                return '-';
            }
            if (trimmed.endsWith('%')) {
                return trimmed;
            }
            const parsed = Number(trimmed);
            if (!Number.isFinite(parsed)) {
                return trimmed;
            }
            return FormatUtils.drawdown(parsed, digits);
        }
        const num = Number(value);
        if (!Number.isFinite(num)) {
            return '-';
        }
        if (Math.abs(num) <= 1) {
            return `${(num * 100).toFixed(digits)}%`;
        }
        return num.toFixed(digits);
    },
};

const HtmlUtils = {
    escape(value) {
        return (value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    },
};

class OptimizerApp {
    constructor(root) {
        this.root = root;
        this.modalRoot = document.getElementById('modal-root');
        this.refreshButton = document.getElementById('btn-refresh');
        this.refreshMetadataButton = document.getElementById('btn-refresh-metadata');
        this.newPlanButton = document.getElementById('btn-new-plan');
        this.state = {
            metadata: { base_metrics: [], feature_metrics: [], algorithms: [] },
            plans: [],
        };
    }

    formatMetricName(metric) {
        return METRIC_LABELS[metric] || metric;
    }

    formatMetricList(list) {
        if (!list || !list.length) {
            return '无';
        }
        return list.map((item) => this.formatMetricName(item)).join(', ');
    }

    formatNumber(value, digits = 2) {
        return FormatUtils.number(value, digits);
    }

    formatDuration(seconds) {
        return FormatUtils.duration(seconds);
    }

    formatPercent(value, digits = 1) {
        return FormatUtils.percent(value, digits);
    }

    formatDrawdown(value, digits = 2) {
        return FormatUtils.drawdown(value, digits);
    }

    async init() {
        await Promise.all([this.loadMetadata(), this.loadPlans()]);
        this.renderMetadataTags();
        this.renderPlanTable();
        this.attachEvents();
    }

    attachEvents() {
        if (this.refreshButton) {
            this.refreshButton.addEventListener('click', () => this.handleRefresh());
        }
        if (this.refreshMetadataButton) {
            this.refreshMetadataButton.addEventListener('click', async () => {
                await this.loadMetadata(true);
                this.renderMetadataTags();
            });
        }
        if (this.newPlanButton) {
            this.newPlanButton.addEventListener('click', () => this.openPlanDialog());
        }
        const table = document.getElementById('plan-table');
        if (table) {
            table.addEventListener('click', (event) => {
                const target = event.target.closest('button[data-action]');
                if (!target) return;
                const planId = target.dataset.planId;
                const action = target.dataset.action;
                if (action && planId) {
                    this.handlePlanAction(action, planId);
                }
            });
        }
    }

    async handleRefresh() {
        await this.loadPlans(true);
        this.renderPlanTable();
    }

    async loadMetadata(force = false) {
        if (
            !force
            && (this.state.metadata.base_metrics.length || this.state.metadata.feature_metrics.length)
        ) {
            return;
        }
        try {
            const res = await fetch('/api/optimize/metadata');
            if (!res.ok) throw new Error('Failed to load metadata');
            this.state.metadata = await res.json();
        } catch (err) {
            console.error(err);
        }
    }

    async loadPlans(force = false) {
        if (!force && this.state.plans.length) return;
        try {
            const res = await fetch('/api/optimize/plans');
            if (!res.ok) throw new Error('Failed to load plans');
            const data = await res.json();
            this.state.plans = data.plans ?? [];
        } catch (err) {
            console.error(err);
            this.state.plans = [];
        }
    }

    renderMetadataTags() {
        const container = document.getElementById('metadata-tags');
        if (!container) return;
        const { base_metrics: baseMetrics, feature_metrics: featureMetrics, algorithms } = this.state.metadata;
        if (!baseMetrics.length && !featureMetrics.length && !algorithms.length) {
            container.innerHTML = '<span class="tag">未检测到配置</span>';
            return;
        }
        const baseTags = baseMetrics.map((m) => `<span class="tag">基础 · ${this.formatMetricName(m)}</span>`).join('');
        const featureTags = featureMetrics
            .map((m) => `<span class="tag">特征 · ${this.formatMetricName(m)}</span>`)
            .join('');
        const algorithmTags = algorithms.map((a) => `<span class="tag">算法 · ${a}</span>`).join('');
        container.innerHTML = baseTags + featureTags + algorithmTags;
    }

    renderPlanTable() {
        const emptyState = document.getElementById('plan-empty-state');
        const wrapper = document.getElementById('plan-table-wrapper');
        const tableBody = document.querySelector('#plan-table tbody');
        if (!tableBody || !emptyState || !wrapper) return;

        if (!this.state.plans.length) {
            emptyState.style.display = 'block';
            wrapper.style.display = 'none';
            tableBody.innerHTML = '';
            return;
        }

        emptyState.style.display = 'none';
        wrapper.style.display = 'block';

        tableBody.innerHTML = this.state.plans
            .map((plan) => this.renderPlanRow(plan))
            .join('');
    }

    renderPlanRow(plan) {
        const statusClass = plan.is_active ? 'running' : 'success';
        const statusLabel = plan.is_active ? '运行中' : '暂停';
        const baseMetrics = this.formatMetricList(plan.base_metrics);
        const featureMetrics = this.formatMetricList(plan.feature_metrics);
        const metricsHtml = `<div>基础：${baseMetrics}</div><div>特征：${featureMetrics}</div>`;
        const frequency = plan.frequency_minutes
            ? `${plan.frequency_minutes} 分钟`
            : '手动触发';
        const updated = plan.updated_at
            ? new Date(plan.updated_at).toLocaleString()
            : '-';
        return `
            <tr>
                <td>${plan.name}</td>
                <td>${plan.symbol}</td>
                <td>${plan.algorithm}</td>
                <td>${metricsHtml}</td>
                <td>${frequency}</td>
                <td><span class="status-pill ${statusClass}">${statusLabel}</span></td>
                <td>${updated}</td>
                <td>
                    <div class="actions-row">
                        <button class="btn btn-ghost" data-action="reports" data-plan-id="${plan.id}">报告</button>
                        ${plan.is_active
                            ? '<button class="btn btn-ghost" disabled title="请先停止计划">编辑</button>'
                            : `<button class="btn btn-ghost" data-action="edit" data-plan-id="${plan.id}">编辑</button>`}
                        <button class="btn btn-ghost" data-action="run" data-plan-id="${plan.id}">手动运行</button>
                        ${plan.is_active
                            ? `<button class="btn btn-ghost" data-action="stop" data-plan-id="${plan.id}">停止</button>`
                            : `<button class="btn btn-ghost" data-action="start" data-plan-id="${plan.id}">启动</button>`}
                        <button class="btn btn-ghost" data-action="duplicate" data-plan-id="${plan.id}">一键复制</button>
                        <button class="btn btn-ghost" data-action="delete" data-plan-id="${plan.id}">删除</button>
                    </div>
                </td>
            </tr>
        `;
    }

    async handlePlanAction(action, planId) {
        try {
            switch (action) {
                case 'start':
                    await this.postJson(`/api/optimize/plans/${planId}/start`);
                    break;
                case 'stop':
                    await this.postJson(`/api/optimize/plans/${planId}/stop`);
                    break;
                case 'run':
                    await this.postJson(`/api/optimize/plans/${planId}/run`);
                    break;
                case 'delete':
                    if (confirm('确定要删除该计划吗？')) {
                        await fetch(`/api/optimize/plans/${planId}`, { method: 'DELETE' });
                    }
                    break;
                case 'duplicate':
                    await this.duplicatePlan(planId);
                    break;
                case 'reports':
                    await this.openReportModal(planId);
                    return;
                case 'edit': {
                    const plan = this.state.plans.find((item) => item.id === planId);
                    if (!plan) return;
                    if (plan.is_active) {
                        alert('请先停止计划，再进行编辑');
                        return;
                    }
                    this.openPlanDialog(plan);
                    return;
                }
                default:
                    return;
            }
            await this.loadPlans(true);
            this.renderPlanTable();
        } catch (err) {
            alert('操作失败，请查看控制台。');
            console.error(err);
        }
    }

    async duplicatePlan(planId) {
        const plan = this.state.plans.find((item) => item.id === planId);
        if (!plan) {
            throw new Error('plan_not_found');
        }
        const payload = {
            name: this.generateCopyName(plan.name),
            strategy: plan.strategy,
            symbol: plan.symbol,
            base_metrics: Array.isArray(plan.base_metrics) ? [...plan.base_metrics] : [],
            feature_metrics: Array.isArray(plan.feature_metrics) ? [...plan.feature_metrics] : [],
            parameters: JSON.parse(JSON.stringify(plan.parameters || {})),
            algorithm: plan.algorithm,
            start_date: plan.start_date,
            end_date: plan.end_date,
            frequency_minutes: plan.frequency_minutes ?? 0,
        };
        if (plan.iterations !== undefined && plan.iterations !== null) {
            payload.iterations = plan.iterations;
        }
        await this.postJson('/api/optimize/plans', payload);
    }

    generateCopyName(originalName) {
        if (!originalName) {
            return '未命名计划（copy）';
        }
        const trimmed = originalName.replace(/（copy）$/, '');
        return `${trimmed}（copy）`;
    }

    async openReportModal(planId) {
        try {
            const plan = this.state.plans.find((item) => item.id === planId);
            const planTitle = plan?.name
                ? `${this.escapeHtml(String(plan.name))} - 计划报告`
                : '计划报告';
            const res = await fetch(`/api/optimize/plans/${planId}/reports`);
            if (!res.ok) throw new Error('failed to load reports');
            const data = await res.json();
            const reports = data.reports ?? [];
            const content = `
                <div class="modal-panel">
                    <header>
                        <h3>${planTitle}</h3>
                        <button class="btn btn-ghost" data-modal-close>关闭</button>
                    </header>
                    <main>
                        ${this.renderReportListContent(reports, planId)}
                    </main>
                </div>
            `;
            this.showModal(content, (backdrop) => this.setupReportModalEvents(backdrop));
        } catch (err) {
            console.error(err);
            alert('加载报告列表失败');
        }
    }

    renderReportListContent(reports, planId) {
        return reports.length
            ? reports.map((report) => this.renderReportCard(report, planId)).join('')
            : '<div class="empty-state">暂无报告</div>';
    }

    async refreshReportModalContent(planId, container) {
        if (!container) {
            await this.openReportModal(planId);
            return;
        }
        try {
            const res = await fetch(`/api/optimize/plans/${planId}/reports`);
            if (!res.ok) throw new Error('failed to load reports');
            const data = await res.json();
            const reports = data.reports ?? [];
            container.innerHTML = this.renderReportListContent(reports, planId);
        } catch (err) {
            console.error(err);
            alert('刷新报告列表失败');
        }
    }

    renderReportCard(report, planId) {
        const statusMap = {
            completed: { className: 'success', label: '完成' },
            failed: { className: 'failed', label: '失败' },
            running: { className: 'running', label: '进行中' },
            stopped: { className: 'stopped', label: '已停止' },
            pending: { className: 'running', label: '排队中' },
        };
        const statusInfo = statusMap[report.state] || { className: 'running', label: report.state || '-' };
        const statusItems = [`<span class="status-pill ${statusInfo.className}">${statusInfo.label}</span>`];
        if (report.state === 'running') {
            statusItems.push(
                `<button class="btn btn-ghost" data-report-action="stop-task" data-task-id="${report.task_id}" data-plan-id="${planId}">停止</button>`
            );
        }
        const statusHtml = `<div class="actions-row">${statusItems.join('')}</div>`;
        const created = report.created_at ? new Date(report.created_at).toLocaleString() : '-';
        const completed = report.completed_at ? new Date(report.completed_at).toLocaleString() : '-';
        const summary = report.report?.summary;
        const bestDrawdown = this.formatDrawdown(summary?.best_max_drawdown, 2);
        const metricsHtml = summary
            ? `
                <div class="tag-list">
                    <span class="tag">Sharpe ${summary.best_sharpe ?? '-'}</span>
                    <span class="tag">回撤 ${bestDrawdown}</span>
                    <span class="tag">组合 ${summary.successful_combinations ?? '-'}/${summary.tested_combinations ?? '-'}</span>
                </div>
            `
            : '';
        const runtime = summary?.runtime ?? report.runtime;
        const runtimeStatus = runtime?.status || report.state;
        const runtimeTitle = runtimeStatus === 'running' ? '运行进度' : '运行效率';
        const expectedLabel = runtimeStatus === 'running' ? '预估总耗时' : '预期';
        const actualLabel = runtimeStatus === 'running' ? '已用时' : '实际';
        const rateLabel = runtimeStatus === 'running' ? '速度' : '速率';
        const estimatedComboLabel = runtimeStatus === 'running' ? '预计组合' : '估算组合';
        const actualComboLabel = runtimeStatus === 'running' ? '已测组合' : '实测组合';
        const runtimeHtml = runtime
            ? `
                <div style="margin-top:1rem;">
                    <h4 style="margin-bottom:0.5rem;">${runtimeTitle}</h4>
                    <div class="tag-list">
                        <span class="tag">${expectedLabel} ${this.formatDuration(runtime.expected_duration_seconds)}</span>
                        <span class="tag">${actualLabel} ${this.formatDuration(runtime.actual_duration_seconds)}</span>
                        <span class="tag">${rateLabel} ${this.formatNumber(runtime.tests_per_second)} 组/秒</span>
                    </div>
                    <p class="section-subtitle" style="margin-top:0.5rem;">${estimatedComboLabel} ${runtime.estimated_total_combinations ?? '-'} · ${actualComboLabel} ${runtime.actual_total_combinations ?? '-'}</p>
                </div>
            `
            : '';
        const distribution = summary?.annual_return_distribution;
        const distributionHtml = distribution && Object.keys(distribution).length
            ? `
                <div style="margin-top:1rem;">
                    <h4 style="margin-bottom:0.5rem;">收益率分布</h4>
                    <div class="tag-list">
                        <span class="tag">最小 ${this.formatPercent(distribution.min)}</span>
                        <span class="tag">25分位 ${this.formatPercent(distribution.p25)}</span>
                        <span class="tag">中位数 ${this.formatPercent(distribution.p50)}</span>
                        <span class="tag">75分位 ${this.formatPercent(distribution.p75)}</span>
                        <span class="tag">最大 ${this.formatPercent(distribution.max)}</span>
                    </div>
                    <p class="section-subtitle" style="margin-top:0.5rem;">平均收益 ${this.formatPercent(distribution.mean)}</p>
                </div>
            `
            : '';
        const conclusionHtml = summary?.conclusion
            ? `
                <div style="margin-top:1rem;">
                    <h4 style="margin-bottom:0.5rem;">结论</h4>
                    <p class="section-subtitle" style="margin-top:0;">${summary.conclusion}</p>
                </div>
            `
            : '';
        const combinationHtml = report.state === 'completed'
            ? this.renderSuccessfulCombinations(report, planId)
            : '';
        return `
            <article class="opt-card">
                <header class="section-header">
                    <div>
                        <h3>任务 ${report.task_id}</h3>
                        <p class="section-subtitle">开始于 ${created}${completed !== '-' ? ` · 完成于 ${completed}` : ''}</p>
                    </div>
                    ${statusHtml}
                </header>
                ${metricsHtml}
                ${combinationHtml}
                ${runtimeHtml}
                ${distributionHtml}
                ${conclusionHtml}
                ${report.message ? `<p class="section-subtitle" style="margin-top:0.8rem;">${report.message}</p>` : ''}
                ${this.renderFailReasons(report)}
            </article>
        `;
    }

    renderFailReasons(report) {
        const rawFails = report.report?.fail_reasons ?? [];
        const seen = new Set();
        const fails = [];
        rawFails.forEach((item) => {
            if (item === null || item === undefined) return;
            const text = String(item).trim();
            if (!text) return;
            if (seen.has(text)) return;
            seen.add(text);
            fails.push(text);
        });
        if (!fails.length) return '';
        return `
            <div style="margin-top:1rem;">
                <h4 style="margin-bottom:0.5rem;">失败组合</h4>
                <ul style="list-style:disc; padding-left:1.5rem; color:#94a3b8;">
                    ${fails.map((item) => `<li>${this.escapeHtml(item)}</li>`).join('')}
                </ul>
            </div>
        `;
    }

    renderSuccessfulCombinations(report, planId) {
        const results = report.report?.results ?? [];
        if (!results.length) return '';
        const ranked = [...results].sort((a, b) => {
            const toNumeric = (value) => {
                if (value === null || value === undefined) {
                    return null;
                }
                if (typeof value === 'string') {
                    const trimmed = value.trim();
                    if (!trimmed) {
                        return null;
                    }
                    if (trimmed.endsWith('%')) {
                        const numeric = Number(trimmed.slice(0, -1));
                        return Number.isFinite(numeric) ? numeric / 100 : null;
                    }
                    const numeric = Number(trimmed);
                    return Number.isFinite(numeric) ? numeric : null;
                }
                const numeric = Number(value);
                return Number.isFinite(numeric) ? numeric : null;
            };
            const score = (item) => {
                if (!item || typeof item !== 'object') {
                    return Number.NEGATIVE_INFINITY;
                }
                const metrics = item.metrics || {};
                const annual = toNumeric(metrics.annual_return);
                if (annual !== null) {
                    return annual;
                }
                const sharpe = toNumeric(metrics.sharpe);
                if (sharpe !== null) {
                    return sharpe;
                }
                return Number.NEGATIVE_INFINITY;
            };
            return score(b) - score(a);
        });
        const items = ranked
            .slice(0, 3)
            .map((result, index) => this.renderCombinationItem(result, index, report, planId))
            .join('');
        return `
            <div class="combo-section">
                <h4 class="combo-title">成功组合</h4>
                <div class="combo-list">${items}</div>
            </div>
        `;
    }

    renderCombinationItem(result, index, report, planId) {
        const { base, features } = this.splitCombinationParameters(result.parameters || {});
        const metrics = result.metrics || {};
        const metricsTags = this.buildComboMetricTags(metrics);
        const baseJson = this.escapeHtml(JSON.stringify(base, null, 2));
        const featureJson = this.escapeHtml(JSON.stringify(features, null, 2));
        const featureCount = Object.keys(features || {}).length;
        const basePayload = this.encodePayload(base);
        const featurePayload = this.encodePayload(features);
        const defaultName = `${report.task_id || 'strategy'}-${index + 1}`;
        const disabledAttr = featureCount ? '' : 'disabled';
        return `
            <div class="combo-card" data-combo-index="${index}">
                <div class="combo-card-header">
                    <h5>组合 #${index + 1}</h5>
                    ${metricsTags}
                </div>
                <div class="combo-params">
                    <div class="combo-params-block">
                        <h5>基础参数</h5>
                        <pre>${baseJson}</pre>
                    </div>
                    <div class="combo-params-block">
                        <h5>特征参数</h5>
                        <pre>${featureCount ? featureJson : this.escapeHtml('{}')}</pre>
                    </div>
                </div>
                <div class="combo-card-actions">
                    <button class="btn btn-ghost" data-report-action="copy-base" data-payload="${basePayload}">复制基础参数</button>
                    <button class="btn btn-ghost" data-report-action="copy-feature" data-payload="${featurePayload}" ${disabledAttr}>复制特征参数</button>
                    <button class="btn btn-ghost" data-report-action="generate-strategy" data-plan-id="${planId}" data-task-id="${report.task_id}" data-base="${basePayload}" data-features="${featurePayload}" data-strategy-name="${defaultName}">生成策略</button>
                </div>
            </div>
        `;
    }

    buildComboMetricTags(metrics) {
        if (!metrics || typeof metrics !== 'object') {
            return '';
        }
        const tags = [];
        if (metrics.sharpe !== undefined) {
            tags.push(`<span class="tag">Sharpe ${this.formatNumber(metrics.sharpe, 3)}</span>`);
        }
        if (metrics.max_drawdown !== undefined) {
            tags.push(`<span class="tag">回撤 ${this.formatDrawdown(metrics.max_drawdown, 2)}</span>`);
        }
        if (metrics.annual_return !== undefined) {
            tags.push(`<span class="tag">收益 ${this.formatPercent(metrics.annual_return, 2)}</span>`);
        }
        if (!tags.length) {
            return '';
        }
        return `<div class="tag-list">${tags.join('')}</div>`;
    }

    splitCombinationParameters(parameters) {
        const base = {};
        const features = {};
        Object.entries(parameters || {}).forEach(([key, value]) => {
            if (key.includes('__')) {
                const [metric, param] = key.split('__');
                if (!features[metric]) {
                    features[metric] = {};
                }
                features[metric][param] = value;
            } else {
                base[key] = value;
            }
        });
        return { base, features };
    }

    encodePayload(payload) {
        try {
            return encodeURIComponent(JSON.stringify(payload ?? {}));
        } catch (err) {
            console.warn('Failed to encode payload', err);
            return '';
        }
    }

    decodePayload(encoded, fallback = {}) {
        if (!encoded) return fallback;
        try {
            const raw = decodeURIComponent(encoded);
            return JSON.parse(raw);
        } catch (err) {
            console.warn('Failed to decode payload', err);
            return fallback;
        }
    }

    escapeHtml(value) {
        return HtmlUtils.escape(value);
    }

    setupReportModalEvents(backdrop) {
        if (!backdrop) return;
        const main = backdrop.querySelector('main');
        if (!main) return;
        main.addEventListener('click', async (event) => {
            const button = event.target.closest('button[data-report-action]');
            if (!button) return;
            const action = button.dataset.reportAction;
            if (!action) return;
            event.preventDefault();
            if (action === 'stop-task') {
                const taskId = button.dataset.taskId;
                const planId = button.dataset.planId || '';
                if (!taskId) return;
                const originalLabel = button.textContent;
                const modalMain = button.closest('.modal-panel')?.querySelector('main');
                button.disabled = true;
                button.textContent = '停止中...';
                try {
                    const response = await this.postJson(`/api/optimize/tasks/${taskId}/stop`);
                    if (planId) {
                        await this.refreshReportModalContent(planId, modalMain);
                    } else {
                        await this.handleRefresh();
                    }
                    if (!response.accepted) {
                        alert('该任务已完成或已停止。');
                    }
                } catch (err) {
                    console.error(err);
                    alert('停止任务失败，请稍后重试。');
                } finally {
                    if (button.isConnected) {
                        button.disabled = false;
                        button.textContent = originalLabel;
                    }
                }
                return;
            }
            if (action === 'copy-base' || action === 'copy-feature') {
                const payload = this.decodePayload(button.dataset.payload);
                if (!payload || !Object.keys(payload).length) {
                    alert('无可复制的参数');
                    return;
                }
                try {
                    await this.copyToClipboard(JSON.stringify(payload, null, 2));
                    this.flashButtonFeedback(button, '已复制');
                } catch (err) {
                    console.error(err);
                    alert('复制失败，请手动复制。');
                }
                return;
            }
            if (action === 'generate-strategy') {
                const base = this.decodePayload(button.dataset.base);
                const features = this.decodePayload(button.dataset.features);
                const planId = button.dataset.planId || undefined;
                const taskId = button.dataset.taskId || undefined;
                const suggested = button.dataset.strategyName || '';
                const strategyName = window.prompt('请输入策略名称', suggested);
                if (!strategyName) return;
                try {
                    const response = await this.postJson('/api/optimize/strategies', {
                        strategy_name: strategyName,
                        plan_id: planId,
                        task_id: taskId,
                        base_parameters: base,
                        feature_parameters: features,
                    });
                    const filePath = response.file_path || '策略已生成';
                    alert(`策略已生成：${filePath}`);
                } catch (err) {
                    console.error(err);
                    alert('策略生成失败，请稍后重试。');
                }
            }
        });
    }

    flashButtonFeedback(button, message) {
        if (!button) return;
        const original = button.dataset.originalLabel || button.textContent;
        button.dataset.originalLabel = original;
        button.disabled = true;
        button.textContent = message;
        setTimeout(() => {
            button.disabled = false;
            button.textContent = button.dataset.originalLabel || original;
        }, 1200);
    }

    async copyToClipboard(text) {
        if (!text) return;
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            return;
        }
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        try {
            document.execCommand('copy');
        } finally {
            document.body.removeChild(textarea);
        }
    }

    openPlanDialog(existingPlan = null) {
        const isEdit = Boolean(existingPlan);
        const { base_metrics: baseMetrics, feature_metrics: featureMetrics, algorithms } = this.state.metadata;
        const baseBucket = [...baseMetrics];
        const featureBucket = [...featureMetrics];
        const existingBase = new Set(existingPlan?.base_metrics ?? []);
        const existingFeature = new Set(existingPlan?.feature_metrics ?? []);
        if (existingPlan) {
            (existingPlan.base_metrics || []).forEach((metric) => {
                if (!baseBucket.includes(metric)) {
                    baseBucket.push(metric);
                }
            });
            (existingPlan.feature_metrics || []).forEach((metric) => {
                if (!featureBucket.includes(metric)) {
                    featureBucket.push(metric);
                }
            });
            const featureKeys = Object.keys(existingPlan.parameters?.features || {});
            featureKeys.forEach((metric) => {
                if (!featureBucket.includes(metric)) {
                    featureBucket.push(metric);
                }
                existingFeature.add(metric);
            });
        }
        const defaultBaseSelection = isEdit ? existingBase : new Set(baseBucket);
        const defaultFeatureSelection = isEdit ? existingFeature : new Set(featureBucket);
        const baseCheckboxes = baseBucket
            .map((metric) => {
                const checked = defaultBaseSelection.has(metric);
                return `
                <label class="tag">
                    <input type="checkbox" name="base_metrics" value="${metric}" ${checked ? 'checked' : ''} /> ${this.formatMetricName(metric)}
                </label>
            `;
            })
            .join('');
        const featureCheckboxes = featureBucket
            .map((metric) => {
                const checked = defaultFeatureSelection.has(metric);
                return `
                <label class="tag">
                    <input type="checkbox" name="feature_metrics" value="${metric}" ${checked ? 'checked' : ''} /> ${this.formatMetricName(metric)}
                </label>
            `;
            })
            .join('');
        const algorithmOptions = algorithms
            .map((algo) => {
                const selected = existingPlan?.algorithm === algo ? 'selected' : '';
                return `<option value="${algo}" ${selected}>${algo}</option>`;
            })
            .join('');
        const titleText = isEdit ? '编辑优化计划' : '新增优化计划';
        const formHtml = `
            <div class="modal-panel">
                <header>
                    <h3>${titleText}</h3>
                    <button class="btn btn-ghost" data-modal-close>取消</button>
                </header>
                <main>
                    <form id="plan-form" class="form-grid" autocomplete="off">
                        <div class="input-group">
                            <label for="name">计划名称</label>
                            <input id="name" name="name" required placeholder="例如：MNQ 多因子" />
                        </div>
                        <div class="form-grid two-columns">
                            <div class="input-group">
                                <label for="strategy">策略标识</label>
                                <input id="strategy" name="strategy" required placeholder="如：mnq_dom" />
                            </div>
                            <div class="input-group">
                                <label for="symbol">标的</label>
                                <input id="symbol" name="symbol" required placeholder="MNQ" />
                            </div>
                            <div class="input-group">
                                <label for="frequency">报告频率（分钟，0 表示手动）</label>
                                <input id="frequency" name="frequency_minutes" type="number" min="0" value="0" />
                            </div>
                        </div>
                        <div class="form-grid two-columns">
                            <div class="input-group">
                                <label for="start_date">开始日期</label>
                                <input id="start_date" name="start_date" type="date" required />
                            </div>
                            <div class="input-group">
                                <label for="end_date">结束日期</label>
                                <input id="end_date" name="end_date" type="date" required />
                            </div>
                        </div>
                        <div class="input-group">
                            <label>基础指标</label>
                            <div class="tag-list">${baseCheckboxes || '<span class="tag">暂无基础指标</span>'}</div>
                        </div>
                        <div class="input-group">
                            <label>特征指标</label>
                            <div class="tag-list">${featureCheckboxes || '<span class="tag">暂无特征指标</span>'}</div>
                        </div>
                        <div class="form-grid two-columns">
                            <div class="input-group">
                                <label for="algorithm">搜索算法</label>
                                <select id="algorithm" name="algorithm">
                                    ${algorithmOptions}
                                </select>
                            </div>
                            <div class="input-group">
                                <label for="iterations">迭代次数（随机/贝叶斯可选）</label>
                                <input id="iterations" name="iterations" type="number" min="1" placeholder="可留空" />
                            </div>
                        </div>
                        <div class="form-grid two-columns">
                            <div class="input-group">
                                <label for="base_parameters">基础阈值参数 JSON <span class="help-text">（例如长短入场阈值、持续时长等）</span></label>
                                <textarea id="base_parameters" name="base_parameters" required></textarea>
                            </div>
                            <div class="input-group">
                                <label for="feature_parameters">特征阈值参数 JSON <span class="help-text">（按指标拆分的阈值网格）</span></label>
                                <textarea id="feature_parameters" name="feature_parameters" required></textarea>
                            </div>
                        </div>
                        <p class="section-subtitle">提示：两个 JSON 字段分别描述基础阈值与特征阈值，需为对象格式且值为数组。</p>
                        <p class="error-text" id="plan-form-error" style="display:none;"></p>
                    </form>
                </main>
                <div class="modal-actions">
                    <button class="btn btn-ghost" data-modal-close>取消</button>
                    <button class="btn" id="plan-form-submit">保存</button>
                </div>
            </div>
        `;
        const defaults = isEdit ? null : this.getHistoricalPlanDefaults();
        this.showModal(formHtml, () => this.handlePlanFormSubmit(existingPlan?.id || null));
        this.initializePlanForm(existingPlan, defaults);
        this.setupParameterAutofill(existingPlan);
        this.prefillDataRange(!isEdit);
    }

    async prefillDataRange(fetchEnabled = true) {
        const symbolInput = document.getElementById('symbol');
        const startInput = document.getElementById('start_date');
        const endInput = document.getElementById('end_date');
        if (!symbolInput || !startInput || !endInput) return;
        const fetchRange = async () => {
            if (!fetchEnabled) return;
            try {
                const symbol = symbolInput.value.trim();
                if (!symbol) return;
                const res = await fetch(`/api/optimize/data-range?symbol=${encodeURIComponent(symbol)}`);
                if (!res.ok) return;
                const data = await res.json();
                if (data.start_date && !startInput.value) startInput.value = data.start_date;
                if (data.end_date && !endInput.value) endInput.value = data.end_date;
            } catch (err) {
                console.warn('无法获取数据范围', err);
            }
        };
        symbolInput.addEventListener('blur', fetchRange);
        await fetchRange();
    }

    cloneValue(value) {
        if (value === undefined) return {};
        return JSON.parse(JSON.stringify(value));
    }

    buildParameterTemplates(selectedBaseMetrics, selectedFeatureMetrics) {
        const base = this.cloneValue(DEFAULT_BASE_PARAMETER_TEMPLATE);
        selectedBaseMetrics.forEach((metric) => {
            const template = BASE_METRIC_PARAMETER_TEMPLATES[metric];
            if (!template) return;
            Object.entries(template).forEach(([key, value]) => {
                base[key] = this.cloneValue(value);
            });
        });
        const features = {};
        selectedFeatureMetrics.forEach((metric) => {
            const template = FEATURE_PARAMETER_TEMPLATES[metric];
            features[metric] = template ? this.cloneValue(template) : {};
        });
        return { base, features };
    }

    safeParseJson(text, fallback = {}) {
        if (!text || !text.trim()) {
            return this.cloneValue(fallback);
        }
        try {
            const parsed = JSON.parse(text);
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                return this.cloneValue(fallback);
            }
            return parsed;
        } catch (err) {
            return this.cloneValue(fallback);
        }
    }

    mergeFeatureTemplates(selectedFeatureMetrics, currentFeatures, templateFeatures) {
        const merged = {};
        const featureSource = currentFeatures && typeof currentFeatures === 'object' && !Array.isArray(currentFeatures)
            ? currentFeatures
            : {};
        selectedFeatureMetrics.forEach((metric) => {
            const template = templateFeatures[metric] ? this.cloneValue(templateFeatures[metric]) : {};
            const existing = featureSource[metric]
                && typeof featureSource[metric] === 'object'
                && !Array.isArray(featureSource[metric])
                ? featureSource[metric]
                : {};
            merged[metric] = { ...template, ...existing };
        });
        return merged;
    }

    setupParameterAutofill(existingPlan) {
        const form = document.getElementById('plan-form');
        if (!form) return;
        const baseField = form.querySelector('#base_parameters');
        const featureField = form.querySelector('#feature_parameters');
        if (!baseField || !featureField) return;

        const markEdited = (event) => {
            event.target.dataset.userEdited = 'true';
        };
        [baseField, featureField].forEach((field) => field.addEventListener('input', markEdited));

        const handleParameterPaste = (event) => {
            const clipboardData = event.clipboardData?.getData('text/plain');
            if (!clipboardData || !clipboardData.trim()) {
                return;
            }
            const parsed = this.safeParseJson(clipboardData, null);
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                return;
            }
            const hasBase = Object.prototype.hasOwnProperty.call(parsed, 'base');
            const hasFeatures = Object.prototype.hasOwnProperty.call(parsed, 'features');
            if (!hasBase && !hasFeatures) {
                return;
            }
            event.preventDefault();
            if (hasBase) {
                const basePayload = parsed.base && typeof parsed.base === 'object' && !Array.isArray(parsed.base)
                    ? parsed.base
                    : {};
                baseField.value = JSON.stringify(basePayload, null, 2);
                baseField.dataset.userEdited = 'true';
            }
            if (hasFeatures) {
                const featurePayload = parsed.features && typeof parsed.features === 'object' && !Array.isArray(parsed.features)
                    ? parsed.features
                    : {};
                featureField.value = JSON.stringify(featurePayload, null, 2);
                featureField.dataset.userEdited = 'true';
            }
        };
        [baseField, featureField].forEach((field) => field.addEventListener('paste', handleParameterPaste));

        const updateTemplates = () => {
            const selectedBase = [...form.querySelectorAll('input[name="base_metrics"]:checked')].map((el) => el.value);
            const selectedFeatures = [...form.querySelectorAll('input[name="feature_metrics"]:checked')].map((el) => el.value);
            const templates = this.buildParameterTemplates(selectedBase, selectedFeatures);
            if (baseField.dataset.userEdited !== 'true') {
                baseField.value = JSON.stringify(templates.base, null, 2);
            }
            const currentFeatures = this.safeParseJson(featureField.value, {});
            const mergedFeatures = featureField.dataset.userEdited === 'true'
                ? this.mergeFeatureTemplates(selectedFeatures, currentFeatures, templates.features)
                : templates.features;
            featureField.value = JSON.stringify(mergedFeatures, null, 2);
        };

        const checkboxList = form.querySelectorAll('input[name="base_metrics"], input[name="feature_metrics"]');
        checkboxList.forEach((checkbox) => {
            checkbox.addEventListener('change', updateTemplates);
        });

        if (!existingPlan) {
            updateTemplates();
        }
    }

    parseBaseParameters(rawValue) {
        const payload = this.safeParseJson(rawValue, {});
        const entries = Object.entries(payload);
        if (!entries.length) {
            throw new Error('基础阈值参数 JSON 需包含至少一项');
        }
        entries.forEach(([key, value]) => {
            if (!Array.isArray(value) || !value.length) {
                throw new Error(`基础阈值参数 ${key} 需为非空数组`);
            }
        });
        return payload;
    }

    parseFeatureParameters(rawValue) {
        const payload = this.safeParseJson(rawValue || '{}', {});
        Object.entries(payload).forEach(([metric, config]) => {
            if (!config || typeof config !== 'object' || Array.isArray(config)) {
                throw new Error(`特征阈值参数 ${metric} 需为对象`);
            }
            Object.entries(config).forEach(([key, values]) => {
                if (!Array.isArray(values) || !values.length) {
                    throw new Error(`特征阈值参数 ${metric}.${key} 需为非空数组`);
                }
            });
        });
        return payload;
    }

    normalizeDateInput(value) {
        if (!value) return '';
        if (value instanceof Date) {
            return value.toISOString().slice(0, 10);
        }
        if (typeof value === 'string') {
            const trimmed = value.trim();
            if (!trimmed) {
                return '';
            }
            if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
                return trimmed;
            }
            const date = new Date(trimmed);
            if (Number.isFinite(date.getTime())) {
                return date.toISOString().slice(0, 10);
            }
            const [head] = trimmed.split('T');
            if (/^\d{4}-\d{2}-\d{2}$/.test(head)) {
                return head;
            }
            return '';
        }
        if (typeof value.toISOString === 'function') {
            return value.toISOString().slice(0, 10);
        }
        return '';
    }

    getHistoricalPlanDefaults() {
        if (!Array.isArray(this.state?.plans) || !this.state.plans.length) {
            return null;
        }
        const toTimestamp = (raw) => {
            if (!raw) return 0;
            const date = raw instanceof Date ? raw : new Date(raw);
            const time = date.getTime();
            return Number.isFinite(time) ? time : 0;
        };
        const latestPlan = this.state.plans.reduce((latest, plan) => {
            if (!latest) return plan;
            const latestTs = toTimestamp(latest.updated_at || latest.created_at);
            const currentTs = toTimestamp(plan.updated_at || plan.created_at);
            return currentTs > latestTs ? plan : latest;
        }, null);
        if (!latestPlan) {
            return null;
        }
        return {
            symbol: latestPlan.symbol || '',
            start_date: this.normalizeDateInput(latestPlan.start_date),
            end_date: this.normalizeDateInput(latestPlan.end_date),
        };
    }

    initializePlanForm(existingPlan, defaults = null) {
        const form = document.getElementById('plan-form');
        if (!form) return;
        const history = defaults || {};
        const setters = {
            name: existingPlan?.name ?? '',
            strategy: existingPlan?.strategy ?? '',
            symbol: existingPlan?.symbol ?? history.symbol ?? '',
            frequency_minutes: existingPlan?.frequency_minutes ?? 0,
            start_date: this.normalizeDateInput(existingPlan?.start_date) || history.start_date || '',
            end_date: this.normalizeDateInput(existingPlan?.end_date) || history.end_date || '',
            iterations: existingPlan?.iterations ?? '',
        };
        Object.entries(setters).forEach(([key, value]) => {
            const input = form.querySelector(`[name="${key}"]`);
            if (!input) return;
            if (value !== undefined && value !== null) {
                input.value = value;
            }
        });
        const baseInput = form.querySelector('#base_parameters');
        const featureInput = form.querySelector('#feature_parameters');
        const parameterPayload = existingPlan?.parameters ?? null;
        const isNested = parameterPayload && typeof parameterPayload === 'object' && !Array.isArray(parameterPayload)
            && ('base' in parameterPayload || 'features' in parameterPayload);
        const baseData = isNested
            ? parameterPayload.base ?? {}
            : parameterPayload ?? {};
        const featureData = isNested
            ? parameterPayload.features ?? {}
            : {};
        if (baseInput) {
            const baseTemplate = this.cloneValue(DEFAULT_BASE_PARAMETER_TEMPLATE);
            const hasBaseData = baseData && Object.keys(baseData).length;
            const mergedBase = hasBaseData ? { ...baseTemplate, ...baseData } : baseTemplate;
            const baseJson = JSON.stringify(mergedBase, null, 2);
            baseInput.value = baseJson;
            baseInput.dataset.userEdited = existingPlan ? 'true' : 'false';
        }
        if (featureInput) {
            const featureJson = JSON.stringify(featureData, null, 2);
            featureInput.value = featureJson;
            featureInput.dataset.userEdited = existingPlan ? 'true' : 'false';
        }
    }

    handlePlanFormSubmit(planId) {
        const submitBtn = document.getElementById('plan-form-submit');
        const form = document.getElementById('plan-form');
        if (!form || !submitBtn) return;
        submitBtn.addEventListener('click', async () => {
            const errorEl = document.getElementById('plan-form-error');
            if (errorEl) errorEl.style.display = 'none';
            try {
                const payload = this.collectPlanPayload(form);
                if (planId) {
                    await this.postJson(`/api/optimize/plans/${planId}`, payload, 'PATCH');
                } else {
                    await this.postJson('/api/optimize/plans', payload, 'POST');
                }
                this.closeModal();
                await this.loadPlans(true);
                this.renderPlanTable();
            } catch (err) {
                console.error(err);
                if (errorEl) {
                    errorEl.textContent = err.message || '提交失败，请检查输入';
                    errorEl.style.display = 'block';
                }
            }
        });
    }

    collectPlanPayload(form) {
        const formData = new FormData(form);
        const getValue = (name) => (formData.get(name) || '').toString().trim();
        const baseMetrics = [...form.querySelectorAll('input[name="base_metrics"]:checked')].map((el) => el.value);
        const featureMetrics = [...form.querySelectorAll('input[name="feature_metrics"]:checked')].map((el) => el.value);
        if (!baseMetrics.length && !featureMetrics.length) {
            throw new Error('至少选择一个基础或特征指标');
        }
        const baseParameters = this.parseBaseParameters(getValue('base_parameters'));
        const featureParameters = this.parseFeatureParameters(getValue('feature_parameters'));
        const normalizedFeatures = {};
        featureMetrics.forEach((metric) => {
            if (featureParameters[metric]) {
                normalizedFeatures[metric] = featureParameters[metric];
            }
        });
        const iterationsRaw = getValue('iterations');
        const payload = {
            name: getValue('name'),
            symbol: getValue('symbol').toUpperCase(),
            strategy: getValue('strategy') || 'default_strategy',
            base_metrics: baseMetrics,
            feature_metrics: featureMetrics,
            parameters: {
                base: baseParameters,
                features: normalizedFeatures,
            },
            algorithm: getValue('algorithm') || this.state.metadata.algorithms[0] || 'grid',
            start_date: getValue('start_date'),
            end_date: getValue('end_date'),
            frequency_minutes: Number(getValue('frequency_minutes') || 0),
        };
        if (!payload.name || !payload.symbol || !payload.start_date || !payload.end_date) {
            throw new Error('请完整填写名称、标的与日期');
        }
        if (iterationsRaw) {
            payload.iterations = Number(iterationsRaw);
        }
        return payload;
    }

    async postJson(url, data, method = 'POST') {
        const res = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: data ? JSON.stringify(data) : undefined,
        });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(text || '请求失败');
        }
        return res.json().catch(() => ({}));
    }

    showModal(content, onOpen) {
        if (!this.modalRoot) return;
        this.closeModal();
        const backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop';
        backdrop.innerHTML = content;
        backdrop.addEventListener('click', (event) => {
            if (event.target === backdrop || event.target.hasAttribute('data-modal-close')) {
                this.closeModal();
            }
        });
        this.modalRoot.appendChild(backdrop);
        if (onOpen) onOpen(backdrop);
    }

    closeModal() {
        if (!this.modalRoot) return;
        while (this.modalRoot.firstChild) {
            this.modalRoot.removeChild(this.modalRoot.firstChild);
        }
    }
}

class ParameterComparisonApp {
    constructor(root) {
        this.root = root;
        this.refreshButton = root.querySelector('[data-action="refresh"]');
        this.content = root.querySelector('[data-role="compare-content"]');
        this.state = { groups: [] };
        this.errorMessage = '';
        this.sortState = { key: 'sharpe', direction: 'desc' };
    }

    async init() {
        await this.loadData();
        this.render();
        this.attachEvents();
    }

    attachEvents() {
        if (this.refreshButton) {
            this.refreshButton.addEventListener('click', async () => {
                await this.handleRefresh();
            });
        }
        if (this.content) {
            this.content.addEventListener('click', async (event) => {
                const sortButton = event.target.closest('button[data-sort-key]');
                if (sortButton) {
                    event.preventDefault();
                    const { sortKey } = sortButton.dataset;
                    if (sortKey) {
                        this.handleSort(sortKey);
                    }
                    return;
                }
                const actionButton = event.target.closest('button[data-compare-action]');
                if (!actionButton) {
                    return;
                }
                const action = actionButton.dataset.compareAction;
                if (!action) {
                    return;
                }
                event.preventDefault();
                if (action === 'generate-strategy') {
                    await this.handleGenerateStrategy(actionButton);
                }
            });
        }
    }

    async handleRefresh() {
        if (this.content) {
            this.content.innerHTML = '<div class="empty-state">数据刷新中...</div>';
        }
        await this.loadData();
        this.render();
    }

    handleSort(sortKey) {
        if (!sortKey) return;
        const normalized = this.getSortAggregateKey(sortKey);
        if (!normalized) return;
        if (this.sortState.key === normalized) {
            this.sortState.direction = this.sortState.direction === 'asc' ? 'desc' : 'asc';
        } else {
            this.sortState = { key: normalized, direction: normalized === 'max_drawdown' ? 'asc' : 'desc' };
        }
        this.render();
    }

    async loadData() {
        this.errorMessage = '';
        try {
            const res = await fetch('/api/optimize/parameter-groups');
            if (!res.ok) throw new Error('failed to load parameter groups');
            const data = await res.json();
            this.state.groups = Array.isArray(data.groups) ? data.groups : [];
        } catch (err) {
            console.error(err);
            this.state.groups = [];
            this.errorMessage = '加载横评数据失败，请稍后重试。';
        }
    }

    render() {
        if (!this.content) return;
        if (this.errorMessage) {
            this.content.innerHTML = `<div class="empty-state">${this.errorMessage}</div>`;
            return;
        }
        if (!this.state.groups.length) {
            this.content.innerHTML = '<div class="empty-state">暂无可对比的参数组，请先完成回测任务。</div>';
            return;
        }
        const groups = this.getSortedGroups();
        const header = `
            <thead>
                <tr>
                    <th>标的 / 策略</th>
                    <th>参数组合</th>
                    ${this.renderSortHeader('Sharpe (均值/中位)', 'sharpe')}
                    ${this.renderSortHeader('回撤率 (均值/中位)', 'max_drawdown')}
                    ${this.renderSortHeader('收益率 (均值/中位)', 'annual_return')}
                    <th>统计</th>
                </tr>
            </thead>
        `;
        const rows = groups.map((group) => this.renderRow(group)).join('');
        this.content.innerHTML = `
            <table class="table compare-table">
                ${header}
                <tbody>${rows}</tbody>
            </table>
        `;
    }

    getSortedGroups() {
        const groups = Array.isArray(this.state.groups) ? [...this.state.groups] : [];
        if (!groups.length) {
            return groups;
        }
        const { key, direction } = this.sortState;
        const multiplier = direction === 'asc' ? 1 : -1;
        groups.sort((a, b) => {
            const valueA = this.getSortValue(a, key);
            const valueB = this.getSortValue(b, key);
            const hasA = valueA !== null;
            const hasB = valueB !== null;
            if (!hasA && !hasB) {
                return (a.symbol || '').localeCompare(b.symbol || '');
            }
            if (!hasA) {
                return 1;
            }
            if (!hasB) {
                return -1;
            }
            if (valueA === valueB) {
                if (a.sample_size === b.sample_size) {
                    return (a.symbol || '').localeCompare(b.symbol || '') || (a.strategy || '').localeCompare(b.strategy || '');
                }
                return direction === 'asc'
                    ? (a.sample_size || 0) - (b.sample_size || 0)
                    : (b.sample_size || 0) - (a.sample_size || 0);
            }
            return (valueA - valueB) * multiplier;
        });
        return groups;
    }

    getSortAggregateKey(sortKey) {
        if (sortKey === 'sharpe' || sortKey === 'max_drawdown' || sortKey === 'annual_return') {
            return sortKey;
        }
        if (sortKey === 'drawdown') {
            return 'max_drawdown';
        }
        if (sortKey === 'return') {
            return 'annual_return';
        }
        return '';
    }

    getSortValue(group, sortKey) {
        if (!group || !sortKey) {
            return null;
        }
        const stats = group.stats || {};
        const aggregate = stats[sortKey];
        if (!aggregate) {
            return null;
        }
        const candidate =
            typeof aggregate.average === 'number' && Number.isFinite(aggregate.average)
                ? aggregate.average
                : typeof aggregate.median === 'number' && Number.isFinite(aggregate.median)
                ? aggregate.median
                : null;
        return candidate;
    }

    renderSortHeader(label, sortKey) {
        const normalized = this.getSortAggregateKey(sortKey);
        const isActive = this.sortState.key === normalized;
        const direction = isActive ? this.sortState.direction : '';
        const indicator = !isActive ? '↕' : direction === 'asc' ? '↑' : '↓';
        return `
            <th>
                <button type="button" class="table-sort-button${isActive ? ' is-active' : ''}" data-sort-key="${normalized}" data-sort-direction="${direction}">
                    <span>${label}</span>
                    <span class="sort-indicator">${indicator}</span>
                </button>
            </th>
        `;
    }

    renderRow(group) {
        const plans = Array.isArray(group.plans) ? group.plans : [];
        const planTags = plans
            .map((plan) => `<span class="tag">${HtmlUtils.escape(plan.name)}</span>`)
            .join('');
        const planHtml = planTags ? `<div class="tag-list">${planTags}</div>` : '';
        const parameters = group.parameters || {};
        const { base, features } = this.splitParameters(parameters);
        const parameterHtml = this.renderParameterBlocks(base, features);
        const basePayload = HtmlUtils.escape(this.encodePayload(base));
        const featurePayload = HtmlUtils.escape(this.encodePayload(features));
        const planId = HtmlUtils.escape(this.pickPlanId(plans));
        const strategyName = HtmlUtils.escape(this.buildDefaultStrategyName(group));
        const sharpeAvg = FormatUtils.number(group.stats?.sharpe?.average, 3);
        const sharpeMedian = FormatUtils.number(group.stats?.sharpe?.median, 3);
        const drawdownAvg = FormatUtils.drawdown(group.stats?.max_drawdown?.average, 2);
        const drawdownMedian = FormatUtils.drawdown(group.stats?.max_drawdown?.median, 2);
        const returnAvg = FormatUtils.percent(group.stats?.annual_return?.average, 2);
        const returnMedian = FormatUtils.percent(group.stats?.annual_return?.median, 2);
        const taskIds = Array.isArray(group.task_ids) ? group.task_ids : [];
        const taskTitle = HtmlUtils.escape(taskIds.join(', '));
        return `
            <tr>
                <td>
                    <strong>${HtmlUtils.escape(group.symbol)}</strong>
                    <div class="section-subtitle">策略 ${HtmlUtils.escape(group.strategy)}</div>
                    ${planHtml}
                </td>
                <td>
                    ${parameterHtml}
                    <div class="table-actions">
                        <button class="btn btn-ghost" data-compare-action="generate-strategy" data-base="${basePayload}" data-features="${featurePayload}" data-plan-id="${planId}" data-strategy-name="${strategyName}">生成策略</button>
                    </div>
                </td>
                <td>${sharpeAvg} / ${sharpeMedian}</td>
                <td>${drawdownAvg} / ${drawdownMedian}</td>
                <td>${returnAvg} / ${returnMedian}</td>
                <td>
                    <div class="tag-list">
                        <span class="tag">组合 ${group.sample_size}</span>
                        <span class="tag" title="${taskTitle}">任务 ${taskIds.length}</span>
                    </div>
                </td>
            </tr>
        `;
    }

    splitParameters(parameters) {
        const base = {};
        const features = {};
        Object.entries(parameters || {}).forEach(([key, value]) => {
            if (key.includes('__')) {
                const [metric, param] = key.split('__');
                if (!features[metric]) {
                    features[metric] = {};
                }
                features[metric][param] = value;
            } else {
                base[key] = value;
            }
        });
        return { base, features };
    }

    renderParameterBlocks(baseParameters, featureParameters) {
        const baseJson = HtmlUtils.escape(JSON.stringify(baseParameters || {}, null, 2));
        const featureEntries = Object.entries(featureParameters || {});
        const hasFeatures = featureEntries.length > 0;
        const featureJson = HtmlUtils.escape(
            hasFeatures ? JSON.stringify(Object.fromEntries(featureEntries), null, 2) : '{}'
        );
        if (!hasFeatures) {
            return `
                <div class="compare-params">
                    ${this.renderParameterBlock('基础参数', baseJson)}
                </div>
            `;
        }
        return `
            <div class="compare-params compare-params--with-features">
                ${this.renderParameterBlock('基础参数', baseJson)}
                ${this.renderParameterBlock('特征参数', featureJson)}
            </div>
        `;
    }

    renderParameterBlock(title, content) {
        return `
            <div class="compare-params-block">
                <h5>${title}</h5>
                <pre>${content}</pre>
            </div>
        `;
    }

    encodePayload(payload) {
        try {
            return encodeURIComponent(JSON.stringify(payload ?? {}));
        } catch (err) {
            console.warn('Failed to encode payload', err);
            return '';
        }
    }

    decodePayload(encoded) {
        if (!encoded) return {};
        try {
            const raw = decodeURIComponent(encoded);
            return JSON.parse(raw);
        } catch (err) {
            console.warn('Failed to decode payload', err);
            return {};
        }
    }

    buildDefaultStrategyName(group) {
        if (!group) {
            return 'optimizer-strategy';
        }
        const symbol = (group.symbol || 'symbol').toString();
        const strategy = (group.strategy || 'strategy').toString();
        return `${symbol}-${strategy}-${group.sample_size || 1}`;
    }

    pickPlanId(plans) {
        if (!Array.isArray(plans) || !plans.length) {
            return '';
        }
        const withId = plans.find((plan) => plan && plan.id);
        return (withId && withId.id) || '';
    }

    async handleGenerateStrategy(button) {
        if (!button) return;
        const base = this.decodePayload(button.dataset.base);
        const features = this.decodePayload(button.dataset.features);
        const planId = button.dataset.planId && button.dataset.planId.trim() ? button.dataset.planId : undefined;
        const suggested = button.dataset.strategyName || '';
        const strategyName = window.prompt('请输入策略名称', suggested);
        if (!strategyName) return;
        try {
            const response = await this.postJson('/api/optimize/strategies', {
                strategy_name: strategyName,
                plan_id: planId,
                base_parameters: base,
                feature_parameters: features,
            });
            const filePath = response.file_path || '策略已生成';
            alert(`策略已生成：${filePath}`);
        } catch (err) {
            console.error(err);
            alert('策略生成失败，请稍后重试。');
        }
    }

    async postJson(url, data) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: data ? JSON.stringify(data) : undefined,
        });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(text || '请求失败');
        }
        return res.json().catch(() => ({}));
    }
}

window.addEventListener('DOMContentLoaded', () => {
    const appRoot = document.getElementById('optimizer-app');
    if (appRoot) {
        const app = new OptimizerApp(appRoot);
        app.init();
    }
    const compareRoot = document.getElementById('parameter-compare-app');
    if (compareRoot) {
        const compareApp = new ParameterComparisonApp(compareRoot);
        compareApp.init();
    }
});
