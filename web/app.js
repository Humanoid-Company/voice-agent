"use strict";

// Backend: the same origin by default; for a separately hosted page open it with ?backend=https://host
const params = new URLSearchParams(location.search);
const BACKEND = (params.get("backend") || location.origin).replace(/\/$/, "");
const WS_URL = BACKEND.replace(/^http/, "ws") + "/ws";

const $ = (id) => document.getElementById(id);
const el = {
  dot: $("dot"), status: $("status"), problem: $("problem"), provider: $("provider"), voice: $("voice"),
  codeRow: $("codeRow"), code: $("code"), talk: $("talk"), interrupt: $("interrupt"), log: $("log"), clear: $("clear"),
};

const STATES = {
  idle: ["", "Не підключено"],
  connecting: ["think", "Підключаюсь…"],
  listening: ["listen", "Слухаю"],
  thinking: ["think", "Думаю…"],
  speaking: ["speak", "Говорю"],
  error: ["err", "Помилка"],
};

let providers = [];
let ws = null;
let recognition = null;
let playCtx = null;
let ttsRate = 24000;
let live = false; // a call is active
let ready = false; // the server has connected to TTS: only then do we send what we heard

// ---------------------------------------------------------------- settings

function save(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

function fillVoices() {
  const p = providers.find((x) => x.id === el.provider.value);
  el.voice.innerHTML = "";
  for (const v of (p ? p.voices : [])) el.voice.add(new Option(v, v));
  const saved = load("voice:" + el.provider.value);
  if (saved && p && p.voices.includes(saved)) el.voice.value = saved;
}

async function init() {
  setState("idle");
  if (!SpeechRec) showProblem("Цей браузер не вміє розпізнавати мову. Відкрийте сторінку в Chrome, Edge або Safari.");
  try {
    const cfg = await (await fetch(BACKEND + "/api/config")).json();
    providers = cfg.providers;
    el.codeRow.hidden = !cfg.needs_code;
    if (cfg.missing.length) showProblem("На сервері не задано: " + cfg.missing.join(", "));
    el.provider.innerHTML = "";
    for (const p of providers) el.provider.add(new Option(p.label, p.id));
    const saved = load("provider");
    if (saved && providers.some((p) => p.id === saved)) el.provider.value = saved;
    fillVoices();
    const ok = providers.length > 0 && !!SpeechRec;
    el.provider.disabled = el.voice.disabled = !ok;
    el.talk.disabled = !ok;
    if (!ok) el.provider.innerHTML = "<option>Немає ключів</option>";
  } catch (e) {
    showProblem("Бекенд недоступний: " + BACKEND);
  }
}

function showProblem(text) { el.problem.textContent = text; el.problem.hidden = !text; }

function setState(name) {
  const [cls, text] = STATES[name] || STATES.idle;
  el.dot.className = "dot " + cls;
  el.status.textContent = text;
  el.interrupt.hidden = !(live && (name === "thinking" || name === "speaking"));
}

// ---------------------------------------------------------------- log

let userBubble = null;
let botBubble = null;
let lastBot = null;

function bubble(role, text) {
  el.log.querySelector(".empty")?.remove();
  const d = document.createElement("div");
  d.className = "msg " + role;
  d.textContent = text;
  el.log.appendChild(d);
  el.log.scrollTop = el.log.scrollHeight;
  return d;
}

function onUser(text, final) {
  if (!userBubble) userBubble = bubble("user partial", text);
  userBubble.textContent = text;
  if (final) { userBubble.classList.remove("partial"); userBubble = null; }
  el.log.scrollTop = el.log.scrollHeight;
}

function onUserDrop() { userBubble?.remove(); userBubble = null; }

function onAssistantDelta(text) {
  if (!botBubble) botBubble = lastBot = bubble("assistant", "");
  botBubble.textContent += text;
  el.log.scrollTop = el.log.scrollHeight;
}

// ---------------------------------------------------------------- playback

let sources = new Set();
let nextTime = 0;
let leftover = null; // an odd trailing byte of the previous chunk
let turnEnded = false;

function playChunk(data) {
  let bytes = new Uint8Array(data);
  if (leftover) {
    const joined = new Uint8Array(leftover.length + bytes.length);
    joined.set(leftover);
    joined.set(bytes, leftover.length);
    bytes = joined;
    leftover = null;
  }
  const usable = bytes.length & ~1;
  if (usable < bytes.length) leftover = bytes.slice(usable);
  if (!usable) return;
  const i16 = new Int16Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + usable));
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
  const buf = playCtx.createBuffer(1, f32.length, ttsRate);
  buf.copyToChannel(f32, 0);
  const src = playCtx.createBufferSource();
  src.buffer = buf;
  src.connect(playCtx.destination);
  // A small head start for the first chunk of a turn, so a late next chunk does not click.
  if (sources.size === 0) nextTime = Math.max(nextTime, playCtx.currentTime + 0.1);
  const at = Math.max(playCtx.currentTime + 0.02, nextTime);
  src.start(at);
  nextTime = at + buf.duration;
  sources.add(src);
  src.onended = () => { sources.delete(src); maybePlayed(); };
}

