/* OpsPilot 前端：原生 JS，无构建、无 CDN。
   - 驾驶舱：KPI / 趋势 / 对比 / 告警
   - 工作台：SSE 流式对话 + 执行轨迹 + 报告卡片
   - 报告中心：历史报告
   图表全部用手写 SVG，保证内网/离线可用。 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const PALETTE = ["#ea580c", "#2563eb", "#0f9d6e", "#9333ea", "#d97706", "#0891b2"];

const state = {
  view: "dashboard",
  sessionId: null,
  runId: null,
  streaming: false,
  staticMode: new URLSearchParams(location.search).get("static") === "1",
  runs: [],
  reports: [],
  dashboard: null,
  kpis: null,
  trendSig: "",
  compareSig: "",
  anomalySig: "",
  alarmSig: "",
  dashboardStream: null,
  budget: null,
  traceItems: new Map(),
  activityBubble: null,
  runToolNames: [],
  runToolResults: [],
  viewingRunId: null,
};

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindNav();
  bindComposer();
  bindExamples();
  $("#new-investigation").addEventListener("click", () => {
    resetWorkbench();
    switchView("workbench");
    $("#input").focus();
  });
  $("#refresh-dashboard").addEventListener("click", async () => {
    await loadDashboard();
    showToast("已刷新至最新数据");
  });
  $("#back-to-live").addEventListener("click", () => {
    if (state.runId) showRunTrace(state.runId);
  });
  $("#trace-history-back").addEventListener("click", () => {
    if (state.runId) showRunTrace(state.runId);
  });
  await checkHealth();
  await Promise.all([loadSessions(), loadReports()]);
  if (state.staticMode) {
    // 截图/打印模式：不开长连接，只取一次快照
    await loadDashboard();
  } else {
    connectDashboard();
  }

  // 深链：/#/run/<run_id> 或 /#/session/<session_id>（便于分享与演示）
  const m = location.hash.match(/#\/run\/([A-Za-z0-9_]+)/);
  const ms = location.hash.match(/#\/session\/([A-Za-z0-9_]+)/);
  if (m) openRun(m[1]);
  else if (ms) openSession(ms[1]);
  else if (location.hash.startsWith("#/reports")) switchView("reports");
}

/* ------------------------------------------------------------------ 实时看板（SSE） */

function connectDashboard() {
  if (!window.EventSource) {
    loadDashboard();
    return;
  }
  const es = new EventSource("/api/dashboard/stream?days=30");
  state.dashboardStream = es;

  es.addEventListener("dashboard", (e) => {
    try {
      applyDashboard(JSON.parse(e.data));
    } catch (err) {
      console.error("dashboard event", err);
    }
  });

  es.addEventListener("stream_error", (e) => {
    let msg = "看板推送异常";
    try {
      msg = JSON.parse(e.data).message || msg;
    } catch {
      /* ignore */
    }
    showToast(msg);
    setSync("数据源异常，重试中…", false);
  });

  // 连接断开/重连（EventSource 内置事件）
  es.addEventListener("error", () => setSync("连接中断，浏览器自动重连中…", false));
  es.onopen = () => setSync("已连接，等待数据…", true);
}

function applyDashboard(data) {
  state.dashboard = data;
  renderKpis(data.kpis);
  renderTrend(data.unit_energy_trend);
  renderCompare(data.workshop_compare);
  renderAnomalies(data.anomalies, data.data_quality);
  renderAlarms(data.alarms_by_workshop);
  $("#page-sub").textContent = `统计区间 ${data.period.start} ~ ${data.period.end}`;
  const time = data.generated_at ? new Date(data.generated_at).toLocaleTimeString("zh-CN", { hour12: false }) : "";
  const label = state.staticMode ? "快照" : "实时";
  setSync(`${label} · ${time} · ${data.data_version || ""}`, true);
  if (data.reason === "changed") showToast("检测到数据变更，看板已更新");
}

function setSync(text, ok) {
  const dot = $("#sync-dot");
  if (dot) dot.className = `live-dot ${ok ? "ok" : "bad"}`;
  const el = $("#sync-text");
  if (el) el.textContent = text;
}

let toastTimer = null;
function showToast(message) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ------------------------------------------------------------------ 导航 */

function bindNav() {
  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
}

const PAGE_META = {
  dashboard: ["业务驾驶舱", "生产 · 能耗 · 设备数据统一视图"],
  workbench: ["AI 调查工作台", "自然语言提问 → 自主取证 → 可追溯报告"],
  reports: ["报告中心", "历史调查结论与证据"],
};

function switchView(name) {
  state.view = name;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  const [title, sub] = PAGE_META[name] || ["", ""];
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
}

/* ------------------------------------------------------------------ 健康检查 */

async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const h = await r.json();
    $("#llm-pill").textContent = `模型 ${h.model}`;
    const dot = $("#status-dot");
    if (h.llm_configured && h.database.ready) {
      dot.className = "status-dot ok";
      $("#model-status").textContent = "模型与数据源已就绪";
    } else if (!h.llm_configured) {
      dot.className = "status-dot bad";
      $("#model-status").textContent = "未配置模型密钥（可离线演示）";
    } else {
      dot.className = "status-dot bad";
      $("#model-status").textContent = "数据库未生成";
    }
  } catch (e) {
    $("#model-status").textContent = "服务未响应";
  }
}

/* ------------------------------------------------------------------ 驾驶舱 */

async function loadDashboard() {
  try {
    const r = await fetch("/api/dashboard?days=30", { cache: "no-store" });
    if (!r.ok) throw new Error(await r.text());
    applyDashboard(await r.json());
  } catch (e) {
    console.error(e);
    $("#kpis").innerHTML = `<div class="anomaly-list-empty">驾驶舱数据加载失败：${esc(String(e.message || e))}</div>`;
  }
}

