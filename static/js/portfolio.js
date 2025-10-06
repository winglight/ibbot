(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const portfolioNamespace = appModules.portfolio = appModules.portfolio || {};

    /**
     * 投资组合（Portfolio）面板模块
     * 负责 Portfolio 面板的渲染与交互（纯 UI 职责），数据来源由 IBKRDataManager 提供。
     */
    class PortfolioManager {
        constructor(app) {
            this.app = app;
        }

        /**
         * 加载并渲染投资组合（使用 DataManager 当前缓存的数据）
         */
        loadPortfolio() {
            try {
                const positions = this.app.dataManager.getAllPositions();
                this.updatePortfolioUI(positions);
            } catch (error) {
                console.error('Failed to load portfolio:', error);
            }
        }

        /**
         * 更新 Portfolio 表格 UI
         */
        updatePortfolioUI(positions) {
            try {
                const tbody = document.querySelector('#positionsTable tbody');
                if (!tbody) return;

                // 使用字符串渲染（数据量较小，简单直接）。
                // 如需逐行 DOM 构建以避免潜在文本节点问题，可改为 createElement 方案。
                tbody.innerHTML = (positions || []).map(pos => `
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
                        <td>${pos.account || '--'}</td>
                        <td>${pos.strategy || '--'}</td>
                        <td class="text-nowrap">
                            <button class="btn btn-sm btn-outline-primary me-1" onclick="app.editPositionRiskRule('${pos.symbol}', ${pos.current_price}, ${pos.avg_cost}, ${pos.position})" title="Set TP/SL">
                                <i class="fas fa-shield-alt"></i>
                            </button>
                            <button class="btn btn-sm btn-outline-success" onclick="app.openMonitorModal('${pos.symbol}', ${pos.position}, ${pos.current_price}, ${pos.avg_cost}, ${pos.unrealized_pnl}, ${pos.unrealized_pnl_percent})" title="Monitor">
                                <i class="fas fa-eye"></i>
                            </button>
                            <button class="btn btn-sm btn-outline-danger ms-1" onclick="app.closePosition('${pos.symbol}', { positionQty: ${Number(pos.position) || 0}, source: 'portfolio' })" title="平仓">
                                <i class="fas fa-times-circle"></i>
                            </button>
                        </td>
                    </tr>
                `).join('');
            } catch (e) {
                console.error('PortfolioManager.updatePortfolioUI failed', e);
            }
        }
    }

    portfolioNamespace.PortfolioManager = PortfolioManager;
    root.PortfolioManager = PortfolioManager;
})(typeof window !== 'undefined' ? window : globalThis);
