/* ============================================================
   AIOps Console V2 — frontend logic (standalone)
   独立于旧版 frontend/app.js，消费同一套 /api/v1 后端。
   ============================================================ */

const API = "/api/v1";

// ---------- tiny DOM helpers ----------
const $  = (id) => document.getElementById(id);
const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, kind = "") {
  const t = el("div", "toast" + (kind ? " toast--" + kind : ""), esc(msg));
  $("toast-wrap").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 250); }, 2800);
}

function renderMd(md) {
  if (!md) return "";
  let s = String(md).replace(/\\n/g, "\n").replace(/\\t/g, "\t");
  try { return window.marked ? marked.parse(s, { breaks: true, gfm: true }) : "<pre>" + esc(s) + "</pre>"; }
  catch { return "<pre>" + esc(s) + "</pre>"; }
}
function renderWikiMd(md) {
  return renderMd(md).replace(/\[\[([a-z0-9_\-]+)\/([a-z0-9_\-]+)\]\]/gi,
    (_, c, sl) => `<a class="wikilink" data-wiki-ref="${esc(c)}/${esc(sl)}">[[${esc(c)}/${esc(sl)}]]</a>`);
}
const fmtTime = (iso) => { if (!iso) return "—"; try { return new Date(iso).toLocaleString("zh-CN", { hour12: false }); } catch { return iso; } };
const ago = (iso) => { if (!iso) return ""; const d = (Date.now() - new Date(iso).getTime()) / 1000; if (d < 60) return Math.floor(d) + "s前"; if (d < 3600) return Math.floor(d / 60) + "分前"; if (d < 86400) return Math.floor(d / 3600) + "时前"; return Math.floor(d / 86400) + "天前"; };

// ============================================================
// View routing
// ============================================================
const PAGES = {
  diagnose:  { title: "智能诊断", sub: "Skill-first · Plan / Execute / Replan 多智能体故障定位" },
  incidents: { title: "事件中心", sub: "后台诊断任务、队列水位、证据链与报告审计" },
  chat:      { title: "智能问答", sub: "基于知识库的 RAG 单体问答，可联网 / 调用 MCP 只读工具" },
  documents: { title: "知识库",   sub: "文档索引管理与检索质量评估" },
  wiki:      { title: "经验库",   sub: "LLM 自维护的故障模式与服务经验 Wiki" },
};
let incTimer = null;

document.querySelectorAll(".nav-item[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});
function switchView(name) {
  document.querySelectorAll(".nav-item[data-view]").forEach((b) => b.classList.toggle("is-active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("is-active", v.id === "view-" + name));
  $("page-title").textContent = PAGES[name].title;
  $("page-sub").textContent = PAGES[name].sub;
  $("topbar-extra").innerHTML = "";

  if (name === "incidents") { incLoad(); incStartAuto(); } else incStopAuto();
  if (name === "documents") { loadDocs(); loadEvalReports(); }
  if (name === "wiki") wikiEnter();
}

// ============================================================
// Health (polled)
// ============================================================
async function checkHealth() {
  try {
    const r = await fetch(`${API}/health/ready`);
    const data = await r.json();
    const d = data?.dependencies || data?.data?.dependencies || {};
    const ready = (data?.status || data?.data?.status) === "ready";
    const mcp = d.mcp || {};
    const dot = $("health-dot"), txt = $("health-text");
    if (ready && mcp.status === "ok") { dot.className = "dot is-ok"; txt.textContent = `就绪 · MCP ${mcp.tools_count || 0} 工具`; }
    else if (ready) { dot.className = "dot is-warn"; txt.textContent = "就绪 · MCP 未连"; }
    else { dot.className = "dot is-err"; txt.textContent = "Milvus 不可用"; }
  } catch { $("health-dot").className = "dot is-err"; $("health-text").textContent = "服务不可达"; }
}
checkHealth(); setInterval(checkHealth, 15000);

// ============================================================
// SSE reader (fetch + ReadableStream)
// ============================================================
async function readSSE(url, body, onEvent, signal) {
  const resp = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { const j = await resp.json(); detail = j?.detail?.message || j?.message || JSON.stringify(j); } catch {}
    throw new Error(`${resp.status} ${detail}`);
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const blocks = buf.split(/\r?\n\r?\n/);
    buf = blocks.pop();
    for (const block of blocks) {
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("data:")) {
          const raw = line.slice(5).trim();
          if (!raw) continue;
          try { onEvent(JSON.parse(raw)); } catch { /* ignore */ }
        }
      }
    }
  }
}

// ============================================================
// Skills
// ============================================================
const RISK = { low: { cls: "badge--ok", label: "低风险" }, medium: { cls: "badge--warn", label: "中风险" }, high: { cls: "badge--err", label: "高风险" } };
let skillEls = {};

async function loadSkills() {
  const grid = $("skill-grid");
  try {
    const r = await fetch(`${API}/skills`); const j = await r.json();
    const skills = j?.data?.skills || [];
    $("skill-count").textContent = `· ${skills.length} 个`;
    grid.innerHTML = ""; skillEls = {};
    if (!skills.length) { grid.innerHTML = '<span class="italic-empty">暂无 Skill 注册</span>'; return; }
    skills.forEach((s) => {
      const risk = RISK[s.risk_level] || RISK.low;
      const card = el("div", "skill");
      card.innerHTML = `<div class="skill__name"><span class="skill__title">${esc(s.display_name || s.name)}</span>
        <span class="badge ${risk.cls} skill__risk">${risk.label}</span></div>
        <div class="skill__desc">${esc(s.description || "")}</div>`;
      grid.appendChild(card);
      skillEls[s.name] = card;
    });
  } catch (e) { grid.innerHTML = `<span style="color:var(--err)">加载失败: ${esc(e.message)}</span>`; }
}
function highlightSkill(name, reason) {
  Object.values(skillEls).forEach((c) => c.classList.remove("is-picked"));
  if (skillEls[name]) skillEls[name].classList.add("is-picked");
  const b = $("skill-picked"); b.classList.remove("hidden"); b.textContent = "本次诊断：" + (name || "—");
  if (reason) { const r = $("skill-reason"); r.classList.remove("hidden"); r.textContent = "选择理由：" + reason; }
}
loadSkills();