function deltaHtml(k) {
  if (k.delta_pct === null || k.delta_pct === undefined) return "<span>本期</span>";
  const up = k.delta_pct >= 0;
  const good = k.inverse ? !up : up;
  return `<span class="${good ? "delta-good" : "delta-bad"}">${up ? "▲" : "▼"} ${Math.abs(k.delta_pct).toFixed(1)}%</span><span>较上期</span>`;
}

function kpiCardHtml(k) {
  return `<div class="kpi ${k.highlight ? "highlight" : ""}" data-kpi="${esc(k.key)}">
    <div class="kpi-label">${esc(k.label)}${k.highlight ? '<span class="kpi-tag">核心</span>' : ""}</div>
    <div class="kpi-value"><span class="kv"></span><span class="kpi-unit"></span></div>
    <div class="kpi-delta" data-role="delta"></div>
  </div>`;
}

function renderKpis(kpis) {
  const el = $("#kpis");
  const prev = new Map((state.kpis || []).map((k) => [k.key, k]));
  if (!el.querySelector("[data-kpi]") || el.children.length !== kpis.length) {
    el.innerHTML = kpis.map(kpiCardHtml).join("");
  }
  kpis.forEach((k) => {
    const card = el.querySelector(`[data-kpi="${k.key}"]`);
    if (!card) return;
    $(".kv", card).textContent = Number(k.value).toLocaleString("zh-CN", { maximumFractionDigits: 3 });
    $(".kpi-unit", card).textContent = k.unit || "";
    $('[data-role="delta"]', card).innerHTML = deltaHtml(k);
    const before = prev.get(k.key);
    if (before && (before.value !== k.value || before.delta_pct !== k.delta_pct)) {
      card.classList.remove("flash");
      void card.offsetWidth; // 重启动画
      card.classList.add("flash");
    }
  });
  state.kpis = kpis;
}

function renderTrend(trend) {
  if (!trend || !trend.dates.length) {
    $("#trend-chart").innerHTML = `<div class="anomaly-list-empty">暂无数据</div>`;
    return;
  }
  const sig = JSON.stringify(trend);
  if (sig === state.trendSig) return;
  state.trendSig = sig;
  $("#trend-legend").innerHTML = trend.series
    .map((s, i) => `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(s.name)}</span>`)
    .join("");
  $("#trend-chart").innerHTML = lineChart(trend.dates, trend.series, { height: 268 });
}

function renderCompare(cmp) {
  if (!cmp || !cmp.workshops.length) {
    $("#compare-chart").innerHTML = `<div class="anomaly-list-empty">暂无数据</div>`;
    return;
  }
  const sig = JSON.stringify(cmp);
  if (sig === state.compareSig) return;
  state.compareSig = sig;
  $("#compare-chart").innerHTML = groupedBarChart(cmp.workshops, cmp.previous, cmp.current, { height: 240 });
}

function renderAnomalies(anomalies, dataQuality = []) {
  const el = $("#anomalies");
  const sig = JSON.stringify([anomalies, dataQuality]);
  if (sig === state.anomalySig) return;
  state.anomalySig = sig;
  const dq = (dataQuality || [])
    .map(
      (d) => `<div class="anomaly warn">
        <div class="a-main">
          <div class="a-title">${esc(d.workshop_name)}</div>
          <div class="a-sub">${esc(d.issue)} · ${esc(d.hint || "")}</div>
        </div>
        <span class="ti-badge warn">数据缺失</span>
      </div>`
    )
    .join("");
  if ((!anomalies || !anomalies.length) && !dq) {
    el.innerHTML = `<div class="anomaly-list-empty">本期没有超过阈值的指标变化。</div>`;
    return;
  }
  const items = (anomalies || [])
    .map(
      (a) => `<div class="anomaly">
        <div class="a-main">
          <div class="a-title">${esc(a.workshop_name)}</div>
          <div class="a-sub">${esc(a.metric)} ${a.previous} → ${a.current} ${esc(a.unit)}</div>
        </div>
        <div class="a-val">${a.change_pct > 0 ? "+" : ""}${a.change_pct}%</div>
        <button class="a-go" data-ws="${esc(a.workshop_id)}">调查</button>
      </div>`
    )
    .join("");
  el.innerHTML = items + dq;
  $$(".a-go", el).forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.closest(".anomaly").querySelector(".a-title").textContent;
      const q = `${name}最近30天的单位产品电耗偏离基线，请调查原因并给出证据和可执行的改进建议。`;
      resetWorkbench();
      switchView("workbench");
      $("#input").value = q;
      sendMessage(q);
    });
  });
}

function renderAlarms(rows) {
  const el = $("#alarms");
  if (!rows || !rows.length) {
    el.innerHTML = `<div class="anomaly-list-empty">本期无告警。</div>`;
    return;
  }
  const sig = JSON.stringify(rows);
  if (sig === state.alarmSig) return;
  state.alarmSig = sig;
  const max = Math.max(...rows.map((r) => r.count), 1);
  el.innerHTML = rows
    .map(
      (r) => `<div class="alarm-row">
        <span class="al-name">${esc(r.workshop_name)}</span>
        <span class="bar"><i style="width:${Math.round((r.count / max) * 100)}%"></i></span>
        <span class="al-num">${r.count}</span>
      </div>`
    )
    .join("");
}

/* ------------------------------------------------------------------ 图表（SVG） */

function niceTicks(min, max, count = 4) {
  const span = max - min || 1;
  const step = span / count;
  return Array.from({ length: count + 1 }, (_, i) => min + step * i);
}

