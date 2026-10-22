// «Налаштування»: the account (/api/auth/me, /api/auth/password), what runs where (/api/system), a short guide,
// and for admins the team's people (/api/admin/users).
(() => {
  const auth = window.EvaAuth;
  const BACKEND = auth.BACKEND;
  const cfg = window.APP_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  let me = auth.account();

  // Text always goes in as text, never as HTML.
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c.nodeType ? c : String(c));
    return node;
  }

  const fmtDate = (s) => (s ? new Date(s * 1000).toLocaleDateString("uk-UA", { day: "numeric", month: "long", year: "numeric" }) : "—");
  const fmtTime = (s) => (s ? new Date(s * 1000).toLocaleString("uk-UA", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "ніколи");
  const yesNo = (on, yes, no) => el("span", { class: on ? "ok-text" : "warn-text" }, on ? yes : no);
  const ROLE = { admin: "адмін", member: "учасник" };

  class ApiError extends Error {
    constructor(status, detail) { super(detail); this.status = status; }
  }

  async function api(path, opts = {}) {
    const res = await fetch(BACKEND + path, {
      ...opts,
      headers: { ...auth.headers(), ...(opts.body ? { "Content-Type": "application/json" } : {}) },
    });
    if (res.status === 401 && path !== "/api/auth/password") { auth.toLogin(); throw new ApiError(401, "Потрібен вхід"); }
    if (!res.ok) {
      let detail = "Помилка " + res.status;
      try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
      throw new ApiError(res.status, typeof detail === "string" ? detail : "Помилка " + res.status);
    }
    return res.json();
  }

  function message(text, kind = "") {
    $("msg").hidden = !text;
    $("msg").textContent = text || "";
    $("msg").className = "status-msg" + (kind ? " " + kind : "");
  }

  // ── tabs ──
  const TABS = ["account", "system", "help", "people"];
  function show(tab) {
    if (!TABS.includes(tab) || (tab === "people" && !auth.isAdmin())) tab = "account";
    for (const t of TABS) {
      $("tab-" + t).setAttribute("aria-selected", String(t === tab));
      $("view-" + t).hidden = t !== tab;
    }
    message("");
    if (history.replaceState) history.replaceState(null, "", "#" + tab);
    if (tab === "system") loadSystem();
    if (tab === "people") loadPeople();
  }

  // ── Акаунт ──
  function showAccount(a) {
    me = a;
    $("whoami").textContent = [a.name, a.email].filter(Boolean).join(" · ");
    $("name").value = a.name || "";
    // Came in by name and the team's code: the name is the login, no email or password.
    $("emailLabel").textContent = a.by_name ? "Вхід" : "Email";
    $("email").textContent = a.by_name ? "за ім'ям і кодом команди — змінене ім'я стане новим для входу" : a.email;
    $("passwordForm").hidden = Boolean(a.by_name);
    $("since").textContent = fmtDate(a.created_at);
    $("roleBadge").textContent = ROLE[a.role] || a.role;
    $("roleBadge").className = "pill" + (a.role === "admin" ? " accent" : "");
    $("roleHint").textContent = a.role === "admin"
      ? "Ви адмін: керуєте людьми, бачите логи й витрати, скидаєте відмітки консьєржа."
      : "Ви учасник: говорите з Євою, бачите свою історію й керуєте «Знаннями». Логи, витрати й людей бачить адмін.";
    for (const node of document.querySelectorAll(".admin-only")) node.hidden = a.role !== "admin";
    $("tab-people").hidden = a.role !== "admin";
  }

  $("profileForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const data = await api("/api/auth/me", { method: "POST", body: JSON.stringify({ name: $("name").value }) });
      auth.setAccount(data.account);
      showAccount(data.account);
      message("Ім'я збережено.", "ok");
    } catch (err) { message(err.message, "err"); }
  });

  $("passwordForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget; // gone from the event after the await
    if ($("pwNew").value.length < 8) { message("Новий пароль — щонайменше 8 символів.", "err"); return; }
    try {
      await api("/api/auth/password", { method: "POST", body: JSON.stringify({ current: $("pwCurrent").value, new: $("pwNew").value }) });
      form.reset();
      message("Пароль змінено. На інших пристроях потрібно увійти знову.", "ok");
    } catch (err) { message(err.message, "err"); }
  });

  $("logout").addEventListener("click", () => auth.logout());

  // ── Система ──
  async function loadSystem() {
    let s;
    try { s = await api("/api/system"); } catch (err) { if (err.status !== 401) message(err.message, "err"); return; }
    const d = s.deploy;
    $("sysCommit").replaceChildren(d.commit
      ? (d.commit_url ? el("a", { href: d.commit_url, target: "_blank", rel: "noopener" }, d.commit) : el("code", {}, d.commit))
      : el("span", { class: "muted" }, "невідомо (локальний запуск)"));
    $("sysBranch").textContent = d.branch || "—";
    $("sysStarted").textContent = fmtTime(d.started_at);
    $("sysHost").textContent = d.host;
    $("sysFront").textContent = location.host || "файл";
    $("sysLive").replaceChildren(el("code", {}, s.models.live));
    $("sysBackend").replaceChildren(el("code", {}, s.models.backend));
    $("sysEffort").textContent = s.models.effort;
    $("sysVoice").textContent = s.models.voice;
    $("sysSearch").replaceChildren(yesNo(s.services.web_search, "працює", "вимкнено (немає ключа Tavily)"));
    $("sysDb").replaceChildren(s.services.database === "postgres"
      ? yesNo(true, "Postgres (Neon) — дані зберігаються", "")
      : yesNo(s.services.persistent, "SQLite (локально)", "SQLite — наступний деплой зітре дані"));
    $("sysLogs").replaceChildren(yesNo(s.services.logs, "увімкнено", "вимкнено (немає LOGS_TOKEN)"));
    $("sysKnowledge").textContent = s.knowledge.sets ? s.knowledge.enabled + " з " + s.knowledge.sets + " наборів увімкнено" : "ще нічого не завантажено";
    $("sysAccounts").textContent = s.accounts ?? "—";
    $("sysSession").textContent = s.limits.session_minutes + " хв (далі розмова йде в новій сесії)";
    $("sysPause").textContent = (cfg.PAUSE_CLOSE_SECONDS || 15) + " с";
    $("sysIdle").textContent = (cfg.IDLE_PAUSE_SECONDS || 90) + " с без вашого голосу";
    $("sysPrice").textContent = "≈ $" + s.limits.live_price_per_minute.toFixed(2) + " за хвилину відкритої сесії";
    $("usageCard").hidden = !s.usage;
    if (s.usage) {
      $("useCalls").textContent = s.usage.calls;
      $("useMinutes").textContent = s.usage.minutes;
      $("useUsd").textContent = "$" + s.usage.usd.toFixed(2);
      $("useLast").textContent = s.usage.today ? s.usage.today.day + ": $" + s.usage.today.usd.toFixed(2) + " · " + s.usage.today.calls + " дзв." : "—";
    }
  }
  $("refreshSystem").addEventListener("click", loadSystem);

  // ── Люди ──
  async function loadPeople() {
    let data;
    try { data = await api("/api/admin/users"); } catch (err) { if (err.status !== 401) message(err.message, "err"); return; }
    $("inviteCode").value = data.invite_code || "";
    $("inviteCode").placeholder = data.invite_code ? "" : "не задано — реєстрація відкрита";
    $("peopleCount").textContent = data.accounts.length ? String(data.accounts.length) : "";
    $("people").replaceChildren(...data.accounts.map(person));
  }

  async function patch(a, body, done) {
    try {
      await api("/api/admin/users/" + a.id, { method: "POST", body: JSON.stringify(body) });
      message(done, "ok");
    } catch (err) { message(err.message, "err"); }
    loadPeople();
  }

  function person(a) {
    const self = me && a.id === me.id;
    const who = a.name || a.email;
    // By name: always a member, no password (anyone with the code could type that name).
    const role = a.by_name
      ? el("span", { class: "muted small" }, "учасник · вхід за ім'ям")
      : el("select", { "aria-label": "Роль " + who },
        el("option", { value: "member", selected: a.role === "member" }, "учасник"),
        el("option", { value: "admin", selected: a.role === "admin" }, "адмін"));
    if (!a.by_name) role.addEventListener("change", () => patch(a, { role: role.value }, who + " тепер " + ROLE[role.value] + "."));
    const active = el("input", { type: "checkbox", class: "switch", role: "switch", checked: !a.disabled, "aria-label": "Доступ " + who });
    active.addEventListener("change", () => {
      if (!active.checked && !confirm("Вимкнути доступ для " + who + "? Людину одразу буде розлогінено.")) { active.checked = true; return; }
      patch(a, { disabled: !active.checked }, active.checked ? "Доступ увімкнено." : "Доступ вимкнено.");
    });
    const reset = !a.by_name && el("button", { class: "btn small-btn ghost", type: "button" }, "Тимчасовий пароль");
    if (reset) reset.addEventListener("click", () => {
      const pw = prompt("Новий тимчасовий пароль для " + who + " (щонайменше 8 символів). Передайте його людині — вона змінить його в «Налаштуваннях».");
      if (pw) patch(a, { password: pw }, "Пароль для " + who + " змінено.");
    });
    return el("article", { class: "set person" + (a.disabled ? " off" : "") },
      el("div", { class: "set-head" },
        el("div", { class: "set-text" },
          el("strong", {}, (a.name || a.email) + (self ? " (ви)" : "")),
          el("span", { class: "muted small" }, [a.name ? a.email : "", "останній вхід: " + fmtTime(a.last_login_at)].filter(Boolean).join(" · "))),
        el("label", { class: "set-switch" }, el("span", { class: "small" }, a.disabled ? "Вимкнено" : "Активний"), active)),
      el("div", { class: "row set-actions" }, role, reset));
  }

  $("inviteShow").addEventListener("click", () => {
    const shown = $("inviteCode").type === "text";
    $("inviteCode").type = shown ? "password" : "text";
    $("inviteShow").textContent = shown ? "Показати" : "Сховати";
  });
  $("inviteCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("inviteCode").value); $("inviteCopy").textContent = "Скопійовано ✓"; } catch { $("inviteCopy").textContent = "Не вдалося"; }
    setTimeout(() => { $("inviteCopy").textContent = "Скопіювати"; }, 1800);
  });

  // ── start ──
  for (const t of TABS) $("tab-" + t).addEventListener("click", () => show(t));
  for (const a of document.querySelectorAll("[data-page]")) a.href = auth.link(a.dataset.page);
  $("backLink").href = auth.link("index.html");
  if (!BACKEND) { message("Не вказано адресу бекенду.", "err"); return; }
  if (!auth.token()) {
    // A local server without a team code needs no login, but an account page does.
    fetch(BACKEND + "/api/config").then((r) => r.json()).then((c) => {
      if (c.login_required) auth.toLogin();
      else { message("Ви не увійшли. Акаунт потрібен для налаштувань і людей.", ""); $("logout").textContent = "Увійти"; $("logout").onclick = () => auth.toLogin(); show("system"); }
    }).catch(() => message("Сервер не відповідає.", "err"));
    return;
  }
  if (me) showAccount(me);
  auth.refresh().then((a) => {
    if (!a) { auth.toLogin(); return; }
    showAccount(a);
    show(location.hash.slice(1) || "account");
  });
})();
