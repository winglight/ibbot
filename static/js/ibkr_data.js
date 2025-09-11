/**
 * IBKR Data Manager
 * 负责处理IBKR相关的数据获取和UI更新
 */
class IBKRDataManager {
    constructor(app) {
        this.app = app;
        this.positions = [];
        this.priceCache = new Map();
        this.priceCacheTimeout = 30000; // 30秒缓存
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

            // 安全提取字段并提供默认值
            const account_id = info.account_id || 'N/A';
            const account_type = info.account_type || 'N/A';
            const net_liquidation = Number(info.net_liquidation) || 0;
            const available_funds = Number(info.available_funds) || 0;
            const buying_power = Number(info.buying_power) || 0;
            const unrealized_pnl = Number(info.unrealized_pnl) || 0;
            const realized_pnl = Number(info.realized_pnl) || 0;

            accountInfoDiv.innerHTML = `
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>Account ID:</strong> ${account_id}</p>
                        <p><strong>Account Type:</strong> ${account_type}</p>
                        <p><strong>Net Liquidation:</strong> $${this.app.formatNumber(net_liquidation)}</p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>Available Funds:</strong> $${this.app.formatNumber(available_funds)}</p>
                        <p><strong>Buying Power:</strong> $${this.app.formatNumber(buying_power)}</p>
                        <p class="${unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Unrealized P&L:</strong> $${this.app.formatNumber(unrealized_pnl)}
                        </p>
                        <p class="${realized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Realized P&L:</strong> $${this.app.formatNumber(realized_pnl)}
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
    async loadPortfolio() {
        try {
            this.updatePortfolioUI(this.positions);
        } catch (error) {
            console.error('Failed to load portfolio:', error);
        }
    }

    /**
     * 更新投资组合UI
     */
    updatePortfolioUI(positions) {
        try {
            const tbody = document.querySelector('#positionsTable tbody');
            if (!tbody) return;
            
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
        } catch (e) {
            console.error('updatePortfolioUI failed', e);
        }
    }

    /**
     * 更新持仓UI（用于WebSocket实时更新）
     */
    updatePositionsUI(positions) {
        this.positions = positions;
        this.updatePortfolioUI(positions);
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
        
        // 缓存过期或不存在，重新获取
        try {
            const response = await this.app.apiCall('/current_price', {
                method: 'POST',
                body: JSON.stringify({ 
                    symbol: symbol,
                    exchange: 'SMART'
                }),
                // 此接口为轻量级非关键查询：缩短超时并禁用重试，避免前端长时间等待
                timeout: 6000,
                maxRetries: 0
            });
            
            if (response.success && response.price) {
                // 更新缓存
                this.priceCache.set(cacheKey, {
                    price: response.price,
                    timestamp: now
                });
                
                return response.price;
            }
        } catch (error) {
            // /api/current_price接口不重要，静默处理错误
        }
        
        return null;
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