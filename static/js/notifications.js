(function() {
    "use strict";
    
    class NotificationsManager {
        constructor(app) {
            this.app = app;
            this.notificationHistory = [];
            this.settingsListenersAttached = false;
            if (this.app) {
                this.app.filterNotificationHistory = this.filterNotificationHistory.bind(this);
            }
        }

        init() {
            this.initEventListeners();
            this.initializeHistoryFilters();
            this.filterNotificationHistory();
        }
        
        initEventListeners() {
            // 通知面板事件监听器
            const clearNotificationsBtn = document.getElementById('clearNotificationsBtn');
            if (clearNotificationsBtn) {
                clearNotificationsBtn.addEventListener('click', () => this.clearNotifications());
            }
        }

        initializeHistoryFilters() {
            this.ensureDefaultHistorySince();
        }

        getTodayDateString() {
            const now = new Date();
            const month = `${now.getMonth() + 1}`.padStart(2, '0');
            const day = `${now.getDate()}`.padStart(2, '0');
            return `${now.getFullYear()}-${month}-${day}`;
        }

        ensureDefaultHistorySince() {
            const sinceInput = document.getElementById('historySinceFilter');
            if (sinceInput && !sinceInput.value) {
                sinceInput.value = this.getTodayDateString();
            }
            return sinceInput ? sinceInput.value : '';
        }

        getHistoryFilters() {
            const status = document.getElementById('historyStatusFilter')?.value || '';
            const channel = document.getElementById('historyChannelFilter')?.value || '';
            const sinceInput = document.getElementById('historySinceFilter');
            const since = sinceInput?.value || this.ensureDefaultHistorySince();
            return {
                status: status === 'all' ? '' : status,
                channel: channel === 'all' ? '' : channel,
                since
            };
        }

        filterNotificationHistory() {
            const filters = this.getHistoryFilters();
            return this.loadNotificationHistory(filters);
        }
        
        // 添加通知到面板
        addNotification(type, message, timestamp = null) {
            const notificationPanel = document.getElementById('notificationPanel');
            if (!notificationPanel) return;
            
            const time = timestamp || new Date().toLocaleTimeString();
            const alertClass = {
                'success': 'alert-success',
                'error': 'alert-danger',
                'warning': 'alert-warning',
                'info': 'alert-info'
            }[type] || 'alert-info';
            
            const notification = document.createElement('div');
            notification.className = `alert ${alertClass} alert-dismissible fade show mb-2`;
            notification.innerHTML = `
                <small class="text-muted">${time}</small>
                <div>${message}</div>
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            `;
            
            notificationPanel.appendChild(notification);
            
            // 自动滚动到最新通知
            notification.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            
            // 限制通知数量，移除旧通知
            const notifications = notificationPanel.querySelectorAll('.alert');
            if (notifications.length > 50) {
                notifications[0].remove();
            }
        }
        
        // 清除所有通知
        clearNotifications() {
            this.app.notifications = [];
            this.updateNotificationPanel();
        }
        
        // 移除单个通知
        removeNotification(notificationId) {
            this.app.notifications = this.app.notifications.filter(n => n.id != notificationId);
            this.updateNotificationPanel();
        }
        
        // 更新通知面板
        updateNotificationPanel() {
            const notificationList = document.getElementById('notificationsList');
            if (!notificationList) return;
            
            notificationList.innerHTML = '';
            
            if (this.app.notifications.length === 0) {
                notificationList.innerHTML = `
                    <div class="text-muted text-center py-3">
                        <i class="fas fa-bell-slash"></i>
                        <p class="mb-0">No notifications</p>
                    </div>
                    <div class="mt-3">
                        <button class="btn btn-sm btn-outline-primary" onclick="app.notificationsManager.showNotificationSettings()">
                            <i class="fas fa-cog"></i> Settings
                        </button>
                        <button class="btn btn-sm btn-outline-secondary ms-2" onclick="app.notificationsManager.showNotificationHistory()">
                            <i class="fas fa-history"></i> History
                        </button>
                    </div>
                `;
                return;
            }
            
            // Add settings and history buttons at the top
            const headerDiv = document.createElement('div');
            headerDiv.className = 'mb-3 d-flex justify-content-between align-items-center';
            headerDiv.innerHTML = `
                <span class="text-muted small">${this.app.notifications.length} notification(s)</span>
                <div>
                    <button class="btn btn-sm btn-outline-primary" onclick="app.notificationsManager.showNotificationSettings()">
                        <i class="fas fa-cog"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-secondary ms-1" onclick="app.notificationsManager.showNotificationHistory()">
                        <i class="fas fa-history"></i>
                    </button>
                </div>
            `;
            notificationList.appendChild(headerDiv);
            
            this.app.notifications.forEach(notification => {
                const notificationElement = document.createElement('div');
                notificationElement.className = `alert alert-${notification.type} alert-dismissible fade show mb-2`;
                notificationElement.innerHTML = `
                    <div class="d-flex justify-content-between align-items-start">
                        <div>
                            <div>${notification.message}</div>
                            <small class="text-muted">${notification.timestamp}</small>
                        </div>
                        <button type="button" class="btn-close" onclick="app.notificationsManager.removeNotification('${notification.id}')"></button>
                    </div>
                `;
                notificationList.appendChild(notificationElement);
            });
        }
        
        // 显示通知设置模态框
        async showNotificationSettings() {
            try {
                await this.populateBasicSettings();
                await this.populateModuleSettings();
                
                const modal = new bootstrap.Modal(document.getElementById('notificationSettingsModal'));
                modal.show();
                
                // 添加事件监听器
                this.addNotificationSettingsListeners();
            } catch (error) {
                console.error('Failed to show notification settings:', error);
                this.app.showAlert('danger', 'Failed to load notification settings');
            }
        }
        
        // 添加通知设置事件监听器
        addNotificationSettingsListeners() {
            if (this.settingsListenersAttached) return;
            // 基础设置开关变化时更新输入状态
            ['telegramEnabled', 'emailEnabled', 'tradingReminderEnabled'].forEach(id => {
                const element = document.getElementById(id);
                if (element) {
                    element.addEventListener('change', () => this.updateNotificationInputStates());
                }
            });

            // 测试按钮
            const testReminderBtn = document.getElementById('testTradingReminderBtn');
            if (testReminderBtn) {
                testReminderBtn.addEventListener('click', () => this.testTradingReminder());
            }

            const testNotificationBtn = document.getElementById('testNotificationBtn');
            if (testNotificationBtn) {
                testNotificationBtn.addEventListener('click', () => this.testNotification());
            }

            const checkStatusBtn = document.getElementById('checkTradingReminderStatusBtn');
            if (checkStatusBtn) {
                checkStatusBtn.addEventListener('click', () => this.checkTradingReminderStatus());
            }

            // 保存按钮
            const saveBtn = document.getElementById('saveNotificationSettingsBtn');
            if (saveBtn) {
                saveBtn.addEventListener('click', () => this.saveNotificationSettings());
            }

            this.settingsListenersAttached = true;
        }
        
        // 填充基础设置
        async populateBasicSettings() {
            try {
                const response = await this.app.apiCall('/notifications/settings');
                const settings = response.settings || {};
                
                // Telegram设置
                const telegram = settings.telegram || {};
                document.getElementById('telegramEnabled').checked = telegram.enabled || false;
                document.getElementById('telegramBotToken').value = telegram.config?.bot_token || '';
                document.getElementById('telegramChatId').value = telegram.config?.chat_id || '';
                
                // Email设置
                document.getElementById('emailEnabled').checked = settings.email_enabled || false;
                document.getElementById('emailAddress').value = settings.email_address || '';
                
                // 交易提醒设置
                const tradingReminder = settings.trading_reminder || {};
                document.getElementById('tradingReminderEnabled').checked = tradingReminder.enabled || false;
                document.getElementById('startTradingTime').value = tradingReminder.start_trading_time || '20:30';
                document.getElementById('endTradingTime').value = tradingReminder.end_trading_time || '22:30';
                document.getElementById('browserNotificationEnabled').checked = tradingReminder.browser_notification_enabled !== false;
                document.getElementById('telegramReminderEnabled').checked = tradingReminder.telegram_reminder_enabled || false;
                
                this.updateNotificationInputStates();
            } catch (error) {
                console.error('Failed to load notification settings:', error);
            }
        }
        
        // 填充模块设置
        async populateModuleSettings() {
            try {
                const response = await this.app.apiCall('/notifications/settings');
                const settings = response.settings || {};
                
                const chrome = settings.chrome || {};
                const telegram = settings.telegram || {};
                
                const chromeEvents = chrome.events || [];
                const telegramEvents = telegram.events || [];
                
                const modules = await this.getAvailableModules();
                const tableBody = document.getElementById('moduleSettingsTableBody');
                tableBody.innerHTML = '';
                
                const hasChromeOverlap = chromeEvents.some(event => modules.includes(event));
                
                modules.forEach(module => {
                    const row = document.createElement('tr');
                    row.innerHTML = `
                        <td class="fw-bold">${module}</td>
                        <td class="text-center">
                            <input type="checkbox" class="form-check-input" 
                                   data-channel="chrome" data-module="${module}"
                                   ${((chromeEvents.length === 0 || !hasChromeOverlap) ? true : chromeEvents.includes(module)) ? 'checked' : ''}>
                        </td>
                        <td class="text-center">
                            <input type="checkbox" class="form-check-input" 
                                   data-channel="telegram" data-module="${module}"
                                   ${(telegram.enabled && telegramEvents.includes(module)) ? 'checked' : ''}>
                        </td>
                        <td class="text-center">
                            <input type="checkbox" class="form-check-input" 
                                   data-channel="email" data-module="${module}">
                        </td>
                    `;
                    tableBody.appendChild(row);
                });
                
                this.updateNotificationInputStates();
            } catch (error) {
                console.error('Failed to load module settings:', error);
            }
        }
        
        // 更新通知输入状态
        updateNotificationInputStates() {
            try {
                const setDisabled = (id, disabled) => {
                    const el = document.getElementById(id);
                    if (el) {
                        el.disabled = !!disabled;
                    }
                };
                
                const tgEnabled = !!document.getElementById('telegramEnabled')?.checked;
                setDisabled('telegramBotToken', !tgEnabled);
                setDisabled('telegramChatId', !tgEnabled);
                
                const emailEnabled = !!document.getElementById('emailEnabled')?.checked;
                setDisabled('emailAddress', !emailEnabled);
                
                const trEnabled = !!document.getElementById('tradingReminderEnabled')?.checked;
                setDisabled('startTradingTime', !trEnabled);
                setDisabled('endTradingTime', !trEnabled);
                setDisabled('browserNotificationEnabled', !trEnabled);
                setDisabled('telegramReminderEnabled', !trEnabled);
                
                // 模块设置表格中禁用/启用 Telegram 列
                document.querySelectorAll('#moduleSettingsTableBody input[data-channel="telegram"]').forEach(el => {
                    el.disabled = !tgEnabled;
                });
                
                // 模块设置表格中禁用/启用 Email 列
                document.querySelectorAll('#moduleSettingsTableBody input[data-channel="email"]').forEach(el => {
                    el.disabled = !emailEnabled;
                });
            } catch (err) {
                console.error('updateNotificationInputStates error:', err);
            }
        }
        
        // 获取可用模块
        async getAvailableModules() {
            return [
                'Order Filled',
                'Position Opened',
                'Position Closed',
                'Risk Rule',
                'Risk Rule Deactivated',
                'Risk Circuit Breaker',
                'Take Profit Triggered',
                'Stop Loss Triggered',
                'Strategy Started',
                'Strategy Stopped',
                'Queue Status',
                'Queue Warning',
                'DOM Level 3',
                'DOM Level 4',
                'Trading Reminder',
                'General'
            ];
        }
        
        // 保存通知设置
        async saveNotificationSettings() {
            try {
                // 收集基础设置
                const telegram_enabled = document.getElementById('telegramEnabled').checked;
                const telegram_bot_token = document.getElementById('telegramBotToken').value;
                const telegram_chat_id = document.getElementById('telegramChatId').value;
                const email_enabled = document.getElementById('emailEnabled').checked;
                const email_address = document.getElementById('emailAddress').value;
                const trading_reminder_enabled = document.getElementById('tradingReminderEnabled').checked;
                const start_trading_time = document.getElementById('startTradingTime').value;
                const end_trading_time = document.getElementById('endTradingTime').value;
                const browser_notification_enabled = document.getElementById('browserNotificationEnabled').checked;
                const telegram_reminder_enabled = document.getElementById('telegramReminderEnabled').checked;
                
                // 收集模块设置
                const chromeModules = [];
                const telegramModules = [];
                const rows = document.querySelectorAll('#moduleSettingsTableBody input[type="checkbox"]');
                rows.forEach(input => {
                    const module = input.getAttribute('data-module');
                    const channel = input.getAttribute('data-channel');
                    if (input.checked) {
                        if (channel === 'chrome') chromeModules.push(module);
                        if (channel === 'telegram') telegramModules.push(module);
                    }
                });
                
                // 构造后端需要的结构
                const settings = {
                    chrome: {
                        enabled: true,
                        config: {},
                        events: chromeModules.length ? chromeModules : await this.getAvailableModules()
                    },
                    telegram: {
                        enabled: telegram_enabled,
                        config: {
                            bot_token: telegram_bot_token,
                            chat_id: telegram_chat_id
                        },
                        events: telegramModules
                    },
                    trading_reminder: {
                        enabled: trading_reminder_enabled,
                        start_trading_time,
                        end_trading_time,
                        browser_notification_enabled,
                        telegram_reminder_enabled
                    },
                    email_enabled,
                    email_address
                };
                
                const response = await this.app.apiCall('/notifications/settings', {
                    method: 'PUT',
                    body: JSON.stringify(settings)
                });
                
                if (response.success) {
                    this.app.showAlert('success', 'Notification settings saved successfully');
                    
                    // Update trading reminder schedules if enabled
                    if (settings.trading_reminder && settings.trading_reminder.enabled) {
                        this.app.scheduleTradingReminders();
                    }
                    
                    const modal = bootstrap.Modal.getInstance(document.getElementById('notificationSettingsModal'));
                    modal.hide();
                } else {
                    this.app.showAlert('danger', 'Failed to save notification settings: ' + (response.error || 'Unknown error'));
                }
            } catch (error) {
                console.error('Failed to save notification settings:', error);
                this.app.showAlert('danger', 'Failed to save notification settings: ' + error.message);
            }
        }
        
        // 测试交易提醒
        async testTradingReminder() {
            try {
                // 测试浏览器通知
                let browserSent = false;
                if ('Notification' in window) {
                    if (Notification.permission === 'granted') {
                        new Notification('Trading Reminder Test', {
                            body: 'This is a test trading reminder notification',
                            icon: '/static/favicon.ico'
                        });
                        browserSent = true;
                    } else if (Notification.permission !== 'denied') {
                        const permission = await Notification.requestPermission();
                        if (permission === 'granted') {
                            new Notification('Trading Reminder Test', {
                                body: 'This is a test trading reminder notification',
                                icon: '/static/favicon.ico'
                            });
                            browserSent = true;
                        }
                    }
                }
                
                // Telegram测试
                const telegramEnabled = document.getElementById('telegramEnabled')?.checked;
                const botToken = document.getElementById('telegramBotToken')?.value.trim();
                const chatId = document.getElementById('telegramChatId')?.value.trim();
                
                if (telegramEnabled && botToken && chatId) {
                    this.app.showAlert('info', '浏览器通知测试已发送。Telegram将在发生实际事件时按模块设置发送通知（保存设置后生效）。');
                } else if (browserSent) {
                    this.app.showAlert('success', '浏览器通知测试已发送。');
                } else {
                    this.app.showAlert('warning', '无法发送浏览器通知（可能未授权）。请允许通知或在地址栏权限设置中启用。');
                }
            } catch (error) {
                console.error('Test reminder failed:', error);
                this.app.showAlert('danger', 'Test reminder failed: ' + error.message);
            }
        }

        // 检查交易提醒状态
        checkTradingReminderStatus() {
            try {
                const statusEl = document.getElementById('tradingReminderStatus');
                const enabled = document.getElementById('tradingReminderEnabled')?.checked;
                const startTime = document.getElementById('startTradingTime')?.value || '20:30';
                const endTime = document.getElementById('endTradingTime')?.value || '22:30';
                const telegramReminderEnabled = document.getElementById('telegramReminderEnabled')?.checked;

                const statusParts = [];
                if (!('Notification' in window)) {
                    statusParts.push('浏览器不支持通知');
                } else {
                    statusParts.push(`浏览器通知权限：${Notification.permission}`);
                }

                if (enabled) {
                    statusParts.push(`提醒时间：${startTime} - ${endTime}`);
                } else {
                    statusParts.push('交易提醒已关闭');
                }

                statusParts.push(this.app.startReminderTimeout || this.app.endReminderTimeout
                    ? '提醒计时器已设置'
                    : '提醒计时器未设置');

                statusParts.push(telegramReminderEnabled ? 'Telegram 提醒开启' : 'Telegram 提醒关闭');

                const message = statusParts.join(' | ');
                if (statusEl) {
                    statusEl.textContent = message;
                }

                this.app.showAlert('info', message);
            } catch (error) {
                console.error('checkTradingReminderStatus error:', error);
                this.app.showAlert('danger', 'Failed to check reminder status: ' + error.message);
            }
        }

        // 测试通知
        async testNotification() {
            try {
                const response = await this.app.apiCall('/notifications/test', {
                    method: 'POST',
                    body: JSON.stringify({
                        channel: 'chrome',
                        message: 'This is a test notification from IBBot'
                    })
                });
                
                if (response.success) {
                    this.app.showAlert('success', 'Test notification sent successfully');
                } else {
                    this.app.showAlert('warning', 'Test notification sent but may have failed: ' + (response.error || 'Check your settings'));
                }
            } catch (error) {
                console.error('Failed to send test notification:', error);
                this.app.showAlert('danger', 'Failed to send test notification: ' + error.message);
            }
        }
        
        // 加载通知历史记录
        async loadNotificationHistory(filters = null) {
            try {
                const resolved = filters || this.getHistoryFilters();
                const params = new URLSearchParams({ limit: '50' });
                if (resolved?.status) params.set('status', resolved.status);
                if (resolved?.channel) params.set('channel', resolved.channel);
                if (resolved?.since) params.set('since', resolved.since);
                const query = params.toString();
                const response = await this.app.apiCall(`/notifications/history?${query}`);
                const history = response.history || [];

                this.notificationHistory = history;
                this.renderNotificationHistoryRows(history);
            } catch (error) {
                console.error('Failed to load notification history:', error);
                const tbody = document.getElementById('notificationHistoryBody');
                if (tbody) {
                    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Failed to load notification history</td></tr>';
                }
            }
        }

        escapeHtml(text) {
            if (text === null || text === undefined) return '';
            return String(text)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        formatTimestamp(value) {
            if (!value) return this.escapeHtml('-');
            try {
                const date = new Date(value);
                if (!Number.isNaN(date.getTime())) {
                    return this.escapeHtml(date.toLocaleString());
                }
            } catch (err) {
                console.warn('formatTimestamp error:', err);
            }
            return this.escapeHtml(value);
        }

        // 渲染通知历史记录行
        renderNotificationHistoryRows(history) {
            const tbody = document.getElementById('notificationHistoryBody');
            if (!tbody) return;

            if (!history || history.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No notification history found</td></tr>';
                return;
            }

            tbody.innerHTML = history.map(notification => {
                const timestamp = this.formatTimestamp(notification.sent_at || notification.created_at || notification.timestamp);
                const statusBadge = this.getNotificationStatusBadge(notification.status);
                const channelIcon = this.getChannelIcon(notification.channel);
                const channelLabel = this.escapeHtml(notification.channel || '');
                const eventLabel = this.escapeHtml(this.getEventLabel(notification.event_type));
                const messageText = this.escapeHtml(notification.message || '');
                const messageTitle = messageText || this.escapeHtml('(empty)');
                const errorMessage = notification.error_message ? this.escapeHtml(notification.error_message) : '';
                const errorCell = errorMessage
                    ? `<span class="text-danger" title="${errorMessage}">${errorMessage}</span>`
                    : '<span class="text-muted">-</span>';

                return `
                    <tr>
                        <td>${timestamp}</td>
                        <td>${channelIcon} ${channelLabel}</td>
                        <td>${eventLabel}</td>
                        <td class="text-truncate" style="max-width: 200px;" title="${messageTitle}">${messageText || '<span class="text-muted">(empty)</span>'}</td>
                        <td>${statusBadge}</td>
                        <td>${errorCell}</td>
                    </tr>
                `;
            }).join('');
        }
        
        // 获取通知状态徽章
        getNotificationStatusBadge(status) {
            const badges = {
                'sent': '<span class="badge bg-success">Sent</span>',
                'failed': '<span class="badge bg-danger">Failed</span>',
                'pending': '<span class="badge bg-warning">Pending</span>'
            };
            return badges[status] || '<span class="badge bg-secondary">Unknown</span>';
        }
        
        // 获取渠道图标
        getChannelIcon(channel) {
            const icons = {
                'chrome': '<i class="fab fa-chrome text-primary"></i>',
                'telegram': '<i class="fab fa-telegram text-info"></i>',
                'email': '<i class="fas fa-envelope text-secondary"></i>'
            };
            return icons[channel] || '<i class="fas fa-bell"></i>';
        }

        getEventLabel(eventType) {
            if (!eventType) return 'N/A';
            const map = {
                'order_filled': 'Order Filled',
                'position_opened': 'Position Opened',
                'position_closed': 'Position Closed',
                'risk_triggered': 'Risk Rule',
                'risk_rule': 'Risk Rule',
                'risk_rule_deactivated': 'Risk Rule Deactivated',
                'take_profit': 'Take Profit Triggered',
                'stop_loss': 'Stop Loss Triggered',
                'strategy_started': 'Strategy Started',
                'strategy_stopped': 'Strategy Stopped',
                'dom_level_3': 'DOM Level 3',
                'dom_level_4': 'DOM Level 4',
                'trading_reminder': 'Trading Reminder',
                'general': 'General',
                'test': 'Test'
            };
            const normalized = String(eventType).trim();
            const lower = normalized.toLowerCase();
            return map[lower] || normalized;
        }
        
        // 显示通知历史记录
        showNotificationHistory() {
            try {
                this.filterNotificationHistory();
                const table = document.getElementById('notificationHistoryTable') || document.getElementById('notificationHistoryBody');
                if (table && table.scrollIntoView) {
                    table.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    const card = (table.closest && table.closest('.card')) || table;
                    const prev = card.style.backgroundColor;
                    card.style.transition = 'background-color .6s ease';
                    card.style.backgroundColor = 'rgba(255,243,205,.6)';
                    setTimeout(() => {
                        card.style.backgroundColor = prev || '';
                    }, 800);
                }
            } catch (e) {
                console.error('showNotificationHistory error:', e);
            }
        }
    }
    
    // 导出到全局
    window.NotificationsManager = NotificationsManager;
})();