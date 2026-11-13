// The main page's look on top of app.js, without touching its logic: it only watches what app.js writes.
//  - the status line → a big title and a line under it («Пауза — чекаю «…»» → «Пауза» / «Чекаю «…»»);
//  - .in-call on <body> while a call is open (app.js gives #talk the class «danger» then);
//  - the last line of the conversation as a caption under the orb;
//  - a round initial and a colour for every character chip;
//  - the drawer with devices, sound and behaviour;
//  - the orb follows the sound: your voice while it listens, the speaker while it talks.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const watch = (el, fn, opts) => { if (el) new MutationObserver(fn).observe(el, opts); };
  const upFirst = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");

  // ── Status: one big line + a hint under it ──
  const status = $("status");
  function showStatus() {
    const t = status.textContent.trim();
    let title = t, sub = "";
    if (!t || t === "Не підключено") title = "Готові до розмови";
    else {
      const i = t.indexOf(" — ");
      if (i > 0) { title = t.slice(0, i); sub = upFirst(t.slice(i + 3)); }
    }
    $("statusTitle").textContent = title;
    $("statusTitle").title = title;
    $("statusSub").textContent = sub || "\u00a0"; // never empty: the line keeps its place, nothing below it jumps
  }
  watch(status, showStatus, { childList: true, characterData: true, subtree: true });
  showStatus();

  // ── In a call or not ──
  const talk = $("talk");
  function showCall() {
    document.body.classList.toggle("in-call", talk.classList.contains("danger"));
    showCaption();
  }
  watch(talk, showCall, { attributes: true, attributeFilter: ["class"] });

  // ── Caption: what was said last, under the orb ──
  const log = $("log");
  function showCaption() {
    const caption = $("caption");
    const all = log.querySelectorAll(".msg.user, .msg.assistant");
    const last = all[all.length - 1];
    const text = last ? last.textContent.trim() : "";
    const on = document.body.classList.contains("in-call") && !!text;
    caption.style.visibility = on ? "visible" : "hidden"; // hidden, not removed: the space stays
    if (!on) return;
    const user = last.classList.contains("user");
    caption.textContent = user ? "«" + text + "»" : text;
    caption.classList.toggle("from-user", user);
  }
  watch(log, showCaption, { childList: true, characterData: true, subtree: true });

  // ── Character chips: a round initial in the character's colour ──
  const COLORS = [["meridian", "#2E64C9"], ["blunt", "#A84A26"], ["lesia", "#23785A"], ["kit", "#7A47B8"]];
  const SPARE = ["#2E64C9", "#A84A26", "#23785A", "#7A47B8", "#8A5A00", "#B03A6E"];
  function decorateChars() {
    [...$("characters").children].forEach((b, i) => {
      const initial = b.textContent.trim().charAt(0).toUpperCase();
      if (b.dataset.initial !== initial) b.dataset.initial = initial;
      const hit = COLORS.find(([prefix]) => (b.dataset.id || "").startsWith(prefix));
      b.style.setProperty("--av", hit ? hit[1] : SPARE[i % SPARE.length]);
    });
  }
  watch($("characters"), decorateChars, { childList: true });
  decorateChars();

  // ── The orb follows the sound ──
  // Microphone: getUserMedia is wrapped so every audio stream app.js opens gets a level meter too (app.js keeps
  // its stream untouched). Speaker: the call's audio element (#remote) is read the same way. Only analysers —
  // nothing is connected to the output, so nothing is heard twice.
  const orb = document.querySelector(".orb");
  const calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  let ctx = null, mic = null, out = null, outStream = null, lvl = 0, running = false;
  const buf = new Uint8Array(512);
  function audio() {
    if (!Ctx) return null;
    if (!ctx) { try { ctx = new Ctx(); } catch { return null; } }
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
  }
  function meter(stream, old) {
    if (old) { try { old.src.disconnect(); } catch { /* gone */ } }
    const c = audio();
    if (!c || !stream || !stream.getAudioTracks || !stream.getAudioTracks().length) return null;
    try {
      const an = c.createAnalyser();
      an.fftSize = 512;
      an.smoothingTimeConstant = 0.5;
      const src = c.createMediaStreamSource(stream);
      src.connect(an);
      return { an, src };
    } catch { return null; }
  }
  function read(m) {
    if (!m) return 0;
    m.an.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) { const x = (buf[i] - 128) / 128; sum += x * x; }
    return Math.min(1, Math.sqrt(sum / buf.length) * 5);
  }
  const md = navigator.mediaDevices;
  if (md && md.getUserMedia && !calm) {
    const gum = md.getUserMedia.bind(md);
    md.getUserMedia = async (c) => {
      const stream = await gum(c);
      if (stream.getAudioTracks().length) mic = meter(stream, mic);
      return stream;
    };
  }
  document.addEventListener("click", () => { if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {}); }, true);
  function tick() {
    const remote = $("remote");
    if (remote && remote.srcObject !== outStream) { outStream = remote.srcObject; out = meter(outStream, out); }
    const dot = $("dot").className;
    const muted = $("micMute").getAttribute("aria-pressed") === "true";
    const target = /\bspeak\b/.test(dot) ? read(out) : /\blive\b/.test(dot) && !muted ? read(mic) : 0;
    lvl = target > lvl ? lvl * 0.5 + target * 0.5 : lvl * 0.85 + target * 0.15; // quick up, soft down
    orb.style.setProperty("--lvl", lvl.toFixed(3));
    if (document.body.classList.contains("in-call") && !document.hidden) requestAnimationFrame(tick);
    else { running = false; lvl = 0; orb.style.setProperty("--lvl", "0"); }
  }
  function startLevels() {
    if (calm || running || !orb || !document.body.classList.contains("in-call")) return;
    running = true;
    requestAnimationFrame(tick);
  }
  watch(document.body, startLevels, { attributes: true, attributeFilter: ["class"] });
  document.addEventListener("visibilitychange", startLevels);

  // ── Drawer ──
  const drawer = $("drawer"), backdrop = $("drawerBackdrop"), opener = $("drawerOpen");
  let returnTo = null;
  function openDrawer(e) {
    if (e) e.preventDefault();
    returnTo = document.activeElement;
    drawer.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add("drawer-open");
    opener.setAttribute("aria-expanded", "true");
    $("drawerClose").focus();
  }
  function closeDrawer() {
    if (drawer.hidden) return;
    drawer.hidden = true;
    backdrop.hidden = true;
    document.body.classList.remove("drawer-open");
    opener.setAttribute("aria-expanded", "false");
    if (returnTo && document.contains(returnTo)) returnTo.focus();
  }
  opener.addEventListener("click", openDrawer);
  $("drawerClose").addEventListener("click", closeDrawer);
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !drawer.hidden) closeDrawer(); });
  document.querySelectorAll('a[href="#devices"]').forEach((a) => a.addEventListener("click", openDrawer));
})();
