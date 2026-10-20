// /chat — for when you can't talk: type, and Єва answers aloud. No link to it from the other pages.
// It is the same conversation as the voice page (server/conversations.py). A call (GPT-Live over WebRTC)
// opens with the first message, sends silence instead of a mic (no permission asked), and each message
// goes to the backend as the user's message — the same path as a request said with the wake phrase
// (askBackend in web/app.js) — whose answer she says. A quiet call closes after a minute (it is billed
// per open second); the next message opens a new one with the whole conversation.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  // The voice page's id: the same conversation and memory.
  let clientId = store.get("va-client-id");
  if (!clientId) {
    clientId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
    store.set("va-client-id", clientId);
  }
  let accessCode = store.get("va-access-code") || "";
  const IDLE_CLOSE_MS = (Number(params.get("idle")) || 60) * 1000; // ?idle=5 for a quick test
  const TURN_PAUSE_MS = 3000; // her text pausing this long starts a new bubble

  let pc = null;
  let channel = null;
  let ready = null; // resolves once the call has started
  let connecting = null;
  let audioCtx = null;
  let outTrack = null; // silence: the call needs an outgoing audio track
  let idleTimer = null;
  let evaBubble = null;
  let evaAt = 0;
  let quietTimer = null; // «Говорить» → «На зв'язку» once her text stops
  let working = false; // the backend is on a message (searching, looking things up)

  const headers = () => ({ "Content-Type": "application/json", "X-Client-Id": clientId, "X-Access-Code": accessCode });
  const eventId = (p) => p + "_" + Math.random().toString(36).slice(2, 10);

  function setStatus(text, state = "") {
    $("status").textContent = text;
    $("dot").className = "dot" + (state ? " " + state : "");
  }

  function line(role, text) {
    const empty = $("messages").querySelector(".empty");
    if (empty) empty.remove();
    const p = document.createElement("p");
    p.className = "msg " + role;
    p.textContent = text;
    $("messages").appendChild(p);
    p.scrollIntoView({ block: "end" });
    return p;
  }

  // Sounds GPT-Live marks in its transcript («[hum]», «[breath]»): heard, not read.
  const spoken = (text) => text.replace(/\[[a-z][a-z _-]*\]\s*/gi, "");

  // Her answer as it is said: one bubble per answer (a tag may come split over deltas: kept raw, shown clean).
  function evaSays(delta) {
    const now = Date.now();
    if (!evaBubble || now - evaAt > TURN_PAUSE_MS) {
      evaBubble = line("assistant", "");
      evaBubble.raw = "";
      delta = delta.trimStart();
    }
    const text = evaBubble.raw;
    evaBubble.raw += /[.!?…]$/.test(text) && /^[^\s.,!?…»)\[]/.test(delta) ? " " + delta : delta;
    evaBubble.textContent = spoken(evaBubble.raw);
    evaAt = now;
    evaBubble.scrollIntoView({ block: "end" });
  }

  function showGate() { $("gate").hidden = false; $("code").focus(); }

  // What the call sends instead of a mic: noise far below hearing (−80 dB). Pure zeros let WebRTC stop
  // sending packets, and with no input flowing GPT-Live never said the backend's answers.
  function quietTrack(ctx) {
    const buffer = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = (Math.random() * 2 - 1) * 1e-4;
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.loop = true;
    const dest = ctx.createMediaStreamDestination();
    source.connect(dest);
    source.start();
    return dest.stream.getAudioTracks()[0];
  }

  // ── audio: started inside the tap (iPhone plays a call's sound only after one) ──
  function unlockAudio() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!audioCtx && AudioCtx) {
      audioCtx = new AudioCtx();
      outTrack = quietTrack(audioCtx);
      if (!$("voiceOut").srcObject) $("voiceOut").srcObject = audioCtx.createMediaStreamDestination().stream;
    }
    if (audioCtx && audioCtx.state !== "running") audioCtx.resume().catch(() => {});
    $("voiceOut").play().catch(() => {});
  }

  // Same as withClearOpus in web/app.js: her voice at a bitrate that keeps «с», «ш», «ц» clear.
  function withClearOpus(sdp) {
    const pt = (sdp.match(/a=rtpmap:(\d+) opus\/48000/i) || [])[1];
    if (!pt) return sdp;
    const want = { maxplaybackrate: "48000", maxaveragebitrate: "64000", useinbandfec: "1", stereo: "0" };
    return sdp.replace(new RegExp("a=fmtp:" + pt + " ([^\\r\\n]*)"), (all, p) => {
      const kept = p.split(";").filter((x) => x && !(x.split("=")[0].trim() in want));
      return "a=fmtp:" + pt + " " + kept.concat(Object.entries(want).map(([k, v]) => k + "=" + v)).join(";");
    });
  }

  function sendEvent(event) {
    if (channel && channel.readyState === "open") channel.send(JSON.stringify(event));
  }

  const isOpen = () => pc && channel && channel.readyState === "open";

  async function connect() {
    if (connecting) return connecting;
    connecting = (async () => {
      setStatus("Підключаюсь…", "busy");
      // The voice and manner chosen on the voice page (the server's, if picked there by voice).
      let me = {};
      try { const r = await fetch(BACKEND + "/api/me", { headers: headers() }); if (r.ok) me = await r.json(); } catch { /* asleep */ }
      const peer = new RTCPeerConnection();
      pc = peer;
      peer.ontrack = (e) => { $("voiceOut").srcObject = e.streams[0]; $("voiceOut").play().catch(() => {}); };
      if (outTrack) peer.addTrack(outTrack);
      else peer.addTransceiver("audio", { direction: "recvonly" });
      const ch = peer.createDataChannel("oai-events");
      channel = ch;
      let started;
      ready = new Promise((resolve, reject) => {
        started = resolve;
        setTimeout(() => reject(new Error("Єва не відповіла на підключення")), 20000);
      });
      ready.catch(() => {});
      ch.onmessage = (e) => { if (pc === peer) onEvent(e.data, started); };
      ch.onclose = () => { if (pc === peer) closed(); };
      peer.onconnectionstatechange = () => {
        if (pc === peer && (peer.connectionState === "failed" || peer.connectionState === "closed")) closed();
      };
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({
          sdp: withClearOpus(offer.sdp), chat: true,
          voice: me.voice || store.get("va-voice") || undefined,
          speed: me.speed || store.get("va-speed") || undefined,
          style: me.style || store.get("va-style") || undefined,
          prompt: store.get("va-prompt") || undefined,
        }),
      });
      if (res.status === 401) { showGate(); throw new Error("Потрібен код доступу"); }
      if (!res.ok) throw new Error("Сервер відповів " + res.status);
      const { sdp } = await res.json();
      await peer.setRemoteDescription({ type: "answer", sdp });
      await ready;
    })();
    try { await connecting; } catch (err) { close(); throw err; } finally { connecting = null; }
  }

  function onEvent(raw, started) {
    let event;
    try { event = JSON.parse(raw); } catch { return; }
    switch (event.type) {
      case "session.started":
        // She hears nothing: the outgoing track is silence. (Muting the input as well kept her from
        // saying the backend's answers at all.)
        setStatus("На зв'язку", "live");
        started();
        break;
      case "session.output_transcript.delta":
        evaSays(event.delta || "");
        setStatus("Говорить", "speak");
        clearTimeout(quietTimer);
        quietTimer = setTimeout(() => { if (isOpen()) setStatus(working ? "Думає…" : "На зв'язку", working ? "think" : "live"); }, 2500);
        touch();
        break;
      case "session.delegation.created":
        working = true;
        setStatus("Думає…", "think");
        touch();
        break;
      case "response.event": {
        // The backend's steps: a search keeps it busy after her «зараз гляну».
        const type = (event.event || {}).type;
        if (type === "response.created") working = true;
        else if (type === "response.completed" || type === "response.failed" || type === "response.cancelled") working = false;
        touch();
        break;
      }
      case "session.closed":
        closed();
        break;
      case "error": {
        const message = (event.error && event.error.message) || "невідома";
        if (!/session is closing/i.test(message)) line("system", "Помилка: " + message);
        break;
      }
      default:
        break;
    }
  }

  // A quiet call is closed (billed per open second); the next message opens another.
  function touch() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      close();
      setStatus("На паузі — напишіть, і Єва відповість", "");
    }, IDLE_CLOSE_MS);
  }

  function close() {
    clearTimeout(idleTimer);
    try { if (channel && channel.readyState === "open") channel.send(JSON.stringify({ type: "session.close" })); } catch { /* closing */ }
    if (pc) pc.close();
    pc = null;
    channel = null;
    ready = null;
  }

  function closed() {
    working = false;
    pc = null;
    channel = null;
    ready = null;
    clearTimeout(idleTimer);
    setStatus("На паузі — напишіть, і Єва відповість", "");
  }

  // The message goes to the backend as the user's own (GPT-Live never delegates a commentary, and
  // answered one itself, made up); the silent note tells her what came in. Same wording as askBackend.
  function ask(text) {
    sendEvent({ type: "session.thinking.append", delegation_id: null, event_id: eventId("think"),
      content: "The user typed in the chat: «" + text + "». It has gone to the backend as their message, and the " +
        "backend handles it the usual way, with its tools (camera, web) — nothing has been looked at or searched " +
        "yet. You, the voice: do not delegate it again; when the backend's answer comes, tell it in your own " +
        "words. Never voice their words or invent what they said." });
    sendEvent({ type: "response.item.create", event_id: eventId("ask"),
      item: { type: "message", role: "user", content: [{ type: "input_text", text }] } });
    sendEvent({ type: "response.create", event_id: eventId("go") });
    setStatus("Думає…", "think");
    touch();
  }

  async function send(e) {
    if (e) e.preventDefault();
    const text = $("text").value.trim();
    if (!text) return;
    if (!BACKEND) { line("system", "Не вказано адресу бекенду."); return; }
    unlockAudio(); // inside the tap
    $("text").value = "";
    fitText();
    line("user", text);
    evaBubble = null;
    try {
      if (!isOpen()) await connect();
      else if (ready) await ready;
    } catch (err) {
      line("system", "Не вдалося підключитись: " + (err.message || err) + ". Спробуйте ще раз.");
      setStatus("Не підключено", "err");
      return;
    }
    // Into the conversation only once the call is open (in the new call's history she would answer it
    // herself), and before it is asked: written after her first words, it cut them off into a line of
    // their own («Зараз д» / «ізнаюся…»).
    await fetch(BACKEND + "/api/conversation/text", { method: "POST", headers: headers(), body: JSON.stringify({ text }) }).catch(() => {});
    ask(text);
  }

  // The box grows with the text, up to a few lines.
  function fitText() {
    const t = $("text");
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 140) + "px";
  }

  // The conversation so far (also what was said on the voice page).
  async function loadConversation() {
    try {
      const res = await fetch(BACKEND + "/api/conversation", { headers: headers() });
      if (res.status === 401) { showGate(); return; }
      if (!res.ok) return;
      const data = await res.json();
      if (!data.turns.length || $("messages").querySelector(".msg")) return;
      $("messages").replaceChildren();
      data.turns.forEach((t) => line(t.role, t.text));
    } catch { /* asleep: the first message wakes it */ }
  }

  // A sleeping free-tier backend takes up to a minute to start.
  async function wake() {
    for (let i = 0; i < 30; i++) {
      try {
        const res = await fetch(BACKEND + "/healthz");
        if (res.ok) return true;
      } catch { /* waking */ }
      setStatus("Сервер прокидається — до хвилини…", "busy");
      await new Promise((r) => setTimeout(r, 3000));
    }
    return false;
  }

  $("composer").addEventListener("submit", send);
  $("text").addEventListener("input", fitText);
  $("text").addEventListener("keydown", (e) => {
    // Enter sends, Shift+Enter is a new line; not while an IME is composing a word.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) send(e);
  });
  $("sound").checked = store.get("va-chat-sound") !== "0";
  $("voiceOut").muted = !$("sound").checked;
  $("sound").addEventListener("change", () => {
    $("voiceOut").muted = !$("sound").checked;
    store.set("va-chat-sound", $("sound").checked ? "1" : "0");
  });
  $("saveCode").addEventListener("click", () => {
    accessCode = $("code").value.trim();
    store.set("va-access-code", accessCode);
    $("gate").hidden = true;
    loadConversation();
  });
  window.addEventListener("pagehide", close); // an open call keeps billing
  if (BACKEND) {
    wake().then((ok) => {
      setStatus(ok ? "Напишіть — Єва відповість голосом" : "Сервер не відповідає", ok ? "" : "err");
      if (ok) loadConversation();
    });
  }
})();