function lineChart(labels, series, { height = 260 } = {}) {
  const W = 820, H = height, padL = 46, padR = 16, padT = 14, padB = 28;
  const values = series.flatMap((s) => s.data).filter((v) => v !== null && v !== undefined);
  if (!values.length) return "";
  let min = Math.min(...values), max = Math.max(...values);
  const pad = (max - min || max * 0.1) * 0.15;
  min = Math.max(0, min - pad);
  max = max + pad;
  const x = (i) => padL + (i * (W - padL - padR)) / Math.max(labels.length - 1, 1);
  const y = (v) => padT + (1 - (v - min) / (max - min || 1)) * (H - padT - padB);

  const ticks = niceTicks(min, max);
  let g = "";
  ticks.forEach((t) => {
    g += `<line x1="${padL}" y1="${y(t).toFixed(1)}" x2="${W - padR}" y2="${y(t).toFixed(1)}" stroke="#eef1f6" stroke-width="1"/>`;
    g += `<text x="${padL - 8}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="#98a2b3">${t.toFixed(2)}</text>`;
  });

  const labelStep = Math.ceil(labels.length / 7);
  labels.forEach((d, i) => {
    if (i % labelStep !== 0 && i !== labels.length - 1) return;
    g += `<text x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="#98a2b3">${esc(d.slice(5))}</text>`;
  });

  let paths = "";
  series.forEach((s, si) => {
    const color = PALETTE[si % PALETTE.length];
    let d = "", started = false;
    s.data.forEach((v, i) => {
      if (v === null || v === undefined) { started = false; return; }
      d += `${started ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
      started = true;
    });
    paths += `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const lastIdx = s.data.reduce((acc, v, i) => (v !== null && v !== undefined ? i : acc), -1);
    if (lastIdx >= 0) {
      paths += `<circle cx="${x(lastIdx).toFixed(1)}" cy="${y(s.data[lastIdx]).toFixed(1)}" r="3" fill="${color}"/>`;
    }
  });

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">${g}${paths}</svg>`;
}

function groupedBarChart(labels, previous, current, { height = 240 } = {}) {
  const W = 820, H = height, padL = 46, padR = 16, padT = 14, padB = 30;
  const values = [...previous, ...current].filter((v) => v !== null && v !== undefined);
  const max = Math.max(...values, 1) * 1.15;
  const y = (v) => padT + (1 - v / max) * (H - padT - padB);
  const groupW = (W - padL - padR) / labels.length;
  const barW = Math.min(26, groupW * 0.3);

  let g = "";
  niceTicks(0, max).forEach((t) => {
    g += `<line x1="${padL}" y1="${y(t).toFixed(1)}" x2="${W - padR}" y2="${y(t).toFixed(1)}" stroke="#eef1f6"/>`;
    g += `<text x="${padL - 8}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="#98a2b3">${t.toFixed(1)}</text>`;
  });

  labels.forEach((name, i) => {
    const cx = padL + groupW * i + groupW / 2;
    const pv = previous[i], cv = current[i];
    if (pv !== null && pv !== undefined) {
      g += `<rect x="${(cx - barW - 3).toFixed(1)}" y="${y(pv).toFixed(1)}" width="${barW}" height="${(H - padB - y(pv)).toFixed(1)}" rx="3" fill="#cbd5e1"/>`;
    }
    if (cv !== null && cv !== undefined) {
      g += `<rect x="${(cx + 3).toFixed(1)}" y="${y(cv).toFixed(1)}" width="${barW}" height="${(H - padB - y(cv)).toFixed(1)}" rx="3" fill="#ea580c"/>`;
    }
    g += `<text x="${cx.toFixed(1)}" y="${H - 9}" text-anchor="middle" font-size="10" fill="#667085">${esc(name)}</text>`;
  });

  const legend = `<g>
    <rect x="${W - 190}" y="10" width="8" height="8" rx="2" fill="#cbd5e1"/><text x="${W - 177}" y="17" font-size="10" fill="#667085">上期</text>
    <rect x="${W - 130}" y="10" width="8" height="8" rx="2" fill="#ea580c"/><text x="${W - 117}" y="17" font-size="10" fill="#667085">本期</text>
  </g>`;

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">${g}${legend}</svg>`;
}

/* ------------------------------------------------------------------ 工作台：输入 */

function bindComposer() {
  $("#send").addEventListener("click", () => {
    const text = $("#input").value.trim();
    if (text) sendMessage(text);
  });
  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      const text = $("#input").value.trim();
      if (text) sendMessage(text);
    }
  });
}

function bindExamples() {
  $$(".example").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#input").value = btn.textContent.trim();
      $("#input").focus();
    });
  });
}

function resetWorkbench() {
  state.sessionId = null;
  state.runId = null;
  state.report = null;
  state.viewingRunId = null;
  state.activityBubble = null;
  state.runToolNames = [];
  state.runToolResults = [];
  state.traceItems.clear();
  $("#messages").innerHTML = "";
  $("#trace").innerHTML = `<div class="trace-empty">每一步模型决策、工具调用、错误与重试都会出现在这里。</div>`;
  $("#trace-meta").textContent = "尚未开始";
  $("#budget").innerHTML = "";
  $("#live-dot").classList.remove("on");
  $("#back-to-live").hidden = true;
  const banner = $("#trace-history");
  if (banner) banner.hidden = true;
}

function addMessage(role, html) {
  const empty = $("#chat-empty");
  if (empty) empty.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  wrap.innerHTML =
    role === "assistant"
      ? `<div class="avatar">OP</div><div class="bubble">${html}</div>`
      : `<div class="bubble">${html}</div>`;
  $("#messages").appendChild(wrap);
  scrollChat();
  return wrap;
}

/* 一次运行一个聚合气泡：直播与回放外观一致，点击可查看该次轨迹 */

