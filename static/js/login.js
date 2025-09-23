/**
 * 登录管理模块
 * 负责用户认证、登录状态管理、自动登录等功能
 */
class LoginManager {
    constructor(app) {
        this.app = app;
        this._pendingReadOnlyUpdate = null;
        this._readOnlyRetryTimer = null;
        this._readOnlyRetryAttempts = 0;
        this.init();
    }

    /**
     * 初始化登录管理器
     */
    init() {
        this.bindEventListeners();
        this.toggleAuthButtons(!!localStorage.getItem('access_token'));
        const storedRole = localStorage.getItem('user_role') || 'admin';
        const isReadOnly = localStorage.getItem('is_read_only') === '1';
        this.applyReadOnlyMode(isReadOnly, storedRole, { persist: false });
    }

    /**
     * 安全地更新应用的只读模式状态
     * 如果 TradingApp 尚未暴露 updateReadOnlyMode，则执行降级处理并重试
     */
    applyReadOnlyMode(isReadOnly, role = 'admin', options = {}) {
        if (this.app && typeof this.app.updateReadOnlyMode === 'function') {
            this.clearReadOnlyRetry();
            this._pendingReadOnlyUpdate = null;
            this.app.updateReadOnlyMode(isReadOnly, role, options);
            return;
        }

        const update = {
            isReadOnly: !!isReadOnly,
            role,
            options
        };
        this._pendingReadOnlyUpdate = update;
        this.syncReadOnlyFallback(update);
        this.scheduleReadOnlyRetry();
    }

    /**
     * 当 updateReadOnlyMode 暂不可用时同步本地状态和 UI
     */
    syncReadOnlyFallback(update) {
        if (!update) return;

        const { isReadOnly, role, options } = update;
        const persist = options?.persist !== false;
        const normalizedRole = role || (isReadOnly ? 'visitor' : 'admin');

        if (this.app) {
            this.app.isReadOnly = !!isReadOnly;
            this.app.authRole = normalizedRole;
        }

        if (persist) {
            if (isReadOnly) {
                localStorage.setItem('is_read_only', '1');
            } else {
                localStorage.removeItem('is_read_only');
            }
            if (role) {
                localStorage.setItem('user_role', role);
            } else {
                localStorage.removeItem('user_role');
            }
        }

        if (typeof document !== 'undefined' && document.body) {
            document.body.classList.toggle('read-only-mode', !!isReadOnly);
            const badge = document.getElementById('authRoleBadge');
            if (badge) {
                if (isReadOnly) {
                    badge.classList.remove('d-none');
                    badge.innerHTML = '<i class="fas fa-user-shield me-1"></i>Visitor Read Only';
                } else if (role && role.toLowerCase() !== 'admin') {
                    badge.classList.remove('d-none');
                    badge.innerHTML = `<i class="fas fa-user me-1"></i>${role}`;
                } else {
                    badge.classList.add('d-none');
                    badge.textContent = '';
                }
            }
            const banner = document.getElementById('readOnlyBanner');
            if (banner) {
                banner.classList.toggle('d-none', !isReadOnly);
            }
        }
    }

    /**
     * 安排重试，等待 TradingApp 初始化完成
     */
    scheduleReadOnlyRetry() {
        if (this._readOnlyRetryTimer) return;
        if (this._readOnlyRetryAttempts >= 20) return;

        const schedule = typeof window !== 'undefined'
            ? window.setTimeout.bind(window)
            : setTimeout;
        this._readOnlyRetryTimer = schedule(() => {
            this._readOnlyRetryTimer = null;
            this._readOnlyRetryAttempts += 1;
            const pending = this._pendingReadOnlyUpdate;
            if (!pending) return;
            this.applyReadOnlyMode(pending.isReadOnly, pending.role, pending.options);
        }, 200);
    }

    /**
     * 清理重试状态
     */
    clearReadOnlyRetry() {
        if (this._readOnlyRetryTimer) {
            clearTimeout(this._readOnlyRetryTimer);
            this._readOnlyRetryTimer = null;
        }
        this._readOnlyRetryAttempts = 0;
    }

    /**
     * 显示登录模态框
     */
    showLoginModal() {
        const modalEl = document.getElementById('loginModal');
        if (!modalEl) return;
        
        const remember = localStorage.getItem('rememberLogin') === '1';
        const savedUser = localStorage.getItem('loginUsername') || '';
        const savedPass = localStorage.getItem('loginPassword') || '';
        
        if (remember) {
            const u = document.getElementById('loginUsername');
            const p = document.getElementById('loginPassword');
            const r = document.getElementById('rememberLogin');
            if (u) u.value = savedUser;
            if (p) p.value = savedPass;
            if (r) r.checked = true;
        }
        
        const modal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
        modal.show();
    }

    /**
     * 处理登录表单提交
     */
    async handleLoginSubmit() {
        try {
            const username = (document.getElementById('loginUsername')?.value || '').trim();
            const password = (document.getElementById('loginPassword')?.value || '').trim();
            const remember = !!document.getElementById('rememberLogin')?.checked;
            
            if (remember) {
                localStorage.setItem('rememberLogin', '1');
                localStorage.setItem('loginUsername', username);
                localStorage.setItem('loginPassword', password);
            } else {
                localStorage.removeItem('rememberLogin');
                localStorage.removeItem('loginUsername');
                localStorage.removeItem('loginPassword');
            }
            
            if (!username || !password) {
                this.app.showAlert('danger', '请输入用户名和密码');
                return;
            }
            
            const res = await this.app.apiCall('/login', {
                method: 'POST',
                body: JSON.stringify({ username, password })
            });
            
            if (res?.access_token) {
                localStorage.setItem('access_token', res.access_token);
                if (res.role) {
                    localStorage.setItem('user_role', res.role);
                } else {
                    localStorage.removeItem('user_role');
                }
                if (res.is_read_only) {
                    localStorage.setItem('is_read_only', '1');
                } else {
                    localStorage.removeItem('is_read_only');
                }
                this.toggleAuthButtons(true);
                this.applyReadOnlyMode(!!res.is_read_only, res.role || 'admin');

                const modalEl = document.getElementById('loginModal');
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();
                
                this.app.showAlert('success', '登录成功');
                
                // 登录成功后的回调
                this.onLoginSuccess();
            }
        } catch (e) {
            console.error('Login error:', e);
        }
    }

