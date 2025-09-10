/**
 * 登录管理模块
 * 负责用户认证、登录状态管理、自动登录等功能
 */
class LoginManager {
    constructor(app) {
        this.app = app;
        this.init();
    }

    /**
     * 初始化登录管理器
     */
    init() {
        this.bindEventListeners();
        this.toggleAuthButtons(!!localStorage.getItem('access_token'));
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
                this.toggleAuthButtons(true);
                
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
-        this.app.checkConnectionStatus();
+        this.app.ibkrManager.checkConnectionStatus();
        
        // 加载基础数据
        this.app.loadStrategies();
        this.app.loadLogs();
        
        // 只在连接成功后加载需要IB连接的数据
        if (this.app.connected) {
            this.app.loadOrders();
            this.app.loadPortfolio();
            this.app.loadRiskRules();
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
                this.toggleAuthButtons(true);
                
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
                    this.app.loadAccountInfo();
                    this.app.loadOrders();
                    this.app.loadPortfolio();
                    this.app.loadRiskRules();
                    this.app.loadStrategies();
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
        this.toggleAuthButtons(false);
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