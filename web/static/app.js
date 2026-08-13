"use strict";

const IS_ADMIN = document.body.dataset.admin === "1";

async function api(path, opts) {
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  return { ok: resp.ok, status: resp.status, data };
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------- Tab 切换 ----------------
const loaded = {};
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("on"));
    document.querySelectorAll(".pane").forEach((x) => x.classList.remove("on"));
    t.classList.add("on");
    const name = t.dataset.tab;
    document.getElementById("pane-" + name).classList.add("on");
    if (!loaded[name]) { loaded[name] = true; (LOADERS[name] || (() => {}))(); }
  });
});

// ---------------- 路由 ----------------
let ROUTES = [];
let machineMenu = null;

function closeMachineMenu() {
  if (machineMenu) { machineMenu.remove(); machineMenu = null; }
}

// 多台机器时在点击位置弹出小菜单，选择机器后跳转 /<machine>/<key>/。
function showMachineMenu(x, y, route) {
  closeMachineMenu();
  const menu = document.createElement("div");
  menu.className = "machine-menu";
  menu.innerHTML = route.machines.map((m) =>
    `<div class="machine-item" data-slug="${esc(m.slug)}">${esc(m.display_name)}</div>`).join("");
  document.body.appendChild(menu);
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth - menu.offsetWidth - 8)) + "px";
  menu.style.top = Math.max(4, Math.min(y, window.innerHeight - menu.offsetHeight - 8)) + "px";
  menu.querySelectorAll(".machine-item").forEach((it) => {
    it.addEventListener("click", (e) => {
      e.stopPropagation();
      location.href = "/" + it.dataset.slug + "/" + route.key + "/";
    });
  });
  machineMenu = menu;
}

document.addEventListener("click", (e) => {
  if (machineMenu && !machineMenu.contains(e.target)) closeMachineMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMachineMenu();
});

async function loadRoutes() {
  const { data } = await api("/api/me");
  const grid = document.getElementById("routeGrid");
  if (!data || !data.routes || data.routes.length === 0) {
    grid.innerHTML = '<div class="muted">暂无可见路由。请联系管理员开通。</div>';
    return;
  }
  ROUTES = data.routes;
  grid.innerHTML = ROUTES.map((r) => `
    <div class="route-card" data-key="${esc(r.key)}">
      <div class="rn">${esc(r.display_name)}</div>
      <div class="rk">${r.machines.length > 1 ? r.machines.length + " 台" : esc(r.machines[0].display_name)}</div>
    </div>`).join("");
  grid.querySelectorAll(".route-card").forEach((c) => {
    c.addEventListener("click", (e) => {
      const r = ROUTES.find((x) => x.key === c.dataset.key);
      if (!r || !r.machines.length) return;
      // 上报卡片点击（排序用，fire-and-forget，不阻塞跳转、不当场重排）。
      fetch("/api/track-click", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: r.key }),
        keepalive: true,
      }).catch(() => {});
      if (r.machines.length === 1) {
        location.href = "/" + r.machines[0].slug + "/" + r.key + "/";
      } else {
        e.stopPropagation();
        showMachineMenu(e.clientX, e.clientY, r);
      }
    });
  });
}