function createActivityBubble(runId) {
  // 必须在插入 DOM 之前算轮次，否则会把自己算进去，导致第一轮显示成第 2 轮
  const round = document.querySelectorAll(".activity-bubble").length + 1;
  const wrap = addMessage("assistant", "");
  const bubble = wrap.querySelector(".bubble");
  bubble.classList.add("activity-bubble");
  bubble.dataset.runId = runId || "";
  bubble.dataset.round = String(round);
  bubble.tabIndex = 0;
  bubble.setAttribute("role", "button");
  bubble.title = "点击查看本次运行的执行轨迹";
  bubble.innerHTML = `<div class="activity-caption">工具调用 <b class="ac-count">0</b> 次</div>`;

  const openTrace = (e) => {
    // 气泡里的按钮/链接自己处理，避免重复触发
    if (e && e.target && e.target.closest && e.target.closest("button, a")) return;
    const rid = bubble.dataset.runId;
    if (rid) showRunTrace(rid);
  };
  bubble.addEventListener("click", openTrace);
  bubble.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openTrace();
    }
  });
  return bubble;
}

function ensureActivityBubble() {
  if (state.activityBubble && document.body.contains(state.activityBubble)) return state.activityBubble;
  state.activityBubble = createActivityBubble(state.runId);
  return state.activityBubble;
}

function appendChip(bubble, callId, name) {
  const chip = document.createElement("span");
  chip.className = "tool-chip";
  chip.dataset.call = callId;
  chip.dataset.name = name;
  chip.innerHTML = `<span class="dot"></span>${esc(name)}`;
  bubble.appendChild(chip);
  const counter = bubble.querySelector(".ac-count");
  if (counter) counter.textContent = String(bubble.querySelectorAll(".tool-chip").length);
  scrollChat();
}

function renderActivityChips(bubble, results, runId, label = "") {
  if (!bubble) return;
  bubble.querySelectorAll(".tool-chip").forEach((c) => c.remove());
  const groups = [];
  results.forEach((r) => {
    const last = groups[groups.length - 1];
    if (last && last.name === r.name) {
      last.count += 1;
      last.ok = last.ok && r.ok;
    } else {
      groups.push({ name: r.name, count: 1, ok: r.ok });
    }
  });
  const caption = bubble.querySelector(".activity-caption");
  if (caption) caption.innerHTML = `本次运行调用工具 <b>${results.length}</b> 次`;
  groups.forEach((g) => {
    const chip = document.createElement("span");
    chip.className = `tool-chip ${g.ok ? "ok" : "fail"}`;
    chip.dataset.name = g.name;
    chip.innerHTML = `<span class="dot"></span>${esc(g.name)}${g.count > 1 ? ` ×${g.count}` : ""}`;
    bubble.appendChild(chip);
  });
  addActivityFooter(bubble, runId, label);
}

function addActivityFooter(bubble, runId, label = "") {
  if (!runId || bubble.querySelector(".activity-footer")) return;
  const footer = document.createElement("div");
  footer.className = "activity-footer";
  footer.innerHTML = `<button class="link-btn">查看本次执行轨迹</button><span class="af-run">${label ? `${esc(label)} · ` : ""}${esc(runId.slice(4, 12))}</span>`;
  footer.querySelector("button").addEventListener("click", (e) => {
    e.stopPropagation();
    showRunTrace(runId);
  });
  bubble.appendChild(footer);
}

function finalizeActivity() {
  const bubble = state.activityBubble;
  if (!bubble) return;
  const round = bubble.dataset.round;
  renderActivityChips(bubble, state.runToolResults, state.runId, round ? `第 ${round} 轮` : "");
}

function markActiveActivity(runId) {
  document.querySelectorAll(".activity-bubble").forEach((b) => {
    b.classList.toggle("trace-active", b.dataset.runId === runId);
  });
}

function scrollChat() {
  const el = $("#messages");
  el.scrollTop = el.scrollHeight;
}

function setThinking(text) {
  let el = $("#thinking");
  if (!el) {
    el = document.createElement("div");
    el.id = "thinking";
    el.className = "thinking";
    el.innerHTML = `<span class="pulse"></span><span class="t"></span>`;
    $("#messages").appendChild(el);
  }
  $(".t", el).textContent = text;
  scrollChat();
}

function clearThinking() {
  const el = $("#thinking");
  if (el) el.remove();
}

/* ------------------------------------------------------------------ 工作台：SSE */

async function sendMessage(text) {
  if (state.streaming) return;
  state.streaming = true;
  $("#send").disabled = true;
  addMessage("user", esc(text).replace(/\n/g, "<br>"));
  $("#input").value = "";
  setThinking("正在启动调查…");
  $("#live-dot").classList.add("on");

  const payload = { message: text, session_id: state.sessionId, mock: $("#mock-toggle").checked };
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok || !resp.body) {
      const err = await resp.text();
      throw new Error(err);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop();
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        try {
          handleEvent(JSON.parse(line.slice(6)));
        } catch (e) {
          console.error("bad event", e, chunk);
        }
      }
    }
  } catch (e) {
    clearThinking();
    addMessage("assistant", `<p style="color:#b42318">请求失败：${esc(String(e.message || e))}</p>`);
  } finally {
    state.streaming = false;
    $("#send").disabled = false;
    $("#live-dot").classList.remove("on");
    clearThinking();
    loadSessions();
    loadReports(false);
  }
}