// ============================================================
// Diagnose
// ============================================================
const dg = { mode: "fast", submit: "realtime", ctrl: null, t0: 0, tools: 0, toolsFail: 0, timer: null, pollTimer: null };

$("dg-mode").addEventListener("click", (e) => { const b = e.target.closest("[data-mode]"); if (!b) return; dg.mode = b.dataset.mode; $("dg-mode").querySelectorAll(".seg__btn").forEach((x) => x.classList.toggle("is-active", x === b)); });
$("dg-submit").addEventListener("click", (e) => { const b = e.target.closest("[data-submit]"); if (!b) return; dg.submit = b.dataset.submit; $("dg-submit").querySelectorAll(".seg__btn").forEach((x) => x.classList.toggle("is-active", x === b)); });
$("dg-start").addEventListener("click", startDiagnose);
$("dg-stop").addEventListener("click", stopDiagnose);

function dgReset() {
  $("dg-plan").innerHTML = '<span class="italic-empty">等待 Planner…</span>';
  $("dg-steps").innerHTML = "";
  $("dg-report").classList.add("hidden"); $("dg-report").innerHTML = "";
  $("dg-monitor").classList.remove("hidden");
  $("dg-right-title").textContent = "诊断监控";
  $("mon-step").textContent = "—"; $("mon-step-label").textContent = "等待启动";
  $("mon-elapsed").textContent = "0.0s"; $("mon-tools").textContent = "0"; $("mon-tools-fail").textContent = "失败 0";
  $("mon-tokens").textContent = "0"; $("mon-tokens-detail").textContent = "输入 0 · 输出 0";
  $("mon-tokens-badge").textContent = "~估算";
  $("mon-stream").innerHTML = '<span class="italic-empty">诊断开始后，模型生成的文本会实时显示在此…</span>';
  $("mon-tool-feed").innerHTML = '<span class="italic-empty">暂无工具调用</span>';
  $("mon-stream-hint").textContent = "等待中";
  dg.tools = 0; dg.toolsFail = 0;
}
function dgBusy(b) { $("dg-start").disabled = b; $("dg-stop").disabled = !b; }
function dgTick() { $("mon-elapsed").textContent = ((Date.now() - dg.t0) / 1000).toFixed(1) + "s"; }

async function startDiagnose() {
  const q = $("dg-query").value.trim();
  if (!q) { toast("请输入故障描述", "err"); return; }
  if (dg.submit === "queue") return submitQueue(q);

  dgReset(); dgBusy(true); dg.t0 = Date.now();
  dg.timer = setInterval(dgTick, 100);
  $("dg-status").className = "badge badge--run"; $("dg-status").textContent = "诊断中";
  dg.ctrl = new AbortController();
  try {
    await readSSE(`${API}/aiops/diagnose`, { session_id: "v2-" + Date.now(), query: q, diagnosis_mode: dg.mode }, handleDgEvent, dg.ctrl.signal);
  } catch (e) {
    if (e.name !== "AbortError") { $("dg-status").className = "badge badge--err"; $("dg-status").textContent = "失败"; toast("诊断失败: " + e.message, "err"); }
  } finally { clearInterval(dg.timer); dgBusy(false); }
}
function stopDiagnose() { if (dg.ctrl) dg.ctrl.abort(); clearInterval(dg.timer); dgBusy(false); $("dg-status").className = "badge badge--mute"; $("dg-status").textContent = "已停止"; }

function handleDgEvent(ev) {
  const t = ev.type, d = ev.data || {};
  const status = (s, cls) => { $("dg-status").className = "badge " + cls; $("dg-status").textContent = s; };
  if (t === "start") status("Skill Router 工作中", "badge--run");
  else if (t === "mode_selected") status(d.group_agent_reserved ? "深度入口已保留" : "日常诊断模式", "badge--run");
  else if (t === "skill_selected") { highlightSkill(d.skill, d.reason); status("已选 Skill: " + (d.skill || "—"), "badge--run"); }
  else if (t === "plan" || t === "replan") {
    const plan = d.plan || d.steps || [];
    const box = $("dg-plan"); box.innerHTML = "";
    plan.forEach((step, i) => { const row = el("div", "plan-step"); row.innerHTML = `<span class="plan-num">${i + 1}</span><span>${esc(step)}</span>`; box.appendChild(row); });
    status(`已生成 ${plan.length} 步计划`, "badge--run");
  }
  else if (t === "step_start") {
    $("mon-step").textContent = d.iteration ?? "—"; $("mon-step-label").textContent = (d.step || "").slice(0, 40);
    let card = $("dg-steps").querySelector(`[data-iter="${d.iteration}"]`);
    if (!card) {
      if ($("dg-steps").querySelector(".italic-empty")) $("dg-steps").innerHTML = "";
      card = el("div", "step-card is-running"); card.dataset.iter = d.iteration;
      card.innerHTML = `<div class="step-card__head"><span class="dot is-run"></span>步骤 ${d.iteration}：${esc((d.step || "").slice(0, 50))}</div><div class="step-card__body"></div>`;
      $("dg-steps").appendChild(card); $("dg-steps").scrollTop = $("dg-steps").scrollHeight;
    }
    $("mon-stream-hint").textContent = "生成中";
  }
  else if (t === "step_token") {
    const sm = $("mon-stream"); if (sm.querySelector(".italic-empty")) sm.innerHTML = "";
    sm.append(d.content || ""); sm.scrollTop = sm.scrollHeight;
  }
  else if (t === "tool_call") {
    dg.tools++; if (d.success === false) dg.toolsFail++;
    $("mon-tools").textContent = dg.tools; $("mon-tools-fail").textContent = "失败 " + dg.toolsFail;
    const feed = $("mon-tool-feed"); if (feed.querySelector(".italic-empty")) feed.innerHTML = "";
    const row = el("div", "tool-row");
    row.innerHTML = `<span class="dot ${d.success === false ? "is-err" : "is-ok"}"></span><span class="mono" style="flex:1">${esc(d.name || "tool")}</span><span class="muted mono">${d.elapsed_ms != null ? Math.round(d.elapsed_ms) + "ms" : ""}</span>`;
    feed.appendChild(row); feed.scrollTop = feed.scrollHeight;
  }
  else if (t === "usage") {
    $("mon-tokens").textContent = (d.total_tokens ?? 0).toLocaleString();
    $("mon-tokens-detail").textContent = `输入 ${d.input_tokens ?? 0} · 输出 ${d.output_tokens ?? 0}`;
    $("mon-tokens-badge").textContent = "真实";
  }
  else if (t === "step_complete") {
    const card = $("dg-steps").querySelector(`[data-iter="${d.iteration}"]`);
    if (card) { card.classList.remove("is-running"); card.classList.add("is-done"); card.querySelector(".dot").className = "dot is-ok"; const body = card.querySelector(".step-card__body"); if (d.result_preview) body.textContent = d.result_preview; }
  }
  else if (t === "tool_pending_approval") { status("等待工具审批", "badge--warn"); toast("有工具调用待审批：" + (d.tool || ""), ""); refreshApprovals(); }
  else if (t === "tool_approval_resolved") { refreshApprovals(); }
  else if (t === "report") {
    $("dg-monitor").classList.add("hidden");
    const rep = $("dg-report"); rep.classList.remove("hidden"); rep.innerHTML = renderMd(d.report);
    $("dg-right-title").textContent = "诊断报告"; status("已完成", "badge--ok");
  }
  else if (t === "complete") { status("已完成", "badge--ok"); $("mon-stream-hint").textContent = "完成"; }
  else if (t === "error") { status("失败", "badge--err"); toast("诊断错误: " + (ev.message || d.error || "未知"), "err"); }
}

