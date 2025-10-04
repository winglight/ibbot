(function (global) {
    const root = global || {};
    const document = root.document;
    if (!document) return;

    const RUNBOOK_ENDPOINT = '/api/runbooks/websocket-remediation';

    const RUNBOOK_STATUS_LABELS = {
        idle: '就绪',
        running: '执行中',
        success: '已完成',
        warning: '完成（含提示）',
        error: '执行失败',
    };

    const RUNBOOK_STATUS_CLASSES = {
        idle: ['bg-secondary'],
        running: ['bg-info'],
        success: ['bg-success'],
        warning: ['bg-warning', 'text-dark'],
        error: ['bg-danger'],
    };

    const TASK_STATUS_LABELS = {
        pending: '待执行',
        running: '执行中',
        success: '完成',
        warning: '完成（提示）',
        error: '失败',
    };

    const TASK_STATUS_CLASSES = {
        pending: ['bg-secondary'],
        running: ['bg-info'],
        success: ['bg-success'],
        warning: ['bg-warning', 'text-dark'],
        error: ['bg-danger'],
    };

    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function formatTimestamp(value) {
        if (!value) return '';
        try {
            const date = new Date(value);
            if (!Number.isNaN(date.getTime())) {
                return date.toLocaleString();
            }
        } catch (error) {
            console.warn('Failed to format timestamp', error);
        }
        return String(value);
    }

    function createRunbookManager() {
        const card = document.getElementById('websocketRunbookCard');
        const statusBadge = document.getElementById('websocketRunbookStatusBadge');
        const summary = document.getElementById('websocketRunbookSummary');
        const meta = document.getElementById('websocketRunbookMeta');
        const tasksList = document.getElementById('websocketRunbookTasks');
        const startButton = document.getElementById('startWebsocketRunbookBtn');

        if (!card || !statusBadge || !summary || !tasksList || !startButton || !meta) {
            return null;
        }

        return {
            pollingHandle: null,
            loading: false,

            init() {
                startButton.addEventListener('click', (event) => {
                    event.preventDefault();
                    this.start();
                });
                this.loadStatus();
            },

            async loadStatus() {
                try {
                    const runbook = await this.fetchStatus();
                    this.render(runbook);
                    if (runbook.status === 'running') {
                        this.schedulePoll();
                    }
                } catch (error) {
                    console.warn('Failed to load runbook status', error);
                    summary.textContent = '任务状态加载失败，请稍后重试。';
                }
            },

            async fetchStatus() {
                const response = await fetch(RUNBOOK_ENDPOINT);
                if (!response.ok) {
                    throw new Error(`Failed to load status: ${response.status}`);
                }
                const payload = await response.json();
                return payload?.runbook || payload;
            },

            async start() {
                if (this.loading) return;
                this.setLoading(true);
                try {
                    const response = await fetch(`${RUNBOOK_ENDPOINT}/start`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                    });
                    if (!response.ok) {
                        if (response.status === 409) {
                            const current = await response.json();
                            const runbook = current?.runbook || current;
                            this.render(runbook);
                            this.schedulePoll();
                            return;
                        }
                        throw new Error(`Failed to start runbook: ${response.status}`);
                    }
                    const payload = await response.json();
                    const runbook = payload?.runbook || payload;
                    this.render(runbook);
                    this.schedulePoll(true);
                } catch (error) {
                    console.error('Failed to start runbook', error);
                    summary.textContent = '任务启动失败，请稍后重试。';
                } finally {
                    this.setLoading(false);
                }
            },

            setLoading(state) {
                this.loading = state;
                startButton.disabled = state;
                if (state) {
                    startButton.classList.add('disabled');
                } else {
                    startButton.classList.remove('disabled');
                }
            },

            schedulePoll(immediate = false) {
                this.clearPoll();
                const delay = immediate ? 500 : 1500;
                this.pollingHandle = root.setTimeout(() => {
                    this.loadStatus();
                }, delay);
            },

            clearPoll() {
                if (this.pollingHandle) {
                    root.clearTimeout(this.pollingHandle);
                    this.pollingHandle = null;
                }
            },

            render(runbook) {
                if (!runbook) {
                    summary.textContent = '暂无任务信息。';
                    return;
                }

                this.clearPoll();

                const status = runbook.status || 'idle';
                const classes = RUNBOOK_STATUS_CLASSES[status] || ['bg-secondary'];
                statusBadge.className = 'badge';
                statusBadge.classList.add(...classes);
                statusBadge.textContent = RUNBOOK_STATUS_LABELS[status] || status;

                summary.textContent = runbook.description || 'WebSocket 数据修复任务。';

                const metaParts = [];
                if (runbook.run_id) {
                    metaParts.push(`任务 ID：${runbook.run_id}`);
                }
                if (runbook.started_at) {
                    metaParts.push(`开始时间：${formatTimestamp(runbook.started_at)}`);
                }
                if (runbook.completed_at) {
                    metaParts.push(`完成时间：${formatTimestamp(runbook.completed_at)}`);
                }
                meta.textContent = metaParts.join(' · ');

                if (status === 'running') {
                    this.schedulePoll();
                }

                const tasks = Array.isArray(runbook.tasks) ? runbook.tasks : [];
                if (!tasks.length) {
                    tasksList.innerHTML = '<li class="list-group-item text-muted">暂无任务</li>';
                    return;
                }

                const fragments = tasks.map((task) => this.renderTask(task));
                tasksList.innerHTML = '';
                fragments.forEach((fragment) => {
                    tasksList.appendChild(fragment);
                });
            },

            renderTask(task) {
                const li = document.createElement('li');
                li.className = 'list-group-item d-flex flex-column gap-1';

                const header = document.createElement('div');
                header.className = 'd-flex justify-content-between align-items-start gap-3 flex-wrap';

                const title = document.createElement('div');
                title.innerHTML = `<strong>${escapeHtml(task.title || task.id || '任务')}</strong>`;
                if (task.description) {
                    const description = document.createElement('div');
                    description.className = 'text-muted small';
                    description.textContent = task.description;
                    title.appendChild(description);
                }

                const status = task.status || 'pending';
                const badge = document.createElement('span');
                badge.className = 'badge';
                const classes = TASK_STATUS_CLASSES[status] || ['bg-secondary'];
                badge.classList.add(...classes);
                badge.textContent = TASK_STATUS_LABELS[status] || status;

                header.appendChild(title);
                header.appendChild(badge);
                li.appendChild(header);

                const details = task.details || {};
                const message = details.message;
                if (message) {
                    const messageEl = document.createElement('div');
                    messageEl.className = 'text-muted small';
                    messageEl.textContent = message;
                    li.appendChild(messageEl);
                }

                const extra = { ...details };
                delete extra.message;
                const keys = Object.keys(extra);
                if (keys.length) {
                    const pre = document.createElement('pre');
                    pre.className = 'small bg-light border rounded p-2 mb-0';
                    try {
                        pre.textContent = JSON.stringify(extra, null, 2);
                    } catch (error) {
                        pre.textContent = String(extra);
                    }
                    li.appendChild(pre);
                }

                const metaParts = [];
                if (task.started_at) {
                    metaParts.push(`开始：${formatTimestamp(task.started_at)}`);
                }
                if (task.completed_at) {
                    metaParts.push(`结束：${formatTimestamp(task.completed_at)}`);
                }
                if (metaParts.length) {
                    const metaEl = document.createElement('div');
                    metaEl.className = 'text-muted small';
                    metaEl.textContent = metaParts.join(' | ');
                    li.appendChild(metaEl);
                }

                return li;
            },
        };
    }

    document.addEventListener('DOMContentLoaded', () => {
        const manager = createRunbookManager();
        if (manager) {
            manager.init();
        }
    });
})(window);