function handleEvent(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case "run_started": {
      state.runId = ev.run_id;
      state.sessionId = d.session_id;
      state.budget = d.budget;
      state.viewingRunId = ev.run_id;
      state.activityBubble = null;
      state.runToolNames = [];
      state.runToolResults = [];
      $("#trace").innerHTML = "";
      state.traceItems.clear();
      $("#trace-meta").textContent = `run ${ev.run_id.slice(4, 12)} · ${d.profile_label || d.model} · 进行中`;
      appendTraceNote(`开始运行 · ${d.profile_label || "异常调查"} · ${d.tools.length} 个工具可用`, "info");
      renderBudget(d.budget, null);
      $("#back-to-live").hidden = true;
      $("#trace-history").hidden = true;
      $("#live-dot").classList.add("on");
      setThinking("正在理解问题…");
      break;
    }
    case "llm_request":
      setThinking(`第 ${d.step} 步 · 模型推理中（上下文 ${d.messages} 条 / ~${d.est_tokens} tokens）`);
      break;
    case "llm_response": {
      clearThinking();
      if (d.content) {
        addMessage("assistant", md(d.content));
      }
      if (d.tool_calls && d.tool_calls.length) {
        ensureActivityBubble();
      }
      if (d.finish_reason === "length") {
        appendTraceNote("模型输出被 max_tokens 截断，参数可能不完整", "warn");
      }
      break;
    }
    case "assistant_message":
      break;
    case "tool_started": {
      state.runToolNames.push(d.name);
      appendChip(ensureActivityBubble(), d.call_id, d.name);
      addTraceItem(d);
      break;
    }
    case "tool_finished": {
      state.runToolResults.push({ name: d.name, ok: d.ok });
      const chip = $(`.tool-chip[data-call="${cssEscape(d.call_id)}"]`);
      if (chip) chip.classList.add(d.ok ? "ok" : "fail");
      updateTraceItem(d);
      if (d.ok && d.artifacts && d.artifacts.length) {
        const wrap = addMessage("assistant", "");
        appendArtifacts(wrap.querySelector(".bubble"), d.artifacts, "本次生成的图表");
      }
      break;
    }
    case "arg_repaired":
      appendNoteToTrace(d.call_id, `Harness 自动修复参数：${(d.repairs || []).join("；")}`);
      break;
    case "tool_retry":
      appendNoteToTrace(d.call_id, `第 ${d.attempt} 次重试（${d.kind}）`, "warn");
      break;
    case "context_compacted":
      if (d.compacted) appendTraceNote(`上下文压缩：省略 ${d.dropped_messages} 条早期消息`, "info");
      break;
    case "checkpoint":
      break;
    case "loop_guard":
      appendTraceNote(`熔断：${d.name} 连续失败，已提示模型换路径`, "warn");
      break;
    case "llm_truncated":
      appendTraceNote(d.message, "warn");
      break;
    case "budget_exceeded":
      appendTraceNote(`预算超限：${d.message}`, "bad");
      break;
    case "budget_warning":
      appendTraceNote(d.message, "warn");
      break;
    case "error":
      appendTraceNote(`错误：${d.message}`, "bad");
      break;
    case "report":
      state.report = d.report;
      addReportCard(d.report);
      scrollChat();
      break;
    case "run_finished": {
      clearThinking();
      finalizeActivity();
      // 用户可能正在看某一轮历史轨迹，不要强行抢回焦点
      if (state.viewingRunId === state.runId) {
        $("#trace-meta").textContent = `run ${(state.runId || "").slice(4, 12)} · 最新运行`;
        $("#back-to-live").hidden = true;
        $("#trace-history").hidden = true;
        markActiveActivity(state.runId);
        renderBudget(d.budget, d.usage);
        const ok = d.status === "completed";
        appendTraceNote(
          `${ok ? "完成" : "结束"} · ${d.budget.steps} 步 / ${d.budget.tool_calls} 次工具 / ${d.usage.total_tokens} tokens / ${(d.duration_ms / 1000).toFixed(1)}s`,
          ok ? "ok" : "bad"
        );
        if (!ok && d.error) appendTraceNote(d.error, "bad");
      }
      break;
    }
    default:
      break;
  }
}

/* ------------------------------------------------------------------ 产物图表 */

function encodePath(path) {
  return String(path).split("/").map(encodeURIComponent).join("/");
}

function artifactUrl(path, runId = state.runId) {
  if (!runId) return "";
  return `/artifacts/${runId}/artifacts/${encodePath(path)}`;
}

function isImage(path) {
  return /\.(png|jpe?g|svg|webp)$/i.test(String(path));
}

function appendArtifacts(bubble, artifacts, caption, runId = state.runId) {
  const imgs = (artifacts || []).filter(isImage);
  if (!imgs.length || !bubble) return;
  let wrap = bubble.querySelector(".tool-artifacts");
  if (!wrap) {
    wrap = document.createElement("div");
    wrap.className = "tool-artifacts";
    bubble.appendChild(wrap);
  }
  if (caption && !wrap.querySelector(".ta-caption")) {
    const cap = document.createElement("div");
    cap.className = "ta-caption";
    cap.textContent = caption;
    wrap.appendChild(cap);
  }
  imgs.forEach((p) => {
    const src = artifactUrl(p, runId);
    if (Array.from(wrap.querySelectorAll("img")).some((i) => i.getAttribute("src") === src)) return;
    const img = document.createElement("img");
    img.src = src;
    img.alt = p;
    img.loading = "lazy";
    img.onclick = () => window.open(src, "_blank");
    img.onerror = () => img.remove();
    wrap.appendChild(img);
  });
  scrollChat();
}

/* ------------------------------------------------------------------ 轨迹面板 */

function addTraceItem(d) {
  const el = document.createElement("div");
  el.className = "trace-item running";
  el.dataset.call = d.call_id;
  el.innerHTML = `
    <div class="ti-head">
      <span class="ti-name">${esc(d.name)}</span>
      <span class="ti-badge info" data-role="status">运行中</span>
      <span class="ti-time" data-role="time"></span>
    </div>
    <div class="ti-summary">${esc(argSummary(d))}</div>
    <div class="ti-note" data-role="note"></div>
    <details class="ti-detail">
      <summary>参数与结果</summary>
      <pre class="ti-args">${esc(prettyArgs(d.arguments))}</pre>
      <pre data-role="preview">—</pre>
    </details>`;
  $("#trace").appendChild(el);
  state.traceItems.set(d.call_id, el);
  $("#trace").scrollTop = $("#trace").scrollHeight;
}