function maybePlayed() {
  if (turnEnded && sources.size === 0) {
    turnEnded = false;
    send({ type: "played" });
  }
}

function stopPlayback() {
  for (const s of sources) { s.onended = null; try { s.stop(); } catch (e) { /* already stopped */ } }
  sources.clear();
  nextTime = 0;
  leftover = null;
  turnEnded = false;
}

// ---------------------------------------------------------------- listening (browser speech recognition)

const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
const REC_LANG = params.get("lang") || "uk-UA";

// Chrome, Edge and Safari recognize in the cloud (Google / Apple) and stream interim text. It ends by
// itself after a while or a long silence, so it is restarted for as long as the call lasts.
function startRecognition() {
  recognition = new SpeechRec();
  recognition.lang = REC_LANG;
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) heard(r[0].transcript, true);
      else interim += r[0].transcript;
    }
    if (interim) heard(interim, false);
  };
  recognition.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      stop("Немає доступу до мікрофона або розпізнавання мови.");
    }
  };
  recognition.onend = () => {
    if (!live) return;
    setTimeout(() => { try { recognition.start(); } catch (err) { /* already started */ } }, 200);
  };
  try { recognition.start(); } catch (err) { /* already started */ }
}

function stopRecognition() {
  if (!recognition) return;
  recognition.onend = recognition.onerror = recognition.onresult = null;
  try { recognition.abort(); } catch (err) { /* not started */ }
  recognition = null;
}

function heard(text, final) {
  text = text.trim();
  if (text && ready) send({ type: "heard", text, final });
}

// ---------------------------------------------------------------- socket

function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

function onMessage(ev) {
  if (typeof ev.data !== "string") { playChunk(ev.data); return; }
  const m = JSON.parse(ev.data);
  switch (m.type) {
    case "ready": ttsRate = m.sample_rate; ready = true; break;
    case "state": setState(m.state); break;
    case "user": onUser(m.text, m.final); break;
    case "user_drop": onUserDrop(); break;
    case "assistant_delta": onAssistantDelta(m.text); break;
    case "turn_end": botBubble = null; turnEnded = true; maybePlayed(); break;
    case "clear":
      stopPlayback();
      (botBubble || lastBot)?.classList.add("cut");
      botBubble = null;
      break;
    case "error": showProblem(m.message); bubble("system", m.message); break;
  }
}

async function start() {
  showProblem("");
  setState("connecting");
  el.talk.disabled = true;
  try {
    // Created in the click handler: browsers only allow audio after a user gesture.
    playCtx = new AudioContext();
    // The browser asks for the microphone itself and recognizes speech with Google, for free.
    // A throwaway getUserMedia first, so a denied permission is reported clearly.
    const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
    probe.getTracks().forEach((t) => t.stop());

    const code = el.code.value ? "?code=" + encodeURIComponent(el.code.value) : "";
    ws = new WebSocket(WS_URL + code);
    ws.binaryType = "arraybuffer";
    ws.onmessage = onMessage;
    ws.onclose = () => stop(live ? (el.problem.hidden ? "Зв'язок розірвано." : el.problem.textContent) : "");
    ws.onopen = () => send({ type: "start", provider: el.provider.value, voice: el.voice.value });
    live = true;
    startRecognition();
    save("provider", el.provider.value);
    save("voice:" + el.provider.value, el.voice.value);
    el.talk.textContent = "Завершити розмову";
    el.talk.classList.add("danger");
    el.talk.disabled = false;
    el.provider.disabled = el.voice.disabled = true;
  } catch (e) {
    stop(e.name === "NotAllowedError" ? "Немає доступу до мікрофона." : "Не вдалося почати: " + e.message);
  }
}

function stop(problem) {
  live = false;
  ready = false;
  if (ws) { ws.onclose = null; try { ws.close(); } catch (e) { /* closed */ } ws = null; }
  stopRecognition();
  stopPlayback();
  playCtx?.close(); playCtx = null;
  userBubble = botBubble = null;
  el.talk.textContent = "Почати розмову";
  el.talk.classList.remove("danger");
  const can = providers.length > 0 && !!SpeechRec;
  el.talk.disabled = !can;
  el.provider.disabled = el.voice.disabled = !can;
  showProblem(problem || "");
  setState("idle");
}

el.talk.onclick = () => (live ? stop("") : start());
el.interrupt.onclick = () => send({ type: "interrupt" });
el.provider.onchange = fillVoices;
el.clear.onclick = () => {
  el.log.innerHTML = '<p class="muted empty">Тут з\'являтиметься текст розмови.</p>';
  userBubble = botBubble = lastBot = null;
};

init();
