// «Налаштування»: the pages, what runs where (/api/system) and a short guide. No accounts: everything is open.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const cfg = window.APP_CONFIG || {};
  const $ = (id) => document.getElementById(id);

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

  const fmtTime = (s) => (s ? new Date(s * 1000).toLocaleString("uk-UA", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : "ніколи");
  const yesNo = (on, yes, no) => el("span", { class: on ? "ok-text" : "warn-text" }, on ? yes : no);

  class ApiError extends Error {
    constructor(status, detail) { super(detail); this.status = status; }
  }

  async function api(path, opts = {}) {
    const res = await fetch(BACKEND + path, {
      ...opts,
      headers: opts.body ? { "Content-Type": "application/json" } : {},
    });
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
  const TABS = ["pages", "system", "help"];
  function show(tab) {
    if (!TABS.includes(tab)) tab = "pages";
    for (const t of TABS) {
      $("tab-" + t).setAttribute("aria-selected", String(t === tab));
      $("view-" + t).hidden = t !== tab;
    }
    message("");
    if (history.replaceState) history.replaceState(null, "", "#" + tab);
    if (tab === "system") loadSystem();
  }

  // ── Система ──
  async function loadSystem() {
    let s;
    try { s = await api("/api/system"); } catch (err) { message(err.message, "err"); return; }
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

  // ── start ──
  for (const t of TABS) $("tab-" + t).addEventListener("click", () => show(t));
  // ?backend=… (a test backend) rides along to the next page.
  const link = (page) => (params.get("backend") ? page + (page.includes("?") ? "&" : "?") + "backend=" + encodeURIComponent(params.get("backend")) : page);
  for (const a of document.querySelectorAll("[data-page]")) a.href = link(a.dataset.page);
  $("backLink").href = link("index.html");
  if (!BACKEND) { message("Не вказано адресу бекенду.", "err"); return; }
  show(location.hash.slice(1) || "pages");
})();
