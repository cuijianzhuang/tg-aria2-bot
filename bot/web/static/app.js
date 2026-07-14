const loginView = document.getElementById("login-view");
const dashboardView = document.getElementById("dashboard-view");

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

function showLogin() {
  loginView.hidden = false;
  dashboardView.hidden = true;
}

function showDashboard() {
  loginView.hidden = true;
  dashboardView.hidden = false;
  refreshAll();
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = document.getElementById("login-password").value;
  const errorEl = document.getElementById("login-error");
  errorEl.textContent = "";
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password }) });
    showDashboard();
  } catch (err) {
    errorEl.textContent = "登录失败：密码错误或后台未配置 ADMIN_PASSWORD";
  }
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

document.getElementById("limit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const speed = document.getElementById("limit-input").value.trim();
  if (!speed) return;
  await api("/api/limit", { method: "POST", body: JSON.stringify({ speed }) });
  document.getElementById("limit-input").value = "";
});

async function restartService(service, statusEl) {
  statusEl.textContent = "重启中…";
  try {
    await api("/api/settings/restart", { method: "POST", body: JSON.stringify({ service }) });
    statusEl.textContent = "已重启";
  } catch (err) {
    statusEl.textContent = `重启失败：${err.message}`;
  }
}

document.getElementById("rclone-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const enabled = document.getElementById("rclone-enabled").checked;
  const drive_name = document.getElementById("rclone-drive-name").value.trim();
  const drive_dir = document.getElementById("rclone-drive-dir").value.trim();
  const savedEl = document.getElementById("rclone-saved");
  const restartBtn = document.getElementById("rclone-restart-btn");
  savedEl.textContent = "";
  try {
    await api("/api/settings/rclone", {
      method: "POST",
      body: JSON.stringify({ enabled, drive_name, drive_dir }),
    });
    savedEl.textContent = "已保存，需要重启 aria2 生效";
    restartBtn.hidden = false;
  } catch (err) {
    savedEl.textContent = `保存失败：${err.message}`;
  }
});
document.getElementById("rclone-restart-btn").addEventListener("click", () => {
  restartService("aria2", document.getElementById("rclone-saved"));
});

document.getElementById("gofile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const enabled = document.getElementById("gofile-enabled").checked;
  const compress = document.getElementById("gofile-compress").checked;
  const delete_local = document.getElementById("gofile-delete-local").checked;
  const token = document.getElementById("gofile-token").value.trim();
  const savedEl = document.getElementById("gofile-saved");
  const restartBtn = document.getElementById("gofile-restart-btn");
  savedEl.textContent = "";
  try {
    await api("/api/settings/gofile", {
      method: "POST",
      body: JSON.stringify({ enabled, token, compress, delete_local }),
    });
    savedEl.textContent = "已保存，需要重启机器人生效";
    restartBtn.hidden = false;
  } catch (err) {
    savedEl.textContent = `保存失败：${err.message}`;
  }
});
document.getElementById("gofile-restart-btn").addEventListener("click", () => {
  restartService("bot", document.getElementById("gofile-saved"));
});

document.getElementById("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const current_password = document.getElementById("password-current").value;
  const new_password = document.getElementById("password-new").value;
  const savedEl = document.getElementById("password-saved");
  savedEl.textContent = "";
  try {
    await api("/api/settings/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    });
    savedEl.textContent = "已修改";
    document.getElementById("password-form").reset();
  } catch (err) {
    savedEl.textContent = `修改失败：${err.message}`;
  }
});

document.getElementById("add-user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const user_id = parseInt(document.getElementById("add-user-id").value, 10);
  const note = document.getElementById("add-user-note").value.trim() || null;
  await api("/api/users", { method: "POST", body: JSON.stringify({ user_id, note }) });
  document.getElementById("add-user-id").value = "";
  document.getElementById("add-user-note").value = "";
  loadUsers();
});

async function loadStats() {
  const s = await api("/api/stats");
  document.getElementById("stat-active").textContent = s.active;
  document.getElementById("stat-waiting").textContent = s.waiting;
  document.getElementById("stat-stopped").textContent = s.stopped;
  document.getElementById("stat-dlspeed").textContent = s.download_speed;
  document.getElementById("stat-upspeed").textContent = s.upload_speed;
  document.getElementById("stat-disk").textContent = s.disk ? `${s.disk.free} (${s.disk.percent_used}% 已用)` : "-";
}

const TASKS_PAGE_SIZE = 20;
let tasksOffset = 0;
let tasksTotal = 0;

