// «Історія» — the account's saved conversations (this browser's without a login) (server/conversations.py via /api/conversations): read one,
// continue it (Єва remembers it from the next call), copy or delete it.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  const clientId = store.get("va-client-id") || "";
  const auth = window.EvaAuth; // the login (web/auth.js)
  let current = "";
  const opened = new Map(); // conversation id → its turns, once opened

  // Text always goes in as text, never as HTML: these are people's conversations.
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

  const lines = (n) => {
    const m10 = n % 10, m100 = n % 100;
    return n + " " + (m10 === 1 && m100 !== 11 ? "репліка" : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? "репліки" : "реплік");
  };
  const fmt = (s) => new Date(s * 1000).toLocaleString("uk-UA", { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" });

  class ApiError extends Error {
    constructor(status, detail) { super(detail); this.status = status; }
  }

  async function api(path, opts = {}) {
    const res = await fetch(BACKEND + path, { ...opts, headers: { "X-Client-Id": clientId, ...auth.headers() } });
    if (!res.ok) {
      let detail = "Помилка " + res.status;
      try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
      if (res.status === 401) auth.toLogin();
      throw new ApiError(res.status, typeof detail === "string" ? detail : "Помилка " + res.status);
    }
    return res.json();
  }

  function message(text, kind = "") {
    $("msg").hidden = !text;
    $("msg").textContent = text || "";
    $("msg").className = "status-msg" + (kind ? " " + kind : "");
  }

  async function load(attempt = 0) {
    if (!clientId) {
      $("list").replaceChildren(el("p", { class: "muted empty" }, "У цьому браузері ще не було розмов з Євою."));
      return;
    }
    let data;
    try {
      data = await api("/api/conversations");
    } catch (err) {
      if (err instanceof ApiError) {
        $("list").replaceChildren(el("p", { class: "muted empty" }, err.status === 401 ? "Введіть код доступу." : err.message));
        return;
      }
      // Render's free plan sleeps: the first request wakes it (up to a minute).
      $("list").replaceChildren(el("p", { class: "muted empty" }, "Сервер прокидається — до хвилини…"));
      if (attempt < 30) setTimeout(() => load(attempt + 1), 3000);
      return;
    }
    current = data.current;
    $("storageNote").hidden = data.persistent;
    $("storageNote").textContent = "Сховище тимчасове: бази даних ще не під'єднано (DATABASE_URL на Render), і наступний деплой зітре історію.";
    render(data.conversations);
  }

  function render(list) {
    $("count").textContent = list.length ? String(list.length) : "";
    if (!list.length) {
      $("list").replaceChildren(el("p", { class: "muted empty" }, "Збережених розмов ще немає. Поговоріть з Євою — розмова з'явиться тут."));
      return;
    }
    $("list").replaceChildren(...list.map((c) => {
      const isCurrent = c.id === current;
      const body = el("div", { class: "conv-body", hidden: !opened.has(c.id) });
      if (opened.has(c.id)) fill(body, c, opened.get(c.id));
      const head = el("button", { class: "conv-head", type: "button", "aria-expanded": String(opened.has(c.id)) },
        el("span", { class: "set-text" },
          el("strong", {}, c.title || "Без слів"),
          el("span", { class: "muted small" }, fmt(c.updated_at) + " · " + lines(c.count))),
        isCurrent && el("span", { class: "badge-now" }, "поточна"));
      head.addEventListener("click", () => toggle(c, body, head));
      return el("article", { class: "set" + (isCurrent ? " current" : "") }, head, body);
    }));
  }

  // The conversation as it was said, then what can be done with it.
  function fill(body, c, turns) {
    const isCurrent = c.id === current;
    body.replaceChildren(
      el("div", { class: "log conv-log" },
        turns.map((t) => el("p", { class: "msg " + t.role }, t.text))),
      el("div", { class: "row set-actions" },
        isCurrent
          ? el("a", { class: "btn small-btn primary", href: "index.html" + location.search }, "Повернутися до неї")
          : el("button", { class: "btn small-btn primary", type: "button", onclick: () => resume(c) }, "Продовжити"),
        el("button", { class: "btn small-btn", type: "button", onclick: (e) => copy(turns, e.currentTarget) }, "Скопіювати"),
        el("button", { class: "btn small-btn ghost danger-text", type: "button", onclick: () => remove(c) }, "Видалити")));
  }

  async function toggle(c, body, head) {
    if (opened.has(c.id)) {
      opened.delete(c.id);
      body.hidden = true;
      head.setAttribute("aria-expanded", "false");
      return;
    }
    try {
      const full = await api("/api/conversations/" + encodeURIComponent(c.id));
      opened.set(c.id, full.turns);
      fill(body, c, full.turns);
      body.hidden = false;
      head.setAttribute("aria-expanded", "true");
    } catch (err) {
      message(err.message, "err");
    }
  }

  async function resume(c) {
    try {
      await api("/api/conversations/" + encodeURIComponent(c.id) + "/resume", { method: "POST" });
      location.href = "index.html" + location.search; // the main page shows it and Єва remembers it
    } catch (err) {
      message(err.message, "err");
    }
  }

  async function copy(turns, btn) {
    const text = turns.map((t) => (t.role === "user" ? "Ви" : "Єва") + ": " + t.text).join("\n\n");
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch { ok = false; }
    btn.textContent = ok ? "Скопійовано ✓" : "Не вдалося";
    setTimeout(() => { btn.textContent = "Скопіювати"; }, 1800);
  }

  async function remove(c) {
    const extra = c.id === current ? " Це поточна розмова — Єва її теж забуде." : "";
    if (!confirm("Видалити розмову «" + (c.title || "без слів") + "»?" + extra + " Відновити не можна.")) return;
    try {
      await api("/api/conversations/" + encodeURIComponent(c.id), { method: "DELETE" });
      opened.delete(c.id);
      message("Розмову видалено.");
      await load();
    } catch (err) {
      message(err.message, "err");
    }
  }

  $("backLink").href = "index.html" + location.search; // keep ?backend=… for a test backend
  if (!BACKEND) { $("list").replaceChildren(el("p", { class: "muted empty" }, "Не вказано адресу бекенду.")); return; }
  load();
})();
