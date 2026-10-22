// /logs — the team's view of past calls (server/call_log.py via /api/logs, token-protected) and of the
// devices' numbers (/api/diag). No link to it from the main page.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  const myClient = (store.get("va-client-id") || "").slice(0, 8);
  let token = store.get("va-logs-token") || "";
  // An admin's login opens the logs without the token (web/auth.js).
  const admin = () => Boolean(window.EvaAuth && window.EvaAuth.token() && window.EvaAuth.isAdmin());
  let calls = [];
  let current = null; // the call shown on the right
  let liveTimer = null;

  // Small element builder: text always goes in as text, never as HTML (these are people's conversations).
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c.nodeType ? c : String(c));
    return node;
  }

  const fmtTime = (s) => new Date(s * 1000).toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const fmtDur = (a, b) => {
    const s = Math.max(0, Math.round((b || Date.now() / 1000) - a));
    return s >= 60 ? Math.floor(s / 60) + " хв " + (s % 60) + " с" : s + " с";
  };
  const json = (v) => JSON.stringify(v, null, 2);
  // A long text (search results, the notes she gets) shows its start; the rest on a click.
  const long = (text, n) => {
    const s = String(text ?? "");
    return s.length <= n ? s : el("details", { class: "long" }, el("summary", {}, s.slice(0, n).trimEnd() + "…"), s);
  };

  async function api(path) {
    const res = await fetch(BACKEND + path, { headers: { "X-Logs-Token": token, ...(window.EvaAuth ? window.EvaAuth.headers() : {}) } });
    if (!res.ok) {
      let detail = res.status + "";
      try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // ── calls ──
  async function loadCalls() {
    if (!token && !admin()) { $("calls").replaceChildren(el("p", { class: "muted empty" }, "Увійдіть як адмін або введіть токен (LOGS_TOKEN на Render).")); return; }
    try {
      calls = await api("/api/logs?n=200");
    } catch (err) {
      const text = String(err.message).startsWith("logs_disabled")
        ? "На сервері не задано LOGS_TOKEN — додайте його в Render → Environment."
        : err.message === "bad_logs_token" ? "Невірний токен." : "Помилка: " + err.message;
      $("calls").replaceChildren(el("p", { class: "muted empty" }, text));
      return;
    }
    renderCalls();
  }

  function renderCalls() {
    const q = $("callSearch").value.trim().toLowerCase();
    const shown = calls.filter((c) => !q || [c.client, c.voice, c.prompt, c.first, c.session_id].join(" ").toLowerCase().includes(q));
    if (!shown.length) {
      $("calls").replaceChildren(el("p", { class: "muted empty" }, calls.length ? "Нічого не знайдено." : "Дзвінків ще немає (після деплою список порожній)."));
      return;
    }
    $("calls").replaceChildren(...shown.map((c) => el("button", {
      class: "call", "aria-current": current && current.session_id === c.session_id ? "true" : "false",
      onclick: () => openCall(c.session_id),
    },
    el("div", { class: "top" },
      el("strong", {}, fmtTime(c.started_at)),
      el("span", { class: "muted small" }, fmtDur(c.started_at, c.ended_at))),
    el("div", { class: "meta", style: "margin:4px 0" },
      c.ended_at ? null : el("span", { class: "badge live" }, "триває"),
      el("span", { class: "badge" }, c.client === myClient ? "цей браузер" : c.client),
      el("span", { class: "badge" }, c.voice), el("span", { class: "badge" }, c.prompt),
      el("span", { class: "badge" }, "реплік " + c.turns), c.tools ? el("span", { class: "badge" }, "інстр. " + c.tools) : null,
      c.errors ? el("span", { class: "badge err" }, "помилок " + c.errors) : null),
    c.first ? el("div", { class: "muted small" }, "«" + c.first + "»") : null)));
  }

  // ── one call ──
  const FILTERS = [
    ["talk", "розмова", true], ["tools", "інструменти", true], ["backend", "бекенд", true],
    ["notes", "нотатки", true], ["system", "службове", false],
  ];
  const filterOf = {
    user: "talk", eva: "talk", tool_call: "tools", tool_result: "tools", backend: "backend",
    backend_start: "system", backend_done: "system", delegation: "system", note: "notes", page_note: "notes",
    error: "talk", closed: "system",
  };

  async function openCall(id, keepScroll = false) {
    try { current = await api("/api/logs/" + encodeURIComponent(id)); } catch (err) {
      $("detail").replaceChildren(el("p", { class: "muted empty" }, "Помилка: " + err.message));
      return;
    }
    renderCall(keepScroll);
    renderCalls();
    clearInterval(liveTimer);
    if (!current.ended_at) liveTimer = setInterval(() => { if (current && !current.ended_at) openCall(current.session_id, true); }, 3000);
  }

  function eventRow(e, t0) {
    const t = "+" + (e.t - t0).toFixed(1) + "с";
    let cls = "k-sys", body;
    switch (e.kind) {
      case "user":
        cls = "k-user"; body = [el("span", { class: "who" }, e.via === "wake" ? "ви (пробудження)" : "ви"), e.text]; break;
      case "eva":
        cls = "k-eva"; body = [el("span", { class: "who" }, "Помічник"), e.text]; break;
      case "tool_call":
        cls = "k-tool";
        body = [el("span", { class: "who" }, "виклик"), el("strong", {}, e.name),
          el("span", { class: "muted small" }, "  " + (e.call_id || "")), el("pre", {}, json(e.args))];
        break;
      case "tool_result":
        cls = "k-tool";
        body = [el("span", { class: "who" }, "результат"), el("strong", {}, e.name), " ",
          el("span", { class: "badge " + (e.ok ? "ok" : "err") }, e.status), " ",
          el("span", { class: "muted small" }, e.ms + " мс" + (e.image ? " · кадр з камери" : "")),
          el("div", {}, long(e.message, 280)),
          e.data && Object.keys(e.data).length ? el("details", {}, el("summary", {}, "дані"), el("pre", {}, json(e.data))) : null];
        break;
      case "backend":
        cls = "k-tool"; body = [el("span", { class: "who" }, "відповідь бекенду"), long(e.text, 600)]; break;
      case "note":
        cls = "k-note"; body = [el("span", { class: "who" }, "нотатка сервера · " + e.via), long(e.text, 140)]; break;
      case "page_note":
        cls = "k-note"; body = [el("span", { class: "who" }, "нотатка сторінки · " + e.via), el("span", { class: "muted small" }, e.id)]; break;
      case "backend_start":
        body = "бекенд почав роботу · " + e.key; break;
      case "backend_done":
        body = "бекенд: " + e.status + " · " + e.key + (e.tokens_in != null ? " · токени " + e.tokens_in + " → " + e.tokens_out : "") + (e.error ? " · " + e.error : "");
        if (e.status !== "completed") cls = "k-err";
        break;
      case "delegation":
        body = "делегування → " + e.target + " · " + e.id; break;
      case "error":
        cls = "k-err"; body = ["помилка (" + e.source + "): ", e.detail]; break;
      case "closed":
        body = "дзвінок закрито"; break;
      default:
        body = e.kind + " " + json(e);
    }
    return el("div", { class: "ev " + cls, "data-f": filterOf[e.kind] || "system" }, el("div", { class: "t" }, t), el("div", { class: "b" }, body));
  }

  function renderCall(keepScroll) {
    const c = current;
    const shown = new Set(FILTERS.filter(([key, , def]) => (store.get("va-logs-f-" + key) ?? (def ? "1" : "0")) === "1").map(([key]) => key));
    const q = (store.get("va-logs-q") || "").toLowerCase();
    const scroll = window.scrollY;
    const rows = c.events
      .filter((e) => shown.has(filterOf[e.kind] || "system"))
      .filter((e) => !q || JSON.stringify(e).toLowerCase().includes(q))
      .map((e) => eventRow(e, c.started_at));
    $("detail").replaceChildren(
      el("div", { class: "top", style: "display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap" },
        el("strong", {}, fmtTime(c.started_at) + " · " + fmtDur(c.started_at, c.ended_at)),
        el("span", {},
          el("button", { class: "btn", type: "button", onclick: () => navigator.clipboard.writeText(json(c)) }, "Копіювати JSON"))),
      el("div", { class: "meta" },
        c.ended_at ? null : el("span", { class: "badge live" }, "триває · оновлюється"),
        ...[["клієнт", c.client === myClient ? c.client + " (цей браузер)" : c.client], ["голос", c.voice], ["промпт", c.prompt],
          ["модель", c.model], ["бекенд", c.backend + " · effort " + c.effort], ["історія", c.history + " повід."],
          ["темп/манера", c.speed + " / " + c.style], ["сесія", c.session_id]]
          .map(([k, v]) => el("span", { class: "badge" }, k + ": " + v))),
      el("div", { class: "filters" },
        ...FILTERS.map(([key, label]) => el("label", {},
          el("input", { type: "checkbox", checked: shown.has(key), onchange: (ev) => { store.set("va-logs-f-" + key, ev.target.checked ? "1" : "0"); renderCall(true); } }),
          label)),
        el("input", { type: "search", placeholder: "Пошук у дзвінку", value: store.get("va-logs-q") || "",
          oninput: (ev) => { store.set("va-logs-q", ev.target.value); renderCall(true); } })),
      rows.length ? el("div", { class: "timeline" }, rows) : el("p", { class: "muted empty" }, "Подій немає."));
    if (keepScroll) window.scrollTo(0, scroll);
  }

  // ── device numbers ──
  async function loadDiag() {
    let rows;
    try {
      const res = await fetch(BACKEND + "/api/diag?n=100");
      rows = await res.json();
    } catch (err) { $("diag").textContent = "Помилка: " + err.message; return; }
    if (!rows.length) { $("diag").textContent = "Звітів ще немає (сторінка шле їх під час дзвінка й після «Завершити»)."; return; }
    const cell = (v) => el("td", {}, v === undefined || v === null ? "" : typeof v === "object" ? el("pre", {}, json(v)) : String(v));
    $("diag").replaceChildren(el("table", {},
      el("thead", {}, el("tr", {}, ...["час", "клієнт", "коміт", "пристрій", "фільтр мікрофона", "перебивання", "мовчання", "аудіо"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...rows.slice().reverse().map((r) => {
        const d = r.data || {};
        return el("tr", {},
          cell(fmtTime(r.at) + (d.final ? " · кінець" : "")),
          cell(r.client === myClient.slice(0, 6) ? r.client + " (цей)" : r.client), cell(r.commit),
          cell([d.ios ? "iOS" : "", d.prompt, d.sens ? "sens" : "", d.clean ? "clean" : ""].filter(Boolean).join(" · ")),
          cell(d.gate), cell(d.barge), cell(d.silences && d.silences.length ? d.silences : "—"), cell(d.audio));
      }))));
  }

  // ── what the calls cost (server/usage.py): kept in the database, so a deploy doesn't clear it ──
  const usd = (v) => "$" + Number(v).toFixed(2);
  const KIND = { voice: "голос", concierge: "консьєрж" };
  const kinds = (k) => Object.entries(k || {}).map(([name, n]) => (KIND[name] || name) + " " + n).join(", ");
  async function loadUsage() {
    if (!token && !admin()) { $("usage").textContent = "Увійдіть як адмін або введіть токен (LOGS_TOKEN на Render)."; return; }
    let data;
    try { data = await api("/api/usage?days=30"); } catch (err) { $("usage").textContent = "Помилка: " + err.message; return; }
    if (!data.days.length) { $("usage").textContent = "Дзвінків за 30 днів ще не записано (облік іде з цього оновлення)."; return; }
    const p = data.prices;
    const table = (head, rows) => el("table", {},
      el("thead", {}, el("tr", {}, ...head.map((h) => el("th", {}, h)))),
      el("tbody", {}, ...rows.map((r) => el("tr", {}, ...r.map((v) => el("td", {}, String(v)))))));
    $("usage").replaceChildren(
      el("p", {}, el("strong", {}, "За 30 днів: " + usd(data.total.usd)), " · " + data.total.calls + " дзвінків · " + data.total.minutes + " хв"),
      el("p", { class: "small muted" }, "Оцінка, не рахунок: GPT-Live " + usd(p.live_per_minute) + "/хв відкритої сесії, бекенд $" +
        p.backend_in_per_m + " / $" + p.backend_out_per_m + " за 1 млн токенів (вхід / вихід). Лише цей застосунок; точне — на сторінці Usage в OpenAI."),
      el("h3", {}, "По днях"),
      table(["день", "дзвінків", "хвилин", "GPT-Live", "токени (вхід / вихід)", "бекенд", "разом", "які"],
        data.days.map((d) => [d.day, d.calls, d.minutes, usd(d.live_usd), d.tokens_in + " / " + d.tokens_out, "$" + d.backend_usd.toFixed(3), usd(d.usd), kinds(d.kinds)])),
      el("h3", {}, "По браузерах"),
      table(["клієнт", "дзвінків", "хвилин", "разом", "які"],
        data.clients.map((c) => [c.client === myClient ? c.client + " (цей)" : c.client, c.calls, c.minutes, usd(c.usd), kinds(c.kinds)])));
  }

  // ── wiring ──
  function showTab(tab) {
    for (const [name, button, view] of [["calls", "tabCalls", "callsView"], ["diag", "tabDiag", "diagView"], ["usage", "tabUsage", "usageView"]]) {
      $(button).setAttribute("aria-selected", String(tab === name));
      $(view).hidden = tab !== name;
    }
    if (tab === "diag") loadDiag(); else if (tab === "usage") loadUsage(); else loadCalls();
  }
  $("tabCalls").addEventListener("click", () => showTab("calls"));
  $("tabDiag").addEventListener("click", () => showTab("diag"));
  $("tabUsage").addEventListener("click", () => showTab("usage"));
  $("tokenForm").addEventListener("submit", (e) => {
    e.preventDefault();
    token = $("token").value.trim();
    store.set("va-logs-token", token);
    loadCalls();
  });
  $("refresh").addEventListener("click", () => {
    if (!$("diagView").hidden) { loadDiag(); return; }
    if (!$("usageView").hidden) { loadUsage(); return; }
    loadCalls();
    if (current) openCall(current.session_id, true);
  });
  $("callSearch").addEventListener("input", renderCalls);
  $("token").value = token;
  if (!BACKEND) $("calls").replaceChildren(el("p", { class: "muted empty" }, "Не задано адресу бекенду (config.js або ?backend=…)."));
  else loadCalls();
})();