async function submitQueue(q) {
  dgReset(); $("dg-monitor").classList.add("hidden");
  const rep = $("dg-report"); rep.classList.remove("hidden"); rep.innerHTML = '<div class="italic-empty">提交中…</div>';
  $("dg-status").className = "badge badge--run"; $("dg-status").textContent = "提交中";
  try {
    const r = await fetch(`${API}/aiops/diagnose/submit`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query: q, mode: dg.mode, session_id: "v2-submit", severity: "warning", service: "" }) });
    if (r.status === 429) { const j = await r.json().catch(() => ({})); const ra = j?.detail?.retry_after || j?.retry_after; rep.innerHTML = `<p style="color:var(--warn)">提交过于频繁，请 ${ra || "稍后"} 秒后重试</p>`; $("dg-status").textContent = "限流"; return; }
    const j = await r.json();
    $("dg-status").className = "badge badge--ok"; $("dg-status").textContent = "已入队";
    rep.innerHTML = `<div class="card card--pad" style="box-shadow:none"><div class="card-title">任务已提交</div>
      <div class="kv"><span class="kv__k">任务 ID</span><span class="kv__v mono">${esc(j.task_id)}</span></div>
      <div class="kv"><span class="kv__k">排队位置</span><span class="kv__v">${j.queue_position ? `第 ${j.queue_position} 位（前方 ${Math.max(0, j.queue_position - 1)} 个）` : "即将开始"}</span></div>
      <div class="kv"><span class="kv__k">说明</span><span class="kv__v">${esc(j.message || "")}</span></div>
      <button class="btn btn--ghost btn--sm" style="margin-top:12px" onclick="window.__goIncident('${esc(j.task_id)}')">前往事件中心查看 →</button></div>`;
    toast("已提交到队列", "ok");
  } catch (e) { rep.innerHTML = `<p style="color:var(--err)">提交失败: ${esc(e.message)}</p>`; $("dg-status").textContent = "失败"; }
}
window.__goIncident = (id) => { switchView("incidents"); setTimeout(() => incSelect(id), 300); };

// ============================================================
// Incidents
// ============================================================
const inc = { tasks: [], filter: "all", selected: null, selectedIds: new Set(), search: "" };

function incStartAuto() { incStopAuto(); if ($("inc-auto").checked) incTimer = setInterval(() => { incLoad(true); if ($("inc-queue")) loadQueue(); }, 5000); loadQueue(); }
function incStopAuto() { if (incTimer) { clearInterval(incTimer); incTimer = null; } }
$("inc-auto").addEventListener("change", incStartAuto);
$("inc-refresh").addEventListener("click", () => { incLoad(); loadQueue(); });
$("inc-search").addEventListener("input", (e) => { inc.search = e.target.value.trim().toLowerCase(); incRenderList(); });
document.querySelectorAll("[data-inc-status]").forEach((b) => b.addEventListener("click", () => {
  inc.filter = b.dataset.incStatus;
  document.querySelectorAll("[data-inc-status]").forEach((x) => x.classList.toggle("is-active", x === b));
  incRenderList();
}));
$("inc-select-all").addEventListener("change", (e) => {
  inc.selectedIds.clear();
  if (e.target.checked) incVisible().filter(incDeletable).forEach((t) => inc.selectedIds.add(t.id));
  incRenderList(); incUpdateBulk();
});
$("inc-bulk-delete").addEventListener("click", incBulkDelete);

const incDeletable = (t) => t.status !== "pending" && t.status !== "running";
const ST_BADGE = { running: ["badge--run", "进行中"], pending: ["badge--warn", "排队"], succeeded: ["badge--ok", "完成"], failed: ["badge--err", "失败"], cancelled: ["badge--mute", "取消"], timeout: ["badge--err", "超时"] };

