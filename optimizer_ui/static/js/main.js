const DEFAULT_BASE_PARAMETER_TEMPLATE = {
    long_thresholds: [0.75, 0.8, 0.85],
    short_thresholds: [-0.75, -0.8, -0.85],
    persistence_seconds: [2, 3, 4],
    cooldown_threshold: [0, 0.1, 0.2],
};

const METRIC_FEATURE_PARAMETER_TEMPLATES = {
    obi: {
        imbalance_threshold: [0.55, 0.6, 0.65],
        obi_window_seconds: [10, 20, 30],
        obi_zscore: [0.8, 1.0, 1.2],
    },
    ofi: {
        ofi_window_seconds: [10, 20, 30],
        ofi_zscore: [0.8, 1.0, 1.2],
        cvd_zscore: [0.8, 1.0, 1.2],
    },
    trend: {
        trend_score_long: [0.7, 0.8, 0.9],
        trend_score_short: [-0.7, -0.8, -0.9],
        trend_smoothing_window: [30, 45, 60],
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
            metadata: { metrics: [], algorithms: [] },
            plans: [],
        };
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
        if (!force && this.state.metadata.metrics.length) return;
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
        const { metrics, algorithms } = this.state.metadata;
        if (!metrics.length && !algorithms.length) {
            container.innerHTML = '<span class="tag">未检测到配置</span>';
            return;
        }
        const metricTags = metrics.map((m) => `<span class="tag">指标 · ${m}</span>`).join('');
        const algorithmTags = algorithms.map((a) => `<span class="tag">算法 · ${a}</span>`).join('');
        container.innerHTML = metricTags + algorithmTags;
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
        const metrics = plan.metrics?.join(', ') || '-';
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
                <td>${metrics}</td>
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

    async openReportModal(planId) {
        try {
            const res = await fetch(`/api/optimize/plans/${planId}/reports`);
            if (!res.ok) throw new Error('failed to load reports');
            const data = await res.json();
            const reports = data.reports ?? [];
            const content = `
                <div class="modal-panel">
                    <header>
                        <h3>计划报告</h3>
                        <button class="btn btn-ghost" data-modal-close>关闭</button>
                    </header>
                    <main>
                                                ${reports.length
                            ? reports.map((report) => this.renderReportCard(report)).join('')
                            : '<div class="empty-state">暂无报告</div>'}
                    </main>
                </div>
            `;
            this.showModal(content);
        } catch (err) {
            console.error(err);
            alert('加载报告列表失败');
        }
    }

    renderReportCard(report) {
        const statusClass = report.state === 'completed' ? 'success' : report.state === 'failed' ? 'failed' : 'running';
        const statusLabel = report.state === 'completed' ? '完成' : report.state === 'failed' ? '失败' : '进行中';
        const created = report.created_at ? new Date(report.created_at).toLocaleString() : '-';
        const completed = report.completed_at ? new Date(report.completed_at).toLocaleString() : '-';
        const summary = report.report?.summary;
        const metricsHtml = summary
            ? `
                <div class="tag-list">
                    <span class="tag">Sharpe ${summary.best_sharpe ?? '-'}</span>
                    <span class="tag">回撤 ${summary.best_max_drawdown ?? '-'}</span>
                    <span class="tag">组合 ${summary.successful_combinations ?? '-'}/${summary.tested_combinations ?? '-'}</span>
                </div>
            `
            : '';
        return `
            <article class="opt-card">
                <header class="section-header">
                    <div>
                        <h3>任务 ${report.task_id}</h3>
                        <p class="section-subtitle">开始于 ${created}${completed !== '-' ? ` · 完成于 ${completed}` : ''}</p>
                    </div>
                    <span class="status-pill ${statusClass}">${statusLabel}</span>
                </header>
                ${metricsHtml}
                ${report.message ? `<p class="section-subtitle" style="margin-top:0.8rem;">${report.message}</p>` : ''}
                ${this.renderFailReasons(report)}
            </article>
        `;
    }

    renderFailReasons(report) {
        const fails = report.report?.fail_reasons ?? [];
        if (!fails.length) return '';
        return `
            <div style="margin-top:1rem;">
                <h4 style="margin-bottom:0.5rem;">失败组合</h4>
                <ul style="list-style:disc; padding-left:1.5rem; color:#94a3b8;">
                    ${fails.map((item) => `<li>${item}</li>`).join('')}
                </ul>
            </div>
        `;
    }

    openPlanDialog(existingPlan = null) {
        const isEdit = Boolean(existingPlan);
        const { metrics, algorithms } = this.state.metadata;
        const metricBucket = [...metrics];
        const existingMetrics = new Set(existingPlan?.metrics ?? []);
        if (existingPlan) {
            existingPlan.metrics.forEach((metric) => {
                if (!metricBucket.includes(metric)) {
                    metricBucket.push(metric);
                }
            });
        }
        const metricCheckboxes = metricBucket
            .map((metric) => {
                const checked = !isEdit || existingMetrics.has(metric);
                return `
                <label class="tag">
                    <input type="checkbox" name="metrics" value="${metric}" ${checked ? 'checked' : ''} /> ${metric}
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
                            <label>指标选择</label>
                            <div class="tag-list">${metricCheckboxes || '<span class="tag">暂无预设指标</span>'}</div>
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
        this.showModal(formHtml, () => this.handlePlanFormSubmit(existingPlan?.id || null));
        this.initializePlanForm(existingPlan);
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

    buildParameterTemplates(selectedMetrics) {
        const base = this.cloneValue(DEFAULT_BASE_PARAMETER_TEMPLATE);
        const features = {};
        selectedMetrics.forEach((metric) => {
            const template = METRIC_FEATURE_PARAMETER_TEMPLATES[metric];
            if (template) {
                features[metric] = this.cloneValue(template);
            } else {
                features[metric] = {};
            }
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

    mergeFeatureTemplates(selectedMetrics, currentFeatures, templateFeatures) {
        const merged = {};
        const featureSource = currentFeatures && typeof currentFeatures === 'object' && !Array.isArray(currentFeatures)
            ? currentFeatures
            : {};
        selectedMetrics.forEach((metric) => {
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

        const updateTemplates = () => {
            const selectedMetrics = [...form.querySelectorAll('input[name="metrics"]:checked')].map((el) => el.value);
            const templates = this.buildParameterTemplates(selectedMetrics);
            if (baseField.dataset.userEdited !== 'true') {
                baseField.value = JSON.stringify(templates.base, null, 2);
            }
            const currentFeatures = this.safeParseJson(featureField.value, {});
            const mergedFeatures = featureField.dataset.userEdited === 'true'
                ? this.mergeFeatureTemplates(selectedMetrics, currentFeatures, templates.features)
                : templates.features;
            featureField.value = JSON.stringify(mergedFeatures, null, 2);
        };

        const checkboxList = form.querySelectorAll('input[name="metrics"]');
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

    initializePlanForm(existingPlan) {
        const form = document.getElementById('plan-form');
        if (!form) return;
        const setters = {
            name: existingPlan?.name ?? '',
            strategy: existingPlan?.strategy ?? '',
            symbol: existingPlan?.symbol ?? '',
            frequency_minutes: existingPlan?.frequency_minutes ?? 0,
            start_date: existingPlan?.start_date ?? '',
            end_date: existingPlan?.end_date ?? '',
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
            const baseJson = JSON.stringify(baseData && Object.keys(baseData).length ? baseData : DEFAULT_BASE_PARAMETER_TEMPLATE, null, 2);
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
        const metrics = [...form.querySelectorAll('input[name="metrics"]:checked')].map((el) => el.value);
        if (!metrics.length) throw new Error('至少选择一个指标');
        const baseParameters = this.parseBaseParameters(getValue('base_parameters'));
        const featureParameters = this.parseFeatureParameters(getValue('feature_parameters'));
        const normalizedFeatures = {};
        metrics.forEach((metric) => {
            if (featureParameters[metric]) {
                normalizedFeatures[metric] = featureParameters[metric];
            }
        });
        const iterationsRaw = getValue('iterations');
        const payload = {
            name: getValue('name'),
            symbol: getValue('symbol').toUpperCase(),
            strategy: getValue('strategy') || 'default_strategy',
            metrics,
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

window.addEventListener('DOMContentLoaded', () => {
    const appRoot = document.getElementById('optimizer-app');
    if (!appRoot) return;
    const app = new OptimizerApp(appRoot);
    app.init();
});
