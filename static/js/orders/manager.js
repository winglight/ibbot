(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const ordersNamespace = appModules.orders = appModules.orders || {};
    const managerNamespace = ordersNamespace.manager = ordersNamespace.manager || {};

/**
 * 订单管理模块
 * 负责订单相关的所有功能，包括加载、创建、修改、取消订单等
 */
class OrderManager {
    constructor(app) {
        this.app = app;
        this.orders = [];
        this.ordersById = new Map();
        this.ordersCache = {};
        this.debouncedLoadOrders = null;
        this.debouncedLoadMonitorOrdersList = null;
        this.lastOrdersStartDate = null;
        this.ordersRefreshInterval = null;
        this.pagination = {
            page: 1,
            pageSize: 10,
            total: 0,
            totalPages: 1,
            hasNext: false,
            hasPrev: false,
        };
        this.initOrdersDebounce();
        this.wsSubscriptions = [];
        this.registerWebSocketHandlers();
        this.setupPaginationControls();
    }

    /**
     * 初始化订单加载防抖
     */
    initOrdersDebounce() {
        this.debouncedLoadOrders = this.debounce(this._loadOrders.bind(this), 300);
    }

    /**
     * 防抖函数
     */
    debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    }

    /**
     * 加载订单列表（带防抖）
     */
    registerWebSocketHandlers() {
        if (!this.app || typeof this.app.registerWebSocketHandler !== 'function') return;
        const unsubscribe = this.app.registerWebSocketHandler('orders_snapshot', (payload) => this.handleOrdersSnapshot(payload));
        this.wsSubscriptions.push(unsubscribe);
    }

    async loadOrders(options = {}) {
        console.log('🔄 loadOrders called');

        // 调用防抖函数
        this.debouncedLoadOrders(options);
    }

    /**
     * 实际加载订单的函数
     */
    async _loadOrders(options = {}) {
        try {
            console.log('🔄 _loadOrders executing');

            // 只获取当天订单，减少数据传输量
            const today = new Date();
            const startOfDay = new Date(today.getFullYear(), today.getMonth(), today.getDate()).toISOString();
            console.log('🌐 Requesting today\'s orders snapshot via WebSocket');
            const pageSize = Number.isFinite(options.limit) ? Number(options.limit) : (this.pagination?.pageSize || 10);
            const requestedPage = Number.isFinite(options.page) ? Number(options.page) : (this.pagination?.page || 1);
            const safePage = Math.max(1, requestedPage);
            if (!Number.isFinite(this.pagination.pageSize) || this.pagination.pageSize !== pageSize) {
                this.pagination.pageSize = pageSize;
            }
            if (!Number.isFinite(this.pagination.page) || this.pagination.page !== safePage) {
                this.pagination.page = safePage;
            }
            if (this.app && this.app.ibkrManager && typeof this.app.ibkrManager.requestOrdersSnapshot === 'function') {
                this.app.ibkrManager.requestOrdersSnapshot({
                    limit: pageSize,
                    startDate: startOfDay,
                    force: Boolean(options.force),
                    page: safePage,
                });
            }

        } catch (error) {
            console.error('Failed to load orders:', error);
            if (this.app && typeof this.app.setPanelLoading === 'function') {
                this.app.setPanelLoading('orders', false);
            }
            const tbody = document.querySelector('#ordersTable tbody');
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="11" class="text-center text-danger">Failed to load orders</td></tr>';
            }
        }
    }

    initMonitorOrdersDebounce() {
        if (!this.debouncedLoadMonitorOrdersList) {
            this.debouncedLoadMonitorOrdersList = this.debounce(this._loadMonitorOrdersList.bind(this), 300);
        }
    }

    async loadMonitorOrdersList(symbol, status = 'working') {
        this.initMonitorOrdersDebounce();

        const ordersList = document.getElementById('monitorOrdersList');
        if (!ordersList) return;

        ordersList.innerHTML = '<div class="text-center text-muted py-2"><div class="spinner-border spinner-border-sm text-primary me-2" role="status"></div><small>加载订单数据中...</small></div>';

        this.debouncedLoadMonitorOrdersList(symbol, status);
    }

    async _loadMonitorOrdersList(symbol, status = 'working') {
        try {
            const ordersList = document.getElementById('monitorOrdersList');
            if (!ordersList) return;

            const cacheKey = `orders_${symbol}_${status}`;
            const now = Date.now();
            const cachedData = this.ordersCache[cacheKey];

            const isSameDay = (dateValue) => {
                if (this.app && typeof this.app.isSameDay === 'function') {
                    return this.app.isSameDay(dateValue);
                }
                const value = dateValue ? new Date(dateValue) : null;
                if (!(value instanceof Date) || Number.isNaN(value.getTime())) return false;
                const today = new Date();
                return value.getFullYear() === today.getFullYear()
                    && value.getMonth() === today.getMonth()
                    && value.getDate() === today.getDate();
            };

            let result;
            if (cachedData && (now - cachedData.timestamp) < 10 * 1000) {
                console.log(`Using cached orders data for ${symbol} (${status})`);
                result = cachedData.data;
            } else {
                console.log(`Fetching fresh orders data for ${symbol} (${status})`);
                result = await this.app.apiCall('/orders', {
                    useCache: true,
                    cacheTime: 10000,
                    timeout: 20000,
                });

                this.ordersCache[cacheKey] = {
                    data: result,
                    timestamp: now,
                };
            }

            let orders = Array.isArray(result?.orders) ? result.orders : [];
            orders = orders.filter(order => isSameDay(order?.submit_time || order?.created_at));

            const filteredOrders = orders.filter(order => {
                if (!order) return false;
                const matchSymbol = order.symbol === symbol;
                if (order.order_type === 'MKT') {
                    return matchSymbol && status === 'filled';
                }
                const workingStatuses = ['PendingSubmit', 'Submitted', 'PreSubmitted', 'ApiPending'];
                const filledStatuses = ['Filled'];
                const matchStatus = status === 'working'
                    ? workingStatuses.includes(order.status)
                    : filledStatuses.includes(order.status);
                return matchSymbol && matchStatus;
            });

            if (filteredOrders.length === 0) {
                ordersList.innerHTML = '<div class="text-center text-muted py-2"><small>暂无订单</small></div>';
                return;
            }

            let html = '';
            filteredOrders.forEach(order => {
                const side = order.action === 'BUY' ? '买入' : '卖出';
                const sideClass = order.action === 'BUY' ? 'text-success' : 'text-danger';
                const quantity = Math.abs(parseFloat(order.quantity || 0));
                const orderType = order.order_type || 'MKT';
                const limitPrice = parseFloat(order.limit_price || 0);
                const avgFillPrice = parseFloat(order.avg_fill_price || 0);

                let priceText;
                if (orderType === 'LMT' && limitPrice > 0) {
                    priceText = `$${limitPrice.toFixed(2)} (限价)`;
                } else if (avgFillPrice > 0) {
                    priceText = `$${avgFillPrice.toFixed(2)} (成交)`;
                } else {
                    priceText = '市价';
                }

                const needSyncButton = orderType === 'LMT' && order.status === 'Submitted';

                html += `
                    <div class="border-bottom py-1">
                        <div class="d-flex justify-content-between align-items-center">
                            <div>
                                <span class="badge ${sideClass === 'text-success' ? 'bg-success' : 'bg-danger'} me-1">${side}</span>
                                <small class="text-muted">${quantity}</small>
                            </div>
                            <div class="text-end d-flex align-items-center">
                                <small class="${sideClass} me-2">${priceText}</small>
                                ${needSyncButton ? `<button class="btn btn-outline-primary btn-sm" onclick="app.orderManager.syncLimitOrder('${order.order_id}', null, event)" title="同步订单状态"><i class="fas fa-sync-alt"></i></button>` : ''}
                            </div>
                        </div>
                    </div>
                `;
            });

            ordersList.innerHTML = html;
        } catch (error) {
            console.error('加载订单列表失败:', error);
            const ordersList = document.getElementById('monitorOrdersList');
            if (ordersList) {
                ordersList.innerHTML = '<div class="text-center text-muted py-2"><small>加载失败</small></div>';
            }
        }
    }

    async closePosition(symbol, options = {}) {
        const { positionQty: rawQty = null, skipConfirm = false } = options || {};
        const normalizedSymbol = typeof symbol === 'string' ? symbol.trim() : '';
        if (!normalizedSymbol) {
            return false;
        }

        const lookupSymbol = normalizedSymbol.toUpperCase();
        let qty = rawQty;
        if (qty === null || typeof qty === 'undefined') {
            const positionInfo = this.app?.dataManager?.getPosition?.(lookupSymbol)
                || this.app?.dataManager?.getPosition?.(normalizedSymbol);
            if (positionInfo) {
                qty = positionInfo.position;
            }
        }
        qty = Number(qty);
        if (!Number.isFinite(qty) || Math.abs(qty) < 1e-9) {
            this.app?.showAlert?.('warning', '当前无持仓，无法平仓');
            return false;
        }

        if (!skipConfirm) {
            const confirmed = typeof window !== 'undefined'
                ? window.confirm(`确认平仓 ${lookupSymbol}？\n持仓量: ${qty}\n将以市价单全部平仓`)
                : true;
            if (!confirmed) {
                return false;
            }
        }

        try {
            const response = await this.app.apiCall('/close_position', {
                method: 'POST',
                body: JSON.stringify({ symbol: normalizedSymbol }),
            });

            if (response?.success) {
                const actionText = response.action || 'CLOSE';
                const quantityText = Number.isFinite(response.quantity) ? response.quantity : qty;
                const orderIdText = response.order_id ? ` (订单ID: ${response.order_id})` : '';
                this.app.showAlert('success', `平仓订单已提交: ${actionText} ${quantityText} ${lookupSymbol}${orderIdText}`);
                await this.app.loadPortfolio();
                this.loadOrders();

                const monitorSymbol = (this.app.currentMonitor?.symbol || '').trim();
                if (monitorSymbol && monitorSymbol.toUpperCase() === lookupSymbol && typeof this.app.syncMonitorModalData === 'function') {
                    try {
                        await this.app.syncMonitorModalData(lookupSymbol);
                    } catch (syncErr) {
                        console.warn('Failed to sync monitor modal after close position:', syncErr);
                    }
                }
                return true;
            }

            const failureReason = (response && response.message) ? response.message : '未知错误';
            this.app.showAlert('danger', `平仓失败: ${failureReason}`);
        } catch (err) {
            if (err?.isReadOnly) {
                return false;
            }
            console.error('平仓失败:', err);
            const message = err?.message || err;
            this.app.showAlert('danger', `平仓失败: ${message}`);
        }

        return false;
    }

    handleOrdersSnapshot(message) {
        try {
            if (this.app && typeof this.app.setPanelLoading === 'function') {
                this.app.setPanelLoading('orders', false);
            }
            if (!message || message.success === false) {
                const errorText = message?.error || message?.detail || 'Failed to fetch orders via websocket';
                console.warn('orders_snapshot error:', errorText);
                const tbody = document.querySelector('#ordersTable tbody');
                const isRateLimited = typeof errorText === 'string' && errorText.toLowerCase().includes('rate');
                if (tbody) {
                    if (isRateLimited) {
                        const previous = Array.isArray(this.app?.orders) ? this.app.orders : [];
                        if (previous.length) {
                            this.updateOrdersTableSmart(previous, tbody);
                        } else {
                            tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">No orders available</td></tr>';
                        }
                    } else {
                        tbody.innerHTML = `<tr><td colspan="11" class="text-center text-danger">${errorText}</td></tr>`;
                    }
                }
                return true;
            }

            const meta = message?.meta || {};
            const normalizedStartDate = typeof meta.start_date === 'string' ? meta.start_date : null;
            if (normalizedStartDate !== this.lastOrdersStartDate) {
                this.ordersById = new Map();
                this.lastOrdersStartDate = normalizedStartDate;
            } else if (!this.ordersById) {
                this.ordersById = new Map();
            }

            const orders = Array.isArray(message?.orders) ? message.orders : [];

            const tbody = document.querySelector('#ordersTable tbody');
            if (!tbody) return true;

            const sortedOrders = orders
                .filter(order => order && order.order_id !== 0)
                .sort((a, b) => {
                    const at = a.submit_time ? new Date(a.submit_time).getTime() : 0;
                    const bt = b.submit_time ? new Date(b.submit_time).getTime() : 0;
                    if (bt !== at) return bt - at;
                    const aid = Number(a.order_id) || 0;
                    const bid = Number(b.order_id) || 0;
                    return bid - aid;
                });
            this.orders = sortedOrders;

            sortedOrders.forEach(order => {
                if (!order || !order.order_id) return;
                this.ordersById.set(order.order_id.toString(), order);
            });

            if (this.app) {
                const aggregated = Array.from(this.ordersById.values()).sort((a, b) => {
                    const at = a.submit_time ? new Date(a.submit_time).getTime() : 0;
                    const bt = b.submit_time ? new Date(b.submit_time).getTime() : 0;
                    if (bt !== at) return bt - at;
                    const aid = Number(a.order_id) || 0;
                    const bid = Number(b.order_id) || 0;
                    return bid - aid;
                });
                this.app.orders = aggregated;
            }
            this.applyPaginationMeta(meta, sortedOrders.length);
            this.updateOrdersTableSmart(sortedOrders, tbody);
            this.updatePaginationControls();
        } catch (error) {
            console.error('Failed to process orders_snapshot:', error);
        }
        return true;
    }

    setupPaginationControls() {
        const prevBtn = document.getElementById('ordersPrevPageBtn');
        const nextBtn = document.getElementById('ordersNextPageBtn');
        if (prevBtn) {
            prevBtn.addEventListener('click', () => {
                if (this.pagination?.hasPrev) {
                    this.goToPage((this.pagination.page || 1) - 1);
                }
            });
        }
        if (nextBtn) {
            nextBtn.addEventListener('click', () => {
                if (this.pagination?.hasNext) {
                    this.goToPage((this.pagination.page || 1) + 1);
                }
            });
        }
        this.updatePaginationControls();
    }

    applyPaginationMeta(meta = {}, receivedCount = 0) {
        if (!this.pagination) {
            this.pagination = {
                page: 1,
                pageSize: 10,
                total: 0,
                totalPages: 1,
                hasNext: false,
                hasPrev: false,
            };
        }
        const limit = Number(meta.page_size ?? meta.limit ?? this.pagination.pageSize ?? 10) || 10;
        const page = Math.max(1, Number(meta.page ?? this.pagination.page ?? 1));
        const total = Number.isFinite(Number(meta.total)) ? Number(meta.total) : null;
        let totalPages = Number.isFinite(Number(meta.total_pages)) ? Number(meta.total_pages) : null;
        if (!totalPages && total !== null && limit > 0) {
            totalPages = Math.max(1, Math.ceil(total / limit));
        }
        const hasNext = typeof meta.has_next === 'boolean'
            ? meta.has_next
            : (total !== null ? page < Math.max(totalPages || 1, 1) : receivedCount >= limit);
        const hasPrev = typeof meta.has_prev === 'boolean' ? meta.has_prev : page > 1;
        const derivedTotal = total !== null
            ? total
            : (!hasNext ? Math.max(0, (page - 1) * limit + receivedCount) : this.pagination.total || 0);

        this.pagination = {
            page,
            pageSize: limit,
            total: derivedTotal,
            totalPages: totalPages || Math.max(1, hasNext ? page + 1 : page),
            hasNext,
            hasPrev,
        };
    }

    updatePaginationControls() {
        const infoEl = document.getElementById('ordersPaginationInfo');
        const prevBtn = document.getElementById('ordersPrevPageBtn');
        const nextBtn = document.getElementById('ordersNextPageBtn');
        const page = this.pagination?.page || 1;
        const totalPages = this.pagination?.totalPages || 1;
        const total = this.pagination?.total ?? null;
        const pageSize = this.pagination?.pageSize || 10;
        const currentCount = Array.isArray(this.orders) ? this.orders.length : 0;
        const rangeStart = currentCount === 0 ? 0 : (page - 1) * pageSize + 1;
        const rangeEnd = (page - 1) * pageSize + currentCount;
        if (infoEl) {
            if (total !== null && Number.isFinite(total)) {
                infoEl.textContent = `第 ${page} / ${Math.max(totalPages, 1)} 页 · 第 ${rangeStart || 0}-${rangeEnd || 0} 条 · 共 ${total} 条`;
            } else {
                infoEl.textContent = `第 ${page} 页 · 第 ${rangeStart || 0}-${rangeEnd || 0} 条`;
            }
        }
        if (prevBtn) {
            prevBtn.disabled = !this.pagination?.hasPrev;
        }
        if (nextBtn) {
            nextBtn.disabled = !this.pagination?.hasNext;
        }
    }

    goToPage(page, options = {}) {
        const target = Math.max(1, Number(page) || 1);
        this.pagination.page = target;
        this.loadOrders({
            ...options,
            page: target,
            limit: this.pagination.pageSize,
            force: true,
        });
    }

    /**
     * 智能更新订单表格
     */
    updateOrdersTableSmart(newOrders, tbody) {
        console.log('🔄 updateOrdersTableSmart called with orders:', newOrders.length);
        
        if (!tbody) {
            console.error('❌ tbody is null in updateOrdersTableSmart');
            return;
        }
        
        // 先清理占位行与异常节点，避免残留“Loading orders...”或孤立文本节点导致的错位
        // 移除包含“Loading”或“No orders found”的占位行
        Array.from(tbody.querySelectorAll('tr')).forEach(row => {
            const cells = row.querySelectorAll('td');
            if (cells.length > 0) {
                const firstText = cells[0].textContent || '';
                if (firstText.includes('Loading') || firstText.includes('No orders found')) {
                    row.remove();
                }
            }
        });
        // 移除非 <tr> 的子节点（如意外出现的文本节点，可能造成“MNQMNQ...”串联显示）
        Array.from(tbody.childNodes).forEach(node => {
            if (node.nodeType !== 1 || node.tagName !== 'TR') {
                node.parentNode && node.parentNode.removeChild(node);
            }
        });

        // 异常检测：若仍存在文本节点、空行、或行数异常，则回退为全量重建
        const hasNonElementNodes = Array.from(tbody.childNodes).some(n => n.nodeType !== 1);
        const rowsAfterClean = Array.from(tbody.querySelectorAll('tr'));
        const hasEmptyRow = rowsAfterClean.some(r => r.querySelectorAll('td').length === 0);
        const rowCountAnomaly = rowsAfterClean.length > (newOrders.length + 1);
        if (hasNonElementNodes || hasEmptyRow || rowCountAnomaly) {
            console.warn('⚠️ Detected anomaly in orders tbody, performing full rebuild. Details:', {
                hasNonElementNodes, hasEmptyRow, rowCountAnomaly, currentRows: rowsAfterClean.length, newOrders: newOrders.length
            });
            const frag = document.createDocumentFragment();
            for (const order of newOrders) {
                const tr = this.generateOrderRowHtml(order);
                if (tr && tr.tagName === 'TR') frag.appendChild(tr);
            }
            tbody.innerHTML = '';
            tbody.appendChild(frag);
            console.log('✅ Orders table rebuilt');
            return;
        }
        
        // 如果没有订单，显示空状态
        if (newOrders.length === 0) {
            tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">No orders found</td></tr>';
            return;
        }
        
        // 获取当前表格中的所有行
        const existingRows = Array.from(tbody.querySelectorAll('tr'));
        const existingOrderIds = new Set();
        
        // 收集现有的订单ID
        existingRows.forEach(row => {
            const cells = row.querySelectorAll('td');
            if (cells.length > 0) {
                const orderId = cells[0].textContent.trim();
                if (orderId && orderId !== 'No orders found' && !orderId.includes('Loading')) {
                    existingOrderIds.add(orderId);
                }
            }
        });
        
        // 处理每个新订单
        newOrders.forEach((order, index) => {
            const orderId = order.order_id.toString();
            const existingRow = this.findOrderRow(tbody, orderId);
            const newRowContent = this.generateOrderRowHtml(order);
            
            if (existingRow) {
                // 检查内容是否有变化
                if (!this.isRowContentEqual(existingRow, newRowContent)) {
                    console.log(`🔄 Updating row for order ${orderId}`);
                    existingRow.replaceWith(newRowContent);
                }
                existingOrderIds.delete(orderId);
            } else {
                // 新订单，需要插入
                // console.log(`➕ Adding new row for order ${orderId}`);
                const insertPosition = this.findInsertPosition(tbody, index);
                if (insertPosition) {
                    tbody.insertBefore(newRowContent, insertPosition);
                } else {
                    tbody.appendChild(newRowContent);
                }
            }
        });
        
        // 删除不再存在的订单行
        existingOrderIds.forEach(orderId => {
            const rowToRemove = this.findOrderRow(tbody, orderId);
            if (rowToRemove) {
                console.log(`🗑️ Removing row for order ${orderId}`);
                rowToRemove.remove();
            }
        });
        
        console.log('✅ updateOrdersTableSmart completed');
    }

    /**
     * 查找指定订单ID的行
     */
    findOrderRow(tbody, orderId) {
        const rows = tbody.querySelectorAll('tr');
        for (let row of rows) {
            const cells = row.querySelectorAll('td');
            if (cells.length > 0 && cells[0].textContent.trim() === orderId) {
                return row;
            }
        }
        return null;
    }

    /**
     * 比较两行内容是否相等
     */
    isRowContentEqual(row1, row2) {
        const cells1 = row1.querySelectorAll('td');
        const cells2 = row2.querySelectorAll('td');
        
        if (cells1.length !== cells2.length) return false;
        
        // 专用于提取货币文本中的数值。如 "$1,234.56" -> 1234.56
        const parseCurrency = (el) => {
            if (!el) return NaN;
            const txt = (el.textContent || '').replace(/[$,\s]/g, '');
            const n = Number(txt);
            return isFinite(n) ? n : NaN;
        };

        for (let i = 0; i < cells1.length; i++) {
            const isActionsCol = (i === cells1.length - 1);
            const isPnlCol = (i === 7);        // 列顺序：0-id,1-sym,2-act,3-qty,4-price,5-status,6-created,7-pnl,8-comm,9-source,10-ops
            const isCommissionCol = (i === 8);

            if (isActionsCol) {
                // 操作列：比较按钮数量与类型（通过 title），忽略禁用状态、图标闪烁等差异
                const buttons1 = cells1[i].querySelectorAll('button');
                const buttons2 = cells2[i].querySelectorAll('button');
                if (buttons1.length !== buttons2.length) return false;
                for (let j = 0; j < buttons1.length; j++) {
                    const t1 = buttons1[j].getAttribute('title') || '';
                    const t2 = buttons2[j].getAttribute('title') || '';
                    if (t1 !== t2) return false;
                }
                continue;
            }
            
            if (isPnlCol) {
                // PnL 列：除内容外，还需要比较单元格 class（盈亏颜色），并尽量按数值比较
                const n1 = parseCurrency(cells1[i]);
                const n2 = parseCurrency(cells2[i]);
                if (!(isNaN(n1) && isNaN(n2)) && n1 !== n2) return false;
                if ((cells1[i].className || '').trim() !== (cells2[i].className || '').trim()) return false;
                // 同时兜底比较 innerHTML，避免存在附加节点时漏检
                if (cells1[i].innerHTML.trim() !== cells2[i].innerHTML.trim()) return false;
                continue;
            }

            if (isCommissionCol) {
                // 佣金列：按数值与文本双重比较
                const n1 = parseCurrency(cells1[i]);
                const n2 = parseCurrency(cells2[i]);
                if (!(isNaN(n1) && isNaN(n2)) && n1 !== n2) return false;
                if (cells1[i].innerHTML.trim() !== cells2[i].innerHTML.trim()) return false;
                continue;
            }

            // 默认：比较 innerHTML（适用于状态列内的 <span class="badge ..."> 等场景）
            if (cells1[i].innerHTML.trim() !== cells2[i].innerHTML.trim()) {
                return false;
            }
        }
        
        return true;
    }

    /**
     * 找到新行的插入位置
     */
    findInsertPosition(tbody, targetIndex) {
        const rows = tbody.querySelectorAll('tr');
        if (targetIndex >= rows.length) {
            return null; // 插入到末尾
        }
        return rows[targetIndex];
    }

    /**
     * 生成订单行的HTML
     */
    formatOrderTimestamp(rawValue) {
        const escapeHtml = (value) => {
            if (this.app && typeof this.app.escapeHtml === 'function') {
                return this.app.escapeHtml(value);
            }
            if (value === null || value === undefined) return '';
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        };

        if (rawValue === null || rawValue === undefined || rawValue === '') {
            return { display: '—', title: '', raw: '' };
        }

        let date = null;
        if (rawValue instanceof Date) {
            date = rawValue;
        } else if (typeof rawValue === 'number' && Number.isFinite(rawValue)) {
            date = new Date(rawValue);
        } else {
            const str = String(rawValue).trim();
            if (!str) {
                return { display: '—', title: '', raw: '' };
            }
            date = new Date(str);
            if (Number.isNaN(date.getTime()) && /^\d+$/.test(str)) {
                const numeric = Number(str);
                if (Number.isFinite(numeric)) {
                    date = new Date(numeric);
                }
            }
        }

        if (date instanceof Date && !Number.isNaN(date.getTime())) {
            const localeString = date.toLocaleString('zh-CN');
            const isoString = date.toISOString();
            return {
                display: escapeHtml(localeString),
                title: escapeHtml(isoString),
                raw: isoString,
            };
        }

        const fallback = escapeHtml(String(rawValue));
        return {
            display: fallback || '—',
            title: fallback,
            raw: String(rawValue),
        };
    }

    editOrder(orderId, symbol, action, quantity, orderType, limitPrice) {
        const symbolInput = document.getElementById('orderSymbol');
        const actionSelect = document.getElementById('orderAction');
        const quantityInput = document.getElementById('orderQuantity');
        const typeSelect = document.getElementById('orderType');
        if (!symbolInput || !actionSelect || !quantityInput || !typeSelect) {
            this.app?.showAlert?.('danger', '订单表单元素缺失，无法编辑订单');
            return;
        }

        symbolInput.value = symbol;
        actionSelect.value = action;
        quantityInput.value = quantity;

        let normalizedType;
        if (orderType === 'LMT') {
            normalizedType = 'LIMIT';
        } else if (orderType === 'MKT') {
            normalizedType = 'MARKET';
        } else {
            normalizedType = orderType;
        }
        typeSelect.value = normalizedType;

        if (typeof this.app.fetchCurrentPrice === 'function') {
            this.app.fetchCurrentPrice(symbol);
        }

        setTimeout(() => {
            const priceInput = document.getElementById('orderPrice');
            if (!priceInput) return;
            if ((normalizedType === 'LIMIT') && limitPrice) {
                priceInput.value = limitPrice;
                priceInput.disabled = false;
            } else if (normalizedType === 'LIMIT') {
                priceInput.disabled = false;
                if (!priceInput.value) {
                    priceInput.value = '';
                }
            }
            if (priceInput.value) {
                priceInput.dispatchEvent(new Event('input'));
            }
        }, 100);

        if (typeof this.app.setupOrderFormListeners === 'function') {
            this.app.setupOrderFormListeners();
        }
        const modalEl = document.getElementById('orderModal');
        if (modalEl) {
            const modal = new bootstrap.Modal(modalEl);
            modal.show();
        }

        this.app.editingOrderId = orderId;
        this.app.showAlert('info', `正在编辑订单 ${orderId}，请修改后重新提交`);
    }

    generateOrderRowHtml(order) {
        // 计算价格显示
        let priceDisplay = '';
        if (order.order_type === 'MKT') {
            priceDisplay = order.avg_fill_price && order.avg_fill_price > 0 ?
                `$${this.app.formatNumber(order.avg_fill_price)}` : 'Market';
        } else {
            const limitPrice = order.limit_price || order.price;
            priceDisplay = limitPrice ? `$${this.app.formatNumber(limitPrice)}` : 'N/A';
        }
        
        let displayStatus = order.status;
        
        // 判断是否可以编辑/取消
        const canEdit = order.status === 'Submitted' || order.status === 'PreSubmitted';
        const canCancel = order.status === 'Submitted' || order.status === 'PreSubmitted';
        
        const sourceDisplay = this.formatOrderSource(order);
        
        // 处理notes字段，转义HTML特殊字符
        const notesText = (order.notes || '').toString().replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        
        // 规范化并转义显示字段，避免意外拼接
        const symbolStr = (order.symbol || '').toString();
        const actionStr = (order.action || '').toString();
        const orderTypeStr = (order.order_type || '').toString();
        const escapedSymbol = symbolStr.replace(/'/g, '&#39;').replace(/"/g, '&quot;');
        const escapedAction = actionStr.replace(/'/g, '&#39;').replace(/"/g, '&quot;');
        const escapedOrderType = orderTypeStr.replace(/'/g, '&#39;').replace(/"/g, '&quot;');
        
        const detailsButton = `<button class="btn btn-sm btn-info me-1" onclick="app.openOrderDetails('${order.order_id}')" title="Details"><i class="fas fa-info-circle"></i></button>`;

        const createdAtInfo = this.formatOrderTimestamp(order.submit_time || order.created_at);
        const createdTitleAttr = createdAtInfo.title ? ` title="${createdAtInfo.title}"` : '';

        const editButton = canEdit ?
            `<button class="btn btn-sm btn-primary me-1" onclick="app.editOrder(${order.order_id}, '${escapedSymbol}', '${escapedAction}', ${order.quantity}, '${escapedOrderType}', ${(order.limit_price || order.price) || 0})" title="Edit"><i class="fas fa-edit"></i></button>` : '';

        const syncButton = (order.order_type === 'LMT' && order.status === 'Submitted') ?
            `<button class="btn btn-sm btn-outline-primary me-1" onclick="app.orderManager.syncLimitOrder('${order.order_id}', null, event)" title="同步订单状态"><i class="fas fa-sync-alt"></i></button>` : '';
        
        const cancelButton = canCancel ? 
            `<button class="btn btn-sm btn-danger" onclick="app.orderManager.cancelOrder(${order.order_id}, this)" title="Cancel"><i class="fas fa-times"></i></button>` : '';
        
        // 使用模板构建一个实际的tr元素，避免返回非元素节点
        const trElement = document.createElement('tr');
        trElement.setAttribute('data-notes', notesText);
        if (createdAtInfo.raw) {
            trElement.setAttribute('data-created-at', createdAtInfo.raw);
        }
        trElement.onmouseenter = (event) => this.app.showNotesTooltip(event, trElement);
        trElement.onmouseleave = () => this.app.hideNotesTooltip();

        trElement.innerHTML = `
            <td>${order.order_id}</td>
            <td><a href="#" class="text-decoration-none" onclick="app.openMonitorModal('${escapedSymbol}'); return false;">${this.app.escapeHtml ? this.app.escapeHtml(symbolStr) : symbolStr}</a></td>
            <td>${this.app.escapeHtml ? this.app.escapeHtml(actionStr) : escapedAction}</td>
            <td>${order.quantity}</td>
            <td>${priceDisplay}</td>
            <td><span class="badge bg-${this.app.getStatusColor(displayStatus)}">${displayStatus}</span></td>
            <td${createdTitleAttr}>${createdAtInfo.display}</td>
            <td class="${(order.pnl ?? 0) > 0 ? 'text-success' : ((order.pnl ?? 0) < 0 ? 'text-danger' : 'text-muted')}">$${this.app.formatNumber(order.pnl ?? 0)}</td>
            <td>$${this.app.formatNumber(order.commission ?? 0)}</td>
            <td>${sourceDisplay}</td>
            <td>${detailsButton}${editButton}${syncButton}${cancelButton}</td>
        `;
        
        // 返回标准化后的TR元素
        return trElement;
    }

    formatOrderSource(order) {
        const escapeHtml = (value) => {
            if (value === null || value === undefined) return '';
            if (this.app && typeof this.app.escapeHtml === 'function') {
                return this.app.escapeHtml(value);
            }
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        };

        const rawSource = order?.order_source;
        const sources = Array.isArray(rawSource)
            ? rawSource.filter(src => src !== null && src !== undefined)
            : (rawSource ? [rawSource] : []);

        if (!sources.length) {
            return '—';
        }

        const displays = [];
        sources.forEach((sourceItem) => {
            if (sourceItem === null || sourceItem === undefined) return;
            const text = String(sourceItem).trim();
            if (!text) return;

            const lower = text.toLowerCase();
            if (lower === 'manual' || lower.startsWith('manual_') || text === '人工') {
                displays.push(escapeHtml(text));
                return;
            }

            if (lower === 'sl') {
                displays.push('<span class="badge bg-danger">止损</span>');
                return;
            }

            if (lower === 'tp') {
                displays.push('<span class="badge bg-success">止盈</span>');
                return;
            }

            if (lower === 'auto' || text === '机器人') {
                displays.push('<span class="badge bg-secondary">自动</span>');
                return;
            }

            if (lower === 'strategy' || lower.startsWith('strategy:')) {
                let strategyName = '';
                if (lower.startsWith('strategy:')) {
                    strategyName = text.slice(text.indexOf(':') + 1).trim();
                } else {
                    strategyName = (order?.strategy_name || order?.strategy || '').toString().trim();
                }
                const safeName = strategyName ? escapeHtml(strategyName) : '';
                const suffix = safeName ? `<span class="ms-1 text-nowrap">${safeName}</span>` : '';
                displays.push(`<span class="badge bg-info">策略</span>${suffix}`);
                return;
            }

            displays.push(escapeHtml(text));
        });

        if (!displays.length) {
            return '—';
        }

        return displays.join('<span class="text-muted mx-1">/</span>');
    }

    /**
     * 下单
     */
    async placeOrder() {
        try {
            const form = document.getElementById('orderForm');
            const formData = new FormData(form);
            
            // 检查是否是编辑模式
            const isEdit = document.getElementById('editOrderId').value;
            
            // 获取表单数据
            const orderData = {
                symbol: formData.get('symbol').toUpperCase(),
                action: formData.get('action'),
                quantity: parseInt(formData.get('quantity')),
                order_type: formData.get('order_type'),
                price: formData.get('price') ? parseFloat(formData.get('price')) : null,
                notes: formData.get('notes') || ''
            };
            
            // 证券类型检测
            const secType = this.app.detectSecurityType(orderData.symbol);
            if (secType) {
                orderData.sec_type = secType;
            }
            
            // 添加风险规则
            const stopLoss = formData.get('stop_loss');
            const takeProfit = formData.get('take_profit');
            const maxTimeSpan = formData.get('max_time_span');
            
            if (stopLoss || takeProfit || maxTimeSpan) {
                orderData.risk_rule = {
                    stop_loss_price: stopLoss ? parseFloat(stopLoss) : null,
                    take_profit_price: takeProfit ? parseFloat(takeProfit) : null,
                    max_time_span: maxTimeSpan || null
                };
            }
            
            // 参数验证
            if (!orderData.symbol || !orderData.action || !orderData.quantity) {
                this.app.showAlert('error', 'Please fill in all required fields');
                return;
            }
            
            if ((orderData.order_type === 'LMT' || orderData.order_type === 'LIMIT') && (!orderData.price || orderData.price <= 0)) {
                this.app.showAlert('error', 'Limit orders require a valid price');
                return;
            }
            
            // 检查IB连接
            if (!this.app.ibkrManager.isConnected) {
                this.app.showAlert('error', '未连接 IBKR，请先点击右上角“连接”或在“连接设置”中配置后连接。');
                // 打开连接设置弹窗，帮助用户快速连接
                const modalEl = document.getElementById('connectionModal');
                if (modalEl) {
                    const modal = new bootstrap.Modal(modalEl);
                    modal.show();
                }
                // 滚动到页面顶部，确保提示可见
                window.scrollTo({ top: 0, behavior: 'smooth' });
                return;
            }
            
            // 发送订单
            let response;
            if (isEdit) {
                orderData.order_id = parseInt(isEdit);
                response = await this.app.apiCall('/orders/modify', {
                    method: 'POST',
                    body: JSON.stringify(orderData)
                });
            } else {
                response = await this.app.apiCall('/place_order', {
                    method: 'POST',
                    body: JSON.stringify(orderData)
                });
            }
            
            if (response.success) {
                this.app.showAlert('success', isEdit ? 'Order modified successfully' : 'Order placed successfully');
                
                // 缓存上一次下单的关键字段，便于下次填充
                try {
                    localStorage.setItem('lastOrderSymbol', orderData.symbol || '');
                    localStorage.setItem('lastOrderAction', orderData.action || '');
                    localStorage.setItem('lastOrderQuantity', String(orderData.quantity ?? ''));
                    localStorage.setItem('lastOrderType', orderData.order_type || '');
                } catch (e) {
                    console.warn('Failed to save last order fields to localStorage:', e);
                }
                
                // 重置表单
                form.reset();
                document.getElementById('editOrderId').value = '';
                document.getElementById('orderModalLabel').textContent = 'Place Order';
                
                // 关闭模态框
                const modal = bootstrap.Modal.getInstance(document.getElementById('orderModal'));
                if (modal) {
                    modal.hide();
                }
                
                // 刷新订单列表
                this.loadOrders();
            } else {
                this.app.showAlert('error', response.message || 'Failed to place order');
            }
        } catch (error) {
            console.error('Error placing order:', error);
            this.app.showAlert('error', 'Failed to place order: ' + error.message);
        }
    }

    /**
     * 取消所有订单
     */
    async cancelAllOrders() {
        if (!confirm('Are you sure you want to cancel all orders?')) {
            return;
        }
        
        try {
            let response;
            try {
                response = await this.app.apiCall('/cancel_all_orders', { method: 'DELETE' });
            } catch (e) {
                // Fallback: older endpoints
                try {
                    response = await this.app.apiCall('/cancel_all_orders', { method: 'POST' });
                } catch (e2) {
                    response = await this.app.apiCall('/orders/cancel_all', { method: 'POST' });
                }
            }
            
            if (response.success) {
                this.app.showAlert('success', 'All orders cancelled successfully');
                this.loadOrders();
            } else {
                this.app.showAlert('error', response.message || 'Failed to cancel orders');
            }
        } catch (error) {
            console.error('Error cancelling orders:', error);
            this.app.showAlert('error', 'Failed to cancel orders');
        }
    }

    /**
     * 同步限价单状态
     */
    async syncLimitOrder(orderId, button, event) {
        if (event) {
            event.stopPropagation();
        }
        
        try {
            // 显示加载状态
            if (button) {
                const originalHtml = button.innerHTML;
                button.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                button.disabled = true;
                
                setTimeout(() => {
                    button.innerHTML = originalHtml;
                    button.disabled = false;
                }, 3000);
            }
            
            const response = await this.app.apiCall(`/orders/${orderId}/sync`, {
                method: 'POST'
            });
            
            if (response.success) {
                this.app.showAlert('success', 'Order status synced successfully');
                
                // 如果有监控弹窗打开，也刷新它
                const monitorModal = document.getElementById('monitorModal');
                if (monitorModal && monitorModal.classList.contains('show')) {
                    const symbol = monitorModal.querySelector('.modal-title').textContent.split(' ')[0];
                    this.app.loadMonitorData(symbol);
                }
                
                // 刷新主订单列表
                this.loadOrders();
            } else {
                this.app.showAlert('error', response.message || 'Failed to sync order status');
            }
        } catch (error) {
            console.error('Error syncing order:', error);
            this.app.showAlert('error', 'Failed to sync order status');
        }
    }

    /**
     * 取消订单
     */
    async cancelOrder(orderId, button) {
        // 防止重复点击
        if (button && button.disabled) {
            return;
        }
        
        try {
            // 显示加载状态
            if (button) {
                const originalHtml = button.innerHTML;
                button.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                button.disabled = true;
                
                // 3秒后恢复按钮状态（防止卡住）
                setTimeout(() => {
                    button.innerHTML = originalHtml;
                    button.disabled = false;
                }, 3000);
            }
            
            let response;
            try {
                response = await this.app.apiCall(`/cancel_order/${orderId}`, {
                    method: 'DELETE'
                });
            } catch (e) {
                // fallback to POST endpoint
                response = await this.app.apiCall(`/cancel_order`, {
                    method: 'POST',
                    body: JSON.stringify({ order_id: orderId })
                });
            }
            
            if (response.success) {
                this.app.showAlert('success', 'Order cancelled successfully');
                this.loadOrders();
            } else {
                this.app.showAlert('error', response.message || 'Failed to cancel order');
            }
        } catch (error) {
            console.error('Error cancelling order:', error);
            this.app.showAlert('error', 'Failed to cancel order');
        }
    }

    /**
     * 渲染内部订单表格
     */
    renderInternalOrders(name, orders = null) {
        const tbody = document.querySelector('#internalOrdersTable tbody');
        if (!tbody) return;

        const strategyName = name || this.app.currentStrategyDetails;
        const sourceOrders = Array.isArray(orders) ? orders : this.app.internalStrategyOrders;
        const data = Array.isArray(sourceOrders) ? sourceOrders : [];
        if (!strategyName || data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">No strategy orders</td></tr>';
            return;
        }

        const frag = document.createDocumentFragment();
        data.forEach(order => {
            if (!order) return;
            const createdTimestamp = order.created_at
                || order.submit_time
                || order.timestamp
                || null;
            const normalized = {
                order_id: order.order_id,
                symbol: order.symbol,
                action: order.action,
                quantity: order.quantity,
                order_type: order.order_type || 'MKT',
                limit_price: order.price ?? order.limit_price ?? null,
                price: order.price ?? null,
                avg_fill_price: order.fill_price ?? order.price ?? 0,
                status: order.status || 'Submitted',
                pnl: Number(order.pnl ?? 0),
                commission: Number(order.commission ?? 0),
                order_source: order.order_source || 'strategy',
                notes: order.notes || '',
                order_source_display: order.order_source_display,
                created_at: createdTimestamp,
                submit_time: order.submit_time
                    || order.created_at
                    || order.timestamp
                    || createdTimestamp,
            };
            const row = this.generateOrderRowHtml(normalized);
            if (row) {
                frag.appendChild(row);
            }
        });

        if (!frag.childNodes.length) {
            tbody.innerHTML = '<tr><td colspan="11" class="text-center text-muted">No strategy orders</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        tbody.appendChild(frag);
    }

    renderExecutionsTable() {
        const tbody = document.querySelector('#executionsTable tbody');
        if (!tbody) return;

        const theadRow = document.querySelector('#executionsTable thead tr');
        if (theadRow) {
            theadRow.innerHTML = `
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

        tbody.innerHTML = '';

        const pagination = this.app.executionsPagination || {};
        const currentPage = pagination.currentPage || 1;
        const pageSize = pagination.pageSize || this.app.strategyExecutionsDefaultLimit || 20;
        const allExecutions = Array.isArray(pagination.allExecutions) ? pagination.allExecutions : [];
        const start = (currentPage - 1) * pageSize;
        const end = start + pageSize;
        const pageItems = allExecutions.slice(start, end);

        pageItems.forEach(exe => {
            const tr = document.createElement('tr');
            const createdAt = exe.created_at || exe.execution_time || null;
            let createdDisplay = '-';
            try {
                createdDisplay = createdAt ? new Date(createdAt).toLocaleString() : '-';
            } catch (_) {}
            const commission = exe.commission != null ? Number(exe.commission).toFixed(2) : '-';
            const pnl = exe.pnl != null ? Number(exe.pnl).toFixed(2) : '-';
            const pnlClass = exe.pnl != null
                ? (Number(exe.pnl) > 0 ? 'text-success' : Number(exe.pnl) < 0 ? 'text-danger' : '')
                : '';
            tr.innerHTML = `
              <td>${exe.id ?? exe.order_id ?? '-'}</td>
              <td>${exe.symbol ?? '-'}</td>
              <td>${createdDisplay}</td>
              <td>${exe.action ?? '-'}</td>
              <td>${exe.quantity ?? '-'}</td>
              <td>${exe.price != null ? exe.price : '-'}</td>
              <td><span class="badge bg-${this.app.getStatusColor(exe.status || '')}">${exe.status}</span></td>
              <td class="${pnlClass}">${pnl}</td>
              <td>${commission}</td>
            `;
            tbody.appendChild(tr);
        });
    }

    renderExecutionsPagination() {
        const ul = document.getElementById('executionsPagination');
        if (!ul) return;

        const pagination = this.app.executionsPagination || {};
        const currentPage = pagination.currentPage || 1;
        const pageSize = pagination.pageSize || this.app.strategyExecutionsDefaultLimit || 20;
        const totalItems = pagination.totalItems || 0;
        const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));

        const makeLi = (label, page, disabled = false, active = false) => {
            const li = document.createElement('li');
            li.className = `page-item ${disabled ? 'disabled' : ''} ${active ? 'active' : ''}`;
            const a = document.createElement('a');
            a.className = 'page-link';
            a.href = '#';
            a.textContent = label;
            a.onclick = (e) => {
                e.preventDefault();
                if (disabled || page === currentPage) return;
                pagination.currentPage = page;
                this.renderExecutionsTable();
                this.renderExecutionsPagination();
            };
            li.appendChild(a);
            return li;
        };

        ul.innerHTML = '';
        ul.appendChild(makeLi('«', Math.max(1, currentPage - 1), currentPage === 1));

        const windowSize = 5;
        const start = Math.max(1, currentPage - Math.floor(windowSize / 2));
        const end = Math.min(totalPages, start + windowSize - 1);
        for (let p = start; p <= end; p++) {
            ul.appendChild(makeLi(String(p), p, false, p === currentPage));
        }

        ul.appendChild(makeLi('»', Math.min(totalPages, currentPage + 1), currentPage === totalPages));
    }

    exportExecutionsCsv(name) {
        const pagination = this.app.executionsPagination || {};
        const allExecutions = Array.isArray(pagination.allExecutions) ? pagination.allExecutions : [];
        const rows = [
            ['ID','Symbol','Created At','Action','Qty','Price','Status','PnL','Commission']
        ];

        allExecutions.forEach(exe => {
            const createdAt = exe.created_at || exe.execution_time || null;
            const createdIso = (() => {
                if (!createdAt) return '';
                try {
                    return new Date(createdAt).toISOString();
                } catch (_) {
                    return createdAt;
                }
            })();
            rows.push([
                exe.id ?? '',
                exe.symbol ?? '',
                createdIso,
                exe.action ?? '',
                exe.quantity ?? '',
                exe.price ?? '',
                exe.status ?? '',
                exe.pnl ?? '',
                exe.commission ?? ''
            ]);
        });

        const csv = rows.map(r => r.map(v => {
            if (typeof v === 'string' && v.includes(',')) {
                return `"${v.replace(/"/g,'""')}"`;
            }
            return v;
        }).join(',')).join('\n');
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const strategyName = document.getElementById('detailsStrategyName')?.textContent || name || 'strategy';
        a.download = `${strategyName}_executions.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    async refreshPositionSuggestion() {
        const symbolRaw = document.getElementById('orderSymbol')?.value || '';
        const symbol = symbolRaw.trim().toUpperCase();
        const action = document.getElementById('orderAction')?.value;
        const priceInput = document.getElementById('orderPrice')?.value?.trim();
        const price = parseFloat(priceInput) || 0;
        const stopLoss = parseFloat(document.getElementById('stopLossPrice')?.value) || 0;

        if (!symbol || !action) {
            this.clearPositionSuggestion();
            return;
        }

        let effectivePrice = price;
        if (!effectivePrice || effectivePrice <= 0) {
            try {
                const position = this.app.dataManager?.getPosition?.(symbol);
                if (position && position.current_price > 0) {
                    effectivePrice = position.current_price;
                }
            } catch (error) {
                console.warn('Failed to read position for suggestion:', error);
            }

            if (!effectivePrice || effectivePrice <= 0) {
                this.app.showAlert('warning', '请输入价格或确保该股票有当前市场价格');
                this.clearPositionSuggestion();
                return;
            }
        }

        let stopDistance = 0;
        if (stopLoss > 0) {
            if (action === 'BUY') {
                stopDistance = Math.abs(effectivePrice - stopLoss);
            } else if (action === 'SELL') {
                stopDistance = Math.abs(stopLoss - effectivePrice);
            }
        } else {
            if (symbol.includes('ES') || symbol.includes('NQ') || symbol.includes('YM') || symbol.includes('RTY')) {
                stopDistance = 10;
            } else {
                stopDistance = effectivePrice * 0.02;
            }
        }

        if (stopDistance <= 0) {
            this.clearPositionSuggestion();
            return;
        }

        try {
            const data = await this.app.apiCall('/position-sizing', {
                method: 'POST',
                body: JSON.stringify({
                    symbol,
                    stop_distance: stopDistance,
                }),
            });

            this.updatePositionSuggestionUI(data);
            if (typeof this.app.loadSizingHistory === 'function') {
                this.app.loadSizingHistory();
            }
        } catch (error) {
            console.error('Failed to get position suggestion:', error);
            this.clearPositionSuggestion();
        }
    }

    updatePositionSuggestionUI(data) {
        const suggestedQuantity = document.getElementById('suggestedQuantity');
        const sizingMethod = document.getElementById('sizingMethod');
        const estimatedInvestment = document.getElementById('estimatedInvestment');
        const maxRisk = document.getElementById('maxRisk');
        const stopDistance = document.getElementById('stopDistance');
        const pointValue = document.getElementById('pointValue');
        const scaleTotal = document.getElementById('scaleTotal');
        const scaleVol = document.getElementById('scaleVol');
        const scaleDd = document.getElementById('scaleDd');
        const scaleSig = document.getElementById('scaleSig');
        const scaleExec = document.getElementById('scaleExec');
        const sizingExplanation = document.getElementById('sizingExplanation');

        const contracts = data?.N || 0;
        const explain = data?.explain || {};

        if (suggestedQuantity) suggestedQuantity.textContent = contracts || '--';
        if (sizingMethod) sizingMethod.textContent = 'Position Sizing';
        if (estimatedInvestment) estimatedInvestment.textContent = data?.estimatedInvestment ? `$${data.estimatedInvestment.toFixed(2)}` : '--';
        if (maxRisk) maxRisk.textContent = data?.maxRisk ? `$${data.maxRisk.toFixed(2)}` : '--';
        if (stopDistance) stopDistance.textContent = data?.stopDistance ? data.stopDistance.toFixed(4) : '--';
        if (pointValue) pointValue.textContent = data?.pointValue ? data.pointValue.toFixed(2) : '--';
        if (scaleTotal) scaleTotal.textContent = data?.scaleTotal ? data.scaleTotal.toFixed(4) : '--';
        if (scaleVol) scaleVol.textContent = data?.scaleVol ? data.scaleVol.toFixed(4) : '--';
        if (scaleDd) scaleDd.textContent = data?.scaleDd ? data.scaleDd.toFixed(4) : '--';
        if (scaleSig) scaleSig.textContent = data?.scaleSig ? data.scaleSig.toFixed(4) : '--';
        if (scaleExec) scaleExec.textContent = data?.scaleExec ? data.scaleExec.toFixed(4) : '--';
        if (sizingExplanation) sizingExplanation.textContent = explain || '--';

        this.app.lastSuggestedQuantity = contracts;
    }

    clearPositionSuggestion() {
        const elements = [
            'suggestedQuantity', 'sizingMethod', 'estimatedInvestment', 'maxRisk',
            'stopDistance', 'pointValue', 'scaleTotal', 'scaleVol', 'scaleDd',
            'scaleSig', 'scaleExec', 'sizingExplanation'
        ];
        elements.forEach(id => {
            const element = document.getElementById(id);
            if (element) element.textContent = '--';
        });
        this.app.lastSuggestedQuantity = null;
    }

    updatePositionValueField() {
        const positionMethod = document.getElementById('s0PositionSizing')?.value;
        const positionValueLabel = document.querySelector('label[for="s1PositionValue"]');
        const positionValueInput = document.getElementById('s1PositionValue');
        const positionValueHelp = document.getElementById('s1PositionValue')?.parentNode?.querySelector('.form-text');

        if (!positionValueLabel || !positionValueInput) return;

        switch (positionMethod) {
            case 'percentage':
                positionValueLabel.textContent = 'Position Percentage (%)';
                positionValueInput.placeholder = '例如: 2.5 (表示2.5%)';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '每个仓位占总资金的百分比';
                }
                break;
            case 'fixed':
                positionValueLabel.textContent = 'Fixed Amount ($)';
                positionValueInput.placeholder = '例如: 1000 (表示$1000)';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '每个仓位的固定金额';
                }
                break;
            case 'shares':
                positionValueLabel.textContent = 'Number of Shares';
                positionValueInput.placeholder = '例如: 100 (表示100股)';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '每个仓位的固定股数';
                }
                break;
            default:
                positionValueLabel.textContent = 'Position Value';
                positionValueInput.placeholder = '';
                if (positionValueHelp) {
                    positionValueHelp.textContent = '';
                }
        }
    }

    startOrdersAutoRefresh() {
        if (this.ordersRefreshInterval) {
            clearInterval(this.ordersRefreshInterval);
        }
        this.ordersRefreshInterval = setInterval(() => {
            const ordersTab = document.querySelector('#orders-tab');
            if (ordersTab && ordersTab.classList.contains('active')) {
                console.log('Auto-refreshing orders status...');
                this.loadOrders();
            }
        }, 15000);
        console.log('Orders auto-refresh started (15s interval)');
    }

    stopOrdersAutoRefresh() {
        if (this.ordersRefreshInterval) {
            clearInterval(this.ordersRefreshInterval);
            this.ordersRefreshInterval = null;
            console.log('Orders auto-refresh stopped');
        }
    }

    /**
     * 调试Orders表格状态的函数
     */
    debugOrdersTable() {
        const tbody = document.querySelector('#ordersTable tbody');
        if (!tbody) {
            console.warn('⚠️ Orders 调试：未找到 #ordersTable tbody');
            return;
        }

        // 先统计并清理 tbody 中的非 <tr> 子节点
        const beforeChildNodesCount = tbody.childNodes.length;
        const beforeRowsCount = tbody.rows ? tbody.rows.length : 0;

        const nonTrNodes = [];
        tbody.childNodes.forEach((node) => {
          if (node.nodeType !== 1 || node.tagName !== 'TR') {
            nonTrNodes.push({
              type: node.nodeType,
              tag: node.tagName || 'TEXT',
              preview:
                node.nodeType === 3
                  ? (node.textContent || '').trim().slice(0, 80)
                  : (node.outerHTML || '').slice(0, 120),
            });
          }
        });

        if (nonTrNodes.length > 0) {
          console.warn('⚠️ 检测到非 <tr> 子节点，将立即清理：', nonTrNodes);
          // 立即移除非 <tr> 子节点
          const toRemove = [];
          tbody.childNodes.forEach((node) => {
            if (node.nodeType !== 1 || node.tagName !== 'TR') {
              toRemove.push(node);
            }
          });
          toRemove.forEach((n) => n.remove());
          console.log(`✅ 已移除 ${toRemove.length} 个非 <tr> 子节点`);
        }

        // 行内基本一致性检查
        const trList = Array.from(tbody.querySelectorAll('tr'));
        trList.forEach((tr, i) => {
          const tds = tr.querySelectorAll('td, th');
          if (tds.length !== 11) {
            console.warn(`⚠️ 第 ${i + 1} 行单元格数量异常：`, tds.length, tr.outerHTML.slice(0, 150));
          }
          if (tr.querySelector('div')) {
            console.warn(`⚠️ 第 ${i + 1} 行包含 <div>，请检查模板：`, tr.outerHTML.slice(0, 150));
          }
        });

        const afterRowsCount = tbody.rows ? tbody.rows.length : 0;
        console.log(
          `📊 Orders 调试汇总：childNodes=${beforeChildNodesCount}，rows(before)=${beforeRowsCount}，removed_non_tr=${nonTrNodes.length}，rows(after)=${afterRowsCount}`
        );
      }

      // 监控并防护 #ordersTable tbody，阻止非 <tr> 节点被插入
      attachOrdersTbodyGuard() {
        const tbody = document.querySelector('#ordersTable tbody');
        if (!tbody) return;
      
        if (tbody.__ordersGuardAttached) return;
        tbody.__ordersGuardAttached = true;
      
        try {
          const observer = new MutationObserver((mutations) => {
            mutations.forEach((m) => {
              if (m.addedNodes && m.addedNodes.length) {
                m.addedNodes.forEach((node) => {
                  if (node.nodeType !== 1 || node.tagName !== 'TR') {
                    const preview = node.nodeType === 3
                      ? (node.textContent || '').trim().slice(0, 120)
                      : (node.outerHTML || '').slice(0, 160);
                    console.warn('🛡️ 阻断：检测到向 #ordersTable tbody 插入的非 <tr> 节点，已移除。', { type: node.nodeType, tag: node.tagName || 'TEXT', preview });
                    try { node.remove(); } catch (e) { /* noop */ }
                  }
                });
              }
            });
          });
          observer.observe(tbody, { childList: true });
          tbody.__ordersGuardObserver = observer;
      
          // 局部补丁：拦截 append/appendChild/insertBefore/insertAdjacentHTML 在该 tbody 上的调用
          const patchMethod = (obj, key) => {
            if (!obj[key] || obj[`__orig_${key}`]) return;
            const orig = obj[key].bind(obj);
            obj[`__orig_${key}`] = orig;
            obj[key] = function (...args) {
              // 针对不同 API 的参数形态进行轻量检查
              const rejectNode = (n) => n && (n.nodeType !== 1 || n.tagName !== 'TR');
              let blocked = false;
              if (key === 'append' || key === 'prepend') {
                for (const a of args) {
                  if (typeof a === 'string') {
                    console.warn(`🛡️ 阻断：${key} 传入字符串到 tbody，将被拒绝`, a.slice(0, 100));
                    blocked = true; break;
                  }
                  if (a && rejectNode(a)) { blocked = true; break; }
                }
              } else if (key === 'appendChild' || key === 'insertBefore' || key === 'replaceChild') {
                const candidate = args[0];
                if (rejectNode(candidate)) blocked = true;
              } else if (key === 'insertAdjacentHTML') {
                console.warn('🛡️ 阻断：不允许对 tbody 使用 insertAdjacentHTML');
                blocked = true;
              }
              if (blocked) {
                console.trace(`🔎 调用栈：${key} 到 tbody 被阻断`);
                return null;
              }
              return orig(...args);
            };
          };
      
          patchMethod(tbody, 'append');
          patchMethod(tbody, 'prepend');
          patchMethod(tbody, 'appendChild');
          patchMethod(tbody, 'insertBefore');
          patchMethod(tbody, 'replaceChild');
          patchMethod(tbody, 'insertAdjacentHTML');
      
          console.log('🛡️ 已启用 #ordersTable tbody 守护，防止非 <tr> 节点插入');
        } catch (e) {
          console.warn('⚠️ 启用 tbody 守护失败：', e);
        }
      }

    /**
     * 清除订单缓存
     */
    clearCache() {
        this.ordersCache = {};
    }
}

// 导出OrderManager类
managerNamespace.OrderManager = OrderManager;
})(typeof window !== 'undefined' ? window : globalThis);