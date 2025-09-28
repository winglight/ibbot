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
        },
        MONITOR_CHART_MAX_MINUTES = 30,
        MONITOR_CHART_MAX_POINTS = 3000,
        MONITOR_SOURCE_TZ = 'America/New_York',
        MONITOR_FALLBACK_TZ = 'Asia/Shanghai'
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

    const fallbackGetMonitorDurationMinutes = (durationValue) => {
        if (!durationValue && durationValue !== 0) return null;
        if (typeof durationValue === 'number') {
            return Number.isFinite(durationValue) ? durationValue : null;
        }
        const normalized = typeof durationValue === 'string'
            ? durationValue.replace(/\s+/g, '').toUpperCase()
            : '';
        if (!normalized) return null;

        const direct = monitoringConstants?.MONITOR_DURATION_MINUTES_MAP || {};
        if (Object.prototype.hasOwnProperty.call(direct, normalized)) {
            return direct[normalized];
        }

        const numeric = Number(normalized);
        if (Number.isFinite(numeric) && numeric > 0) {
            return numeric;
        }

        const match = normalized.match(/^(\d+)([A-Z]+)$/);
        if (!match) return null;

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
    };

    const fallbackRefreshMonitorDurationSelect = (intervalValue, options = {}) => {
        const selectEl = options.selectEl || document.getElementById('monitorDuration');
        if (!selectEl) return [];

        const available = fallbackGetMonitorDurationOptions(intervalValue);
        const previousValue = selectEl.value;
        const preferredValue = options.preferredValue;

        selectEl.innerHTML = '';
        available.forEach((item, index) => {
            const option = document.createElement('option');
            option.value = item.value;
            option.textContent = item.label;
            if (index === 0 && !preferredValue && !previousValue) {
                option.selected = true;
            }
            selectEl.appendChild(option);
        });

        const hasPreferred = preferredValue && available.some(item => item.value === preferredValue);
        const hasPrevious = available.some(item => item.value === previousValue);
        const fallbackValue = available[0]?.value || '';

        if (hasPreferred) {
            selectEl.value = preferredValue;
        } else if (hasPrevious) {
            selectEl.value = previousValue;
        } else if (fallbackValue) {
            selectEl.value = fallbackValue;
        }

        if (options.triggerChange && selectEl.value !== previousValue) {
            selectEl.dispatchEvent(new Event('change'));
        }

        return available;
    };

    const {
        normalizeIntervalKey = fallbackNormalizeIntervalKey,
        getMonitorDurationOptions = fallbackGetMonitorDurationOptions,
        getMonitorDurationMinutes = fallbackGetMonitorDurationMinutes,
        refreshMonitorDurationSelect = fallbackRefreshMonitorDurationSelect
    } = monitoringUtils;

        async function legacyOpenMonitorModal(symbol, positionQty = 0, currentPrice = 0, avgCost = 0, pnl = 0, pnlPercent = 0) {
    try {
        // 获取最新订单以确定建仓/加仓时间
        try {
            await this.loadOrders();
        } catch (e) {}

        const args = Array.from(arguments);
        const positionArgProvided = args.length >= 2;
        const priceArgProvided = args.length >= 3;
        const avgArgProvided = args.length >= 4;
        const pnlArgProvided = args.length >= 5;
        const pnlPctArgProvided = args.length >= 6;

        let resolvedPositionQty = positionArgProvided ? Number(positionQty) || 0 : null;
        let resolvedCurrentPrice = priceArgProvided ? Number(currentPrice) || 0 : null;
        let resolvedAvgCost = avgArgProvided ? Number(avgCost) || 0 : null;
        let resolvedPnl = pnlArgProvided ? Number(pnl) || 0 : null;
        let resolvedPnlPercent = pnlPctArgProvided ? Number(pnlPercent) || 0 : null;

        const normalizedSymbol = typeof symbol === 'string' ? symbol.trim() : '';
        const lookupSymbol = normalizedSymbol ? normalizedSymbol.toUpperCase() : '';
        if (lookupSymbol) {
            let positionInfo = this.dataManager?.getPosition?.(lookupSymbol);
            if (!positionInfo && this.dataManager?.loadPortfolio) {
                try {
                    await this.dataManager.loadPortfolio();
                    positionInfo = this.dataManager.getPosition?.(lookupSymbol);
                } catch (portfolioErr) {
                    console.warn('Failed to refresh portfolio for monitor modal:', portfolioErr);
                }
            }
            if (positionInfo) {
                if (!positionArgProvided && resolvedPositionQty === null) {
                    resolvedPositionQty = Number(positionInfo.position) || 0;
                }
                if (!avgArgProvided && resolvedAvgCost === null) {
                    resolvedAvgCost = Number(positionInfo.avg_cost) || 0;
                }
                if (!priceArgProvided && resolvedCurrentPrice === null) {
                    resolvedCurrentPrice = Number(positionInfo.current_price) || 0;
                }
                if (!pnlArgProvided && resolvedPnl === null && positionInfo.unrealized_pnl !== undefined) {
                    resolvedPnl = Number(positionInfo.unrealized_pnl) || 0;
                }
                if (!pnlPctArgProvided && resolvedPnlPercent === null && positionInfo.unrealized_pnl_percent !== undefined) {
                    resolvedPnlPercent = Number(positionInfo.unrealized_pnl_percent) || 0;
                }
            }
        }

        resolvedPositionQty = resolvedPositionQty ?? 0;
        resolvedCurrentPrice = resolvedCurrentPrice ?? 0;
        resolvedAvgCost = resolvedAvgCost ?? 0;
        resolvedPnl = resolvedPnl ?? 0;
        resolvedPnlPercent = resolvedPnlPercent ?? 0;

        if (normalizedSymbol) {
            symbol = normalizedSymbol;
        }

        const multiplierSymbol = lookupSymbol || (typeof symbol === 'string' ? symbol.toUpperCase() : '');
        const contractMultiplier = this.getContractMultiplier(multiplierSymbol);

        positionQty = resolvedPositionQty;
        currentPrice = resolvedCurrentPrice;
        avgCost = resolvedAvgCost;
        pnl = resolvedPnl;
        pnlPercent = resolvedPnlPercent;

        // 填充基础字段
        const symEl = document.getElementById('monitorSymbol');
        if (!symEl) return;
        symEl.textContent = symbol || '—';
        const combinedEl = document.getElementById('monitorPositionCombined');
        if (combinedEl) {
            const positionValue = (Math.abs(positionQty) || 0) * (currentPrice || 0) * (contractMultiplier || 1);
            combinedEl.textContent = `${(positionQty || 0)} ($${this.formatNumber(positionValue)})`;
        }
        const curEl = document.getElementById('monitorCurrentPrice');
        if (curEl) curEl.textContent = `$${this.formatNumber(currentPrice || 0)}`;
        const avgEl = document.getElementById('monitorAvgCost');
        if (avgEl) avgEl.textContent = `$${this.formatNumber(avgCost || 0)}`;
        const pnlEl = document.getElementById('monitorPnL');
        if (pnlEl) {
            pnlEl.textContent = `$${this.formatNumber(pnl || 0)}`;
            pnlEl.classList.remove('text-success', 'text-danger');
            if ((pnl || 0) > 1e-8) pnlEl.classList.add('text-success');
            else if ((pnl || 0) < -1e-8) pnlEl.classList.add('text-danger');
        }
        const pnlPctEl = document.getElementById('monitorPnLPercent');
        if (pnlPctEl) {
            pnlPctEl.textContent = `(${this.formatNumber(pnlPercent || 0)}%)`;
            pnlPctEl.classList.remove('text-success', 'text-danger');
            if ((pnl || 0) > 1e-8) pnlPctEl.classList.add('text-success');
            else if ((pnl || 0) < -1e-8) pnlPctEl.classList.add('text-danger');
        }

        // 计算建仓/加仓时间：取该标的最新订单时间
        let entryTime = new Date();
        let lastOrderTime = null;
        if (Array.isArray(this.orders)) {
            const latestOrderTime = this.orders
                .filter(o => o.symbol === symbol && o.submit_time)
                .map(o => this.parseMonitorChartTime(o.submit_time))
                .filter(d => !isNaN(d.getTime()))
                .sort((a, b) => b - a)[0];
            if (latestOrderTime) {
                entryTime = latestOrderTime;
            }
        }

        const previousSymbol = this.currentMonitor?.symbol;

        // 保存当前监控上下文
        const durationSelectEl = document.getElementById('monitorDuration');
        const selectedDurationMinutes = getMonitorDurationMinutes(durationSelectEl?.value);
        const configuredSpanRaw = (this.riskRules && this.riskRules[symbol] && this.riskRules[symbol].max_time_span) || null;
        const configuredSpanMinutes = getMonitorDurationMinutes(configuredSpanRaw);
        const initialSpan = Number.isFinite(configuredSpanMinutes) && configuredSpanMinutes > 0
            ? configuredSpanMinutes
            : (Number.isFinite(selectedDurationMinutes) && selectedDurationMinutes > 0
                ? selectedDurationMinutes
                : MONITOR_CHART_MAX_MINUTES);

        this.currentMonitor = {
            symbol,
            positionQty: Number(positionQty) || 0,
            currentPrice: Number(currentPrice) || 0,
            avgCost: Number(avgCost) || 0,
            pnl: Number(pnl) || 0,
            pnlPercent: Number(pnlPercent) || 0,
            contractMultiplier: Number(contractMultiplier) || 1,
            maxTimeSpan: initialSpan,
            entryTime,
            lastOrderTime, // 添加最近订单时间
            timeAlertSent: false,
        };

        if (!previousSymbol || previousSymbol !== symbol) {
            this.resetMonitorTrendData();
        }
        if (this.domManager && typeof this.domManager.handleMonitorSymbolChange === 'function') {
            await this.domManager.handleMonitorSymbolChange(symbol);
        }

        // 清理输入框
        const slInput = document.getElementById('monitorStopLoss');
        if (slInput) slInput.value = '';
        const tpInput = document.getElementById('monitorTakeProfit');
        if (tpInput) tpInput.value = '';

        // 预填充风险规则与开关状态
        try {
            let rule = (this.riskRules && this.riskRules[symbol]) ? this.riskRules[symbol] : null;
            if (!rule) {
                const riskResp = await this.apiCall('/risk_rules');
                this.riskRules = riskResp.rules || {};
                rule = this.riskRules[symbol];
            }
            const toggle = document.getElementById('monitorRiskToggle');
            if (toggle) toggle.checked = !!(rule && rule.enabled);

            const slInputRef = document.getElementById('monitorStopLoss');
            const tpInputRef = document.getElementById('monitorTakeProfit');

            const posQty = Number(positionQty) || 0;
            const avg = Number(avgCost) || 0;
            const setFromAvg = async () => {
                if (!slInputRef || !tpInputRef) return;
                if (avg > 0 && posQty !== 0) {
                    let stopLossRatio = 0.05, takeProfitRatio = 0.05, lev = null;
                    try {
                        const cfg = await this.apiCall('/risk-config');
                        stopLossRatio = cfg.stop_loss_ratio || 0.025;
        takeProfitRatio = cfg.take_profit_ratio || 0.025;
                        const leverageMap = (cfg && cfg.leverage) ? cfg.leverage : {};
                        lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || null;
                    } catch (e) { /* keep defaults */ }
                    const adjSL = lev && lev > 0 ? (stopLossRatio / lev) : stopLossRatio;
                    const adjTP = lev && lev > 0 ? (takeProfitRatio / lev) : takeProfitRatio;
                    if (posQty > 0) {
                        // 多头：SL低于成本、TP高于成本
                        slInputRef.value = this.correctPriceStep(avg * (1 - adjSL), symbol);
                        tpInputRef.value = this.correctPriceStep(avg * (1 + adjTP), symbol);
                    } else {
                        // 空头：SL高于成本、TP低于成本
                        slInputRef.value = this.correctPriceStep(avg * (1 + adjSL), symbol);
                        tpInputRef.value = this.correctPriceStep(avg * (1 - adjTP), symbol);
                    }
                } else {
                    slInputRef.value = '';
                    tpInputRef.value = '';
                }
            };

            if (rule && rule.enabled) {
                if (slInputRef) slInputRef.value = rule.stop_loss_price ?? '';
                if (tpInputRef) tpInputRef.value = rule.take_profit_price ?? '';
                if (this.currentMonitor) {
                    const requestedSpan = getMonitorDurationMinutes(rule.max_time_span);
                    const durationLimit = getMonitorDurationMinutes(durationSelectEl?.value);
                    const baseSpan = Number.isFinite(requestedSpan) && requestedSpan > 0
                        ? requestedSpan
                        : this.currentMonitor.maxTimeSpan;
                    const effectiveSpan = Number.isFinite(durationLimit) && durationLimit > 0
                        ? Math.min(baseSpan, durationLimit)
                        : baseSpan;
                    this.currentMonitor.maxTimeSpan = Math.max(1, effectiveSpan);
                }

                // 显示激活的风险规则信息
                this.showActiveRiskRule(rule);
            } else {
                await setFromAvg();
                // 隐藏激活风险规则显示
                this.hideActiveRiskRule();
            }
        
            // 预填充后立即更新差值显示
            this.updateExpectedPnL();
        } catch (prefillErr) {
            console.warn('Prefill risk rule failed:', prefillErr);
            const toggle = document.getElementById('monitorRiskToggle');
            if (toggle) toggle.checked = false;
            // 读取失败时亦按成本价设置默认值
            const slInputRef = document.getElementById('monitorStopLoss');
            const tpInputRef = document.getElementById('monitorTakeProfit');
            const posQty = Number(positionQty) || 0;
            const avg = Number(avgCost) || 0;
            if (slInputRef && tpInputRef && avg > 0 && posQty !== 0) {
                let stopLossRatio = 0.05, takeProfitRatio = 0.05, lev = null;
                try {
                    const cfg = await this.apiCall('/risk-config');
                    stopLossRatio = cfg.stop_loss_ratio || 0.025;
                    takeProfitRatio = cfg.take_profit_ratio || 0.025;
                    const leverageMap = (cfg && cfg.leverage) ? cfg.leverage : {};
                    lev = leverageMap[symbol] || leverageMap[symbol?.toUpperCase?.()] || null;
                } catch (e) { 
                    console.warn('Failed to load risk config for fallback calculation:', e);
                    /* keep defaults */ 
                }
                const adjSL = lev && lev > 0 ? (stopLossRatio / lev) : stopLossRatio;
                const adjTP = lev && lev > 0 ? (takeProfitRatio / lev) : takeProfitRatio;
                if (posQty > 0) {
                    slInputRef.value = this.correctPriceStep(avg * (1 - adjSL), symbol);
                    tpInputRef.value = this.correctPriceStep(avg * (1 + adjTP), symbol);
                } else {
                    slInputRef.value = this.correctPriceStep(avg * (1 + adjSL), symbol);
                    tpInputRef.value = this.correctPriceStep(avg * (1 - adjTP), symbol);
                }
            
                // 预填充后立即更新差值显示
                this.updateExpectedPnL();
            }
        }

        // 显示模态框
        const modalEl = document.getElementById('monitorModal');
        if (modalEl) {
            const modal = new bootstrap.Modal(modalEl);
            modal.show();

            if (this._monitorFullscreenChangeHandler) {
                document.removeEventListener('fullscreenchange', this._monitorFullscreenChangeHandler);
                this._monitorFullscreenChangeHandler = null;
            }
            this.monitorModalFullscreen = false;
            modalEl.classList.remove('monitor-fullscreen');

            // 检查monitorTrendChart初始化状态
            console.log('[Monitor Modal] Modal shown, checking monitorTrendChart status:');
            console.log('[Monitor Modal] monitorTrendChart exists:', !!this.monitorTrendChart);
            console.log('[Monitor Modal] monitorTrendData length:', this.monitorTrendData ? this.monitorTrendData.length : 'undefined');
            if (this.monitorTrendChart) {
                console.log('[Monitor Modal] monitorTrendChart data length:', this.monitorTrendChart.data ? this.monitorTrendChart.data.datasets[0].data.length : 'no data');
            } else {
                console.warn('[Monitor Modal] monitorTrendChart is not initialized!');
            }

            if (this.domManager && typeof this.domManager.updateToggleButton === 'function') {
                this.domManager.updateToggleButton();
            }
            if (this.domManager && this.domManager.active && typeof this.domManager.connectWS === 'function') {
                this.domManager.shouldReconnect = true;
                this.domManager.connectWS();
            }

            // 重置缩放按钮事件
            const resetZoomBtn = document.getElementById('resetChartZoomBtn');
            if (resetZoomBtn) {
                resetZoomBtn.onclick = () => {
                    if (this.monitorChart) {
                        this.monitorChart.resetZoom();
                    }
                    if (this.monitorTrendChart) {
                        this.monitorTrendChart.resetZoom();
                    }
                };
            }

            // 图表类型切换事件
            const chartTypeRadios = document.querySelectorAll('input[name="chartType"]');
            chartTypeRadios.forEach(radio => {
                radio.onchange = () => {
                    if (radio.checked) {
                        this.loadMonitorChart(symbol);
                    }
                };
            });

            let updateFullscreenButton = null;
            const fullscreenBtn = document.getElementById('monitorFullscreenBtn');
            if (fullscreenBtn) {
                updateFullscreenButton = (active) => {
                    fullscreenBtn.classList.toggle('monitor-fullscreen-active', active);
                    fullscreenBtn.classList.toggle('monitor-fullscreen-inactive', !active);
                    const icon = fullscreenBtn.querySelector('i');
                    if (icon) {
                        icon.classList.toggle('fa-expand', !active);
                        icon.classList.toggle('fa-compress', active);
                    }
                    fullscreenBtn.setAttribute('title', active ? '退出全屏' : '全屏');
                };
                updateFullscreenButton(false);

                const enterFullscreen = async () => {
                    modalEl.classList.add('monitor-fullscreen');
                    try {
                        if (modalEl.requestFullscreen) {
                            await modalEl.requestFullscreen();
                        } else if (modalEl.webkitRequestFullscreen) {
                            await modalEl.webkitRequestFullscreen();
                        }
                    } catch (err) {
                        console.warn('requestFullscreen failed', err);
                    }
                    this.monitorModalFullscreen = true;
                    updateFullscreenButton(true);
                };

                const exitFullscreen = async () => {
                    await this.exitMonitorFullscreen({ removeListener: false });
                    updateFullscreenButton(false);
                };

                fullscreenBtn.onclick = async () => {
                    const isFullscreen = document.fullscreenElement === modalEl
                        || document.webkitFullscreenElement === modalEl;
                    if (this.monitorModalFullscreen || modalEl.classList.contains('monitor-fullscreen') || isFullscreen) {
                        await exitFullscreen();
                    } else {
                        await enterFullscreen();
                    }
                };

                this._monitorFullscreenChangeHandler = () => {
                    const activeElement = document.fullscreenElement || document.webkitFullscreenElement;
                    if (activeElement === modalEl) {
                        this.monitorModalFullscreen = true;
                        modalEl.classList.add('monitor-fullscreen');
                        updateFullscreenButton(true);
                    } else {
                        this.monitorModalFullscreen = false;
                        modalEl.classList.remove('monitor-fullscreen');
                        updateFullscreenButton(false);
                    }
                };
                document.addEventListener('fullscreenchange', this._monitorFullscreenChangeHandler);
            }

            if (this.monitorRealtimeResumeSymbol && this.monitorRealtimeResumeSymbol !== symbol) {
                this.monitorRealtimeShouldResume = false;
                this.monitorRealtimeResumeSymbol = null;
            }
            if (this.monitorRealtimeSymbol && this.monitorRealtimeSymbol !== symbol) {
                this.unsubscribeMonitorRealtime({ keepPreference: false, reason: 'symbol-change' });
            }
            const shouldResumeRealtime = this.shouldResumeMonitorRealtime(symbol);

            const realtimeBtn = document.getElementById('monitorRealtimeBtn');
            if (realtimeBtn) {
                const setActive = (active) => {
                    realtimeBtn.classList.toggle('btn-success', active);
                    realtimeBtn.classList.toggle('btn-info', !active);
                    const icon = realtimeBtn.querySelector('i');
                    if (icon) {
                        icon.classList.toggle('fa-play', !active);
                        icon.classList.toggle('fa-pause', active);
                    }
                    realtimeBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
                };

                realtimeBtn.onclick = async () => {
                    if (this.monitorRealtimeSymbol) {
                        this.unsubscribeMonitorRealtime({ reason: 'manual' });
                        setActive(false);
                    } else {
                        const subscribed = await this.subscribeMonitorRealtime(symbol);
                        setActive(subscribed && this.monitorRealtimeSymbol === symbol);
                    }
                };

                if (shouldResumeRealtime) {
                    const subscribed = await this.subscribeMonitorRealtime(symbol, { skipReload: true, reason: 'auto-resume' });
                    setActive(subscribed && this.monitorRealtimeSymbol === symbol);
                } else {
                    setActive(this.monitorRealtimeSymbol === symbol);
                }
            }

            const slInput = document.getElementById('monitorStopLoss');
            if (slInput) {
                slInput.oninput = (e) => {
                    const correctedPrice = this.correctPriceStep(e.target.value, symbol);
                    if (correctedPrice !== parseFloat(e.target.value)) {
                        e.target.value = correctedPrice;
                    }
                    this.updateMonitorStopLossLine();
                    this.updateExpectedPnL();
                };
            }
            const tpInput = document.getElementById('monitorTakeProfit');
            if (tpInput) {
                tpInput.oninput = (e) => {
                    const correctedPrice = this.correctPriceStep(e.target.value, symbol);
                    if (correctedPrice !== parseFloat(e.target.value)) {
                        e.target.value = correctedPrice;
                    }
                    this.updateMonitorTakeProfitLine();
                    this.updateExpectedPnL();
                };
            }

            const toggle = document.getElementById('monitorRiskToggle');
            if (toggle) {
                // 切换启用/禁用
                toggle.onchange = async () => {
                    try {
                        await this.toggleRiskRule(symbol, toggle.checked);
                        if (this.riskRules && this.riskRules[symbol]) {
                            this.riskRules[symbol].enabled = toggle.checked;
                        }
                    } catch (e) {
                        console.error('toggle risk rule failed:', e);
                        // revert UI on failure
                        toggle.checked = !toggle.checked;
                    }
                };
            }

            const saveBtn = document.getElementById('saveMonitorRiskBtn');
            if (saveBtn) {
                saveBtn.onclick = async () => {
                    const sl = slInput?.value ? parseFloat(slInput.value) : null;
                    const tp = tpInput?.value ? parseFloat(tpInput.value) : null;

                    if (!sl && !tp) {
                        this.showAlert('warning', '请至少输入止损或止盈价');
                        return;
                    }

                    try {
                        await this.apiCall('/risk_rules', {
                            method: 'POST',
                            body: JSON.stringify({
                                symbol: symbol,
                                stop_loss_price: sl,
                                take_profit_price: tp,
                                max_time_span: this.currentMonitor?.maxTimeSpan
                            })
                        });
                        // 更新本地缓存
                        if (!this.riskRules) this.riskRules = {};
                        this.riskRules[symbol] = Object.assign({}, this.riskRules[symbol] || {}, {
                            symbol,
                            stop_loss_price: sl,
                            take_profit_price: tp,
                            max_time_span: this.currentMonitor?.maxTimeSpan,
                        });
                        this.showAlert('success', '已保存风险规则');
                    } catch (err) {
                        console.error('保存风险规则失败:', err);
                        this.showAlert('danger', `保存失败: ${err.message || err}`);
                    }
                };
            }

            // 平仓按钮事件
            const closePositionBtn = document.getElementById('closePositionBtn');
            if (closePositionBtn) {
                closePositionBtn.onclick = () => this.closePosition(symbol, {
                    positionQty: this.currentMonitor?.positionQty ?? 0
                });
            }
        
            // 反向下单按钮事件
            const reversePositionBtn = document.getElementById('reversePositionBtn');
            if (reversePositionBtn) {
                reversePositionBtn.onclick = async () => {
                    if (!this.currentMonitor || this.currentMonitor.positionQty === 0) {
                        this.showAlert('warning', '当前无持仓，无法反向下单');
                        return;
                    }
                
                    const confirmed = confirm(`确认反向下单 ${symbol}？\n当前持仓: ${this.currentMonitor.positionQty}\n将执行反向操作（2倍持仓量，完全反转头寸）`);
                    if (!confirmed) return;
                
                    try {
                        const response = await this.apiCall('/reverse_position', {
                            method: 'POST',
                            body: JSON.stringify({ symbol })
                        });
                    
                        if (response.success) {
                            this.showAlert('success', `反向订单已提交: ${response.action} ${response.quantity} ${symbol} (订单ID: ${response.order_id})`);
                            // 刷新持仓和订单数据
                            await this.loadPortfolio();
                            this.loadOrders();

                            // 同步弹窗右侧区域数据
                            await this.syncMonitorModalData(symbol);
                        } else {
                            this.showAlert('danger', `反向下单失败: ${response.message || '未知错误'}`);
                        }
                    } catch (err) {
                        console.error('反向下单失败:', err);
                        this.showAlert('danger', `反向下单失败: ${err.message || err}`);
                    }
                };
            }

            // 关闭时释放资源并根据用户偏好保留实时订阅状态
            modalEl.addEventListener('hidden.bs.modal', async () => {
                const keepPreference = this.shouldResumeMonitorRealtime(symbol);
                this.unsubscribeMonitorRealtime({ keepPreference, reason: 'modal-hidden' });
                if (typeof updateFullscreenButton === 'function') {
                    updateFullscreenButton(false);
                }
                await this.exitMonitorFullscreen({ removeListener: true });
            }, { once: true });

            // 订单状态过滤器事件绑定
            const orderStatusRadios = document.querySelectorAll('input[name="orderStatusFilter"]');
            orderStatusRadios.forEach(radio => {
                radio.onchange = () => {
                    if (radio.checked) {
                        this.loadMonitorOrdersList(symbol, radio.value);
                    }
                };
            });
        
            const chartLoadOptions = shouldResumeRealtime ? { useCache: false, forceRefresh: true } : undefined;

            // 初次载入图表
            this.loadMonitorChart(symbol, chartLoadOptions);

            const intervalSelectEl = document.getElementById('monitorInterval');
            const durationSelectEl = document.getElementById('monitorDuration');

            refreshMonitorDurationSelect(intervalSelectEl?.value, {
                preferredValue: durationSelectEl?.value
            });

            const bindMonitorControl = (element) => {
                if (!element || element.dataset.monitorBound === '1') return;
                element.addEventListener('change', () => {
                    if (element.id === 'monitorInterval') {
                        refreshMonitorDurationSelect(element.value, {
                            preferredValue: null,
                            triggerChange: false
                        });
                    }
                    const activeSymbol = (this.currentMonitor?.symbol || symbol || '').trim();
                    if (!activeSymbol) return;
                    const barSize = document.getElementById('monitorInterval')?.value || '1 min';
                    const durationValue = document.getElementById('monitorDuration')?.value || '30 M';
                    if (this.chartDataCache) {
                        const cacheKey = `${activeSymbol}_${barSize}_${durationValue}`;
                        delete this.chartDataCache[cacheKey];
                    }
                    this.loadMonitorChart(activeSymbol, { useCache: false, forceRefresh: true });
                });
                element.dataset.monitorBound = '1';
            };

            bindMonitorControl(intervalSelectEl);
            bindMonitorControl(durationSelectEl);

            // 初始化预期PnL显示
            this.updateExpectedPnL();
        
            // 初次加载订单列表
            this.loadMonitorOrdersList(symbol, 'working');
        }
    } catch (e) {
        console.error('openMonitorModal failed:', e);
        this.showAlert('danger', `打开监控失败: ${e.message || e}`);
    }
    }

    async function legacySyncMonitorModalData(symbol) {
    try {
        // 从缓存的持仓数据中查找
        const positions = this.dataManager.getAllPositions();
        const position = positions.find(p => p.symbol === symbol);
    
        // 更新持仓信息（无论是否有持仓）
        const positionQtyEl = document.getElementById('monitorPositionQty');
        const avgCostEl = document.getElementById('monitorAvgCost');
        const currentPriceEl = document.getElementById('monitorCurrentPrice');
        const pnlEl = document.getElementById('monitorPnL');
    
        if (position) {
            // 有持仓时更新持仓信息
            if (positionQtyEl) positionQtyEl.textContent = position.position || '0';
            if (avgCostEl) avgCostEl.textContent = `$${parseFloat(position.avg_cost || 0).toFixed(2)}`;
            if (currentPriceEl) currentPriceEl.textContent = `$${parseFloat(position.market_price || 0).toFixed(2)}`;
        
            // 更新PnL
            const pnl = parseFloat(position.unrealized_pnl || 0);
            if (pnlEl) {
                pnlEl.textContent = `$${pnl.toFixed(2)}`;
                pnlEl.className = pnl >= 0 ? 'fw-bold text-success' : 'fw-bold text-danger';
            }
        
            // 更新监控上下文（保持现有对象引用，只更新属性）
            if (!this.currentMonitor) {
                this.currentMonitor = {};
            }
            this.currentMonitor.symbol = symbol;
            this.currentMonitor.positionQty = parseFloat(position.position || 0);
            this.currentMonitor.avgCost = parseFloat(position.avg_cost || 0);
            this.currentMonitor.currentPrice = parseFloat(position.market_price || 0);
        
            // 保留现有的止损止盈设置，不清空
            // const riskRuleSwitch = document.getElementById('monitorRiskRuleSwitch');
            // const stopLossInput = document.getElementById('monitorStopLoss');
            // const takeProfitInput = document.getElementById('monitorTakeProfit');
        
            // if (riskRuleSwitch) {
            //     riskRuleSwitch.checked = false;
            //     riskRuleSwitch.dispatchEvent(new Event('change'));
            // }
        
            // if (stopLossInput) stopLossInput.value = '';
            // if (takeProfitInput) takeProfitInput.value = '';
        
            // 重新计算推荐的SL/TP价格
            await this.updateRiskPrices();
        
            // 更新预期PnL显示
            this.updateExpectedPnL();
        } else {
            // 平仓后没有持仓时，清零显示
            if (positionQtyEl) positionQtyEl.textContent = '0';
            if (avgCostEl) avgCostEl.textContent = '$0.00';
            if (pnlEl) {
                pnlEl.textContent = '$0.00';
                pnlEl.className = 'fw-bold text-muted';
            }
        
            // 更新监控上下文
            if (!this.currentMonitor) {
                this.currentMonitor = {};
            }
            this.currentMonitor.symbol = symbol;
            this.currentMonitor.positionQty = 0;
            this.currentMonitor.avgCost = 0;
        
            // 保留现有的止损止盈设置，不清空
            // const riskRuleSwitch = document.getElementById('monitorRiskRuleSwitch');
            // const stopLossInput = document.getElementById('monitorStopLoss');
            // const takeProfitInput = document.getElementById('monitorTakeProfit');
        
            // if (riskRuleSwitch) {
            //     riskRuleSwitch.checked = false;
            //     riskRuleSwitch.dispatchEvent(new Event('change'));
            // }
        
            // if (stopLossInput) stopLossInput.value = '';
            // if (takeProfitInput) takeProfitInput.value = '';
        
            // 隐藏激活的风险规则显示
            this.hideActiveRiskRule();
        }
    
        // 刷新订单列表
        const checkedRadio = document.querySelector('input[name="orderStatusFilter"]:checked');
        const currentStatus = checkedRadio ? checkedRadio.value : 'working';
        await this.loadMonitorOrdersList(symbol, currentStatus);
    
    } catch (error) {
        console.error('同步监控弹窗数据失败:', error);
    }
    }

    async function legacyLoadMonitorChart(symbol, options = {}

    function legacyDisplayChart(symbol, data) {
     // Show chart modal
      const modal = new bootstrap.Modal(document.getElementById('chartModal'));
      document.getElementById('chartSymbol').textContent = `${symbol} Price Chart`;
     
     const chartContainer = document.getElementById('chartContainer');
     if (!chartContainer) return;
     
     // Clear previous chart
     chartContainer.innerHTML = `<canvas id="priceChart" width="900" height="450"></canvas>`; // 增加canvas大小以解决数据点显示不完整问题
     
     modal.show();
     
     // Wait for modal to be shown before creating chart
     setTimeout(() => {
         const ctx = document.getElementById('priceChart').getContext('2d');
     
         // Prepare data for Chart.js
         const labels = data.map(bar => new Date(bar.date).toLocaleDateString());
         const prices = data.map(bar => bar.close);
     
         new Chart(ctx, {
             type: 'line',
             data: {
                 labels: labels,
                 datasets: [{
                     label: `${symbol} Price`,
                     data: prices,
                     borderColor: 'rgb(75, 192, 192)',
                     backgroundColor: 'rgba(75, 192, 192, 0.1)',
                     tension: 0.1
                 }]
             },
             options: {
                 responsive: true,
                 plugins: {
                     title: {
                         display: true,
                         text: `${symbol} Price Chart`
                     }
                 },
                 scales: {
                     y: {
                         beginAtZero: false
                     }
                 }
             }
         });
     }, 300);
     }

    function legacyUpdateMonitorRealtime(data) {
    try {
        if (!this.currentMonitor || !this.currentMonitor.symbol) return;
        const symbol = this.currentMonitor.symbol;

        // 兼容服务器返回结构，确保处理数组形式的数据
        let payload = (data && data.data) ? data.data : data;
        if (Array.isArray(payload)) payload = payload[0] || {};
        if (!payload || (payload.symbol && payload.symbol !== symbol)) return;

        let price = null;
        const priceCandidates = [payload.last, payload.price, payload.close, payload.mark, payload.current];
        for (const candidate of priceCandidates) {
            const numeric = Number(candidate);
            if (Number.isFinite(numeric)) {
                price = numeric;
                break;
            }
        }
        if (!Number.isFinite(price)) return;

        const volume = Number(payload.volume ?? payload.vol ?? payload.lastVolume ?? 0) || 0;

        const qty = Number(this.currentMonitor.positionQty || 0);
        const avgCostRaw = Number(this.currentMonitor.avgCost || 0);
        const avgCostForCalc = qty >= 0 ? avgCostRaw : Math.abs(avgCostRaw);
        const storedMultiplier = Number(this.currentMonitor.contractMultiplier || 0);
        const multiplierSymbol = (this.currentMonitor.symbol || symbol || '').toString().toUpperCase();
        const contractMultiplier = (Number.isFinite(storedMultiplier) && storedMultiplier > 0)
            ? storedMultiplier
            : this.getContractMultiplier(multiplierSymbol);
        this.currentMonitor.contractMultiplier = contractMultiplier;

        // 更新当前价格到监控上下文
        this.currentMonitor.currentPrice = price;

        const curEl = document.getElementById('monitorCurrentPrice');
        if (curEl) curEl.textContent = `$${this.formatNumber(price)}`;

        const combinedEl = document.getElementById('monitorPositionCombined');
        if (combinedEl) {
            const positionValue = Math.abs(qty) * price * (contractMultiplier || 1);
            combinedEl.textContent = `${qty} ($${this.formatNumber(positionValue)})`;
        }

        // 计算未实现盈亏，区分多头和空头持仓
        let pnl = 0;
        if (qty > 0) {  // 多头
            // 多头盈亏 = (当前价格 - 成本价) * 持仓数量
            pnl = (price - avgCostForCalc) * qty * (contractMultiplier || 1);
        } else if (qty < 0) {  // 空头
            // 空头盈亏 = (成本价 - 当前价格) * 持仓数量的绝对值
            pnl = (avgCostForCalc - price) * Math.abs(qty) * (contractMultiplier || 1);
        }

        const pnlEl = document.getElementById('monitorPnL');
        if (pnlEl) {
            pnlEl.textContent = `$${this.formatNumber(pnl)}`;
            pnlEl.classList.toggle('text-success', pnl >= 0);
            pnlEl.classList.toggle('text-danger', pnl < 0);
        }

        const denom = Math.max(
            1e-9,
            Math.abs(avgCostForCalc)
                * Math.max(1, Math.abs(qty))
                * Math.max(Math.abs(contractMultiplier) || 0, 1e-9)
        );
        const pnlPct = (pnl / denom) * 100;
        const pnlPctEl = document.getElementById('monitorPnLPercent');
        if (pnlPctEl) {
            pnlPctEl.textContent = `(${this.formatNumber(pnlPct)}%)`;
            pnlPctEl.classList.toggle('text-success', pnl >= 0);
            pnlPctEl.classList.toggle('text-danger', pnl < 0);
        }

        this.currentMonitor.pnl = pnl;
        this.currentMonitor.pnlPercent = pnlPct;

        // Update asset change (daily change) in real-time
        if (this.currentMonitor.todayOpenPrice && price) {
            const assetChange = price - this.currentMonitor.todayOpenPrice;
            const assetChangePercent = (assetChange / this.currentMonitor.todayOpenPrice) * 100;
        
            const assetChangeEl = document.getElementById('monitorAssetChange');
            const assetChangePercentEl = document.getElementById('monitorAssetChangePercent');
        
            if (assetChangeEl) {
                const changeText = assetChange >= 0 ? `+$${assetChange.toFixed(2)}` : `-$${Math.abs(assetChange).toFixed(2)}`;
                assetChangeEl.textContent = changeText;
                assetChangeEl.className = assetChange >= 0 ? 'text-success' : 'text-danger';
            }
        
            if (assetChangePercentEl) {
                const percentText = assetChangePercent >= 0 ? `+${assetChangePercent.toFixed(2)}%` : `${assetChangePercent.toFixed(2)}%`;
                assetChangePercentEl.textContent = percentText;
                assetChangePercentEl.className = assetChangePercent >= 0 ? 'text-success' : 'text-danger';
            }
        }

        if (this.monitorChart && this.monitorChart.data && this.monitorChart.data.datasets && this.monitorChart.data.datasets.length > 0) {
            const dataset = this.monitorChart.data.datasets[0];
            const chartType = this.monitorChart.config.type || '';
            let candlestickData = null;

            const intervalSelect = document.getElementById('monitorInterval');
            const intervalRaw = intervalSelect ? intervalSelect.value : '1 min';
            const interval = normalizeIntervalKey(intervalRaw) || '1min';
            const durationSelect = document.getElementById('monitorDuration');
            const duration = durationSelect?.value || '30 M';

            const rawTimestamp = payload.timestamp ?? payload.lastTimestamp ?? payload.lastTime ?? payload.time ?? payload.last_time ?? payload.updateTime ?? payload.datetime ?? null;
            let baseTime = this.parseMonitorChartTime(rawTimestamp, { skipDstAdjustment: true });
            if (!(baseTime instanceof Date) || Number.isNaN(baseTime.getTime())) {
                baseTime = new Date();
            }

            const alignToInterval = (date) => {
                const aligned = new Date(date.getTime());
                const intervalKey = typeof interval === 'string' ? interval : '';
                switch (intervalKey) {
                    case '1min':
                    case '1mins':
                        aligned.setSeconds(0, 0);
                        break;
                    case '5min':
                    case '5mins': {
                        const minutes5 = Math.floor(aligned.getMinutes() / 5) * 5;
                        aligned.setMinutes(minutes5, 0, 0);
                        break;
                    }
                    case '15min':
                    case '15mins': {
                        const minutes15 = Math.floor(aligned.getMinutes() / 15) * 15;
                        aligned.setMinutes(minutes15, 0, 0);
                        break;
                    }
                    case '30min':
                    case '30mins': {
                        const minutes30 = Math.floor(aligned.getMinutes() / 30) * 30;
                        aligned.setMinutes(minutes30, 0, 0);
                        break;
                    }
                    case '1hour':
                    case '1hours':
                    case '60min':
                    case '60mins':
                        aligned.setMinutes(0, 0, 0);
                        break;
                    case '1day':
                    case '1days':
                        aligned.setHours(0, 0, 0, 0);
                        break;
                    default:
                        break;
                }
                return aligned;
            };

            let intervalTime = alignToInterval(baseTime);
            let timeValue = this.normalizeMonitorTime(intervalTime);

            let timeLabel;
            if (intervalRaw.includes('day') || duration.includes('M') || duration.includes('Y')) {
                timeLabel = intervalTime.toLocaleDateString('zh-CN', {
                    month: 'short',
                    day: 'numeric'
                });
            } else if (intervalRaw.includes('hour') || duration === '1 W') {
                timeLabel = intervalTime.toLocaleString('zh-CN', {
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    hour12: false
                });
            } else {
                timeLabel = intervalTime.toLocaleTimeString('zh-CN', {
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: false
                });
            }

            if (chartType === 'candlestick') {
                const sourceOhlc = Array.isArray(dataset.data) ? dataset.data : [];
                const ohlcData = sourceOhlc.map(bar => ({ ...bar }));
                candlestickData = ohlcData;
                const volumeDataset = this.monitorChart.data.datasets?.[1];
                const sourceVolumes = Array.isArray(volumeDataset?.data) ? volumeDataset.data : [];
                const vData = sourceVolumes.map((entry) => {
                    if (typeof entry === 'object' && entry !== null) {
                        return { ...entry };
                    }
                    const numericVolume = Number(entry);
                    return {
                        x: timeValue,
                        y: Number.isFinite(numericVolume) ? numericVolume : 0
                    };
                });
                const labelArray = Array.isArray(this.monitorChart.data.labels)
                    ? [...this.monitorChart.data.labels]
                    : [];
                let estimatedStep = null;
                if (ohlcData.length > 0) {
                    const lastBar = ohlcData[ohlcData.length - 1];
                    const prevBar = ohlcData.length > 1 ? ohlcData[ohlcData.length - 2] : null;
                    if (prevBar && Number.isFinite(lastBar?.x) && Number.isFinite(prevBar.x)) {
                        estimatedStep = Math.max(1, Math.abs(lastBar.x - prevBar.x));
                    }
                    if (Number.isFinite(lastBar?.x)) {
                        const diff = timeValue - lastBar.x;
                        if (estimatedStep) {
                            if (Math.abs(diff) <= estimatedStep * 0.75) {
                                timeValue = lastBar.x;
                            } else if (diff > estimatedStep * 0.75) {
                                const multiples = Math.max(1, Math.round(diff / estimatedStep));
                                timeValue = lastBar.x + multiples * estimatedStep;
                            } else if (diff < -estimatedStep * 0.75) {
                                timeValue = lastBar.x;
                            }
                        } else if (timeValue <= lastBar.x) {
                            timeValue = lastBar.x;
                        }
                        intervalTime = new Date(timeValue);
                    }
                }

                const tolerance = (estimatedStep || 60000) * 0.75;
                if (ohlcData.length > 0) {
                    const lastBar = ohlcData[ohlcData.length - 1];
                    if (Number.isFinite(lastBar?.x) && Math.abs(lastBar.x - timeValue) <= tolerance) {
                        const updatedBar = {
                            ...lastBar,
                            x: Number.isFinite(lastBar.x) ? lastBar.x : timeValue,
                            c: price,
                            h: Math.max(Number(lastBar.h ?? price), price),
                            l: Math.min(Number(lastBar.l ?? price), price),
                            volume
                        };
                        ohlcData[ohlcData.length - 1] = updatedBar;

                        if (labelArray.length) {
                            labelArray[labelArray.length - 1] = timeLabel;
                        } else {
                            labelArray.push(timeLabel);
                        }

                        if (vData.length > 0) {
                            const lastVolume = vData[vData.length - 1];
                            if (Number.isFinite(lastVolume?.x) && Math.abs(lastVolume.x - timeValue) <= tolerance) {
                                vData[vData.length - 1] = {
                                    ...lastVolume,
                                    x: Number.isFinite(lastVolume.x) ? lastVolume.x : timeValue,
                                    y: volume
                                };
                            } else {
                                vData.push({ x: timeValue, y: volume });
                            }
                        } else {
                            vData.push({ x: timeValue, y: volume });
                        }
                    } else {
                        ohlcData.push({ x: timeValue, o: price, h: price, l: price, c: price, volume });
                        vData.push({ x: timeValue, y: volume });
                        labelArray.push(timeLabel);
                    }
                } else {
                    ohlcData.push({ x: timeValue, o: price, h: price, l: price, c: price, volume });
                    vData.push({ x: timeValue, y: volume });
                    labelArray.push(timeLabel);
                }

                const MAX_POINTS = 300;
                if (ohlcData.length > MAX_POINTS) {
                    ohlcData.shift();
                    vData.shift();
                    if (labelArray.length > MAX_POINTS) {
                        while (labelArray.length > MAX_POINTS) {
                            labelArray.shift();
                        }
                    } else if (labelArray.length > 0) {
                        labelArray.shift();
                    }
                }

                const sanitizedOhlc = ohlcData.map((bar) => {
                    const normalizedX = this.normalizeMonitorTime(bar?.x ?? bar?.t ?? bar?.time ?? timeValue);
                    const openCandidate = Number(bar?.o ?? bar?.open ?? bar?.c ?? bar?.close ?? price);
                    const closeCandidate = Number(bar?.c ?? bar?.close ?? bar?.o ?? bar?.open ?? price);
                    const highCandidate = Number(bar?.h ?? bar?.high ?? closeCandidate ?? openCandidate ?? price);
                    const lowCandidate = Number(bar?.l ?? bar?.low ?? closeCandidate ?? openCandidate ?? price);
                    const volumeCandidate = Number(bar?.volume ?? bar?.v ?? 0);

                    const resolvedX = Number.isFinite(normalizedX) ? normalizedX : timeValue;
                    const resolvedClose = Number.isFinite(closeCandidate) ? closeCandidate : price;
                    const resolvedOpen = Number.isFinite(openCandidate) ? openCandidate : resolvedClose;
                    const resolvedHighBase = Number.isFinite(highCandidate) ? highCandidate : resolvedClose;
                    const resolvedLowBase = Number.isFinite(lowCandidate) ? lowCandidate : resolvedClose;
                    const resolvedVolume = Number.isFinite(volumeCandidate) ? volumeCandidate : 0;

                    const resolvedHigh = Math.max(resolvedHighBase, resolvedOpen, resolvedClose);
                    const resolvedLow = Math.min(resolvedLowBase, resolvedOpen, resolvedClose);

                    return {
                        x: resolvedX,
                        t: resolvedX,
                        o: resolvedOpen,
                        h: resolvedHigh,
                        l: resolvedLow,
                        c: resolvedClose,
                        volume: resolvedVolume,
                        v: resolvedVolume
                    };
                });
                dataset.data = sanitizedOhlc;
                if (this.monitorChart.config?.data?.datasets?.[0]) {
                    this.monitorChart.config.data.datasets[0].data = sanitizedOhlc;
                }

                const sanitizedVolumes = vData.map((entry) => {
                    const rawX = entry?.x ?? entry?.t ?? entry?.time ?? entry;
                    const normalizedX = this.normalizeMonitorTime(rawX ?? timeValue);
                    const volumeCandidate = Number(entry?.y ?? entry?.volume ?? entry?.v ?? entry ?? 0);
                    const resolvedX = Number.isFinite(normalizedX) ? normalizedX : timeValue;
                    const resolvedVolume = Number.isFinite(volumeCandidate) ? volumeCandidate : 0;
                    return {
                        x: resolvedX,
                        t: resolvedX,
                        y: resolvedVolume,
                        volume: resolvedVolume
                    };
                });
                if (volumeDataset) {
                    volumeDataset.data = sanitizedVolumes;
                    if (this.monitorChart.config?.data?.datasets?.[1]) {
                        this.monitorChart.config.data.datasets[1].data = sanitizedVolumes;
                    }
                }
                candlestickData = dataset.data;
                this.monitorChart.data.labels = labelArray.slice();
                if (this.monitorChart.config?.data) {
                    this.monitorChart.config.data.labels = labelArray.slice();
                }
                if (this.currentMonitor) {
                    this.currentMonitor.displayTimes = sanitizedOhlc.map(item => new Date(item.x));
                    this.currentMonitor.displayLabels = labelArray.slice();
                }

                const xOptions = this.monitorChart.options?.scales?.x;
                if (xOptions) {
                    const firstBar = dataset.data[0];
                    const latestBar = dataset.data[dataset.data.length - 1];
                    const previousBar = dataset.data[dataset.data.length - 2];
                    const step = previousBar && latestBar
                        ? Math.max(1, Math.abs(latestBar.x - previousBar.x))
                        : 60000;
                    if (firstBar && Number.isFinite(firstBar.x) && !Number.isFinite(xOptions.min)) {
                        xOptions.min = firstBar.x;
                    }
                    if (latestBar && Number.isFinite(latestBar.x)) {
                        xOptions.max = latestBar.x + step * 0.1;
                    }
                }
            } else {
                const baseLabels = Array.isArray(this.monitorChart.data.labels)
                    ? [...this.monitorChart.data.labels]
                    : [];
                const volumeDataset = this.monitorChart.data.datasets[1];
                const priceData = Array.isArray(dataset.data) ? [...dataset.data] : [];
                const volumeData = Array.isArray(volumeDataset.data) ? [...volumeDataset.data] : [];
                let displayTimes = Array.isArray(this.currentMonitor?.displayTimes)
                    ? this.currentMonitor.displayTimes.map(value => {
                        if (value instanceof Date) return new Date(value.getTime());
                        const parsed = this.parseMonitorChartTime(value);
                        return parsed instanceof Date ? parsed : new Date(value);
                    })
                    : [];

                const trimTrailingPlaceholders = () => {
                    let trimmed = false;
                    while (priceData.length > 0) {
                        const lastIdx = priceData.length - 1;
                        const lastPrice = priceData[lastIdx];
                        if (lastPrice == null || Number.isNaN(lastPrice)) {
                            priceData.pop();
                            if (volumeData.length > lastIdx) {
                                volumeData.splice(lastIdx, 1);
                            } else if (volumeData.length) {
                                volumeData.pop();
                            }
                            if (baseLabels.length > lastIdx) {
                                baseLabels.splice(lastIdx, 1);
                            } else if (baseLabels.length) {
                                baseLabels.pop();
                            }
                            if (displayTimes.length > lastIdx) {
                                displayTimes.splice(lastIdx, 1);
                            } else if (displayTimes.length) {
                                displayTimes.pop();
                            }
                            trimmed = true;
                        } else {
                            break;
                        }
                    }
                    return trimmed;
                };

                if (trimTrailingPlaceholders()) {
                    console.log('[实时更新] 移除尾部占位空值，准备连接实时数据点');
                }

                const intervalMinutes = INTERVAL_MINUTES_MAP[interval] || 1;
                const fallbackStep = Math.max(60000, intervalMinutes * 60 * 1000);
                let estimatedStep = fallbackStep;
                if (displayTimes.length > 1) {
                    const lastTime = this.normalizeMonitorTime(displayTimes[displayTimes.length - 1]);
                    const prevTime = this.normalizeMonitorTime(displayTimes[displayTimes.length - 2]);
                    if (Number.isFinite(lastTime) && Number.isFinite(prevTime) && Math.abs(lastTime - prevTime) > 0) {
                        estimatedStep = Math.max(1, Math.abs(lastTime - prevTime));
                    }
                }

                const tolerance = estimatedStep * 0.75;
                const lastStoredTime = displayTimes.length
                    ? this.normalizeMonitorTime(displayTimes[displayTimes.length - 1])
                    : null;
                let updatedExisting = false;

                console.log(`[实时更新] interval="${intervalRaw}" duration="${duration}" 时间值=${intervalTime.toISOString()}`);
                console.log(`[实时更新] 最新数据 - 价格: ${price}, 成交量: ${volume}`);

                if (lastStoredTime != null && Math.abs(timeValue - lastStoredTime) <= tolerance) {
                    if (priceData.length) {
                        priceData[priceData.length - 1] = price;
                    } else {
                        priceData.push(price);
                    }
                    if (volumeData.length) {
                        volumeData[volumeData.length - 1] = volume;
                    } else {
                        volumeData.push(volume);
                    }
                    if (baseLabels.length) {
                        baseLabels[baseLabels.length - 1] = timeLabel;
                    } else {
                        baseLabels.push(timeLabel);
                    }
                    displayTimes[displayTimes.length - 1] = new Date(timeValue);
                    updatedExisting = true;
                    console.log(`[实时更新] 更新最后一个数据点: ${timeLabel}, 价格: ${price}`);
                } else {
                    baseLabels.push(timeLabel);
                    priceData.push(price);
                    volumeData.push(volume);
                    displayTimes.push(new Date(timeValue));
                    console.log(`[实时更新] 添加新数据点: ${timeLabel}, 价格: ${price}`);
                }

                const MAX_POINTS = 300;
                while (priceData.length > MAX_POINTS) {
                    priceData.shift();
                    if (volumeData.length) volumeData.shift();
                    if (baseLabels.length) baseLabels.shift();
                    if (displayTimes.length) displayTimes.shift();
                }

                this.monitorChart.data.labels = baseLabels.slice();
                dataset.data = priceData.slice();
                volumeDataset.data = volumeData.slice();

                if (this.currentMonitor) {
                    this.currentMonitor.displayTimes = displayTimes.map(value =>
                        value instanceof Date ? new Date(value.getTime()) : new Date(value)
                    );
                    this.currentMonitor.displayLabels = baseLabels.slice();
                }

                const xScale = this.monitorChart.options?.scales?.x;
                if (xScale && baseLabels.length) {
                    const firstLabel = baseLabels[0];
                    const lastLabel = baseLabels[baseLabels.length - 1];
                    if (typeof xScale.min === 'string' || xScale.min == null) {
                        xScale.min = firstLabel;
                    }
                    if (typeof xScale.max === 'string' || xScale.max == null || !updatedExisting) {
                        xScale.max = lastLabel;
                    }
                }
            }
            if (chartType === 'candlestick' && Array.isArray(candlestickData)) {
                const extremes = candlestickData.reduce((acc, bar) => {
                    if (!bar) return acc;
                    const high = Number(bar.h);
                    const low = Number(bar.l);
                    if (Number.isFinite(high)) acc.max = Math.max(acc.max, high);
                    if (Number.isFinite(low)) acc.min = Math.min(acc.min, low);
                    return acc;
                }, { min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY });
                if (Number.isFinite(extremes.min) && Number.isFinite(extremes.max)) {
                    const span = extremes.max - extremes.min;
                    const padding = span > 0 ? span * 0.05 : (extremes.max || 0) * 0.002 || 0.5;
                    const yScale = this.monitorChart.options?.scales?.y;
                    if (yScale) {
                        yScale.min = Math.max(0, extremes.min - padding);
                        yScale.max = extremes.max + padding;
                    }
                }
            }

            const annotations = this.ensureMonitorAnnotations();
            if (annotations) {
                const currentPriceLabel = `$${this.formatNumber(price)}`;
                if (annotations.currentPrice) {
                    annotations.currentPrice.yMin = price;
                    annotations.currentPrice.yMax = price;
                    annotations.currentPrice.borderColor = annotations.currentPrice.borderColor || 'rgb(255, 206, 84)';
                    annotations.currentPrice.borderWidth = annotations.currentPrice.borderWidth || 2;
                    if (annotations.currentPrice.label) {
                        annotations.currentPrice.label.content = currentPriceLabel;
                        annotations.currentPrice.label.backgroundColor = 'rgba(255, 206, 84, 0.8)';
                        annotations.currentPrice.label.color = 'black';
                        annotations.currentPrice.label.position = 'end';
                        annotations.currentPrice.label.enabled = true;
                        annotations.currentPrice.label.padding = 4;
                    } else {
                        annotations.currentPrice.label = {
                            content: currentPriceLabel,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(255, 206, 84, 0.8)',
                            color: 'black',
                            padding: 4
                        };
                    }
                } else {
                    annotations.currentPrice = {
                        type: 'line',
                        yMin: price,
                        yMax: price,
                        borderColor: 'rgb(255, 206, 84)',
                        borderWidth: 2,
                        label: {
                            content: currentPriceLabel,
                            enabled: true,
                            position: 'end',
                            backgroundColor: 'rgba(255, 206, 84, 0.8)',
                            color: 'black',
                            padding: 4
                        }
                    };
                }
            }
            // 调整成交量轴的最大值，使柱状图高度不超过整体三分之一
            const volDataset = this.monitorChart.data.datasets?.[1];
            const volumes = Array.isArray(volDataset?.data)
                ? volDataset.data
                    .map(v => (typeof v === 'object' && v !== null) ? v.y : v)
                    .filter(v => typeof v === 'number' && !Number.isNaN(v))
                : [];
            const maxVol = volumes.length ? Math.max(...volumes) : 0;
            if (this.monitorChart.options?.scales?.y1) {
                this.monitorChart.options.scales.y1.max = (maxVol * 3) || 1;
            }

            const volumeSample = Array.isArray(volDataset?.data) ? volDataset.data : [];
            console.log(`[图表更新] 准备更新图表，数据点数量: ${dataset.data.length}, 成交量数据点: ${volumeSample.length}`);
            console.log(`[图表更新] 价格数据: ${JSON.stringify(dataset.data.slice(-3))}`);
            console.log(`[图表更新] 成交量数据: ${JSON.stringify(volumeSample.slice(-3))}`);
            console.log(`[图表更新] 标签数据: ${JSON.stringify(this.monitorChart.data.labels.slice(-3))}`);

            this.monitorChart.update('none');
            console.log(`[图表更新] 图表更新完成`);

            const updatedRange = this.extractRangeFromMonitorChart();
            if (updatedRange) {
                if (!this.currentMonitor) this.currentMonitor = {};
                this.currentMonitor.chartTimeRange = updatedRange;
            }
        }

        // 更新预期PnL显示
        this.updateExpectedPnL();

        // 检查时间跨度达到右边界提醒
        try {
            // 使用实际的时间比较，而不是依赖图表标签数量
            if (this.currentMonitor && this.currentMonitor.entryTime && !this.currentMonitor.timeAlertSent) {
                const entryTime = new Date(this.currentMonitor.entryTime);
                const currentTime = new Date();
                const elapsedMinutes = (currentTime - entryTime) / (60 * 1000);
                const maxTimeSpan = this.currentMonitor.maxTimeSpan || 30;
            
                if (elapsedMinutes >= maxTimeSpan) {
                    const symbol = this.currentMonitor.symbol;
                    const msg = `${symbol} 持仓时间已达预设上限 (${Math.floor(elapsedMinutes)}分钟)`;
                    this.showAlert('warning', msg);
                    // 发送通知，即使失败也不影响标记已发送
                    this.apiCall('/notify', { method: 'POST', body: JSON.stringify({ message: msg, event_type: 'risk_rule' }) })
                        .then(result => {
                            if (!result.success) {
                                console.warn('Position expiry notification failed:', result.message || 'Unknown reason');
                            }
                        })
                        .catch(err => console.warn('Position expiry notification error:', err));
                    this.currentMonitor.timeAlertSent = true;
                }
            }
        } catch (e) { console.warn('检查持仓时间失败:', e); }
    } catch (e) {
        console.warn('updateMonitorRealtime failed:', e);
    }
    }class MonitoringManager {
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
            this.monitorChart = null;
            this.monitorTrendData = [];
            this.monitorTrendDatasetMap = { trend: 0, events: 1, price: 2 };
            this.monitorDomHistoryList = null;
            this.monitorDomHistoryMeta = null;
            this.currentMonitor = null;
            this.chartDataCache = {};
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
            this.pendingMonitorCandlestickUpgradeToken = null;
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
                'monitorChart',
                'monitorTrendData',
                'monitorTrendDatasetMap',
                'monitorDomHistoryList',
                'monitorDomHistoryMeta',
                'currentMonitor',
                'chartDataCache',
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
                'pendingMonitorCandlestickUpgradeToken',
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
            try {
                if (value == null) return null;
                if (value instanceof Date) return value.getTime();
                if (typeof value === 'number') {
                    return Number.isFinite(value) ? value : null;
                }
                if (typeof value === 'string' && !value.trim()) {
                    return null;
                }
                if (this.app && typeof this.app.parseMonitorChartTime === 'function') {
                    const parsed = this.app.parseMonitorChartTime(value);
                    if (parsed && !Number.isNaN(parsed.getTime())) {
                        return parsed.getTime();
                    }
                }
                const fallback = new Date(value);
                return Number.isNaN(fallback.getTime()) ? null : fallback.getTime();
            } catch (e) {
                return null;
            }
        }

        getMonitorDisplayTimes() {
            const times = this.currentMonitor?.displayTimes;
            return Array.isArray(times) ? times : [];
        }

        getMonitorDisplayLabels() {
            const labels = this.currentMonitor?.displayLabels;
            return Array.isArray(labels) ? labels : [];
        }

        extractRangeFromMonitorChart() {
            if (!this.monitorChart || !this.monitorChart.scales?.x) return null;
            const chartType = this.currentMonitor?.chartType || this.monitorChart.config?.type;
            const times = this.getMonitorDisplayTimes();
            if (!times.length) return null;

            if (chartType === 'candlestick') {
                const min = this.normalizeMonitorTime(this.monitorChart.scales.x.min);
                const max = this.normalizeMonitorTime(this.monitorChart.scales.x.max);
                if (min == null || max == null) return null;
                return { min, max };
            }

            const labels = this.getMonitorDisplayLabels();
            if (!labels.length || labels.length !== times.length) return null;

            const toIndex = (value, fallback) => {
                if (typeof value === 'number' && Number.isFinite(value)) {
                    return Math.round(value);
                }
                if (typeof value === 'string') {
                    const idx = labels.indexOf(value);
                    if (idx !== -1) return idx;
                }
                return fallback;
            };

            const minIdx = Math.max(0, Math.min(times.length - 1, toIndex(this.monitorChart.scales.x.min, 0)));
            const maxIdx = Math.max(minIdx, Math.min(times.length - 1, toIndex(this.monitorChart.scales.x.max, times.length - 1)));

            const minTime = this.normalizeMonitorTime(times[minIdx]);
            const maxTime = this.normalizeMonitorTime(times[maxIdx]);
            if (minTime == null || maxTime == null) return null;
            return { min: minTime, max: maxTime };
        }

        syncCharts(source, target) {
            if (!source || !target) return;
            if (this._syncingCharts) return;

            this._syncingCharts = true;

            try {
                const isMonitorTrendPair =
                    (source === this.monitorChart && target === this.monitorTrendChart)
                    || (source === this.monitorTrendChart && target === this.monitorChart);

                if (!isMonitorTrendPair) {
                    const srcScale = source.scales?.x;
                    const targetScale = target.options?.scales?.x;
                    if (srcScale && targetScale) {
                        targetScale.min = srcScale.min;
                        targetScale.max = srcScale.max;
                    }
                    target.update('none');
                }
                if (source === this.monitorChart || target === this.monitorChart) {
                    this.app?.updateDomSignalAnnotations?.();
                }
            } finally {
                this._syncingCharts = false;
            }
        }

        shouldResumeMonitorRealtime(symbol) {
            if (typeof symbol !== 'string') return false;
            const target = symbol.trim();
            if (!target) return false;
            return !!(this.monitorRealtimeShouldResume && this.monitorRealtimeResumeSymbol === target);
        }

        async subscribeMonitorRealtime(symbol, options = {}) {
            const { skipReload = false, reason = 'manual' } = options || {};
            if (typeof symbol !== 'string') return false;
            const target = symbol.trim();
            if (!target) return false;

            if (this.monitorRealtimeSymbol && this.monitorRealtimeSymbol !== target) {
                this.unsubscribeMonitorRealtime({ keepPreference: false, reason: 'switch-symbol' });
            }
            if (this.monitorRealtimeResumeSymbol && this.monitorRealtimeResumeSymbol !== target) {
                this.monitorRealtimeShouldResume = false;
                this.monitorRealtimeResumeSymbol = null;
            }

            if (this.monitorRealtimeSymbol === target) {
                this.monitorRealtimeShouldResume = true;
                this.monitorRealtimeResumeSymbol = target;
                if (this.currentMonitor) {
                    this.currentMonitor.realtimeActive = true;
                }
                if (!skipReload) {
                    this.loadMonitorChart(target, { useCache: false, forceRefresh: true });
                }
                return true;
            }

            try {
                console.log('[monitor] subscribe realtime', { symbol: target, reason });
                const ws = this.app?.ibkrManager?.ws;
                ws?.send?.(JSON.stringify({ type: 'subscribe_realtime', symbol: target }));
                this.monitorRealtimeSymbol = target;
                this.monitorRealtimeShouldResume = true;
                this.monitorRealtimeResumeSymbol = target;
                if (this.currentMonitor) {
                    this.currentMonitor.realtimeActive = true;
                }
                if (!skipReload) {
                    this.loadMonitorChart(target, { useCache: false, forceRefresh: true });
                }
                return true;
            } catch (err) {
                console.error('subscribe realtime failed', err);
                return false;
            }
        }

        unsubscribeMonitorRealtime(options = {}) {
            const { keepPreference = false, reason = 'manual' } = options || {};
            const activeSymbol = this.monitorRealtimeSymbol;
            if (activeSymbol) {
                try {
                    console.log('[monitor] unsubscribe realtime', activeSymbol, reason);
                    const ws = this.app?.ibkrManager?.ws;
                    ws?.send?.(JSON.stringify({ type: 'unsubscribe_realtime', symbol: activeSymbol }));
                } catch (err) {
                    console.warn('unsubscribe realtime failed', err);
                }
            }
            this.monitorRealtimeSymbol = null;
            if (this.currentMonitor) {
                this.currentMonitor.realtimeActive = keepPreference ? !!this.currentMonitor.realtimeActive : false;
            }
            if (!keepPreference) {
                this.monitorRealtimeShouldResume = false;
                this.monitorRealtimeResumeSymbol = null;
            } else if (activeSymbol) {
                this.monitorRealtimeShouldResume = true;
                this.monitorRealtimeResumeSymbol = activeSymbol;
            }
            return activeSymbol;
        }

        async exitMonitorFullscreen(options = {}) {
            const { removeListener = true } = options || {};
            const modalEl = document.getElementById('monitorModal');
            if (modalEl) {
                modalEl.classList.remove('monitor-fullscreen');
            }
            this.monitorModalFullscreen = false;
            if (document.fullscreenElement) {
                try {
                    if (document.exitFullscreen) {
                        await document.exitFullscreen();
                    } else if (document.webkitExitFullscreen) {
                        await document.webkitExitFullscreen();
                    }
                } catch (err) {
                    console.warn('exitFullscreen failed', err);
                }
            }
            if (removeListener && this._monitorFullscreenChangeHandler) {
                document.removeEventListener('fullscreenchange', this._monitorFullscreenChangeHandler);
                this._monitorFullscreenChangeHandler = null;
            }
        }

        updateMonitorStopLossLine() {
            if (!this.monitorChart) return;

            const stopLossInput = document.getElementById('monitorStopLoss');
            const stopLossPrice = stopLossInput?.value ? parseFloat(stopLossInput.value) : null;

            const annotations = this.app?.ensureMonitorAnnotations?.();
            if (!annotations) return;

            if (stopLossPrice && !Number.isNaN(stopLossPrice)) {
                annotations.stopLoss = {
                    type: 'line',
                    yMin: stopLossPrice,
                    yMax: stopLossPrice,
                    borderColor: 'rgb(255, 99, 132)',
                    borderWidth: 2,
                    borderDash: [5, 5],
                    label: {
                        content: `Stop Loss: $${stopLossPrice.toFixed(2)}`,
                        enabled: true,
                        position: 'start'
                    }
                };
                if (annotations.lossBox) {
                    if ((this.currentMonitor?.positionQty || 0) > 0) {
                        annotations.lossBox.yMin = stopLossPrice;
                    } else {
                        annotations.lossBox.yMax = stopLossPrice;
                    }
                }
            } else {
                delete annotations.stopLoss;
            }

            this.monitorChart.update('none');
        }

        updateMonitorTakeProfitLine() {
            if (!this.monitorChart) return;

            const takeProfitInput = document.getElementById('monitorTakeProfit');
            const takeProfitPrice = takeProfitInput?.value ? parseFloat(takeProfitInput.value) : null;

            const annotations = this.app?.ensureMonitorAnnotations?.();
            if (!annotations) return;

            if (takeProfitPrice && !Number.isNaN(takeProfitPrice)) {
                annotations.takeProfit = {
                    type: 'line',
                    yMin: takeProfitPrice,
                    yMax: takeProfitPrice,
                    borderColor: 'rgb(75, 192, 192)',
                    borderWidth: 2,
                    borderDash: [5, 5],
                    label: {
                        content: `Take Profit: $${takeProfitPrice.toFixed(2)}`,
                        enabled: true,
                        position: 'start'
                    }
                };
                if (annotations.profitBox) {
                    if ((this.currentMonitor?.positionQty || 0) > 0) {
                        annotations.profitBox.yMax = takeProfitPrice;
                    } else {
                        annotations.profitBox.yMin = takeProfitPrice;
                    }
                }
            } else {
                delete annotations.takeProfit;
            }

            this.monitorChart.update('none');
        }

        async loadMonitorOrdersList(symbol, status = 'working') {
            if (this.app?.orderManager?.loadMonitorOrdersList) {
                return this.app.orderManager.loadMonitorOrdersList(symbol, status);
            }
            return null;
        }

        async _loadMonitorOrdersList(symbol, status = 'working') {
            if (this.app?.orderManager?._loadMonitorOrdersList) {
                return this.app.orderManager._loadMonitorOrdersList(symbol, status);
            }
            return null;
        }

        async openMonitorModal(...args) {
            if (typeof legacyOpenMonitorModal === 'function' && this.app) {
                return legacyOpenMonitorModal.apply(this.app, args);
            }
            return undefined;
        }

        async syncMonitorModalData(...args) {
            if (typeof legacySyncMonitorModalData === 'function' && this.app) {
                return legacySyncMonitorModalData.apply(this.app, args);
            }
            return undefined;
        }

        async loadMonitorChart(...args) {
            if (typeof legacyLoadMonitorChart === 'function' && this.app) {
                return legacyLoadMonitorChart.apply(this.app, args);
            }
            return undefined;
        }

        displayChart(...args) {
            if (typeof legacyDisplayChart === 'function' && this.app) {
                return legacyDisplayChart.apply(this.app, args);
            }
            return undefined;
        }

        updateMonitorRealtime(...args) {
            if (typeof legacyUpdateMonitorRealtime === 'function' && this.app) {
                return legacyUpdateMonitorRealtime.apply(this.app, args);
            }
            return undefined;
        }
    }

    monitoring.manager = monitoring.manager || {};
    monitoring.manager.MonitoringManager = MonitoringManager;
    root.MonitoringManager = MonitoringManager;
})(typeof window !== 'undefined' ? window : globalThis);
