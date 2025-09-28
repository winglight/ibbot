(function(global) {
    const root = global || {};
    const appModules = root.appModules = root.appModules || {};
    const uiNamespace = appModules.ui = appModules.ui || {};
    const controllerNamespace = uiNamespace.controller = uiNamespace.controller || {};

    class UIController {
        constructor(app) {
            this.app = app;
            this.historicalSymbolsKey = 'historicalSymbols';
        }

        showAlert(type, message) {
            const alertContainer = document.getElementById('alertContainer');
            if (!alertContainer) {
                console[type === 'danger' || type === 'error' ? 'error' : 'log']?.(message);
                return;
            }

            const alertId = `alert-${Date.now()}`;

            alertContainer.style.position = 'fixed';
            alertContainer.style.top = document.querySelector('.modal.show') ? '10px' : '20px';
            alertContainer.style.right = '20px';
            alertContainer.style.zIndex = '20000';
            alertContainer.style.maxWidth = '420px';

            const iconClass = type === 'success' ? 'fa-check-circle text-success'
                : (type === 'danger' || type === 'error') ? 'fa-times-circle text-danger'
                : type === 'warning' ? 'fa-exclamation-triangle text-warning'
                : 'fa-info-circle text-info';

            const alertHtml = `
                <div id="${alertId}" class="alert alert-${type} alert-dismissible fade show" role="alert" style="background:#ffffff; color:#212529; border:1px solid rgba(0,0,0,.125); box-shadow:0 0.5rem 1rem rgba(0,0,0,.15); opacity:1;">
                    <div style="display:flex; align-items:center; gap:.5rem;">
                        <i class="fas ${iconClass}"></i>
                        <span>${message}</span>
                    </div>
                    <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
                </div>
            `;

            alertContainer.insertAdjacentHTML('beforeend', alertHtml);

            if (this.app?.notificationsManager?.addNotification) {
                this.app.notificationsManager.addNotification(type, message);
            }

            setTimeout(() => {
                const alert = document.getElementById(alertId);
                if (alert && window.bootstrap?.Alert) {
                    const bsAlert = new window.bootstrap.Alert(alert);
                    bsAlert.close();
                } else if (alert) {
                    alert.remove();
                }
            }, 5000);
        }

        initSymbolCombobox() {
            this.setupSymbolInput('orderSymbol', 'orderSymbolDropdown');
        }

        setupSymbolInput(inputId, dropdownId) {
            const input = document.getElementById(inputId);
            const dropdown = document.getElementById(dropdownId);
            if (!input || !dropdown) return;

            dropdown.dataset.inputId = inputId;
            let searchTimeout;

            input.addEventListener('focus', () => {
                this.showHistoricalSymbols(dropdownId);
                dropdown.classList.add('show');
            });

            document.addEventListener('click', (e) => {
                if (!input.contains(e.target) && !dropdown.contains(e.target)) {
                    dropdown.classList.remove('show');
                }
            });

            input.addEventListener('input', (e) => {
                const query = e.target.value.trim();

                clearTimeout(searchTimeout);

                if (query.length === 0) {
                    this.showHistoricalSymbols(dropdownId);
                    if (inputId === 'orderSymbol') {
                        const priceDisplay = document.getElementById('currentPriceDisplay');
                        if (priceDisplay) {
                            priceDisplay.style.display = 'none';
                        }
                    }
                    return;
                }

                if (query.length >= 1) {
                    searchTimeout = setTimeout(() => {
                        this.searchSymbols(query, dropdownId);
                    }, 300);
                }

                if (inputId === 'orderSymbol' && query.length >= 2) {
                    clearTimeout(this.app.priceTimeout);
                    this.app.priceTimeout = setTimeout(() => {
                        this.app.fetchCurrentPrice(query);
                    }, 1000);
                }
            });

            input.addEventListener('keydown', (e) => {
                const items = dropdown.querySelectorAll('.dropdown-item');
                const activeItem = dropdown.querySelector('.dropdown-item.active');
                let activeIndex = Array.from(items).indexOf(activeItem);

                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    activeIndex = activeIndex < items.length - 1 ? activeIndex + 1 : 0;
                    this.setActiveDropdownItem(items, activeIndex);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    activeIndex = activeIndex > 0 ? activeIndex - 1 : items.length - 1;
                    this.setActiveDropdownItem(items, activeIndex);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    if (activeItem) {
                        activeItem.click();
                    }
                } else if (e.key === 'Escape') {
                    dropdown.classList.remove('show');
                    input.blur();
                }
            });
        }

        setActiveDropdownItem(items, activeIndex) {
            items.forEach((item, index) => {
                item.classList.toggle('active', index === activeIndex);
            });
        }

        showHistoricalSymbols(dropdownId) {
            const dropdown = document.getElementById(dropdownId);
            if (!dropdown) return;

            const historicalSymbols = this.getHistoricalSymbols();
            let html = '';

            if (historicalSymbols.length > 0) {
                html += '<h6 class="dropdown-header">Recent Symbols</h6>';
                historicalSymbols.forEach((symbol) => {
                    html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
                });
                html += '<div class="dropdown-divider"></div>';
            }

            html += '<h6 class="dropdown-header">Popular Symbols</h6>';
            const popularSymbols = ['MNQ', 'NQ', 'ES', 'YM', 'AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA', 'AMZN'];
            popularSymbols.forEach((symbol) => {
                if (!historicalSymbols.includes(symbol)) {
                    html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
                }
            });

            dropdown.innerHTML = html;

            dropdown.querySelectorAll('.dropdown-item').forEach((item) => {
                item.addEventListener('click', (e) => {
                    e.preventDefault();
                    const symbol = e.target.getAttribute('data-symbol');
                    const targetId = dropdown.dataset.inputId || (dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol');
                    const targetInput = document.getElementById(targetId);
                    if (targetInput) {
                        targetInput.value = symbol;
                        const changeEvent = new Event('change', { bubbles: true });
                        targetInput.dispatchEvent(changeEvent);
                    }
                    dropdown.classList.remove('show');
                    this.addToHistoricalSymbols(symbol);

                    if (targetId === 'orderSymbol') {
                        this.app.fetchCurrentPrice(symbol);
                    }
                });
            });
        }

        async searchSymbols(query, dropdownId) {
            const dropdown = document.getElementById(dropdownId);
            if (!dropdown) return;

            try {
                dropdown.innerHTML = '<div class="dropdown-item-text">Searching...</div>';
                dropdown.classList.add('show');

                const allSymbols = [
                    'MNQ', 'NQ', 'ES', 'YM', 'RTY', 'CL', 'GC', 'SI',
                    'AAPL', 'MSFT', 'GOOGL', 'GOOG', 'TSLA', 'NVDA', 'AMZN', 'META',
                    'NFLX', 'AMD', 'INTC', 'CRM', 'ORCL', 'ADBE', 'PYPL', 'UBER',
                    'SPOT', 'ZM', 'DOCU', 'SNOW', 'PLTR', 'COIN', 'RBLX', 'HOOD'
                ];

                const filteredSymbols = allSymbols.filter((symbol) =>
                    symbol.toLowerCase().includes(query.toLowerCase())
                );

                let html = '';

                if (filteredSymbols.length > 0) {
                    html += '<h6 class="dropdown-header">Search Results</h6>';
                    filteredSymbols.slice(0, 10).forEach((symbol) => {
                        html += `<a class="dropdown-item" href="#" data-symbol="${symbol}">${symbol}</a>`;
                    });
                } else {
                    html += '<div class="dropdown-item-text text-muted">No symbols found</div>';
                }

                dropdown.innerHTML = html;

                dropdown.querySelectorAll('.dropdown-item').forEach((item) => {
                    item.addEventListener('click', (e) => {
                        e.preventDefault();
                        const symbol = e.target.getAttribute('data-symbol');
                        const inputId = dropdown.dataset.inputId || (dropdownId === 'symbolDropdown' ? 'dataSymbol' : 'orderSymbol');
                        const inputEl = document.getElementById(inputId);
                        if (inputEl) {
                            inputEl.value = symbol;
                            const changeEvent = new Event('change', { bubbles: true });
                            inputEl.dispatchEvent(changeEvent);
                        }
                        dropdown.classList.remove('show');
                        this.addToHistoricalSymbols(symbol);

                        if (inputId === 'orderSymbol') {
                            this.app.fetchCurrentPrice(symbol);
                        }
                    });
                });
            } catch (error) {
                console.error('Error searching symbols:', error);
                dropdown.innerHTML = '<div class="dropdown-item-text text-danger">Search failed</div>';
            }
        }

        getHistoricalSymbols() {
            const stored = localStorage.getItem(this.historicalSymbolsKey);
            try {
                return stored ? JSON.parse(stored) : [];
            } catch (error) {
                console.warn('Failed to parse historical symbols from storage', error);
                return [];
            }
        }

        addToHistoricalSymbols(symbol) {
            if (!symbol) return;
            let historical = this.getHistoricalSymbols();
            historical = historical.filter((s) => s !== symbol);
            historical.unshift(symbol);
            historical = historical.slice(0, 10);
            localStorage.setItem(this.historicalSymbolsKey, JSON.stringify(historical));
        }

        ensureChartDestroyed(id) {
            const charts = this.app?.charts || {};
            if (charts[id]) {
                try {
                    charts[id].destroy();
                } catch (error) {
                    console.warn('Failed to destroy chart', error);
                }
                delete charts[id];
            }
        }

        renderLineChart(canvasId, label, labels, data, color) {
            const ctx = document.getElementById(canvasId);
            if (!ctx) return;
            this.ensureChartDestroyed(canvasId);
            this.app.charts[canvasId] = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [{
                        label,
                        data,
                        borderColor: color,
                        backgroundColor: color,
                        fill: false,
                        tension: 0.2,
                        pointRadius: 0,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: { x: { display: true }, y: { display: true } }
                }
            });
        }

        renderBarChart(canvasId, label, labels, data, color) {
            const ctx = document.getElementById(canvasId);
            if (!ctx) return;
            this.ensureChartDestroyed(canvasId);
            this.app.charts[canvasId] = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels,
                    datasets: [{ label, data, backgroundColor: color }]
                },
                options: { responsive: true, maintainAspectRatio: false }
            });
        }

        renderPieChart(canvasId, labels, data, colors) {
            const ctx = document.getElementById(canvasId);
            if (!ctx) return;
            this.ensureChartDestroyed(canvasId);
            this.app.charts[canvasId] = new Chart(ctx, {
                type: 'pie',
                data: {
                    labels,
                    datasets: [{ data, backgroundColor: colors }]
                },
                options: { responsive: true, maintainAspectRatio: false }
            });
        }

        makeHistogram(values, bins) {
            if (!Array.isArray(values) || values.length === 0 || !Number.isFinite(bins) || bins <= 0) {
                return { labels: [], counts: [] };
            }
            const min = Math.min(...values);
            const max = Math.max(...values);
            if (min === max) {
                return {
                    labels: [min.toFixed(2)],
                    counts: [values.length]
                };
            }
            const step = (max - min) / bins;
            const labels = [];
            const counts = new Array(bins).fill(0);
            for (let i = 0; i < bins; i++) {
                labels.push(`${(min + i * step).toFixed(2)}`);
            }
            values.forEach((value) => {
                const idx = Math.min(bins - 1, Math.max(0, Math.floor((value - min) / step)));
                counts[idx] += 1;
            });
            return { labels, counts };
        }

        clearData() {
            this.app?.resetPanelLoading?.('orders');
            this.app?.resetPanelLoading?.('portfolio');
            this.app?.resetPanelLoading?.('risk-rules');
            this.app?.resetPanelLoading?.('strategies');

            const accountInfo = document.getElementById('accountInfo');
            if (accountInfo) {
                accountInfo.innerHTML = '<p class="text-muted">Connect to view account information</p>';
            }

            const ordersTableBody = document.querySelector('#ordersTable tbody');
            if (ordersTableBody) {
                ordersTableBody.innerHTML = '';
            }

            const positionsTableBody = document.querySelector('#positionsTable tbody');
            if (positionsTableBody) {
                positionsTableBody.innerHTML = '';
            }

            const autoOrdersTableBody = document.querySelector('#autoOrdersTable tbody');
            if (autoOrdersTableBody) {
                autoOrdersTableBody.innerHTML = '';
            }

            const riskRulesTableBody = document.querySelector('#riskRulesTable2 tbody');
            if (riskRulesTableBody) {
                riskRulesTableBody.innerHTML = '';
            }
        }

        getStatusColor(status) {
            switch ((status || '').toLowerCase()) {
                case 'filled':
                case 'executed':
                case 'success':
                    return 'success';
                case 'cancelled':
                case 'canceled':
                case 'failed':
                case 'error':
                case 'rejected':
                    return 'danger';
                case 'submitted':
                case 'presubmitted':
                case 'pendingsubmit':
                    return 'primary';
                case 'pendingcancel':
                case 'apiwaiting':
                case 'apipending':
                case 'pending':
                    return 'warning';
                case 'inactive':
                    return 'secondary';
                default:
                    return 'secondary';
            }
        }

        isSameDay(dateInput) {
            if (!dateInput) return false;
            const d = new Date(dateInput);
            if (Number.isNaN(d.getTime())) return false;
            const now = new Date();
            return d.getFullYear() === now.getFullYear()
                && d.getMonth() === now.getMonth()
                && d.getDate() === now.getDate();
        }
    }

    controllerNamespace.UIController = UIController;
})(typeof window !== 'undefined' ? window : globalThis);