async function incLoad(silent) {
  if (!silent) $("inc-list").innerHTML = '<div class="italic-empty" style="text-align:center;padding:32px 0">加载中…</div>';
  try {
    const r = await fetch(`${API}/incidents/tasks?limit=20`); const j = await r.json();
    inc.tasks = j.items || [];
    const c = { running: 0, pending: 0, succeeded: 0, failed: 0 };
    inc.tasks.forEach((t) => { if (c[t.status] != null) c[t.status]++; });
    $("inc-total").textContent = inc.tasks.length; $("inc-running").textContent = c.running; $("inc-pending").textContent = c.pending; $("inc-succeeded").textContent = c.succeeded; $("inc-failed").textContent = c.failed;
    incRenderList();
    if (inc.selected) { const cur = inc.tasks.find((t) => t.id === inc.selected); if (cur && (cur.status === "running" || cur.status === "pending")) incSelect(inc.selected, true); }
  } catch (e) { if (!silent) $("inc-list").innerHTML = `<div style="color:var(--err);padding:20px">加载失败: ${esc(e.message)}</div>`; }
}
function incVisible() {
  return inc.tasks.filter((t) => {
    if (inc.filter !== "all" && t.status !== inc.filter) return false;
    if (inc.search) { const p = t.payload || {}; const hay = `${p.alertname || ""} ${p.service || ""} ${t.id}`.toLowerCase(); if (!hay.includes(inc.search)) return false; }
    return true;
  });
}
function incRenderList() {
  const list = $("inc-list"); const items = incVisible();
  if (!items.length) { list.innerHTML = '<div class="italic-empty" style="text-align:center;padding:32px 0">无匹配任务</div>'; return; }
  list.innerHTML = "";
  items.forEach((t) => {
    const p = t.payload || {}; const [cls, label] = ST_BADGE[t.status] || ["badge--mute", t.status];
    const item = el("div", "task-item" + (t.id === inc.selected ? " is-sel" : ""));
    const canDel = incDeletable(t);
    item.innerHTML = `<div style="display:flex;gap:8px;align-items:flex-start">
      ${canDel ? `<input type="checkbox" class="inc-cb" data-id="${t.id}" ${inc.selectedIds.has(t.id) ? "checked" : ""} style="margin-top:2px">` : '<span style="width:13px"></span>'}
      <div style="flex:1;min-width:0">
        <div class="task-item__title">${esc(p.alertname || p.query?.slice(0, 40) || "诊断任务")}</div>
        <div class="task-item__meta">
          <span class="badge ${cls}">${label}</span>
          <span class="badge badge--info">${t.diagnosis_mode === "deep" ? "深度" : "日常"}</span>
          ${p.service ? `<span>${esc(p.service)}</span>` : ""}
          <span>${ago(t.created_at)}</span>
        </div>
      </div></div>`;
    item.addEventListener("click", (e) => { if (e.target.classList.contains("inc-cb")) return; incSelect(t.id); });
    list.appendChild(item);
  });
  list.querySelectorAll(".inc-cb").forEach((cb) => cb.addEventListener("change", (e) => {
    const id = e.target.dataset.id; if (e.target.checked) inc.selectedIds.add(id); else inc.selectedIds.delete(id); incUpdateBulk();
  }));
  incUpdateBulk();
}
function incUpdateBulk() { const n = inc.selectedIds.size; $("inc-sel-count").textContent = n; $("inc-bulk-delete").disabled = n === 0; }

