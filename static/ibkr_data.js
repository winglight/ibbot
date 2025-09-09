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
            const info = data.account_info;
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
            
            accountInfoDiv.innerHTML = `
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>Account ID:</strong> ${info.account_id || 'N/A'}</p>
                        <p><strong>Account Type:</strong> ${info.account_type}</p>
                        <p><strong>Net Liquidation:</strong> $${this.app.formatNumber(info.net_liquidation)}</p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>Available Funds:</strong> $${this.app.formatNumber(info.available_funds)}</p>
                        <p><strong>Buying Power:</strong> $${this.app.formatNumber(info.buying_power)}</p>
                        <p class="${info.unrealized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Unrealized P&L:</strong> $${this.app.formatNumber(info.unrealized_pnl)}
                        </p>
                        <p class="${info.realized_pnl >= 0 ? 'profit' : 'loss'}">
                            <strong>Realized P&L:</strong> $${this.app.formatNumber(info.realized_pnl)}
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
            const data = await this.app.apiCall('/positions');
            this.positions = data.positions;
            this.updatePortfolioUI(data.positions);
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