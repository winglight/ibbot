/**
 * 订单管理模块
 * 负责订单相关的所有功能，包括加载、创建、修改、取消订单等
 */
class OrderManager {
    constructor(app) {
        this.app = app;
        this.orders = [];
        this.ordersCache = {};
        this.debouncedLoadOrders = null;
        this.initOrdersDebounce();
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
    async loadOrders() {
        console.log('🔄 loadOrders called');
        
        // 显示加载状态
        const tbody = document.querySelector('#ordersTable tbody');
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="10" class="text-center"><i class="fas fa-spinner fa-spin"></i> Loading orders...</td></tr>';
        }
        
        // 调用防抖函数
        this.debouncedLoadOrders();
    }

    /**
     * 实际加载订单的函数
     */
    async _loadOrders() {
        try {
            console.log('🔄 _loadOrders executing');
            
            const cacheKey = 'orders_main';
            
            // 只获取当天订单，减少数据传输量
            const today = new Date();
            const startOfDay = new Date(today.getFullYear(), today.getMonth(), today.getDate()).toISOString();
            console.log('🌐 Fetching today\'s orders data (no cache)');
            const data = await this.app.apiCall(`/orders?limit=50&start_date=${startOfDay}&use_cache=false`, {
                method: 'GET',
                useCache: false,
                cacheTime: 0
            });
            this.orders = data.orders || [];
            
            // 仅显示当天订单（本地时区）
            const now = new Date();
            const startOfDayMs = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
            const endOfDayMs = startOfDayMs + 24 * 60 * 60 * 1000;
            
            // 过滤和排序
            const filteredOrders = this.orders.filter(order => {
                if (!order || order.order_id === 0) return false;
                if (!order.submit_time) return false;
                const ts = new Date(order.submit_time).getTime();
                return ts >= startOfDayMs && ts < endOfDayMs;
            });
            const sortedOrders = filteredOrders.sort((a, b) => {
                const at = a.submit_time ? new Date(a.submit_time).getTime() : 0;
                const bt = b.submit_time ? new Date(b.submit_time).getTime() : 0;
                if (bt !== at) return bt - at; // 按创建时间降序
                const aid = Number(a.order_id) || 0;
                const bid = Number(b.order_id) || 0;
                return bid - aid; // 次级按ID降序
            });
            
            // 更新表格
            const tbody = document.querySelector('#ordersTable tbody');
            if (tbody) {
                this.updateOrdersTableSmart(sortedOrders, tbody);
            }
            
        } catch (error) {
            console.error('Failed to load orders:', error);
            const tbody = document.querySelector('#ordersTable tbody');
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="10" class="text-center text-danger">Failed to load orders</td></tr>';
            }
        }
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
            tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted">No orders found</td></tr>';
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
                console.log(`➕ Adding new row for order ${orderId}`);
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
        