async function incBulkDelete() {
  const ids = [...inc.selectedIds]; if (!ids.length) return;
  if (!confirm(`确认删除 ${ids.length} 个任务？`)) return;
  try {
    const r = await fetch(`${API}/incidents/tasks/bulk-delete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_ids: ids }) });
    const j = await r.json();
    toast(`已删除 ${j.deleted || 0} 个${j.skipped_active?.length ? `，跳过 ${j.skipped_active.length} 个进行中` : ""}`, "ok");
    inc.selectedIds.clear(); $("inc-select-all").checked = false; incLoad();
  } catch (e) { toast("删除失败: " + e.message, "err"); }
}

async function incSelect(id, silent) {
  inc.selected = id;
  if (!silent) document.querySelectorAll(".task-item").forEach((x) => x.classList.remove("is-sel"));
  incRenderList();
  const detail = $("inc-detail");
  if (!silent) detail.innerHTML = '<div class="italic-empty" style="text-align:center;padding:40px 0">加载中…</div>';
  try {
    const [tR, eR, aR, cR] = await Promise.all([
      fetch(`${API}/incidents/tasks/${encodeURIComponent(id)}`),
      fetch(`${API}/incidents/tasks/${encodeURIComponent(id)}/evidence?limit=100`),
      fetch(`${API}/incidents/tasks/${encodeURIComponent(id)}/agent-runs`),
      fetch(`${API}/incidents/tasks/${encodeURIComponent(id)}/tool-calls`),
    ]);
    const task = await tR.json(); const ev = (await eR.json()).items || []; const runs = (await aR.json()).items || []; const tools = (await cR.json()).items || [];
    incRenderDetail(task, ev, runs, tools);
  } catch (e) { detail.innerHTML = `<div style="color:var(--err)">加载详情失败: ${esc(e.message)}</div>`; }
}

function incRenderDetail(task, evidence, runs, tools) {
  const p = task.payload || {}; const [cls, label] = ST_BADGE[task.status] || ["badge--mute", task.status];
  let html = `<div class="card-h"><div><div class="card-title" style="font-size:15px">${esc(p.alertname || "诊断任务")}</div>
      <div class="mono muted" style="font-size:10.5px;margin-top:3px">${esc(task.id)}</div></div>
      <span class="badge ${cls}">${label}</span></div>`;

  html += `<div style="margin:14px 0">
    <div class="kv"><span class="kv__k">模式</span><span class="kv__v">${task.diagnosis_mode === "deep" ? "深度多 Agent" : "日常 Plan-Execute"}</span></div>
    ${p.service ? `<div class="kv"><span class="kv__k">服务</span><span class="kv__v">${esc(p.service)}</span></div>` : ""}
    ${p.severity ? `<div class="kv"><span class="kv__k">级别</span><span class="kv__v">${esc(p.severity)}</span></div>` : ""}
    <div class="kv"><span class="kv__k">创建</span><span class="kv__v">${fmtTime(task.created_at)}</span></div>
    ${task.finished_at ? `<div class="kv"><span class="kv__k">完成</span><span class="kv__v">${fmtTime(task.finished_at)}</span></div>` : ""}
    <div class="kv"><span class="kv__k">尝试</span><span class="kv__v">${task.attempts ?? 0} / ${task.max_attempts ?? "—"}</span></div>
    ${task.queue_position ? `<div class="kv"><span class="kv__k">排队位置</span><span class="kv__v">第 ${task.queue_position} 位</span></div>` : ""}
    ${task.error ? `<div class="kv"><span class="kv__k">错误</span><span class="kv__v" style="color:var(--err)">${esc(task.error)}</span></div>` : ""}
  </div>`;

  if (p.query) html += `<div class="card card--pad" style="box-shadow:none;background:var(--surface-2);margin-bottom:14px"><div class="eyebrow" style="margin-bottom:6px">原始描述</div><div style="font-size:12.5px;line-height:1.6;color:var(--text-2)">${esc(p.query)}</div></div>`;

  // agent runs + tool calls summary
  if (runs.length || tools.length) {
    html += `<div class="grid" style="grid-template-columns:1fr 1fr;margin-bottom:14px">`;
    if (runs.length) {
      html += `<div><div class="eyebrow" style="margin-bottom:6px">Agent 运行 (${runs.length})</div>`;
      runs.forEach((a) => { html += `<div class="tool-row"><span class="mono" style="flex:1">${esc(a.agent_name || a.name || a.role || "agent")}</span><span class="muted mono">${(a.total_tokens ?? a.tokens ?? 0)} tok</span></div>`; });
      html += `</div>`;
    }
    if (tools.length) {
      html += `<div><div class="eyebrow" style="margin-bottom:6px">工具调用 (${tools.length})</div>`;
      tools.forEach((tc) => { const ok = tc.success ?? tc.ok; html += `<div class="tool-row"><span class="dot ${ok === false ? "is-err" : "is-ok"}"></span><span class="mono" style="flex:1">${esc(tc.tool_name || tc.name || "tool")}</span><span class="muted mono">${tc.elapsed_ms != null ? Math.round(tc.elapsed_ms) + "ms" : ""}</span></div>`; });
      html += `</div>`;
    }
    html += `</div>`;
  }

  // evidence
  if (evidence.length) {
    html += `<div class="eyebrow" style="margin-bottom:8px">证据链 (${evidence.length})</div>`;
    evidence.forEach((e) => {
      html += `<div class="ev-item"><div class="ev-item__top"><span class="badge badge--mute">${esc(e.source || "?")}</span>
        ${e.score != null ? `<span class="mono muted">score ${Number(e.score).toFixed(3)}</span>` : ""}
        <span class="muted" style="margin-left:auto">${esc(e.type || "")}</span></div>
        <div class="ev-item__sum">${esc(e.summary || "")}</div></div>`;
    });
  }

  // report
  if (p.report) html += `<div class="eyebrow" style="margin:16px 0 8px">诊断报告</div><div class="prose">${renderMd(p.report)}</div>`;
  else if (task.status === "running") html += `<div class="card card--pad" style="text-align:center;box-shadow:none"><span class="dot is-run"></span> <span class="muted">后台 Worker 正在诊断…</span></div>`;
  else if (task.status === "pending") html += `<div class="card card--pad" style="text-align:center;box-shadow:none"><span class="muted">排队中，等待 Worker 领取</span></div>`;

  $("inc-detail").innerHTML = html;
}

async function loadQueue() {
  try {
    const r = await fetch(`${API}/queue/status`); const d = await r.json();
    const card = $("inc-queue");
    if (!d.configured) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    $("q-depth").textContent = d.depth ?? "—"; $("q-pending").textContent = d.pending ?? "—";
    $("q-workers").textContent = d.alive_workers ?? (d.workers?.length ?? "—"); $("q-dlq").textContent = d.dlq_depth ?? "—";
    const sm = d.slots?.manual_diagnosis, sw = d.slots?.worker_diagnosis;
    $("q-slot-manual").textContent = sm ? `${sm.used}/${sm.limit}` : "—";
    $("q-slot-worker").textContent = sw ? `${sw.used}/${sw.limit}` : "—";
    $("q-stream").textContent = d.consumer_group || "";
  } catch { $("inc-queue").classList.add("hidden"); }
}

// ============================================================
// Chat
// ============================================================
const chat = { web: false, mcp: true, sending: false, lastQ: "", lastHits: [] };
$("chat-web").addEventListener("click", () => { chat.web = !chat.web; toggleChip("chat-web", chat.web); });
$("chat-mcp").addEventListener("click", () => { chat.mcp = !chat.mcp; toggleChip("chat-mcp", chat.mcp); });
function toggleChip(id, on) { const b = $(id); b.classList.toggle("is-active", on); b.querySelector("span").textContent = on ? "开" : "关"; }
$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendChat(); } });
$("chat-escalate").addEventListener("click", escalateChat);

function addMsg(role, name) {
  const box = $("chat-messages"); if (box.querySelector(".italic-empty")) box.innerHTML = "";
  const m = el("div", "msg msg--" + role);
  m.innerHTML = `<div class="msg__avatar">${role === "user" ? "你" : "AI"}</div>
    <div class="msg__body"><div class="msg__name">${esc(name)}</div><div class="msg__bubble"></div></div>`;
  box.appendChild(m); box.scrollTop = box.scrollHeight;
  return m.querySelector(".msg__bubble");
}

async function sendChat() {
  const q = $("chat-input").value.trim();
  if (!q || chat.sending) return;
  chat.sending = true; chat.lastQ = q; $("chat-input").value = "";
  addMsg("user", "提问").textContent = q;
  const bubble = addMsg("ai", "AIOps 助手");
  bubble.innerHTML = '<span class="muted">检索中…</span>';
  const trace = el("div", "rag-trace hidden");
  bubble.parentElement.appendChild(trace);
  let answer = "", progressHtml = "", hits = [];

  try {
    await readSSE(`${API}/chat/stream`, { session_id: "v2-chat", question: q, top_k: null, web_search: chat.web, mcp_tools: chat.mcp }, (ev) => {
      if (ev.type === "progress") {
        const d = ev.data || {};
        if (ev.stage === "rewrite_done" && d.rewritten) progressHtml += `<div class="prog-line">✏️ 改写：${esc(d.rewritten)}</div>`;
        else if (ev.stage === "retrieve_done") { hits = d.hits || []; chat.lastHits = hits; progressHtml += `<div class="prog-line">📚 命中 ${hits.length} 个片段</div>`; }
        else if (ev.stage === "web_done") progressHtml += `<div class="prog-line">🌐 联网 ${(d.results || []).length} 条结果</div>`;
        else if (ev.stage === "web_degraded") progressHtml += `<div class="prog-line">🌐 联网降级：${esc(d.skip_reason || "")}</div>`;
        else if (ev.stage === "tool_call") progressHtml += `<div class="prog-line">🔧 ${esc(d.name)} · ${d.elapsed_ms != null ? Math.round(d.elapsed_ms) + "ms" : ""}</div>`;
        else if (ev.stage === "stats") progressHtml += `<div class="prog-line">📊 ${d.total_tokens ?? 0} tok · ${d.total_ms != null ? Math.round(d.total_ms) + "ms" : ""}</div>`;
        renderTrace();
      } else if (ev.type === "token") {
        if (bubble.querySelector(".muted")) bubble.innerHTML = "";
        answer += ev.content || ""; bubble.innerHTML = renderMd(answer);
        $("chat-messages").scrollTop = $("chat-messages").scrollHeight;
      } else if (ev.type === "end") {
        if (!answer) bubble.innerHTML = '<span class="muted">（无回答）</span>';
      } else if (ev.type === "error") { bubble.innerHTML = `<span style="color:var(--err)">错误: ${esc(ev.message)}</span>`; }
    });
  } catch (e) { bubble.innerHTML = `<span style="color:var(--err)">请求失败: ${esc(e.message)}</span>`; }
  finally { chat.sending = false; }

  function renderTrace() {
    let body = progressHtml;
    if (hits.length) { body += '<div style="margin-top:8px" class="eyebrow">检索片段</div>'; hits.forEach((h) => { body += `<div class="hit"><span class="hit__score">${Number(h.score).toFixed(3)}</span> <span class="muted">${esc(h.source || "")} · ${esc(h.chapter || "")}</span><div style="margin-top:3px;color:var(--text-2)">${esc((h.preview || "").slice(0, 160))}</div></div>`; }); }
    trace.innerHTML = `<details><summary>检索 / 推理过程</summary><div class="rag-trace__body">${body}</div></details>`;
    trace.classList.remove("hidden");
  }
}

async function escalateChat() {
  const q = ($("chat-input").value.trim() || chat.lastQ);
  if (!q) { toast("没有可升级的问题", "err"); return; }
  try {
    const r = await fetch(`${API}/incidents/from_chat`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: "v2-chat", query: q, title: q.slice(0, 60), severity: "warning", diagnosis_mode: "fast", service: "", chat_excerpt: q, rag_hits: chat.lastHits || [] }) });
    const j = await r.json();
    toast("已升级为事件，任务 ID: " + (j.task_id || ""), "ok");
    setTimeout(() => window.__goIncident(j.task_id), 600);
  } catch (e) { toast("升级失败: " + e.message, "err"); }
}

// ============================================================
// Documents + eval
// ============================================================
$("docs-refresh").addEventListener("click", loadDocs);
$("eval-refresh").addEventListener("click", loadEvalReports);
$("dropzone").addEventListener("click", () => $("upload-input").click());
$("upload-input").addEventListener("change", (e) => { if (e.target.files[0]) uploadDoc(e.target.files[0]); });
["dragover", "dragenter"].forEach((ev) => $("dropzone").addEventListener(ev, (e) => { e.preventDefault(); $("dropzone").classList.add("is-over"); }));
["dragleave", "drop"].forEach((ev) => $("dropzone").addEventListener(ev, (e) => { e.preventDefault(); $("dropzone").classList.remove("is-over"); }));
$("dropzone").addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) uploadDoc(f); });

async function loadDocs() {
  const box = $("docs-list"); box.innerHTML = '<span class="italic-empty">加载中…</span>';
  try {
    const r = await fetch(`${API}/documents`); const j = await r.json();
    const docs = j?.data?.documents || [];
    if (!docs.length) { box.innerHTML = '<span class="italic-empty">暂无已索引文档</span>'; return; }
    box.innerHTML = "";
    docs.forEach((d) => {
      const row = el("div", "doc-row");
      row.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--brand-1)" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
        <span style="flex:1;word-break:break-all">${esc(d.source)}</span>
        <span class="badge badge--mute">${d.chunk_count} 块</span>
        <button class="btn btn--danger btn--sm" data-src="${esc(d.source)}">删</button>`;
      row.querySelector("button").addEventListener("click", () => delDoc(d.source));
      box.appendChild(row);
    });
  } catch (e) { box.innerHTML = `<span style="color:var(--err)">加载失败: ${esc(e.message)}</span>`; }
}
async function uploadDoc(file) {
  const res = $("upload-result"); res.innerHTML = '<span class="muted">上传中…</span>';
  const token = window.prompt("请输入知识库管理 Token (X-KB-Admin-Token)", "");
  if (token === null) { res.innerHTML = ""; return; }
  try {
    const fd = new FormData(); fd.append("file", file);
    const r = await fetch(`${API}/documents/upload`, { method: "POST", headers: token ? { "X-KB-Admin-Token": token } : {}, body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j?.message || j?.detail || r.statusText);
    res.innerHTML = `<span style="color:var(--ok)">✓ 已索引 ${j?.data?.chunks_indexed ?? 0} 块（${j?.data?.bytes ?? 0} 字节）</span>`;
    loadDocs();
  } catch (e) { res.innerHTML = `<span style="color:var(--err)">上传失败: ${esc(e.message)}</span>`; }
  $("upload-input").value = "";
}
async function delDoc(source) {
  if (!confirm(`删除文档「${source}」及其所有索引块？`)) return;
  const token = window.prompt("请输入知识库管理 Token", "");
  if (token === null) return;
  try {
    const r = await fetch(`${API}/documents/${encodeURIComponent(source)}`, { method: "DELETE", headers: token ? { "X-KB-Admin-Token": token } : {} });
    const j = await r.json(); if (!r.ok) throw new Error(j?.message || r.statusText);
    toast(`已删除 ${j?.data?.deleted_chunks ?? 0} 块`, "ok"); loadDocs();
  } catch (e) { toast("删除失败: " + e.message, "err"); }
}

const evalState = { selected: null };
async function loadEvalReports() {
  const box = $("eval-list"); box.innerHTML = '<span class="italic-empty">加载中…</span>';
  try {
    const r = await fetch(`${API}/eval/reports?limit=30`); const j = await r.json();
    const items = j.items || [];
    if (!items.length) { box.innerHTML = '<span class="italic-empty">暂无评估报告</span>'; return; }
    box.innerHTML = "";
    items.forEach((it) => {
      const s = it.summary || {};
      const metric = it.mode === "ragas" ? (s.faithfulness != null ? `faith ${Number(s.faithfulness).toFixed(2)}` : "") : (s.hit_at_k != null ? `hit@k ${Number(s.hit_at_k).toFixed(2)}` : "");
      const row = el("div", "eval-row" + (evalState.selected === it.name ? " is-sel" : ""));
      row.innerHTML = `<div style="flex:1;min-width:0"><div style="font-weight:600">${it.mode === "ragas" ? "RAGAS" : "检索"} · <span class="muted" style="font-weight:400">${s.rows ?? "?"} 题</span></div>
        <div class="muted mono" style="font-size:10px;margin-top:2px">${fmtTime(it.generated_at)}</div></div>
        <span class="badge badge--info">${metric}</span>`;
      row.addEventListener("click", () => loadEvalDetail(it.name));
      box.appendChild(row);
    });
  } catch (e) { box.innerHTML = `<span style="color:var(--err)">加载失败: ${esc(e.message)}</span>`; }
}
async function loadEvalDetail(name) {
  evalState.selected = name; loadEvalReports();
  const box = $("eval-detail"); box.innerHTML = '<span class="italic-empty">加载中…</span>';
  try {
    const r = await fetch(`${API}/eval/reports/${encodeURIComponent(name)}`); const d = await r.json();
    const metrics = [];
    if (d.mode === "ragas") { const a = d.averages || d.openevals_averages || {}; ["faithfulness", "answer_relevancy", "context_precision", "context_recall"].forEach((k) => { if (a[k] != null) metrics.push([k, Number(a[k]).toFixed(3)]); }); }
    else { [["hit@k", d.hit_at_k], ["mrr@k", d.mrr_at_k], ["recall@k", d.recall_at_k]].forEach(([k, v]) => { if (v != null) metrics.push([k, Number(v).toFixed(3)]); }); }
    let html = `<div class="card-h"><div class="card-title">${d.mode === "ragas" ? "RAGAS 评估" : "检索评估"}</div><span class="muted">${d.rows ?? "?"} 题 · ${d.elapsed_sec != null ? d.elapsed_sec.toFixed(1) + "s" : ""}</span></div>`;
    html += `<div class="metric-grid">` + metrics.map(([k, v]) => `<div class="metric"><div class="metric__v">${v}</div><div class="metric__k">${k}</div></div>`).join("") + `</div>`;
    if (d.mode !== "ragas") html += `<div class="muted" style="font-size:11.5px;margin-top:12px">hybrid: ${d.hybrid ? "✓" : "✗"} · rerank: ${d.rerank ? "✓" : "✗"} · k=${d.retrieve_k ?? "?"}</div>`;
    box.innerHTML = html;
    loadLowScores(name, d.mode);
  } catch (e) { box.innerHTML = `<span style="color:var(--err)">加载失败: ${esc(e.message)}</span>`; }
}
async function loadLowScores(name, mode) {
  try {
    const metric = mode === "ragas" ? "faithfulness" : "hit";
    const r = await fetch(`${API}/eval/reports/${encodeURIComponent(name)}/low-scores?metric=${metric}&threshold=0.5&limit=10`);
    const j = await r.json(); const items = j.items || [];
    if (!items.length) return;
    let html = `<div class="eyebrow" style="margin:16px 0 8px">低分题 (${items.length})</div>`;
    items.forEach((it) => { html += `<div class="ev-item"><div class="ev-item__top"><span class="hit__score">${Number(it.score).toFixed(2)}</span> <span class="muted">${esc(it.scenario || "")}</span></div><div class="ev-item__sum">${esc(it.question || "")}</div></div>`; });
    $("eval-detail").innerHTML += html;
  } catch {}
}

// ============================================================
// Wiki
// ============================================================
const wiki = { pages: [], cat: "all", selected: null, loaded: false };
$("wiki-refresh").addEventListener("click", () => { wiki.loaded = false; wikiEnter(); });
document.querySelectorAll("[data-wiki-cat]").forEach((b) => b.addEventListener("click", () => {
  wiki.cat = b.dataset.wikiCat;
  document.querySelectorAll("[data-wiki-cat]").forEach((x) => x.classList.toggle("is-active", x === b));
  wikiRenderPages();
}));
$("wiki-content").addEventListener("click", (e) => { const a = e.target.closest(".wikilink"); if (a) { const [c, s] = a.dataset.wikiRef.split("/"); wikiOpen(c, s); } });

async function wikiEnter() {
  if (wiki.loaded) return;
  try {
    const [oR, pR, lR] = await Promise.all([fetch(`${API}/wiki/overview`), fetch(`${API}/wiki/pages?limit=300`), fetch(`${API}/wiki/log?limit=30`)]);
    const ov = await oR.json(); wiki.pages = (await pR.json()).items || []; const log = (await lR.json()).items || [];
    $("wiki-overview").textContent = ov.enabled
      ? `已启用 · 故障模式 ${ov.pages?.patterns ?? 0} 页 · 服务 ${ov.pages?.services ?? 0} 页${ov.recall_enabled ? " · 召回开启" : ""}`
      : "Wiki 未启用";
    $("wiki-log").innerHTML = log.length ? log.map((l) => `<div>[${esc(l.date)}] ${esc(l.entry)}</div>`).join("") : '<span class="italic-empty">暂无流水</span>';
    wikiRenderPages(); wiki.loaded = true;
  } catch (e) { $("wiki-overview").textContent = "加载失败: " + e.message; }
}
function wikiRenderPages() {
  const box = $("wiki-pages");
  const items = wiki.pages.filter((p) => wiki.cat === "all" || p.category === wiki.cat);
  if (!items.length) { box.innerHTML = '<span class="italic-empty">暂无页面</span>'; return; }
  box.innerHTML = "";
  items.forEach((p) => {
    const ref = p.ref || `${p.category}/${p.slug}`;
    const item = el("div", "wiki-page" + (wiki.selected === ref ? " is-sel" : ""));
    item.innerHTML = `<div class="wiki-page__slug">${esc(p.slug)}</div><div class="wiki-page__prev">${esc(p.preview || "")}</div>`;
    item.addEventListener("click", () => wikiOpen(p.category, p.slug));
    box.appendChild(item);
  });
}
async function wikiOpen(category, slug) {
  wiki.selected = `${category}/${slug}`; wikiRenderPages();
  $("wiki-title").textContent = slug; $("wiki-content").innerHTML = '<span class="italic-empty">加载中…</span>';
  try {
    const r = await fetch(`${API}/wiki/pages/${encodeURIComponent(category)}/${encodeURIComponent(slug)}`);
    const d = await r.json();
    $("wiki-meta").textContent = `${category} · ${d.size_bytes ?? 0}B · ${fmtTime(d.modified_at)}`;
    $("wiki-content").innerHTML = renderWikiMd(d.content || "");
  } catch (e) { $("wiki-content").innerHTML = `<span style="color:var(--err)">加载失败: ${esc(e.message)}</span>`; }
}

// ============================================================
// Approvals
// ============================================================
$("approval-entry").addEventListener("click", openDrawer);
$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-mask").addEventListener("click", closeDrawer);
function openDrawer() { $("approval-drawer").classList.add("is-open"); $("drawer-mask").classList.add("is-open"); refreshApprovals(); }
function closeDrawer() { $("approval-drawer").classList.remove("is-open"); $("drawer-mask").classList.remove("is-open"); }

async function refreshApprovals() {
  try {
    const r = await fetch(`${API}/approvals/pending?limit=50`); const j = await r.json();
    if (j.available === false) { $("approval-entry").classList.add("hidden"); return; }
    const items = j.items || []; const entry = $("approval-entry"); const badge = $("approval-badge");
    if (items.length) { entry.classList.remove("hidden"); badge.classList.remove("hidden"); badge.textContent = items.length; }
    else { badge.classList.add("hidden"); }
    const list = $("approval-list");
    if (!items.length) { list.innerHTML = '<div class="italic-empty" style="text-align:center;padding:30px 0">暂无待审批项</div>'; return; }
    list.innerHTML = "";
    items.forEach((a) => {
      const card = el("div", "approval-card");
      card.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center"><span class="mono" style="font-weight:600">${esc(a.tool_name)}</span><span class="muted" style="font-size:10px">${ago(a.created_at)}</span></div>
        ${a.impact_summary ? `<div style="font-size:12px;color:var(--text-2);margin:6px 0">${esc(a.impact_summary)}</div>` : ""}
        ${a.reason ? `<div class="muted" style="font-size:11px">${esc(a.reason)}</div>` : ""}
        <pre class="mono" style="font-size:10.5px;background:var(--surface-3);padding:8px;border-radius:8px;margin:8px 0;overflow-x:auto">${esc(JSON.stringify(a.tool_args || {}, null, 1))}</pre>
        <div style="display:flex;gap:8px"><button class="btn btn--primary btn--sm" style="flex:1" data-act="approved">批准</button><button class="btn btn--danger btn--sm" style="flex:1" data-act="denied">拒绝</button></div>`;
      card.querySelector('[data-act="approved"]').addEventListener("click", () => decideApproval(a.id, "approved"));
      card.querySelector('[data-act="denied"]').addEventListener("click", () => decideApproval(a.id, "denied"));
      list.appendChild(card);
    });
  } catch { $("approval-entry").classList.add("hidden"); }
}
async function decideApproval(id, decision) {
  try {
    await fetch(`${API}/approvals/${encodeURIComponent(id)}/decide`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, decided_by: "web-v2", reason: "" }) });
    toast(decision === "approved" ? "已批准" : "已拒绝", "ok"); refreshApprovals();
  } catch (e) { toast("操作失败: " + e.message, "err"); }
}
refreshApprovals(); setInterval(refreshApprovals, 10000);
