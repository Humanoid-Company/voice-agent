// Вхід / Реєстрація (server/accounts.py via /api/auth/login and /api/auth/register). The browser's client id
// goes along, so what it said to Єва before logging in becomes the account's.
(() => {
  const auth = window.EvaAuth;
  const BACKEND = auth.BACKEND;
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  // Only our own pages: «next» is never a link out of the site.
  const asked = (params.get("next") || "").replace(/\.html$/, "");
  const next = /^[a-z]+$/.test(asked) && asked !== "login" ? asked + ".html" : "index.html";
  let clientId = "";
  try { clientId = localStorage.getItem("va-client-id") || ""; } catch { /* private mode */ }

  function message(text, kind = "") {
    $("msg").hidden = !text;
    $("msg").textContent = text || "";
    $("msg").className = "status-msg" + (kind ? " " + kind : "");
  }

  function showAdmin(open) {
    $("adminBlock").hidden = !open;
    $("adminToggle").setAttribute("aria-expanded", String(open));
  }

  function show(tab) {
    const login = tab === "login";
    $("tabLogin").setAttribute("aria-selected", String(login));
    $("tabRegister").setAttribute("aria-selected", String(!login));
    $("loginForm").hidden = !login;
    $("registerForm").hidden = login;
    message("");
    (login ? $("loginEmail") : $("regName")).focus();
  }

  async function send(path, body, form) {
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    message("Зачекайте…");
    try {
      const res = await fetch(BACKEND + path, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(clientId ? { "X-Client-Id": clientId } : {}) },
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await res.json(); } catch { /* not json */ }
      if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : "Помилка " + res.status;
        message(res.status === 422 ? "Заповніть усі поля." : detail, "err");
        return;
      }
      auth.save(data);
      location.href = auth.link(next);
    } catch {
      message("Сервер не відповідає — можливо, прокидається. Спробуйте за хвилину.", "err");
    } finally {
      button.disabled = false;
    }
  }

  // The usual way in: a name and the team's code (/api/auth/join).
  $("joinForm").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!$("joinName").value.trim()) { $("joinName").focus(); message("Напишіть, як вас звати.", "err"); return; }
    send("/api/auth/join", { name: $("joinName").value, invite_code: $("joinCode").value }, e.currentTarget);
  });
  $("adminToggle").addEventListener("click", () => {
    showAdmin($("adminBlock").hidden);
    if (!$("adminBlock").hidden) show("login");
  });
  $("tabLogin").addEventListener("click", () => show("login"));
  $("tabRegister").addEventListener("click", () => show("register"));
  $("loginForm").addEventListener("submit", (e) => {
    e.preventDefault();
    send("/api/auth/login", { email: $("loginEmail").value, password: $("loginPassword").value }, e.currentTarget);
  });
  $("registerForm").addEventListener("submit", (e) => {
    e.preventDefault();
    if ($("regPassword").value.length < 8) { message("Пароль — щонайменше 8 символів.", "err"); return; }
    send("/api/auth/register", {
      name: $("regName").value, email: $("regEmail").value,
      password: $("regPassword").value, invite_code: $("regCode").value,
    }, e.currentTarget);
  });

  if (!BACKEND) { message("Не вказано адресу бекенду.", "err"); return; }
  if (auth.token()) {
    // Already logged in (a bookmarked login page): straight on, if the login still works.
    auth.refresh().then((a) => { if (a) location.href = auth.link(next); });
  }
  if (location.hash === "#register" || location.hash === "#admin") {
    showAdmin(true);
    show(location.hash === "#register" ? "register" : "login");
  } else {
    $("joinName").focus();
  }
  // Without a team code (a local server) there is nothing to ask for.
  fetch(BACKEND + "/api/config").then((r) => r.json()).then((cfg) => {
    $("inviteBlock").hidden = !cfg.invite_required;
    $("joinCodeBlock").hidden = !cfg.invite_required;
  }).catch(() => {});
})();