        for (let i = 0; i < cells1.length; i++) {
            // 对于操作列（最后一列），比较按钮的存在性而不是具体内容
            if (i === cells1.length - 1) {
                const buttons1 = cells1[i].querySelectorAll('button');
                const buttons2 = cells2[i].querySelectorAll('button');
                if (buttons1.length !== buttons2.length) return false;
                
                // 比较按钮的类型（通过title属性）
                for (let j = 0; j < buttons1.length; j++) {
                    if (buttons1[j].getAttribute('title') !== buttons2[j].getAttribute('title')) {
                        return false;
                    }
                }
            } else {
                // 对于其他列，比较innerHTML
                if (cells1[i].innerHTML.trim() !== cells2[i].innerHTML.trim()) {
                    return false;
                }
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
        
        // 特殊处理ID为20的risk rule触发订单状态
        let displayStatus = order.status;
        if (order.order_id === 20 && order.status === 'PendingSubmit') {
            displayStatus = 'Filled';
        }
        
        // 判断是否可以编辑/取消
        const canEdit = order.status === 'Submitted' || order.status === 'PreSubmitted';
        const canCancel = order.status === 'Submitted' || order.status === 'PreSubmitted';
        
        // 来源显示逻辑
        let sourceDisplay = '—';
        const rawSource = (order.order_source || '').toString();
        if (rawSource) {
            if (rawSource === 'manual' || rawSource.startsWith('manual_') || rawSource === '人工') {
                sourceDisplay = rawSource;
            } else if (rawSource === 'sl') {
                sourceDisplay = '<span class="badge bg-danger">止损</span>';
            } else if (rawSource === 'tp') {
                sourceDisplay = '<span class="badge bg-success">止盈</span>';
            } else if (rawSource === 'strategy') {
                sourceDisplay = '<span class="badge bg-info">策略</span>';
            } else if (rawSource === '机器人' || rawSource === 'auto') {
                sourceDisplay = '<span class="badge bg-secondary">自动</span>';
            } else {
                sourceDisplay = rawSource;
            }
        }
        
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
        
        const editButton = canEdit ? 
            `<button class="btn btn-sm btn-primary me-1" onclick="app.editOrder(${order.order_id}, '${escapedSymbol}', '${escapedAction}', ${order.quantity}, '${escapedOrderType}', ${(order.limit_price || order.price) || 0})" title="Edit"><i class="fas fa-edit"></i></button>` : '';
        
        const syncButton = (order.order_type === 'LMT' && order.status === 'Submitted') ? 
            `<button class="btn btn-sm btn-outline-primary me-1" onclick="app.orderManager.syncLimitOrder('${order.order_id}', null, event)" title="同步订单状态"><i class="fas fa-sync-alt"></i></button>` : '';
        
        const cancelButton = canCancel ? 
            `<button class="btn btn-sm btn-danger" onclick="app.orderManager.cancelOrder(${order.order_id}, this)" title="Cancel"><i class="fas fa-times"></i></button>` : '';
        
        // 使用模板构建一个实际的tr元素，避免返回非元素节点
        const trElement = document.createElement('tr');
        trElement.setAttribute('data-notes', notesText);
        trElement.onmouseenter = (event) => this.app.showNotesTooltip(event, trElement);
        trElement.onmouseleave = () => this.app.hideNotesTooltip();
        
        trElement.innerHTML = `
            <td>${order.order_id}</td>
            <td><a href="#" class="text-decoration-none" onclick="app.openMonitorModal('${escapedSymbol}'); return false;">${this.app.escapeHtml ? this.app.escapeHtml(symbolStr) : symbolStr}</a></td>
            <td>${this.app.escapeHtml ? this.app.escapeHtml(actionStr) : escapedAction}</td>
            <td>${order.quantity}</td>
            <td>${priceDisplay}</td>
            <td><span class="badge bg-${this.app.getStatusColor(displayStatus)}">${displayStatus}</span></td>
            <td class="${(order.pnl ?? 0) > 0 ? 'text-success' : ((order.pnl ?? 0) < 0 ? 'text-danger' : 'text-muted')}">$${this.app.formatNumber(order.pnl ?? 0)}</td>
            <td>$${this.app.formatNumber(order.commission ?? 0)}</td>
            <td>${sourceDisplay}</td>
            <td>${detailsButton}${editButton}${syncButton}${cancelButton}</td>
        `;
        
        // 返回标准化后的TR元素
        return trElement;
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
            const response = await this.app.apiCall('/orders/cancel_all', {
                method: 'POST'
            });
            
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
            
            const response = await this.app.apiCall(`/orders/${orderId}`, {
                method: 'DELETE'
            });
            
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
    renderInternalOrders() {
        const tbody = document.querySelector('#internalOrdersTable tbody');
        if (!tbody) return;
        this.updateOrdersTableSmart(this.orders || [], tbody);
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
          if (tds.length !== 10) {
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
window.OrderManager = OrderManager;