function argSummary(d) {
  try {
    const o = JSON.parse(d.arguments || "{}");
    if (o.purpose) return o.purpose;
    if (o.sql) return `SQL · ${String(o.sql).replace(/\s+/g, " ").slice(0, 110)}`;
    if (o.code) return "Python 分析代码（见参数与结果）";
    if (o.workshop_id) return `${o.workshop_id} ${o.start_date || ""}${o.end_date ? " ~ " + o.end_date : ""}`;
    if (o.month) return `月份 ${o.month}`;
    if (o.table) return `表 ${o.table}`;
    if (o.title) return `报告：${o.title}`;
    const keys = Object.keys(o);
    if (!keys.length) return "无参数";
    return keys.slice(0, 3).map((k) => `${k}=${String(o[k]).slice(0, 40)}`).join(" · ");
  } catch {
    return String(d.arguments || "").slice(0, 110);
  }
}

function updateTraceItem(d) {
  const el = state.traceItems.get(d.call_id);
  if (!el) return;
  el.classList.remove("running");
  el.classList.add(d.ok ? "ok" : "fail");
  const badge = $('[data-role="status"]', el);
  badge.className = `ti-badge ${d.ok ? "ok" : "fail"}`;
  badge.textContent = d.ok ? "成功" : (d.error ? d.error.kind : "失败");
  $('[data-role="time"]', el).textContent = `${d.latency_ms}ms`;
  if (d.error) {
    const note = $('[data-role="note"]', el);
    note.classList.add("bad");
    note.textContent = d.error.message + (d.error.hint ? ` · ${d.error.hint}` : "");
  }
  if (d.preview) {
    const pre = $('[data-role="preview"]', el);
    pre.textContent = typeof d.preview === "string" ? d.preview.slice(0, 1200) : JSON.stringify(d.preview, null, 2).slice(0, 1200);
  }
}

function appendNoteToTrace(callId, text, kind = "") {
  const el = state.traceItems.get(callId);
  if (!el) return;
  const note = $('[data-role="note"]', el);
  note.textContent = (note.textContent ? note.textContent + "；" : "") + text;
  if (kind === "warn") note.style.color = "#b54708";
  if (kind === "bad") note.classList.add("bad");
}

function appendTraceNote(text, kind = "") {
  const el = document.createElement("div");
  el.className = "trace-item";
  el.innerHTML = `<div class="ti-head"><span class="ti-name" style="font-family:inherit;font-weight:500;color:#667085">${esc(text)}</span></div>`;
  if (kind) el.style.borderLeftColor = kind === "bad" ? "#f6cfcc" : kind === "warn" ? "#f3ddb5" : "#d8defb";
  $("#trace").appendChild(el);
  $("#trace").scrollTop = $("#trace").scrollHeight;
}

function prettyArgs(raw) {
  if (!raw) return "{}";
  try {
    const obj = JSON.parse(raw);
    return JSON.stringify(obj, null, 1).slice(0, 400);
  } catch {
    return String(raw).slice(0, 400);
  }
}

function renderBudget(budget, usage) {
  if (!budget) return;
  const rows = [
    ["步数", budget.steps, budget.max_steps],
    ["工具", budget.tool_calls, budget.max_tool_calls],
    ["Token", (usage && usage.total_tokens) || budget.tokens || 0, budget.max_tokens],
  ];
  $("#budget").innerHTML = rows
    .map(([label, val, max]) => {
      const pct = Math.min(100, Math.round((val / max) * 100));
      return `<div class="brow">
        <span class="blabel">${label}</span>
        <span class="bbar"><i class="${pct > 80 ? "hot" : ""}" style="width:${pct}%"></i></span>
        <span class="bval">${val}/${max}</span>
      </div>`;
    })
    .join("");
}

/* ------------------------------------------------------------------ 历史轨迹 */

async function showRunTrace(runId) {
  if (!runId) return;
  let events = [];
  try {
    const r = await fetch(`/api/runs/${runId}/events`, { cache: "no-store" });
    if (!r.ok) throw new Error(await r.text());
    events = (await r.json()).events || [];
  } catch (e) {
    showToast("该次运行的轨迹加载失败");
    return;
  }

  state.viewingRunId = runId;
  $("#trace").innerHTML = "";
  state.traceItems.clear();
  let finished = null;

  for (const ev of events) {
    const d = ev.data || {};
    switch (ev.type) {
      case "run_started":
        appendTraceNote(`开始运行 · ${d.profile_label || "异常调查"}`, "info");
        break;
      case "tool_started": {
        addTraceItem(d);
        const item = state.traceItems.get(d.call_id);
        if (item) item.classList.remove("running");
        break;
      }
      case "tool_finished":
        updateTraceItem(d);
        break;
      case "arg_repaired":
        appendNoteToTrace(d.call_id, `Harness 自动修复参数：${(d.repairs || []).join("；")}`);
        break;
      case "tool_retry":
        appendNoteToTrace(d.call_id, `第 ${d.attempt} 次重试（${d.kind}）`, "warn");
        break;
      case "loop_guard":
        appendTraceNote(`熔断：${d.name} 连续失败，已提示模型换路径`, "warn");
        break;
      case "budget_warning":
        appendTraceNote(d.message, "warn");
        break;
      case "budget_exceeded":
        appendTraceNote(`预算超限：${d.message}`, "bad");
        break;
      case "error":
        appendTraceNote(`错误：${d.message}`, "bad");
        break;
      case "context_compacted":
        if (d.compacted) appendTraceNote(`上下文压缩：省略 ${d.dropped_messages} 条早期消息`, "info");
        break;
      case "run_finished":
        finished = d;
        break;
      default:
        break;
    }
  }

  if (finished) {
    renderBudget(finished.budget, finished.usage);
    appendTraceNote(
      `${statusText(finished.status)} · ${finished.budget?.steps} 步 / ${finished.usage?.total_tokens} tokens / ${((finished.duration_ms || 0) / 1000).toFixed(1)}s`,
      finished.status === "completed" ? "ok" : "bad"
    );
  } else {
    $("#budget").innerHTML = "";
    appendTraceNote("该运行没有结束事件（可能被中断）", "warn");
  }

  const isLatest = runId === state.runId;
  const bubble = document.querySelector(`.activity-bubble[data-run-id="${runId}"]`);
  const round = bubble && bubble.dataset.round;
  $("#trace-meta").textContent = `run ${runId.slice(4, 12)} · ${isLatest ? "最新运行" : "历史运行"}`;
  $("#back-to-live").hidden = isLatest || !state.runId;
  const banner = $("#trace-history");
  if (banner) {
    banner.hidden = isLatest;
    $("#trace-history-text").textContent = isLatest
      ? ""
      : `正在查看历史运行 · ${runId.slice(4, 12)}${round ? ` · 第 ${round} 轮` : ""}`;
  }
  $("#live-dot").classList.toggle("on", isLatest && state.streaming);
  $("#trace").scrollTop = 0;
  markActiveActivity(runId);
}

