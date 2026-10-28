"use strict";

// Backend: the same origin by default; for a separately hosted page open it with ?backend=https://host
const params = new URLSearchParams(location.search);
const BACKEND = (params.get("backend") || (window.APP_CONFIG && window.APP_CONFIG.BACKEND_URL) || location.origin).replace(/\/$/, "");
const WS_URL = BACKEND.replace(/^http/, "ws") + "/ws";

const $ = (id) => document.getElementById(id);
const el = {
  dot: $("dot"), status: $("status"), problem: $("problem"), provider: $("provider"), voice: $("voice"),
  recog: $("recog"), micMute: $("micMute"), talk: $("talk"), interrupt: $("interrupt"), log: $("log"), newChat: $("newChat"),
  pause: $("pause"), bgListen: $("bgListen"), volume: $("volume"), volumeValue: $("volumeValue"), meter: $("meter"),
  assistantName: $("assistantName"), micSelect: $("micSelect"), speakerSelect: $("speakerSelect"),
  voiceApply: $("voiceApply"), voicePreview: $("voicePreview"), greet: $("greet"), character: $("character"), emotion: $("emotion"), characterInfo: $("characterInfo"),
  tuneVoice: $("tuneVoice"), tuningInfo: $("tuningInfo"), engine: $("engine"),
  hearing: $("hearing"), levelFill: $("levelFill"), levelNeed: $("levelNeed"), hearingStatus: $("hearingStatus"),
  hearLog: $("hearLog"), historyList: $("historyList"), memorySel: $("memorySel"), historyView: $("historyView"),
  historyBack: $("historyBack"), historyCopy: $("historyCopy"), historyStats: $("historyStats"), timeline: $("timeline"), bargeSens: $("bargeSens"), devicesInfo: $("devicesInfo"), devicesAllow: $("devicesAllow"), copyChat: $("copyChat"), clearChat: $("clearChat"), about: $("about"), aboutCount: $("aboutCount"),
  usageTable: $("usageTable"), openHistory: $("openHistory"), openUsage: $("openUsage"),
  historyDialog: $("historyDialog"), usageDialog: $("usageDialog"),
};

const STATES = {
  idle: ["", "Не підключено"],
  connecting: ["think", "Підключаюсь…"],
  listening: ["listen", "Слухаю"],
  thinking: ["think", "Думаю…"],
  speaking: ["speak", "Говорю"],
  paused: ["pause", "Пауза"],
  error: ["err", "Помилка"],
};

let providers = [];
let ws = null;
let recognition = null;
let playCtx = null;
let playGain = null; // all assistant audio goes through it: barge-in ducks and mutes here
let gate = null; // echo-aware noise gate (gate.js), optional
let gateTrack = null; // the gate's output: the microphone, shut while she is audible
let pcmTap = null; // pcm.js: the gated microphone as PCM16 24 kHz, for the Realtime engine
let realtime = false; // this call's ears are Realtime (the server said so in "ready")
let micStream = null;
let curState = "idle";
let micMuted = false; // the user turned the microphone off: nothing is heard until they turn it on
let ttsRate = 24000;
let paused = false; // the user (or silence) paused the call: nobody is addressed
let lastActivity = Date.now(); // the last time anybody spoke or was spoken to: 90 s of nothing pauses the call
let recLang = params.get("lang") || "uk-UA"; // speech recognition language: the model can change it
let micSource = null; // the microphone's node in the gate: replaced when another microphone is picked
let activeVoice = { provider: "", voice: "" }; // the voice that is speaking now (what «Застосувати» compares with)
let live = false; // a call is active
let ready = false; // the server has connected to TTS: only then do we send what we heard

// ---------------------------------------------------------------- settings

function save(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

// Any voice can be chosen for any character. The voices that suit the current one are marked with ★ and come first.
const characterVoices = {}; // character id -> [{ provider, voice, label }] from the server

function recommendedVoices(provider) { // in the character's own order: the first is the best fit
  return (characterVoices[characterId()] || []).filter((v) => v.provider === provider).map((v) => v.voice);
}

function fillVoices() {
  const p = providers.find((x) => x.id === el.provider.value);
  const voices = p ? p.voices : [];
  const recIds = recommendedVoices(el.provider.value);
  const rec = recIds.map((id) => voices.find((v) => v.id === id)).filter(Boolean);
  el.voice.innerHTML = "";
  for (const v of rec) el.voice.add(new Option("★ " + v.label, v.id));
  for (const v of voices.filter((x) => !recIds.includes(x.id))) el.voice.add(new Option(v.label, v.id));
  const saved = load("voice:" + el.provider.value);
  if (saved && p && p.voices.some((v) => v.id === saved)) el.voice.value = saved;
  updateButtons(); // «Прослухати» needs a picked voice
}

// A sleeping free-tier backend takes up to a minute to start: wait for it instead of failing.
async function wakeBackend() {
  const deadline = Date.now() + 90 * 1000;
  let waiting = false;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(BACKEND + "/api/config");
      if (res.ok) { if (waiting) setState("idle"); return await res.json(); }
    } catch (e) { /* still waking up */ }
    if (!waiting) {
      waiting = true;
      el.dot.className = "dot think";
      el.status.textContent = "Сервер прокидається — до хвилини…";
    }
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
  return null;
}

async function init() {
  setState("idle");
  if (!SpeechRec) showProblem("Цей браузер не вміє розпізнавати мову. Відкрийте сторінку в Chrome, Edge або Safari.");
  try {
    const cfg = await wakeBackend();
    if (!cfg) throw new Error("backend is not answering");
    // While the page is open and visible, keep the free-tier backend awake.
    setInterval(() => {
      if (document.visibilityState === "visible") fetch(BACKEND + "/healthz").catch(() => {});
    }, 10 * 60 * 1000);
    providers = cfg.providers;
    if (cfg.missing.length) showProblem("На сервері не задано: " + cfg.missing.join(", "));
    el.provider.innerHTML = "";
    for (const p of providers) el.provider.add(new Option(p.label, p.id));
    const saved = load("provider");
    if (saved && providers.some((p) => p.id === saved)) el.provider.value = saved;
    fillVoices();
    buildCharacterMenu(cfg.characters);
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
  curState = name;
  const [cls, text] = STATES[name] || STATES.idle;
  el.dot.className = "dot " + cls;
  el.status.textContent = text;
  if (name === "thinking" || name === "speaking") touch();
  updateButtons();
}

function touch() { lastActivity = Date.now(); }

// What the buttons show for the call's state.
function updateButtons() {
  el.pause.hidden = el.micMute.hidden = !live;
  el.pause.textContent = paused ? "Продовжити" : "Пауза";
  el.pause.classList.toggle("primary", paused);
  el.interrupt.hidden = !(live && !paused && (curState === "thinking" || curState === "speaking"));
  el.voicePreview.disabled = live || !el.voice.value; // a sample would talk over the agent, and the agent would hear it
  updateVoiceApply();
}

// ---------------------------------------------------------------- voice sample («▶ Прослухати»)

let preview = null;

function stopPreview() {
  if (preview) {
    preview.onplaying = preview.onended = preview.onerror = null;
    preview.pause();
    preview.removeAttribute("src"); // unloaded, not only paused
    preview.load();
    preview = null;
  }
  el.voicePreview.textContent = "▶ Прослухати";
}

function togglePreview() {
  if (preview) { stopPreview(); return; }
  if (live || !el.voice.value) return;
  const query = new URLSearchParams({ provider: el.provider.value, voice: el.voice.value, language: recLang.slice(0, 2) });
  // The server makes the sample once per voice (the first listen takes a moment) and the browser keeps it.
  const audio = new Audio(BACKEND + "/api/preview?" + query);
  preview = audio;
  el.voicePreview.textContent = "… Готую зразок";
  audio.onplaying = () => { el.voicePreview.textContent = "■ Стоп"; };
  audio.onended = stopPreview;
  audio.onerror = () => { stopPreview(); showProblem("Зразок цього голосу зараз недоступний."); };
  const sink = el.speakerSelect.value;
  if (sink && audio.setSinkId) audio.setSinkId(sink).catch(() => { /* the default speaker */ });
  showProblem("");
  audio.play().catch(stopPreview);
}

el.voicePreview.onclick = togglePreview;

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
  if (final) { userBubble.classList.remove("partial"); userBubble = null; logEvent("user", text); }
  el.log.scrollTop = el.log.scrollHeight;
}