    /**
     * 登录成功后的处理
     */
    onLoginSuccess() {
        // 检查连接状态
        this.app.ibkrManager.checkConnectionStatus();
        
        // 加载基础数据
        this.app.strategiesManager.loadStrategies();
        this.app.loadLogs();
        
        // 只在连接成功后加载需要IB连接的数据
        if (this.app.connected) {
            this.app.loadOrders();
            this.app.loadPortfolio();
            this.app.riskRulesManager.loadRiskRules();
        }
    }

    /**
     * 尝试自动登录
     */
    async tryAutoLogin(options = {}) {
        const auto = localStorage.getItem('enableAutoLogin') === '1';
        const savedUser = localStorage.getItem('loginUsername');
        const savedPass = localStorage.getItem('loginPassword');
        
        if (!auto || !savedUser || !savedPass) return;
        
        const { timeout = 7000, maxRetries = 0, connectTimeout = 10000, connectRetries = 0 } = options;
        
        try {
            const res = await this.app.apiCall('/login', {
                method: 'POST',
                body: JSON.stringify({ username: savedUser, password: savedPass }),
                timeout,
                maxRetries
            });
            
            if (res?.access_token) {
                localStorage.setItem('access_token', res.access_token);
                if (res.role) {
                    localStorage.setItem('user_role', res.role);
                } else {
                    localStorage.removeItem('user_role');
                }
                if (res.is_read_only) {
                    localStorage.setItem('is_read_only', '1');
                } else {
                    localStorage.removeItem('is_read_only');
                }
                this.toggleAuthButtons(true);
                this.applyReadOnlyMode(!!res.is_read_only, res.role || localStorage.getItem('user_role') || 'admin');

                await this.app.ensureDefaultConnectionConfig();

                // 自动连接使用保存的连接设置
                await this.tryAutoConnect(connectTimeout, connectRetries);
            }
        } catch (e) {
            console.warn('Auto login failed:', e);
        }
    }

    /**
     * 尝试自动连接
     */
    async tryAutoConnect(connectTimeout, connectRetries) {
        const host = localStorage.getItem('conn_host');
        const port = localStorage.getItem('conn_port');
        const clientId = localStorage.getItem('conn_clientId');
        const accountType = localStorage.getItem('conn_accountType') || 'paper';
        
        if (host && port && clientId) {
            try {
                await this.app.apiCall('/connect', {
                    method: 'POST',
                    body: JSON.stringify({
                        host,
                        port: parseInt(port),
                        client_id: parseInt(clientId),
                        paper_trading: accountType === 'paper'
                    }),
                    timeout: connectTimeout,
                    maxRetries: connectRetries
                });
                
                // 连接成功后异步加载数据
                setTimeout(() => {
                    // this.app.loadAccountInfo(); // 改为由 WebSocket account_update 推送
                    this.app.loadOrders();
                    this.app.loadPortfolio();
                    this.app.riskRulesManager.loadRiskRules();
                    this.app.strategiesManager.loadStrategies();
                    this.app.loadLogs();
                }, 200);
            } catch (e) {
                console.warn('Auto-connect failed:', e);
            }
        }
    }

    /**
     * 切换认证按钮显示状态
     */
    toggleAuthButtons(isLoggedIn) {
        const loginBtn = document.getElementById('loginBtn');
        const logoutBtn = document.getElementById('logoutBtn');
        
        if (loginBtn) loginBtn.style.display = isLoggedIn ? 'none' : 'inline-block';
        if (logoutBtn) logoutBtn.style.display = isLoggedIn ? 'inline-block' : 'none';
    }

    /**
     * 退出登录
     */
    logout() {
        localStorage.removeItem('access_token');
        localStorage.removeItem('is_read_only');
        localStorage.removeItem('user_role');
        this.toggleAuthButtons(false);
        this.applyReadOnlyMode(false, 'admin');
        this.app.showAlert('info', '已退出登录');
    }

    /**
     * 检查是否已登录
     */
    isLoggedIn() {
        return !!localStorage.getItem('access_token');
    }

    /**
     * 获取访问令牌
     */
    getAccessToken() {
        return localStorage.getItem('access_token');
    }

    /**
     * 绑定事件监听器
     */
    bindEventListeners() {
        // 登录按钮
        const loginBtn = document.getElementById('loginBtn');
        if (loginBtn) {
            loginBtn.addEventListener('click', () => this.showLoginModal());
        }

        // 登录提交按钮
        const loginSubmitBtn = document.getElementById('loginSubmitBtn');
        if (loginSubmitBtn) {
            loginSubmitBtn.addEventListener('click', () => this.handleLoginSubmit());
        }

        // 密码输入框回车事件
        const loginPasswordInput = document.getElementById('loginPassword');
        if (loginPasswordInput) {
            loginPasswordInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') this.handleLoginSubmit();
            });
        }

        // 退出登录按钮
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn) {
            logoutBtn.addEventListener('click', () => this.logout());
        }
    }
}

// 模块导出
if (typeof module !== 'undefined' && module.exports) {
    module.exports = LoginManager;
} else {
    window.LoginManager = LoginManager;
}