/* ------------------------------------------------------------------ 报告 */

function addReportCard(report, runId = state.runId) {
  const el = document.createElement("div");
  el.className = "report-card";
  el.innerHTML = renderReportHtml(report, runId);
  $("#messages").appendChild(el);
  bindReportActions(el);
}

function renderReportHtml(report, runId) {
  const badge = (c) =>
    c === "已验证事实" ? "verified" : c === "证据支持" ? "supported" : "unverified";
  const metrics = (report.metrics || [])
    .map(
      (m) => `<div class="rc-metric">
        <div class="m-name">${esc(m.name)}</div>
        <div class="m-val">${Number(m.value).toLocaleString("zh-CN", { maximumFractionDigits: 3 })}<span class="m-unit">${esc(m.unit || "")}</span></div>
        <div class="m-period">${esc(m.period || "")}</div>
      </div>`
    )
    .join("");
  const findings = (report.findings || [])
    .map(
      (f) => `<div class="finding">
        <span class="badge ${badge(f.confidence)}">${esc(f.confidence)}</span>
        <div class="f-main">
          <div class="f-statement">${esc(f.statement)}</div>
          <div class="f-evidence">证据：${esc(f.evidence)}</div>
        </div>
      </div>`
    )
    .join("");
  const recs = (report.recommendations || []).map((r) => `<li>${esc(r)}</li>`).join("");
  const limits = (report.limitations || []).map((r) => `<li>${esc(r)}</li>`).join("");
  const charts = (report.charts || []).filter(isImage);
  const chartsHtml = charts.length
    ? `<div class="rc-h">图表</div><div class="tool-artifacts">${charts
        .map(
          (p) =>
            `<img src="${artifactUrl(p, runId)}" alt="${esc(p)}" loading="lazy" onerror="this.remove()">`
        )
        .join("")}</div>`
    : "";

  return `<div class="rc-head"><h3>${esc(report.title || "调查报告")}</h3>
      <div class="rc-sub">OpsPilot 自主调查 · 所有数字来自工具查询结果</div></div>
    <div class="rc-body">
      <p class="rc-summary">${esc(report.summary || "")}</p>
      ${metrics ? `<div class="rc-metrics">${metrics}</div>` : ""}
      ${chartsHtml}
      <div class="rc-h">结论与证据</div>${findings}
      <div class="rc-cols">
        <div><div class="rc-h">改进建议</div><ul class="rc-list">${recs || "<li>—</li>"}</ul></div>
        <div><div class="rc-h">局限与待核实</div><ul class="rc-list">${limits || "<li>—</li>"}</ul></div>
      </div>
      <div class="rc-foot">
        ${runId ? `<a class="btn ghost" href="/api/runs/${runId}/report.md" download>下载 Markdown</a>` : ""}
        <a class="btn ghost" href="/api/runs/${runId}/events" target="_blank">查看完整 Trace</a>
      </div>
    </div>`;
}

function bindReportActions(root) {
  // 下载与 trace 都是普通链接，无需额外处理
}

/* ------------------------------------------------------------------ 历史 */

let sessionsSeq = 0;

async function loadSessions() {
  const seq = ++sessionsSeq;
  try {
    const r = await fetch("/api/sessions?limit=20", { cache: "no-store" });
    const data = await r.json();
    if (seq !== sessionsSeq) return; // 旧请求晚到，丢弃，避免把新数据覆盖成旧的
    const sessions = data.sessions || [];
    const el = $("#session-list");
    if (!sessions.length) {
      el.innerHTML = `<div class="side-empty">暂无记录</div>`;
      return;
    }
    el.innerHTML = sessions
      .map(
        (s) => `<button class="session-item ${s.session_id === state.sessionId ? "active" : ""}" data-session="${esc(s.session_id)}">
          <span class="si-title">${esc(s.title || "未命名对话")}</span>
          <span class="st"><span>${s.runs} 轮</span>·<span>${relTime(s.last_at)}</span></span>
        </button>`
      )
      .join("");
    $$(".session-item", el).forEach((btn) =>
      btn.addEventListener("click", () => openSession(btn.dataset.session))
    );
  } catch (e) {
    console.error(e);
  }
}

/* 一段对话 = 一个 session 下的多轮 run，按时间顺序还原 */

async function openSession(sessionId, focusRunId = null) {
  switchView("workbench");
  resetWorkbench();
  state.sessionId = sessionId;
  try {
    const resp = await fetch(`/api/sessions/${sessionId}`, { cache: "no-store" });
    if (!resp.ok) throw new Error(await resp.text());
    const runs = (await resp.json()).runs || [];

    for (const [i, run] of runs.entries()) {
      let events = [];
      try {
        const er = await fetch(`/api/runs/${run.run_id}/events`, { cache: "no-store" });
        events = (await er.json()).events || [];
      } catch {
        events = [];
      }
      appendRunToChat(run, events, i + 1);
    }

    if (runs.length) {
      const latest = runs[runs.length - 1].run_id;
      state.runId = latest;
      await showRunTrace(focusRunId || latest);
    }
    $("#chat-empty")?.remove();
    scrollChat();
    // 图片懒加载会改变高度，稍后再滚一次，确保停在最新一轮
    setTimeout(scrollChat, 400);
  } catch (e) {
    console.error(e);
    showToast("会话加载失败");
  }
  await loadSessions();
}