async function loadTasks() {
  const status = document.getElementById("tasks-status-filter").value;
  const params = new URLSearchParams({ limit: TASKS_PAGE_SIZE, offset: tasksOffset });
  if (status) params.set("status", status);
  const { items, total } = await api(`/api/tasks?${params}`);
  tasksTotal = total;

  const tbody = document.querySelector("#tasks-table tbody");
  tbody.innerHTML = "";
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td class="empty-state" colspan="7">暂无任务</td></tr>`;
  }
  for (const t of items) {
    const tr = document.createElement("tr");
    const pathCell = t.gofile_link
      ? `${escapeHtml(t.save_path || "")}<br><a href="${escapeHtml(t.gofile_link)}" target="_blank" rel="noopener">☁️ gofile</a>`
      : escapeHtml(t.save_path || "");
    tr.innerHTML = `
      <td>${t.id}</td>
      <td>${t.status}</td>
      <td>${renderProgress(t.progress)}</td>
      <td>${escapeHtml(t.source_type)}</td>
      <td>${escapeHtml(t.file_name || "")}</td>
      <td class="mono">${pathCell}</td>
      <td class="actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    if (["PENDING", "ACTIVE", "PAUSED"].includes(t.status)) {
      actions.appendChild(makeButton("暂停", "secondary", () => taskAction(t.id, "pause")));
      actions.appendChild(makeButton("恢复", "secondary", () => taskAction(t.id, "resume")));
      actions.appendChild(makeButton("取消", "danger", () => taskAction(t.id, "cancel")));
    }
    tbody.appendChild(tr);
  }

  const page = Math.floor(tasksOffset / TASKS_PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(tasksTotal / TASKS_PAGE_SIZE));
  document.getElementById("tasks-page-info").textContent = `第 ${page} / ${totalPages} 页，共 ${tasksTotal} 条`;
  document.getElementById("tasks-prev").disabled = tasksOffset <= 0;
  document.getElementById("tasks-next").disabled = tasksOffset + TASKS_PAGE_SIZE >= tasksTotal;
}

document.getElementById("tasks-status-filter").addEventListener("change", () => {
  tasksOffset = 0;
  loadTasks();
});
document.getElementById("tasks-prev").addEventListener("click", () => {
  tasksOffset = Math.max(0, tasksOffset - TASKS_PAGE_SIZE);
  loadTasks();
});
document.getElementById("tasks-next").addEventListener("click", () => {
  if (tasksOffset + TASKS_PAGE_SIZE < tasksTotal) tasksOffset += TASKS_PAGE_SIZE;
  loadTasks();
});

async function taskAction(id, action) {
  try {
    await api(`/api/tasks/${id}/${action}`, { method: "POST" });
  } catch (err) {
    alert(err.message);
  }
  loadTasks();
}

async function loadUsers() {
  const users = await api("/api/users");
  const tbody = document.querySelector("#users-table tbody");
  tbody.innerHTML = "";
  if (users.length === 0) {
    tbody.innerHTML = `<tr><td class="empty-state" colspan="4">暂无白名单用户</td></tr>`;
  }
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${u.user_id}</td>
      <td>${escapeHtml(u.note || "")}</td>
      <td>${escapeHtml(u.source)}</td>
      <td class="actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    if (u.removable) {
      actions.appendChild(makeButton("删除", "danger", async () => {
        await api(`/api/users/${u.user_id}`, { method: "DELETE" });
        loadUsers();
      }));
    }
    tbody.appendChild(tr);
  }
}

function renderProgress(progress) {
  if (!progress) return "";
  return `
    <div class="progress">
      <div class="progress-bar"><div class="progress-fill" style="width:${progress.percent}%"></div></div>
      <span class="progress-label">${progress.percent}% · ${progress.speed}</span>
    </div>
  `;
}

function makeButton(label, cls, onClick) {
  const btn = document.createElement("button");
  btn.textContent = label;
  btn.className = cls;
  btn.type = "button";
  btn.addEventListener("click", onClick);
  return btn;
}

async function loadRcloneSettings() {
  const s = await api("/api/settings/rclone");
  document.getElementById("rclone-enabled").checked = s.enabled;
  document.getElementById("rclone-drive-dir").value = s.drive_dir;

  const select = document.getElementById("rclone-drive-name");
  select.innerHTML = "";
  const remotes = [...s.remotes];
  // keep whatever's already saved selectable even if it's no longer a configured
  // remote (deleted, or rclone.conf unreadable) — don't silently drop it
  if (s.drive_name && !remotes.includes(s.drive_name)) remotes.unshift(s.drive_name);

  document.getElementById("rclone-no-remotes").hidden = remotes.length > 0;

  if (remotes.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "-- 未配置任何 remote --";
    select.appendChild(opt);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const name of remotes) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    select.appendChild(opt);
  }
  select.value = s.drive_name || remotes[0];
}

async function loadGofileSettings() {
  const s = await api("/api/settings/gofile");
  document.getElementById("gofile-enabled").checked = s.enabled;
  document.getElementById("gofile-compress").checked = s.compress;
  document.getElementById("gofile-delete-local").checked = s.delete_local;
  document.getElementById("gofile-token").value = s.token;
}

async function refreshAll() {
  await Promise.all([loadStats(), loadTasks(), loadUsers(), loadRcloneSettings(), loadGofileSettings()]);
}

let pollTimer = null;

async function init() {
  try {
    await api("/api/me");
    showDashboard();
  } catch {
    showLogin();
  }
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!dashboardView.hidden) {
      loadStats().catch(() => {});
      loadTasks().catch(() => {});
    }
  }, 5000);
}

init();
