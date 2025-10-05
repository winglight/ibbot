(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const strategiesNamespace = appModules.strategies = appModules.strategies || {};
    const detailsNamespace = strategiesNamespace.details = strategiesNamespace.details || {};

    class StrategyDetailsController {
        constructor(app) {
            this.app = app;
            this.strategyDetailsState = this.createEmptyStrategyDetailsState();
            this.strategyDetailsTabListenerBound = false;
            this.currentStrategyDetails = null;
        }

        getManagedProperties() {
            return [
                'strategyDetailsState',
                'strategyDetailsTabListenerBound',
                'currentStrategyDetails'
            ];
        }

        get manager() {
            return this.app?.strategiesManager || null;
        }

        createEmptyStrategyDetailsState() {
            return {
                name: null,
                basicInfo: null,
                strategyType: null,
                initialSymbol: '',
                defaultTabTarget: '#charts',
                tabLoaded: {},
                fetches: {},
                internalInfo: null,
                internalInfoCached: false,
                preloadedRiskSettings: null,
            };
        }

        async openStrategyDetails(name) {
            try {
                this.prepareStrategyDetailsModal(name);
                this.ensureStrategyDetailsTabListener();
                this.showStrategyDetailsModal();
                await this.initializeStrategyDetailsContent(name);
                this.activateStrategyDetailsDefaultTab();
                await this.requestActiveStrategyDetailsTabLoad();
            } catch (error) {
                console.error('Open details error:', error);
                this.app?.showAlert?.('danger', 'Failed to open strategy details');
            }
        }

        initializeStrategyDetailsState(name) {
            this.strategyDetailsState = this.createEmptyStrategyDetailsState();
            this.strategyDetailsState.name = name;
        }

        prepareStrategyDetailsModal(name) {
            this.initializeStrategyDetailsState(name);
            this.currentStrategyDetails = name;

            const typeEl = document.getElementById('detailsStrategyType');
            if (typeEl) typeEl.textContent = '-';
            const trigEl = document.getElementById('detailsTriggerCount');
            if (trigEl) trigEl.textContent = '0';
            const lastEl = document.getElementById('detailsLastTriggered');
            if (lastEl) lastEl.textContent = '-';

            const titleEl = document.getElementById('detailsStrategyName');
            if (titleEl) {
                const newTitle = titleEl.cloneNode(true);
                titleEl.parentNode.replaceChild(newTitle, titleEl);
                newTitle.textContent = name;
                newTitle.title = '双击可重命名';
                newTitle.style.cursor = 'text';
                newTitle.ondblclick = () => {
                    const original = newTitle.textContent || name;
                    const input = document.createElement('input');
                    input.type = 'text';
                    input.className = 'form-control form-control-sm d-inline-block';
                    input.style.width = Math.max(120, original.length * 9) + 'px';
                    input.value = original;
                    newTitle.replaceWith(input);
                    input.focus();
                    const commit = async () => {
                        const newName = input.value.trim();
                        if (!newName || newName === original) {
                            input.replaceWith(newTitle);
                            newTitle.textContent = original;
                            return;
                        }
                        try {
                            await this.app?.apiCall?.(`/strategies/${encodeURIComponent(original)}/rename`, {
                                method: 'POST',
                                body: JSON.stringify({ new_name: newName, rename_file: false }),
                            });
                            this.app?.showAlert?.('success', `策略已重命名为 ${newName}`);
                            newTitle.textContent = newName;
                            this.currentStrategyDetails = newName;
                            input.replaceWith(newTitle);
                            if (this.manager?.loadStrategies) {
                                this.manager.loadStrategies();
                            }
                        } catch (err) {
                            console.error('Rename strategy error:', err);
                            this.app?.showAlert?.('danger', '重命名失败');
                            input.replaceWith(newTitle);
                            newTitle.textContent = original;
                        }
                    };
                    input.addEventListener('keydown', (event) => {
                        if (event.key === 'Enter') {
                            event.preventDefault();
                            commit();
                        } else if (event.key === 'Escape') {
                            event.preventDefault();
                            input.replaceWith(newTitle);
                            newTitle.textContent = original;
                        }
                    });
                    input.addEventListener('blur', commit);
                };
            }

            const symbolWrap = document.getElementById('detailsPrimarySymbolWrap');
            if (symbolWrap) symbolWrap.classList.add('d-none');
            const symbolEl = document.getElementById('detailsPrimarySymbol');
            if (symbolEl) symbolEl.textContent = '';

            const kpiIds = ['kpiPnL', 'kpiWinRate', 'kpiMaxDD', 'kpiSharpe', 'kpiTrades', 'kpiAvgDur'];
            kpiIds.forEach((id) => {
                const el = document.getElementById(id);
                if (el) el.textContent = '--';
            });

            const customSection = document.getElementById('customRangeSection');
            if (customSection) customSection.style.display = 'none';
            const startInput = document.getElementById('perfStart');
            if (startInput) startInput.value = '';
            const endInput = document.getElementById('perfEnd');
            if (endInput) endInput.value = '';

            const rangeSelect = document.getElementById('performanceRange');
            if (rangeSelect) {
                const newSelect = rangeSelect.cloneNode(true);
                rangeSelect.parentNode.replaceChild(newSelect, rangeSelect);
                newSelect.value = '1d';
                newSelect.addEventListener('change', async () => {
                    if (customSection) customSection.style.display = newSelect.value === 'custom' ? '' : 'none';
                    if (newSelect.value !== 'custom') {
                        await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
                    }
                });
            }

            const applyBtn = document.getElementById('applyCustomRangeBtn');
            if (applyBtn) {
                const newBtn = applyBtn.cloneNode(true);
                applyBtn.parentNode.replaceChild(newBtn, applyBtn);
                newBtn.addEventListener('click', async () => {
                    await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
                });
            }

            ['cumulativePnLChart', 'drawdownChart', 'pnlDistributionChart', 'winLossChart'].forEach((chartId) => {
                this.app?.ensureChartDestroyed?.(chartId);
                const canvas = document.getElementById(chartId);
                if (canvas && canvas.getContext) {
                    const ctx = canvas.getContext('2d');
                    if (ctx) {
                        ctx.clearRect(0, 0, canvas.width || canvas.offsetWidth || 0, canvas.height || canvas.offsetHeight || 0);
                    }
                }
            });

            const summaryWrap = document.getElementById('summary-tab-wrap');
            const riskWrap = document.getElementById('risk-settings-tab-wrap');
            const ordersWrap = document.getElementById('internal-orders-tab-wrap');
            const execWrap = document.getElementById('executions-tab-wrap');
            const apiWrap = document.getElementById('api-examples-tab-wrap');
            const summaryPane = document.getElementById('summary');
            const riskPane = document.getElementById('risk-settings');
            const ordersPane = document.getElementById('internal-orders');
            summaryWrap?.classList.add('d-none');
            riskWrap?.classList.add('d-none');
            ordersWrap?.classList.add('d-none');
            execWrap?.classList.remove('d-none');
            apiWrap?.classList.remove('d-none');
            summaryPane?.classList.add('d-none');
            riskPane?.classList.add('d-none');
            ordersPane?.classList.add('d-none');

            const chartsTabText = document.getElementById('charts-tab-text');
            if (chartsTabText) chartsTabText.textContent = 'Charts';

            const summaryLists = [
                ['summaryHighlights', '暂无摘要'],
                ['summaryRiskParams', '暂无数据'],
                ['summaryPositionSettings', '暂无限制'],
                ['summarySchedule', '暂无排程'],
            ];
            summaryLists.forEach(([id, placeholder]) => {
                const el = document.getElementById(id);
                if (el) el.innerHTML = `<li class=\"text-muted\">${placeholder}</li>`;
            });
            const summaryDataLog = document.getElementById('summaryDataLogList');
            if (summaryDataLog) summaryDataLog.innerHTML = '<li class="text-muted">暂无记录</li>';

            const summaryParamBody = document.querySelector('#summaryParameterTable tbody');
            if (summaryParamBody) {
                summaryParamBody.innerHTML = '<tr><td colspan="4" class="text-muted">打开 Summary 标签后加载</td></tr>';
            }
            const summaryRiskBody = document.querySelector('#summaryRiskTable tbody');
            if (summaryRiskBody) {
                summaryRiskBody.innerHTML = '';
            }

            const symbolInput = document.getElementById('summaryPrimarySymbolInput');
            if (symbolInput) {
                symbolInput.value = '';
                this.app.summaryPrimaryCurrent = null;
                this.app?.updateSummaryPrimaryStatus?.('empty');
            }
            const dataSelect = document.getElementById('summaryDataFeedSelect');
            if (dataSelect) dataSelect.value = 'market_data';
            const pushSwitch = document.getElementById('summaryDataPushSwitch');
            if (pushSwitch) pushSwitch.checked = false;
            const pushStatusEl = document.getElementById('summaryDataPushStatus');
            if (pushStatusEl) pushStatusEl.textContent = '未推送';

            const summaryStatusIds = [
                'summaryStatusReceiving',
                'summaryStatusRuntime',
                'summaryStatusProcessed',
                'summaryStatusThresholdHits',
                'summaryStatusBuySignals',
                'summaryStatusSellSignals',
            ];
            summaryStatusIds.forEach((id) => {
                const el = document.getElementById(id);
                if (el) el.textContent = '-';
            });
            this.app.summaryStatusTarget = null;

            this.app.data_push_state = {};
            this.app.summaryPushAwaitingData = false;
            if (this.app.summaryPushAwaitingTimer) {
                clearTimeout(this.app.summaryPushAwaitingTimer);
                this.app.summaryPushAwaitingTimer = null;
            }
            this.app.summaryDataFeedCurrent = null;

            const riskForm = document.getElementById('riskSettingsForm');
            if (riskForm) riskForm.reset();
            if (this.app) {
                this.app.currentRiskSettings = null;
                this.app.updateRiskSettingsStatus?.('default', '加载风控设置后显示。');
            }

            const ordersBody = document.querySelector('#internalOrdersTable tbody');
            if (ordersBody) {
                ordersBody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">等待加载</td></tr>';
            }
            if (this.manager) {
                this.manager.internalStrategyOrders = [];
            }

            const execBody = document.querySelector('#executionsTable tbody');
            if (execBody) {
                execBody.innerHTML = '<tr><td colspan="9" class="text-center text-muted">等待加载</td></tr>';
            }
            const execHead = document.querySelector('#executionsTable thead tr');
            if (execHead) {
                execHead.innerHTML = `
                  <th>ID</th>
                  <th>Symbol</th>
                  <th>Created At</th>
                  <th>Action</th>
                  <th>Qty</th>
                  <th>Price</th>
                  <th>Status</th>
                  <th>PnL</th>
                  <th>Commission</th>
                `;
            }
            const pagination = document.getElementById('executionsPagination');
            if (pagination) {
                pagination.innerHTML = '<li class="page-item disabled"><span class="page-link">-</span></li>';
            }
            if (this.app?.executionsPagination) {
                this.app.executionsPagination.currentPage = 1;
                this.app.executionsPagination.totalItems = 0;
                this.app.executionsPagination.allExecutions = [];
            }

            const pageSizeSelect = document.getElementById('executionsPageSize');
            if (pageSizeSelect) {
                const newSelect = pageSizeSelect.cloneNode(true);
                pageSizeSelect.parentNode.replaceChild(newSelect, pageSizeSelect);
                newSelect.value = String(this.app?.executionsPagination?.pageSize || this.app?.strategyExecutionsDefaultLimit);
                newSelect.addEventListener('change', async () => {
                    const newSize = parseInt(newSelect.value, 10) || this.app?.strategyExecutionsDefaultLimit;
                    if (this.app?.executionsPagination) {
                        this.app.executionsPagination.pageSize = newSize;
                        this.app.executionsPagination.currentPage = 1;
                    }
                    await this.triggerStrategyDetailsTabLoad('#executions', { force: true }).catch(() => {});
                    if (this.strategyDetailsState?.strategyType === 'internal') {
                        await this.triggerStrategyDetailsTabLoad('#internal-orders', { force: true }).catch(() => {});
                    }
                    await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
                });
            }

            const refreshBtn = document.getElementById('refreshExecutionsBtn');
            if (refreshBtn) {
                refreshBtn.disabled = true;
                refreshBtn.title = 'WebSocket updates executions automatically';
            }

            const clearBtn = document.getElementById('clearExecutionsBtn');
            if (clearBtn) {
                const newClear = clearBtn.cloneNode(true);
                clearBtn.parentNode.replaceChild(newClear, clearBtn);
                newClear.addEventListener('click', async () => {
                    try {
                        const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                        if (!currentName) return;
                        if (!confirm(`确定要清空策略 "${currentName}" 的全部执行记录吗？此操作不可撤销！`)) {
                            return;
                        }
                        await this.app?.apiCall?.(`/strategies/${encodeURIComponent(currentName)}/executions`, { method: 'DELETE' });
                        this.manager?.clearStrategyExecutionsCache?.(currentName);
                        if (this.app?.executionsPagination) {
                            this.app.executionsPagination.allExecutions = [];
                            this.app.executionsPagination.totalItems = 0;
                            this.app.executionsPagination.currentPage = 1;
                        }
                        await this.triggerStrategyDetailsTabLoad('#executions', { force: true }).catch(() => {});
                        await this.triggerStrategyDetailsTabLoad('#charts', { force: true }).catch(() => {});
                        if (this.strategyDetailsState?.strategyType === 'internal') {
                            await this.triggerStrategyDetailsTabLoad('#internal-orders', { force: true }).catch(() => {});
                        }
                        this.app?.showAlert?.('success', '已清空该策略的执行记录');
                    } catch (error) {
                        console.error('Clear executions error:', error);
                        this.app?.showAlert?.('danger', '清空执行记录失败');
                    }
                });
            }

            const exportBtn = document.getElementById('exportExecutionsBtn');
            if (exportBtn) {
                const newExport = exportBtn.cloneNode(true);
                exportBtn.parentNode.replaceChild(newExport, exportBtn);
                newExport.addEventListener('click', () => {
                    const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                    if (currentName) {
                        this.app?.exportExecutionsCsv?.(currentName);
                    }
                });
            }

            const resetBtn = document.getElementById('resetStrategyDataBtn');
            if (resetBtn) {
                const newReset = resetBtn.cloneNode(true);
                resetBtn.parentNode.replaceChild(newReset, resetBtn);
                newReset.addEventListener('click', async () => {
                    const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                    if (!currentName) return;
                    if (!confirm(`确定要重置策略 "${currentName}" 的全部订单与统计数据吗？此操作不可撤销！`)) {
                        return;
                    }
                    const originalHtml = newReset.innerHTML;
                    newReset.disabled = true;
                    newReset.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
                    try {
                        const res = await this.app?.apiCall?.(`/strategies/${encodeURIComponent(currentName)}/reset-data`, {
                            method: 'POST',
                        });
                        await Promise.all([
                            this.triggerStrategyDetailsTabLoad('#executions', { force: true }),
                            this.triggerStrategyDetailsTabLoad('#charts', { force: true }),
                            this.triggerStrategyDetailsTabLoad('#pnl-calendar', { force: true }),
                            this.triggerStrategyDetailsTabLoad('#summary', { force: true }),
                            this.triggerStrategyDetailsTabLoad('#internal-orders', { force: true }),
                        ]);
                        try {
                            const detailRes = await this.app?.apiCall?.(`/strategies/${encodeURIComponent(currentName)}`);
                            const detailStrategy = detailRes && typeof detailRes === 'object'
                                ? (detailRes.success === undefined
                                    ? detailRes
                                    : detailRes.success
                                        ? detailRes.strategy || null
                                        : null)
                                : null;
                            const normalizedDetail = detailStrategy
                                ? this.app?.normalizeStrategyDocument?.(detailStrategy) || detailStrategy
                                : null;
                            this.updateStrategyDetailsHeader(normalizedDetail);
                        } catch (refreshErr) {
                            console.warn('Failed to refresh strategy info after reset', refreshErr);
                            this.updateStrategyDetailsHeader(null);
                        }
                        if (this.manager?.loadStrategies) {
                            await this.manager.loadStrategies({ force: true }).catch((err) => {
                                console.warn('Failed to refresh strategies list after reset', err);
                            });
                        }
                        const successMessage = res?.message || '策略订单与统计数据已重置';
                        this.app?.showAlert?.('success', successMessage);
                    } catch (error) {
                        console.error('Reset strategy data error:', error);
                        this.app?.showAlert?.('danger', error?.message || '重置策略数据失败');
                    } finally {
                        newReset.disabled = false;
                        newReset.innerHTML = originalHtml;
                    }
                });
            }

            const renameBtn = document.getElementById('renameStrategyFromDetails');
            if (renameBtn) {
                const newBtn = renameBtn.cloneNode(true);
                renameBtn.parentNode.replaceChild(newBtn, renameBtn);
                newBtn.addEventListener('click', () => {
                    const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                    if (!currentName) return;
                    this.app?.showConfigModal?.(currentName);
                });
            }

            const resetStrategyBtn = document.getElementById('resetStrategyFromDetails');
            if (resetStrategyBtn) {
                const newReset = resetStrategyBtn.cloneNode(true);
                resetStrategyBtn.parentNode.replaceChild(newReset, resetStrategyBtn);
                newReset.addEventListener('click', async () => {
                    const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                    if (!currentName) return;
                    const originalHtml = newReset.innerHTML;
                    newReset.disabled = true;
                    newReset.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>';
                    try {
                        await this.app?.apiCall?.(`/strategies/${encodeURIComponent(currentName)}/reset`, { method: 'POST' });
                        this.app?.showAlert?.('success', '策略参数已重置');
                        this.manager?.clearStrategyExecutionsCache?.(currentName);
                        await this.manager?.loadStrategies?.({ force: true });
                        await this.requestActiveStrategyDetailsTabLoad({ force: true });
                    } catch (error) {
                        console.error('重置策略失败:', error);
                        this.app?.showAlert?.('danger', '重置失败');
                    } finally {
                        newReset.disabled = false;
                        newReset.innerHTML = originalHtml;
                    }
                });
            }

            const deleteBtn = document.getElementById('deleteStrategyFromDetails');
            if (deleteBtn) {
                const newDelete = deleteBtn.cloneNode(true);
                deleteBtn.parentNode.replaceChild(newDelete, deleteBtn);
                newDelete.addEventListener('click', async () => {
                    const currentName = document.getElementById('detailsStrategyName')?.textContent || name;
                    if (!currentName) return;
                    await this.app?.deleteStrategy?.(currentName);
                    const modalEl = document.getElementById('strategyDetailsModal');
                    const modal = bootstrap.Modal.getInstance(modalEl);
                    if (modal) modal.hide();
                });
            }

            const calendarContainer = document.getElementById('pnlCalendarContainer');
            if (calendarContainer) {
                calendarContainer.innerHTML = '<div class="text-muted text-center py-4">打开 PnL Calendar 标签以加载数据</div>';
            }

            const apiContainer = document.getElementById('apiExampleContent');
            if (apiContainer) {
                apiContainer.innerHTML = '<p class="text-muted">打开 API Examples 标签查看示例。</p>';
            }

            this.app?.setupStrategyChartTab?.();
            this.app?.resetStrategyChartState?.({ keepControls: false, resetSymbol: true });
        }

        ensureStrategyDetailsTabListener() {
            if (this.strategyDetailsTabListenerBound) return;
            const tabs = document.getElementById('strategyDetailsTabs');
            if (!tabs) return;
            tabs.addEventListener('shown.bs.tab', (event) => {
                const target = event.target?.getAttribute('data-bs-target') || event.target?.dataset?.bsTarget;
                if (target) {
                    this.triggerStrategyDetailsTabLoad(target).catch(() => {});
                }
            });
            this.strategyDetailsTabListenerBound = true;
        }

        showStrategyDetailsModal() {
            const modalEl = document.getElementById('strategyDetailsModal');
            if (!modalEl) return;
            const modal = bootstrap.Modal?.getOrCreateInstance
                ? bootstrap.Modal.getOrCreateInstance(modalEl)
                : new bootstrap.Modal(modalEl);
            try {
                modal.show();
            } catch (err) {
                console.warn('Failed to show strategy details modal:', err);
            }
        }

        async initializeStrategyDetailsContent(name) {
            const cached = this.app?.getStrategyFromCache?.(name);
            const normalizedCached = cached ? this.app?.normalizeStrategyDocument?.(cached) : null;
            if (normalizedCached) {
                this.strategyDetailsState.basicInfo = normalizedCached;
                this.updateStrategyDetailsHeader(normalizedCached);
                this.configureStrategyDetailsTabs(normalizedCached);
                this.app?.setupStrategyChartState?.(normalizedCached, name);
            }

            const strategy = await this.fetchStrategyBasicInfo(name);
            if (this.strategyDetailsState.name !== name) {
                return;
            }
            const resolved = strategy
                || this.strategyDetailsState.basicInfo
                || normalizedCached
                || null;
            this.strategyDetailsState.basicInfo = resolved;
            this.updateStrategyDetailsHeader(resolved);
            this.configureStrategyDetailsTabs(resolved);
            this.app?.setupStrategyChartState?.(resolved, name);
        }

        updateStrategyDetailsHeader(strategy) {
            const typeEl = document.getElementById('detailsStrategyType');
            const trigEl = document.getElementById('detailsTriggerCount');
            const lastEl = document.getElementById('detailsLastTriggered');
            if (typeEl) {
                const type = this.app?.determineStrategyType?.(strategy) || strategy?.strategy_type || '-';
                typeEl.textContent = type || '-';
            }
            if (trigEl) trigEl.textContent = strategy?.trigger_count ?? 0;
            if (lastEl) {
                lastEl.textContent = strategy?.last_triggered
                    ? new Date(strategy.last_triggered).toLocaleString()
                    : '-';
            }
            const symbolWrap = document.getElementById('detailsPrimarySymbolWrap');
            const symbolEl = document.getElementById('detailsPrimarySymbol');
            if (symbolWrap && symbolEl) {
                const symbol = this.app?.resolveStrategySymbol?.(strategy);
                if (symbol) {
                    symbolEl.textContent = symbol;
                    symbolWrap.classList.remove('d-none');
                } else {
                    symbolEl.textContent = '';
                    symbolWrap.classList.add('d-none');
                }
            }
        }

        configureStrategyDetailsTabs(strategy) {
            const resolvedType = this.app?.determineStrategyType?.(strategy) || strategy?.strategy_type || 'external';
            const normalizedType = typeof resolvedType === 'string' ? resolvedType.toLowerCase() : 'external';
            const isInternal = normalizedType === 'internal';
            const summaryWrap = document.getElementById('summary-tab-wrap');
            const riskWrap = document.getElementById('risk-settings-tab-wrap');
            const ordersWrap = document.getElementById('internal-orders-tab-wrap');
            const execWrap = document.getElementById('executions-tab-wrap');
            const apiWrap = document.getElementById('api-examples-tab-wrap');
            const summaryPane = document.getElementById('summary');
            const riskPane = document.getElementById('risk-settings');
            const ordersPane = document.getElementById('internal-orders');
            const chartsTabText = document.getElementById('charts-tab-text');

            if (isInternal) {
                summaryWrap?.classList.remove('d-none');
                riskWrap?.classList.remove('d-none');
                ordersWrap?.classList.remove('d-none');
                execWrap?.classList.add('d-none');
                apiWrap?.classList.add('d-none');
                summaryPane?.classList.remove('d-none');
                riskPane?.classList.remove('d-none');
                ordersPane?.classList.remove('d-none');
                if (chartsTabText) chartsTabText.textContent = 'Visual Metrics';
                this.app?.updateRiskSettingsStatus?.('loading');
                this.strategyDetailsState.defaultTabTarget = '#summary';
            } else {
                summaryWrap?.classList.add('d-none');
                riskWrap?.classList.add('d-none');
                ordersWrap?.classList.add('d-none');
                execWrap?.classList.remove('d-none');
                apiWrap?.classList.remove('d-none');
                summaryPane?.classList.add('d-none');
                riskPane?.classList.add('d-none');
                ordersPane?.classList.add('d-none');
                if (chartsTabText) chartsTabText.textContent = 'Charts';
                if (this.app) {
                    this.app.currentRiskSettings = null;
                    this.app.updateRiskSettingsStatus?.('default', '风控设置将在内部策略中显示。');
                }
                this.strategyDetailsState.defaultTabTarget = '#charts';
            }

            if (strategy && typeof strategy === 'object') {
                strategy.strategy_type = normalizedType;
            }
            this.strategyDetailsState.strategyType = isInternal ? 'internal' : 'external';
        }

        activateStrategyDetailsDefaultTab() {
            const state = this.strategyDetailsState;
            const target = state?.defaultTabTarget || '#charts';
            const button = document.querySelector(`#strategyDetailsTabs button[data-bs-target="${target}"]`);
            if (!button) return;
            try {
                const tabInstance = bootstrap.Tab?.getOrCreateInstance
                    ? bootstrap.Tab.getOrCreateInstance(button)
                    : new bootstrap.Tab(button);
                tabInstance.show();
            } catch (err) {
                console.warn('Tab show failed', err);
            }
        }

        async requestActiveStrategyDetailsTabLoad(options = {}) {
            const active = document.querySelector('#strategyDetailsTabs .nav-link.active');
            if (!active) return;
            const target = active.getAttribute('data-bs-target') || active.dataset?.bsTarget;
            if (target) {
                await this.triggerStrategyDetailsTabLoad(target, options);
            }
        }

        async triggerStrategyDetailsTabLoad(targetSelector, options = {}) {
            const state = this.strategyDetailsState;
            if (!state || !state.name) return;
            const key = (targetSelector || '').replace(/^#/, '');
            if (!key) return;
            if (!options.force && state.tabLoaded[key]) return;
            const loaders = {
                summary: (opts) => this.loadStrategySummaryTab(opts),
                'risk-settings': (opts) => this.loadStrategyRiskTab(opts),
                'internal-orders': (opts) => this.loadStrategyInternalOrdersTab(opts),
                charts: (opts) => this.loadStrategyChartsTab(opts),
                'strategy-chart': (opts) => this.loadStrategyStrategyChartTab(opts),
                executions: (opts) => this.loadStrategyExecutionsTab(opts),
                'pnl-calendar': (opts) => this.loadStrategyPnlCalendarTab(opts),
                'api-examples': () => this.loadStrategyApiExamplesTab(),
            };
            const loader = loaders[key];
            if (!loader) return;
            try {
                await loader(options);
                state.tabLoaded[key] = true;
            } catch (err) {
                console.error(`Failed to load strategy details tab ${targetSelector}:`, err);
                state.tabLoaded[key] = false;
            }
        }

        async fetchStrategyBasicInfo(name) {
            const state = this.strategyDetailsState;
            if (!state || state.name !== name) return null;
            if (!state.fetches.basicInfo) {
                state.fetches.basicInfo = (async () => {
                    let normalized = null;
                    try {
                        const res = await this.app?.apiCall?.(`/strategies/${encodeURIComponent(name)}`);
                        if (res && typeof res === 'object') {
                            let strategyPayload = res;
                            if (Object.prototype.hasOwnProperty.call(res, 'success')) {
                                strategyPayload = res.success ? res.strategy || null : null;
                            }
                            if (strategyPayload && typeof strategyPayload === 'object') {
                                normalized = this.app?.normalizeStrategyDocument?.(strategyPayload) || strategyPayload;
                            }
                        }
                    } catch (err) {
                        console.error('Failed to load strategy info:', err);
                    }
                    if (this.strategyDetailsState && this.strategyDetailsState.name === name) {
                        this.strategyDetailsState.basicInfo = normalized || null;
                    }
                    return normalized || null;
                })();
            }
            try {
                return await state.fetches.basicInfo;
            } catch (err) {
                state.fetches.basicInfo = null;
                throw err;
            }
        }

        async fetchStrategyInternalInfo(name) {
            const state = this.strategyDetailsState;
            if (!state || state.name !== name) return null;
            if (!state.fetches.internalInfo) {
                state.internalInfoCached = false;
                state.fetches.internalInfo = (async () => {
                    try {
                        const res = await this.app?.apiCall?.(`/strategies/${encodeURIComponent(name)}/internal-info`);
                        if (res?.success) {
                            if (!this.strategyDetailsState || this.strategyDetailsState.name !== name) {
                                return null;
                            }
                            const info = res.strategy || {};
                            state.internalInfoCached = Boolean(res.cached || info?.stale);
                            return info;
                        }
                    } catch (err) {
                        console.error('Failed to load internal strategy info:', err);
                    }
                    state.internalInfoCached = false;
                    return null;
                })();
            }
            try {
                return await state.fetches.internalInfo;
            } catch (err) {
                state.fetches.internalInfo = null;
                state.internalInfoCached = false;
                throw err;
            }
        }

        async loadStrategySummaryTab() {
            const state = this.strategyDetailsState;
            if (!state || state.strategyType !== 'internal') return;
            const name = state.name;
            if (!name) return;

            this.app?.ensureSummaryControls?.();

            const infoStatus = document.getElementById('summaryInfoStatus');
            if (infoStatus) {
                infoStatus.textContent = '';
                infoStatus.classList.add('d-none');
                infoStatus.classList.remove('alert-danger');
                infoStatus.classList.add('alert-warning');
            }

            const baseInfo = state.basicInfo || {};
            const fallbackSymbol = baseInfo.primary_symbol || state.initialSymbol || '';
            const symbolInput = document.getElementById('summaryPrimarySymbolInput');
            if (symbolInput) {
                symbolInput.value = fallbackSymbol || '';
                this.app.summaryPrimaryCurrent = fallbackSymbol ? fallbackSymbol.toUpperCase() : null;
                this.app?.updateSummaryPrimaryStatus?.(fallbackSymbol ? 'saved' : 'empty');
            }
            const fallbackSub = baseInfo.data_subscription || '';
            const dataSelect = document.getElementById('summaryDataFeedSelect');
            this.app.summaryDataFeedCurrent = fallbackSub || null;
            if (dataSelect) dataSelect.value = fallbackSub || 'market_data';

            const pushSwitch = document.getElementById('summaryDataPushSwitch');
            if (pushSwitch) pushSwitch.checked = false;
            const pushStatusEl = document.getElementById('summaryDataPushStatus');
            if (pushStatusEl) pushStatusEl.textContent = '未推送';
            this.app.data_push_state = {};
            this.app.summaryPushAwaitingData = false;
            if (this.app.summaryPushAwaitingTimer) {
                clearTimeout(this.app.summaryPushAwaitingTimer);
                this.app.summaryPushAwaitingTimer = null;
            }

            try {
                const info = await this.fetchStrategyInternalInfo(name);
                if (info) {
                    state.internalInfo = info;
                    if (infoStatus) {
                        const usedCached = Boolean(state.internalInfoCached || info?.stale);
                        if (usedCached) {
                            infoStatus.textContent = '显示缓存的策略信息，可能不是最新。';
                            infoStatus.classList.remove('d-none');
                            infoStatus.classList.remove('alert-danger');
                            infoStatus.classList.add('alert-warning');
                        } else {
                            infoStatus.textContent = '';
                            infoStatus.classList.add('d-none');
                        }
                    }

                    const symbol = info.primary_symbol || fallbackSymbol || '';
                    const symbolDisplay = document.getElementById('summaryPrimarySymbol');
                    if (symbolDisplay) {
                        symbolDisplay.textContent = symbol || '-';
                    }

                    if (symbolInput) {
                        symbolInput.value = symbol || '';
                        this.app.summaryPrimaryCurrent = symbol ? symbol.toUpperCase() : null;
                        this.app?.updateSummaryPrimaryStatus?.(symbol ? 'saved' : 'empty');
                    }

                    const subKey = info.data_subscription || fallbackSub || '';
                    this.app.summaryDataFeedCurrent = subKey || null;
                    if (dataSelect) dataSelect.value = subKey || 'market_data';

                    const positionList = document.getElementById('summaryPositionSettings');
                    if (positionList) {
                        const timing = info.position_settings || {};
                        positionList.innerHTML = '';
                        const entries = [];
                        if (timing.position_add_cooldown_minutes) {
                            entries.push(['Add Cooldown', `${timing.position_add_cooldown_minutes} min`]);
                        }
                        if (timing.close_before_close_minutes) {
                            entries.push(['Exit Before Close', `${timing.close_before_close_minutes} min`]);
                        }
                        if (entries.length) {
                            entries.forEach(([label, value]) => {
                                const li = document.createElement('li');
                                li.innerHTML = `<span class="text-muted">${label}:</span> <span class="fw-semibold">${value}</span>`;
                                positionList.appendChild(li);
                            });
                        } else {
                            positionList.innerHTML = '<li class="text-muted">No timing limits</li>';
                        }
                    }

                    const schedList = document.getElementById('summarySchedule');
                    if (schedList) {
                        schedList.innerHTML = '';
                        (info.schedule || []).forEach((item) => {
                            const li = document.createElement('li');
                            li.textContent = `${item.time || ''} ${item.action || ''} ${item.qty || ''} ${item.symbol || ''}`.trim();
                            schedList.appendChild(li);
                        });
                    }

                    const riskBody = document.querySelector('#summaryRiskTable tbody');
                    if (riskBody) {
                        riskBody.innerHTML = '';
                        const rr = info.risk_rules || {};
                        Object.keys(rr).forEach((sym) => {
                            const rule = rr[sym] || {};
                            const tr = document.createElement('tr');
                            tr.innerHTML = `<td>${sym}</td><td>${rule.stop_loss_price ?? ''}</td><td>${rule.take_profit_price ?? ''}</td>`;
                            riskBody.appendChild(tr);
                        });
                    }
                } else if (infoStatus) {
                    infoStatus.textContent = '暂时无法加载策略内部信息。';
                    infoStatus.classList.remove('d-none');
                    infoStatus.classList.remove('alert-warning');
                    infoStatus.classList.add('alert-danger');
                }
            } catch (err) {
                console.error('Failed to load internal strategy info:', err);
            }

            await this.manager?.loadStrategyStatus?.(name, { force: true });
        }

        async loadStrategyRiskTab() {
            if (this.strategyDetailsState.strategyType !== 'internal') {
                return;
            }
            this.app?.ensureRiskSettingsControls?.();
            const name = this.strategyDetailsState.name;
            if (!name) return;
            if (this.strategyDetailsState.preloadedRiskSettings) {
                this.app.currentRiskSettings = this.strategyDetailsState.preloadedRiskSettings;
                this.app?.renderRiskSettingsForm?.(this.app.currentRiskSettings);
                this.app?.updateRiskSettingsStatus?.('loaded');
                return;
            }
            await this.app?.loadStrategyRiskSettings?.(name);
        }

        async loadStrategyInternalOrdersTab() {
            if (this.strategyDetailsState.strategyType !== 'internal') return;
            const name = this.strategyDetailsState.name;
            if (!name) return;
            const limit = this.app?.executionsPagination?.pageSize || this.app?.strategyExecutionsDefaultLimit;
            await this.manager?.loadInternalStrategyOrders?.(name, { limit, force: true });
        }

        async loadStrategyChartsTab() {
            const name = this.strategyDetailsState.name;
            if (!name) return;
            const rangeSelect = document.getElementById('performanceRange');
            let range = rangeSelect ? rangeSelect.value : '1d';
            let start = null;
            let end = null;
            if (range === 'custom') {
                start = document.getElementById('perfStart')?.value || null;
                end = document.getElementById('perfEnd')?.value || null;
                if (!start || !end) {
                    range = '1d';
                    start = null;
                    end = null;
                }
            }
            await this.app?.loadStrategyPerformance?.(name, { range, start, end });
        }

        async loadStrategyStrategyChartTab() {
            const state = this.app?.getStrategyChartState?.();
            if (!state?.currentStrategy) return;
            if (!state.symbol) {
                this.app?.updateStrategyChartStatus?.('缺少标的');
                return;
            }
            await this.app?.loadStrategyChart?.(state.currentStrategy, {
                symbol: state.symbol,
                forceExecutions: true,
            });
        }

        async loadStrategyExecutionsTab() {
            const name = this.strategyDetailsState.name;
            if (!name) return;
            const limit = this.app?.executionsPagination?.pageSize || this.app?.strategyExecutionsDefaultLimit;
            await this.app?.loadStrategyExecutions?.(name, { limit, force: true });
        }

        async loadStrategyPnlCalendarTab() {
            const name = this.strategyDetailsState.name;
            if (!name) return;
            await this.app?.loadPnLCalendar?.(name);
        }

        loadStrategyApiExamplesTab() {
            const container = document.getElementById('apiExampleContent');
            if (!container) return;
            const strategy = this.strategyDetailsState.basicInfo;
            if (strategy?.strategy_type === 'external') {
                const endpoint = strategy.api_endpoint || '/api/strategy';
                const samplePayload = { symbol: 'AAPL', action: 'BUY', quantity: 10, price: 190.12, meta: { note: 'optional' } };
                const payloadStr = JSON.stringify(samplePayload).replace(/\"/g, '\\"');
                const curl = `curl -X POST \"${endpoint}\" \\
  -H \"Content-Type: application/json\" \\
  -d \"${payloadStr}\"`;
                container.innerHTML = `
                  <div class="mb-2"><span class="badge bg-primary">external</span> endpoint: <code>${endpoint}</code></div>
                  <pre class="bg-light p-3"><code>${curl}</code></pre>
                  <p class="text-muted">如果在策略设置中开启鉴权，需要在请求体中加入 <code>{ token: &lt;TOKEN&gt; }</code> 字段，其中 TOKEN 来自策略设置</p>`;
            } else {
                container.innerHTML = '<p class="text-muted">API examples are only available for external strategies.</p>';
            }
        }
    }

    detailsNamespace.StrategyDetailsController = StrategyDetailsController;
    root.StrategyDetailsController = StrategyDetailsController;
})(typeof window !== 'undefined' ? window : globalThis);