function appendRunToChat(run, events, roundIndex = 0) {
  if (run.task) addMessage("user", esc(run.task).replace(/\n/g, "<br>"));
  let activity = null;
  const toolResults = [];
  let sawReport = false;

  for (const ev of events) {
    const d = ev.data || {};
    switch (ev.type) {
      case "assistant_message":
        addMessage("assistant", md(d.content || ""));
        break;
      case "tool_started":
        if (!activity) {
          activity = createActivityBubble(run.run_id);
          activity.dataset.round = String(roundIndex);
        }
        appendChip(activity, d.call_id, d.name);
        break;
      case "tool_finished":
        toolResults.push({ name: d.name, ok: Boolean(d.ok) });
        if (d.ok && d.artifacts && d.artifacts.length) {
          const wrap = addMessage("assistant", "");
          appendArtifacts(wrap.querySelector(".bubble"), d.artifacts, "本次生成的图表", run.run_id);
        }
        break;
      case "report":
        addReportCard(d.report, run.run_id);
        sawReport = true;
        break;
      default:
        break;
    }
  }
  if (activity) {
    renderActivityChips(activity, toolResults, run.run_id, roundIndex ? `第 ${roundIndex} 轮` : "");
  }
  if (run.report && !sawReport) addReportCard(run.report, run.run_id);
}

async function openRun(runId) {
  try {
    const resp = await fetch(`/api/runs/${runId}`, { cache: "no-store" });
    if (!resp.ok) throw new Error(await resp.text());
    const { run } = await resp.json();
    if (run?.session_id) {
      await openSession(run.session_id, runId);
    } else {
      showToast("该运行没有会话信息");
    }
  } catch (e) {
    console.error(e);
    showToast("运行加载失败");
  }
}

async function loadReports(autoSelect = true) {
  try {
    const r = await fetch("/api/reports", { cache: "no-store" });
    const data = await r.json();
    state.reports = data.reports || [];
    const el = $("#report-list");
    if (!state.reports.length) {
      el.innerHTML = `<div class="side-empty">还没有报告，先去工作台跑一次调查。</div>`;
      return;
    }
    el.innerHTML = state.reports
      .map((rep) => {
        const title = (rep.report && rep.report.title) || rep.title || rep.task || "调查报告";
        return `<button class="report-item" data-run="${rep.run_id}">
          <div class="ri-title">${esc(title)}</div>
          <div class="ri-meta">${relTime(rep.created_at)} · ${statusText(rep.status)}${
            rep.duplicates > 1 ? ` · <span class="dup">同内容 ${rep.duplicates} 份</span>` : ""
          }</div>
        </button>`;
      })
      .join("");
    $$(".report-item", el).forEach((btn) =>
      btn.addEventListener("click", () => showReport(btn.dataset.run))
    );
    if (autoSelect) showReport(state.reports[0].run_id);
  } catch (e) {
    console.error(e);
  }
}

async function showReport(runId) {
  $$(".report-item").forEach((b) => b.classList.toggle("active", b.dataset.run === runId));
  try {
    const r = await fetch(`/api/runs/${runId}`);
    const { run } = await r.json();
    if (!run || !run.report) {
      $("#report-detail").innerHTML = `<div class="empty-state small"><p>该运行没有结构化报告。</p></div>`;
      return;
    }
    $("#report-detail").innerHTML = `<div class="report-card" style="margin-top:0">${renderReportHtml(run.report, runId)}</div>`;
  } catch (e) {
    console.error(e);
  }
}

function statusText(status) {
  return {
    running: "进行中",
    completed: "已完成",
    failed: "失败",
    paused: "已暂停",
    cancelled: "已取消",
    budget_exceeded: "预算超限",
  }[status] || status || "";
}

function relTime(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

/* ------------------------------------------------------------------ Markdown 精简渲染 */

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function cssEscape(s) {
  return String(s).replace(/["\\]/g, "\\$&");
}

function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function md(text) {
  if (!text) return "";
  const src = String(text).replace(/\r\n/g, "\n");
  const out = [];
  const lines = src.split("\n");
  let i = 0;

  const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l) => /^\s*\|[\s:|-]+\|\s*$/.test(l);

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*```/.test(line)) {
      const lang = line.replace(/^\s*```/, "").trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code data-lang="${esc(lang)}">${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }

    if (isTableRow(line) && i + 1 < lines.length && isSep(lines[i + 1])) {
      const header = line.split("|").slice(1, -1).map((c) => c.trim());
      i += 2;
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) {
        rows.push(lines[i].split("|").slice(1, -1).map((c) => c.trim()));
        i++;
      }
      out.push(
        `<table><thead><tr>${header.map((h) => `<th>${inline(esc(h))}</th>`).join("")}</tr></thead><tbody>${rows
          .map((r) => `<tr>${r.map((c) => `<td>${inline(esc(c))}</td>`).join("")}</tr>`)
          .join("")}</tbody></table>`
      );
      continue;
    }

    if (/^#{1,6}\s/.test(line)) {
      out.push(`<h4>${inline(esc(line.replace(/^#{1,6}\s/, "")))}</h4>`);
      i++;
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      out.push(`<ul>${items.map((it) => `<li>${inline(esc(it))}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      out.push(`<ol>${items.map((it) => `<li>${inline(esc(it))}</li>`).join("")}</ol>`);
      continue;
    }

    if (!line.trim()) {
      i++;
      continue;
    }

    const buf = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*```/.test(lines[i]) &&
      !/^#{1,6}\s/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !isTableRow(lines[i])
    ) {
      buf.push(lines[i++]);
    }
    out.push(`<p>${inline(esc(buf.join(" ")))}</p>`);
  }
  return out.join("");
}