function onUserDrop() { userBubble?.remove(); userBubble = null; }

function onAssistantDelta(text) {
  barge.recent = (barge.recent + text).slice(-300);
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
  src.connect(playGain);
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

// Chrome, Edge and Safari recognize in the cloud (Google / Apple) and stream interim text. It ends by
// itself after a while or a long silence, so it is restarted for as long as the call lasts.
// Chrome ends an utterance ~1-2 s after the last word. We do not wait for it, but the end of a phrase is not «the text
// stopped changing»: the transcript lags behind the voice, so a stalled text while the user is still speaking cut
// phrases in two and she answered half a sentence. The phrase ends when the text has settled AND the user's voice (the
// gate's `active` flag) has been silent for ENDPOINT_MS (?endpoint=1000 waits longer, for people who pause a lot).
// Without the gate there is no voice signal, so the text alone must stay unchanged for longer; and a phrase is
// never held back more than ENDPOINT_MAX_MS (a noisy room keeps the gate «active»). The recognizer is then
// restarted, which drops the late «final» of the same words.
const ENDPOINT_MS = Number(params.get("endpoint")) || 700;
const ENDPOINT_TEXT_MS = 400; // the text must not have changed for this long
const ENDPOINT_NO_GATE_MS = 1100;
const ENDPOINT_MAX_MS = 2500;
let endpointTimer = null;
let interimText = "";
let interimChangedAt = 0;
let lastActiveAt = 0;

function showRecog(text) { el.recog.textContent = text; el.recog.hidden = !text; }

function commitInterim() {
  const text = interimText;
  interimText = "";
  if (!text || !live) return;
  heard(text, true);
  try { recognition.abort(); } catch (err) { /* not started */ } // onend restarts it
}

// Is the phrase finished? Called again and again until it is (or the text is gone).
function checkEndpoint() {
  if (!interimText || !live) return;
  const now = Date.now();
  const settled = now - interimChangedAt;
  const quiet = gate ? now - lastActiveAt : Infinity;
  const done = gate
    ? settled >= ENDPOINT_TEXT_MS && quiet >= ENDPOINT_MS
    : settled >= ENDPOINT_NO_GATE_MS;
  if (done || settled >= ENDPOINT_MAX_MS) commitInterim();
  else endpointTimer = setTimeout(checkEndpoint, 100);
}

function startRecognition() {
  recognition = new SpeechRec();
  recognition.lang = recLang;
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onstart = () => showRecog(recogMode);
  recognition.onresult = (e) => {
    lastResultAt = Date.now();
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) {
        clearTimeout(endpointTimer);
        interimText = "";
        showRecog("🎤 Почув: «" + r[0].transcript.trim().slice(-70) + "»");
        heard(r[0].transcript, true);
      } else {
        interim += r[0].transcript;
      }
    }
    if (interim) {
      showRecog("🎤 Почув: «" + interim.trim().slice(-70) + "»");
      heard(interim, false);
      if (interim !== interimText) interimChangedAt = Date.now();
      interimText = interim;
      clearTimeout(endpointTimer);
      endpointTimer = setTimeout(checkEndpoint, 100);
    }
  };
  recognition.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      stop("Немає доступу до мікрофона або розпізнавання мови.");
    } else if (e.error !== "no-speech" && e.error !== "aborted") {
      showRecog("🎤 Помилка розпізнавання: " + e.error); // «network»: no route to Google's recognizer
    }
  };
  recognition.onend = () => {
    if (!live || micMuted) return;
    setTimeout(() => beginRecognizer(), 200);
  };
  beginRecognizer();
}

// Chrome 139+ takes the audio to recognize as a track: ours is the gate's output, shut while she talks, so the
// recognizer cannot hear her. Older browsers ignore the argument and listen to the microphone directly, so
// support is judged by a feature that shipped with it (?gated=0 / ?gated=1 overrides).
const TRACK_SUPPORT = Boolean(SpeechRec) && "processLocally" in SpeechRec.prototype;
let useGate = params.get("gated") ? params.get("gated") === "1" : TRACK_SUPPORT;
let recogMode = "";
let lastResultAt = 0;

function beginRecognizer() {
  if (!recognition) return;
  if (useGate && gateTrack) {
    try {
      recognition.start(gateTrack);
      recogMode = "🎤 Слухаю через шлюз: агент не чує себе";
      showRecog(recogMode);
      return;
    } catch (err) {
      if (err && err.name === "InvalidStateError") return; // already started
      useGate = false; // this browser does not take a track: listen directly
    }
  }
  recogMode = "🎤 Слухаю напряму: на колонках можливе ехо агента";
  try { recognition.start(); } catch (err) { /* already started */ }
  showRecog(recogMode);
}

// If the gate hears the user (near voice) three times and the recognizer, fed with its sound, returns no text either
// time, the gated path does not work in this browser: go back to the microphone directly.
let gateMisses = 0;
function checkGateWorks(since) {
  if (!useGate || !live || lastResultAt >= since) { gateMisses = 0; return; }
  gateMisses += 1;
  if (gateMisses < 3) return;
  useGate = false;
  showRecog("🎤 Шлюз не працює в цьому браузері: слухаю напряму");
  try { recognition.abort(); } catch (err) { /* not started */ } // onend restarts it, now directly
}

function stopRecognition() {
  clearTimeout(endpointTimer);
  interimText = "";
  showRecog("");
  if (!recognition) return;
  recognition.onend = recognition.onerror = recognition.onresult = recognition.onstart = null;
  try { recognition.abort(); } catch (err) { /* not started */ }
  recognition = null;
}

function heard(text, final) {
  text = text.trim();
  if (!text || !ready || micMuted) return;
  if (paused && !final) return; // on pause only whole phrases are kept, the server does it
  if (!paused && !passesGate(text, final)) {
    if (final) {
      hearEvent("✖", "розпізнано «" + text + "», але не передано: це не ваш голос або він тихіший за ехо агента", true);
      logEvent("dropped", text, { why: "quiet" });
    }
    return;
  }
  touch();
  barge.last = text;
  if (final) hearEvent("✔", "розпізнано й передано: «" + text + "»");
  if (final && paused) logEvent("background", text);
  send({ type: "heard", text, final });
}

