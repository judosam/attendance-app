"use strict";

/* Attendance Workbench - client logic
 * Hash-routed views (#/dashboard, #/reports, #/reports/<id>, #/generate),
 * restart-safe run history, and per-report analytics. Dynamic text is always
 * inserted with textContent; charts are hand-built inline SVG with labels. */

(function () {
  const $ = (id) => document.getElementById(id);

  /* ---------------- tiny DOM helpers ---------------- */
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (key === "text") node.textContent = value;
        else if (key === "class") node.className = value;
        else if (key === "hidden") node.hidden = Boolean(value);
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (value !== null && value !== undefined) node.setAttribute(key, value);
      }
    }
    for (const child of children) {
      if (child === null || child === undefined) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value !== null && value !== undefined) node.setAttribute(key, value);
      }
    }
    return node;
  }

  /* Material Symbols icon set (matches the uploaded dashboard design).
   * Maps our internal names to Material Symbols tokens. */
  const ICONS = {
    download: "download",
    view: "visibility",
    trash: "delete",
    arrow: "arrow_forward",
    file: "description",
    schedule: "schedule",
    fingerprint: "fingerprint",
    refresh: "refresh",
    add: "add",
    close: "close",
    upload: "upload_file",
  };
  function icon(name, size) {
    const span = document.createElement("span");
    span.className = "material-symbols-outlined";
    span.setAttribute("aria-hidden", "true");
    if (size) span.style.fontSize = size + "px";
    span.textContent = ICONS[name] || name;
    return span;
  }

  const els = {
    liveStatus: $("liveStatus"),
    navPills: document.querySelectorAll(".nav-pill"),
    confirmDialog: $("confirmDialog"),
    confirmTitle: $("confirmTitle"),
    confirmCopy: $("confirmCopy"),
    confirmOk: $("confirmOk"),
    confirmCancel: $("confirmCancel"),
  };

  function announce(message) {
    els.liveStatus.textContent = message;
  }

  /* ---------------- toast notifications ----------------
   * Popup notes for errors and issues: failures surface here even when the
   * user is on another view. Auto-dismiss after 7s; closable; click to open
   * the related view when a link is supplied. */
  const toastStack = $("toastStack");

  function showToast(kind, title, message, options) {
    options = options || {};
    // Never render an empty popup: fall back to a usable title/message.
    title = String(title || "").trim();
    message = String(message || "").trim();
    if (!title && !message) {
      title = kind === "error" ? "Something went wrong"
            : kind === "warn" ? "Attention needed"
            : "Done";
    } else if (!title) {
      title = message.length > 60 ? message.slice(0, 60) + "…" : message;
      message = "";
    }
    const toast = el("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" });
    const iconToken = kind === "error" ? "error" : kind === "warn" ? "warning" : "check_circle";
    const ic = document.createElement("span");
    ic.className = "material-symbols-outlined";
    ic.setAttribute("aria-hidden", "true");
    ic.textContent = iconToken;
    toast.appendChild(ic);
    const body = el("div", { class: "toast-body" },
      el("p", { class: "toast-title" }, title));
    if (message) body.appendChild(el("p", { class: "toast-msg" }, message));
    if (options.actionLabel && options.actionHref) {
      const link = el("a", { class: "toast-link", href: options.actionHref }, options.actionLabel);
      body.appendChild(link);
    }
    toast.appendChild(body);
    const close = el("button", {
      type: "button", class: "toast-close", "aria-label": "Dismiss notification",
    }, "\u00d7");
    close.addEventListener("click", () => dismiss());
    toast.appendChild(close);

    let timer = null;
    function dismiss() {
      if (timer) window.clearTimeout(timer);
      toast.classList.add("toast-out");
      window.setTimeout(() => toast.remove(), 220);
    }
    toast.addEventListener("click", (event) => {
      if (event.target === close) return;
      dismiss();
    });
    toastStack.appendChild(toast);
    timer = window.setTimeout(dismiss, options.sticky ? 20000 : 7000);
    return toast;
  }

  function showErrorToast(title, message, options) {
    return showToast("error", title, message, options);
  }

  function showWarnToast(title, message, options) {
    return showToast("warn", title, message, options);
  }

  function showOkToast(title, message, options) {
    return showToast("ok", title, message, options);
  }

  /* ---------------- formatting ---------------- */
  const nf0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const nf1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

  function fmtInt(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "–";
    const number = Number(value);
    return nf0.format(Math.round(number));
  }

  function fmtSize(bytes) {
    if (bytes >= 1024 * 1024) return nf1.format(bytes / (1024 * 1024)) + " MB";
    if (bytes >= 1024) return nf1.format(bytes / 1024) + " KB";
    return nf0.format(bytes || 0) + " B";
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (!total) return "—";
    if (total < 60) return total + "s";
    const minutes = Math.floor(total / 60);
    if (minutes < 60) {
      const rest = total % 60;
      return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
    }
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }

  function fmtElapsed(seconds) {
    return fmtDuration(seconds);
  }

  function timeAgo(epochSeconds) {
    if (!epochSeconds) return "";
    const then = new Date(epochSeconds * 1000);
    const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    if (seconds < 86400 * 7) return Math.floor(seconds / 86400) + "d ago";
    return shortDate(then);
  }

  const dateTimeFmt = new Intl.DateTimeFormat("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
  const dateFmt = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });
  const yearFmt = new Intl.DateTimeFormat("en-US", { year: "numeric" });

  function dateTimeLabel(val) {
    if (!val) return "—";
    const date = val instanceof Date
      ? val
      : new Date(Number(val) * (Number(val) > 1e11 ? 1 : 1000));
    return dateTimeFmt.format(date) +
      (date.getFullYear() !== new Date().getFullYear() ? ", " + yearFmt.format(date) : "");
  }

  function shortDate(date) {
    return dateFmt.format(date);
  }

  function statusText(status) {
    return { done: "Done", error: "Failed", running: "Running", queued: "Queued" }[status] || status;
  }

  async function fetchJSON(url, options) {
    const response = await fetch(url, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (jsonError) {
      // fall through - the caller handles missing payload
    }
    if (!response.ok) {
      if (response.status === 401 &&
        !url.startsWith("/api/login") && !url.startsWith("/api/session") &&
        !url.startsWith("/api/logout")) {
        showLogin(); // session expired - lock the app again
      }
      throw new Error((payload && payload.error) || payload && payload.description ||
        `Request failed (${response.status})`);
    }
    return payload;
  }

  function apiErrorText(error) {
    return error && error.message ? error.message : String(error);
  }

  /* ---------------- sign-in window ---------------- */
  const authEls = {
    overlay: $("loginOverlay"),
    form: $("loginForm"),
    user: $("loginUser"),
    pass: $("loginPass"),
    toggle: $("loginToggle"),
    error: $("loginError"),
    btn: $("loginBtn"),
    hint: $("loginHint"),
    badge: $("loginBadge"),
    initial: $("loginInitial"),
    userName: $("loginUserName"),
    logout: $("logoutBtn"),
  };
  let loginBusy = false;

  function showLogin(message) {
    authEls.overlay.hidden = false;
    document.body.style.overflow = "hidden";
    if (message) {
      authEls.error.textContent = message;
      authEls.error.hidden = false;
    } else {
      authEls.error.textContent = "";
      authEls.error.hidden = true;
    }
    authEls.user.focus();
  }

  function hideLogin() {
    authEls.overlay.hidden = true;
    document.body.style.overflow = "";
  }

  function renderUserBadge(user) {
    if (!user) {
      authEls.badge.hidden = true;
      return;
    }
    authEls.initial.textContent = String(user).slice(0, 1).toUpperCase();
    authEls.userName.textContent = user;
    authEls.badge.hidden = false;
  }

  async function checkSession() {
    try {
      const response = await fetch("/api/session");
      const payload = await response.json().catch(() => null);
      if (response.ok && payload && payload.authenticated) {
        renderUserBadge(payload.user || "");
        hideLogin();
        return true;
      }
      authEls.hint.textContent = (payload && payload.hint) || "";
      showLogin();
      return false;
    } catch (error) {
      authEls.hint.textContent = "The app server is unreachable — start it, then refresh.";
      showLogin();
      return false;
    }
  }

  async function submitLogin(event) {
    event.preventDefault();
    if (loginBusy) return;
    const username = authEls.user.value.trim();
    const password = authEls.pass.value;
    if (!username || !password) {
      authEls.error.textContent = "Enter both your username and password.";
      authEls.error.hidden = false;
      authEls.user.focus();
      return;
    }
    loginBusy = true;
    authEls.btn.disabled = true;
    authEls.btn.textContent = "Signing in…";
    authEls.error.hidden = true;
    try {
      const payload = await fetchJSON("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      renderUserBadge(payload.user || username);
      authEls.pass.value = "";
      hideLogin();
      routeNow();
      announce(`Signed in as ${payload.user || username}.`);
    } catch (error) {
      showLogin(apiErrorText(error));
      authEls.pass.focus();
      authEls.pass.select();
    } finally {
      loginBusy = false;
      authEls.btn.disabled = false;
      authEls.btn.textContent = "Sign In";
    }
  }

  async function signOut() {
    try {
      await fetchJSON("/api/logout", { method: "POST" });
    } catch (error) {
      // ignore - the server session is cleared best-effort
    }
    renderUserBadge(null);
    showLogin("You have been signed out.");
  }

  authEls.form.addEventListener("submit", submitLogin);
  authEls.toggle.addEventListener("click", () => {
    const reveal = authEls.pass.type === "password";
    authEls.pass.type = reveal ? "text" : "password";
    authEls.toggle.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
    authEls.toggle.title = reveal ? "Hide password" : "Show password";
    authEls.toggle.querySelector("span").textContent = reveal ? "visibility_off" : "visibility";
  });
  authEls.logout.addEventListener("click", signOut);

  /* ---------------- router ---------------- */
  const VIEWS = ["dashboard", "generate", "reports", "report", "schedules"];
  const VIEW_TITLES = {
    dashboard: "Dashboard · Attendance Workbench",
    generate: "Generate Report · Attendance Workbench",
    reports: "Reports History · Attendance Workbench",
    report: "Report Summary · Attendance Workbench",
    schedules: "Schedules · Attendance Workbench",
  };

  function parseRoute() {
    const hash = location.hash || "#/dashboard";
    const match = hash.match(/^#\/reports\/([0-9a-fA-F]{1,32})/);
    if (match) return { name: "report", id: match[1] };
    const name = hash.replace(/^#\//, "").split("?")[0].split("/")[0];
    return { name: VIEWS.includes(name) ? name : "dashboard" };
  }

  function showView(route) {
    for (const name of VIEWS) {
      $(`view-${name}`).hidden = name !== route.name;
    }
    document.title = VIEW_TITLES[route.name];
    for (const pill of els.navPills) {
      const active = pill.dataset.nav === route.name ||
        (route.name === "report" && pill.dataset.nav === "reports");
      pill.classList.toggle("active", active);
      if (active) pill.setAttribute("aria-current", "page");
      else pill.removeAttribute("aria-current");
    }
    window.scrollTo({ top: 0, behavior: "auto" });

    if (route.name === "dashboard") renderDashboard();
    else if (route.name === "reports") renderReports();
    else if (route.name === "report") renderReportDetail(route.id);
    else if (route.name === "schedules") renderSchedules();
  }

  function routeNow() {
    try {
      showView(parseRoute());
    } catch (error) {
      announce("Could not load this view: " + apiErrorText(error));
    }
  }

  window.addEventListener("hashchange", routeNow);

  /* ---------------- status & type badges ----------------
   * Single accent per element: table statuses are clean colored badges
   * (no inner dot), so the row never doubles up on colour emphasis. */
  function statusPill(status) {
    return el("span", { class: "status-pill", dataset: { status } }, statusText(status));
  }

  function typeChip(type) {
    return el("span", { class: "type-chip", dataset: { type: type || "—" } }, type || "—");
  }

  /* How a run was produced: Manual upload / Portal fetch / Scheduled. */
  function appendTriggerChip(cell, run) {
    const trigger = run && run.trigger;
    if (!trigger || trigger === "Manual upload") return;
    cell.appendChild(el("span", {
      class: "trigger-chip" + (trigger === "Scheduled" ? " trigger-sched" : ""),
      title: run.range_label
        ? `${trigger} · data range ${run.range_label}`
        : trigger,
    }, trigger));
  }

  /* Row action cluster: a visible primary "View Report" plus icon actions.
   * Rows themselves are clickable (see attachRowNavigation). */
  function rowActions(run, withDelete) {
    const actions = el("div", { class: "row-actions" });
    const view = el("a", {
      class: "btn-mini primary",
      href: `#/reports/${encodeURIComponent(run.id)}`,
      title: "Open the report summary page",
    }, "View Report");
    const download = el("a", {
      class: "icon-btn",
      href: `/api/jobs/${encodeURIComponent(run.id)}/download`,
      title: "Download XLSX",
      "aria-label": `Download report ${run.id}`,
    }, icon("download", 18));
    actions.append(view, download);
    if (withDelete) {
      const del = el("button", {
        type: "button",
        class: "icon-btn danger",
        title: "Delete run",
        "aria-label": `Delete run ${run.id}`,
      }, icon("trash", 18));
      del.addEventListener("click", (event) => {
        event.stopPropagation();
        confirmDeleteRun(run);
      });
      actions.appendChild(del);
    }
    return actions;
  }

  function attachRowNavigation(tbody) {
    if (tbody.dataset.navBound) return;
    tbody.dataset.navBound = "1";
    tbody.addEventListener("click", (event) => {
      if (event.target.closest("a") || event.target.closest("button")) return;
      const row = event.target.closest("tr");
      if (row && row.dataset.id) {
        location.hash = `#/reports/${encodeURIComponent(row.dataset.id)}`;
      }
    });
  }

  function runRow(run, options) {
    const compact = options && options.compact;
    const tr = el("tr", { dataset: { id: run.id } });
    const idTd = el("td");
    const idLink = el("a", {
      class: "job-id",
      href: `#/reports/${encodeURIComponent(run.id)}`,
      title: "Open the report summary page",
    }, run.id);
    idTd.appendChild(idLink);

    const fileTd = el("td");
    const fileName = run.display_names && run.display_names.length
      ? run.display_names.join(", ") : "—";
    const fileCell = el("div", { class: "file-cell" });
    fileCell.appendChild(icon("file", 16));
    fileCell.appendChild(el("span", { class: "file-name-cell mono", title: fileName }, fileName));
    fileTd.appendChild(fileCell);

    const typeTd = el("td");
    typeTd.appendChild(typeChip(run.input_type));
    appendTriggerChip(typeTd, run);

    const statusTd = el("td");
    statusTd.appendChild(statusPill(run.status));
    if (run.status === "error" && run.error) {
      statusTd.appendChild(el("span", {
        class: "muted",
        style: "display:block;font-size:11px;margin-top:4px;max-width:220px;",
        title: run.error,
      }, String(run.error).split("\n")[0].slice(0, 90)));
    }

    const durTd = el("td", { class: "num" },
      el("span", { class: "mono" }, fmtDuration(run.duration)));

    const actionTd = el("td", { class: "num" });
    actionTd.appendChild(rowActions(run, !compact));

    tr.append(idTd, fileTd, typeTd, statusTd, durTd, actionTd);
    return tr;
  }

  function runSummaryRow(run) {
    const tr = el("tr", { dataset: { id: run.id } });
    const when = el("td", {}, el("span", { class: "file-name-cell" }, dateTimeLabel(run.created)),
      run.created ? el("span", { class: "muted", style: "display:block;font-size:11px;" }, timeAgo(run.created)) : null);

    const fileTd = el("td");
    const fileName = run.display_names && run.display_names.length
      ? run.display_names.join(", ") : "—";
    const fileCell = el("div", { class: "file-cell" });
    fileCell.appendChild(icon("file", 16));
    fileCell.appendChild(el("span", { class: "file-name-cell mono", title: fileName }, fileName));
    fileTd.appendChild(fileCell);

    const typeTd = el("td");
    typeTd.appendChild(typeChip(run.input_type));
    appendTriggerChip(typeTd, run);

    const statusTd = el("td");
    statusTd.appendChild(statusPill(run.status));
    if (run.status === "error" && run.error) {
      statusTd.appendChild(el("span", {
        class: "muted",
        style: "display:block;font-size:11px;margin-top:4px;max-width:220px;",
        title: run.error,
      }, String(run.error).split("\n")[0].slice(0, 90)));
    }

    const durTd = el("td", { class: "num" }, el("span", { class: "mono" }, fmtDuration(run.duration)));
    const actionTd = el("td", { class: "num" });
    actionTd.appendChild(rowActions(run, true));
    tr.append(when, fileTd, typeTd, statusTd, durTd, actionTd);
    return tr;
  }

  /* ---------------- confirmation modal ---------------- */
  let confirmResolver = null;
  function confirmAction(title, copy, okLabel) {
    els.confirmTitle.textContent = title;
    els.confirmCopy.textContent = copy;
    els.confirmOk.textContent = okLabel || "Delete";
    els.confirmDialog.hidden = false;
    els.confirmCancel.focus();
    return new Promise((resolve) => {
      confirmResolver = resolve;
    });
  }

  function settleConfirm(result) {
    els.confirmDialog.hidden = true;
    if (confirmResolver) {
      confirmResolver(result);
      confirmResolver = null;
    }
  }
  els.confirmOk.addEventListener("click", () => settleConfirm(true));
  els.confirmCancel.addEventListener("click", () => settleConfirm(false));
  els.confirmDialog.addEventListener("click", (event) => {
    if (event.target === els.confirmDialog) settleConfirm(false);
  });
  document.addEventListener("keydown", (event) => {
    if (!els.confirmDialog.hidden && event.key === "Escape") {
      settleConfirm(false);
    } else if (!consoleEls.overlay.hidden && event.key === "Escape") {
      closeAuditConsole();
    }
  });

  function wireConsoleEvents() {
    const rail = document.querySelectorAll(".rail-item");
    for (const item of rail) {
      item.addEventListener("click", () => setConsoleTab(item.dataset.consoleTab));
    }
    consoleEls.close.addEventListener("click", closeAuditConsole);
    consoleEls.dismiss.addEventListener("click", closeAuditConsole);
    consoleEls.exportBtn.addEventListener("click", () => {
      if (consoleState.analytics) {
        exportFlaggedCSV(consoleState.analytics.flagged || [], consoleState.id);
      }
    });
    consoleEls.overlay.addEventListener("click", (event) => {
      if (event.target === consoleEls.overlay) closeAuditConsole();
    });
  }

  async function confirmDeleteRun(run) {
    const confirmed = await confirmAction(
      `Delete run ${run.id}?`,
      "This removes the run from history and deletes its report folder from this computer. This cannot be undone.",
      "Delete run"
    );
    if (!confirmed) return;
    try {
      await fetchJSON(`/api/jobs/${encodeURIComponent(run.id)}`, { method: "DELETE" });
      announce(`Deleted run ${run.id}.`);
    } catch (error) {
      announce(apiErrorText(error));
      setAlert($("repError"), apiErrorText(error));
      return;
    }
    refreshCurrent();
  }

  async function clearHistory() {
    const confirmed = await confirmAction(
      "Clear all run history?",
      "Every run and its report folder will be deleted from this computer. The newest 20 runs are usually kept automatically — this removes them all. This cannot be undone.",
      "Clear history"
    );
    if (!confirmed) return;
    try {
      await fetchJSON("/api/history", { method: "DELETE" });
      announce("Run history cleared.");
    } catch (error) {
      setAlert($("repError"), apiErrorText(error));
      return;
    }
    refreshCurrent();
  }

  function setAlert(node, message) {
    if (!node) return;
    node.textContent = message;
    node.hidden = false;
    if (message) node.focus({ preventScroll: false });
  }

  function clearAlert(node) {
    if (node) {
      node.textContent = "";
      node.hidden = true;
    }
  }

  function refreshCurrent() {
    const route = parseRoute();
    if (route.name === "dashboard") renderDashboard();
    else if (route.name === "reports") renderReports();
    else if (route.name === "report") renderReportDetail(route.id);
    else if (route.name === "schedules") renderSchedules();
  }

  // History-backed views should pick up a run that just finished anywhere.
  function maybeRefreshHistory() {
    const name = parseRoute().name;
    if (name === "reports" || name === "dashboard") refreshCurrent();
  }

  /* =================================================================
   * DASHBOARD
   * ================================================================= */
  const MIX_COLORS = {
    "Full Day": "#16a34a",
    "Half Day": "#f59e0b",
    Absent: "#dc2626",
    WeekOff: "#94a3b8",
    "Week Off": "#94a3b8",
  };
  const MIX_SEG_CLASS = {
    "Full Day": "seg-full",
    "Half Day": "seg-half",
    Absent: "seg-absent",
    WeekOff: "seg-off",
    "Week Off": "seg-off",
  };

  function donutSVG(segments, radius) {
    const r = radius || 60;
    const c = 2 * Math.PI * r;
    const svg = svgEl("svg", { viewBox: "0 0 160 160", width: 176, height: 176 });
    svg.appendChild(svgEl("circle", {
      cx: 80, cy: 80, r: r, fill: "none", stroke: "#e2e8f0", "stroke-width": 15,
    }));
    let acc = 0;
    for (const segment of segments) {
      const length = c * (segment.pct / 100);
      if (length <= 0) continue;
      const circle = svgEl("circle", {
        cx: 80, cy: 80, r: r, fill: "none",
        stroke: segment.color, "stroke-width": 15,
        "stroke-dasharray": `${length.toFixed(2)} ${(c - length).toFixed(2)}`,
        "stroke-dashoffset": String(-acc.toFixed(2)),
      });
      const title = svgEl("title");
      title.textContent = `${segment.label}: ${segment.pct}% (${fmtInt(segment.count)} records)`;
      circle.appendChild(title);
      svg.appendChild(circle);
      acc += length;
    }
    return svg;
  }

  function renderMixBlock(container, mix, total, centerValue, centerCaption, dept) {
    container.textContent = "";
    const wrap = el("div", { class: "mix-wrap" });
    const donutWrap = el("div", { class: "donut" });
    const ordered = mix.slice().sort((a, b) => {
      const rank = (label) =>
        label === "Full Day" ? 0 : label === "Half Day" ? 1 : label === "Absent" ? 2 : 3;
      return rank(a.label) - rank(b.label);
    });
    const summary = ordered.length
      ? ordered.map((s) => `${s.label} ${s.pct}%`).join(", ")
      : "No attendance records";
    const svg = donutSVG(ordered.map((s) => ({
      label: s.label,
      pct: s.pct,
      count: s.count,
      color: MIX_COLORS[s.label] || "#94a3b8",
    })));
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Attendance mix: ${summary}.`);
    donutWrap.appendChild(svg);
    const center = el("div", { class: "donut-center" });
    center.appendChild(el("span", { class: "donut-value num" }, centerValue));
    center.appendChild(el("span", { class: "donut-caption" }, centerCaption));
    donutWrap.appendChild(center);
    wrap.appendChild(donutWrap);

    const side = el("div", { class: "mix-side" });
    const track = el("div", { class: "bar-track", "aria-hidden": "true" });
    for (const s of ordered) {
      const seg = el("div", {
        class: "seg " + (MIX_SEG_CLASS[s.label] || "seg-off"),
        style: `width:${s.pct}%;`,
        title: `${s.label}: ${s.pct}%`,
      });
      track.appendChild(seg);
    }
    side.appendChild(track);
    const legend = el("div", { class: "mix-legend" });
    for (const s of ordered) {
      legend.appendChild(el("div", { class: "legend-item" },
        el("div", { class: "legend-top" },
          el("span", { class: "legend-dot", style: `background:${MIX_COLORS[s.label] || "#94a3b8"};` }),
          el("span", { class: "legend-name" }, s.label)),
        el("div", { class: "legend-bottom" },
          el("span", { class: "legend-count num" }, fmtInt(s.count)),
          el("span", { class: "legend-pct" }, s.pct + "%"))));
    }
    side.appendChild(legend);
    wrap.appendChild(side);
    container.appendChild(wrap);
    if (dept && dept.length) {
      // Compact per-department strip — uses the vertical space next to the
      // Compliance Flags panel instead of leaving it empty.
      const deptWrap = el("div", { class: "dept-strip" });
      deptWrap.appendChild(el("p", { class: "dept-strip-title" }, "By department"));
      const list = el("div", { class: "dept-strip-list" });
      const maxRows = Math.max(1, ...dept.map((d) => d.rows || 1));
      for (const d of dept.slice(0, 4)) {
        const pct = total > 0 ? Math.round(((d.rows || 0) / total) * 100) : 0;
        list.appendChild(el("div", { class: "dept-strip-item" },
          el("div", { class: "dept-strip-top" },
            el("span", { class: "dept-strip-name", title: d.name }, d.name),
            el("span", { class: "dept-strip-count num" }, `${fmtInt(d.rows)} · ${pct}%`)),
          el("div", { class: "dept-strip-bar" },
            el("div", { class: "dept-strip-fill" , style: `width:${Math.max(2, Math.round(((d.rows || 0) / maxRows) * 100))}%;` }))));
      }
      deptWrap.appendChild(list);
      container.appendChild(deptWrap);
    }
    if (ordered.length === 0) {
      container.textContent = "";
      container.appendChild(el("p", { class: "muted" }, "No classified attendance rows in the latest report."));
    }
  }

  function renderTileGrid(container, tiles) {
    container.textContent = "";
    const grid = el("div", { class: "tile-grid" });
    for (const tile of tiles) {
      grid.appendChild(el("div", { class: "tile" },
        el("p", { class: "tile-label" }, tile.label),
        el("p", { class: "tile-num " + tile.tone }, tile.value,
          tile.unit ? el("span", { class: "unit" }, " " + tile.unit) : null),
        tile.note ? el("p", { class: "tile-note" }, tile.note) : null));
    }
    container.appendChild(grid);
  }

  function breachTiles(breaches) {
    return [
      { label: "Late Logins", value: fmtInt(breaches.late), tone: "err", note: "logged late entries" },
      { label: "Early Logouts", value: fmtInt(breaches.early), tone: "warn", note: "left before shift end" },
      { label: "Worked < 4 hrs", value: fmtInt(breaches.under4), tone: "err", note: "very short days" },
      { label: "Break Hours Exceeded", value: fmtInt(breaches.break_hours), tone: "ink", note: `under-8h days: ${fmtInt(breaches.under8)} · >4 breaks: ${fmtInt(breaches.break_counts)}` },
    ];
  }

  function initials(name) {
    if (!name) return "–";
    return name.split(/\s+/).filter(Boolean).slice(0, 2)
      .map((part) => part[0].toUpperCase()).join("");
  }

  function personMeta(person) {
    if (!person) return "";
    return person.team && person.team !== "—"
      ? `${person.id || ""} · ${person.team}`
      : (person.id || "");
  }

  function severityClass(flags) {
    if (flags >= 40) return "hi";
    if (flags >= 20) return "mid";
    return "lo";
  }

  function avatarClass(flags) {
    if (flags >= 40) return "avatar rose";
    return "avatar amber";
  }

  function flagRows(container, flagged, limit) {
    container.textContent = "";
    const list = el("div", { class: "flag-list" });
    for (const person of (flagged || []).slice(0, limit || 5)) {
      const row = el("div", { class: "flag-row" });
      row.appendChild(el("span", { class: avatarClass(person.flags) }, initials(person.name)));
      const who = el("div", { class: "flag-who" });
      who.appendChild(el("p", { class: "flag-name", title: person.name || "—" }, person.name || "—"));
      who.appendChild(el("p", { class: "flag-meta", title: personMeta(person) }, personMeta(person)));
      row.appendChild(who);
      row.appendChild(el("span", { class: "flag-count " + severityClass(person.flags) },
        fmtInt(person.flags) + " flags"));
      list.appendChild(row);
    }
    if (!(flagged || []).length) {
      list.appendChild(el("p", { class: "muted", style: "padding:6px 2px;" },
        "No flagged employees in this report."));
    }
    container.appendChild(list);
  }

  async function renderDashboard() {
    const errorNode = $("dashError");
    const emptyNode = $("dashEmpty");
    const contentNode = $("dashContent");
    clearAlert(errorNode);
    try {
      const payload = await fetchJSON("/api/history");
      const runs = payload.runs || [];
      const finished = runs.filter((run) => run.status === "done" || run.status === "error");
      const done = runs.filter((run) => run.status === "done");

      if (!runs.length) {
        contentNode.hidden = true;
        emptyNode.hidden = false;
        $("kpiRuns").textContent = "0";
        const runsNote = $("kpiRunsNote");
        if (runsNote) runsNote.textContent = "no runs this month";
        return;
      }
      emptyNode.hidden = true;
      contentNode.hidden = false;

      // KPIs — the headline count is this calendar month's runs.
      const nowDate = new Date();
      const thisMonthRuns = runs.filter((run) => {
        const created = new Date((run.created || 0) * 1000);
        return created.getFullYear() === nowDate.getFullYear()
          && created.getMonth() === nowDate.getMonth();
      }).length;
      $("kpiRuns").textContent = fmtInt(thisMonthRuns);
      const runsNote = $("kpiRunsNote");
      if (runsNote) runsNote.textContent =
        `of ${fmtInt(runs.length)} total · ${nowDate.toLocaleString("en-US", { month: "long" })}`;
      const successRate = finished.length
        ? Math.round((done.length / finished.length) * 100) : 0;
      $("kpiSuccess").textContent = successRate + "%";
      $("kpiSuccess").className = "kpi-value" +
        (successRate >= 90 ? " ok" : successRate >= 60 ? " warn" : " err");
      const durations = done.map((run) => run.duration).filter((d) => d);
      $("kpiDur").textContent = durations.length
        ? fmtDuration(durations.reduce((a, b) => a + b, 0) / durations.length) : "—";
      const latest = done[0] || null;
      $("kpiLatest").textContent = latest ? shortDate(new Date(latest.created * 1000)) : "—";
      $("kpiLatestNote").textContent = latest
        ? (latest.display_names && latest.display_names[0]) || "latest report"
        : "no completed runs yet";

      // Latest-run band (needs analytics of the newest completed run)
      const latestBody = $("latestBody");
      const flagsBody = $("flagsBody");
      const flagsBadge = $("flagsBadge");
      const latestLink = $("latestLink");
      const flagsLink = $("flagsLink");
      const latestBadge = $("latestBadge");
      if (!latest) {
        latestBody.textContent = "";
        latestBody.appendChild(el("p", { class: "muted" }, "No completed runs yet — generate a report to see its attendance mix here."));
        flagsBody.textContent = "";
        flagsBody.appendChild(el("p", { class: "muted" }, "No completed runs to audit yet."));
        flagsBadge.textContent = "–";
        latestBadge.textContent = "–";
        latestLink.hidden = true;
        flagsLink.hidden = true;
      } else {
        latestBadge.textContent = latest.input_type || "Report";
        latestLink.href = `#/reports/${encodeURIComponent(latest.id)}`;
        flagsLink.href = `#/reports/${encodeURIComponent(latest.id)}`;
        latestLink.hidden = false;
        flagsLink.hidden = false;
        $("latestEyebrow").textContent = "Latest Report · " + timeAgo(latest.created);
        $("latestTitle").textContent = (latest.display_names && latest.display_names[0]) || "Latest Report";
        try {
          const analytics = await fetchJSON(`/api/jobs/${encodeURIComponent(latest.id)}/analytics`);
          const mix = analytics.mix || [];
          const total = (analytics.meta && analytics.meta.rows) || mix.reduce((a, s) => a + s.count, 0);
          renderMixBlock(latestBody, mix, total, fmtInt(total), "records", analytics.dept || []);
          const breaches = analytics.breaches || {};
          flagsBadge.textContent = fmtInt(breaches.total_flags) + " flags";
          renderTileGrid(flagsBody, breachTiles(breaches));
          const hr = analytics.hr || {};
          const hrNote = el("p", {
            class: "muted",
            style: "font-size:12px;margin-top:10px;",
          }, `HR agreement ${fmtInt(hr.agreement_pct)}% · mismatches ${fmtInt(hr.mismatches)}`);
          flagsBody.appendChild(hrNote);
          const flaggedPanel = el("div");
          flagRows(flaggedPanel, analytics.flagged || [], 3);
          flagsBody.appendChild(flaggedPanel);
          const meta = analytics.meta || {};
          const rangeLabel = meta.date_min === meta.date_max
            ? meta.date_min : `${meta.date_min} → ${meta.date_max}`;
          if (rangeLabel && !rangeLabel.includes("null")) {
            // Small range line under the report title in the panel head.
            const head = $("latestTitle").parentElement;
            let rangeEl = head.querySelector(".panel-range");
            if (!rangeEl) {
              rangeEl = el("p", { class: "panel-range" });
              head.appendChild(rangeEl);
            }
            rangeEl.textContent = rangeLabel;
          }
        } catch (error) {
          latestBody.textContent = "";
          latestBody.appendChild(el("p", { class: "muted" },
            "Summary for this run is unavailable: " + apiErrorText(error)));
          flagsBody.textContent = "";
          flagsBadge.textContent = "–";
        }
      }

      // Recent runs table (newest 6)
      const recentRows = $("recentRows");
      recentRows.textContent = "";
      for (const run of runs.slice(0, 6)) recentRows.appendChild(runRow(run, { compact: true }));
      attachRowNavigation(recentRows);
      $("recentCount").textContent = runs.length > 6
        ? `Showing latest ${runs.length > 6 ? 6 : runs.length} of ${runs.length} runs` : "Newest runs first";
      announce("Dashboard updated.");
    } catch (error) {
      contentNode.hidden = true;
      emptyNode.hidden = true;
      setAlert(errorNode, apiErrorText(error));
    }
  }

  /* =================================================================
   * REPORTS HISTORY
   * ================================================================= */
  let repFilter = "all";
  let repRuns = [];
  let repPage = 0;
  const REP_PAGE_SIZE = 20;

  function applyRepFilter() {
    const chips = document.querySelectorAll("#view-reports .chip");
    for (const chip of chips) {
      const active = chip.dataset.status === repFilter;
      chip.classList.toggle("chip-active", active);
      if (active) chip.setAttribute("aria-pressed", "true");
      else chip.removeAttribute("aria-pressed");
    }
    const rows = repRuns.filter((run) => repFilter === "all" || run.status === repFilter);
    const pages = Math.max(1, Math.ceil(rows.length / REP_PAGE_SIZE));
    if (repPage >= pages) repPage = pages - 1;
    const start = repPage * REP_PAGE_SIZE;
    const slice = rows.slice(start, start + REP_PAGE_SIZE);
    const body = $("repRows");
    body.textContent = "";
    for (const run of slice) body.appendChild(runSummaryRow(run));
    attachRowNavigation(body);
    $("repCount").textContent = `${fmtInt(rows.length)} of ${fmtInt(repRuns.length)} runs`;
    $("repTableCard").hidden = rows.length === 0;
    $("repEmpty").hidden = repRuns.length !== 0;
    $("repClear").hidden = repRuns.length === 0;
    const pager = $("repPager");
    pager.hidden = rows.length <= REP_PAGE_SIZE;
    $("repPageInfo").textContent = `Page ${fmtInt(repPage + 1)} of ${fmtInt(pages)}`;
    $("repPrev").disabled = repPage <= 0;
    $("repNext").disabled = repPage >= pages - 1;
  }

  async function renderReports() {
    clearAlert($("repError"));
    repPage = 0;
    try {
      const payload = await fetchJSON("/api/history");
      repRuns = payload.runs || [];
      $("repEmpty").hidden = repRuns.length !== 0;
      $("repTableCard").hidden = repRuns.length === 0;
      $("repClear").hidden = repRuns.length === 0;
      applyRepFilter();
    } catch (error) {
      $("repEmpty").hidden = true;
      $("repTableCard").hidden = true;
      $("repClear").hidden = true;
      setAlert($("repError"), apiErrorText(error));
    }
  }

  function bindReportFilters() {
    const chips = document.querySelectorAll("#view-reports .chip");
    for (const chip of chips) {
      chip.addEventListener("click", () => {
        repFilter = chip.dataset.status;
        repPage = 0;
        applyRepFilter();
      });
    }
    $("repPrev").addEventListener("click", () => {
      repPage = Math.max(0, repPage - 1);
      applyRepFilter();
    });
    $("repNext").addEventListener("click", () => {
      repPage += 1;
      applyRepFilter();
    });
    $("repClear").addEventListener("click", clearHistory);
  }

  /* =================================================================
   * REPORT DETAIL (summary page)
   * ================================================================= */
  async function renderReportDetail(id) {
    const body = $("repDetailBody");
    body.textContent = "";
    body.appendChild(el("p", { class: "muted" }, "Loading report summary…"));
    body.focus({ preventScroll: true });
    let run = null;
    try {
      const history = await fetchJSON("/api/history");
      run = (history.runs || []).find((candidate) => candidate.id === id) || null;
    } catch (error) {
      // history fetch is best-effort; analytics below still works for live jobs
    }
    try {
      const analytics = await fetchJSON(`/api/jobs/${encodeURIComponent(id)}/analytics`);
      body.textContent = "";
      body.appendChild(buildSummaryPage(id, run, analytics));
      announce("Report summary loaded.");
    } catch (error) {
      body.textContent = "";
      body.appendChild(buildSummaryFallback(id, run, error));
    }
  }

  function buildSummaryFallback(id, run, error) {
    const card = el("section", { class: "card empty-card" });
    card.appendChild(el("h2", {}, "Summary not available"));
    card.appendChild(el("p", {},
      run
        ? "This run's workbook summary could not be built."
        : "This run is not in history."));
    if (run) {
      card.appendChild(el("p", { class: "muted", style: "font-size:13px;" }, apiErrorText(error)));
      card.appendChild(el("div", { class: "result-actions", style: "justify-content:center;" },
        el("a", {
          class: "btn btn-primary",
          href: `/api/jobs/${encodeURIComponent(id)}/download`,
        }, "Download Report"),
        el("a", { class: "btn", href: "#/generate" }, "Generate Another")));
    } else {
      card.appendChild(el("a", { class: "btn btn-primary", href: "#/reports" }, "Back to Reports History"));
    }
    return card;
  }

  function metaStrip(analytics) {
    const meta = analytics.meta || {};
    const chips = [];
    if (meta.source) {
      chips.push(el("span", { class: "badge-soft badge-blue" }, meta.source));
    }
    if (meta.date_min) {
      const range = meta.date_min === meta.date_max
        ? meta.date_min : `${meta.date_min} → ${meta.date_max}`;
      chips.push(el("span", { class: "badge-soft" }, range));
    }
    chips.push(el("span", { class: "badge-soft" }, `${fmtInt(meta.rows)} rows · ${fmtInt(meta.employees)} employees`));
    return chips;
  }

  function buildSummaryPage(id, run, analytics) {
    const meta = analytics.meta || {};
    const breaches = analytics.breaches || {};
    const mix = analytics.mix || [];
    const title = run && run.display_names && run.display_names.length
      ? run.display_names.join(", ") : (meta.source || "Attendance") + " Report";

    const page = el("div", { class: "summary-page" });

    /* Masthead / run context */
    const mast = el("section", { class: "card", style: "margin-bottom:16px;" });
    const mastInner = el("div", { class: "mix-wrap", style: "align-items:flex-start;justify-content:space-between;" });
    const left = el("div", { style: "min-width:0;flex:1;" });
    const crumb = el("nav", { class: "breadcrumb", "aria-label": "Breadcrumb" },
      el("a", { href: "#/reports" }, "Reports History"),
      el("span", { "aria-hidden": "true" }, "·"),
      el("span", { class: "mono", style: "color:var(--ink-soft);" }, "run-" + id));
    left.appendChild(crumb);
    left.appendChild(el("h1", {
      id: "repDetailHeading",
      style: "margin-top:8px;font-size:24px;",
    }, title));
    const badgeRow = el("div", { class: "summary-band", style: "margin-top:10px;" });
    badgeRow.append(...metaStrip(analytics));
    left.appendChild(badgeRow);

    const range = run && run.created ? `Generated ${dateTimeLabel(run.created)}` : "";
    if (run) {
      const note = el("p", { class: "muted", style: "font-size:13px;margin-top:10px;" },
        `${range}${run.duration ? ` · completed in ${fmtDuration(run.duration)}` : ""}`);
      left.appendChild(note);
    }
    const actions = el("div", { class: "result-actions",
      style: "margin-top:0;flex-wrap:wrap;justify-content:flex-end;max-width:100%;flex:none;" });
    const auditBtn = el("button", {
      type: "button",
      class: "btn btn-primary",
    }, icon("view", 17), "Audit Console");
    auditBtn.addEventListener("click", () => openAuditConsole(id, "overview"));
    actions.appendChild(auditBtn);
    actions.appendChild(el("a", { class: "btn",
      href: `/api/jobs/${encodeURIComponent(id)}/download` },
      icon("download", 17), "Download Excel Report (.xlsx)"));
    actions.appendChild(el("a", { class: "btn btn-secondary", href: "#/generate" }, "Generate Another"));
    mastInner.append(left, actions);
    mast.appendChild(mastInner);
    page.appendChild(mast);

    /* KPI row */
    const worked = analytics.worked || {};
    const hr = analytics.hr || {};
    const kpiRow = el("div", { class: "kpi-row" });
    const kpis = [
      { label: "Total Employees", value: fmtInt(meta.employees), note: "unique in Detailed Report" },
      { label: "Worked Days", value: fmtInt(worked.days), note: "per Overall Summary" },
      { label: "HR Mismatches", value: fmtInt(hr.mismatches), tone: hr.mismatches ? "warn" : "ok",
        note: `${fmtInt(hr.agreement_pct)}% agreement` },
      { label: "Policy Breaches", value: fmtInt(breaches.total_flags), tone: breaches.total_flags ? "err" : "ok",
        note: `across ${fmtInt(breaches.people)} people` },
    ];
    for (const kpi of kpis) {
      const card = el("article", { class: "kpi-card" });
      card.appendChild(el("span", { class: "kpi-label" }, kpi.label));
      card.appendChild(el("span", {
        class: "kpi-value " + (kpi.tone || ""),
      }, kpi.value));
      card.appendChild(el("span", { class: "kpi-note" }, kpi.note));
      kpiRow.appendChild(card);
    }
    page.appendChild(kpiRow);

    /* Attendance distribution + compliance breaches */
    const donutSection = el("div", { class: "donut-section", style: "margin-bottom:16px;" });

    const mixCard = el("section", { class: "card", "aria-labelledby": "mixCardTitle" });
    mixCard.appendChild(el("div", { class: "panel-head" },
      el("div", {},
        el("p", { class: "panel-eyebrow" }, "Attendance Distribution"),
        el("h2", { id: "mixCardTitle", class: "panel-title" }, "Classified shift outcomes")),
      el("span", { class: "badge-soft" }, `${fmtInt(meta.rows)} rows evaluated`)));
    const mixBody = el("div", { class: "panel-body" });
    const biggest = mix.slice().sort((a, b) => b.pct - a.pct)[0];
    renderMixBlock(mixBody, mix, meta.rows || 0, biggest ? biggest.pct + "%" : "0%",
      biggest ? biggest.label : "no data");
    mixCard.appendChild(mixBody);

    const details = el("details", { class: "audit-details" });
    const sum = el("summary", {}, "View raw category breakdown");
    details.appendChild(sum);
    const tableWrap = el("div", { class: "audit-table-wrap table-scroll" });
    const table = el("table", { class: "data-table" });
    const head = el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Status category"),
      el("th", { scope: "col", class: "num" }, "Records"),
      el("th", { scope: "col", class: "num" }, "Ratio")));
    const tbody = el("tbody");
    for (const s of mix) {
      tbody.appendChild(el("tr", {},
        el("td", {}, s.label),
        el("td", { class: "num mono" }, fmtInt(s.count)),
        el("td", { class: "num mono" }, s.pct + "%")));
    }
    table.append(head, tbody);
    tableWrap.appendChild(table);
    details.appendChild(tableWrap);
    mixCard.appendChild(details);
    donutSection.appendChild(mixCard);

    const breachCard = el("section", { class: "card", "aria-labelledby": "breachCardTitle" });
    breachCard.appendChild(el("div", { class: "panel-head" },
      el("div", {},
        el("p", { class: "panel-eyebrow" }, "Compliance Breaches"),
        el("h2", { id: "breachCardTitle", class: "panel-title" }, "Threshold violations")),
      el("span", { class: "badge-soft badge-rose" }, `${fmtInt(breaches.total_flags)} flags`)));
    const breachBody = el("div", { class: "panel-body" });
    renderTileGrid(breachBody, breachTiles(breaches));
    const note = el("div", { class: "rule-note" },
      icon("view", 18),
      el("p", {}, el("strong", {}, "How to read this: "),
        "Every breach above comes from the report's Overall Summary sheet — the same numbers you would see summing it in Excel."));
    breachBody.appendChild(note);
    breachCard.appendChild(breachBody);
    donutSection.appendChild(breachCard);
    page.appendChild(donutSection);

    /* Department mix + flagged employees */

    const deptCard = el("section", { class: "card", "aria-labelledby": "deptCardTitle" });
    deptCard.appendChild(el("div", { class: "panel-head" },
      el("div", {},
        el("p", { class: "panel-eyebrow" }, "Department Attendance Mix"),
        el("h2", { id: "deptCardTitle", class: "panel-title" }, "Present · Off · Absent by team")),
      el("span", { class: "badge-soft" }, `${fmtInt((analytics.dept || []).length)} units`)));
    const deptBody = el("div", { class: "panel-body" });
    const deptList = el("div", { class: "dept-list" });
    for (const team of analytics.dept || []) {
      const rowTotal = Math.max(1, team.rows);
      const pct = (value) => Math.round((value / rowTotal) * 100);
      const deptRow = el("div", { class: "dept-row" });
      deptRow.appendChild(el("div", { class: "dept-head" },
        el("span", { class: "dept-name" }, team.name || "—",
          el("span", { class: "dept-staff" }, `(${fmtInt(team.staff)} staff)`)),
        el("span", { class: "dept-meta" },
          `${pct(team.present)}% present · ${fmtInt(team.mismatch)} mismatches`)));
      const track = el("div", { class: "bar-track thick", "aria-hidden": "true" });
      const segs = [
        { value: team.present, cls: "seg-full", title: `Present: ${fmtInt(team.present)}` },
        { value: team.off, cls: "seg-off", title: `Off: ${fmtInt(team.off)}` },
        { value: team.absent, cls: "seg-absent", title: `Absent: ${fmtInt(team.absent)}` },
      ];
      for (const seg of segs) {
        if (!seg.value) continue;
        track.appendChild(el("div", {
          class: "seg " + seg.cls,
          style: `width:${(seg.value / rowTotal) * 100}%;`,
          title: seg.title,
        }));
      }
      deptRow.appendChild(track);
      deptList.appendChild(deptRow);
    }
    if (!(analytics.dept || []).length) {
      deptList.appendChild(el("p", { class: "muted" }, "No department data in this report."));
    }
    deptBody.appendChild(deptList);
    const legend = el("div", { class: "trend-legend", style: "margin-top:14px;" },
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-full);" }), "Present (Full + Half day)"),
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-off);" }), "Off / Holiday"),
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-absent);" }), "Absent"));
    deptBody.appendChild(legend);
    deptCard.appendChild(deptBody);
    page.appendChild(deptCard);

    const flagCard = el("section", { class: "card", "aria-labelledby": "flagCardTitle" });
    flagCard.appendChild(el("div", { class: "panel-head" },
      el("div", {},
        el("p", { class: "panel-eyebrow" }, "Top Flagged Employees"),
        el("h2", { id: "flagCardTitle", class: "panel-title" }, "Most compliance flags")),
      el("span", { class: "badge-soft badge-rose" }, "Action needed")));
    const flagBody = el("div", { class: "panel-body", style: "padding:0;" });
    const flaggedTable = el("table", { class: "data-table tight" });
    flaggedTable.appendChild(el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Employee"),
      el("th", { scope: "col", class: "num" }, "Late"),
      el("th", { scope: "col", class: "num" }, "Early"),
      el("th", { scope: "col", class: "num" }, "<4h"),
      el("th", { scope: "col", class: "num" }, "Flags"),
      el("th", { scope: "col", class: "num" }, "Audit"))));
    const flagTbody = el("tbody");
    for (const person of (analytics.flagged || []).slice(0, 6)) {
      const tr = el("tr");
      const who = el("td", {},
        el("div", { class: "file-cell" },
          el("span", { class: avatarClass(person.flags) }, initials(person.name)),
          el("div", {},
            el("p", { style: "font-weight:600;", title: person.name || "—" }, person.name || "—"),
            el("p", { class: "muted", style: "font-size:11px;", title: personMeta(person) },
              personMeta(person)))));
      const reviewCell = el("td", { class: "num" });
      const review = el("button", { type: "button", class: "btn-mini" }, "Review");
      review.addEventListener("click", () => openAuditConsole(id, "employees", person.name || ""));
      reviewCell.appendChild(review);
      tr.append(who,
        el("td", { class: "num mono", style: "color:var(--err-strong);" }, fmtInt(person.late)),
        el("td", { class: "num mono", style: "color:var(--warn-strong);" }, fmtInt(person.early)),
        el("td", { class: "num mono" }, fmtInt(person.under4)),
        el("td", { class: "num" }, el("span", { class: "flag-count " + severityClass(person.flags) },
          fmtInt(person.flags) + " flags")),
        reviewCell);
      flagTbody.appendChild(tr);
    }
    if (!(analytics.flagged || []).length) {
      flagTbody.appendChild(el("tr", {}, el("td", { colspan: "6", class: "muted" },
        "No flagged employees.")));
    }
    flaggedTable.appendChild(flagTbody);
    const flagScroll = el("div", { class: "table-scroll" });
    flagScroll.appendChild(flaggedTable);
    flagBody.appendChild(flagScroll);
    const flagged = analytics.flagged || [];
    const shownFlags = Math.min(6, flagged.length);
    const subFooter = el("div", { class: "table-foot", style: "border-top:0;padding:12px 0 0;flex-wrap:wrap;gap:10px 12px;" });
    subFooter.appendChild(el("span", { class: "muted", style: "font-size:12px;align-self:center;" },
      `Showing top ${fmtInt(shownFlags)} of ${fmtInt(breaches.people)} flagged people`));
    const footActions = el("div", { style: "display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-left:auto;" });
    if (flagged.length > shownFlags) {
      const more = el("button", { type: "button", class: "btn-mini" },
        "View all in Audit Console");
      more.addEventListener("click", () => openAuditConsole(id, "employees", ""));
      footActions.appendChild(more);
    }
    const exportBtn = el("button", { type: "button", class: "btn-mini primary" },
      icon("download", 14), "Export flagged list (.csv)");
    exportBtn.addEventListener("click", () => exportFlaggedCSV(flagged, id));
    footActions.appendChild(exportBtn);
    subFooter.appendChild(footActions);
    flagBody.appendChild(subFooter);
    flagCard.appendChild(flagBody);
    page.appendChild(flagCard);

    /* Daily trend */
    const trendCard = el("section", { class: "card trend-card", "aria-labelledby": "trendTitle",
      style: "margin-top:16px;" });
    trendCard.appendChild(el("div", { class: "panel-head" },
      el("div", {},
        el("p", { class: "panel-eyebrow" }, "Daily Trend"),
        el("h2", { id: "trendTitle", class: "panel-title" }, "Attendance by day")),
      el("span", { class: "badge-soft" }, `${fmtInt((analytics.daily || []).length)} days`)));
    const trendBody = el("div", { class: "panel-body" });
    const days = analytics.daily || [];
    const dayMax = Math.max(1, ...days.map((d) => d.present + d.absent + d.off));
    const bars = el("div", { class: "trend-bars", role: "img",
      "aria-label": "Bar chart of daily attendance: present, off and absent counts per day." });
    for (const day of days) {
      const column = el("div", {
        class: "trend-col",
        style: "flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;gap:1px;",
        title: `${day.date} — present ${day.present}, off ${day.off}, absent ${day.absent}`,
      });
      const mk = (count, cls) => {
        if (!count) return null;
        return el("div", {
          class: "trend-bar " + cls,
          style: `height:${(count / dayMax) * 100}%;`,
        });
      };
      const segments = [mk(day.present, ""), mk(day.off, "off"), mk(day.absent, "absent")];
      // present drawn below off/absent: order children bottom-up
      for (const seg of segments.reverse()) if (seg) column.appendChild(seg);
      bars.appendChild(column);
    }
    trendBody.appendChild(bars);
    const firstDate = days.length ? days[0].date : "";
    const lastDate = days.length ? days[days.length - 1].date : "";
    trendBody.appendChild(el("div", { class: "trend-x" },
      el("span", {}, firstDate), el("span", {}, lastDate)));
    trendBody.appendChild(el("div", { class: "trend-legend" },
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-full);" }), "Present"),
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-off);" }), "Off"),
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-absent);" }), "Absent")));

    const trendDetails = el("details", { class: "audit-details", style: "margin-top:14px;" });
    trendDetails.appendChild(el("summary", {}, "View daily data table"));
    const tableWrap2 = el("div", { class: "audit-table-wrap table-scroll" });
    const table2 = el("table", { class: "data-table" });
    table2.appendChild(el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Date"),
      el("th", { scope: "col", class: "num" }, "Present"),
      el("th", { scope: "col", class: "num" }, "Off"),
      el("th", { scope: "col", class: "num" }, "Absent"),
      el("th", { scope: "col", class: "num" }, "Mismatches"))));
    const tbody2 = el("tbody");
    for (const day of days) {
      tbody2.appendChild(el("tr", {},
        el("td", { class: "mono" }, day.date),
        el("td", { class: "num" }, fmtInt(day.present)),
        el("td", { class: "num" }, fmtInt(day.off)),
        el("td", { class: "num" }, fmtInt(day.absent)),
        el("td", { class: "num mono", style: day.mismatch ? "color:var(--warn-strong);" : "" },
          fmtInt(day.mismatch))));
    }
    table2.appendChild(tbody2);
    tableWrap2.appendChild(table2);
    trendDetails.appendChild(tableWrap2);
    trendBody.appendChild(trendDetails);
    trendCard.appendChild(trendBody);
    page.appendChild(trendCard);

    return page;
  }

  /* =================================================================
   * GENERATE FLOW (unchanged behaviour, adapted ids stay the same)
   * ================================================================= */
  const ALLOWED = [".xls", ".xlsx", ".csv"];
  const MAX_BYTES = 512 * 1024 * 1024; // must match the server limit
  const POLL_MS = 900;

  const gen = {
    fileInput: $("fileInput"),
    browseBtn: $("browseBtn"),
    dropzone: $("dropzone"),
    fileList: $("fileList"),
    inputError: $("inputError"),
    runBtn: $("runBtn"),
    clearFilesBtn: $("clearFilesBtn"),
    runPlaceholder: $("runPlaceholder"),
    statusCard: $("statusCard"),
    statusText: $("statusText"),
    cancelBtn: $("cancelJobBtn"),
    log: $("log"),
    resultCard: $("resultCard"),
    resultName: $("resultName"),
    resultFolder: $("resultFolder"),
    downloadLink: $("downloadLink"),
    resultViewLink: $("resultViewLink"),
    resetBtn: $("resetBtn"),
    errorCard: $("errorCard"),
    errorText: $("errorText"),
    retryBtn: $("retryBtn"),
    files: [],
    jobId: null,
    jobQueue: null,
    cursor: 0,
    pollTimer: null,
    busy: false,
    stageLabel: null,
    stagePct: null,
  };

  const progressBar = document.querySelector(".progress-track");
  const progressFill = document.querySelector(".progress-fill");

  function showErrorInline(message) {
    gen.inputError.textContent = message;
    gen.inputError.hidden = false;
    gen.inputError.focus({ preventScroll: false });
  }

  function clearErrorInline() {
    gen.inputError.textContent = "";
    gen.inputError.hidden = true;
  }

  function validExtension(name) {
    const dot = name.lastIndexOf(".");
    if (dot === -1) return false;
    return ALLOWED.includes(name.slice(dot).toLowerCase());
  }

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    clearErrorInline();
    const bad = incoming.find((file) => !validExtension(file.name));
    if (bad) {
      showErrorInline(`“${bad.name}” is not supported. Add .xls, .xlsx, or .csv files only.`);
      return;
    }
    const oversized = incoming.find((file) => file.size > MAX_BYTES);
    if (oversized) {
      showErrorInline(`“${oversized.name}” is larger than the 512 MB upload limit.`);
      return;
    }
    gen.files = gen.files.concat(incoming);
    renderFileList();
  }

  function renderFileList() {
    gen.fileList.textContent = "";
    gen.files.forEach((file) => {
      const item = el("li", { class: "file-item" });
      const name = el("span", { class: "file-name", title: file.name }, file.name);
      const meta = el("span", { class: "file-meta" }, fmtSize(file.size));
      const remove = el("button", {
        type: "button",
        class: "remove-btn",
        "aria-label": `Remove ${file.name}`,
        title: "Remove",
      }, "\u00d7");
      remove.addEventListener("click", () => {
        gen.files = gen.files.filter((other) => other !== file);
        renderFileList();
      });
      item.append(name, meta, remove);
      gen.fileList.appendChild(item);
    });
    const count = gen.files.length;
    gen.runBtn.disabled = count === 0 || gen.busy;
    gen.runBtn.textContent = count
      ? `Generate Audited Report (${count} ${count === 1 ? "File" : "Files"})`
      : "Generate Audited Report";
    gen.clearFilesBtn.hidden = count === 0;
  }

  function clearAllFiles() {
    gen.files = [];
    renderFileList();
    clearErrorInline();
  }

  function setBusy(busy) {
    gen.busy = busy;
    gen.runBtn.disabled = busy || gen.files.length === 0;
    gen.browseBtn.disabled = busy;
  }

  const STAGE_MATCHERS = [
    { re: /Styling ([\w &\-]+): \d+\/(\d+) rows \((\d+)%\)/, kind: "styling" },
    { re: /(?:Biometric|Timechamp|Manual Tracking|File Consolidate) Report Processing/, kind: "processing" },
  ];

  function updateProgressStage(lines) {
    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i];
      if (typeof line !== "string") continue;
      const styling = line.match(STAGE_MATCHERS[0].re);
      if (styling) {
        gen.stageLabel = `Styling ${styling[1]}…`;
        gen.stagePct = Math.max(0, Math.min(100, parseInt(styling[3], 10)));
        return;
      }
      if (STAGE_MATCHERS[1].re.test(line)) {
        gen.stageLabel = "Processing attendance data…";
        gen.stagePct = null;
        return;
      }
    }
    if (gen.stagePct !== null && gen.stagePct < 100) {
      gen.stagePct = null; // new phase without a percent yet
    }
  }

  function renderProgress() {
    const determinate = gen.stagePct !== null && gen.stagePct !== undefined;
    if (determinate) {
      progressFill.style.width = gen.stagePct + "%";
      progressFill.classList.remove("indeterminate");
      progressBar.setAttribute("aria-valuenow", String(gen.stagePct));
      progressBar.setAttribute("aria-valuetext", gen.stagePct + " percent complete");
    } else {
      progressFill.style.width = "";
      progressFill.classList.add("indeterminate");
      progressBar.setAttribute("aria-valuenow", "0");
      progressBar.removeAttribute("aria-valuetext");
    }
  }

  function appendLogLines(lines) {
    if (!lines || !lines.length) return;
    let grew = false;
    for (const line of lines) {
      if (typeof line !== "string" || line === "") continue;
      // Timestamp every physical line (a chunk may embed newlines).
      const stamped = line.split("\n").map((part) => {
        if (part.trim() === "") return "";
        const ts = new Date().toTimeString().slice(0, 8); // HH:MM:SS
        return `[${ts}] ${part}`;
      }).join("\n");
      gen.log.textContent += stamped + "\n";
      grew = true;
    }
    if (grew) gen.log.scrollTop = gen.log.scrollHeight;
    updateProgressStage(lines);
    renderProgress();
  }

  async function startJob() {
    if (gen.busy || !gen.files.length) return;
    clearErrorInline();
    gen.runPlaceholder.hidden = true;
    gen.resultCard.hidden = true;
    gen.errorCard.hidden = true;
    gen.log.textContent = "";
    gen.statusText.textContent = "Starting…";
    gen.statusCard.hidden = false;
    setBusy(true);
    announce("Started generating the report.");

    const form = new FormData();
    gen.files.forEach((file) => form.append("files", file, file.name));
    form.append("drive_upload", mode.uploadDrive && mode.uploadDrive.checked ? "1" : "0");
    if (mode.uploadMail && mode.uploadMail.checked) {
      form.append("email_to", (mode.uploadMailTo && mode.uploadMailTo.value.trim()) || "__DEFAULT__");
    }

    let response;
    try {
      response = await fetch("/api/run", { method: "POST", body: form });
    } catch (networkError) {
      finishWithError(
        `Could not reach the report app: ${networkError.message}\n` +
        "Make sure the server is still running, then choose your files again."
      );
      return;
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (jsonError) {
      // ignore - fall through to the status check below
    }

    if (!response.ok || !payload || !payload.ok) {
      finishWithError((payload && payload.error) ||
        "The server rejected the upload. Choose your files again.");
      return;
    }

    gen.jobId = payload.job_id;
    gen.cursor = 0;
    pollJob();
  }

  function pollJob() {
    window.clearInterval(gen.pollTimer);
    gen.pollTimer = window.setInterval(async () => {
      try {
        const response = await fetch(
          `/api/jobs/${encodeURIComponent(gen.jobId)}?cursor=${gen.cursor}`
        );
        if (!response.ok) {
          window.clearInterval(gen.pollTimer);
          finishWithError("Lost contact with the report app. Start a new report and try again.");
          return;
        }
        const data = await response.json();
        appendLogLines(data.logs || []);
        gen.cursor = data.cursor;

        if (data.status === "done") {
          window.clearInterval(gen.pollTimer);
          if (data.email_error) {
            showWarnToast("Report ready, but the email failed", data.email_error);
          }
          showOkToast(
            "Report ready",
            data.output && data.output.name ? data.output.name : "The workbook was generated.",
            { actionLabel: "View report", actionHref: `#/reports/${encodeURIComponent(gen.jobId)}` }
          );
          finishSuccess(data.output);
        } else if (data.status === "cancelled") {
          window.clearInterval(gen.pollTimer);
          gen.stageLabel = null;
          gen.stagePct = null;
          gen.statusCard.hidden = true;
          gen.runPlaceholder.hidden = false;
          setBusy(false);
          showWarnToast("Run cancelled", "Report generation was cancelled.");
          announce("Report generation was cancelled.");
          maybeRefreshHistory();
        } else if (data.status === "error") {
          window.clearInterval(gen.pollTimer);
          const detail = data.error ||
            "The report engine stopped without producing a file.";
          finishWithError(`${detail}\n\nCheck the log above for more detail, then choose your files again.`);
        } else {
          if (data.elapsed !== undefined) {
            const prefix = gen.stageLabel
              ? `${gen.stageLabel} ${gen.stagePct !== null ? gen.stagePct + "%" : ""}`.trim()
              : "Generating your report…";
            gen.statusText.textContent =
              `${prefix} (${fmtElapsed(data.elapsed)} elapsed)`;
          }
          if (data.status === "running" && gen.stagePct !== null) renderProgress();
        }
      } catch (pollError) {
        // transient network hiccup - the next tick will retry
      }
    }, POLL_MS);
  }

  function finishSuccess(output) {
    gen.stageLabel = null;
    gen.stagePct = null;
    gen.statusCard.hidden = true;
    gen.resultName.textContent = output && output.name ? output.name : "Report.xlsx";
    gen.resultFolder.textContent = output && output.folder ? output.folder : "";
    gen.downloadLink.href = `/api/jobs/${encodeURIComponent(gen.jobId)}/download`;
    gen.resultViewLink.href = `#/reports/${encodeURIComponent(gen.jobId)}`;
    gen.resultViewLink.hidden = false;
    // The run is done - clear the selected files so the form is ready to start fresh.
    if (gen.files.length) {
      gen.files = [];
      renderFileList();
    }
    setBusy(false);
    maybeRefreshHistory();
    if (parseRoute().name === "generate") {
      // Open the finished report immediately.
      gen.resultCard.hidden = true;
      gen.runPlaceholder.hidden = false;
      const jobId = gen.jobId;
      location.hash = `#/reports/${encodeURIComponent(jobId)}`;
      announce("Report is ready — opening it now.");
    } else {
      // User moved away during the run; leave the ready card for when they return.
      gen.resultCard.hidden = false;
      announce("Report is ready. Find it under Reports History.");
    }
  }

  function finishWithError(message) {
    gen.stageLabel = null;
    gen.stagePct = null;
    gen.statusCard.hidden = true;
    gen.runPlaceholder.hidden = true;
    gen.errorText.textContent = message;
    gen.errorCard.hidden = false;
    setBusy(false);
    announce("Report generation failed.");
    showErrorToast(
      "Report generation failed",
      String(message).split("\n")[0].slice(0, 160),
      { actionLabel: "Review", actionHref: "#/generate", sticky: true }
    );
    gen.errorCard.focus({ preventScroll: false });
    maybeRefreshHistory();
  }

  function resetAll() {
    window.clearInterval(gen.pollTimer);
    gen.files = [];
    gen.jobId = null;
    gen.cursor = 0;
    gen.busy = false;
    gen.resultCard.hidden = true;
    gen.resultViewLink.hidden = true;
    gen.errorCard.hidden = true;
    gen.statusCard.hidden = true;
    gen.runPlaceholder.hidden = false;
    gen.log.textContent = "";
    renderFileList();
    gen.browseBtn.disabled = false;
    gen.browseBtn.focus();
  }

  /* =================================================================
   * AUDIT CONSOLE - drill-in overlay (Overview / Department /
   * Compliance & Breaches / Employee Audit) built from one run's analytics
   * ================================================================= */
  const consoleState = { id: null, analytics: null, tab: "overview", band: "all", query: "" };
  const consoleEls = {
    overlay: $("consoleOverlay"),
    pane: $("consolePane"),
    eyebrow: $("consoleEyebrow"),
    title: $("consoleTitle"),
    sub: $("consoleSub"),
    close: $("consoleClose"),
    dismiss: $("consoleDismiss"),
    exportBtn: $("consoleExport"),
  };
  let consoleOpener = null;

  function downloadCSV(filename, rows, columns) {
    const header = columns.map((column) => column.label).join(",");
    const lines = rows.map((row) => columns.map((column) => {
      const value = String(row[column.key] === null || row[column.key] === undefined ? "" : row[column.key]);
      return /[,"\n]/.test(value) ? '"' + value.replace(/"/g, '""') + '"' : value;
    }).join(","));
    const csv = "\uFEFF" + [header].concat(lines).join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
    announce("Flagged list exported.");
  }

  const FLAGGED_COLUMNS = [
    { key: "id", label: "Employee ID" },
    { key: "name", label: "Employee Name" },
    { key: "team", label: "Team/Department" },
    { key: "late", label: "Late Logins" },
    { key: "early", label: "Early Logouts" },
    { key: "under4", label: "Worked Under 4h" },
    { key: "under8", label: "Worked Under 8h" },
    { key: "break_hours", label: "Break Hours Exceeded" },
    { key: "break_counts", label: "Break Pauses > 4" },
    { key: "flags", label: "Total Flags" },
  ];

  function exportFlaggedCSV(flagged, runId) {
    downloadCSV(`flagged_employees_${runId || "run"}.csv`, flagged || [], FLAGGED_COLUMNS);
  }

  function bandOf(flags) {
    return flags >= 50 ? "critical" : flags >= 20 ? "elevated" : "watch";
  }

  function bandChip(flags) {
    const band = bandOf(flags);
    const label = band === "critical" ? "Critical" : band === "elevated" ? "Elevated" : "Watch";
    const cls = band === "critical" ? "hi" : band === "elevated" ? "mid" : "lo";
    return el("span", { class: "flag-count " + cls }, label);
  }

  async function openAuditConsole(runId, initialTab, personQuery) {
    consoleOpener = document.activeElement;
    consoleState.id = runId;
    consoleState.tab = initialTab || "overview";
    consoleState.band = "all";
    consoleState.query = personQuery || "";
    consoleEls.overlay.hidden = false;
    document.body.style.overflow = "hidden";
    consoleEls.eyebrow.textContent = "Audit Console · run-" + (runId || "");
    consoleEls.title.textContent = "Attendance & Policy Ledger";
    consoleEls.sub.textContent = "Loading analytics…";
    consoleEls.pane.textContent = "";
    consoleEls.pane.appendChild(el("p", { class: "muted" }, "Loading analytics…"));
    consoleEls.close.focus();
    try {
      const analytics = await fetchJSON(`/api/jobs/${encodeURIComponent(runId)}/analytics`);
      consoleState.analytics = analytics;
      const meta = analytics.meta || {};
      const range = meta.date_min === meta.date_max
        ? meta.date_min || ""
        : `${meta.date_min || ""} → ${meta.date_max || ""}`;
      consoleEls.sub.textContent =
        `${meta.source || "Report"} · ${fmtInt(meta.rows)} rows · ${fmtInt(meta.employees)} employees`
        + (range ? ` · ${range}` : "");
      renderConsoleTab();
      announce("Audit console opened.");
    } catch (error) {
      consoleEls.pane.textContent = "";
      consoleEls.pane.appendChild(el("p", { class: "muted" },
        "Could not open the audit console for this run: " + apiErrorText(error)));
    }
  }

  function closeAuditConsole() {
    consoleEls.overlay.hidden = true;
    document.body.style.overflow = "";
    consoleState.id = null;
    consoleState.analytics = null;
    if (consoleOpener && document.contains(consoleOpener)) consoleOpener.focus();
    consoleOpener = null;
  }

  function setConsoleTab(tab) {
    consoleState.tab = tab;
    if (consoleState.analytics) renderConsoleTab();
  }

  function paneSection(title, sub) {
    const section = el("div");
    const head = el("div", { style: "margin-bottom:16px;" });
    head.appendChild(el("h3", { style: "font-size:16px;" }, title));
    if (sub) {
      head.appendChild(el("p", { class: "muted", style: "font-size:13px;margin-top:2px;" }, sub));
    }
    section.appendChild(head);
    return section;
  }

  function renderConsoleTab() {
    const items = document.querySelectorAll(".rail-item");
    for (const item of items) {
      const active = item.dataset.consoleTab === consoleState.tab;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    }
    const pane = consoleEls.pane;
    pane.textContent = "";
    pane.scrollTop = 0;
    const analytics = consoleState.analytics;
    if (!analytics) return;
    if (consoleState.tab === "overview") renderConsoleOverview(pane, analytics);
    else if (consoleState.tab === "dept") renderConsoleDept(pane, analytics);
    else if (consoleState.tab === "breaches") renderConsoleBreaches(pane, analytics);
    else renderConsoleEmployees(pane, analytics);
    pane.focus({ preventScroll: true });
  }

  function renderConsoleOverview(pane, analytics) {
    const section = paneSection("Attendance Distribution & Classification Mix",
      "Status composition across the whole report.");
    const mix = analytics.mix || [];
    const total = (analytics.meta && analytics.meta.rows) ||
      mix.reduce((acc, s) => acc + s.count, 0);
    const mixHost = el("div");
    renderMixBlock(mixHost, mix, total, fmtInt(total), "records");
    section.appendChild(mixHost);

    const stats = el("div", { class: "tile-grid", style: "margin-top:16px;" });
    const hr = analytics.hr || {};
    const worked = analytics.worked || {};
    const hours = analytics.hours || {};
    const tiles = [
      { label: "HR agreement", value: fmtInt(hr.agreement_pct) + "%",
        tone: hr.agreement_pct >= 90 ? "ok" : "warn", note: `${fmtInt(hr.mismatches)} mismatches` },
      { label: "Worked days", value: fmtInt(worked.days), tone: "ink", note: "from Overall Summary" },
      { label: "Total break hours", value: fmtInt(hours.break_hours_total), tone: "ink", note: "across all employees" },
      { label: "Worked hours vs HR", value: fmtInt(hours.diff), tone: hours.diff < 0 ? "warn" : "ok",
        note: "negative = worked less than HR expected" },
    ];
    for (const tile of tiles) {
      stats.appendChild(el("div", { class: "tile" },
        el("p", { class: "tile-label" }, tile.label),
        el("p", { class: "tile-num " + tile.tone }, tile.value),
        el("p", { class: "tile-note" }, tile.note)));
    }
    section.appendChild(stats);
    pane.appendChild(section);
  }

  function renderConsoleDept(pane, analytics) {
    const section = paneSection("Department Attendance Distribution & Share",
      "Per-unit composition: present (Full + Half day), off, and absent.");
    const dept = analytics.dept || [];
    const list = el("div", { class: "dept-list" });
    for (const team of dept) {
      const rowTotal = Math.max(1, team.rows);
      const pct = (value) => Math.round((value / rowTotal) * 100);
      const deptRow = el("div", { class: "dept-row" });
      deptRow.appendChild(el("div", { class: "dept-head" },
        el("span", { class: "dept-name" }, team.name || "—",
          el("span", { class: "dept-staff" }, `(${fmtInt(team.staff)} staff)`)),
        el("span", { class: "dept-meta" },
          `${pct(team.present)}% present · ${fmtInt(team.mismatch)} mismatches`)));
      const track = el("div", { class: "bar-track thick", "aria-hidden": "true" });
      const segs = [
        { value: team.present, cls: "seg-full", title: `Present: ${fmtInt(team.present)}` },
        { value: team.off, cls: "seg-off", title: `Off: ${fmtInt(team.off)}` },
        { value: team.absent, cls: "seg-absent", title: `Absent: ${fmtInt(team.absent)}` },
      ];
      for (const seg of segs) {
        if (!seg.value) continue;
        track.appendChild(el("div", {
          class: "seg " + seg.cls,
          style: `width:${(seg.value / rowTotal) * 100}%;`,
          title: seg.title,
        }));
      }
      deptRow.appendChild(track);
      list.appendChild(deptRow);
    }
    if (!dept.length) list.appendChild(el("p", { class: "muted" }, "No department data."));
    section.appendChild(list);
    section.appendChild(el("div", { class: "trend-legend", style: "margin-top:14px;" },
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-full);" }), "Present (Full + Half day)"),
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-off);" }), "Off / Holiday"),
      el("span", {}, el("span", { class: "legend-dot", style: "background:var(--chart-absent);" }), "Absent")));
    pane.appendChild(section);
  }

  function renderConsoleBreaches(pane, analytics) {
    const breaches = analytics.breaches || {};
    const section = paneSection("Violation Matrix & Severity Rules",
      "Policy infractions with incident counts taken from the Overall Summary sheet.");
    const rules = [
      { name: "Late Login", rule: "Logged in after the shift grace window", count: breaches.late, sev: "Moderate" },
      { name: "Early Logout", rule: "Left before the shift end time", count: breaches.early, sev: "Moderate" },
      { name: "Worked < 4 hrs", rule: "Day with fewer than 4 worked hours", count: breaches.under4, sev: "High" },
      { name: "Worked < 8 hrs", rule: "Day flagged below 8 worked hours", count: breaches.under8, sev: "Moderate" },
      { name: "Break hours exceeded", rule: "Daily break time above the policy limit", count: breaches.break_hours, sev: "High" },
      { name: "Break pauses > 4", rule: "More than four pause events in one day", count: breaches.break_counts, sev: "High" },
    ];
    const table = el("table", { class: "data-table" });
    table.appendChild(el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Rule"),
      el("th", { scope: "col" }, "Definition"),
      el("th", { scope: "col", class: "num" }, "Incidents"),
      el("th", { scope: "col" }, "Severity"))));
    const tbody = el("tbody");
    for (const rule of rules) {
      const severityNode = rule.sev === "High"
        ? el("span", { class: "badge-soft badge-rose" }, "High")
        : el("span", { class: "badge-soft badge-blue" }, "Moderate");
      tbody.appendChild(el("tr", {},
        el("td", { style: "font-weight:600;" }, rule.name),
        el("td", { class: "muted" }, rule.rule),
        el("td", { class: "num mono", style: "color:var(--err-strong);" }, fmtInt(rule.count)),
        el("td", {}, severityNode)));
    }
    table.appendChild(tbody);
    const rulesScroll = el("div", { class: "table-scroll" });
    rulesScroll.appendChild(table);
    section.appendChild(rulesScroll);

    const logSection = paneSection("Shift Boundary Anomaly Log",
      `Showing the top flagged employees of ${fmtInt(breaches.people)} people with violations.`);
    const flagHost = el("div");
    flagRows(flagHost, analytics.flagged || [], 8);
    logSection.appendChild(flagHost);
    pane.append(section, logSection);
  }

  function renderConsoleEmployees(pane, analytics) {
    const flagged = analytics.flagged || [];
    const counts = { all: flagged.length, critical: 0, elevated: 0, watch: 0 };
    for (const person of flagged) counts[bandOf(person.flags)]++;

    const section = paneSection("Flagged Profiles & Employee Audit",
      "Employees exceeding threshold limits across the report.");
    const controls = el("div", { class: "filter-bar" });
    const chips = [
      { key: "all", label: `All Flagged (${fmtInt(counts.all)})` },
      { key: "critical", label: `Critical (${fmtInt(counts.critical)})` },
      { key: "elevated", label: `Elevated (${fmtInt(counts.elevated)})` },
      { key: "watch", label: `Watch (${fmtInt(counts.watch)})` },
    ];
    for (const chip of chips) {
      const button = el("button", { type: "button", class: "chip", dataset: { band: chip.key } }, chip.label);
      if (chip.key === consoleState.band) button.classList.add("chip-active");
      button.addEventListener("click", () => {
        consoleState.band = chip.key;
        renderConsoleTab();
      });
      controls.appendChild(button);
    }
    const search = el("input", {
      type: "search",
      class: "chip-search",
      placeholder: "Filter by name…",
      "aria-label": "Filter flagged employees by name",
    });
    search.value = consoleState.query;
    search.addEventListener("input", () => {
      consoleState.query = search.value.trim().toLowerCase();
      renderEmployeeTable(section);
    });
    controls.appendChild(search);
    section.appendChild(controls);
    section.appendChild(el("p", { id: "empCount", class: "muted", style: "font-size:12px;margin-top:10px;" }));
    renderEmployeeTable(section);
    pane.appendChild(section);
  }

  function renderEmployeeTable(section) {
    const oldTable = section.querySelector(".console-emp-table");
    const oldCount = section.querySelector(".console-emp-count");
    if (oldTable) oldTable.remove();
    if (oldCount) oldCount.remove();
    const analytics = consoleState.analytics || {};
    const flagged = (analytics.flagged || []).filter((person) => {
      if (consoleState.band !== "all" && bandOf(person.flags) !== consoleState.band) return false;
      if (consoleState.query &&
        !(person.name || "").toLowerCase().includes(consoleState.query)) return false;
      return true;
    });
    const countP = el("p", { class: "muted console-emp-count", style: "font-size:12px;margin-bottom:8px;" },
      `Showing ${fmtInt(flagged.length)} flagged employee${flagged.length === 1 ? "" : "s"}`);
    section.appendChild(countP);
    const table = el("table", { class: "data-table tight console-emp-table" });
    table.appendChild(el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Employee"),
      el("th", { scope: "col", class: "num" }, "Late"),
      el("th", { scope: "col", class: "num" }, "Early"),
      el("th", { scope: "col", class: "num" }, "<4h"),
      el("th", { scope: "col", class: "num" }, "<8h"),
      el("th", { scope: "col", class: "num" }, "Break hrs"),
      el("th", { scope: "col" }, "Band"))));
    const tbody = el("tbody");
    for (const person of flagged) {
      tbody.appendChild(el("tr", {},
        el("td", {}, el("div", { class: "file-cell" },
          el("span", { class: avatarClass(person.flags) }, initials(person.name)),
          el("div", {},
            el("p", { style: "font-weight:600;", title: person.name || "—" }, person.name || "—"),
            el("p", { class: "muted", style: "font-size:11px;", title: personMeta(person) },
              personMeta(person))))),
        el("td", { class: "num mono" }, fmtInt(person.late)),
        el("td", { class: "num mono" }, fmtInt(person.early)),
        el("td", { class: "num mono" }, fmtInt(person.under4)),
        el("td", { class: "num mono" }, fmtInt(person.under8)),
        el("td", { class: "num mono" }, fmtInt(person.break_hours)),
        el("td", {}, bandChip(person.flags))));
    }
    if (!flagged.length) {
      tbody.appendChild(el("tr", {}, el("td", { colspan: "7", class: "muted" },
        "No flagged employees match this filter.")));
    }
    table.appendChild(tbody);
    const empScroll = el("div", { class: "table-scroll" });
    empScroll.appendChild(table);
    section.appendChild(empScroll);
  }

  /* ---- generate events ---- */
  gen.browseBtn.addEventListener("click", () => gen.fileInput.click());
  gen.fileInput.addEventListener("change", () => {
    addFiles(gen.fileInput.files);
    gen.fileInput.value = ""; // allow picking the same file again
  });

  ["dragenter", "dragover"].forEach((type) =>
    gen.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      gen.dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((type) =>
    gen.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      gen.dropzone.classList.remove("dragover");
    })
  );
  gen.dropzone.addEventListener("drop", (event) => {
    if (!gen.busy) addFiles(event.dataTransfer.files);
  });

  gen.runBtn.addEventListener("click", startJob);
  gen.clearFilesBtn.addEventListener("click", clearAllFiles);
  gen.resetBtn.addEventListener("click", resetAll);
  gen.retryBtn.addEventListener("click", resetAll);

  /* =================================================================
   * GENERATE MODE SWITCH + PORTAL FETCH FLOW
   * ================================================================= */
  const mode = {
    fetchTab: $("modeFetch"),
    uploadTab: $("modeUpload"),
    fetchPanel: $("fetchPanel"),
    uploadPanel: $("uploadPanel"),
    fetchBtn: $("fetchBtn"),
    fetchFrom: $("fetchFrom"),
    fetchTo: $("fetchTo"),
    fetchError: $("fetchError"),
    srcBiometric: $("srcBiometric"),
    srcTimechamp: $("srcTimechamp"),
    drive: $("fetchDrive"),
    fetchMail: $("fetchMail"),
    fetchMailRow: $("fetchMailRow"),
    fetchMailTo: $("fetchMailTo"),
    uploadDrive: $("uploadDrive"),
    uploadMail: $("uploadMail"),
    uploadMailRow: $("uploadMailRow"),
    uploadMailTo: $("uploadMailTo"),
    current: "fetch",
  };

  function isoDay(offsetDays) {
    const day = new Date();
    day.setDate(day.getDate() + offsetDays);
    return day.toISOString().slice(0, 10);
  }

  function setGenerateMode(next) {
    mode.current = next;
    const fetchActive = next === "fetch";
    mode.fetchTab.classList.toggle("active", fetchActive);
    mode.uploadTab.classList.toggle("active", !fetchActive);
    mode.fetchTab.setAttribute("aria-selected", String(fetchActive));
    mode.uploadTab.setAttribute("aria-selected", String(!fetchActive));
    mode.fetchPanel.hidden = !fetchActive;
    mode.uploadPanel.hidden = fetchActive;
  }

  mode.fetchTab.addEventListener("click", () => setGenerateMode("fetch"));
  mode.uploadTab.addEventListener("click", () => setGenerateMode("upload"));

  function showFetchError(message) {
    mode.fetchError.textContent = message;
    mode.fetchError.hidden = false;
    showErrorToast("Portal fetch problem", message);
  }

  function clearFetchError() {
    mode.fetchError.textContent = "";
    mode.fetchError.hidden = true;
  }

  function selectedFetchSources() {
    const sources = [];
    if (mode.srcBiometric.checked) sources.push("biometric");
    if (mode.srcTimechamp.checked) sources.push("timechamp");
    return sources;
  }

  async function startFetchJob() {
    if (gen.busy) return;
    clearFetchError();
    const sources = selectedFetchSources();
    if (!sources.length) {
      showFetchError("Select at least one source: Biometric portal or TimeChamp.");
      return;
    }
    const fromValue = mode.fetchFrom.value;
    const toValue = mode.fetchTo.value;
    if (!fromValue || !toValue) {
      showFetchError("Pick both a From date and a To date.");
      return;
    }
    if (fromValue > toValue) {
      showFetchError("The From date must be on or before the To date.");
      return;
    }

    gen.runPlaceholder.hidden = true;
    gen.resultCard.hidden = true;
    gen.errorCard.hidden = true;
    gen.log.textContent = "";
    gen.statusText.textContent = "Fetching raw data from the portals…";
    gen.statusCard.hidden = false;
    setBusy(true);
    mode.fetchBtn.disabled = true;
    announce("Fetching attendance data from the selected portals.");

    let payload = null;
    try {
      const fetchReqBody = {
        sources,
        from: fromValue,
        to: toValue,
        drive_upload: mode.drive.checked,
      };
      if (mode.fetchMail && mode.fetchMail.checked) {
        fetchReqBody.email_to = (mode.fetchMailTo && mode.fetchMailTo.value.trim()) || "__DEFAULT__";
      }
      const response = await fetch("/api/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fetchReqBody),
      });
      payload = await response.json().catch(() => null);
      if (!response.ok || !payload || !payload.ok) {
        const message = (payload && payload.error) ||
          `The portal fetch failed (${response.status}).`;
        finishWithError(message + "\n\nThe engine log above shows how far it got.");
        return;
      }
    } catch (networkError) {
      finishWithError(
        `Could not reach the report app: ${networkError.message}\n` +
        "Make sure the server is still running, then try again."
      );
      return;
    } finally {
      mode.fetchBtn.disabled = false;
    }

    // Each source runs as its own engine job; the monitor follows the LAST
    // one (all jobs run back-to-back, earlier reports land in history too).
    const jobIds = payload.job_ids || [payload.job_id].filter(Boolean);
    gen.jobId = jobIds[jobIds.length - 1];
    gen.jobQueue = jobIds.length > 1 ? jobIds : null;
    gen.cursor = 0;
    gen.statusText.textContent = sources.length > 1
      ? "Downloading raw data… (2 sources queued)"
      : "Downloading raw data…";
    pollJob();
  }

  mode.fetchBtn.addEventListener("click", startFetchJob);

  // Default the range to the last 7 days (attendance up to yesterday is available).
  const yesterday = isoDay(-1);
  mode.fetchFrom.max = yesterday;
  mode.fetchTo.max = yesterday;
  mode.fetchFrom.value = isoDay(-7);
  mode.fetchTo.value = yesterday;
  mode.fetchTo.min = mode.fetchFrom.value;

  mode.fetchFrom.addEventListener("change", () => {
    if (mode.fetchTo.value && mode.fetchFrom.value > mode.fetchTo.value) {
      mode.fetchTo.value = mode.fetchFrom.value;
    }
    mode.fetchTo.min = mode.fetchFrom.value;
  });
  mode.fetchTo.addEventListener("change", () => {
    if (mode.fetchTo.value > yesterday) {
      mode.fetchTo.value = yesterday;
    }
  });

  if (mode.fetchMail && mode.fetchMailRow) {
    mode.fetchMail.addEventListener("change", () => {
      mode.fetchMailRow.hidden = !mode.fetchMail.checked;
      if (mode.fetchMail.checked && mode.fetchMailTo) mode.fetchMailTo.focus();
    });
  }
  if (mode.uploadMail && mode.uploadMailRow) {
    mode.uploadMail.addEventListener("change", () => {
      mode.uploadMailRow.hidden = !mode.uploadMail.checked;
      if (mode.uploadMail.checked && mode.uploadMailTo) mode.uploadMailTo.focus();
    });
  }

  if (gen.cancelBtn) {
    gen.cancelBtn.addEventListener("click", async () => {
      if (!gen.jobId) return;
      if (!confirm("Are you sure you want to cancel this report generation?")) return;
      try {
        gen.cancelBtn.disabled = true;
        gen.statusText.textContent = "Cancelling…";
        await fetch(`/api/jobs/${encodeURIComponent(gen.jobId)}/cancel`, { method: "POST" });
        window.clearInterval(gen.pollTimer);
        gen.statusCard.hidden = true;
        gen.runPlaceholder.hidden = false;
        setBusy(false);
        showWarnToast("Run cancelled", "Report generation was cancelled.");
        announce("Report generation cancelled.");
        maybeRefreshHistory();
      } catch (err) {
        showErrorToast("Cancel failed", apiErrorText(err));
      } finally {
        gen.cancelBtn.disabled = false;
      }
    });
  }

  // Populate email recipient placeholder from default mail settings
  fetchJSON("/api/mail-settings").then((res) => {
    const rec = (res && res.settings && res.settings.recipients) || "";
    if (rec) {
      const ph = `e.g. ${rec} (or enter custom)`;
      if (mode.fetchMailTo) mode.fetchMailTo.placeholder = ph;
      if (mode.uploadMailTo) mode.uploadMailTo.placeholder = ph;
    }
  }).catch(() => {});

  /* ---------------- portal + mail settings modal ---------------- */
  const portal = {
    dialog: $("portalDialog"),
    openBtn: $("portalSettingsBtn"),
    form: $("portalForm"),
    cancel: $("portalCancel"),
    error: $("portalError"),
    fields: {
      biometric: { url: $("bioUrl"), user: $("bioUser"), pass: $("bioPass") },
      timechamp: { url: $("tcUrl"), user: $("tcUser"), pass: $("tcPass") },
    },
    mail: {
      host: $("mailHost"),
      port: $("mailPort"),
      user: $("mailUser"),
      pass: $("mailPass"),
      from: $("mailFrom"),
      recipients: $("mailRecipients"),
      tls: $("mailTls"),
      testBtn: $("mailTestBtn"),
      testResult: $("mailTestResult"),
    },
    drive: {
      enabled: $("driveEnabled"),
      folder: $("driveFolder"),
      uploadRaw: $("driveUploadRaw"),
      uploadReports: $("driveUploadReports"),
      deleteSync: $("driveDeleteSync"),
      saEmail: $("driveSaEmail"),
      testBtn: $("driveTestBtn"),
      testResult: $("driveTestResult"),
    },
  };

  function openPortalDialog() {
    portal.error.hidden = true;
    portal.mail.testResult.textContent = "";
    portal.drive.testResult.textContent = "";
    Promise.all([
      fetchJSON("/api/portal-settings"),
      fetchJSON("/api/mail-settings"),
      fetchJSON("/api/drive-settings"),
    ]).then(([portalPayload, mailPayload, drivePayload]) => {
      const settings = (portalPayload && portalPayload.settings) || {};
      for (const source of ["biometric", "timechamp"]) {
        const data = settings[source] || {};
        portal.fields[source].url.value = data.base_url || "";
        portal.fields[source].user.value = data.username || "";
        portal.fields[source].pass.value = "";
        portal.fields[source].pass.placeholder = data.has_password
          ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (leave blank to keep)" : "not set";
      }
      const mail = (mailPayload && mailPayload.settings) || {};
      portal.mail.host.value = mail.smtp_host || "";
      portal.mail.port.value = mail.smtp_port || 587;
      portal.mail.user.value = mail.username || "";
      portal.mail.pass.value = "";
      portal.mail.pass.placeholder = mail.has_password
        ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (leave blank to keep)" : "not set";
      portal.mail.from.value = mail.from_addr || "";
      portal.mail.recipients.value = mail.recipients || "";
      portal.mail.tls.checked = Boolean(mail.use_tls);
      const drive = (drivePayload && drivePayload.settings) || {};
      portal.drive.enabled.checked = Boolean(drive.enabled);
      portal.drive.folder.value = drive.folder_id || "";
      portal.drive.uploadRaw.checked = drive.upload_raw !== false;
      portal.drive.uploadReports.checked = drive.upload_reports !== false;
      portal.drive.deleteSync.checked = drive.delete_with_history !== false;
      portal.drive.saEmail.textContent = drive.service_account_email
        || (drive.auth_source === "gcp-adc"
          ? "auto — instance's attached service account"
          : "not detected yet — Test Drive connection to confirm");
      portal.dialog.hidden = false;
      portal.fields.biometric.user.focus();
    }).catch((error) => {
      announce("Could not load settings: " + apiErrorText(error));
    });
  }

  portal.openBtn.addEventListener("click", openPortalDialog);
  portal.cancel.addEventListener("click", () => { portal.dialog.hidden = true; });
  portal.dialog.addEventListener("click", (event) => {
    if (event.target === portal.dialog) portal.dialog.hidden = true;
  });

  portal.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    portal.error.hidden = true;
    const body = {};
    for (const source of ["biometric", "timechamp"]) {
      body[source] = {
        base_url: portal.fields[source].url.value.trim(),
        username: portal.fields[source].user.value.trim(),
      };
      const password = portal.fields[source].pass.value;
      if (password) body[source].password = password;
    }
    body.mail = {
      smtp_host: portal.mail.host.value.trim(),
      smtp_port: portal.mail.port.value || 587,
      username: portal.mail.user.value.trim(),
      from_addr: portal.mail.from.value.trim(),
      recipients: portal.mail.recipients.value.trim(),
      use_tls: portal.mail.tls.checked,
    };
    const mailPassword = portal.mail.pass.value;
    if (mailPassword) body.mail.password = mailPassword;
    body.gdrive = {
      enabled: portal.drive.enabled.checked,
      folder_id: portal.drive.folder.value.trim(),
      upload_raw: portal.drive.uploadRaw.checked,
      upload_reports: portal.drive.uploadReports.checked,
      delete_with_history: portal.drive.deleteSync.checked,
    };
    try {
      await fetchJSON("/api/portal-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      portal.dialog.hidden = true;
      announce("Settings saved.");
      showOkToast("Settings saved", "Portal and email settings are up to date.");
    } catch (error) {
      portal.error.textContent = apiErrorText(error);
      portal.error.hidden = false;
    }
  });

  portal.mail.testBtn.addEventListener("click", async () => {
    portal.mail.testResult.textContent = "Sending…";
    // Save current form values first so the test uses what is on screen.
    const body = { mail: {
      smtp_host: portal.mail.host.value.trim(),
      smtp_port: portal.mail.port.value || 587,
      username: portal.mail.user.value.trim(),
      from_addr: portal.mail.from.value.trim(),
      recipients: portal.mail.recipients.value.trim(),
      use_tls: portal.mail.tls.checked,
    } };
    const mailPassword = portal.mail.pass.value;
    if (mailPassword) body.mail.password = mailPassword;
    try {
      await fetchJSON("/api/mail-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await fetchJSON("/api/mail-test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      portal.mail.testResult.textContent = `Sent to ${(result.sent_to || []).join(", ")}`;
      showOkToast("Test email sent", `Delivered to ${(result.sent_to || []).join(", ")}`);
    } catch (error) {
      portal.mail.testResult.textContent = "";
      showWarnToast("Test email failed", apiErrorText(error));
    }
  });

  /* ---------------- Google Drive settings test ---------------- */
  portal.drive.testBtn.addEventListener("click", async () => {
    portal.drive.testResult.textContent = "Testing…";
    // Save what's on screen so the test uses the current values.
    try {
      await fetchJSON("/api/drive-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: portal.drive.enabled.checked,
          folder_id: portal.drive.folder.value.trim(),
          upload_raw: portal.drive.uploadRaw.checked,
          upload_reports: portal.drive.uploadReports.checked,
          delete_with_history: portal.drive.deleteSync.checked,
        }),
      });
      const result = await fetchJSON("/api/drive-test", { method: "POST", body: JSON.stringify({}) });
      portal.drive.testResult.textContent = `Connected to “${result.folder_name}”`;
      showOkToast("Drive connected", `Folder “${result.folder_name}” is writable.`);
    } catch (error) {
      portal.drive.testResult.textContent = "";
      showErrorToast("Drive connection failed", apiErrorText(error));
    }
  });

  // Warn before closing/reloading while files are pending or a run is active.
  window.addEventListener("beforeunload", (event) => {
    if (gen.busy || (gen.files.length > 0 && !gen.jobId)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  /* =================================================================
   * SCHEDULES VIEW (daily / weekly / monthly auto-fetch)
   * ================================================================= */
  const sched = {
    rows: $("schedRows"),
    tableCard: $("schedTableCard"),
    empty: $("schedEmpty"),
    emptyCta: $("schedEmptyCta"),
    newBtn: $("newScheduleBtn"),
    error: $("schedError"),
    note: $("schedNote"),
    dialog: $("scheduleDialog"),
    form: $("scheduleForm"),
    formTitle: $("scheduleFormTitle"),
    cancel: $("scheduleCancel"),
    errorField: $("scheduleError"),
    name: $("schedName"),
    freq: $("schedFreq"),
    weekdayField: $("schedWeekdayField"),
    weekdayLabel: $("schedWeekdayLabel"),
    sendDays: $("schedSendDays"),
    dayHint: $("schedDayHint"),
    monthDayField: $("schedMonthDayField"),
    monthDay: $("schedMonthDay"),
    time: $("schedTime"),
    range: $("schedRange"),
    srcBiometric: $("schedSrcBiometric"),
    srcTimechamp: $("schedSrcTimechamp"),
    enabled: $("schedEnabled"),
    emailChips: $("schedEmailChips"),
    emailPlus: $("schedEmailPlus"),
    emailAddRow: $("schedEmailAddRow"),
    emailInput: $("schedEmailInput"),
    emailAdd: $("schedEmailAdd"),
    drive: $("schedDrive"),
    monthly: $("schedMonthly"),
    monthlyHint: $("schedMonthlyHint"),
    compileBtn: $("compileMonthBtn"),
    editingId: null,
    emailList: [],
    cache: [],
  };

  const WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const FREQ_LABELS = { daily: "Daily", weekly: "Weekly", monthly: "Monthly" };

  // Fill the month-day picker (1..31) once.
  for (let day = 1; day <= 31; day++) {
    sched.monthDay.appendChild(el("option", { value: String(day) }, String(day)));
  }

  function fmtClock(epochSeconds) {
    if (!epochSeconds) return "—";
    return dateTimeLabel(epochSeconds);
  }

  function fmtNextRun(epochSeconds) {
    if (!epochSeconds) return "—";
    // Legacy rows stored this as ms/µs; collapse to seconds before showing.
    while (epochSeconds > 1e11) epochSeconds /= 1000;
    const nowSec = Date.now() / 1000;
    if (epochSeconds < nowSec - 366 * 86400 || epochSeconds > nowSec + 366 * 86400) {
      return "—"; // implausible even after normalization
    }
    const delta = epochSeconds - Date.now() / 1000;
    const when = new Date(epochSeconds * 1000);
    const clock = when.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    if (delta <= 0) return "due now";
    if (delta < 3600) return `in ${Math.max(1, Math.round(delta / 60))} min`;
    if (delta < 86400) {
      const hours = Math.floor(delta / 3600);
      return `in ${hours}h ${Math.round((delta % 3600) / 60)}m`;
    }
    return dateTimeLabel(epochSeconds);
  }

  const RANGE_LABELS = {
    full: "previous month",
  };

  function rangeLabel(row) {
    if (row.range_days in RANGE_LABELS) return RANGE_LABELS[row.range_days];
    const days = Number(row.range_days) || 1;
    return days === 1 ? "yesterday" : `last ${days} days`;
  }

  function scheduleSubtitle(row) {
    const dayList = (row.send_days || []).length
      ? (row.send_days || []).map((d) => WEEKDAY_LABELS[d] || "?").join(", ")
      : (WEEKDAY_LABELS[row.weekday || 0] || "?");
    if (row.frequency === "weekly") {
      return `${dayList} at ${row.time} · ${rangeLabel(row)}`;
    }
    if (row.frequency === "monthly") {
      return `day ${row.month_day || 1} of the month at ${row.time} · ${rangeLabel(row)}`;
    }
    // Daily: selected days limit the run to those days only.
    return (row.send_days || []).length
      ? `${dayList} at ${row.time} · ${rangeLabel(row)}`
      : `every day at ${row.time} · ${rangeLabel(row)}`;
  }

  function sourceChips(sources) {
    const wrap = el("div", { class: "source-chips" });
    for (const source of sources || []) {
      wrap.appendChild(el("span", { class: "source-chip" },
        source === "biometric" ? "Biometric" : "TimeChamp"));
    }
    return wrap;
  }

  function scheduleRow(row) {
    const tr = el("tr", { dataset: { id: row.id } });

    const nameTd = el("td");
    nameTd.appendChild(el("div", { class: "file-cell" },
      el("span", { class: "material-symbols-outlined muted", style: "font-size:17px;", "aria-hidden": "true" },
        row.enabled ? "event_repeat" : "event_busy"),
      el("div", {},
        el("p", { style: "font-weight:600;" }, row.name || "—"),
        el("p", { class: "muted", style: "font-size:11px;" }, scheduleSubtitle(row)))));

    const rhythmTd = el("td");
    rhythmTd.appendChild(el("span", {
      class: "type-chip",
      dataset: { type: FREQ_LABELS[row.frequency] || row.frequency },
    }, FREQ_LABELS[row.frequency] || row.frequency || "—"));
    rhythmTd.appendChild(el("span", {
      class: "muted", style: "display:block;font-size:11px;margin-top:4px;",
    }, row.enabled ? "Enabled" : "Paused"));

    const sourcesTd = el("td");
    sourcesTd.appendChild(sourceChips(row.sources));
    if (row.drive_upload) {
      sourcesTd.appendChild(el("span", {
        class: "email-chip", title: "Runs back up raw data & reports to Google Drive",
      }, "\u2601 Drive"));
    }
    if (row.monthly_compile) {
      sourcesTd.appendChild(el("span", {
        class: "email-chip", title: "Compiles the previous month's raw data into a monthly workbook",
      }, "Monthly"));
    }
    if (row.email_to) {
      sourcesTd.appendChild(el("span", {
        class: "email-chip",
        title: `Report emailed to ${row.email_to}`,
      }, `\u2709 ${row.email_to.split(",")[0].trim()}${row.email_to.includes(",") ? " +more" : ""}`));
    }

    const nextTd = el("td");
    nextTd.appendChild(el("span", { class: "mono", style: "font-size:12px;" },
      row.enabled ? fmtNextRun(row.next_run) : "paused"));

    const lastTd = el("td");
    if (row.last_run) {
      lastTd.appendChild(el("span", { style: "font-size:12px;" }, fmtClock(row.last_run)));
      lastTd.appendChild(el("span", {
        class: "muted", style: "display:block;font-size:11px;margin-top:2px;",
      }, (row.last_status || "").startsWith("error")
        ? String(row.last_status).slice(0, 60)
        : "completed"));
    } else {
      lastTd.appendChild(el("span", { class: "muted", style: "font-size:12px;" }, "never"));
    }

    const actionsTd = el("td", { class: "num" });
    const actions = el("div", { class: "row-actions" });
    const runNow = el("button", {
      type: "button", class: "btn-mini primary", title: "Run this schedule now",
    }, "Run now");
    runNow.addEventListener("click", () => runScheduleNow(row));
    const edit = el("button", {
      type: "button", class: "icon-btn", title: "Edit schedule",
      "aria-label": `Edit schedule ${row.name}`,
    }, icon("view", 18));
    edit.addEventListener("click", () => openScheduleDialog(row));
    const toggle = el("button", {
      type: "button", class: "icon-btn", title: row.enabled ? "Pause schedule" : "Resume schedule",
      "aria-label": (row.enabled ? "Pause schedule " : "Resume schedule ") + row.name,
    }, icon("refresh", 18));
    toggle.addEventListener("click", () => toggleSchedule(row));
    const del = el("button", {
      type: "button", class: "icon-btn danger", title: "Delete schedule",
      "aria-label": `Delete schedule ${row.name}`,
    }, icon("trash", 18));
    del.addEventListener("click", () => deleteSchedule(row));
    actions.append(runNow, edit, toggle, del);
    actionsTd.appendChild(actions);

    tr.append(nameTd, rhythmTd, sourcesTd, nextTd, lastTd, actionsTd);
    return tr;
  }

  async function renderSchedules() {
    clearAlert(sched.error);
    try {
      const payload = await fetchJSON("/api/schedules");
      sched.cache = payload.schedules || [];
    } catch (error) {
      setAlert(sched.error, "Could not load schedules: " + apiErrorText(error));
      return;
    }
    sched.rows.textContent = "";
    for (const row of sched.cache) sched.rows.appendChild(scheduleRow(row));
    const has = sched.cache.length > 0;
    sched.tableCard.hidden = !has;
    sched.empty.hidden = has;
    sched.note.hidden = !has;
  }

  async function runScheduleNow(row) {
    try {
      const payload = await fetchJSON(`/api/schedules/${encodeURIComponent(row.id)}/run`, {
        method: "POST",
      });
      announce("Schedule started — opening the run monitor.");
      location.hash = "#/generate";
      setGenerateMode("fetch");
      gen.runPlaceholder.hidden = true;
      gen.resultCard.hidden = true;
      gen.errorCard.hidden = true;
      gen.log.textContent = "";
      gen.statusText.textContent = `Scheduled fetch · ${row.name}…`;
      gen.statusCard.hidden = false;
      setBusy(true);
      const jobIds = payload.job_ids || [payload.job_id].filter(Boolean);
      gen.jobId = jobIds[jobIds.length - 1];
      gen.jobQueue = jobIds.length > 1 ? jobIds : null;
      gen.cursor = 0;
      pollJob();
    } catch (error) {
      setAlert(sched.error, "Run now failed: " + apiErrorText(error));
    }
  }

  async function toggleSchedule(row) {
    try {
      await fetchJSON(`/api/schedules/${encodeURIComponent(row.id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...row, enabled: !row.enabled }),
      });
      renderSchedules();
      announce(row.enabled ? "Schedule paused." : "Schedule resumed.");
    } catch (error) {
      setAlert(sched.error, "Could not update the schedule: " + apiErrorText(error));
    }
  }

  async function deleteSchedule(row) {
    const ok = await confirmAction(
      "Delete this schedule?",
      `“${row.name}” will stop running automatically. Past reports stay in Reports History.`,
      "Delete Schedule"
    );
    if (!ok) return;
    try {
      await fetchJSON(`/api/schedules/${encodeURIComponent(row.id)}`, { method: "DELETE" });
      renderSchedules();
      announce("Schedule deleted.");
    } catch (error) {
      setAlert(sched.error, "Could not delete the schedule: " + apiErrorText(error));
    }
  }

  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function renderEmailChips() {
    sched.emailChips.replaceChildren();
    for (const addr of sched.emailList) {
      const chip = el("span", { class: "email-chip", title: addr }, addr);
      const remove = el("button", {
        type: "button", class: "chip-remove",
        title: `Remove ${addr}`, "aria-label": `Remove ${addr}`,
      }, "\u00d7");
      remove.addEventListener("click", () => {
        sched.emailList = sched.emailList.filter((a) => a !== addr);
        renderEmailChips();
      });
      chip.appendChild(remove);
      sched.emailChips.appendChild(chip);
    }
  }

  function addEmailFromInput() {
    const value = sched.emailInput.value.trim().replace(/,+$/, "");
    if (!value) return;
    for (const piece of value.split(",")) {
      const addr = piece.trim();
      if (!addr) continue;
      if (!EMAIL_RE.test(addr)) {
        showToast("error", "Invalid email", `"${addr}" is not a valid email address.`);
        return;
      }
      if (!sched.emailList.some((a) => a.toLowerCase() === addr.toLowerCase())) {
        sched.emailList.push(addr);
      }
    }
    sched.emailInput.value = "";
    renderEmailChips();
    // Collapse after adding; the "+" button reopens it for the next address.
    if (sched.emailAddRow) sched.emailAddRow.hidden = true;
    if (sched.emailPlus) {
      sched.emailPlus.setAttribute("aria-expanded", "false");
      sched.emailPlus.classList.remove("is-open");
    }
  }

  sched.emailAdd.addEventListener("click", addEmailFromInput);
  sched.emailInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addEmailFromInput();
    }
  });

  // The "+" button reveals the recipient input row; it hides again once an
  // address is added and stays available for the next one. Guarded so a
  // stale cached page (old HTML + new JS) can't break the whole script.
  if (sched.emailPlus && sched.emailAddRow) {
    sched.emailPlus.addEventListener("click", () => {
      const show = sched.emailAddRow.hidden;
      sched.emailAddRow.hidden = !show;
      sched.emailPlus.setAttribute("aria-expanded", show ? "true" : "false");
      sched.emailPlus.classList.toggle("is-open", show);
      if (show) sched.emailInput.focus();
    });
  }

  function openScheduleDialog(row) {
    sched.editingId = row ? row.id : null;
    sched.formTitle.textContent = row ? "Edit Schedule" : "New Schedule";
    sched.errorField.hidden = true;
    sched.name.value = row ? row.name || "" : "";
    sched.freq.value = row ? row.frequency || "daily" : "daily";
    sched.time.value = row ? row.time || "07:00" : "07:00";
    sched.range.value = row && row.range_days === "full"
      ? "full"
      : String(typeof row?.range_days === "number" ? row.range_days : 1);
    // Rows saved with the removed "workday" range now show "Yesterday only".
    sched.sendDaySet = new Set((row && row.send_days || []).map(Number));
    if (!sched.sendDaySet.size && row && row.weekday !== null && row.weekday !== undefined) {
      sched.sendDaySet = new Set([Number(row.weekday)]);  // legacy single-day rows
    }
    if (!sched.sendDaySet.size && row && (row.frequency || "daily") !== "daily") {
      sched.sendDaySet = new Set([0]);  // default Monday for new weekly rows
    }
    // Daily starts with no days selected = run every day.
    renderSendDays();
    sched.monthDay.value = String(row ? row.month_day || 1 : 1);
    sched.srcBiometric.checked = !row || (row.sources || []).includes("biometric");
    sched.srcTimechamp.checked = !row || (row.sources || []).includes("timechamp");
    sched.enabled.checked = row ? Boolean(row.enabled) : true;
    sched.emailList = (row && row.email_to ? row.email_to.split(",") : [])
      .map((addr) => addr.trim())
      .filter(Boolean);
    renderEmailChips();
    sched.emailInput.value = "";
    if (sched.emailAddRow) sched.emailAddRow.hidden = true;   // start collapsed
    if (sched.emailPlus) {
      sched.emailPlus.setAttribute("aria-expanded", "false");
      sched.emailPlus.classList.remove("is-open");
    }
    sched.drive.checked = row ? Boolean(row.drive_upload) : true;
    sched.monthly.checked = row ? Boolean(row.monthly_compile) : false;
    sched.monthlyHint.hidden = !sched.monthly.checked;
    syncScheduleFields();
    sched.dialog.hidden = false;
    sched.name.focus();
  }

  const DAY_LETTERS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  // The day picker shows for weekly schedules and, since runs can be
  // limited to chosen days, for daily schedules too (none selected =
  // every day). Monthly compiles use the "On date" picker instead.
  function dayPickerVisible() {
    const freq = sched.freq.value;
    return freq === "weekly" || (freq === "daily" && !sched.monthly.checked);
  }

  function renderSendDays() {
    const isDaily = sched.freq.value === "daily";
    sched.sendDays.replaceChildren();
    DAY_LETTERS.forEach((letter, index) => {
      const chip = el("button", {
        type: "button", class: "day-chip",
        dataset: { day: String(index) },
        "aria-pressed": sched.sendDaySet.has(index) ? "true" : "false",
        title: isDaily
          ? `Run on ${WEEKDAY_LABELS[index]}`
          : `Send the report on ${WEEKDAY_LABELS[index]}`,
      }, letter);
      chip.addEventListener("click", () => {
        if (sched.sendDaySet.has(index)) {
          if (sched.sendDaySet.size === 1 && !isDaily) return;  // weekly keeps one day
          sched.sendDaySet.delete(index);  // empty set = every day (daily)
        } else {
          sched.sendDaySet.add(index);
        }
        renderSendDays();
      });
      sched.sendDays.appendChild(chip);
    });
    syncScheduleFields();  // refresh the label + hint (e.g. "none = every day") live
  }

  function syncScheduleFields() {
    const freq = sched.freq.value;
    const compiling = sched.monthly.checked;
    sched.weekdayField.hidden = !dayPickerVisible();
    sched.monthDayField.hidden = freq !== "monthly";
    const picked = sched.sendDaySet && sched.sendDaySet.size;
    sched.weekdayLabel.textContent = freq === "monthly"
      ? "On day" : picked ? "Run on days" : "Run on days (none = every day)";
    // Monthly-compile mode generates from raw data already on Drive,
    // so the portal sources and fetch range do not apply — but the run
    // frequency/time stay configurable (it always compiles the previous
    // calendar month at fire time).
    [sched.range, sched.srcBiometric, sched.srcTimechamp]
      .forEach((control) => { control.disabled = compiling; });
    sched.monthDay.disabled = freq !== "monthly";
    sched.dayHint.hidden = !(dayPickerVisible() && !compiling);
  }

  sched.monthly.addEventListener("change", () => {
    sched.monthlyHint.hidden = !sched.monthly.checked;
    syncScheduleFields();
  });

  sched.newBtn.addEventListener("click", () => openScheduleDialog(null));
  sched.emptyCta.addEventListener("click", () => openScheduleDialog(null));
  sched.cancel.addEventListener("click", () => { sched.dialog.hidden = true; });
  sched.dialog.addEventListener("click", (event) => {
    if (event.target === sched.dialog) sched.dialog.hidden = true;
  });
  sched.freq.addEventListener("change", () => {
    // Weekly still needs at least one day (Monday by default); daily may
    // have none picked — that means run every day.
    if (sched.freq.value === "weekly" && !sched.sendDaySet.size) {
      sched.sendDaySet = new Set([0]);
    }
    syncScheduleFields();
    renderSendDays();
  });

  sched.range.addEventListener("change", syncScheduleFields);

  sched.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    sched.errorField.hidden = true;
    const sources = [];
    if (sched.srcBiometric.checked) sources.push("biometric");
    if (sched.srcTimechamp.checked) sources.push("timechamp");
    const body = {
      name: sched.name.value.trim(),
      frequency: sched.freq.value,
      time: sched.time.value,
      // Legacy single-weekday field: the first picked day (or Monday for
      // new weekly rows). Daily rows with no days picked send null so the
      // schedule fires every day.
      weekday: sched.sendDaySet.size
        ? [...sched.sendDaySet].sort((a, b) => a - b)[0]
        : (sched.freq.value === "daily" ? null : 0),
      send_days: [...sched.sendDaySet].sort((a, b) => a - b),
      month_day: sched.monthDay.value,
      sources,
      range_days: sched.range.value,
      enabled: sched.enabled.checked,
      email_to: sched.emailList.join(","),
      drive_upload: sched.drive.checked,
      monthly_compile: sched.monthly.checked,
    };
    try {
      if (sched.editingId) {
        await fetchJSON(`/api/schedules/${encodeURIComponent(sched.editingId)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        await fetchJSON("/api/schedules", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      sched.dialog.hidden = true;
      renderSchedules();
      announce(sched.editingId ? "Schedule updated." : "Schedule created.");
    } catch (error) {
      sched.errorField.textContent = apiErrorText(error);
      sched.errorField.hidden = false;
    }
  });

  /* ---------------- monthly compilation (from Drive dailies) ---------------- */
  const monthly = {
    dialog: $("monthlyDialog"),
    closeX: $("monthlyCloseX"),
    form: $("monthlyForm"),
    cancel: $("monthlyCancel"),
    go: $("monthlyGo"),
    month: $("monthlyMonth"),
    year: $("monthlyYear"),
    error: $("monthlyError"),
    run: $("monthlyRun"),
    log: $("monthlyLog"),
    summary: $("monthlySummary"),
    jobId: null,
    cursor: 0,
    pollTimer: null,
  };

  const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December"];

  // Fill month + year pickers once (years: this one and the two before).
  MONTH_NAMES.forEach((name, index) => {
    monthly.month.appendChild(el("option", { value: String(index + 1) }, name));
  });
  const thisYear = new Date().getFullYear();
  for (let y = thisYear; y >= thisYear - 2; y--) {
    monthly.year.appendChild(el("option", { value: String(y) }, String(y)));
  }
  monthly.month.value = String(new Date().getMonth() + 1);
  monthly.year.value = String(thisYear);



  sched.compileBtn.addEventListener("click", () => {
    monthly.error.hidden = true;
    monthly.dialog.hidden = false;
  });
  function stopMonthlyPoll() {
    if (monthly.pollTimer) window.clearInterval(monthly.pollTimer);
    monthly.pollTimer = null;
  }

  if (monthly.closeX) {
    monthly.closeX.addEventListener("click", () => {
      monthly.cancel.click();
    });
  }

  monthly.cancel.addEventListener("click", async () => {
    if (monthly.jobId && monthly.go.disabled) {
      if (!confirm("Compilation is currently in progress. Cancel it?")) return;
      try {
        await fetch(`/api/jobs/${encodeURIComponent(monthly.jobId)}/cancel`, { method: "POST" });
        showWarnToast("Compilation cancelled", "Monthly compilation was cancelled.");
      } catch (e) {
        // ignore
      }
      monthly.go.disabled = false;
      monthly.go.textContent = "Compile";
    }
    stopMonthlyPoll();
    monthly.dialog.hidden = true;
  });
  monthly.dialog.addEventListener("click", (event) => {
    if (event.target === monthly.dialog) {
      stopMonthlyPoll();
      monthly.dialog.hidden = true;
    }
  });

  monthly.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    monthly.error.hidden = true;
    monthly.summary.hidden = true;
    const year = parseInt(monthly.year.value, 10);
    const month = parseInt(monthly.month.value, 10);
    const monthLabel = `${MONTH_NAMES[month - 1]} ${year}`;
    monthly.go.disabled = true;
    monthly.go.textContent = "Compiling…";
    monthly.label = monthLabel;
    monthly.run.hidden = false;
    monthly.log.textContent = "";
    monthly.log.textContent = `[${new Date().toTimeString().slice(0, 8)}] Starting monthly compilation for ${monthLabel}...\n`;
    try {
      const result = await fetchJSON("/api/drive-monthly", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ year, month }),
      });
      monthly.jobId = result.job_id;
      monthly.cursor = 0;
      stopMonthlyPoll();
      monthly.pollTimer = window.setInterval(pollMonthly, 900);
    } catch (error) {
      const message = apiErrorText(error);
      monthly.error.textContent = message;
      monthly.error.hidden = false;
      showErrorToast(`Monthly compile failed — ${monthLabel}`, message);
      monthly.go.disabled = false;
      monthly.go.textContent = "Compile";
    }
  });

  async function pollMonthly() {
    if (!monthly.jobId) return;
    let data;
    try {
      const response = await fetch(
        `/api/jobs/${encodeURIComponent(monthly.jobId)}?cursor=${monthly.cursor}`
      );
      if (!response.ok) throw new Error("Lost contact with the engine.");
      data = await response.json();
    } catch (error) {
      stopMonthlyPoll();
      monthly.error.textContent =
        "Lost contact with the engine — check Reports History for the outcome.";
      monthly.error.hidden = false;
      monthly.go.disabled = false;
      monthly.go.textContent = "Compile";
      return;
    }
    for (const line of data.logs || []) {
      if (typeof line !== "string" || line === "") continue;
      const stamped = line.split("\n").map((part) => {
        if (part.trim() === "") return "";
        const ts = new Date().toTimeString().slice(0, 8);
        return `[${ts}] ${part}`;
      }).join("\n");
      monthly.log.textContent += stamped + "\n";
    }
    monthly.cursor = data.cursor;
    monthly.log.scrollTop = monthly.log.scrollHeight;
    if (data.status === "done") {
      stopMonthlyPoll();
      showMonthlySummary(data);
    } else if (data.status === "cancelled") {
      stopMonthlyPoll();
      monthly.error.textContent = "Monthly compilation was cancelled.";
      monthly.error.hidden = false;
      monthly.go.disabled = false;
      monthly.go.textContent = "Compile";
    } else if (data.status === "error") {
      stopMonthlyPoll();
      const message = data.error || "Monthly compilation failed.";
      monthly.error.textContent = message;
      monthly.error.hidden = false;
      showErrorToast(`Monthly compile failed — ${monthly.label}`, message);
      monthly.go.disabled = false;
      monthly.go.textContent = "Compile";
    }
  }

  function showMonthlySummary(data) {
    const s = data.summary || {};
    monthly.summary.textContent = "";
    monthly.summary.appendChild(
      el("p", { class: "monthly-summary-title" }, "Compilation complete"));
    const dl = el("dl");
    const row = (label, value) => {
      dl.appendChild(el("dt", {}, label));
      dl.appendChild(el("dd", {}, value || "—"));
    };
    row("Output", s.output_name);
    row("Raw files combined", s.files != null ? String(s.files) : "—");
    row("Drive location", s.folder && s.month
      ? `“${s.folder}” / ${s.year}/${String(s.month).toLowerCase()}/reports`
      : s.folder ? `“${s.folder}”` : "—");
    if (data.email_error) row("Email", `failed — ${data.email_error}`);
    else if (s.emailed) row("Email", "sent with the workbook attached");
    monthly.summary.appendChild(dl);
    monthly.summary.hidden = false;
    monthly.go.disabled = false;
    monthly.go.textContent = "Compile Again";
    showOkToast(
      "Monthly report compiled",
      `${s.output_name} — ${s.files || 0} raw file(s) combined, uploaded to “${s.folder}”.`,
      { actionLabel: "View report", actionHref: `#/reports/${encodeURIComponent(monthly.jobId)}` }
    );
    announce(`Monthly report compiled: ${s.output_name}.`);
  }

  /* ---------------- boot ---------------- */
  bindReportFilters();
  wireConsoleEvents();
  $("dashRefresh").addEventListener("click", () => {
    refreshCurrent();
    announce("Dashboard refreshed.");
  });
  renderFileList();

  // Re-clicking the pill of the view you are already on does not fire a
  // hashchange - refresh explicitly so the list always shows the newest runs.
  for (const pill of els.navPills) {
    pill.addEventListener("click", () => {
      if (parseRoute().name === pill.dataset.nav) refreshCurrent();
    });
  }
  // Returning to the tab after a background run also refreshes.
  window.addEventListener("focus", () => {
    if (!authEls.overlay.hidden) return; // still locked - nothing to refresh
    const name = parseRoute().name;
    if (name === "reports" || name === "dashboard") refreshCurrent();
  });

  // Show the app only after the session check passes.
  (async () => {
    const authed = await checkSession();
    if (authed) routeNow();
  })();
})();
