/**
 * IBKR Data Manager
 * 负责处理IBKR相关的数据获取和UI更新
 */
class IBKRDataManager {
    constructor(app) {
        this.app = app;
        this.positions = [];
        this.priceCache = new Map();
        this.priceRequestPromises = new Map();
        this.priceCacheTimeout = 30000; // 30秒缓存
        this.dailySummaryLoading = false;
        this.dailySummaryChart = null;
        this.lastAccountInfo = null;
    }

    /**
     * 加载账户信息
     */
    async loadAccountInfo() {
        try {
            const data = await this.app.apiCall('/account_info');
            // 后端在未连接时返回 { success: false, connected: false, last_error }
            if (data && data.connected === false) {
                this.updateAccountInfoUI({ connected: false, last_error: data.last_error || null });
                return;
            }
            const info = (data && data.account_info) ? data.account_info : null;
            this.updateAccountInfoUI(info);
        } catch (error) {
            console.error('Failed to load account info:', error);
        }
    }

    async loadDailyAccountSummary(options = {}) {
        const { days = 30, force = false, showLoading = false } = options || {};
        const normalizedDays = Math.max(1, Math.min(365, parseInt(days, 10) || 30));
        const tbody = document.querySelector('#dailyAccountSummaryTable tbody');
        if (tbody && showLoading) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" class="text-center text-muted py-3">
                        <div class="spinner-border spinner-border-sm me-2" role="status"></div>
                        正在加载每日盈余数据...
                    </td>
                </tr>
            `;
        }

        if (this.dailySummaryLoading && !force) {
            return;
        }
        this.dailySummaryLoading = true;

        try {
            const response = await this.app.apiCall(`/account/daily-summary?days=${normalizedDays}`, {
                allowReadOnly: true,
                useCache: false,
                skipReadOnlyCheck: false,
            });

            if (response && response.success) {
                this.updateDailySummaryUI(response);
            } else {
                const message = response?.detail || '未能获取每日盈余数据';
                this.showDailySummaryError(message);
            }
        } catch (error) {
            this.showDailySummaryError(error.message || '加载每日盈余数据失败');
        } finally {
            this.dailySummaryLoading = false;
        }
    }

    showDailySummaryError(message) {
        const tbody = document.querySelector('#dailyAccountSummaryTable tbody');
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" class="text-center text-muted py-3">
                        <i class="fas fa-exclamation-triangle me-2"></i>${message}
                    </td>
                </tr>
            `;
        }
        const metaEl = document.getElementById('dailySummaryMeta');
        if (metaEl) {
            metaEl.textContent = '';
        }
        this.renderDailySummaryChart([]);
    }

    updateDailySummaryUI(data) {
        const tbody = document.querySelector('#dailyAccountSummaryTable tbody');
        if (!tbody) return;

        const summaryDays = Array.isArray(data.days) ? [...data.days] : [];
        summaryDays.sort((a, b) => {
            const da = new Date(a?.date || a?.day || 0);
            const db = new Date(b?.date || b?.day || 0);
            return db - da;
        });

        const rows = summaryDays.map(day => {
            const dateText = day.date || '';
            const orders = Number(day.orders || 0);
            const pnlValue = Number(day.realized_pnl || 0);
            const pnlClass = pnlValue >= 0 ? 'profit' : 'loss';
            const startVal = day.starting_surplus;
            const endVal = day.ending_surplus;
            const formatCurrency = (value) => {
                if (value === null || value === undefined) return '—';
                return `$${this.app.formatNumber(Number(value))}`;
            };
            return `
                <tr>
                    <td>${dateText}</td>
                    <td class="text-end">${orders}</td>
                    <td class="text-end ${pnlClass}">$${this.app.formatNumber(pnlValue)}</td>
                    <td class="text-end">${formatCurrency(startVal)}</td>
                    <td class="text-end">${formatCurrency(endVal)}</td>
                </tr>
            `;
        }).join('');

        if (rows) {
            tbody.innerHTML = rows;
        } else {
            tbody.innerHTML = `
                <tr>
                    <td colspan="5" class="text-center text-muted py-3">暂无数据</td>
                </tr>
            `;
        }

        const metaEl = document.getElementById('dailySummaryMeta');
        if (metaEl) {
            const sourceMap = {
                ibkr: 'IBKR实时净值',
                ibkr_cached: 'IBKR缓存净值',
                risk_manager: '风险控制缓存',
                unknown: '估算'
            };
            const source = sourceMap[data.surplus_source] || sourceMap.unknown;
            const netLiq = data.net_liquidation !== null && data.net_liquidation !== undefined
                ? `$${this.app.formatNumber(Number(data.net_liquidation))}`
                : '未提供';
            const netLiqTime = data.net_liquidation_timestamp ? `（更新于 ${this.app.formatDateTime?.(data.net_liquidation_timestamp) || data.net_liquidation_timestamp}）` : '';
            const rangeText = data.range
                ? `${data.range.start || ''} → ${data.range.end || ''}`
                : '';
            const tz = data.timezone || 'America/New_York';
            metaEl.textContent = `参考净资产：${netLiq}${netLiqTime}，数据范围：${rangeText}，盈余来源：${source}，时区：${tz}`;
        }

        this.renderDailySummaryChart(summaryDays);
    }

    renderDailySummaryChart(summaryDays) {
        const canvas = document.getElementById('dailySummaryChart');
        if (!canvas) return;
        if (typeof Chart === 'undefined') return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;

        const chartSeries = Array.isArray(summaryDays) ? [...summaryDays].reverse() : [];
        if (!chartSeries.length) {
            if (this.dailySummaryChart) {
                this.dailySummaryChart.data.labels = [];
                this.dailySummaryChart.data.datasets.forEach(ds => { ds.data = []; });
                this.dailySummaryChart.update('none');
            }
            return;
        }
        const labels = chartSeries.map(item => item?.date || item?.day || '');
        const pnlData = chartSeries.map(item => {
            const value = Number(item?.realized_pnl);
            return Number.isFinite(value) ? value : 0;
        });
        const surplusData = chartSeries.map(item => {
            const endValue = item?.ending_surplus;
            const startValue = item?.starting_surplus;
            const resolved = endValue !== null && endValue !== undefined ? Number(endValue)
                : (startValue !== null && startValue !== undefined ? Number(startValue) : null);
            return Number.isFinite(resolved) ? resolved : null;
        });

        const datasets = [
            {
                label: 'Realized PnL',
                data: pnlData,
                borderColor: '#38bdf8',
                backgroundColor: 'rgba(56,189,248,0.15)',
                tension: 0.25,
                fill: true,
                yAxisID: 'pnlAxis',
            },
            {
                label: 'Ending Surplus',
                data: surplusData,
                borderColor: '#22c55e',
                backgroundColor: 'rgba(34,197,94,0.15)',
                tension: 0.25,
                fill: false,
                yAxisID: 'surplusAxis',
            }
        ];

        if (!this.dailySummaryChart) {
            this.dailySummaryChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets,
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: {
                            ticks: { maxRotation: 0, autoSkip: true },
                            grid: { color: 'rgba(148,163,184,0.15)' },
                        },
                        pnlAxis: {
                            position: 'left',
                            title: { display: true, text: 'PnL (USD)' },
                            grid: { color: 'rgba(148,163,184,0.1)' },
                        },
                        surplusAxis: {
                            position: 'right',
                            title: { display: true, text: 'Surplus (USD)' },
                            grid: { drawOnChartArea: false },
                        },
                    },
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label(context) {
                                    const label = context.dataset.label || '';
                                    const value = Number(context.parsed.y);
                                    if (!Number.isFinite(value)) return `${label}: N/A`;
                                    return `${label}: $${value.toFixed(2)}`;
                                },
                            },
                        },
                    },
                },
            });
        } else {
            this.dailySummaryChart.data.labels = labels;
            this.dailySummaryChart.data.datasets[0].data = pnlData;
            this.dailySummaryChart.data.datasets[1].data = surplusData;
            this.dailySummaryChart.update('none');
        }
    }

    /**
     * 更新账户信息UI
     */
    updateAccountInfoUI(info) {
        try {
            const accountInfoDiv = document.getElementById('accountInfo');
            if (!accountInfoDiv) return;

            // 未连接或无信息时的占位展示
            if (!info || (typeof info === 'object' && info.connected === false)) {
                const lastErr = info && info.last_error ? String(info.last_error) : '';
                this.lastAccountInfo = null;
                accountInfoDiv.innerHTML = `
                    <div class="alert alert-warning mb-2" role="alert">
                        未连接到 IBKR${lastErr ? `：${lastErr}` : ''}
                    </div>
                    <div class="row">
                        <div class="col-md-6">
                            <p><strong>Account ID:</strong> N/A</p>
                            <p><strong>Account Type:</strong> N/A</p>
                            <p><strong>Net Liquidation:</strong> $0</p>
                        </div>
                        <div class="col-md-6">
                            <p><strong>Available Funds:</strong> $0</p>
                            <p><strong>Buying Power:</strong> $0</p>
                            <p class="loss"><strong>Unrealized P&L:</strong> $0</p>
                            <p class="loss"><strong>Realized P&L:</strong> $0</p>
                        </div>
                    </div>
                `;
                return;
            }

            const parseNumber = (value) => {
                if (typeof value === 'number' && Number.isFinite(value)) {
                    return value;
                }
                if (typeof value === 'string') {
                    const normalized = value.replace(/,/g, '').trim();
                    if (!normalized) return null;
                    const parsed = Number(normalized);
                    return Number.isFinite(parsed) ? parsed : null;
                }
                return null;
            };

            const lastSnapshot = this.lastAccountInfo || {};

            // 安全提取字段并提供默认值
            const account_id = info.account_id || lastSnapshot.account_id || 'N/A';
            const account_type = info.account_type || lastSnapshot.account_type || 'N/A';
            const net_liquidation = parseNumber(info.net_liquidation);
            const available_funds = parseNumber(info.available_funds);
            const buying_power = parseNumber(info.buying_power);
            const unrealized_pnl = parseNumber(info.unrealized_pnl);
            const realized_pnl = parseNumber(info.realized_pnl);

            const normalizedInfo = {
                account_id,
                account_type,
                net_liquidation: net_liquidation ?? lastSnapshot.net_liquidation ?? 0,
                available_funds: available_funds ?? lastSnapshot.available_funds ?? 0,
                buying_power: buying_power ?? lastSnapshot.buying_power ?? 0,
                unrealized_pnl: unrealized_pnl ?? lastSnapshot.unrealized_pnl ?? 0,
                realized_pnl: realized_pnl ?? lastSnapshot.realized_pnl ?? 0,
            };

            this.lastAccountInfo = { ...normalizedInfo };

            accountInfoDiv.innerHTML = `
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>Account ID:</strong> ${normalizedInfo.account_id}</p>
                        <p><strong>Account Type:</strong> ${normalizedInfo.account_type}</p>
                        <p><strong>Net Liquidation:</strong> $${this.app.formatNumber(normalizedInfo.net_liquidation)}</p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>Available Funds:</strong> $${this.app.formatNumber(normalizedInfo.available_funds)}</p>
                        <p><strong>Buying Power:</strong> $${this.app.formatNumber(normalizedInfo.buying_power)}</p>
                        <p class="${normalizedInfo.unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Unrealized P&L:</strong> $${this.app.formatNumber(normalizedInfo.unrealized_pnl)}
                        </p>
                        <p class="${normalizedInfo.realized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Realized P&L:</strong> $${this.app.formatNumber(normalizedInfo.realized_pnl)}
                        </p>
                    </div>
                </div>
            `;
        } catch (e) {
            console.error('updateAccountInfoUI failed', e);
        }
    }

    /**
     * 加载投资组合
     */
    async loadPortfolio(options = {}) {
        const {
            fast = false,
            useCache = false,
            force = false,
            minInterval,
        } = options || {};

        try {
            if (!this.app?.ibkrManager || typeof this.app.ibkrManager.requestPositionsSnapshot !== 'function') {
                console.warn('IBKR manager unavailable when requesting portfolio snapshot');
                this.showPortfolioError('无法请求持仓快照：未初始化 WebSocket 连接');
                return false;
            }

            const status = this.app.ibkrManager.requestPositionsSnapshot({
                fast,
                useCache,
                force,
                minInterval,
            });

            if (status === 'failed') {
                this.showPortfolioError('无法通过 WebSocket 请求持仓数据');
                return false;
            }

            // 返回 true 表示已通过 WebSocket 请求，等待异步更新
            return status === 'sent' || status === 'queued';
        } catch (error) {
            console.error('Failed to request portfolio via websocket:', error);
            this.showPortfolioError('请求持仓数据失败');
            return false;
        }
    }

    showPortfolioError(message) {
        if (this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('portfolio', false);
        }
        const tbody = document.querySelector('#positionsTable tbody');
        if (!tbody) return;
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="text-center text-danger py-3">
                    <i class="fas fa-exclamation-triangle me-2"></i>${message}
                </td>
            </tr>
        `;
    }

    showPortfolioEmpty(message = '暂无持仓数据') {
        if (this.app && typeof this.app.setPanelLoading === 'function') {
            this.app.setPanelLoading('portfolio', false);
        }
        const tbody = document.querySelector('#positionsTable tbody');
        if (!tbody) return;
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="text-center text-muted py-3">${message}</td>
            </tr>
        `;
    }

    /**
     * 更新投资组合UI
     */
    updatePortfolioUI(positions) {
        try {
            const tbody = document.querySelector('#positionsTable tbody');
            if (!tbody) {
                if (this.app && typeof this.app.setPanelLoading === 'function') {
                    this.app.setPanelLoading('portfolio', false);
                }
                return;
            }

            if (!Array.isArray(positions) || positions.length === 0) {
                this.showPortfolioEmpty();
                return;
            }

            tbody.innerHTML = positions.map(pos => `
                <tr>
                    <td>${pos.symbol}</td>
                    <td>${pos.position}</td>
                    <td>$${this.app.formatNumber(pos.avg_cost)}</td>
                    <td>$${this.app.formatNumber(pos.current_price)}</td>
                    <td class="${pos.unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                        $${this.app.formatNumber(pos.unrealized_pnl)}
                    </td>
                    <td class="${pos.unrealized_pnl_percent >= 0 ? 'profit' : 'loss'}">
                        ${this.app.formatNumber(pos.unrealized_pnl_percent)}%
                    </td>
                    <td>
                        <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editPositionRiskRule('${pos.symbol}', ${pos.current_price}, ${pos.avg_cost}, ${pos.position})" title="Set TP/SL">
                            <i class="fas fa-shield-alt"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-success" onclick="app.openMonitorModal('${pos.symbol}', ${pos.position}, ${pos.current_price}, ${pos.avg_cost}, ${pos.unrealized_pnl}, ${pos.unrealized_pnl_percent})" title="Monitor">
                            <i class="fas fa-eye"></i>
                        </button>
                    </td>
                </tr>
            `).join('');
            if (this.app && typeof this.app.setPanelLoading === 'function') {
                this.app.setPanelLoading('portfolio', false);
            }
        } catch (e) {
            console.error('updatePortfolioUI failed', e);
        }
    }

    /**
     * 更新持仓UI（用于WebSocket实时更新）
     */
    updatePositionsUI(positions) {
        this.positions = Array.isArray(positions) ? positions : [];
        this.updatePortfolioUI(this.positions);
    }

    handlePositionsSnapshot(payload) {
        if (!payload) return false;

        if (payload.success === false) {
            const err = payload.error || payload.detail || '无法获取持仓数据';
            this.showPortfolioError(err);
            if (Array.isArray(payload.positions)) {
                this.updatePositionsUI(payload.positions);
            }
            return true;
        }

        if (Array.isArray(payload.positions)) {
            this.updatePositionsUI(payload.positions);
            return true;
        }

        return false;
    }

    /**
     * 检查是否为开仓操作
     */
    isOpeningPosition(symbol, action) {
        if (!this.positions || !symbol) return true;
        
        const position = this.positions.find(pos => pos.symbol === symbol.toUpperCase());
        if (!position || position.position === 0) {
            return true; // No existing position, this is opening
        }
        
        // Check if action is opposite to current position
        const currentPosition = position.position;
        if ((currentPosition > 0 && action === 'SELL') || (currentPosition < 0 && action === 'BUY')) {
            return false; // This is closing position
        }
        
        return true; // This is adding to existing position (opening)
    }

    /**
     * 带缓存的价格获取方法
     */
    async getCachedPrice(symbol) {
        const cacheKey = symbol.toUpperCase();
        const now = Date.now();

        // 检查缓存
        if (this.priceCache.has(cacheKey)) {
            const cached = this.priceCache.get(cacheKey);
            if (now - cached.timestamp < this.priceCacheTimeout) {
                return cached.price;
            }
        }

        if (this.priceRequestPromises.has(cacheKey)) {
            return this.priceRequestPromises.get(cacheKey);
        }

        const fetchPromise = (async () => {
            try {
                const response = await this.app.apiCall('/current_price', {
                    method: 'POST',
                    body: JSON.stringify({
                        symbol: symbol,
                        exchange: 'SMART'
                    }),
                    allowReadOnly: true,
                    // 此接口为轻量级非关键查询：缩短超时并禁用重试，避免前端长时间等待
                    timeout: 6000,
                    maxRetries: 0
                });

                if (response.success && response.price) {
                    // 更新缓存
                    this.priceCache.set(cacheKey, {
                        price: response.price,
                        timestamp: Date.now()
                    });

                    return response.price;
                }
            } catch (error) {
                // /current_price接口不重要，静默处理错误
            } finally {
                this.priceRequestPromises.delete(cacheKey);
            }

            return null;
        })();

        this.priceRequestPromises.set(cacheKey, fetchPromise);
        return fetchPromise;
    }

    /**
     * 清除价格缓存
     */
    clearPriceCache() {
        this.priceCache.clear();
    }

    /**
     * 获取持仓信息
     */
    getPosition(symbol) {
        if (!this.positions || !symbol) return null;
        return this.positions.find(pos => pos.symbol === symbol.toUpperCase());
    }

    /**
     * 获取所有持仓
     */
    getAllPositions() {
        return this.positions || [];
    }
}

// 导出到全局
window.IBKRDataManager = IBKRDataManager;
