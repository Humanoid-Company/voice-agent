// The login every page shares (server/accounts.py via /api/auth/*): the token in this browser, the header the
// backend checks, the account (name, role) for the page, and the way to the login page and back.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* private mode */ } },
  };
  const TOKEN = "va-auth-token";
  const ACCOUNT = "va-account";

  function account() {
    try { return JSON.parse(store.get(ACCOUNT) || "null"); } catch { return null; }
  }

  // ?backend=… (a test backend) rides along to the next page.
  const withBackend = (page) => {
    const b = params.get("backend");
    if (!b) return page;
    return page + (page.includes("?") ? "&" : "?") + "backend=" + encodeURIComponent(b);
  };

  window.EvaAuth = {
    BACKEND,
    token: () => store.get(TOKEN) || "",
    account,
    isAdmin: () => (account() || {}).role === "admin",
    headers: () => (store.get(TOKEN) ? { Authorization: "Bearer " + store.get(TOKEN) } : {}),
    save(data) {
      store.set(TOKEN, data.token);
      store.set(ACCOUNT, JSON.stringify(data.account));
    },
    setAccount(a) { store.set(ACCOUNT, JSON.stringify(a)); },
    clear() { store.remove(TOKEN); store.remove(ACCOUNT); },
    link: withBackend,
    // Not logged in (401): to the login page, then back here.
    toLogin() {
      this.clear();
      // Vercel serves pages without «.html» (cleanUrls): «/knowledge» and «knowledge.html» are the same page.
      const here = (location.pathname.split("/").pop() || "index").replace(/\.html$/, "");
      location.href = withBackend("login.html?next=" + encodeURIComponent(here));
    },
    // The account as the server sees it now (a role changed by an admin shows at once); null = logged out.
    async refresh() {
      if (!BACKEND || !store.get(TOKEN)) return null;
      try {
        const res = await fetch(BACKEND + "/api/auth/me", { headers: this.headers() });
        if (res.status === 401) { this.clear(); return null; }
        if (!res.ok) return account();
        const data = await res.json();
        this.setAccount(data.account);
        return data.account;
      } catch { return account(); } // asleep / offline: the last known one
    },
    async logout() {
      const clientId = store.get("va-client-id") || "";
      try {
        await fetch(BACKEND + "/api/auth/logout", { method: "POST", headers: { ...this.headers(), "X-Client-Id": clientId } });
      } catch { /* logged out here anyway */ }
      this.clear();
      location.href = withBackend("login.html");
    },
  };
})();