// ---------------------------------------------------------------- not hearing herself, barge-in
//
// The browser's speech recognition hears the speakers too, and we cannot filter its audio. So the gate
// (gate.js) judges the *sound* and this code decides which *words* count:
//  - while the assistant is busy, text reaches the server only when the person at the mic is speaking
//    (loudly enough over her echo), or it is a stop word she did not say herself;
//  - two stages, like Eva: their voice over hers ducks her volume, a stop (or real words, judged by the
//    server) cuts her off, a short sound just restores the volume.
// Without AudioWorklet there is no gate: everything is sent and the server's text checks are the only guard.

// The single words that mean «stop» (the server knows more phrases and judges the whole utterance).
const STOP_WORDS = new Set(["стоп", "стій", "зачекай", "почекай", "чекай", "погоди", "стривай", "тихо", "досить", "перестань", "замовкни", "помовч", "stop", "wait"]);
const DUCK_LEVEL = 0.5;
const NEAR_WINDOW_MS = 1200; // transcripts lag behind the sound
const NEAR_WINDOW_FINAL_MS = 2500; // a final result lags more, and losing it loses the whole turn
const AFTER_AUDIO_MS = 1500; // her echo tail

const barge = {
  audible: false, audibleEndedAt: 0, speaking: false, speechEndedAt: 0,
  stage: "idle", timers: [], recent: "", last: "",
};

