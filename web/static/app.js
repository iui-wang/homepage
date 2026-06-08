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
async function loadRoutes() {
  const { data } = await api("/api/me");
  const grid = document.getElementById("routeGrid");
  if (!data || !data.routes || data.routes.length === 0) {
    grid.innerHTML = '<div class="muted">暂无可见路由。请联系管理员开通。</div>';
    return;
  }
  grid.innerHTML = data.routes.map((r) => `
    <div class="route-card" data-key="${esc(r.key)}">
      <div class="rn">${esc(r.display_name)}</div>
      <div class="rk">/${esc(r.key)}</div>
      <div class="go">点击进入 →</div>
    </div>`).join("");
  grid.querySelectorAll(".route-card").forEach((c) => {
    c.addEventListener("click", () => { location.href = "/" + c.dataset.key + "/"; });
  });
}

// ---------------- 用户管理 ----------------
let ALL_ROUTES = [];
async function loadUsers() {
  const { data } = await api("/api/users");
  if (!data) return;
  ALL_ROUTES = data.routes;
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
    ? data.rows.map((r) => `<tr><td class="muted">${esc(r.ts)}</td><td>${esc(r.username)}</td><td>${esc(r.ip)}</td><td>${esc(r.method)}</td><td>${esc(r.path)}</td></tr>`).join("")
    : '<tr><td colspan="5" class="muted">暂无记录</td></tr>';
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
async function loadRouteTable() {
  const { data } = await api("/api/routes");
  if (!data) return;
  const tb = document.querySelector("#routeTable tbody");
  tb.innerHTML = data.routes.map((r) => `<tr data-id="${r.id}">
      <td><code>${esc(r.key)}</code></td>
      <td><input type="text" class="rt-name" value="${esc(r.display_name)}"></td>
      <td><input type="text" class="rt-host" value="${esc(r.upstream_host)}"></td>
      <td><input type="number" class="rt-port" value="${esc(r.upstream_port)}" style="width:90px"></td>
      <td style="text-align:center"><input type="checkbox" class="rt-strip" ${r.strip_prefix ? "checked" : ""}></td>
      <td><button class="btn sm primary" data-save="${r.id}">保存</button>
          <button class="btn sm danger" data-del="${r.id}">删除</button></td></tr>`).join("");
  tb.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", async () => {
    const tr = b.closest("tr");
    const body = {
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
if (IS_ADMIN) bindUserAdd();
bindChangePassword();
