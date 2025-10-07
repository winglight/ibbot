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

    function formatNumber(value) {
        const num = Number(value);
        if (Number.isFinite(num)) {
            return num.toLocaleString();
        }
        if (value === null || value === undefined) {
            return '';
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
                const detailFragment = this.renderTaskDetails(task, extra);
                if (detailFragment) {
                    li.appendChild(detailFragment);
                } else {
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

            renderTaskDetails(task, details) {
                if (!details || typeof details !== 'object') {
                    return null;
                }
                const taskId = task?.id || '';
                if (taskId === 'realtime-price') {
                    return this.renderRealtimeDetails(details);
                }
                if (taskId === 'dom-depth') {
                    return this.renderDomDetails(details);
                }
                return null;
            },

            renderRealtimeDetails(details) {
                const container = document.createElement('div');
                container.className = 'w-100';
                const list = document.createElement('ul');
                list.className = 'small mb-0 list-unstyled';
                container.appendChild(list);

                let appended = false;
                const appendLine = (text) => {
                    if (!text) return;
                    const li = document.createElement('li');
                    li.textContent = text;
                    list.appendChild(li);
                    appended = true;
                };

                if (details.symbol) {
                    appendLine(`Symbol：${details.symbol}`);
                }

                if (details.wait_seconds !== undefined) {
                    appendLine(`检测窗口：${formatNumber(details.wait_seconds)} 秒`);
                }

                if (details.snapshot) {
                    const snapshot = details.snapshot || {};
                    const parts = [];
                    if (snapshot.timestamp) {
                        parts.push(`时间 ${formatTimestamp(snapshot.timestamp)}`);
                    }
                    if (snapshot.last !== undefined) {
                        parts.push(`Last ${formatNumber(snapshot.last)}`);
                    }
                    if (snapshot.bid !== undefined || snapshot.ask !== undefined) {
                        const bidText = snapshot.bid !== undefined ? formatNumber(snapshot.bid) : '-';
                        const askText = snapshot.ask !== undefined ? formatNumber(snapshot.ask) : '-';
                        parts.push(`Bid ${bidText} / Ask ${askText}`);
                    }
                    appendLine(`快照：${parts.length ? parts.join(' · ') : '无'}`);
                }

                if (details.event) {
                    const eventData = details.event || {};
                    const eventParts = [];
                    if (eventData.timestamp) {
                        eventParts.push(`时间 ${formatTimestamp(eventData.timestamp)}`);
                    }
                    if (eventData.source) {
                        eventParts.push(`来源 ${eventData.source}`);
                    }
                    appendLine(`推送：${eventParts.length ? eventParts.join(' · ') : '未收到'}`);
                }

                if (!appended) {
                    return null;
                }
                return container;
            },

            renderDomDetails(details) {
                const container = document.createElement('div');
                container.className = 'w-100';
                const list = document.createElement('ul');
                list.className = 'small mb-0 list-unstyled';
                container.appendChild(list);

                let appended = false;
                const appendLine = (text) => {
                    if (!text) return;
                    const li = document.createElement('li');
                    li.textContent = text;
                    list.appendChild(li);
                    appended = true;
                };

                if (details.symbol) {
                    appendLine(`Symbol：${details.symbol}`);
                }

                if (details.requested_rows !== undefined) {
                    appendLine(`订阅深度：${formatNumber(details.requested_rows)} 行`);
                }

                if (details.wait_seconds !== undefined) {
                    appendLine(`检测窗口：${formatNumber(details.wait_seconds)} 秒`);
                }

                if (details.snapshot) {
                    const snapshot = details.snapshot || {};
                    const parts = [];
                    if (snapshot.timestamp) {
                        parts.push(`时间 ${formatTimestamp(snapshot.timestamp)}`);
                    }
                    if (snapshot.bid_levels !== undefined || snapshot.ask_levels !== undefined) {
                        const bid = snapshot.bid_levels ?? 0;
                        const ask = snapshot.ask_levels ?? 0;
                        parts.push(`层级 Bid ${formatNumber(bid)} / Ask ${formatNumber(ask)}`);
                    }
                    appendLine(`缓存快照：${parts.length ? parts.join(' · ') : '无'}`);
                }

                if (details.event) {
                    const eventData = details.event || {};
                    const eventParts = [];
                    if (eventData.timestamp) {
                        eventParts.push(`时间 ${formatTimestamp(eventData.timestamp)}`);
                    }
                    if (eventData.source) {
                        eventParts.push(`来源 ${eventData.source}`);
                    }
                    appendLine(`实时推送：${eventParts.length ? eventParts.join(' · ') : '未收到'}`);
                }

                if (details.storage) {
                    const storage = details.storage || {};
                    const storageParts = [];
                    if (storage.total_rows !== undefined) {
                        storageParts.push(`快照 ${formatNumber(storage.total_rows)}`);
                    }
                    if (storage.total_files !== undefined) {
                        storageParts.push(`文件 ${formatNumber(storage.total_files)}`);
                    }
                    if (storage.earliest || storage.latest) {
                        storageParts.push(`覆盖 ${formatTimestamp(storage.earliest)} ~ ${formatTimestamp(storage.latest)}`);
                    }
                    if (storage.error) {
                        storageParts.push(`错误 ${storage.error}`);
                    }
                    appendLine(`落盘：${storageParts.length ? storageParts.join(' · ') : '未发现文件'}`);
                }

                if (details.cache) {
                    const cache = details.cache || {};
                    const cacheParts = [];
                    if (cache.total_snapshots !== undefined) {
                        cacheParts.push(`快照 ${formatNumber(cache.total_snapshots)}`);
                    }
                    if (cache.total_metrics !== undefined) {
                        cacheParts.push(`指标 ${formatNumber(cache.total_metrics)}`);
                    }
                    const metricTimestamp = cache.latest_metric?.timestamp_iso || cache.latest_metric?.timestamp;
                    if (metricTimestamp) {
                        cacheParts.push(`最新指标 ${formatTimestamp(metricTimestamp)}`);
                    }
                    if (cache.error) {
                        cacheParts.push(`错误 ${cache.error}`);
                    }
                    appendLine(`缓存：${cacheParts.length ? cacheParts.join(' · ') : '暂无数据'}`);
                }

                if (details.note) {
                    const note = document.createElement('div');
                    note.className = 'text-info small';
                    note.textContent = details.note;
                    container.appendChild(note);
                    appended = true;
                }

                if (!appended) {
                    return null;
                }
                return container;
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