const words = (text) => text.toLowerCase().replace(/[ʼ’`]/g, "'").split(/[^а-яіїєґa-z'-]+/i).filter(Boolean);

// level: barge-in ducking (1 / 0.5) times the user's volume slider.
function setPlayVolume(level) {
  if (playGain) playGain.gain.setTargetAtTime(level * volume, playCtx.currentTime, 0.02); // a ramp, not a click
}

function bargeReset() {
  barge.timers.forEach(clearTimeout);
  barge.timers = [];
  barge.stage = "idle";
  barge.last = "";
  setPlayVolume(1);
}

// Words of the user: what she herself said lately is her echo, stop words included.
function ownWords(text) {
  const echo = new Set(words(barge.recent));
  return words(text).filter((w) => !echo.has(w));
}

function guarded() {
  if (!gate) return false;
  return barge.audible || curState === "thinking" || curState === "speaking"
    || Date.now() - barge.audibleEndedAt < AFTER_AUDIO_MS;
}

function passesGate(text, final) {
  if (!guarded()) return true;
  if (barge.speaking || Date.now() - barge.speechEndedAt < (final ? NEAR_WINDOW_FINAL_MS : NEAR_WINDOW_MS)) return true;
  return ownWords(text).some((w) => STOP_WORDS.has(w)); // a quiet «стоп» over loud speakers
}

function onGate(sig) {
  if ("levels" in sig) { onLevels(sig.levels); return; }
  if (paused) return;
  if (sig.speaking) touch();
  if ("audible" in sig) {
    if (barge.audible && !sig.audible) barge.audibleEndedAt = Date.now();
    barge.audible = sig.audible;
    return;
  }
  barge.speaking = sig.speaking;
  if (sig.speaking) {
    const since = Date.now();
    setTimeout(() => checkGateWorks(since), 4000);
  } else {
    barge.speechEndedAt = Date.now();
  }
}

// Interrupting by voice, judged on a window rather than a stopwatch. The gate reports ten times a second whether the
// user's voice is on (its `speaking` flag: 120 ms of voice over the bar to start, gaps up to 250 ms bridged, so a
// burst of her echo is not enough and the dips between words do not count against the user). Of the last second,
// enough samples duck her (their voice has reached her) and a few more stop her. The server still decides about the
// words (stop words, takeover) from the transcript, and refuses a stop by sound when the words were only «угу».
const VOICE_WINDOW = 10; // samples: one second
const DUCK_AT = 3; // 300 ms of voice in the last second duck her
const STOP_AT = 5; // 500 ms stop her
const STOP_RETRY_MS = 1500; // after a stop the server refused (a listener's «угу»), try again no sooner than this
const voicedWin = [];
let duckedAt = 0;
let stopSentAt = 0;

function bargeFromLevels(l) {
  const herPlaying = l.audible && sources.size > 0; // not her fading tail after a stop, not a paused page
  if (!herPlaying || paused) { voicedWin.length = 0; return; }
  voicedWin.push(l.speaking ? 1 : 0);
  if (voicedWin.length > VOICE_WINDOW) voicedWin.shift();
  const voiced = voicedWin.reduce((a, b) => a + b, 0);
  if (barge.stage === "idle") {
    if (voiced >= DUCK_AT) { barge.stage = "ducked"; duckedAt = Date.now(); setPlayVolume(DUCK_LEVEL); }
    return;
  }
  if (voiced >= STOP_AT && Date.now() - stopSentAt > STOP_RETRY_MS) {
    stopSentAt = Date.now();
    send({ type: "interrupt", by: "sound", text: barge.last });
  } else if (voicedWin.length >= STOP_AT && voicedWin.slice(-STOP_AT).every((v) => v === 0) && Date.now() - duckedAt > 600) {
    // A short sound: a cough, «угу» or her echo. She gets her volume back; the record keeps the attempt.
    barge.stage = "idle";
    setPlayVolume(1);
    logEvent("attempt", "");
  }
}

async function setupGate() {
  if (!playCtx.audioWorklet || typeof AudioWorkletNode === "undefined") return;
  try {
    await playCtx.audioWorklet.addModule("gate.js");
    const node = new AudioWorkletNode(playCtx, "echo-gate", {
      numberOfInputs: 2, numberOfOutputs: 1, outputChannelCount: [1],
      processorOptions: { near: Number(params.get("near")) || 0.06, sens: bargeSens() }, // ?near=0.04 more sensitive, 0.09 less
    });
    node.port.onmessage = (e) => onGate(e.data);
    micSource = playCtx.createMediaStreamSource(micStream);
    micSource.connect(node, 0, 0);
    playGain.connect(node, 0, 1);
    // The gate's sound is never played: it only goes to the speech recognizer, as a track.
    const dest = playCtx.createMediaStreamDestination();
    node.connect(dest);
    gateTrack = dest.stream.getAudioTracks()[0];
    gate = node;
  } catch (e) {
    gate = null; // no gate: the server's checks alone
    gateTrack = null;
  }
}

// The Realtime engine hears the same sound as the recognizer: the gate's output (the microphone itself without a gate).
async function setupPcmTap() {
  pcmTap = null;
  if (el.engine.value !== "realtime" || !playCtx.audioWorklet) return;
  await playCtx.audioWorklet.addModule("pcm.js");
  const node = new AudioWorkletNode(playCtx, "pcm-tap", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
  node.port.onmessage = (e) => {
    if (realtime && !micMuted && !paused && ws && ws.readyState === WebSocket.OPEN) ws.send(e.data);
  };
  (gate || (micSource = micSource || playCtx.createMediaStreamSource(micStream))).connect(node);
  const mute = playCtx.createGain(); // a node nobody listens to is not run by some browsers: it goes nowhere audible
  mute.gain.value = 0;
  node.connect(mute).connect(playCtx.destination);
  pcmTap = node;
}

// ---------------------------------------------------------------- socket

function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

function onMessage(ev) {
  if (typeof ev.data !== "string") { playChunk(ev.data); return; }
  const m = JSON.parse(ev.data);
  switch (m.type) {
    case "ready":
      ttsRate = m.sample_rate;
      ready = true;
      realtime = m.engine === "realtime";
      if (m.name) el.assistantName.textContent = m.name;
      if (m.character) showStyle(m.character, m.emotion);
      if (m.recognition) setRecLang(m.recognition);
      setState("listening");
      break;
    case "state": setState(m.state); if (m.state === "listening") bargeReset(); break;
    case "voice": onVoice(m); break;
    case "language": setRecLang(m.recognition); break;
    case "persona":
      el.assistantName.textContent = m.name;
      showStyle(m.character, m.emotion, m.description);
      break;
    case "usage": onUsage(m); break;
    case "tuning": showTuning(m); break;
    case "overheard": showRecog("🎤 На паузі, запам'ятовую фоном: " + m.count + " реплік"); break;
    case "end": stop(""); bubble("system", "Розмову завершено."); break;
    case "user": onUser(m.text, m.final); break;
    case "user_drop":
      onUserDrop();
      hearEvent("✖", "агент не відповів на «" + (m.text || "…") + "»: " + (DROP_REASONS[m.reason] || "відкинуто"), true);
      // «стоп» is already in the record as the interruption it was
      if (m.text && m.reason !== "stop") logEvent("dropped", m.text, { why: m.reason });
      break;
    case "record": if (m.role === "assistant") logEvent("assistant", m.text, { cut: /…$/.test(m.text || "") }); break;
    case "assistant_delta": onAssistantDelta(m.text); break;
    case "turn_end": botBubble = null; turnEnded = true; maybePlayed(); break;
    case "clear":
      stopPlayback();
      bargeReset();
      if (["stop", "takeover", "button", "sound"].includes(m.reason)) logEvent("interrupt", m.text, { by: m.reason });
      (botBubble || lastBot)?.classList.add("cut");
      botBubble = null;
      break;
    case "error": showProblem(m.message); bubble("system", m.message); resetVoiceApply(); break;
  }
}

async function start() {
  showProblem("");
  setState("connecting");
  el.talk.disabled = true;
  try {
    stopPreview(); // a sample left playing would talk over the agent
    // Created in the click handler: browsers only allow audio after a user gesture.
    playCtx = new AudioContext();
    playGain = playCtx.createGain();
    playGain.connect(playCtx.destination);
    setPlayVolume(1);
    applySink();
    // Speech recognition opens the microphone itself (Google, free). This second capture, with the browser's
    // echo canceller on, only measures levels for the gate, and reports a denied permission clearly.
    micStream = await getMic();
    refreshDevices(); // the names are known now
    await setupGate();
    await setupPcmTap();

    ws = new WebSocket(WS_URL);
    ws.binaryType = "arraybuffer";
    ws.onmessage = onMessage;
    ws.onclose = () => stop(live ? (el.problem.hidden ? "Зв'язок розірвано." : el.problem.textContent) : "");
    ws.onopen = () => send({ type: "start", provider: el.provider.value, voice: el.voice.value, language: recLang.slice(0, 2), history: memoryForServer(),
      character: characterId(), emotion: el.emotion.value, tune: el.tuneVoice.checked, greet: el.greet.checked, about: el.about.value,
      engine: pcmTap ? "realtime" : "browser" });
    live = true;
    paused = false;
    activeVoice = { provider: el.provider.value, voice: el.voice.value };
    renderHearing();
    usageLast = { seconds: 0, cost: 0 };
    usageNow = { seconds: 0, cost: 0, at: Date.now(), llm_in: 0, llm_out: 0, tts_chars: 0, searches: 0 };
    touch();
    startRecognition();
    save("provider", el.provider.value);
    save("voice:" + el.provider.value, el.voice.value);
    el.talk.textContent = "Завершити розмову";
    el.talk.classList.add("danger");
    el.talk.disabled = false;
    updateButtons();
  } catch (e) {
    stop(e.name === "NotAllowedError" ? "Немає доступу до мікрофона." : "Не вдалося почати: " + e.message);
  }
}

function stop(problem) {
  if (live) finishUsage(); // the seconds since the last report still count
  live = false;
  ready = false;
  paused = false;
  if (ws) { ws.onclose = null; try { ws.close(); } catch (e) { /* closed */ } ws = null; }
  stopRecognition();
  stopPlayback();
  bargeReset();
  micStream?.getTracks().forEach((t) => t.stop());
  micStream = null;
  micSource = null;
  gate = null;
  gateTrack = null;
  pcmTap = null;
  realtime = false;
  playGain = null;
  playCtx?.close(); playCtx = null;
  userBubble = botBubble = null;
  setMicMuted(false);
  el.meter.hidden = true;
  el.hearing.hidden = true;
  deadSince = 0;
  updateButtons();
  el.talk.textContent = "Почати розмову";
  el.talk.classList.remove("danger");
  const can = providers.length > 0 && !!SpeechRec;
  el.talk.disabled = !can;
  el.provider.disabled = el.voice.disabled = !can;
  showProblem(problem || "");
  setState("idle");
}

el.talk.onclick = () => (live ? stop("") : start());
el.interrupt.onclick = () => send({ type: "interrupt", by: "button" });

// Mic off: the gate and the recognizer get silence (the track is disabled, the recognizer is stopped), so the
// agent hears nothing, her own voice included; she can still finish what she is saying. The conversation stays.
function setMicMuted(muted) {
  if (muted === micMuted) return;
  micMuted = muted;
  micStream?.getAudioTracks().forEach((t) => { t.enabled = !muted; });
  el.micMute.textContent = muted ? "Увімкнути мікрофон" : "Вимкнути мікрофон";
  el.micMute.setAttribute("aria-pressed", String(muted));
  renderHearing();
  el.micMute.classList.toggle("primary", muted);
  if (!live) return;
  if (muted) {
    stopRecognition();
    onUserDrop(); // an unfinished phrase is not sent
    showRecog("🎤 Мікрофон вимкнено");
  } else {
    startRecognition();
  }
}
el.micMute.onclick = () => setMicMuted(!micMuted);
function resetLog() {
  el.log.innerHTML = '<p class="muted empty">Тут з\'являтиметься текст розмови.</p>';
  userBubble = botBubble = lastBot = null;
}

// ---------------------------------------------------------------- the conversation record and the agent's memory

// Everything is recorded, the agent remembers little. A conversation keeps a full timeline: what the user said and
// was answered, what she said, how the user tried to interrupt (and what did not work), pauses, what was said on
// pause. The agent is given only the real dialogue (the user's turns and her answers), and only the last few of
// them (the «memory» setting): attempts, dropped phrases, noise and background talk never reach her. The server
// keeps nothing between calls, so the page hands her that memory when a call starts.
const CONV_MAX = 40;
const EVENTS_MAX = 400;
let convs = [];
try { convs = JSON.parse(load("convs")) || []; } catch (err) { convs = []; }
for (const c of convs) { // the first version kept only the dialogue, as {r, t}
  if (!c.events) c.events = (c.msgs || []).map((m, i) => ({ k: m.r, at: c.t + i, text: m.t }));
  delete c.msgs;
}
let current = convs.find((c) => c.id === load("convCurrent")) || null;
let viewing = null; // the id of the conversation whose timeline is open in the history window

function persistConvs() {
  convs.sort((a, b) => b.u - a.u);
  convs.length = Math.min(convs.length, CONV_MAX);
  save("convs", JSON.stringify(convs));
  save("convCurrent", current ? current.id : "");
}

function logEvent(k, text, extra) {
  text = String(text || "").trim();
  if ((k === "user" || k === "assistant") && !text) return;
  if (!current && (k === "pause" || k === "resume")) return; // not worth a conversation of its own
  if (!current) {
    current = { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6), t: Date.now(), u: Date.now(), title: "", events: [] };
    convs.unshift(current);
  }
  current.events.push({ k, at: Date.now(), text, ...extra });
  current.events = current.events.slice(-EVENTS_MAX);
  current.u = Date.now();
  if (!current.title && k === "user") current.title = text.slice(0, 60);
  persistConvs();
  renderHistory();
}

function memoryTurns() { return Number(load("memory") ?? 0); } // by default she remembers nothing between calls

// What the agent is told of a conversation: only its dialogue, only the last few turns.
function memoryForServer(conv = current) {
  const n = memoryTurns();
  if (!conv || !n) return [];
  return conv.events
    .filter((e) => (e.k === "user" || e.k === "assistant") && e.text)
    .slice(-n)
    .map((e) => ({ role: e.k, content: e.text }));
}

el.memorySel.value = String(memoryTurns());
el.memorySel.onchange = () => save("memory", el.memorySel.value);

function showConversation(conv) {
  resetLog();
  for (const e of conv ? conv.events : []) if (e.k === "user" || e.k === "assistant") bubble(e.k, e.text);
}

function newConversation() {
  current = null;
  viewing = null;
  persistConvs();
  resetLog();
  if (live) send({ type: "history", history: [] });
  renderHistory();
}

function openConversation(id) {
  const conv = convs.find((c) => c.id === id);
  if (!conv) return;
  closeDialog(el.historyDialog);
  current = conv;
  viewing = null;
  persistConvs();
  showConversation(conv);
  if (live) send({ type: "history", history: memoryForServer(conv) });
  renderHistory();
}

function deleteConversation(id) {
  convs = convs.filter((c) => c.id !== id);
  if (viewing === id) viewing = null;
  if (current && current.id === id) { current = null; resetLog(); if (live) send({ type: "history", history: [] }); }
  persistConvs();
  renderHistory();
}

const INTERRUPT_BY = {
  stop: "стоп-слово голосом",
  takeover: "голосом: ви почали говорити своє",
  sound: "голосом: ви говорили поверх агента",
  button: "кнопкою",
};

// One timeline row: [icon, text, style].
function describeEvent(e, name) {
  switch (e.k) {
    case "user": return ["🗣", "Ви: " + e.text, ""];
    case "assistant": return ["🤖", name + ": " + e.text + (e.cut ? " (перервано)" : ""), ""];
    case "interrupt": return ["✂", "Ви перебили (" + (INTERRUPT_BY[e.by] || e.by) + ")" + (e.text ? ": «" + e.text + "»" : ""), ""];
    case "attempt": return ["…", "Ви заговорили поверх агента, але це був короткий звук, не перебивання", "dim"];
    case "dropped": return ["✖", "Не спрацювало: «" + e.text + "». Чому: " + (DROP_REASONS[e.why] || e.why || "відкинуто"), "warn"];
    case "pause": return ["⏸", "Пауза" + (e.text ? " (" + e.text + ")" : ""), "dim"];
    case "resume": return ["▶", "Продовжено", "dim"];
    case "background": return ["👂", "На паузі ви сказали: «" + e.text + "»", "dim"];
    default: return ["•", e.text || e.k, "dim"];
  }
}

const clock = (at) => new Date(at).toLocaleTimeString("uk-UA");

function timelineText(conv) {
  const name = el.assistantName.textContent;
  return conv.events.map((e) => { const [icon, text] = describeEvent(e, name); return clock(e.at) + "  " + icon + " " + text; }).join("\n");
}

function renderTimeline(conv) {
  const name = el.assistantName.textContent;
  el.timeline.innerHTML = "";
  for (const e of conv.events) {
    const [icon, text, style] = describeEvent(e, name);
    const row = document.createElement("div");
    row.className = "trow" + (style ? " " + style : "");
    for (const [cls, content] of [["t", clock(e.at)], ["i", icon], ["x", text]]) {
      const cell = document.createElement("span");
      cell.className = cls;
      cell.textContent = content; // what the user said: textContent, never HTML
      row.appendChild(cell);
    }
    el.timeline.appendChild(row);
  }
  const count = (k) => conv.events.filter((e) => e.k === k).length;
  el.historyStats.textContent = "Ваших реплік: " + count("user") + " · перебивань: " + count("interrupt")
    + " · не спрацювало: " + (count("dropped") + count("attempt")) + " · записано подій: " + conv.events.length;
}

function renderHistory() {
  const conv = viewing && convs.find((c) => c.id === viewing);
  if (viewing && !conv) viewing = null;
  el.historyList.hidden = Boolean(conv);
  el.historyView.hidden = !conv;
  if (conv) { renderTimeline(conv); return; }
  el.historyList.innerHTML = "";
  if (!convs.length) {
    const none = document.createElement("p");
    none.className = "muted";
    none.textContent = "Поки порожньо: розмова з'явиться тут, щойно ви щось скажете.";
    el.historyList.appendChild(none);
    return;
  }
  for (const c of convs) {
    const isCurrent = Boolean(current && current.id === c.id);
    const row = document.createElement("div");
    row.className = "hitem" + (isCurrent ? " current" : "");
    const text = document.createElement("div");
    text.className = "htext";
    const title = document.createElement("div");
    title.className = "htitle";
    title.textContent = c.title || "(без назви)"; // user text: textContent, never HTML
    const meta = document.createElement("div");
    meta.className = "hmeta";
    const said = c.events.filter((e) => e.k === "user").length;
    meta.textContent = new Date(c.u).toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
      + " · ваших реплік: " + said + " · подій: " + c.events.length + (isCurrent ? " · поточна" : "");
    text.appendChild(title);
    text.appendChild(meta);
    const btns = document.createElement("div");
    btns.className = "hbtns";
    const addButton = (label, ghost, handler) => {
      const b = document.createElement("button");
      b.className = "btn small-btn" + (ghost ? " ghost" : "");
      b.type = "button";
      b.textContent = label;
      b.onclick = handler;
      btns.appendChild(b);
    };
    addButton("Переглянути", false, () => { viewing = c.id; renderHistory(); });
    if (!isCurrent) addButton("Відкрити", false, () => openConversation(c.id));
    addButton("Видалити", true, () => deleteConversation(c.id));
    row.appendChild(text);
    row.appendChild(btns);
    el.historyList.appendChild(row);
  }
}

el.historyBack.onclick = () => { viewing = null; renderHistory(); };
el.newChat.onclick = newConversation;

// «Очистити»: this conversation is gone from the screen, the history and the agent's memory. Two presses (the second
// within 3 s), because it cannot be undone and «Нова розмова» is the button for keeping it.
let clearArmed = null;

function disarmClear() {
  clearTimeout(clearArmed);
  clearArmed = null;
  el.clearChat.textContent = "Очистити";
}

el.clearChat.onclick = () => {
  if (!clearArmed) {
    el.clearChat.textContent = "Точно очистити?";
    clearArmed = setTimeout(disarmClear, 3000);
    return;
  }
  disarmClear();
  clearChat();
};

function clearChat() {
  if (current) { deleteConversation(current.id); return; } // also resets the log and the agent's memory
  viewing = null;
  resetLog();
  if (live) send({ type: "history", history: [] });
  renderHistory();
}

// ---------------------------------------------------------------- the windows behind the two buttons at the top

function openDialog(dialog) {
  if (dialog.open) return;
  if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
}

function closeDialog(dialog) {
  if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
}

el.openHistory.onclick = () => { viewing = null; renderHistory(); openDialog(el.historyDialog); };
el.openUsage.onclick = () => { renderUsageTable(); openDialog(el.usageDialog); };
for (const dialog of [el.historyDialog, el.usageDialog]) {
  // a click on the dimmed backdrop (the dialog element itself, not its content) closes it, like Esc does
  dialog.addEventListener("click", (e) => { if (e.target === dialog) closeDialog(dialog); });
}
document.querySelectorAll("[data-close]").forEach((b) => {
  b.onclick = () => closeDialog(document.getElementById(b.dataset.close));
});

// ---------------------------------------------------------------- the voice, mid-call too

function sendVoice() { send({ type: "voice", provider: el.provider.value, voice: el.voice.value }); }

// Picking a voice mid-call only prepares the change: «Застосувати голос» makes it. The server connects the new
// voice at once and swaps it in at the next sentence, so the answer goes on in the new voice without a pause.
let applyTimer = null;

function updateVoiceApply() {
  const differs = live && (el.provider.value !== activeVoice.provider || el.voice.value !== activeVoice.voice);
  el.voiceApply.hidden = !differs;
  if (!differs) resetVoiceApply();
}

function resetVoiceApply() {
  clearTimeout(applyTimer);
  el.voiceApply.disabled = false;
  el.voiceApply.textContent = "Застосувати голос";
}

el.provider.onchange = () => { stopPreview(); fillVoices(); updateVoiceApply(); updateButtons(); };
el.voice.onchange = () => { stopPreview(); save("voice:" + el.provider.value, el.voice.value); updateVoiceApply(); };
function applyVoice() {
  el.voiceApply.hidden = false;
  el.voiceApply.disabled = true;
  el.voiceApply.textContent = "Застосовую…";
  save("provider", el.provider.value);
  save("voice:" + el.provider.value, el.voice.value);
  sendVoice();
  clearTimeout(applyTimer);
  applyTimer = setTimeout(resetVoiceApply, 8000); // no answer: the button comes back, the user can try again
}

el.voiceApply.onclick = applyVoice;

// The server changed the voice (the page's selector, or the user asked for it): the selectors follow, and the
// audio that comes next has the new sample rate.
function onVoice(m) {
  ttsRate = m.sample_rate;
  if (el.provider.value !== m.provider) { el.provider.value = m.provider; fillVoices(); }
  if (![...el.voice.options].some((o) => o.value === m.voice)) el.voice.add(new Option(m.voice, m.voice));
  el.voice.value = m.voice;
  activeVoice = { provider: m.provider, voice: m.voice };
  updateVoiceApply();
  save("provider", m.provider);
  save("voice:" + m.provider, m.voice);
}

function setRecLang(code) {
  if (!code || code === recLang) return;
  recLang = code;
  if (recognition) { // it restarts by itself, now in the new language
    recognition.lang = code;
    try { recognition.abort(); } catch (err) { /* not started */ }
  }
}

// ---------------------------------------------------------------- the character and the emotion

// The character is a manner (its own name, how it talks); the emotion is how alive the delivery is. The voice is a
// separate choice, any voice, and never changes by itself: not with the character, not when the character is asked
// for by voice («поклич Толіка»). What follows the character and the emotion is only how the same voice is delivered
// (the «Підлаштовувати звучання» switch), and the voice list marks the voices that suit the character.
function parseCharacter(value) {
  // An old stored choice can be «tolik|respeecher|voice» (when the menu had a voice per entry) or «kit».
  const id = String(value || "eva").split("|")[0];
  return { id: id === "kit" ? "kishka" : id };
}

function characterId() { return parseCharacter(el.character.value).id; }

function describeCharacter() {
  const option = el.character.options && [...el.character.options].find((o) => o.value === el.character.value);
  el.characterInfo.textContent = (option && option.dataset && option.dataset.desc) || "";
}

function buildCharacterMenu(characters) {
  if (!characters || !characters.length) return;
  el.character.innerHTML = "";
  for (const c of characters) {
    const option = new Option(c.label, c.id);
    option.dataset.desc = c.description;
    el.character.add(option);
    characterVoices[c.id] = c.voices || [];
  }
  const want = parseCharacter(load("character")).id;
  el.character.value = [...el.character.options].some((o) => o.value === want) ? want : "eva";
  describeCharacter();
  fillVoices(); // now the voices that suit the character can be marked
  const saved = load("voice:" + el.provider.value); // marking reorders the list: keep the user's voice
  if (saved && [...el.voice.options].some((o) => o.value === saved)) el.voice.value = saved;
}

// The voice list is rebuilt for another character (marks, order), the voice the user has picked stays.
function refreshVoiceList() {
  const keep = el.voice.value;
  fillVoices();
  if ([...el.voice.options].some((o) => o.value === keep)) el.voice.value = keep;
  updateVoiceApply();
}

function selectCharacterId(id) {
  if (characterId() === id) return;
  if ([...el.character.options].some((o) => o.value === id)) el.character.value = id;
  refreshVoiceList();
}

function showStyle(character, emotion, description) {
  if (character) selectCharacterId(character);
  if (emotion && el.emotion.value !== emotion) el.emotion.value = emotion;
  if (character) save("character", characterId());
  if (emotion) save("emotion", emotion);
  if (description !== undefined) el.characterInfo.textContent = description; else describeCharacter();
}

function sendStyle() {
  send({ type: "style", character: characterId(), emotion: el.emotion.value, tune: el.tuneVoice.checked });
}

// What the server did to the delivery, in words: the right factors are found by ear, so it is shown.
function showTuning(m) {
  let text;
  if (m.active) text = "Звучання під характер: temperature " + m.value + " (власне значення голосу " + m.base + " × " + m.factor + ").";
  else if (m.reason === "off") text = "Підлаштування звучання вимкнено: голос звучить як за замовчуванням.";
  else if (m.reason === "provider") text = "Цей синтезатор (Soniox) не має параметрів звучання: характер і емоційність лише в тексті.";
  else text = "Не вдалося підлаштувати звучання: провайдер не віддав параметри цього голосу.";
  el.tuningInfo.textContent = text;
}

el.character.value = parseCharacter(load("character")).id;
el.emotion.value = load("emotion") || "emotional";
el.tuneVoice.checked = load("tune") !== "0";
describeCharacter();
el.character.onchange = () => {
  save("character", characterId());
  describeCharacter();
  refreshVoiceList();
  if (live) sendStyle();
};
el.emotion.onchange = () => { save("emotion", el.emotion.value); if (live) sendStyle(); };
el.tuneVoice.onchange = () => { save("tune", el.tuneVoice.checked ? "1" : "0"); if (live) sendStyle(); };

// ---------------------------------------------------------------- pause

const IDLE_PAUSE_MS = (((window.APP_CONFIG || {}).IDLE_PAUSE_SECONDS ?? 90)) * 1000;

function setPaused(on, why) {
  if (!live || on === paused) return;
  paused = on;
  logEvent(on ? "pause" : "resume", on ? (why ? "тиша" : "кнопкою") : "");
  if (on) {
    send({ type: "pause", background: el.bgListen.checked });
    stopPlayback();
    bargeReset();
    onUserDrop();
    if (why) bubble("system", why);
    pauseRecognition();
  } else {
    send({ type: "resume" });
    touch();
    usageNow.at = Date.now();
    if (!recognition && !micMuted) startRecognition();
  }
  updateButtons();
  renderHearing();
}

// On pause the recognizer goes on only if the user lets the agent listen in the background.
function pauseRecognition() {
  if (!el.bgListen.checked) {
    stopRecognition();
    showRecog("🎤 На паузі: нічого не слухаю");
  } else if (!recognition && !micMuted) {
    startRecognition();
  }
}

el.pause.onclick = () => setPaused(!paused);
el.bgListen.checked = load("bgListen") !== "0";
el.bgListen.onchange = () => {
  save("bgListen", el.bgListen.checked ? "1" : "0");
  if (!paused) return;
  send({ type: "pause", background: el.bgListen.checked }); // the server learns the new choice
  pauseRecognition();
};
setInterval(() => {
  if (!live || !ready || paused || !IDLE_PAUSE_MS || curState !== "listening" || barge.audible || micMuted) return;
  if (Date.now() - lastActivity > IDLE_PAUSE_MS) {
    setPaused(true, "Пауза: ніхто не говорив " + Math.round(IDLE_PAUSE_MS / 1000) + " с. Натисніть «Продовжити».");
  }
}, 5000);

// ---------------------------------------------------------------- how easily the voice interrupts her

function bargeSens() { return Number(load("bargeSens") ?? 2); }

// What the agent knows about itself and the place (who it is, where, what event): kept here, sent when a call starts
// and whenever it is edited, and in his prompt from the next answer on.
const ABOUT_MAX = 4000;

function showAboutCount() { el.aboutCount.textContent = el.about.value.length + " / " + ABOUT_MAX; }

el.about.value = load("about") || "";
showAboutCount();
el.about.oninput = () => {
  save("about", el.about.value);
  showAboutCount();
  send({ type: "about", text: el.about.value });
};

el.greet.checked = load("greet") !== "0";
el.greet.onchange = () => save("greet", el.greet.checked ? "1" : "0");

el.engine.value = load("engine") === "browser" ? "browser" : "realtime";
el.engine.onchange = () => save("engine", el.engine.value);

el.bargeSens.value = String(bargeSens());
el.bargeSens.onchange = () => {
  save("bargeSens", el.bargeSens.value);
  if (gate) gate.port.postMessage({ sens: Number(el.bargeSens.value) }); // at once, mid-sentence too
};

// ---------------------------------------------------------------- what the microphone hears

// The gate reports the microphone's level ten times a second. The panel shows it with the bar a voice has to pass
// to count as the user, and says in words which case this is: a dead microphone, a voice too quiet against her
// echo, or all fine. The log lists every phrase: recognized, passed on, dropped and why.
const LEVEL_FULL = 0.2; // the bar's full scale (rms)
const DROP_REASONS = {
  quiet: "це не ваш голос або він тихіший за ехо агента",
  echo: "це ехо: агент почув власний голос через колонки",
  stop: "ви сказали «стоп»: агент замовк і не відповідає на це",
  backchannel: "«угу», «ага» чи фоновий звук: це не перебивання",
  not_interruption: "сказано поверх агента, але це не схоже на перебивання (замало слів)",
};
let mic = { level: 0, need: 0, audible: false, speaking: false };
let deadSince = 0;
const hearLines = [];

function onLevels(l) {
  if (!l || !live) return;
  mic = l;
  if (l.active) lastActiveAt = Date.now();
  bargeFromLevels(l);
  el.levelFill.style.width = Math.min(100, (l.level / LEVEL_FULL) * 100) + "%";
  el.levelNeed.style.left = Math.min(100, (l.need / LEVEL_FULL) * 100) + "%";
  el.levelNeed.hidden = !l.audible;
  if (l.level < 0.0002) deadSince = deadSince || Date.now(); else deadSince = 0;
  renderHearing();
}

function renderHearing() {
  el.hearing.hidden = !live;
  if (!live) return;
  const n = (x) => (x * 100).toFixed(1);
  let text;
  if (micMuted) text = "🔇 Мікрофон вимкнено: нічого не чую.";
  else if (paused) text = el.bgListen.checked ? "⏸ Пауза: слухаю фоном." : "⏸ Пауза: нічого не слухаю.";
  else if (!gate) text = "Рівень мікрофона в цьому браузері не видно (немає AudioWorklet).";
  else if (deadSince && Date.now() - deadSince > 3000) {
    text = "⚠ Мікрофон не передає звук (рівень 0). Це проблема мікрофона, не фільтра: перевірте вибір пристрою й дозвіл у браузері.";
  } else if (mic.audible) {
    if (mic.level > mic.need) text = "✔ Ваш голос (" + n(mic.level) + ") голосніший за ехо агента: він має зупинитись.";
    else if (mic.level > 0.01) text = "Чую звук (" + n(mic.level) + "), але він тихіший за ехо агента: потрібно більше " + n(mic.need) + ". Говоріть голосніше або підніміть чутливість.";
    else text = "Агент говорить, мікрофон майже мовчить (" + n(mic.level) + ").";
  } else if (mic.speaking) text = "✔ Чую ваш голос (рівень " + n(mic.level) + ").";
  else if (mic.level > 0.01) text = "Чую звук (рівень " + n(mic.level) + "), але це не схоже на голос поруч.";
  else text = "Тихо (рівень " + n(mic.level) + ").";
  el.hearingStatus.textContent = text;
}

function hearEvent(icon, text, bad) {
  hearLines.unshift({ at: new Date(), icon, text, bad: Boolean(bad) });
  hearLines.length = Math.min(hearLines.length, 12);
  el.hearLog.innerHTML = "";
  for (const h of hearLines) {
    const row = document.createElement("div");
    row.className = "hrow" + (h.bad ? " bad" : "");
    row.textContent = h.at.toLocaleTimeString("uk-UA") + "  " + h.icon + " " + h.text;
    el.hearLog.appendChild(row);
  }
}

// ---------------------------------------------------------------- volume

let volume = Math.min(1, Math.max(0, Number(load("volume") ?? 100) / 100));
el.volume.value = String(Math.round(volume * 100));
el.volumeValue.textContent = Math.round(volume * 100) + "%";
el.volume.oninput = () => {
  volume = Number(el.volume.value) / 100;
  el.volumeValue.textContent = el.volume.value + "%";
  save("volume", el.volume.value);
  setPlayVolume(barge.stage === "ducked" ? DUCK_LEVEL : 1); // at once, mid-sentence too
};

// ---------------------------------------------------------------- devices

const CAN_PICK_SPEAKER = typeof AudioContext !== "undefined" && "setSinkId" in AudioContext.prototype;

function getMic() {
  const id = load("mic");
  const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 };
  const ask = (a) => navigator.mediaDevices.getUserMedia({ audio: a });
  // A microphone that was unplugged since: fall back to the default one instead of failing the call.
  return id ? ask({ ...audio, deviceId: { exact: id } }).catch(() => ask(audio)) : ask(audio);
}

function fillDevices(select, list, saved, fallbackLabel) {
  select.innerHTML = "";
  select.add(new Option("За замовчуванням", ""));
  list.forEach((d, i) => { if (d.deviceId && d.deviceId !== "default") select.add(new Option(d.label || fallbackLabel + " " + (i + 1), d.deviceId)); });
  select.value = [...select.options].some((o) => o.value === saved) ? saved : "";
}

async function refreshDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const list = await navigator.mediaDevices.enumerateDevices();
    const named = list.some((d) => d.label);
    fillDevices(el.micSelect, list.filter((d) => d.kind === "audioinput"), load("mic"), "Мікрофон");
    fillDevices(el.speakerSelect, list.filter((d) => d.kind === "audiooutput"), load("speaker"), "Динамік");
    el.speakerSelect.disabled = !CAN_PICK_SPEAKER;
    el.devicesAllow.hidden = named;
    el.devicesInfo.textContent = (named ? "" : "Назви пристроїв з'являться, коли браузер дасть доступ до мікрофона. ")
      + (CAN_PICK_SPEAKER ? "" : "Динамік можна вибрати в Chrome і Edge; в інших браузерах звук іде в системний. ")
      + "Мікрофон для шлюзу змінюється одразу; у режимі «напряму» розпізнавання слухає мікрофон за замовчуванням.";
  } catch (err) { /* no device list */ }
}

function applySink() {
  if (!CAN_PICK_SPEAKER || !playCtx) return;
  playCtx.setSinkId(load("speaker") || "").catch(() => { /* the device is gone: the default one plays */ });
}

async function switchMic() {
  save("mic", el.micSelect.value);
  if (!live || !micStream) return;
  try {
    const next = await getMic();
    const old = micStream;
    micStream = next;
    next.getAudioTracks().forEach((t) => { t.enabled = !micMuted; });
    if (gate) {
      micSource?.disconnect();
      micSource = playCtx.createMediaStreamSource(next);
      micSource.connect(gate, 0, 0);
    }
    old.getTracks().forEach((t) => t.stop());
    refreshDevices();
  } catch (err) {
    bubble("system", "Не вдалося перемкнути мікрофон: " + err.message);
  }
}

el.micSelect.onchange = switchMic;
el.speakerSelect.onchange = () => { save("speaker", el.speakerSelect.value); applySink(); };
el.devicesAllow.onclick = async () => {
  try { (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach((t) => t.stop()); } catch (err) { /* refused */ }
  refreshDevices();
};
if (navigator.mediaDevices) navigator.mediaDevices.addEventListener?.("devicechange", refreshDevices);

// ---------------------------------------------------------------- the meter and the 30-day costs

// What the call costs is counted on the server (tokens, voice characters, searches) and reported after each
// answer; the seconds tick here. Totals per day are kept in this browser, not on the server.
let usageLast = { seconds: 0, cost: 0 }; // the last report of the current call, to add only the difference
let usageNow = { seconds: 0, cost: 0, at: 0, llm_in: 0, llm_out: 0, tts_chars: 0, searches: 0 };

const dayKey = (d = new Date()) => "usage:" + d.toISOString().slice(0, 10);

function addToday(seconds, usd) {
  if (seconds <= 0 && usd <= 0) return;
  let day = { sec: 0, usd: 0 };
  try { day = JSON.parse(load(dayKey())) || day; } catch (err) { /* a new day */ }
  save(dayKey(), JSON.stringify({ sec: day.sec + Math.max(0, seconds), usd: day.usd + Math.max(0, usd) }));
  renderUsageTable();
}

function onUsage(m) {
  addToday(m.seconds - usageLast.seconds, m.cost - usageLast.cost);
  usageLast = { seconds: m.seconds, cost: m.cost };
  usageNow = { ...m, at: Date.now() };
  renderMeter();
}

function finishUsage() { // the call ends: the seconds since the last report are still used
  const seconds = usageNow.seconds + (paused ? 0 : (Date.now() - usageNow.at) / 1000);
  addToday(seconds - usageLast.seconds, 0);
}

const fmtTime = (sec) => Math.floor(sec / 60) + ":" + String(Math.floor(sec % 60)).padStart(2, "0");
const fmtUsd = (usd) => (usd > 0 && usd < 0.01 ? "<$0.01" : "$" + usd.toFixed(2));

function renderMeter() {
  el.meter.hidden = !live;
  if (!live) return;
  const sec = usageNow.seconds + (paused ? 0 : (Date.now() - usageNow.at) / 1000);
  el.meter.textContent = "⏱ " + fmtTime(sec) + " · ≈ " + fmtUsd(usageNow.cost);
  el.meter.title = "Токенів моделі: " + usageNow.llm_in + " на вході, " + usageNow.llm_out + " на виході; символів голосу: "
    + usageNow.tts_chars + "; пошуків: " + usageNow.searches + ". Пауза не рахується.";
}
setInterval(renderMeter, 1000);
setInterval(() => renderHearing(), 1000);

function renderUsageTable() {
  const rows = [];
  let totalSec = 0;
  let totalUsd = 0;
  for (let i = 0; i < 30; i++) {
    const d = new Date(Date.now() - i * 86400000);
    let day = null;
    try { day = JSON.parse(load(dayKey(d))); } catch (err) { /* none */ }
    if (!day) continue;
    totalSec += day.sec;
    totalUsd += day.usd;
    rows.push('<div class="urow"><span>' + d.toLocaleDateString("uk-UA") + "</span><span>" + Math.round(day.sec / 60) + " хв · " + fmtUsd(day.usd) + "</span></div>");
  }
  el.usageTable.innerHTML = rows.length
    ? rows.join("") + '<div class="urow total"><span>Разом</span><span>' + Math.round(totalSec / 60) + " хв · " + fmtUsd(totalUsd) + "</span></div>"
    : '<p class="muted">Поки порожньо: дані з\'являться після першої розмови.</p>';
}

// ---------------------------------------------------------------- copy the conversation

async function copyText(button, text) {
  const old = button.textContent;
  if (!text) { button.textContent = "Немає що копіювати"; } else {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Скопійовано";
    } catch (err) { // no clipboard permission (an insecure page, an old browser): the old way
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); button.textContent = "Скопійовано"; } catch (e2) { button.textContent = "Не вдалося"; }
      area.remove();
    }
  }
  setTimeout(() => { button.textContent = old; }, 1800);
}

el.copyChat.onclick = () => {
  const name = el.assistantName.textContent;
  const text = [...el.log.querySelectorAll(".msg")]
    .filter((m) => !m.classList.contains("partial"))
    .map((m) => (m.classList.contains("user") ? "Ви: " : m.classList.contains("assistant") ? name + ": " : "") + m.textContent)
    .join("\n");
  copyText(el.copyChat, text);
};
el.historyCopy.onclick = () => {
  const conv = convs.find((c) => c.id === viewing);
  copyText(el.historyCopy, conv ? timelineText(conv) : "");
};

showConversation(current); // after a reload the conversation is still here
renderHistory();
refreshDevices();
renderUsageTable();
init();