// ---------------- 用户管理 ----------------
let ALL_ROUTES = [];
async function loadUsers() {
  const { data } = await api("/api/users");
  if (!data) return;
  ALL_ROUTES = [];
  const seenKeys = new Set();
  // 同一 key 可能有多台机器的路由行，授权按 key 去重展示。
  for (const r of data.routes) {
    if (!seenKeys.has(r.key)) { seenKeys.add(r.key); ALL_ROUTES.push(r); }
  }
  const tb = document.querySelector("#userTable tbody");
  tb.innerHTML = data.users.map((u) => {
    const pills = u.is_admin
      ? '<span class="muted">全部（admin）</span>'
      : ALL_ROUTES.map((r) => `<span class="pill ${u.routes.includes(r.key) ? "on" : ""}" data-uid="${u.id}" data-key="${esc(r.key)}">${esc(r.display_name)}</span>`).join(" ");
    const del = u.is_admin ? "" : `<button class="btn sm danger" data-del="${u.id}">删除</button>`;
    return `<tr>
      <td>${esc(u.username)}</td>
      <td>${u.is_admin ? '<span class="badge-admin">admin</span>' : "用户"}</td>
      <td><div class="pills">${pills}</div></td>
      <td class="muted">${esc(u.created_at)}</td>
      <td>${del}</td></tr>`;
  }).join("");

  tb.querySelectorAll(".pill[data-uid]").forEach((p) => {
    p.addEventListener("click", async () => {
      p.classList.toggle("on");
      const uid = p.dataset.uid;
      const keys = Array.from(tb.querySelectorAll(`.pill.on[data-uid="${uid}"]`)).map((x) => x.dataset.key);
      const { ok, data } = await api(`/api/users/${uid}/routes`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ routes: keys }),
      });
      if (!ok) { alert((data && data.error) || "保存失败"); p.classList.toggle("on"); }
    });
  });
  tb.querySelectorAll("[data-del]").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!confirm("确认删除该用户？")) return;
      const { ok, data } = await api(`/api/users/${b.dataset.del}`, { method: "DELETE" });
      if (ok) loadUsers(); else alert((data && data.error) || "删除失败");
    });
  });
}
function bindUserAdd() {
  document.getElementById("nuAdd").addEventListener("click", async () => {
    const username = document.getElementById("nuUser").value.trim();
    const password = document.getElementById("nuPass").value;
    const err = document.getElementById("nuErr");
    err.textContent = "";
    const { ok, data } = await api("/api/users", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (ok) {
      document.getElementById("nuUser").value = "";
      document.getElementById("nuPass").value = "";
      loadUsers();
    } else { err.textContent = (data && data.error) || "添加失败"; }
  });
}

// ---------------- 活跃记录 ----------------
let logPage = 1, logTotal = 0, logFilterVal = "";
async function loadActiveLast() {
  const { data } = await api("/api/active/last");
  if (!data) return;
  document.querySelector("#lastTable tbody").innerHTML = data.users.map((u) =>
    `<tr><td>${esc(u.username)}</td><td class="muted">${u.last_ts ? esc(u.last_ts) : "—"}</td></tr>`).join("");
  const sel = document.getElementById("logFilter");
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部用户</option>' +
    data.users.map((u) => `<option value="${esc(u.username)}">${esc(u.username)}</option>`).join("");
  sel.value = cur;
}
async function loadActiveLog() {
  const q = new URLSearchParams({ page: logPage });
  if (logFilterVal) q.set("username", logFilterVal);
  const { data } = await api("/api/active/log?" + q.toString());
  if (!data) return;
  logTotal = data.total;
  const tb = document.querySelector("#logTable tbody");
  tb.innerHTML = data.rows.length
    ? data.rows.map((r) => `<tr><td>${esc(r.username)}</td><td>${esc(r.path)}</td><td class="muted">${esc(r.start_ts)}</td><td class="muted">${esc(r.end_ts)}</td><td>${esc(String(r.count))}</td><td class="muted">${esc(r.ip)}</td></tr>`).join("")
    : '<tr><td colspan="6" class="muted">暂无记录</td></tr>';
  const pages = Math.max(1, Math.ceil(logTotal / data.per_page));
  document.getElementById("logInfo").textContent = `第 ${logPage} / ${pages} 页 · 共 ${logTotal} 条`;
  document.getElementById("logPrev").disabled = logPage <= 1;
  document.getElementById("logNext").disabled = logPage >= pages;
}
function loadActive() {
  loadActiveLast();
  loadActiveLog();
  document.getElementById("logFilter").addEventListener("change", (e) => {
    logFilterVal = e.target.value; logPage = 1; loadActiveLog();
  });
  document.getElementById("logPrev").addEventListener("click", () => { if (logPage > 1) { logPage--; loadActiveLog(); } });
  document.getElementById("logNext").addEventListener("click", () => {
    const pages = Math.max(1, Math.ceil(logTotal / 10));
    if (logPage < pages) { logPage++; loadActiveLog(); }
  });
}

// ---------------- 配置 ----------------
async function loadConfig() {
  const { data } = await api("/api/settings");
  if (data) document.getElementById("sessDays").value = data.session_days;
  document.getElementById("sessSave").addEventListener("click", async () => {
    const days = parseInt(document.getElementById("sessDays").value, 10);
    const msg = document.getElementById("sessMsg");
    const { ok, data } = await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_days: days }),
    });
    msg.textContent = ok ? "已保存" : ((data && data.error) || "保存失败");
    msg.className = ok ? "form-ok" : "form-err";
  });
  bindRouteAdd();
  loadRouteTable();
}
let ALL_MACHINES = [];
function machineOptions(selected) {
  return ALL_MACHINES.map((m) =>
    `<option value="${esc(m.slug)}" ${m.slug === selected ? "selected" : ""}>${esc(m.display_name)}</option>`).join("");
}
async function loadRouteTable() {
  const { data } = await api("/api/routes");
  if (!data) return;
  ALL_MACHINES = data.machines || [];
  const nrMachine = document.getElementById("nrMachine");
  nrMachine.innerHTML = machineOptions(nrMachine.value);
  const tb = document.querySelector("#routeTable tbody");
  tb.innerHTML = data.routes.map((r) => `<tr data-id="${r.id}">
      <td><code>${esc(r.key)}</code></td>
      <td><select class="rt-machine">${machineOptions(r.machine)}</select></td>
      <td><input type="text" class="rt-name" value="${esc(r.display_name)}"></td>
      <td><input type="text" class="rt-host" value="${esc(r.upstream_host)}"></td>
      <td><input type="number" class="rt-port" value="${esc(r.upstream_port)}" style="width:90px"></td>
      <td style="text-align:center"><input type="checkbox" class="rt-strip" ${r.strip_prefix ? "checked" : ""}></td>
      <td><button class="btn sm primary" data-save="${r.id}">保存</button>
          <button class="btn sm danger" data-del="${r.id}">删除</button></td></tr>`).join("");
  tb.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", async () => {
    const tr = b.closest("tr");
    const body = {
      machine: tr.querySelector(".rt-machine").value,
      display_name: tr.querySelector(".rt-name").value.trim(),
      upstream_host: tr.querySelector(".rt-host").value.trim(),
      upstream_port: parseInt(tr.querySelector(".rt-port").value, 10),
      strip_prefix: tr.querySelector(".rt-strip").checked,
    };
    const { ok, data } = await api(`/api/routes/${b.dataset.save}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    alert(ok ? "已保存" : ((data && data.error) || "保存失败"));
  }));
  tb.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("确认删除该路由？删除后用户将无法访问该网站。")) return;
    const { ok, data } = await api(`/api/routes/${b.dataset.del}`, { method: "DELETE" });
    if (ok) loadRouteTable(); else alert((data && data.error) || "删除失败");
  }));
}
function bindRouteAdd() {
  document.getElementById("nrAdd").addEventListener("click", async () => {
    const err = document.getElementById("nrErr");
    err.textContent = "";
    const body = {
      machine: document.getElementById("nrMachine").value,
      key: document.getElementById("nrKey").value.trim(),
      display_name: document.getElementById("nrName").value.trim(),
      upstream_host: document.getElementById("nrHost").value.trim(),
      upstream_port: parseInt(document.getElementById("nrPort").value, 10),
      strip_prefix: document.getElementById("nrStrip").checked,
    };
    const { ok, data } = await api("/api/routes", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (ok) {
      document.getElementById("nrKey").value = "";
      document.getElementById("nrName").value = "";
      document.getElementById("nrPort").value = "";
      document.getElementById("nrStrip").checked = false;
      loadRouteTable();
    } else { err.textContent = (data && data.error) || "添加失败"; }
  });
}

// ---------------- 我的 ----------------
function bindChangePassword() {
  document.getElementById("cpSave").addEventListener("click", async () => {
    const old = document.getElementById("cpOld").value;
    const np = document.getElementById("cpNew").value;
    const np2 = document.getElementById("cpNew2").value;
    const err = document.getElementById("cpErr"), ok = document.getElementById("cpOk");
    err.textContent = ""; ok.textContent = "";
    if (np !== np2) { err.textContent = "两次输入的新密码不一致"; return; }
    const r = await api("/api/change-password", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_password: old, new_password: np }),
    });
    if (r.ok) {
      ok.textContent = "密码已修改";
      document.getElementById("cpOld").value = "";
      document.getElementById("cpNew").value = "";
      document.getElementById("cpNew2").value = "";
    } else { err.textContent = (r.data && r.data.error) || "修改失败"; }
  });
}

// ---------------- 机器监控 ----------------
const MET_WINDOWS = [
  ["1m", "1 分钟"], ["10m", "10 分钟"], ["30m", "30 分钟"], ["1h", "1 小时"],
  ["3h", "3 小时"], ["1d", "1 天"], ["3d", "3 天"], ["7d", "7 天"],
];

function fmtBytes(n) {
  if (n == null) return "—";
  const g = n / (1024 ** 3);
  if (g >= 1) return g.toFixed(1) + "G";
  return (n / (1024 ** 2)).toFixed(0) + "M";
}

function fmtTime(ts, win) {
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  const hms = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  if (["1d", "3d", "7d"].includes(win)) return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  return hms;
}

// 在指定锚点画一个带底色的读数小标签。anchor: "left"|"right"（相对 x 的对齐）。
function drawTag(ctx, x, y, text, anchor, color) {
  ctx.font = "10px sans-serif";
  const w = ctx.measureText(text).width + 8, h = 14;
  const bx = anchor === "right" ? x - w : x;
  const by = Math.max(0, Math.min(y - h / 2, ctx.canvas.clientHeight - h));
  ctx.fillStyle = color;
  ctx.fillRect(bx, by, w, h);
  ctx.fillStyle = "#fff"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(text, bx + 4, by + h / 2);
}

class MetricChart {
  // opts: { valKey, hasRight, usedKey, totalKey }
  constructor(canvas, color, opts) {
    this.canvas = canvas; this.color = color; this.opts = opts;
    this.points = []; this.win = "1h"; this.hoverX = null;
    canvas.addEventListener("mousemove", (e) => {
      const r = canvas.getBoundingClientRect();
      this.hoverX = e.clientX - r.left; this.draw();
    });
    canvas.addEventListener("mouseleave", () => { this.hoverX = null; this.draw(); });
  }

  setData(points, win) { this.points = points; this.win = win; this.hoverX = null; this.draw(); }

  draw() {
    const cv = this.canvas, dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    if (!W || !H) return;
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const padL = 40, padR = this.opts.hasRight ? 50 : 14, padT = 8, padB = 20;
    const x0 = padL, y0 = padT, w = W - padL - padR, h = H - padT - padB;
    const pts = this.points;
    const total = pts.length ? pts[pts.length - 1][this.opts.totalKey] : 0;

    // 网格 + 左轴（%）/右轴（绝对值）刻度
    ctx.font = "10px sans-serif"; ctx.textBaseline = "middle"; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const frac = i / 4, yy = y0 + h - frac * h;
      ctx.strokeStyle = "#eef0f3";
      ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x0 + w, yy); ctx.stroke();
      ctx.fillStyle = "#8a8f99"; ctx.textAlign = "right";
      ctx.fillText((frac * 100).toFixed(0) + "%", x0 - 6, yy);
      if (this.opts.hasRight && total) {
        ctx.textAlign = "left"; ctx.fillText(fmtBytes(frac * total), x0 + w + 6, yy);
      }
    }

    if (!pts.length) {
      ctx.fillStyle = "#8a8f99"; ctx.textAlign = "center";
      ctx.fillText("暂无数据", W / 2, H / 2);
      return;
    }

    const ts0 = pts[0].ts, ts1 = pts[pts.length - 1].ts, span = Math.max(1, ts1 - ts0);
    const px = (ts) => x0 + ((ts - ts0) / span) * w;
    const py = (v) => y0 + h - (v / 100) * h;
    const vk = this.opts.valKey;

    // 折线 + 填充
    ctx.beginPath(); ctx.strokeStyle = this.color; ctx.lineWidth = 1.5;
    pts.forEach((p, i) => { const X = px(p.ts), Y = py(p[vk]); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
    ctx.stroke();
    ctx.lineTo(px(ts1), y0 + h); ctx.lineTo(px(ts0), y0 + h); ctx.closePath();
    ctx.fillStyle = this.color + "22"; ctx.fill();

    // x 轴时间标签（起 / 中 / 止）
    ctx.fillStyle = "#8a8f99"; ctx.textBaseline = "top";
    [[0, "left"], [Math.floor(pts.length / 2), "center"], [pts.length - 1, "right"]].forEach(([idx, al]) => {
      ctx.textAlign = al; ctx.fillText(fmtTime(pts[idx].ts, this.win), px(pts[idx].ts), y0 + h + 4);
    });

    // 悬停准星：竖线交曲线点，向左右轴拉横线并读数
    if (this.hoverX != null && this.hoverX >= x0 && this.hoverX <= x0 + w) {
      let best = 0, bd = Infinity;
      pts.forEach((p, i) => { const d = Math.abs(px(p.ts) - this.hoverX); if (d < bd) { bd = d; best = i; } });
      const p = pts[best], X = px(p.ts), Y = py(p[vk]);
      ctx.strokeStyle = "#b0b4bb"; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(X, y0); ctx.lineTo(X, y0 + h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x0, Y); ctx.lineTo(x0 + w, Y); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = this.color; ctx.beginPath(); ctx.arc(X, Y, 3, 0, Math.PI * 2); ctx.fill();
      drawTag(ctx, x0 - 1, Y, p[vk].toFixed(1) + "%", "right", this.color);
      if (this.opts.hasRight) drawTag(ctx, x0 + w + 1, Y, fmtBytes(p[this.opts.usedKey]), "left", this.color);
    }
  }
}

let MET_CHARTS = null, MET_WIN = "1h";

function buildMetricCharts() {
  MET_CHARTS = [
    new MetricChart(document.getElementById("cpuCanvas"), "#3b6ef5", { valKey: "cpu", hasRight: false }),
    new MetricChart(document.getElementById("memCanvas"), "#2f9e44", { valKey: "mem_pct", hasRight: true, usedKey: "mem_used", totalKey: "mem_total" }),
    new MetricChart(document.getElementById("diskCanvas"), "#e8893b", { valKey: "disk_pct", hasRight: true, usedKey: "disk_used", totalKey: "disk_total" }),
  ];
}

async function loadMetrics() {
  const { data } = await api("/api/metrics?window=" + MET_WIN);
  if (!data) return;
  const pts = data.points || [], c = data.current;
  MET_CHARTS.forEach((ch) => ch.setData(pts, MET_WIN));
  document.getElementById("cpuTitle").textContent = "CPU 利用率" + (c ? `　${c.cpu_pct.toFixed(1)}%` : "");
  document.getElementById("memTitle").textContent = "内存" + (c ? `　${c.mem_pct.toFixed(1)}%　${fmtBytes(c.mem_used)}/${fmtBytes(c.mem_total)}` : "");
  document.getElementById("diskTitle").textContent = "磁盘 /" + (c ? `　${c.disk_pct.toFixed(1)}%　${fmtBytes(c.disk_used)}/${fmtBytes(c.disk_total)}` : "");
}

function initMetrics() {
  const modal = document.getElementById("metricsModal");
  const sel = document.getElementById("metWindow");
  sel.innerHTML = MET_WINDOWS.map(([v, t]) => `<option value="${v}" ${v === "1h" ? "selected" : ""}>${t}</option>`).join("");
  document.getElementById("metricsBtn").addEventListener("click", () => {
    modal.classList.add("on");
    if (!MET_CHARTS) buildMetricCharts();
    loadMetrics();
  });
  document.getElementById("metClose").addEventListener("click", () => modal.classList.remove("on"));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("on"); });
  sel.addEventListener("change", () => { MET_WIN = sel.value; loadMetrics(); });
  window.addEventListener("resize", () => {
    if (modal.classList.contains("on") && MET_CHARTS) MET_CHARTS.forEach((ch) => ch.draw());
  });
}

const LOADERS = {
  routes: loadRoutes,
  users: loadUsers,
  active: loadActive,
  config: loadConfig,
  me: () => {},
};

// 初始化：路由页默认加载；绑定一次性事件。
loaded.routes = true;
loadRoutes();
if (IS_ADMIN) { bindUserAdd(); initMetrics(); }
if (!IS_ADMIN) bindChangePassword();
