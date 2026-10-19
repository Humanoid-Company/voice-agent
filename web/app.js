// Browser side: microphone + speaker go straight to OpenAI GPT-Live over WebRTC. The backend only
// turns our SDP offer into a session (the keys stay there) and runs the tools.
(() => {
  const params = new URLSearchParams(location.search);
  const BACKEND = (params.get("backend") || (window.APP_CONFIG || {}).BACKEND_URL || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  // Random id that keeps this browser's conversation and settings separate from other testers'.
  let clientId = store.get("va-client-id");
  if (!clientId) {
    clientId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
    store.set("va-client-id", clientId);
  }
  let accessCode = store.get("va-access-code") || "";
  let assistantName = ""; // set when renamed by voice (/api/me); «Єва» otherwise
  // Voice picker: the choice lives here (the server forgets it on restart) and is sent with
  // every new call. Voice is fixed for a Live session, so a change applies from the next call.
  let voices = [];
  let voice = store.get("va-voice") || "";
  let speed = store.get("va-speed") || "normal";
  let style = store.get("va-style") || "normal";
  let volume = parseFloat(store.get("va-volume") || "1");
  // Keep what is said nearby during a pause (voice/background.py); the user can turn it off.
  let bgListen = store.get("va-bg-listen") !== "0";
  // A voice picked while Єва answers: switch at once (she goes on in the new voice) or once she is done.
  let voiceNow = store.get("va-voice-now") !== "0";
  if (!(volume >= 0 && volume <= 1)) volume = 1;
  // Devices picked in «Пристрої» ("" = the system default); kept in this browser only.
  let micId = store.get("va-mic") || "";
  let speakerId = store.get("va-speaker") || "";
  let cameraId = store.get("va-camera") || "";

  const DEBUG = params.get("debug") === "1";
  // Delivery prompt (prompts/live_prompt.py): v4 is the usual one (v3 with full, concrete answers), v3
  // behaves like a person. Picked in the voice card (or ?prompt=v3); a change reopens the call, as a
  // voice change does. v1 and v2 are gone from the web (the desktop keeps them): a choice saved
  // earlier becomes v4.
  const WEB_PROMPTS = ["v3", "v4"];
  let promptVariant = params.get("prompt") || store.get("va-prompt") || "v4";
  if (!WEB_PROMPTS.includes(promptVariant)) promptVariant = "v4";
  let pc = null;
  let channel = null;
  let sessionVoice = ""; // the voice the current call was opened with
  let mic = null;
  let gate = null;
  let gateStats = null; // ?debug=1: the gate's totals (evaGate())
  let micSender = null; // the current call's mic sender: another mic replaces its track in place
  // One open bubble per speaker: user and assistant transcripts arrive interleaved when they
  // overlap, and a single "current" bubble shredded both into word fragments.
  const openMsg = { user: null, assistant: null };
  const lastDeltaAt = { user: 0, assistant: 0 };
  const TURN_PAUSE_MS = 3000; // the transcript of one utterance can pause this long mid-sentence
  // Her reply after the user spoke is a new bubble below their line once her text has paused this long
  // (a stream that goes on over them stays one bubble). With only TURN_PAUSE_MS her answer to «А зараз»
  // was added to her previous bubble, above the question — the log read as if she ignored it.
  const TURN_RESUME_MS = 1000;

  const headers = () => ({
    "Content-Type": "application/json",
    "X-Client-Id": clientId,
    "X-Access-Code": accessCode,
  });

  function setStatus(text, state) {
    $("status").textContent = text;
    $("dot").className = "dot" + (state ? " " + state : "");
  }

  function addLine(role, text) {
    const empty = $("log").querySelector(".empty");
    if (empty) empty.remove();
    const p = document.createElement("p");
    p.className = "msg " + role;
    p.textContent = text;
    $("log").appendChild(p);
    $("log").scrollTop = $("log").scrollHeight;
    return p;
  }

  function appendTranscript(role, delta) {
    if (!delta) return;
    const now = Date.now();
    const other = role === "user" ? "assistant" : "user";
    // A new turn starts after a pause, or once the other side has spoken since this bubble.
    const otherSpoke = lastDeltaAt[other] > lastDeltaAt[role];
    if (!openMsg[role] || now - lastDeltaAt[role] > TURN_PAUSE_MS || lastDeltaAt[other] > lastDeltaAt[role] + TURN_PAUSE_MS
        || (otherSpoke && now - lastDeltaAt[role] > TURN_RESUME_MS)) {
      openMsg[role] = addLine(role, "");
    }
    lastDeltaAt[role] = now;
    const text = openMsg[role].textContent;
    // Sentences can arrive glued («недочула.О, та добре»): keep a space between them.
    openMsg[role].textContent += /[.!?…]$/.test(text) && /^[^\s.,!?…»)]/.test(delta) ? " " + delta : delta;
    $("log").scrollTop = $("log").scrollHeight;
  }

  function closeBubbles() {
    openMsg.user = openMsg.assistant = null;
    lastDeltaAt.user = lastDeltaAt.assistant = 0;
  }

  function clearLog() {
    closeBubbles();
    $("log").replaceChildren();
    const empty = document.createElement("p");
    empty.className = "muted empty";
    empty.textContent = "Тут з'являтиметься текст розмови.";
    $("log").appendChild(empty);
  }

  // Noise gate between the mic and WebRTC: background sounds (typing, TV, people nearby)
  // never reach the model, only speech louder than the room's noise floor does. While the
  // assistant is audible the bar also sits above its echo in the mic, learned from how loud
  // that echo really is: high on laptop speakers, near zero in headphones — so a quiet voice
  // in a headset still gets through.
  // It also hears the assistant's own audio (2nd input) and reports to the page when the
  // user starts/stops talking and when the assistant is audible — that drives barge-in.
  // ?gate=off turns it off. It runs on the audio thread (AudioWorklet), so a background tab,
  // where timers are throttled to once a second, doesn't chop the user's speech.
  const GATE_WORKLET = `
    class NoiseGate extends AudioWorkletProcessor {
      constructor(options) {
        super();
        this.frame = Math.round(sampleRate * 0.02);       // judge loudness per 20 ms
        // 250 ms look-ahead: the gate opens on voiced sound, and a soft start before it («с», «ф»,
        // «х», «п» — «Скажи», «Привіт») was cut at 60 ms and misheard; at 150 ms quiet words still lost
        // their start (measured, see the gain below).
        this.ring = new Float32Array(Math.round(sampleRate * 0.25));
        this.pos = 0; this.sum = 0; this.outSum = 0; this.count = 0;
        this.floor = 0.003; this.open = false; this.loud = 0; this.quietFor = 1;
        this.gain = 0;
        // Barge-in only listens to "near" voice: the person at the mic is far louder than
        // people talking in the background, a TV or a door bang (those are short).
        this.near = (options.processorOptions || {}).near || 0.06;
        // How loud this user speaks, learned while Єва is silent: the «near voice» bar follows it, so a
        // quiet user can still interrupt her (a fixed 0.06 missed them even in headphones), while
        // background voices and a TV, quieter than the user, still don't count.
        this.voice = 0;
        this.speaking = false; this.nearFrames = 0; this.nearQuietFor = 1;
        this.audible = false; this.outQuietFor = 1;       // assistant playback
        // Mic level per unit of assistant output level while only the assistant talks. Starts
        // as "loud speakers" and drops within a second or two when there is no echo (headset).
        this.echo = 0.5;
        // ?debug=1: totals posted every second for evaGate() — how much of the user the gate cut while
        // she talked, measured on the real device (echo and voice levels differ per laptop).
        this.stats = (options.processorOptions || {}).stats
          ? { her: 0, herOpen: 0, cut: 0, quiet: 0, quietOpen: 0, frames: 0 } : null;
      }
      judge(level, outLevel) {
        if (outLevel > 0.01) this.outQuietFor = 0; else this.outQuietFor += 0.02;
        const audible = this.outQuietFor < 0.3;
        if (audible !== this.audible) { this.audible = audible; this.port.postMessage({ audible }); }
        // 0.006 ≈ quiet speech into a headset mic; the room's noise floor raises the bar.
        const echoLevel = audible ? outLevel * this.echo * 2.5 : 0;
        // Speakers leave bursts of echo that the browser's echo canceller misses; averaged into
        // this.echo they look small, and letting them through can make the model hear itself
        // and break off mid-sentence. So while the assistant talks keep the old 0.04 bar unless the mic plainly
        // hears no echo at all (headphones). this.echo starts high, so "speakers" is the default.
        const headset = this.echo < 0.05;
        // 3× the room's noise (was 4×): very quiet speech by a laptop fan lost whole words at 4×.
        const threshold = Math.max(audible && !headset ? 0.04 : 0.006, this.floor * 3, echoLevel);
        // Learn the echo while the gate is shut (the user isn't talking over the assistant).
        if (audible && !this.open && outLevel > 0.01) {
          const ratio = level / outLevel;
          this.echo += (ratio - this.echo) * (ratio < this.echo ? 0.1 : 0.03);
        }
        if (level > threshold) { this.loud += 1; this.quietFor = 0; }
        else { this.quietFor += 0.02; if (level < threshold * 0.6) this.loud = 0; }
        if (!this.open && this.loud >= 2) this.open = true;
        // 1 s: a quiet word ending or a pause mid-sentence is not cut off (0.6 s cut phrase ends).
        else if (this.open && this.quietFor > 1.0) this.open = false;
        if (this.open && !audible && level > threshold * 1.5) this.voice += (level - this.voice) * (this.voice ? 0.02 : 1);
        const near = this.voice ? Math.min(this.near, Math.max(0.01, this.voice * 0.5)) : this.near;
        const nearThreshold = Math.max(audible ? near * 1.3 : near, this.floor * 8, echoLevel * 1.3);
        if (level > nearThreshold) { this.nearFrames += 1; this.nearQuietFor = 0; }
        else { this.nearQuietFor += 0.02; if (this.nearQuietFor > 0.25) this.nearFrames = 0; }
        // Near voice: ~120 ms of loud frames to start (a click/bang is shorter), 250 ms gap to end.
        const speaking = this.speaking ? this.nearQuietFor <= 0.25 : this.nearFrames >= 6;
        if (speaking !== this.speaking) { this.speaking = speaking; this.port.postMessage({ speaking }); }
        if (this.stats) {
          const st = this.stats;
          // «cut»: while she talks the gate stays shut on a sound at the user's own level, well above her
          // expected echo — most likely their words lost (an estimate: a loud echo burst counts too).
          if (audible) {
            st.her += 1;
            if (this.open) st.herOpen += 1;
            else if (this.voice && level > Math.max(this.voice * 0.4, this.floor * 4, outLevel * this.echo * 1.5)) st.cut += 1;
          } else { st.quiet += 1; if (this.open) st.quietOpen += 1; }
          if (++st.frames % 50 === 0) {
            this.port.postMessage({ stats: { ...st, echo: this.echo, voice: this.voice, floor: this.floor, threshold } });
          }
        }
        // Learn the room's noise only while nobody speaks, so a long sentence can't raise it.
        if (!this.open && !audible) {
          this.floor = level < this.floor ? this.floor * 0.7 + level * 0.3 : this.floor + (level - this.floor) * 0.01;
        }
      }
      process(inputs, outputs) {
        const input = inputs[0][0];
        const remote = inputs[1] && inputs[1][0];
        const output = outputs[0][0];
        if (!input || !output) return true;
        for (let i = 0; i < input.length; i++) {
          const x = input[i];
          this.sum += x * x;
          if (remote) this.outSum += remote[i] * remote[i];
          if (++this.count >= this.frame) {
            this.judge(Math.sqrt(this.sum / this.count), Math.sqrt(this.outSum / this.count));
            this.sum = 0; this.outSum = 0; this.count = 0;
          }
          const delayed = this.ring[this.pos];
          this.ring[this.pos] = x;
          this.pos = (this.pos + 1) % this.ring.length;
          // Closed while she is silent = −14 dB, not silence: a quiet user below the bar is still heard.
          // Cutting to zero made GPT-Live mishear a quiet voice by a fan (feedback: «говорю одне, а
          // виходить інше»): measured with this gate ported to Python on real GPT-Live, 8 phrases, a
          // quiet voice and room noise — word errors 51% / 24% → 6%, a TV-level voice alone still 0 of
          // 70 words heard. While she talks it still closes fully: her echo must not reach the model.
          const closed = this.audible ? 0 : 0.2;
          this.gain += ((this.open ? 1 : closed) - this.gain) * (this.open ? 0.004 : 0.0005);
          output[i] = delayed * this.gain;
        }
        for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(output);
        return true;
      }
    }
    registerProcessor("noise-gate", NoiseGate);
  `;

  async function createNoiseGate(stream, onSignal) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx || params.get("gate") === "off") return null;
    const ctx = new AudioCtx();
    try {
      if (!ctx.audioWorklet) throw new Error("no AudioWorklet");
      const url = URL.createObjectURL(new Blob([GATE_WORKLET], { type: "application/javascript" }));
      try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
      await ctx.resume();
      const node = new AudioWorkletNode(ctx, "noise-gate", {
        numberOfInputs: 2, channelCount: 1, channelCountMode: "explicit",
        // ?near=0.04 makes barge-in more sensitive (quiet voice / far mic), 0.09 less.
        processorOptions: { near: parseFloat(params.get("near")) || 0.06, stats: DEBUG },
      });
      node.port.onmessage = (e) => onSignal(e.data);
      const out = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(stream).connect(node, 0, 0).connect(out);
      let remoteSource = null;
      return {
        stream,
        track: out.stream.getAudioTracks()[0],
        // The assistant's audio, measured only (never played from here). A gate kept for the next
        // call (voice switch) drops the old call's audio first.
        listenTo(remote) {
          if (remoteSource) remoteSource.disconnect();
          remoteSource = remote ? ctx.createMediaStreamSource(remote) : null;
          if (remoteSource) remoteSource.connect(node, 0, 1);
        },
        // Still usable for the next call: not closed by the browser, and running again.
        alive() { if (ctx.state === "suspended") ctx.resume().catch(() => {}); return ctx.state !== "closed"; },
        close() { ctx.close().catch(() => {}); },
      };
    } catch {
      ctx.close().catch(() => {});
      return null; // old browser: send the mic as is
    }
  }

  // ── Єва: «Єва, скажи» wakes her (or «Привіт, Єва», «Гей, Єва», …), «Дякую, Єва» pauses ───────
  // Same rules as voice/wake_phrases.py — keep the two in sync (tests/test_eva_phrases.py has the
  // cases). The name is matched against a list (fuzzy matching a 3-letter word would accept
  // «два»), the verbs fuzzily, like Python's difflib ratio ≥ 0.75.
  const EVA_NAME_FORMS = new Set(["єва", "єво", "ева", "эва", "эво", "ево", "єфа", "ефа", "їва", "іва", "йева", "йєва", "єєва",
    "eva", "evo", "eve", "yeva", "yevo", "jeva"]);
  const EVA_WAKE_VERBS = [
    "скажи", "кажи", "скажіть", "скажи-но",
    "привіт", "привітик", "вітаю", "гей", "хей", "агов", "алло",
    "слухай", "послухай", "допоможи", "підкажи", "прокидайся",
    "hello", "hey",
  ];
  const EVA_STOP_VERBS = ["дякую", "дякуємо"]; // one pause phrase: «Дякую, Єва»
  const EVA_MAX_GAP = 2;

  function evaTokens(text) {
    const lowered = String(text || "").toLowerCase().replace(/’/g, "'").replace(/ё/g, "е");
    const words = (lowered.match(/[a-zа-яіїєґё'-]+/g) || []).map((w) => w.replace(/^['-]+|['-]+$/g, "")).filter(Boolean);
    const merged = [];
    for (const word of words) {
      const last = merged[merged.length - 1];
      if ((last === "є" || last === "е" || last === "э") && word === "ва") merged[merged.length - 1] = last + word;
      else merged.push(...unglue(word));
    }
    return merged;
  }

  // «привітєва» / «гейєва» / «євоскажи»: the recogniser glued the name to the verb — split them.
  function unglue(word) {
    if (word.length <= 4 || EVA_NAME_FORMS.has(word)) return [word];
    const verbs = EVA_WAKE_VERBS.concat(EVA_STOP_VERBS);
    for (const size of [4, 3]) {
      if (EVA_NAME_FORMS.has(word.slice(-size)) && like(word.slice(0, -size), verbs)) return [word.slice(0, -size), word.slice(-size)];
      if (EVA_NAME_FORMS.has(word.slice(0, size)) && like(word.slice(size), verbs)) return [word.slice(0, size), word.slice(size)];
    }
    return [word];
  }

  // difflib.SequenceMatcher.ratio(): 2·matches / total length, matches = longest common blocks.
  function matchingChars(a, b) {
    let best = 0, ai = 0, bj = 0;
    for (let i = 0; i < a.length; i++) {
      for (let j = 0; j < b.length; j++) {
        let k = 0;
        while (i + k < a.length && j + k < b.length && a[i + k] === b[j + k]) k++;
        if (k > best) { best = k; ai = i; bj = j; }
      }
    }
    if (!best) return 0;
    return best + matchingChars(a.slice(0, ai), b.slice(0, bj)) + matchingChars(a.slice(ai + best), b.slice(bj + best));
  }
  const like = (word, verbs) => verbs.some((v) => word === v || (2 * matchingChars(word, v)) / (word.length + v.length) >= 0.75);

  function evaPair(words, verbs) {
    for (let i = 0; i < words.length; i++) {
      if (!EVA_NAME_FORMS.has(words[i])) continue;
      const lo = Math.max(0, i - EVA_MAX_GAP - 1), hi = Math.min(words.length, i + EVA_MAX_GAP + 2);
      for (let j = lo; j < hi; j++) if (j !== i && like(words[j], verbs)) return [i, j];
    }
    return null;
  }

  // «Єва, скажи …» → what was said after the phrase ("" if nothing); null if no wake phrase.
  function matchWake(text) {
    const words = evaTokens(text);
    const pair = evaPair(words, EVA_WAKE_VERBS);
    return pair ? words.slice(Math.max(pair[0], pair[1]) + 1).join(" ") : null;
  }
  const isStop = (text) => evaPair(evaTokens(text), EVA_STOP_VERBS) !== null;
  // «зміни голос на …» — same pattern as VOICE_REQUEST_RE in voice/options.py; the server picks the voice.
  const VOICE_REQUEST_RE = /(змін|змин|поміня|постав|переключ|перемкн|увімкн|зроби|давай)[а-яіїєґ'a-z]*[\s,]+([^\s,]+[\s,]+){0,3}?голос(?:у|а|ом)?(?![а-яіїєґ'a-z])|(^|[^а-яіїєґ'a-z])голос\s+на\s/i;

  // mode: off → connecting → waiting («Єва, скажи»; the model is muted) ⇄ active (listening /
  // thinking / speaking); switching = reconnecting with a new voice.
  const eva = {
    mode: "off",
    pending: null,
    recentUser: "",   // rolling user transcript, for «Дякую, Єва»
    lastUserAt: 0,
    lastAudibleAt: 0,
    thinking: false,
    thinkTimer: null,
    pauseTimer: null,
    recognizer: null,
    voiceRequestBusy: false,
    inputBuf: "",       // the Live transcript of the user's current turn, for «зміни голос …»
    voiceCheckTimer: null,
    wakeSupported: Boolean(window.SpeechRecognition || window.webkitSpeechRecognition) && params.get("wake") !== "off",
  };

  function showState() {
    const m = eva.mode;
    if (m === "off") return; // stop() sets its own text
    let state;
    if (m === "connecting") state = ["Підключаюсь…", "busy"];
    else if (m === "switching") state = ["Перемикаю голос…", "busy"];
    else if (m === "waiting" && eva.ready) state = ["На зв'язку — чекаю «Привіт, Єва»", "wait"];
    else if (m === "waiting") state = eva.wakeSupported
      ? [bgListen ? "Пауза — слухаю фоном, чекаю «Єва, скажи»" : "Пауза — чекаю «Єва, скажи»", "wait"]
      : ["Пауза — натисніть «Продовжити»", "wait"];
    else if (barge.audible) state = ["Говорю", "speak"];
    else if (barge.speaking) state = ["Слухаю", "live"];
    else if (eva.thinking) state = ["Думаю…", "think"];
    else state = ["Слухаю — говоріть", "live"];
    setStatus(state[0], state[1]);
  }

  function updateButtons() {
    const on = eva.mode !== "off";
    $("talk").textContent = on ? "Завершити" : "Почати розмову";
    $("talk").classList.toggle("primary", !on);
    $("talk").classList.toggle("danger", on);
    $("talk").disabled = false;
    $("resume").hidden = eva.mode !== "waiting";
    $("pause").hidden = eva.mode !== "active";
    $("wakeHint").textContent = eva.wakeSupported
      ? "Покличте Єву — «Єва, скажи», «Привіт, Єва», «Гей, Єва», «Єво, слухай». «Дякую, Єва» — пауза. Браузер попросить доступ до мікрофона."
      : "Цей браузер не вміє слухати «Єва, скажи» (потрібен Chrome, Edge або Safari) — користуйтеся кнопками «Пауза» / «Продовжити». «Дякую, Єва» працює.";
    showMeter(); // «Останній дзвінок» once she is off
  }

  // Barge-in, like the desktop app: the Live model alone reacts slowly and keeps talking over
  // the user. Only the person at the mic counts ("near" voice — background talk, a TV, bangs
  // don't): their voice over the assistant ducks it a little; a stop word or real words from
  // them mute it and tell the model to stop. A sound alone never stops the assistant.
  // Whole words only; no «секунду»/«хвилинку» — they are everyday words, not a stop request.
  const STOP_WORDS = new Set(["стоп", "зачекай", "почекай", "стривай", "досить", "stop", "wait"]);
  const splitWords = (text) => text.toLowerCase().replace(/[ʼ’`]/g, "'").split(/[^а-яіїєґa-z'-]+/i).filter(Boolean);
  const BACKCHANNEL = new Set(["угу", "ага", "так", "ммм", "мм", "м", "ок", "окей", "добре", "ну", "ого", "ага-ага", "мгм", "ясно", "зрозуміло", "да",
    "так-так", "угу-угу", "хм", "о", "ой", "ох", "слухаю", "нічого", "собі", "та", "ха", "ха-ха", "хаха", "ахах", "вау"]);
  const barge = {
    audible: false,     // assistant audio is playing right now
    speaking: false,    // user voice right now
    stage: "idle",      // idle → ducked → muted
    heard: "",          // user words since this barge-in candidate started
    burst: "",          // what she has said since her last pause
    burstAt: 0,
    timers: [],
    releaseTimer: null,
    speechEndedAt: 0,   // when the near voice last stopped (transcripts lag behind it)
    recentAssistant: "", // for the echo check
    ducks: 0, mutes: 0, // how often she was ducked / cut off (?debug=1: evaDebug())
  };

  function bargeClear() {
    barge.timers.forEach(clearTimeout);
    barge.timers = [];
  }

  // level: barge-in ducking (1 / 0.5 / 0) × the user's volume; silent while Єва is paused.
  let remoteLevel = 1;
  function setRemoteVolume(level) {
    remoteLevel = level;
    const el = $("remote");
    const v = eva.mode === "waiting" ? 0 : level * volume;
    if (fx && fx.on) {
      // She is heard through the «Чистіший звук» chain; the element only keeps the stream flowing.
      clearInterval(volRamp);
      el.muted = true;
      fx.setVolume(v);
      return;
    }
    rampVolume(el, v);
  }

  // Her volume moves over ~60 ms instead of jumping: an instant step in a playing voice is a click, and
  // on speakers — where her own echo makes barge-in ducks (1 → 0.5 → 1) and mutes frequent — the clicks
  // came as crackle, louder the higher the volume (feedback: «хрипи … повʼязане з самоперебиванням»).
  let volRamp = null;
  function rampVolume(el, target) {
    clearInterval(volRamp);
    if (el.muted) { el.volume = 0; el.muted = false; }
    const from = el.volume;
    let step = 0;
    volRamp = setInterval(() => {
      step += 1;
      el.volume = Math.min(1, Math.max(0, from + (target - from) * (step / 8)));
      if (step >= 8) { clearInterval(volRamp); volRamp = null; el.muted = target === 0; }
    }, 8);
  }

  // Her voice into the speakers (straight, or through the chain) and into the gate's echo meter.
  function playRemote(stream) {
    $("remote").srcObject = stream;
    if (gate) gate.listenTo(stream);
    if (cleanVoice) {
      ensureFx().then((f) => { if (f && $("remote").srcObject === stream) { f.attach(stream); setRemoteVolume(remoteLevel); } });
    }
  }

  // ── «Чистіший звук»: a small chain on her voice before the speakers ────────────────────────────
  // Rumble cut, a little presence (clearer words), a de-esser that only tames sharp «с»/«ш»/«ц»,
  // and a gentle compressor so quiet sentence ends don't drop. It can't change how the model says a
  // sound — only how it comes out. Off by default (?clean=1 or the switch turns it on).
  let cleanVoice = params.get("clean") === "1" || store.get("va-clean") === "1";
  let fx = null;
  let fxLoading = null;
  const DEESS_WORKLET = `
    class DeEsser extends AudioWorkletProcessor {
      constructor() {
        super();
        this.a = 1 - Math.exp((-2 * Math.PI * 5000) / sampleRate); // split at 5 kHz
        this.atk = 1 - Math.exp(-1 / (sampleRate * 0.002));
        this.rel = 1 - Math.exp(-1 / (sampleRate * 0.06));
        this.lp = 0; this.env = 0; this.g = 1;
        this.thr = 0.035; // sibilance above this (linear) is pulled down
      }
      process(inputs, outputs) {
        const input = inputs[0][0];
        const output = outputs[0][0];
        if (!input || !output) return true;
        for (let i = 0; i < input.length; i++) {
          const x = input[i];
          this.lp += this.a * (x - this.lp);
          const high = x - this.lp;
          const level = Math.abs(high);
          this.env += (level - this.env) * (level > this.env ? this.atk : this.rel);
          // Ratio ~3:1 above the threshold, at most −9 dB, smoothed so it never clicks.
          const want = this.env > this.thr ? Math.max(0.35, Math.pow(this.thr / this.env, 0.66)) : 1;
          this.g += (want - this.g) * (want < this.g ? this.atk : this.rel);
          output[i] = this.lp + high * this.g;
        }
        for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(output);
        return true;
      }
    }
    registerProcessor("de-esser", DeEsser);
  `;

  function ensureFx() {
    if (fx) return Promise.resolve(fx);
    if (!fxLoading) fxLoading = createFx().then((f) => { fx = f; fxLoading = null; return f; });
    return fxLoading;
  }

  async function createFx() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return null;
    const ctx = new AudioCtx();
    // She is heard from this context, not from the <audio> element: it plays to the picked speaker too
    // (Chrome/Edge 110+; elsewhere the system default).
    const setSink = (id) => { if (ctx.setSinkId) ctx.setSinkId(id).catch(() => {}); };
    if (speakerId) setSink(speakerId);
    try {
      if (!ctx.audioWorklet) throw new Error("no AudioWorklet");
      const url = URL.createObjectURL(new Blob([DEESS_WORKLET], { type: "application/javascript" }));
      try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
      await ctx.resume().catch(() => {});
      const highpass = new BiquadFilterNode(ctx, { type: "highpass", frequency: 70, Q: 0.7 });
      const presence = new BiquadFilterNode(ctx, { type: "peaking", frequency: 3200, Q: 1, gain: 2.5 });
      const deEsser = new AudioWorkletNode(ctx, "de-esser", { channelCount: 1, channelCountMode: "explicit" });
      const comp = new DynamicsCompressorNode(ctx, { threshold: -22, knee: 12, ratio: 2.5, attack: 0.004, release: 0.18 });
      // The compressor already adds its own make-up gain; no more on top — louder would just
      // sound "better" in a side-by-side comparison.
      const makeup = new GainNode(ctx, { gain: 1 });
      const volumeNode = new GainNode(ctx, { gain: 1 });
      // The compressor adds its own make-up gain: on loud phrases the chain went past full scale and
      // clipped (a rasp at high volume). A brick-wall limiter last keeps it just under.
      const limiter = new DynamicsCompressorNode(ctx, { threshold: -2, knee: 0, ratio: 20, attack: 0.002, release: 0.08 });
      highpass.connect(presence).connect(deEsser).connect(comp).connect(makeup).connect(volumeNode).connect(limiter);
      limiter.connect(ctx.destination);
      let source = null;
      return {
        on: false,
        attach(stream) {
          if (source) source.disconnect();
          source = ctx.createMediaStreamSource(stream);
          source.connect(highpass);
          ctx.resume().catch(() => {});
          this.on = true;
        },
        detach() {
          if (source) source.disconnect();
          source = null;
          this.on = false;
        },
        setVolume(v) { volumeNode.gain.setTargetAtTime(v, ctx.currentTime, 0.02); },
        setSink,
      };
    } catch {
      ctx.close().catch(() => {});
      return null;
    }
  }

  async function toggleCleanVoice() {
    cleanVoice = $("cleanVoice").checked;
    store.set("va-clean", cleanVoice ? "1" : "0");
    const stream = $("remote").srcObject;
    if (cleanVoice) {
      const f = await ensureFx();
      if (!f) { addLine("system", "Цей браузер не підтримує обробку звуку."); $("cleanVoice").checked = cleanVoice = false; return; }
      if (stream) f.attach(stream);
    } else if (fx) {
      fx.detach();
    }
    setRemoteVolume(remoteLevel);
  }
  // Her voice for the current call.
  function attachVoice(stream) {
    if (stream) playRemote(stream);
  }

  const bargeLevel = () => (barge.stage === "muted" ? 0 : barge.stage === "ducked" ? 0.5 : 1);

  function bargeReset() {
    bargeClear();
    clearTimeout(barge.releaseTimer);
    barge.stage = "idle";
    barge.heard = "";
    barge.speaking = false;
    barge.audible = false;
    setRemoteVolume(1);
  }

  function steer(kind) {
    const content = {
      // A stop word is often followed by what they want instead («Stop, stop. Explain it to a kid»):
      // «say only Добре and wait» made her answer «Добре.» and nothing else.
      ack: "Stop your previous answer immediately. Do not continue or resume it. If the user goes on with a question or request, answer that. If they only told you to stop, say at most one short «Добре.» and wait silently.",
      // Prod: she once said this aloud — «Стоп, слухаю тебе.»
      stop: "Stop speaking immediately. The user is talking. Do not finish or resume your previous sentence, and do not say that you stop or listen — just go silent. Listen and answer what they say.",
      pause: "The user said «Дякую, Єва»: stop speaking immediately and stay completely silent. Do not reply to it. You will be woken again later — then continue with full memory of this conversation.",
    }[kind];
    try {
      if (channel && channel.readyState === "open") {
        channel.send(JSON.stringify({
          type: "session.instructions.append",
          content,
          delegation_id: null,
          event_id: "barge_" + Math.random().toString(16).slice(2, 10),
        }));
      }
    } catch { /* channel closing */ }
  }

  function bargeConfirm(kind) {
    if (barge.stage === "muted") return;
    bargeClear();
    barge.stage = "muted";
    barge.mutes += 1;
    setRemoteVolume(0);
    steer(kind);
    scheduleRelease();
  }

  // Unmute once the interrupted answer has stopped (assistant quiet) and the user is done, or
  // at the latest 1.5 s after the user stopped — the model may go straight into a new reply.
  function scheduleRelease() {
    clearTimeout(barge.releaseTimer);
    if (barge.stage !== "muted") return;
    if (barge.speaking) return; // re-checked when the user stops
    const sinceSpeech = Date.now() - barge.speechEndedAt;
    if (!barge.audible || sinceSpeech > 1500) {
      barge.stage = "idle";
      barge.heard = "";
      setRemoteVolume(1);
      return;
    }
    barge.releaseTimer = setTimeout(scheduleRelease, 100);
  }

  function onGateSignal(signal) {
    if ("stats" in signal) { gateStats = signal.stats; return; } // ?debug=1 only
    bargeOnGate(signal);
    if ("audible" in signal) eva.lastAudibleAt = Date.now(); // when she started / stopped sounding
    if (barge.audible) eva.thinking = false;
    showState();
  }

  function bargeOnGate(signal) {
    if ("audible" in signal) {
      barge.audible = signal.audible;
      if (barge.stage === "muted") scheduleRelease();
      return;
    }
    barge.speaking = signal.speaking;
    if (signal.speaking) {
      clearTimeout(barge.releaseTimer);
      if (barge.stage !== "idle" || !barge.audible || herBackchannel()) return;
      if (Date.now() - barge.speechEndedAt > 1500) barge.heard = "";
      barge.timers.push(setTimeout(() => {
        if (barge.speaking && barge.stage === "idle") { barge.stage = "ducked"; barge.ducks += 1; setRemoteVolume(0.5); }
      }, 400));
      // A quiet voice over her on speakers: the gate rightly keeps it below her echo, so its words never
      // reach the model and nothing confirms the barge-in — she went on and the user felt ignored.
      // Someone who keeps talking to her for 1.5 s with none of their own words heard (only her echo,
      // or nothing) wants the floor; once she stops, the rest of what they say gets through. Their
      // own words, an «угу» included, are judged by bargeOnWords as before.
      barge.timers.push(setTimeout(() => {
        if (barge.speaking && barge.stage === "ducked" && !ownWords().length) bargeConfirm("stop");
      }, 1500));
    } else {
      barge.speechEndedAt = Date.now();
      if (barge.stage === "muted") { scheduleRelease(); return; }
      bargeClear();
      if (barge.stage === "ducked") {
        // A short sound: wait briefly for its transcript, else it was a cough / «угу».
        barge.timers.push(setTimeout(() => {
          if (barge.stage === "ducked" && !barge.speaking) { barge.stage = "idle"; barge.heard = ""; setRemoteVolume(1); }
        }, 400));
      }
    }
  }

  // User transcript while the assistant talks: decide by the words, not just the sound.
  // The user's words since this barge-in started; words she herself just said are her echo, not the
  // user — stop words included.
  function ownWords() {
    const echo = new Set(splitWords(barge.recentAssistant));
    return splitWords(barge.heard).filter((w) => !echo.has(w));
  }

  function bargeOnWords(delta) {
    if (barge.stage === "muted" || !(barge.audible || barge.stage === "ducked")) return;
    if (herBackchannel() && !splitWords(delta).some((w) => STOP_WORDS.has(w))) return;
    // Words count only while the near voice is on (or just ended — transcripts lag); words
    // heard with no one at the mic are background talk.
    if (!barge.speaking && Date.now() - barge.speechEndedAt > 1200) return;
    barge.heard += delta;
    const own = ownWords();
    if (own.some((w) => STOP_WORDS.has(w))) { bargeConfirm("ack"); return; }
    const meaningful = own.filter((w) => !BACKCHANNEL.has(w));
    if (meaningful.length >= 3) bargeConfirm("stop");
  }

  // A stop word the user said — not one in her own recent words (the recogniser hears her too).
  function stopWordOverHer(text) {
    const echo = new Set(splitWords(barge.recentAssistant));
    return splitWords(text).some((w) => STOP_WORDS.has(w) && !echo.has(w));
  }

  function bargeOnAssistantText(delta) {
    barge.recentAssistant = (barge.recentAssistant + delta).slice(-300);
    if (Date.now() - barge.burstAt > 800) barge.burst = "";
    barge.burst += delta;
    barge.burstAt = Date.now();
  }

  // She is only saying «угу» / «так-так» / «ммм» while the user talks (voice/interrupt_intent.py
  // is_backchannel_utterance): that overlap is the point, not an answer to barge into.
  function herBackchannel() {
    const words = splitWords(barge.burst);
    return words.length > 0 && words.length <= 3 && words.every((w) => BACKCHANNEL.has(w));
  }

  function onServerEvent(raw) {
    let event;
    try { event = JSON.parse(raw); } catch { return; }
    switch (event.type) {
      case "session.started":
        applyStart();
        break;
      case "session.input_transcript.delta":
        if (eva.mode === "waiting") break;
        appendTranscript("user", event.delta);
        eva.lastUserAt = Date.now();
        eva.recentUser = (eva.recentUser + event.delta).slice(-80);
        checkVoiceRequest(event.delta);
        // «Дякую, Єва» — also while she is talking: cut her off and pause.
        if (isStop(eva.recentUser)) { pauseEva(); break; }
        eva.thinking = true;
        clearTimeout(eva.thinkTimer);
        eva.thinkTimer = setTimeout(() => { eva.thinking = false; showState(); }, 15000);
        bargeOnWords(event.delta);
        touchActivity();
        showState();
        break;
      case "session.output_transcript.delta":
        if (eva.mode === "waiting") break; // whatever she says to «Дякую, Єва» is not played
        appendTranscript("assistant", event.delta);
        bargeOnAssistantText(event.delta);
        eva.inputBuf = ""; // she answers: the user's turn is over
        eva.thinking = false;
        touchActivity();
        break;
      case "session.closed":
        onSessionClosed();
        break;
      case "session.delegation.created":
        // A request went to the backend: if it is «що ти бачиш», the frame is on the server before
        // the backend has even decided to look (the tool takes any frame since the work started).
        snapForLook("");
        break;
      case "response.event": {
        // The backend is about to look through the camera: snap what is in front of it right now.
        const inner = event.event || {};
        const item = inner.item || {};
        if ((inner.type === "response.output_item.added" || inner.type === "response.output_item.done")
            && item.type === "function_call" && item.name === "look_at_camera") snapForLook(item.call_id || item.id);
        break;
      }
      case "error": {
        const message = (event.error && event.error.message) || "невідома";
        // A voice switch closes the call while the page may still be sending: not a real error.
        if (!/session is closing/i.test(message)) addLine("system", "Помилка: " + message);
        break;
      }
      default:
        break;
    }
  }

  // Every start() gets a number; a teardown (or a newer start) makes the old one stale, so an
  // attempt that is still awaiting the mic / backend never touches the new call's state.
  let attempt = 0;
  let dropTimer = null;
  let idleTimer = null;
  // GPT-Live bills every second a session is open, muted or not ($0.05/min), so a forgotten call
  // pauses after this long with nobody speaking… (config.js: IDLE_PAUSE_SECONDS)
  const CFG = window.APP_CONFIG || {};
  const IDLE_LIMIT_MS = (CFG.IDLE_PAUSE_SECONDS ?? 90) * 1000;
  // …and a pause this long closes the Live session; «Єва, скажи» reopens it with the history.
  // (config.js: PAUSE_CLOSE_SECONDS)
  const PAUSE_CLOSE_MS = (CFG.PAUSE_CLOSE_SECONDS ?? 30) * 1000;
  // A pause never keeps a call open longer than this, even while the server asks to.
  const PAUSE_HOLD_MAX_MS = 5 * 60 * 1000;

  // ── call meter: minutes and approximate cost from «Почати розмову» to «Завершити» ──
  // GPT-Live bills every second a session is open, so this adds up every open session: the call,
  // a pause's first PAUSE_CLOSE_MS, the standby call of a voice switch. A session counts from the
  // backend's answer (it exists then) until the page closes it. (config.js: PRICE_PER_MINUTE_USD)
  const PRICE_PER_MIN = Number(CFG.PRICE_PER_MINUTE_USD ?? 0.05);
  const meter = { doneMs: 0, open: new Map(), timer: null };

  function meterReset() {
    meter.doneMs = 0;
    meter.open.clear();
    showMeter();
  }

  function meterOpen(peer) {
    if (peer.signalingState === "closed" || meter.open.has(peer)) return;
    meter.open.set(peer, Date.now());
    if (!meter.timer) meter.timer = setInterval(showMeter, 1000);
    showMeter();
  }

  // Every call is closed here, so the meter stops counting it.
  function closePeer(peer) {
    const since = meter.open.get(peer);
    if (since !== undefined) {
      meter.doneMs += Date.now() - since;
      meter.open.delete(peer);
    }
    peer.close();
    if (!meter.open.size) { clearInterval(meter.timer); meter.timer = null; }
    showMeter();
  }

  function showMeter() {
    const now = Date.now();
    let ms = meter.doneMs;
    meter.open.forEach((since) => { ms += now - since; });
    $("meter").hidden = ms <= 0;
    if (ms <= 0) return;
    const sec = Math.floor(ms / 1000);
    const used = Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0") +
      " · ≈ $" + ((ms / 60000) * PRICE_PER_MIN).toFixed(2);
    $("meter").textContent = meter.open.size ? "Дзвінок: " + used
      : eva.mode === "off" ? "Останній дзвінок: " + used
      : "Дзвінок: " + used + " · на паузі не рахується";
  }

  function touchActivity() {
    clearTimeout(idleTimer);
    if (!pc || eva.mode !== "active") return;
    idleTimer = setTimeout(() => {
      if (eva.mode !== "active") return;
      addLine("system", "Тиша вже " + Math.round(IDLE_LIMIT_MS / 1000) + " с — ставлю Єву на паузу, щоб не витрачати хвилини. Вона все пам'ятає.");
      pauseEva();
    }, IDLE_LIMIT_MS);
  }

  function sendEvent(event) {
    try {
      if (channel && channel.readyState === "open") { channel.send(JSON.stringify(event)); return true; }
    } catch { /* channel closing */ }
    return false;
  }

  const eventId = (prefix) => prefix + "_" + Math.random().toString(16).slice(2, 10);
  // Context the model acts on right away (it answers it out loud).
  const commentary = (content) =>
    sendEvent({ type: "session.commentary.append", content, delegation_id: null, event_id: eventId("say") });

  // «Почати розмову»: the call opens paused and she waits for «Привіт, Єва» — she spoke first on her
  // own, and the user wants to start the talk. Testers could not tell whether she was on at all
  // (feedback: «при запуску … мовчить»), so the status and the log say she is on and waiting. Without
  // the phrase listener (Firefox) nothing could wake her: she greets at once.
  const defaultStart = () => (eva.wakeSupported ? { paused: true, ready: true } : { greet: true, hello: true });

  // opts: paused — connect and wait for «Єва, скажи»; ask — a request said with the wake phrase;
  // greet — a short «Слухаю» unless the user starts talking; switched — first words in a new voice.
  async function start(opts = {}) {
    if (!BACKEND) {
      addLine("system", "Не вказано адресу бекенду (web/config.js або ?backend=…).");
      return;
    }
    const my = ++attempt;
    eva.pending = opts;
    if (eva.mode !== "switching") eva.mode = "connecting";
    showState();
    updateButtons();
    try {
      // A voice switch / new conversation keeps the microphone open: no new permission, faster.
      const live = mic && mic.getAudioTracks().some((t) => t.readyState === "live");
      const stream = live ? mic : await openMic();
      if (my !== attempt) { if (stream !== mic) stream.getTracks().forEach((t) => t.stop()); return; }
      if (stream !== mic) listDevices(); // device names are known once the mic is allowed
      mic = stream;
      const peer = new RTCPeerConnection();
      pc = peer;
      peer.ontrack = (e) => attachVoice(e.streams[0]);
      // A voice switch keeps the gate too (it is built on the same mic): no new AudioContext.
      if (gate && (gate.stream !== mic || !gate.alive())) { gate.close(); gate = null; }
      if (!gate) {
        const newGate = await createNoiseGate(mic, onGateSignal);
        if (my !== attempt) { if (newGate) newGate.close(); return; }
        gate = newGate;
      }
      const track = gate ? gate.track : mic.getAudioTracks()[0];
      track.enabled = true;
      micSender = peer.addTrack(track, mic);
      channel = peer.createDataChannel("oai-events");
      wireCall(peer, channel);

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      sessionVoice = voice;
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(sessionBody({ sdp: withClearOpus(offer.sdp), user_text: opts.userText || undefined })),
      });
      if (my !== attempt) return;
      if (res.status === 401) {
        showGate();
        throw new Error("Потрібен код доступу");
      }
      if (!res.ok) throw new Error("Сервер відповів " + res.status);
      const { sdp } = await res.json();
      if (my !== attempt) return;
      meterOpen(peer);
      await peer.setRemoteDescription({ type: "answer", sdp });
    } catch (err) {
      if (my !== attempt) return;
      const msg = err && err.name === "NotAllowedError" ? "Немає доступу до мікрофона" : (err.message || String(err));
      addLine("system", msg);
      stop("Не вдалося підключитись", "err");
    }
  }

  // What a new call is opened with: voice and delivery.
  const sessionBody = (extra) => Object.assign({
    voice: voice || undefined, speed, style, prompt: promptVariant,
  }, extra);

  // The offer's Opus line says how we want to receive her voice. Browser defaults leave the bitrate
  // to the sender, and a low one smears «с», «ш», «ц» (they live at 4–12 kHz) — she sounded lisped.
  // Only the copy sent to the server is changed; the local description stays as the browser made it.
  function withClearOpus(sdp) {
    const pt = (sdp.match(/a=rtpmap:(\d+) opus\/48000/i) || [])[1];
    if (!pt) return sdp;
    const want = { maxplaybackrate: "48000", maxaveragebitrate: "64000", useinbandfec: "1", stereo: "0" };
    return sdp.replace(new RegExp("a=fmtp:" + pt + " ([^\\r\\n]*)"), (line, params) => {
      const kept = params.split(";").filter((p) => p && !(p.split("=")[0].trim() in want));
      return "a=fmtp:" + pt + " " + kept.concat(Object.entries(want).map(([k, v]) => k + "=" + v)).join(";");
    });
  }

  // The current call's connection and server events (a standby call gets them when swapped in).
  function wireCall(peer, ch) {
    peer.onconnectionstatechange = () => {
      if (pc !== peer) return;
      const state = peer.connectionState;
      clearTimeout(dropTimer);
      // The server ends the call itself to restart it in a new voice: check before giving up.
      if (state === "failed" || state === "closed") onSessionClosed("З'єднання втрачено", "err");
      // "disconnected" is often a network blip that recovers by itself (Wi-Fi switch etc.).
      else if (state === "disconnected") {
        setStatus("Зв'язок перервався — відновлюю…", "busy");
        dropTimer = setTimeout(() => { if (pc === peer) onSessionClosed("З'єднання втрачено", "err"); }, 8000);
      } else if (state === "connected") showState();
    };
    ch.onmessage = (e) => { if (pc === peer) onServerEvent(e.data); };
    ch.onclose = () => { if (pc === peer && eva.mode !== "connecting" && eva.mode !== "switching") onSessionClosed(); };
  }

  // Sent as silent context (session.thinking.append): as a commentary, which is speakable, she once said
  // part of it aloud («користувач змінив ваш голос»). Measured on GPT-Live: from a silent note she carries
  // on the interrupted sentence just as well (9/9 runs, no note read out). Kept short: notes are ≤500 tokens.
  const continueInNewVoice = (said) => "Your voice was just changed while you were saying: «" + said.slice(-300) +
    "». Continue exactly that answer in the new voice from where it stopped (its last few words may not have been heard) — do not start it over, do not mention the voice change.";

  // session.started: the call is up — pause, or talk.
  function applyStart() {
    const opts = eva.pending || {};
    eva.pending = null;
    if (opts.paused) { enterPause(false, opts.ready); return; }
    eva.mode = "active";
    setRemoteVolume(1);
    startPhraseListener();
    sendFrame(); // frames stop on a pause: the server gets a fresh one at once
    if (opts.switched) {
      // A new voice is a new session: carry on as if nothing happened — no «тепер я іншим голосом».
      // Asked by voice: nothing to say — she just listens on in the new voice.
      if (opts.interrupted) thinking(continueInNewVoice(opts.interrupted));
    } else if (opts.ask) {
      askWoken(opts.ask, opts.note);
    } else if (opts.greet) {
      // A fresh call after a wake: the user finished the phrase seconds ago — greet at once.
      greetIfQuiet(opts.heard, opts.note, opts.fresh ? 0 : 700, opts.hello ? START_GREETING : "");
    }
    showState();
    updateButtons();
    touchActivity();
  }

  // Close the call but keep Єва's mode (pause, voice switch); stop() also turns her off.
  // keepMic: a new session follows right away (voice switch, new conversation).
  function teardown({ keepMic = false } = {}) {
    attempt += 1;
    clearTimeout(dropTimer);
    clearTimeout(idleTimer);
    sendEvent({ type: "session.close" });
    if (pc) { closePeer(pc); pc = null; }
    micSender = null;
    if (gate && keepMic) gate.listenTo(null);
    else if (gate) { gate.close(); gate = null; }
    if (mic && !keepMic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    channel = null;
    closeBubbles();
    bargeReset();
    barge.recentAssistant = "";
  }

  function stop(text = "Не підключено", state = "") {
    dropStandby();
    teardown();
    stopPhraseListener();
    if (cam) { $("camera").checked = false; stopCamera(); } // the call is over: so is the camera
    clearTimeout(eva.pauseTimer);
    eva.mode = "off";
    eva.ready = false;
    eva.pending = null;
    setStatus(text, state);
    updateButtons();
    refreshMe(); // picks up a voice the agent changed by voice during the call
  }

  // ── pause («Дякую, Єва») / wake («Єва, скажи») ──
  // ready: the call was just started — she is on and waits to be greeted, not paused.
  function enterPause(announce, ready = false) {
    eva.mode = "waiting";
    if (ready && !eva.ready) addLine("system", "Єва на зв'язку. Скажіть «Привіт, Єва» — і говоріть.");
    eva.ready = ready; // kept across a reconnect during the wait (voice switch): no second line
    eva.recentUser = "";
    eva.thinking = false;
    clearTimeout(idleTimer);
    sendEvent({ type: "session.input_audio.mute", event_id: eventId("mute") }); // the model hears nothing
    bargeReset(); // and nothing it says is played while waiting (see setRemoteVolume)
    closeBubbles();
    if (announce) {
      addLine("system", eva.wakeSupported
        ? (bgListen
          ? "Пауза. Єва слухає фоном і запам'ятовує, про що говорять поруч, — зможе нагадати. Скажіть «Єва, скажи», щоб продовжити."
          : "Пауза. Скажіть «Єва, скажи», щоб продовжити — Єва все пам'ятає.")
        : "Пауза. Натисніть «Продовжити» — Єва все пам'ятає.");
    }
    clearTimeout(eva.pauseTimer);
    schedulePauseClose(Date.now());
    startPhraseListener();
    showState();
    updateButtons();
  }

  // The paused call is closed after PAUSE_CLOSE_MS — unless a tool is still running or Єва waits
  // for «так/ні» (that confirmation belongs to this session): then ask again a bit later.
  function schedulePauseClose(pausedAt) {
    clearTimeout(eva.pauseTimer);
    eva.pauseTimer = setTimeout(async () => {
      if (eva.mode !== "waiting" || !pc) return;
      if (Date.now() - pausedAt < PAUSE_HOLD_MAX_MS) {
        const me = await fetchMe(); // offline: close as usual
        if (eva.mode !== "waiting" || !pc) return;
        if (me && me.keep_session) { schedulePauseClose(pausedAt); return; }
      }
      teardown({ keepMic: true }); // the wake phrase listener needs it anyway; the next wake skips getUserMedia
    }, PAUSE_CLOSE_MS);
  }

  function pauseEva() {
    if (eva.mode !== "active") return; // both recognisers may report the same «Дякую, Єва»
    steer("pause");
    enterPause(true);
  }

  function sendOverheard(text) {
    return fetch(BACKEND + "/api/overheard", { method: "POST", headers: headers(), body: JSON.stringify({ text }) })
      .catch(() => {});
  }

  // Held a moment before it is kept: the recogniser splits speech into several results, so the user's
  // own request can come apart from the «Єва, скажи» that names her — prod: «…розкажи мені про
  // Монте-Крісто» was kept as background and she said «хтось поруч теж згадав Монте-Крісто».
  const OVERHEARD_HOLD_MS = 4000;
  const overheardPending = [];
  function postOverheard(text) {
    const entry = { text };
    entry.timer = setTimeout(() => {
      overheardPending.splice(overheardPending.indexOf(entry), 1);
      sendOverheard(text);
    }, OVERHEARD_HOLD_MS);
    overheardPending.push(entry);
  }

  // At a wake: a held piece that shares the request's words is the user's own lead-in — dropped; the
  // rest is kept now, before the digest is read.
  const contentWords = (text) => evaTokens(text).filter((w) => w.length >= 4 && !EVA_NAME_FORMS.has(w));
  function flushOverheard(request) {
    const asked = new Set(contentWords(request));
    const sent = overheardPending.splice(0).map((entry) => {
      clearTimeout(entry.timer);
      const words = contentWords(entry.text);
      const shared = words.filter((w) => asked.has(w)).length;
      if (shared >= 2 || (shared && shared === words.length)) return null;
      return sendOverheard(entry.text);
    });
    return Promise.all(sent);
  }

  // What she heard in the background during the pause, as a commentary ("" if nothing).
  async function overheardNote(flushed) {
    if (!bgListen) return "";
    try {
      await flushed;
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 4000);
      const res = await fetch(BACKEND + "/api/overheard/digest", { method: "POST", headers: headers(), signal: ctrl.signal });
      clearTimeout(timer);
      return res.ok ? (await res.json()).commentary || "" : "";
    } catch { return ""; }
  }

  function showBgListen() {
    $("bgListen").checked = bgListen;
    $("bgListenInfo").textContent = bgListen
      ? "Увімкнено: Єва запам'ятовує, про що говорять поруч, і може нагадати."
      : "Вимкнено: на паузі Єва нічого не запам'ятовує.";
  }

  function toggleBgListen() {
    bgListen = $("bgListen").checked;
    store.set("va-bg-listen", bgListen ? "1" : "0");
    showBgListen();
    showState();
    // Turned off: forget what was already heard, it must not reach her.
    if (!bgListen) fetch(BACKEND + "/api/overheard", { method: "DELETE", headers: headers() }).catch(() => {});
    addLine("system", bgListen
      ? "На паузі Єва слухатиме фоном і запам'ятовуватиме, про що говорять поруч."
      : "Фонове слухання вимкнено: на паузі Єва нічого не запам'ятовує.");
  }

  // The model was muted and never heard the phrase: hand it what followed «Єва, скажи», together
  // with what she overheard during the pause (fetched while the call connects, not before it).
  const withNote = (note, text) => (note ? note + "\n\n" + text : text);
  const thinking = (content) =>
    sendEvent({ type: "session.thinking.append", content, delegation_id: null, event_id: eventId("think") });

  // What followed «Єва, скажи» was never heard by the model (it was muted), and GPT-Live never delegates
  // a request handed over as a commentary (it made up a room with a laptop and a cup for «що ти бачиш»).
  // So it goes to the backend as the user's message, with a silent note — woken_request_note in
  // prompts/live_prompt.py says why; keep the wording the same (tests/test_wake_request.py checks).
  // «Зміни голос …» is the page's own.
  function askBackend(text) {
    thinking("The user woke you with: «" + text + "». It has gone to the backend as their message, and the " +
      "backend handles it the usual way, with its tools (camera, web) — nothing has been looked at or searched " +
      "yet. You, the voice: do not delegate it again; when the backend's answer comes, tell it in your own " +
      "words. Never voice their words or invent what they said.");
    sendEvent({ type: "response.item.create", event_id: eventId("ask"),
      item: { type: "message", role: "user", content: [{ type: "input_text", text }] } });
    sendEvent({ type: "response.create", event_id: eventId("go") });
    eva.thinking = true;
    clearTimeout(eva.thinkTimer);
    eva.thinkTimer = setTimeout(() => { eva.thinking = false; showState(); }, 15000);
  }

  // note: what she overheard during the pause (a promise) — silent context, never waited for.
  function askWoken(text, note) {
    if (VOICE_REQUEST_RE.test(text)) requestVoiceChange(text);
    else askBackend(text);
    Promise.resolve(note).then((n) => { if (n) thinking(n); });
  }

  function wakeEva(rest, heard = "") {
    if (eva.mode !== "waiting") return;
    clearTimeout(eva.pauseTimer);
    const note = overheardNote(flushOverheard((heard || "") + " " + (rest || ""))); // a promise: never delays the wake
    if (rest) addLine("user", "Єва, скажи, " + rest);
    if (pc && channel && channel.readyState === "open") {
      eva.mode = "active";
      sendEvent({ type: "session.input_audio.unmute", event_id: eventId("unmute") });
      setRemoteVolume(1);
      sendFrame();
      if (rest) askWoken(rest, note);
      else greetIfQuiet(heard, note);
      showState();
      updateButtons();
      touchActivity();
    } else {
      // The pause closed the call: reopen it right away. What they asked goes into the new call's
      // history as their own message — quoted in a commentary, GPT-Live sometimes read it back.
      start({ userText: rest ? "Єва, скажи, " + rest : "", ask: rest, greet: !rest, heard, note, fresh: true });
    }
  }

  // Same wording as WAKE_GREETING in voice/live_driver.py.
  const wakeGreeting = (phrase) =>
    "The user just called you: «" + phrase + "». You are back and listening — let them hear it. " +
    "Reply right away in two to four words that match it: to «привіт», «вітаю», «гей» greet back " +
    "warmly («Привіт! Що робимо?», «О, привіт! Слухаю»), otherwise a short «Так, слухаю» or " +
    "«Слухаю тебе». Vary it, then wait.";

  // A greeting tells the user Єва is on. Skipped only when they already went on talking (the model
  // answers that) or she is already speaking; a noise at the wrong moment just delays it a little.
  // note: what she overheard during the pause — delivered with the greeting, or alone if skipped.
  // note: a string or a promise of one (overheardNote) — never waited for before the greeting.
  const START_GREETING = "The user just pressed «Почати розмову»: the call is open. Greet them in a few " +
    "words so they hear you are on and listening — a short hello and that they can just talk; vary it, " +
    "no question about how you can help. Then wait.";

  // text: what to say instead of the wake greeting (START_GREETING when the call was just started).
  function greetIfQuiet(heard = "", note = "", delayMs = 700, text = "") {
    const wokeAt = Date.now();
    const phrase = heard || "Єва, скажи";
    const giveUpAt = wokeAt + 4000;
    const tryGreet = async () => {
      if (eva.mode !== "active") return;
      const n = await Promise.resolve(note);
      if (eva.mode !== "active") return;
      if (eva.lastUserAt > wokeAt || barge.audible) { if (n) commentary(n); return; }
      if (barge.speaking && Date.now() < giveUpAt) { setTimeout(tryGreet, 300); return; }
      commentary(withNote(n, text || wakeGreeting(phrase)));
    };
    setTimeout(tryGreet, delayMs);
  }

  // The browser's own recogniser (free; Chrome, Edge, Safari) runs the whole time Єва is on: it
  // hears «Єва, скажи» while the model is muted, and «Дякую, Єва» during the conversation too —
  // a second pair of ears, as GPT-Live's transcript sometimes drops the name («Дякую»). It stops
  // by itself after a while of silence, so it is restarted.
  function startPhraseListener() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR || !eva.wakeSupported || eva.recognizer) return;
    let rec;
    try { rec = new SR(); } catch { eva.wakeSupported = false; showState(); updateButtons(); return; }
    rec.lang = "uk-UA";
    rec.continuous = true;
    rec.interimResults = true; // «Дякую, Єва» cuts her off as soon as it is heard
    rec.maxAlternatives = 3;
    let retryMs = 300;
    let stopResult = -1; // the result a stop word already cut her off for (interim results repeat)
    rec.onresult = (e) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        for (let k = 0; k < result.length; k++) {
          const text = result[k].transcript;
          if (eva.mode === "active" && isStop(text)) { pauseEva(); return; }
          // «Стоп» / «зачекай» over her: on speakers the gate keeps a normal voice below her echo, so
          // neither the model nor the page heard it and she talked on through long answers. This
          // recogniser hears it anyway; GPT-Live stops on the steer (measured: ~0.8 s, also mid-way
          // through a delegated answer). Her own words are echo, not a stop.
          if (eva.mode === "active" && k === 0 && i !== stopResult && (barge.audible || barge.stage === "ducked")
              && stopWordOverHer(text)) {
            stopResult = i;
            barge.speechEndedAt = Date.now(); // keep her muted until she has actually stopped
            bargeConfirm("ack");
            return;
          }
          // Voice change: only the recogniser's best guess (k = 0), and not while Єва is talking —
          // this recogniser has no echo cancellation; GPT-Live's own transcript covers that case.
          if (eva.mode === "active" && result.isFinal && k === 0 && !barge.audible && VOICE_REQUEST_RE.test(text)) {
            requestVoiceChange(text);
            return;
          }
          // ?debug=1: what the browser heard during the pause — shows why a wake phrase was missed.
          if (DEBUG && eva.mode === "waiting" && result.isFinal) addLine("system", "браузер почув: «" + text.trim() + "»");
          // Wake on the final text only: it carries the whole request after «Єва, скажи».
          if (eva.mode === "waiting" && result.isFinal) {
            const rest = matchWake(text);
            if (rest !== null) { wakeEva(rest, text.trim()); return; }
          }
        }
        // Paused, not switched off: what is said nearby is kept for her (voice/background.py).
        // Not a phrase that names her: a wake phrase the recogniser mangled is the user, not background.
        if (bgListen && eva.mode === "waiting" && result.isFinal && !isStop(result[0].transcript)
            && !evaTokens(result[0].transcript).some((w) => EVA_NAME_FORMS.has(w))) {
          postOverheard(result[0].transcript);
        }
      }
    };
    rec.onerror = (e) => {
      if (["not-allowed", "service-not-allowed", "language-not-supported"].includes(e.error)) {
        eva.wakeSupported = false;
        addLine("system", "Браузер не дає слухати «Єва, скажи» — натискайте «Продовжити».");
        showState();
        updateButtons();
      } else if (e.error === "network") {
        retryMs = 3000;
      }
    };
    rec.onend = () => {
      if (eva.recognizer !== rec) return;
      eva.recognizer = null;
      if (eva.mode !== "off" && eva.wakeSupported) setTimeout(startPhraseListener, retryMs);
    };
    eva.recognizer = rec;
    // A mic picked in «Пристрої»: browsers that take a track (newer Chrome) listen to it, older ones
    // ignore the argument and keep the system default.
    const track = micId && mic ? mic.getAudioTracks().find((t) => t.readyState === "live") : null;
    try { rec.start(...(track ? [track] : [])); } catch {
      try { if (track) rec.start(); else eva.recognizer = null; } catch { eva.recognizer = null; }
    }
  }

  function stopPhraseListener() {
    const rec = eva.recognizer;
    eva.recognizer = null;
    if (rec) { rec.onend = null; try { rec.abort(); } catch { /* already stopped */ } }
  }

  // GPT-Live voices are fixed per session: a new voice (or a fresh start) is a new session that
  // the server seeds with the conversation so far, so Єва remembers everything.
  async function restartSession(mode, opts) {
    const paused = eva.mode === "waiting";
    if (eva.mode === "off" || eva.mode === "connecting") return;
    if (paused && !pc) return; // closed during a pause: the next «Єва, скажи» opens it fresh
    dropStandby();
    clearTimeout(eva.pauseTimer);
    teardown({ keepMic: true });
    eva.mode = mode;
    await start(paused ? { paused: true, ready: eva.ready } : opts);
  }

  // The server closed the call: after change_voice (asked by voice) to restart it in the new
  // voice — otherwise it really ended.
  async function onSessionClosed(text = "Розмову завершено", state = "") {
    if (eva.mode === "switching" || eva.mode === "off" || eva.mode === "connecting") return;
    const wasPaused = eva.mode === "waiting";
    dropStandby();
    teardown({ keepMic: true });
    eva.mode = "connecting";
    showState();
    const me = await fetchMe();
    if (eva.mode !== "connecting") return; // the user did something meanwhile
    if (me && me.reconnect && voices.some((x) => x.id === me.voice)) {
      voice = me.voice;
      store.set("va-voice", voice);
      showVoice();
      addLine("system", "Голос змінено на «" + voiceLabel(voice) + "»" + (me.voice_note ? " (" + me.voice_note + ")" : "") + ".");
      eva.mode = "switching";
      await start(wasPaused ? { paused: true, ready: eva.ready } : { switched: true });
      return;
    }
    if (gate) { gate.close(); gate = null; }
    if (mic) { mic.getTracks().forEach((t) => t.stop()); mic = null; }
    if (wasPaused) { eva.mode = "waiting"; startPhraseListener(); showState(); updateButtons(); return; }
    stop(text, state);
  }

  // ── voice settings ──
  const voiceLabel = (id) => (voices.find((x) => x.id === id) || { label: id }).label;

  // The picker may show a voice that isn't applied yet (to listen to it first).
  function showVoice(keepSelection = false) {
    if (!keepSelection) $("voice").value = voice;
    const picked = voices.find((x) => x.id === $("voice").value);
    $("voiceInfo").textContent = picked ? picked.description : "";
    $("voiceApply").hidden = !picked || picked.id === voice;
  }

  async function loadVoices() {
    try {
      const res = await fetch(BACKEND + "/api/voices");
      if (!res.ok) return;
      const data = await res.json();
      voices = data.voices || [];
      voice = store.get("va-voice") || "";
      if (!voices.some((x) => x.id === voice)) voice = data.default;
      if (!voices.some((x) => x.id === voice) && voices.length) voice = voices[0].id;
      const group = (label, list) => {
        const g = document.createElement("optgroup");
        g.label = label;
        g.append(...list.map((x) => new Option(x.label, x.id)));
        return g;
      };
      $("voice").replaceChildren(
        group("Жіночі", voices.filter((x) => x.feminine)),
        group("Чоловічі", voices.filter((x) => !x.feminine)),
      );
      $("voice").disabled = !voices.length;
      $("voicePreview").disabled = !voices.length;
      showVoice();
    } catch { /* backend offline: picker stays disabled */ }
  }

  // What Єва is saying right now (the transcript runs slightly ahead of the audio).
  const speakingNow = () => (barge.audible && openMsg.assistant ? openMsg.assistant.textContent.trim() : "");

  async function applyVoice() {
    const id = $("voice").value;
    if (!voices.some((x) => x.id === id) || id === voice) return;
    voice = id;
    store.set("va-voice", id);
    showVoice();
    // Not awaited: the new call carries the voice itself (/api/session), this only keeps the server
    // in step (it remembers the voice).
    fetch(BACKEND + "/api/voice", { method: "POST", headers: headers(), body: JSON.stringify({ voice: id }) }).catch(() => {});
    if (eva.mode === "active") {
      const later = !voiceNow && answering();
      addLine("system", later
        ? "Голос зміниться на «" + voiceLabel(id) + "», щойно Єва договорить."
        : "Голос змінено на «" + voiceLabel(id) + "».");
      switchVoiceFast(later);
    } else if (eva.mode === "waiting") {
      addLine("system", "Голос змінено на «" + voiceLabel(id) + "».");
      restartSession("switching", { switched: true }); // paused: she is silent, nothing to continue
    }
  }

  // ── fast voice switch ──
  // Opening a GPT-Live session takes seconds and its voice is fixed, so the call in the new voice
  // opens beside the current one — it hears nothing and plays nothing — while Єва goes on talking.
  // Once it is up it takes over the mic and the speaker («Одразу»), or waits until she has finished
  // («Після відповіді»). What the old call said meanwhile is handed over, so nothing is lost.
  let standby = null;
  let voiceTimer = null;
  const STANDBY_WAIT_MS = 60 * 1000; // a ready call waiting for her to finish — longer: start over
  // A swap doesn't derail her (unlike speed/style), so a short silence is enough: her answer is over.
  const SWAP_QUIET_MS = 800;
  const stillAnswering = () => barge.audible || eva.thinking || Date.now() - eva.lastAudibleAt < SWAP_QUIET_MS;

  function dropStandby() {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    const s = standby;
    standby = null;
    if (!s) return;
    clearTimeout(s.timer);
    try { if (s.channel.readyState === "open") s.channel.send(JSON.stringify({ type: "session.close" })); } catch { /* closing */ }
    if (s.peer) closePeer(s.peer);
  }

  // The log from `mark` on: what the old call said / heard after the new one got the history.
  function logMark() {
    const last = $("log").lastElementChild;
    return { el: last, len: last ? last.textContent.length : 0 };
  }
  function logSince(mark, skip) {
    const lines = [];
    // The bubble open at the mark goes in whole: the history had only its start, and the model
    // took that for an unfinished answer and told it again.
    const whole = mark.el && mark.len < mark.el.textContent.length;
    let el = mark.el ? (whole ? mark.el : mark.el.nextElementSibling) : $("log").firstElementChild;
    for (; el; el = el.nextElementSibling) {
      if (el === skip || el.classList.contains("system") || el.classList.contains("empty")) continue;
      const text = el.textContent.trim();
      if (text) lines.push((el.classList.contains("user") ? "User: " : "You: ") + text);
    }
    return lines.join("\n");
  }

  // announce: say «Голос змінено» when it actually switches (the user was told «зміниться»).
  // command: asked by voice — swap as soon as the new call is up, with nothing to continue.
  async function switchVoiceFast(announce = false, command = false) {
    dropStandby();
    if (!pc || !mic) { restartSession("switching", { switched: true }); return; }
    const s = { voice, mark: logMark(), ready: false, announce, command };
    standby = s;
    const fail = () => {
      if (standby !== s) return;
      dropStandby();
      switchVoiceWhenQuiet(s.announce); // the old way: reconnect (once she is quiet, if asked to wait)
    };
    s.timer = setTimeout(fail, 15000);
    try {
      const peer = new RTCPeerConnection();
      s.peer = peer;
      peer.ontrack = (e) => { s.stream = e.streams[0]; };
      // No mic until the swap: replaceTrack later needs no renegotiation.
      s.sender = peer.addTransceiver("audio", { direction: "sendrecv" }).sender;
      s.channel = peer.createDataChannel("oai-events");
      s.channel.onmessage = (e) => {
        if (standby !== s) return;
        let event;
        try { event = JSON.parse(e.data); } catch { return; }
        if (event.type === "session.started" || event.type === "session.created") { s.ready = true; s.readyAt = Date.now(); clearTimeout(s.timer); swapWhenQuiet(s); }
        else if (event.type === "session.closed") fail();
      };
      s.channel.onclose = fail;
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const res = await fetch(BACKEND + "/api/session", {
        method: "POST",
        headers: headers(),
        // keep_old: the current call goes on until the page swaps (the server would end it).
        body: JSON.stringify(sessionBody({ sdp: withClearOpus(offer.sdp), voice: s.voice, keep_old: true })),
      });
      if (standby !== s) return;
      if (!res.ok) throw new Error("session " + res.status);
      const { sdp } = await res.json();
      if (standby !== s) return;
      meterOpen(peer);
      await peer.setRemoteDescription({ type: "answer", sdp });
    } catch {
      fail();
    }
  }

  function swapWhenQuiet(s) {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    if (standby !== s) return;
    if (eva.mode !== "active" || !pc) { dropStandby(); if (eva.mode === "waiting") restartSession("switching", {}); return; }
    // Never mid-sentence of the user (half of it would go to each call — prod: the new call heard
    // only «…чи сьогодні день»). The gate notices speech a moment late and the transcript lags
    // more, so a user who spoke in the last second still counts. «Після відповіді» also waits for her.
    const userTalking = barge.speaking || Date.now() - barge.speechEndedAt < 1000 || Date.now() - eva.lastUserAt < 1000;
    if (userTalking || (!voiceNow && !s.command && stillAnswering())) {
      if (Date.now() - s.readyAt > STANDBY_WAIT_MS) { dropStandby(); switchVoiceWhenQuiet(s.announce); return; }
      voiceTimer = setTimeout(() => swapWhenQuiet(s), 100);
      return;
    }
    swapIn(s);
  }

  async function swapIn(s) {
    standby = null;
    const open = openMsg.assistant;
    // Mid-answer also counts a short gap between her sentences (the bubble is still open).
    // «Після відповіді» swaps only once she has finished: nothing to continue then.
    const midAnswer = voiceNow && !s.command && (barge.audible || Date.now() - eva.lastAudibleAt < 1500);
    const interrupted = open && midAnswer ? open.textContent.trim() : "";
    const said = logSince(s.mark, interrupted ? open : null);
    teardown({ keepMic: true }); // the old call: closed, its bubbles done
    pc = s.peer;
    channel = s.channel;
    sessionVoice = s.voice;
    wireCall(s.peer, s.channel);
    attachVoice(s.stream);
    s.peer.ontrack = (e) => attachVoice(e.streams[0]);
    try {
      const track = gate ? gate.track : mic.getAudioTracks()[0];
      track.enabled = true;
      micSender = s.sender;
      await s.sender.replaceTrack(track);
    } catch { /* closed meanwhile: its close handler takes over */ }
    if (pc !== s.peer) return;
    setRemoteVolume(1);
    if (s.announce) addLine("system", "Голос змінено на «" + voiceLabel(s.voice) + "».");
    // The history ended when this call opened; what came after is handed over here. The model
    // answers a context message out loud, so it is told plainly when to stay silent.
    const context = said ? "After your history ended, the conversation went on (the user heard all of it; " +
      "these replies of yours are finished — never repeat or retell them):\n" + said.slice(-800) + "\n\n" : "";
    if (interrupted) {
      thinking(context + continueInNewVoice(interrupted));
    } else if (context) {
      sendEvent({ type: "session.instructions.append", delegation_id: null, event_id: eventId("ctx"),
        content: context + "Say nothing now — no acknowledgement. Wait silently for the user's next words." });
    }
    showState();
    updateButtons();
    touchActivity();
  }

  // Without a standby call: reconnect in the new voice, once she is quiet if asked to wait.
  function switchVoiceWhenQuiet(announce = false) {
    clearTimeout(voiceTimer);
    voiceTimer = null;
    if (sessionVoice === voice || eva.mode === "off" || eva.mode === "connecting" || eva.mode === "switching") return; // a new call already has it
    if (eva.mode === "active" && !voiceNow && answering()) { voiceTimer = setTimeout(() => switchVoiceWhenQuiet(announce), 300); return; }
    if (announce) addLine("system", "Голос змінено на «" + voiceLabel(voice) + "».");
    restartSession("switching", { switched: true, interrupted: eva.mode === "active" ? speakingNow() : "" });
  }

  function showVoiceNow() {
    $("voiceNow").checked = voiceNow;
    $("voiceNowInfo").textContent = voiceNow
      ? "Одразу: Єва договорить відповідь уже новим голосом."
      : "Після відповіді: новий голос — коли Єва договорить.";
  }

  function toggleVoiceNow() {
    voiceNow = $("voiceNow").checked;
    store.set("va-voice-now", voiceNow ? "1" : "0");
    showVoiceNow();
    // A waiting switch happens now.
    if (voiceNow && standby && standby.ready) swapWhenQuiet(standby);
    else if (voiceNow && voiceTimer && !standby) switchVoiceWhenQuiet(true);
  }

  // Speed and manner are instructions to the model. One that arrives while she talks derails the
  // answer (instructions.append is also how a barge-in stops her), so it waits until she has been
  // quiet for a moment — the gate hears her real audio. A newer setting replaces a waiting one.
  let pendingDelivery = "";
  let deliveryTimer = null;
  const DELIVERY_QUIET_MS = 2500;
  // She is talking, thinking, or stopped only a moment ago (between sentences).
  const answering = () => barge.audible || barge.speaking || eva.thinking || Date.now() - eva.lastAudibleAt < DELIVERY_QUIET_MS;

  async function applyDelivery() {
    speed = $("speed").value;
    style = $("style").value;
    store.set("va-speed", speed);
    store.set("va-style", style);
    try {
      const res = await fetch(BACKEND + "/api/voice", { method: "POST", headers: headers(), body: JSON.stringify({ speed, style }) });
      const data = res.ok ? await res.json() : null;
      if (data && data.instruction) { pendingDelivery = data.instruction; sendDeliveryWhenQuiet(); }
    } catch { /* offline: the next session starts with these settings anyway */ }
  }

  // ?debug=1: evaDebug() in the console shows the page's state.
  if (DEBUG) {
    // evaGate(): the mic gate on this device — echo of her voice in the mic (speakers or headphones),
    // how loud the user speaks, and how much of what they said over her was cut. Counts since the page
    // opened; talk over her a few times first.
    window.evaGate = () => {
      const g = gateStats;
      if (!g) return null;
      const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
      return {
        mic: g.echo < 0.05 ? "headphones (no echo)" : "speakers (her echo in the mic)",
        echo: +g.echo.toFixed(3), voice: +g.voice.toFixed(3), floor: +g.floor.toFixed(4),
        herTalkS: +(g.her * 0.02).toFixed(1), openWhileHerPct: pct(g.herOpen, g.her),
        cutWhileHerS: +(g.cut * 0.02).toFixed(1), quietS: +(g.quiet * 0.02).toFixed(1),
        openWhileQuietPct: pct(g.quietOpen, g.quiet),
      };
    };
    window.evaDebug = () => ({
      mode: eva.mode, pendingDelivery, audible: barge.audible, speaking: barge.speaking,
      thinking: eva.thinking, quietMs: Date.now() - eva.lastAudibleAt, channel: channel && channel.readyState,
      // Many ducks while only she talks = her echo is taken for the user (crackle, self-interruption).
      ducks: barge.ducks, mutes: barge.mutes,
    });
    // await evaAudio(): what her voice actually arrives as — codec line, kbit/s over 3 s, lost and
    // concealed audio. A low bitrate or a lot of concealment = the lisp is the transport, not the model.
    window.evaAudio = async () => {
      if (!pc) return null;
      const read = async () => {
        let inbound = null, codecs = {};
        (await pc.getStats()).forEach((r) => {
          if (r.type === "inbound-rtp" && r.kind === "audio") inbound = r;
          if (r.type === "codec") codecs[r.id] = r;
        });
        return { inbound, codec: inbound && codecs[inbound.codecId] };
      };
      const a = await read();
      await new Promise((r) => setTimeout(r, 3000));
      const b = await read();
      if (!a.inbound || !b.inbound) return null;
      const s = b.inbound;
      return {
        codec: b.codec && b.codec.mimeType + " " + b.codec.clockRate + " " + (b.codec.sdpFmtpLine || ""),
        kbps: Math.round(((s.bytesReceived - a.inbound.bytesReceived) * 8) / 3000),
        packetsLost: s.packetsLost,
        concealedPct: s.totalSamplesReceived ? +((100 * s.concealedSamples) / s.totalSamplesReceived).toFixed(1) : 0,
        jitterMs: Math.round((s.jitter || 0) * 1000),
      };
    };
  }

  function sendDeliveryWhenQuiet() {
    clearTimeout(deliveryTimer);
    if (!pendingDelivery) return;
    if (!channel || eva.mode !== "active") { pendingDelivery = ""; return; } // a new session gets them at start
    if (answering()) {
      deliveryTimer = setTimeout(sendDeliveryWhenQuiet, 300);
      return;
    }
    sendEvent({ type: "session.instructions.append", content: pendingDelivery, delegation_id: null, event_id: eventId("style") });
    pendingDelivery = "";
  }

  function showVolume() {
    $("volume").value = String(Math.round(volume * 100));
    $("volumeValue").textContent = Math.round(volume * 100) + "%";
  }

  let preview = null;
  function stopPreview() {
    if (preview) preview.pause();
    preview = null;
    $("voicePreview").textContent = "▶ Прослухати";
  }
  function togglePreview() {
    if (preview) { stopPreview(); return; }
    const audio = new Audio("samples/" + encodeURIComponent($("voice").value) + ".m4a");
    audio.volume = volume;
    if (speakerId && audio.setSinkId) audio.setSinkId(speakerId).catch(() => {});
    audio.onended = stopPreview;
    audio.onerror = () => { stopPreview(); addLine("system", "Зразок цього голосу недоступний."); };
    preview = audio;
    $("voicePreview").textContent = "■ Стоп";
    audio.play().catch(stopPreview);
  }

  // «зміни голос …» in the Live transcript too (browsers without a recogniser, or it missed it):
  // once the user pauses, like voice/options.py on the server — which now waits for the page.
  function checkVoiceRequest(delta) {
    eva.inputBuf = (eva.inputBuf + delta).slice(-300);
    clearTimeout(eva.voiceCheckTimer);
    if (!VOICE_REQUEST_RE.test(eva.inputBuf)) return;
    eva.voiceCheckTimer = setTimeout(() => {
      const text = eva.inputBuf;
      eva.inputBuf = "";
      if (eva.mode === "active") requestVoiceChange(text);
    }, 900);
  }

  // Heard «зміни голос …» ourselves: the page switches without a gap; the server's own transcript
  // check is only a fallback (it waits longer and dedups with this).
  async function requestVoiceChange(text) {
    if (eva.voiceRequestBusy) return;
    eva.voiceRequestBusy = true;
    try {
      const res = await fetch(BACKEND + "/api/voice-request", { method: "POST", headers: headers(), body: JSON.stringify({ text }) });
      const data = res.ok ? await res.json() : null;
      if (data && data.switched && eva.mode === "active" && voices.some((x) => x.id === data.voice)) {
        voice = data.voice;
        store.set("va-voice", voice);
        showVoice();
        addLine("system", "Голос змінено на «" + voiceLabel(voice) + "» (почула: «" + text.trim().slice(0, 80) + "»).");
        // The old call goes on for a few seconds: it must not answer the request meanwhile.
        sendEvent({ type: "session.instructions.append", delegation_id: null, event_id: eventId("voice"),
          content: "The app is changing your voice right now. Say nothing at all and do not delegate it — stay silent." });
        switchVoiceFast(false, true);
      }
    } catch { /* offline: the server-side path may still switch */ } finally {
      eva.voiceRequestBusy = false;
    }
  }

  async function newConversation() {
    await fetch(BACKEND + "/api/conversation", { method: "DELETE", headers: headers() }).catch(() => {});
    clearLog();
    addLine("system", "Нова розмова — Єва почне з чистого аркуша.");
    restartSession("connecting", { greet: true });
  }

  // «Скопіювати»: the dialogue as plain text, without the page's own notes.
  function conversationText() {
    const name = assistantName || "Єва";
    return [...$("log").querySelectorAll(".msg.user, .msg.assistant")]
      .map((el) => [el.classList.contains("user") ? "Ви" : name, el.textContent.trim()])
      .filter(([, text]) => text)
      .map(([who, text]) => who + ": " + text)
      .join("\n\n");
  }

  // The async clipboard needs a secure page with focus; the old way works where it doesn't.
  function copyFallback(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { return document.execCommand("copy"); } finally { area.remove(); }
  }

  // The button says what happened for a moment, then goes back to its label.
  function flashButton(btn, text) {
    btn.dataset.label = btn.dataset.label || btn.textContent;
    clearTimeout(btn.flashTimer);
    btn.textContent = text;
    btn.flashTimer = setTimeout(() => { btn.textContent = btn.dataset.label; }, 1800);
  }

  async function copyConversation() {
    const text = conversationText();
    const btn = $("copyChat");
    if (!text) { flashButton(btn, "Поки порожньо"); return; }
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch { ok = copyFallback(text); }
    flashButton(btn, ok ? "Скопійовано ✓" : "Не вдалося");
  }

  // The server's view of this browser (voice settings, call state); null if unreachable.
  async function fetchMe() {
    try {
      const res = await fetch(BACKEND + "/api/me", { headers: headers() });
      if (res.status === 401) { showGate(); return null; }
      return res.ok ? await res.json() : null;
    } catch { return null; } // backend asleep / offline
  }

  // Voice settings changed on the server (asked by voice).
  async function refreshMe() {
    if (!BACKEND) return;
    try {
      const me = await fetchMe();
      if (!me) return;
      assistantName = me.assistant_name || ""; // renamed by voice: the copied conversation uses it
      const { voice: serverVoice, speed: serverSpeed, style: serverStyle } = me;
      // Set on the server only when chosen there (e.g. asked by voice) — adopt it then.
      if (serverVoice && serverVoice !== voice && voices.some((x) => x.id === serverVoice)) {
        voice = serverVoice;
        store.set("va-voice", voice);
        showVoice();
      }
      // «Говори повільніше» by voice: show it (a restarted server reports "normal" — keep ours then).
      if (serverSpeed && serverSpeed !== "normal" && serverSpeed !== speed) { speed = serverSpeed; store.set("va-speed", speed); $("speed").value = speed; }
      if (serverStyle && serverStyle !== "normal" && serverStyle !== style) { style = serverStyle; store.set("va-style", style); $("style").value = style; }
    } catch { /* backend asleep / offline: keep the old state */ }
  }

  function showGate() { $("gate").hidden = false; $("code").focus(); }

  const PROMPT_INFO = {
    v3: "Говорить живо, як у телефонній розмові з другом, і поводиться як людина: має свою думку, цікавиться " +
      "вами, спершу реагує на ваші новини, повертається до сказаного раніше, без «чим ще можу допомогти?».",
    v4: "v3 з повними відповідями: довжина за питанням (коротко на дрібниці, розгорнуто на «поясни», «порадь», " +
      "«як»), конкретні назви, числа й кроки, без «хочеш, розкажу?» замість відповіді, рідше питання наприкінці, " +
      "не перебиває, поки ви не договорили.",
  };
  const showPromptInfo = () => { $("promptInfo").textContent = PROMPT_INFO[$("promptVariant").value] || ""; };

  function changePrompt() {
    showPromptInfo();
    promptVariant = $("promptVariant").value;
    store.set("va-prompt", promptVariant);
    if (eva.mode === "active") switchVoiceFast(false, true); // a new call with the same voice and memory
    else if (eva.mode === "waiting") restartSession("switching", {});
  }

  // ── devices: microphone (in), speaker (out), camera ──
  // Browser echo cancellation keeps the agent from hearing (and interrupting) itself.
  const micConstraints = () => Object.assign(
    { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    micId ? { deviceId: { exact: micId } } : {},
  );
  const canPickSpeaker = "setSinkId" in HTMLMediaElement.prototype; // Chrome, Edge; not Safari/Firefox
  const deviceKeys = { mic: "va-mic", speaker: "va-speaker", camera: "va-camera" };

  function setDevice(kind, id) {
    if (kind === "mic") micId = id;
    else if (kind === "speaker") speakerId = id;
    else cameraId = id;
    store.set(deviceKeys[kind], id);
  }

  // The picked mic, or the default one when it is gone (unplugged headset).
  async function openMic() {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    } catch (err) {
      if (!micId || !["OverconstrainedError", "NotFoundError"].includes(err && err.name)) throw err;
      addLine("system", "Обраного мікрофона немає — беру той, що за замовчуванням.");
      setDevice("mic", "");
      $("micSelect").value = "";
      return navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    }
  }

  // Fill the three pickers. Before the mic is allowed the browser hides device names (and ids),
  // so only «За замовчуванням» is offered until then.
  async function listDevices() {
    const md = navigator.mediaDevices;
    if (!md || !md.enumerateDevices) { $("devicesInfo").textContent = "Цей браузер не дає вибрати пристрої."; return; }
    let devices;
    try { devices = await md.enumerateDevices(); } catch { return; }
    const fill = (select, kind, current, noun) => {
      // Chrome's «default» / «communications» entries are the system default again: "" covers them.
      const list = devices.filter((d) => d.kind === kind && d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications");
      $(select).replaceChildren(new Option("За замовчуванням", ""),
        ...list.map((d, i) => new Option(d.label || noun + " " + (i + 1), d.deviceId)));
      $(select).value = list.some((d) => d.deviceId === current) ? current : "";
      return list.length;
    };
    fill("micSelect", "audioinput", micId, "Мікрофон");
    fill("speakerSelect", "audiooutput", speakerId, "Динамік");
    const cameras = fill("cameraSelect", "videoinput", cameraId, "Камера");
    $("speakerSelect").disabled = !canPickSpeaker;
    const named = devices.some((d) => d.label);
    $("devicesInfo").textContent = [
      named ? "" : "Назви пристроїв з'являться, коли браузер дасть доступ до мікрофона.",
      canPickSpeaker ? "" : "Динамік у цьому браузері не вибирається (потрібен Chrome або Edge) — звук іде в системний.",
      named && !cameras ? "Камери не знайдено." : "",
    ].filter(Boolean).join(" ") || "Зміна діє одразу, навіть посеред розмови.";
  }

  // Another mic mid-call: a new stream and gate, the call's track replaced in place — no reconnect.
  async function changeMic() {
    setDevice("mic", $("micSelect").value);
    if (!mic) return; // the next call opens it
    const old = mic;
    let stream;
    try { stream = await openMic(); } catch (err) {
      addLine("system", "Не вдалося відкрити мікрофон: " + ((err && err.message) || err));
      return;
    }
    const newGate = await createNoiseGate(stream, onGateSignal);
    if (mic !== old) { // the call ended or restarted meanwhile: it opens its own mic
      stream.getTracks().forEach((t) => t.stop());
      if (newGate) newGate.close();
      return;
    }
    const oldGate = gate;
    mic = stream;
    gate = newGate;
    if (gate) gate.listenTo($("remote").srcObject || null);
    const track = gate ? gate.track : mic.getAudioTracks()[0];
    track.enabled = true;
    try { if (micSender) await micSender.replaceTrack(track); } catch { /* call closing */ }
    if (oldGate) oldGate.close();
    old.getTracks().forEach((t) => t.stop());
    if (eva.recognizer) { stopPhraseListener(); startPhraseListener(); } // it follows the mic
    addLine("system", "Мікрофон: " + $("micSelect").selectedOptions[0].textContent + ".");
  }

  async function changeSpeaker() {
    const id = $("speakerSelect").value;
    try {
      await $("remote").setSinkId(id);
    } catch {
      addLine("system", "Цей динамік недоступний — лишаю попередній.");
      $("speakerSelect").value = speakerId;
      return;
    }
    setDevice("speaker", id);
    if (fx) fx.setSink(id);
    if (preview && preview.setSinkId) preview.setSinkId(id).catch(() => {});
  }

  // Camera: Єва can't see it herself (GPT-Live takes no images). The backend tool look_at_camera hands
  // a frame to the model that runs her tools. The page snaps that frame the moment it sees the tool
  // called (the data channel carries the session's events), so a look shows what is held up now; the
  // tool waits for it. A frame every FRAME_EVERY_MS is only the fallback (an upload lost). Nothing is
  // sent on a pause or with the camera off.
  const FRAME_EVERY_MS = 3000;
  const FRAME_MAX_SIDE = 768;  // fallback frames: ~400 image tokens
  const LOOK_MAX_SIDE = 1024;  // the look itself: small print stays legible (~600 tokens, ≈$0.0001)
  const FRAME_TIMEOUT_MS = 4000; // a hung upload must not stop the next ones
  let cam = null;
  let frameTimer = null;
  let frameBusy = false;
  let frameCanvas = null;
  const snapped = new Set(); // calls already snapped for (output_item.added and .done both come)

  function showCamera() {
    $("cameraInfo").textContent = cam
      ? "Увімкнено: коли попросите «Єва, подивись», вона гляне на кадр. Кадри ніде не зберігаються."
      : "Попросіть «Єва, подивись» — вона гляне через камеру.";
  }

  async function startCamera() {
    // Front camera by default: «подивись на мене»; the back one is picked in «Пристрої».
    const video = Object.assign({ width: { ideal: 1280 }, height: { ideal: 720 } },
      cameraId ? { deviceId: { exact: cameraId } } : { facingMode: "user" });
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video });
    } catch (err) {
      if (!cameraId || !["OverconstrainedError", "NotFoundError"].includes(err && err.name)) throw err;
      setDevice("camera", "");
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
    }
    if (!$("camera").checked) { stream.getTracks().forEach((t) => t.stop()); return; } // turned off meanwhile
    if (cam) cam.getTracks().forEach((t) => t.stop());
    cam = stream;
    const track = cam.getVideoTracks()[0];
    const view = $("cameraView");
    view.srcObject = cam;
    view.hidden = false;
    // The preview is mirrored for a front camera, like a mirror; frames go out as the camera sees.
    const facing = (track.getSettings && track.getSettings().facingMode) || "user";
    view.classList.toggle("mirror", facing === "user");
    track.onended = () => { if (cam === stream) { $("camera").checked = false; stopCamera(); addLine("system", "Камеру вимкнено."); } };
    clearInterval(frameTimer);
    frameTimer = setInterval(() => sendFrame(), FRAME_EVERY_MS);
    showCamera();
    listDevices();
    view.onloadeddata = () => sendFrame();
  }

  function stopCamera() {
    clearInterval(frameTimer);
    frameTimer = null;
    if (cam) cam.getTracks().forEach((t) => t.stop());
    cam = null;
    $("cameraView").srcObject = null;
    $("cameraView").hidden = true;
    showCamera();
    // The server forgets the last frame: Єва says the camera is off instead of describing an old one.
    if (BACKEND) fetch(BACKEND + "/api/frame", { method: "DELETE", headers: headers() }).catch(() => {});
  }

  async function toggleCamera() {
    if (!$("camera").checked) { stopCamera(); return; }
    try {
      await startCamera();
    } catch (err) {
      $("camera").checked = false;
      stopCamera();
      addLine("system", err && err.name === "NotAllowedError" ? "Немає доступу до камери." : "Не вдалося увімкнути камеру.");
    }
  }

  async function changeCamera() {
    setDevice("camera", $("cameraSelect").value);
    if (cam) await toggleCamera();
  }

  let lastSnapAt = 0;
  // callId "" = a request just went to the backend; a call id = the backend calls look_at_camera — then
  // only if nothing was snapped in the last 2 s (the frame from the start of the work is fresh enough).
  function snapForLook(callId) {
    if (!cam || (callId && (snapped.has(callId) || Date.now() - lastSnapAt < 2000))) return;
    if (callId) snapped.add(callId);
    lastSnapAt = Date.now();
    if (snapped.size > 50) snapped.clear();
    // The next fallback frame (smaller) comes a full interval later: it must not replace this one
    // before the tool has taken it.
    clearInterval(frameTimer);
    frameTimer = setInterval(() => sendFrame(), FRAME_EVERY_MS);
    sendFrame(true);
  }

  // A frame to the backend, only while she is in an active call. look: snapped for look_at_camera —
  // sharper, and sent even while a fallback frame is still uploading.
  async function sendFrame(look = false) {
    const view = $("cameraView");
    if (!cam || (frameBusy && !look) || eva.mode !== "active" || !view.videoWidth) return;
    const side = look ? LOOK_MAX_SIDE : FRAME_MAX_SIDE;
    const scale = Math.min(1, side / Math.max(view.videoWidth, view.videoHeight));
    const canvas = look ? document.createElement("canvas") : (frameCanvas = frameCanvas || document.createElement("canvas"));
    canvas.width = Math.round(view.videoWidth * scale);
    canvas.height = Math.round(view.videoHeight * scale);
    canvas.getContext("2d").drawImage(view, 0, 0, canvas.width, canvas.height);
    if (!look) frameBusy = true;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), FRAME_TIMEOUT_MS);
    try {
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", look ? 0.85 : 0.7));
      if (blob && cam) {
        await fetch(BACKEND + "/api/frame", {
          method: "POST", headers: Object.assign(headers(), { "Content-Type": "image/jpeg" }), body: blob, signal: ctrl.signal,
        });
      }
    } catch { /* offline or timed out: the next frame tries again */ } finally {
      clearTimeout(timer);
      if (!look) frameBusy = false;
    }
  }

  async function init() {
    $("talk").addEventListener("click", () => {
      if (eva.mode !== "off") { stop(); return; }
      meterReset(); // a new call counts from zero
      start(defaultStart());
    });
    $("pause").addEventListener("click", pauseEva);
    $("resume").addEventListener("click", () => wakeEva(""));
    $("bgListen").addEventListener("change", toggleBgListen);
    showBgListen();
    $("voiceNow").addEventListener("change", toggleVoiceNow);
    $("cleanVoice").checked = cleanVoice;
    $("cleanVoice").addEventListener("change", toggleCleanVoice);
    $("promptVariant").value = promptVariant;
    $("promptVariant").addEventListener("change", changePrompt);
    showPromptInfo();
    showVoiceNow();
    $("newChat").addEventListener("click", newConversation);
    $("copyChat").addEventListener("click", copyConversation);
    $("voice").addEventListener("change", () => { stopPreview(); showVoice(true); });
    $("voicePreview").addEventListener("click", togglePreview);
    $("voiceApply").addEventListener("click", applyVoice);
    $("speed").value = speed;
    $("style").value = style;
    $("speed").addEventListener("change", applyDelivery);
    $("style").addEventListener("change", applyDelivery);
    showVolume();
    $("volume").addEventListener("input", (e) => {
      volume = Math.min(1, Math.max(0, Number(e.target.value) / 100));
      store.set("va-volume", String(volume));
      showVolume();
      setRemoteVolume(bargeLevel());
      if (preview) preview.volume = volume;
    });
    $("micSelect").addEventListener("change", changeMic);
    $("speakerSelect").addEventListener("change", changeSpeaker);
    $("cameraSelect").addEventListener("change", changeCamera);
    $("camera").addEventListener("change", toggleCamera);
    if (navigator.mediaDevices) {
      // A headset plugged in or out: the pickers follow.
      navigator.mediaDevices.addEventListener("devicechange", listDevices);
      listDevices();
    } else {
      $("camera").disabled = true;
    }
    // The picked speaker from the last visit (gone → the system default).
    if (canPickSpeaker && speakerId) $("remote").setSinkId(speakerId).catch(() => { setDevice("speaker", ""); listDevices(); });
    updateButtons();
    $("saveCode").addEventListener("click", () => {
      accessCode = $("code").value.trim();
      store.set("va-access-code", accessCode);
      $("gate").hidden = true;
      refreshMe();
    });
    // Closing or leaving the tab ends the paid Live session instead of leaving it open.
    window.addEventListener("pagehide", () => { if (eva.mode !== "off") stop(); });
    if (!BACKEND) { addLine("system", "Не вказано адресу бекенду."); return; }
    const cfg = await wakeBackend();
    if (cfg) {
      if (cfg.access_code_required && !accessCode) showGate();
      await loadVoices();
      refreshMe();
    }
    // Free Render sleeps after ~15 min without requests and forgets every conversation;
    // while the page is open and visible, keep it awake.
    setInterval(() => {
      if (document.visibilityState === "visible") fetch(BACKEND + "/healthz").catch(() => {});
    }, 10 * 60 * 1000);
  }

  // A sleeping free-tier backend takes up to a minute to start: wait for it instead of failing.
  async function wakeBackend() {
    const deadline = Date.now() + 90 * 1000;
    let notified = false;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(BACKEND + "/api/config");
        if (res.ok) {
          if (notified) setStatus("Не підключено");
          $("talk").disabled = false;
          return await res.json();
        }
      } catch { /* still waking up */ }
      if (!notified) {
        notified = true;
        $("talk").disabled = true;
        setStatus("Сервер прокидається — до хвилини…", "busy");
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
    $("talk").disabled = false;
    setStatus("Сервер не відповідає", "err");
    addLine("system", "Бекенд не відповідає. Оновіть сторінку трохи згодом.");
    return null;
  }

  init();
})();
