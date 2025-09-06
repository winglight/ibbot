# MNQ 盘口失衡与“压单/撤单/假突破”监测系统（IB API）

*——设计思路、NumPy 环形缓冲（可变深度）、风控集成 & Web 面板（纯 JS + CSS）*

> 目标：基于 IB API 的 Level 2（DOM）与 tick-by-tick 成交，构建盘中“订单簿失衡 + 订单流失衡 + 墙单（压/托）行为”的监测模块，用于**过滤假突破、确认趋势延续、动态降杠杆**。本版新增**纯 JavaScript + CSS** 的 Web 面板 UI 与前后端对接样例（无框架、无构建步骤）。

---

## 目录

1. [整体架构](#整体架构)
2. [数据与订阅](#数据与订阅)
3. [内存与数据结构（NumPy 环形缓冲，支持可变深度）](#内存与数据结构numpy-环形缓冲支持可变深度)
4. [核心特征与行为判定](#核心特征与行为判定)
5. [与仓位/风控集成](#与仓位风控集成)
6. [Web 面板 UI：信息架构与交互（纯 JS+CSS）](#web-面板-ui信息架构与交互纯-jscss)
7. [前端实现（JS+CSS）—示例代码](#前端实现jscss示例代码)
8. [后端 API/WS 协议](#后端-apiws-协议)
9. [运行与校验](#运行与校验)
10. [监控指标与告警格式](#监控指标与告警格式)
11. [已知限制与对策 & 安全注意](#已知限制与对策--安全注意)
12. [后续迭代建议](#后续迭代建议)

---

## 整体架构

```mermaid
flowchart LR
    A[IB Gateway/TWS] -->|reqMktDepth/TickByTick| B[数据采集(Async)]
    B --> C[盘口快照重建(10-20Hz)]
    C --> D[NumPy 环形缓冲(可变深度)]
    D --> E[特征工程: OBI/OFI/墙强度/撤单速率/价差]
    E --> F[行为识别: 压/托/拉撤/吸收/假突破评分]
    F --> G[风控/仓位引擎集成]
    F --> H[事件总线]
    H --> I[WebSocket/REST]
    I --> J[Web 面板 (JS+CSS)]
```

---

## 数据与订阅

* **Level 2（DOM）**：`reqMktDepth(contract, numRows=K)`；K 可配（12/20/30），实际返回 ≤ 场内可见深度。
* **Tick-by-Tick**：`reqTickByTickData`（`Last`、`BidAsk`）。
* **采样频率**：统一为 **10–20 Hz**（面板端再二次下采样到 5–10 FPS）。

---

## 内存与数据结构（NumPy 环形缓冲，支持可变深度）

**目标**：低开销、可变深度、上限可控（详见前文方案）。关键数据结构：

```python
WINDOW, MAX_LEVELS, SIDES, FIELDS = 10000, 30, 2, 2
book = np.zeros((WINDOW, SIDES, MAX_LEVELS, FIELDS), dtype=np.int32)  # [:,:,:,0]=price_tick, [:,:,:,1]=size
valid_levels = np.zeros((WINDOW, SIDES), dtype=np.int16)
mid_tick = np.zeros(WINDOW, dtype=np.int32)
ts_ns = np.zeros(WINDOW, dtype=np.int64)
```

* **写入**：按实际可见层数写入；
* **计算**：如 `OBI_k` 用 `k=min(目标K, 实际层数)`；
* **存储**：窗口内实时、原始事件批量落盘 Parquet；RSS 超阈值→缩窗/降采样/近档优先。

---

## 核心特征与行为判定

* **OBI**：k 档两侧量差/总量；
* **OFI**：新增/吃单/撤单/移位的净效应 + 成交方向；
* **墙强度**：1–3 档异常大单（>均值+zσ）的连续性与累积量；
* **撤单速率**：靠近墙位时挂量骤降且几乎无成交；
* **吸收指数**：同价反复成交但可见量不降；
* **假突破启发式**：突破侧出现“撤墙 + 成交不足 + OBI/OFI 快速反向”等。

> 统计启发式≠操纵定性；需成交流/价格交叉验证。

---

## 与仓位/风控集成

* 输出：`trend_score ∈ [-1,1]`、`fake_breakout_prob ∈ [0,1]`、`wall_state ∈ {none, stacking_buy/sell, pulling_buy/sell, absorption}`。
* **动态仓位缩放**：

  * 基础每笔风险 $R_0 = \text{Eq} \times r$；基础合约 $N_0=\lfloor R_0/(点值×止损点数)\rfloor$。
  * 若 `trend_score<0` 或 `fake_breakout_prob>0.6` → $f=0.25$；中性 $f=0.5$；强一致 $f=1.0$。
  * 实际下单 $N=\max(1,\lfloor N_0 f \rfloor)$。
* **事件过滤**：`pulling_*` 与入场方向一致 → 延迟入场 30–60s；`absorption` 且成交不足 → 减半仓/收紧止盈。
* **断路器**：单日亏损 ≥ 3R0 或连亏 ≥3 笔 → 停止交易（盘口模块只能“从紧”，不能绕过）。

---

## Web 面板 UI：信息架构与交互（纯 JS+CSS）

### 布局（单页）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 顶栏：MNQ 盘口监控 | 连接状态● | 合约 | 时区 | FPS | 深度可见度            │
├─────────────────────────────────────────────────────────────────────────────┤
│ 左侧参数栏（可折叠）                                                         │
│  - 采样Hz  - DOM行数K  - 窗口帧数N  - TickSize   - 近档near_levels        │
│  - z阈值   - OBI_k    - OFI窗口(s) - 吸收阈值α  - 延迟入场(s)            │
│  - 预设：保守/标准/激进   [保存参数]  [启动 ▶]  [停止 ■]  [导出CSV]        │
├─────────────────────────────────────────────────────────────────────────────┤
│ 主区：两行                                                                  │
│  行1：K线+中位价/价差（<canvas>）、OBI/OFI 折线（<canvas>）                │
│  行2：深度热力/梯形（<canvas>） + 告警表（可筛选）                          │
│  底部：推荐操作条（按钮：延迟入场/减仓/收紧TP/不追/平半仓…）                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 交互要点

* **参数设置**：即时校验（范围/单位）；保存后持久到 `localStorage` 并 `PUT /api/config`。
* **启动/停止**：调用 `POST /api/monitor/start|stop`；启动后建立 `WebSocket`；停止时关闭订阅并保留最后一帧。
* **告警**：表格分页、按级别/类型/方向过滤；`ACK` 按钮调用 `POST /api/alerts/{id}/ack`；点击行展开上下文片段（前后 ±10s 的采样）。
* **推荐操作**：按钮不会直接下单，仅 `POST /api/recommendations/execute` 发送建议（默认需人工确认）。
* **性能**：绘图节流到 5–10 FPS；深度热力 1–2 Hz；视图不在屏时暂停绘制（`IntersectionObserver`）。
* **样式**：CSS 变量支持暗黑/明亮主题一键切换。

---

## 前端实现（JS+CSS）—示例代码

> 仅示例关键片段：无需构建工具。实际项目可拆分文件并增强错误处理与样式。

### 1) `index.html`

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>MNQ 盘口监控</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="stylesheet" href="./styles.css" />
</head>
<body>
  <header class="topbar">
    <h1>MNQ 盘口监控</h1>
    <div class="status">
      <span id="connDot" class="dot offline"></span><span id="connText">未连接</span>
      <span id="fps">FPS: -</span>
      <span id="depthVis">深度: -</span>
      <button id="themeBtn">切换主题</button>
    </div>
  </header>

  <section class="layout">
    <aside class="panel">
      <form id="cfgForm">
        <h3>参数设置</h3>
        <label>采样Hz <input type="number" name="sampleHz" value="20" min="1" max="60"></label>
        <label>DOM行数K <input type="number" name="numRows" value="20" min="5" max="30"></label>
        <label>窗口帧数N <input type="number" name="windowFrames" value="10000" min="1000" max="50000"></label>
        <label>Tick Size <input type="number" step="0.01" name="tickSize" value="0.25"></label>
        <label>近档 near_levels <input type="number" name="nearLevels" value="3" min="1" max="10"></label>
        <label>z阈值 <input type="number" step="0.1" name="zThreshold" value="3.0" min="1" max="8"></label>
        <label>OBI_k <input type="number" name="obiK" value="5" min="1" max="30"></label>
        <label>OFI窗口(s) <input type="number" name="ofiWindowSec" value="2" min="1" max="10"></label>
        <label>吸收阈值α <input type="number" step="0.1" name="absorptionAlpha" value="0.6" min="0" max="1"></label>
        <label>延迟入场(s) <input type="number" name="delayEntrySec" value="45" min="0" max="180"></label>
        <div class="row">
          <select id="preset">
            <option value="standard">标准</option>
            <option value="conservative">保守</option>
            <option value="aggressive">激进</option>
          </select>
          <button type="button" id="saveBtn">保存参数</button>
        </div>
        <div class="row">
          <button type="button" id="startBtn">启动 ▶</button>
          <button type="button" id="stopBtn" disabled>停止 ■</button>
          <button type="button" id="exportBtn">导出CSV</button>
        </div>
      </form>

      <div class="suggest">
        <h3>推荐操作</h3>
        <div class="buttons">
          <button data-action="delay_entry">延迟入场</button>
          <button data-action="reduce_position">减仓</button>
          <button data-action="tighten_tp">收紧TP</button>
          <button data-action="no_chase">不追</button>
          <button data-action="flat_half">平半仓</button>
        </div>
      </div>
    </aside>

    <main class="main">
      <div class="charts">
        <canvas id="candle" height="240"></canvas>
        <canvas id="obiOfi" height="160"></canvas>
        <canvas id="depth" height="200"></canvas>
      </div>

      <div class="alerts">
        <div class="toolbar">
          <select id="filterLevel">
            <option value="">全部级别</option><option>warn</option><option>crit</option><option>info</option>
          </select>
          <select id="filterType">
            <option value="">全部类型</option>
            <option>pulling</option><option>stacking</option><option>absorption</option>
            <option>false_breakout</option><option>vol_spike</option><option>spread_jump</option>
          </select>
        </div>
        <table>
          <thead><tr><th>时间</th><th>级别</th><th>类型</th><th>方向</th><th>描述</th><th>推荐</th><th>操作</th></tr></thead>
          <tbody id="alertsBody"></tbody>
        </table>
      </div>
    </main>
  </section>

  <script src="./app.js"></script>
</body>
</html>
```

### 2) `styles.css`（核心样式&暗黑主题）

```css
:root {
  --bg: #0f172a; --fg: #e2e8f0; --muted: #94a3b8;
  --card: #111827; --accent: #22d3ee; --warn: #f59e0b; --crit: #ef4444; --ok: #10b981;
}
.light {
  --bg:#f8fafc; --fg:#0f172a; --muted:#475569; --card:#ffffff;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 ui-sans-serif,system-ui; }
.topbar { display:flex; align-items:center; justify-content:space-between; padding:10px 16px; border-bottom:1px solid #334155; }
.dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:6px; background:#64748b; }
.dot.online { background: var(--ok); } .dot.offline { background:#64748b; }
.layout { display:grid; grid-template-columns:320px 1fr; gap:12px; padding:12px; }
.panel { background:var(--card); padding:12px; border-radius:12px; }
.panel label { display:flex; justify-content:space-between; margin:6px 0; gap:8px; }
.panel input, select, button { padding:6px 8px; border-radius:8px; border:1px solid #334155; background:transparent; color:var(--fg); }
.row { display:flex; gap:8px; margin-top:8px; }
.suggest .buttons { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.main .charts { display:grid; gap:10px; grid-template-rows:240px 160px 200px; }
.charts canvas { width:100%; background:#0b1220; border:1px solid #1f2937; border-radius:12px; }
.alerts { margin-top:12px; background:var(--card); border-radius:12px; padding:8px; }
.alerts table { width:100%; border-collapse:collapse; }
.alerts th, .alerts td { padding:6px 8px; border-bottom:1px solid #1f2937; color:var(--fg); }
.badge { padding:2px 6px; border-radius:999px; font-size:12px; }
.badge.warn { background:rgba(245,158,11,.15); color:var(--warn); }
.badge.crit { background:rgba(239,68,68,.15); color:var(--crit); }
.badge.info { background:rgba(34,211,238,.15); color:var(--accent); }
button.primary { background:var(--accent); color:#001; border:0; }
```

### 3) `app.js`（参数、启动/停止、WS、绘图）

```js
// 简易状态
let ws = null, running = false, lastFrameTs = 0, frames = 0;
let cfg = loadCfg();
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

// 主题
$('#themeBtn').onclick = () => document.body.classList.toggle('light');

// 参数加载/保存
function loadCfg() {
  const saved = localStorage.getItem('mnq_cfg');
  return saved ? JSON.parse(saved) : {
    sampleHz:20, numRows:20, windowFrames:10000, tickSize:0.25,
    nearLevels:3, zThreshold:3.0, obiK:5, ofiWindowSec:2, absorptionAlpha:0.6,
    delayEntrySec:45, recMode:'standard'
  };
}
function applyCfgToForm() {
  const f = $('#cfgForm');
  Object.entries(cfg).forEach(([k,v]) => { if (k in f.elements) f.elements[k].value = v; });
}
applyCfgToForm();

$('#saveBtn').onclick = async () => {
  const f = $('#cfgForm');
  cfg = Object.fromEntries(new FormData(f).entries());
  // 数值字段转型
  ['sampleHz','numRows','windowFrames','nearLevels','obiK','ofiWindowSec','delayEntrySec'].forEach(k=>cfg[k]=parseInt(cfg[k]));
  ['tickSize','zThreshold','absorptionAlpha'].forEach(k=>cfg[k]=parseFloat(cfg[k]));
  cfg.recMode = $('#preset').value || cfg.recMode;
  localStorage.setItem('mnq_cfg', JSON.stringify(cfg));
  await fetch('/api/config', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)});
  toast('参数已保存');
};

// 启动/停止
$('#startBtn').onclick = async () => {
  if (running) return;
  await fetch('/api/monitor/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
  openWS();
};
$('#stopBtn').onclick = async () => {
  if (!running) return;
  await fetch('/api/monitor/stop', {method:'POST'});
  closeWS();
};

function openWS() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/stream');
  ws.onopen = () => { running = true; $('#connDot').className='dot online'; $('#connText').textContent='已连接'; $('#stopBtn').disabled=false; };
  ws.onclose = () => { running = false; $('#connDot').className='dot offline'; $('#connText').textContent='未连接'; $('#stopBtn').disabled=true; };
  ws.onmessage = onMessage;
}
function closeWS() { if (ws) { ws.close(); ws = null; } }

// 推荐操作按钮
$$('.suggest .buttons button').forEach(btn=>{
  btn.onclick = async () => {
    const action = btn.dataset.action;
    await fetch('/api/recommendations/execute', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})});
    toast(`已发送建议：${action}`);
  };
});

// 告警过滤
$('#filterLevel').onchange = () => renderAlerts();
$('#filterType').onchange = () => renderAlerts();

// 数据缓存与绘图
const buffer = {
  candle: [], // {t, o,h,l,c}
  obiOfi: [], // {t, obi, ofi}
  depth: [],  // 简化为若干行强度 [{t, asks:[sizes...], bids:[sizes...]}]
  alerts: []  // AlertItem
};

const canv = { candle: $('#candle'), obiOfi: $('#obiOfi'), depth: $('#depth') };
const ctx   = { candle: canv.candle.getContext('2d'), obiOfi: canv.obiOfi.getContext('2d'), depth: canv.depth.getContext('2d') };

let lastPaint = 0;
function raf() {
  requestAnimationFrame(raf);
  const now = performance.now();
  // 限制 8 FPS
  if (now - lastPaint < 1000/8) return;
  lastPaint = now; draw();
}
raf();

function onMessage(ev) {
  const msg = JSON.parse(ev.data);
  if (msg.type === 'status') {
    $('#fps').textContent = `FPS: ${msg.fps?.toFixed?.(1) ?? '-'}`;
  } else if (msg.type === 'chart') {
    // 预期 payload: { candle, obiOfi, depth }
    if (msg.candle) buffer.candle.push(...msg.candle);
    if (msg.obiOfi) buffer.obiOfi.push(...msg.obiOfi);
    if (msg.depth)  buffer.depth.push(...msg.depth);
    trimBuffers();
  } else if (msg.type === 'alert') {
    buffer.alerts.unshift(msg.payload);
    renderAlerts();
  } else if (msg.type === 'features') {
    // 可用于顶栏显示深度可见度等
    if (typeof msg.depth_rows_seen === 'number') $('#depthVis').textContent = `深度: ${msg.depth_rows_seen}`;
  }
}

function trimBuffers(max=500) {
  ['candle','obiOfi','depth'].forEach(k => { if (buffer[k].length > max) buffer[k].splice(0, buffer[k].length - max); });
  if (buffer.alerts.length > 1000) buffer.alerts.splice(500);
}

// 简易绘图（不引入第三方库）
function drawCandle(ctx, data) {
  const W = ctx.canvas.width, H = ctx.canvas.height;
  ctx.clearRect(0,0,W,H);
  if (data.length < 2) return;
  const xs = data.map(d=>d.t), minT = xs[0], maxT = xs[xs.length-1];
  const minP = Math.min(...data.map(d=>d.l)), maxP = Math.max(...data.map(d=>d.h));
  const x = t => (t-minT)/(maxT-minT) * (W-20) + 10;
  const y = p => H - (p-minP)/(maxP-minP) * (H-20) - 10;

  data.forEach(d=>{
    // wick
    ctx.strokeStyle = '#94a3b8';
    ctx.beginPath(); ctx.moveTo(x(d.t), y(d.h)); ctx.lineTo(x(d.t), y(d.l)); ctx.stroke();
    // body
    const up = d.c >= d.o; ctx.fillStyle = up ? '#10b981' : '#ef4444';
    const bw = Math.max(2, (W-20)/data.length * .7);
    ctx.fillRect(x(d.t)-bw/2, y(Math.max(d.o,d.c)), bw, Math.max(1, Math.abs(y(d.o)-y(d.c))));
  });
}
function drawLines(ctx, data) {
  const W = ctx.canvas.width, H = ctx.canvas.height;
  ctx.clearRect(0,0,W,H);
  if (data.length < 2) return;
  const xs = data.map(d=>d.t), minT = xs[0], maxT = xs[xs.length-1];
  const minY = Math.min(...data.map(d=>Math.min(d.obi, d.ofi))), maxY = Math.max(...data.map(d=>Math.max(d.obi, d.ofi)));
  const x = t => (t-minT)/(maxT-minT) * (W-20) + 10;
  const y = v => H - (v-minY)/(maxY-minY) * (H-20) - 10;

  ctx.strokeStyle = '#22d3ee'; ctx.beginPath();
  data.forEach((d,i)=>{ i?ctx.lineTo(x(d.t), y(d.obi)):ctx.moveTo(x(d.t), y(d.obi)); }); ctx.stroke();
  ctx.strokeStyle = '#f59e0b'; ctx.beginPath();
  data.forEach((d,i)=>{ i?ctx.lineTo(x(d.t), y(d.ofi)):ctx.moveTo(x(d.t), y(d.ofi)); }); ctx.stroke();
}
function drawDepth(ctx, data) {
  const W = ctx.canvas.width, H = ctx.canvas.height;
  ctx.clearRect(0,0,W,H);
  if (!data.length) return;
  const last = data[data.length-1]; // 仅绘制最近一次（热力）
  const levels = Math.max(last.asks.length, last.bids.length);
  const cellW = Math.max(2, (W-20)/levels);
  const maxSize = Math.max(1, ...last.asks, ...last.bids);

  function cell(x, val, color) {
    const alpha = Math.min(1, val / maxSize);
    ctx.fillStyle = `rgba(${color},${alpha})`;
    ctx.fillRect(x, 10, cellW-1, (H-20)/2 - 2);
  }
  // asks on top
  for (let i=0;i<last.asks.length;i++) cell(10+i*cellW, last.asks[i], '239,68,68');   // red
  // bids bottom
  for (let i=0;i<last.bids.length;i++) {
    ctx.save(); ctx.translate(0, H/2);
    const alpha = Math.min(1, last.bids[i]/maxSize);
    ctx.fillStyle = `rgba(16,185,129,${alpha})`;
    ctx.fillRect(10+i*cellW, 0, cellW-1, (H-20)/2 - 2);
    ctx.restore();
  }
}
function draw() {
  drawCandle(ctx.candle, buffer.candle);
  drawLines(ctx.obiOfi, buffer.obiOfi);
  drawDepth(ctx.depth, buffer.depth);
}

// 告警渲染
function renderAlerts() {
  const lvl = $('#filterLevel').value, typ = $('#filterType').value;
  const tbody = $('#alertsBody'); tbody.innerHTML = '';
  buffer.alerts
    .filter(a => (!lvl || a.level===lvl) && (!typ || a.type===typ))
    .slice(0, 200)
    .forEach(a=>{
      const tr = document.createElement('tr');
      const badge = l => `<span class="badge ${l}">${l}</span>`;
      tr.innerHTML = `
        <td>${new Date(a.ts).toLocaleTimeString()}</td>
        <td>${badge(a.level)}</td>
        <td>${a.type}</td>
        <td>${a.side||'-'}</td>
        <td>${a.message||''}</td>
        <td>${a.suggestion||'-'}</td>
        <td><button data-id="${a.id}" class="primary ackBtn">ACK</button></td>
      `;
      tbody.appendChild(tr);
    });
  $$('.ackBtn').forEach(b=>{
    b.onclick = async () => {
      await fetch(`/api/alerts/${b.dataset.id}/ack`, {method:'POST'});
      b.disabled = true; b.textContent = '已确认';
    };
  });
}

// 简易提示
function toast(text) {
  const el = document.createElement('div');
  el.textContent = text; el.style.position='fixed'; el.style.right='12px'; el.style.bottom='12px';
  el.style.background='rgba(34,211,238,.15)'; el.style.border='1px solid #22d3ee'; el.style.padding='8px 12px'; el.style.borderRadius='8px';
  document.body.appendChild(el); setTimeout(()=>el.remove(), 1800);
}

// 导出 CSV（只导出告警）
$('#exportBtn').onclick = () => {
  const rows = [['ts','level','type','side','message','suggestion']];
  buffer.alerts.forEach(a=> rows.push([a.ts,a.level,a.type,a.side||'',a.message||'',a.suggestion||'']));
  const csv = rows.map(r=>r.map(x=>`"${String(x).replaceAll('"','""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], {type:'text/csv'}); const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = `alerts_${Date.now()}.csv`; a.click(); URL.revokeObjectURL(url);
};
```

---

## 后端 API/WS 协议

* **REST**

  * `GET  /api/config` → 当前参数
  * `PUT  /api/config` → 更新参数
  * `POST /api/monitor/start|stop` → 启动/停止监控
  * `GET  /api/alerts?since=&level=&type=&limit=` → 拉取告警
  * `POST /api/alerts/{id}/ack` → 确认
  * `POST /api/recommendations/execute` → 发送建议（不直接下单）

* **WebSocket `/ws/stream`**

  * `status`：`{state,fps,latency_ms,depth_rows_seen,...}`
  * `chart`：`{candle:[{t,o,h,l,c}], obiOfi:[{t,obi,ofi}], depth:[{t,asks:[],bids:[]}]}`
  * `features`：`{ts,obi,ofi,spread,wall_strength_*...}`
  * `alert`：载荷为 `AlertItem`

---

## 运行与校验

1. **后端**：确保 IB 行情开通、API 连接可用；配置 `numRows / sampleHz / windowFrames` 等。
2. **前端**：将 `index.html / styles.css / app.js` 放置于静态目录；同域提供 REST/WS。
3. **检查性能**：FPS、延迟、内存；必要时启用“低资源模式”（降低绘制频率/热力二次下采样）。
4. **复盘验证**：Parquet 回放，评估“假突破过滤率/延迟入场的收益-风险改善”。

---

## 监控指标与告警格式

* **系统**：`ui_fps`, `ws_backpressure`, `proc_rss_mb`, `gc_pause_ms`, `depth_rows_seen`, `dropped_frames_total`
* **交易**：`dom_obi5`, `dom_ofi`, `dom_wall_strength_{buy,sell}`, `dom_pull_rate_{buy,sell}`, `fake_breakout_prob`, `trend_score`
* **告警 JSON 示例**

```json
{
  "ts": 1694000001000,
  "symbol": "MNQ",
  "level": "warn",
  "type": "pulling",
  "side": "sell",
  "metrics": { "obi5": -0.42, "pull_rate": 0.81, "spread": 1.0 },
  "message": "Sell pulling near L1, low prints.",
  "suggestion": "delay_entry"
}
```

---

## 已知限制与对策 & 安全注意

* **可见深度波动**：后端 `valid_levels` 自适应，面板仅展示近档 + 下采样热力。
* **IBKR 节流/聚合**：统一采样 + 滑窗统计；前端绘制限速。
* **无订单队列/ID**：以补单速率 + 成交流交叉验证；
* **推荐操作默认需确认**：若开启自动执行，需**风控白名单 + 二次确认**；
* **模块定位**：用于**过滤/确认/降杠杆**，非单独开仓触发器。

---

## 后续迭代建议

1. **学习化评分器**：LGBM/Logit 训练 `fake_breakout_prob`/`trend_score`（特征：OBI/OFI/墙/价差/成交跟进/结构位）。
2. **跨尺度融合**：与 M5 结构做两阶段判定（先结构、后盘口）。
3. **自适应阈值**：随 ATR/RV 动态收紧；事件窗口（如数据/FOMC）自动降风险。
4. **可视化升级**：K 线与深度热力联动缩放、跨品种对比视图。

---

**一句话总结**：后端以 **NumPy 环形缓冲（可变深度）** 控内存与吞吐，前端用**纯 JS + CSS** 搭建单页面板（参数设置、启动/停止、告警列表、推荐操作与轻量绘图），将盘口线索转化为**可执行的风险过滤与仓位决策**。
