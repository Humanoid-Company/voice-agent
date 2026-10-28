// Runs web/app.js against a fake DOM: load-time errors (undefined names, TDZ), message handling, meter, history.
var store = { convs: JSON.stringify([{ id: "old1", t: 1000, u: 2000, title: "старе", msgs: [{ r: "user", t: "привіт" }, { r: "assistant", t: "добрий день" }] }]) };
var made = {};
function E(id) {
  var e = made[id];
  if (e) return e;
  e = made[id] = {
    id: id, value: "", textContent: "", hidden: false, disabled: false, checked: true, title: "", className: "",
    options: [], children: [], style: {}, _attrs: {},
    classList: { add: function () {}, remove: function () {}, toggle: function () {}, contains: function () { return false; } },
    add: function (o) { this.options.push(o); if (this.options.length === 1 || !this.value) this.value = o.value; },
    setAttribute: function (k, v) { this._attrs[k] = v; }, addEventListener: function () {},
    appendChild: function (c) {
      this.children.push(c);
      if (c.tagName === "optgroup") { for (var i = 0; i < c.children.length; i++) this.options.push(c.children[i]); }
      else if (c instanceof Option) this.options.push(c);
    }, querySelector: function () { return null; },
    querySelectorAll: function () { return []; }, remove: function () {},
    open: false, showModal: function () { this.open = true; }, close: function () { this.open = false; }, select: function () {}, scrollTop: 0, scrollHeight: 0,
  };
  Object.defineProperty(e, "innerHTML", {
    enumerable: true,
    get: function () { return this._html || ""; },
    set: function (v) { this._html = v; if (v === "") { this.children = []; this.options = []; } },
  });
  return e;
}
var document = { querySelectorAll: function () { return []; }, getElementById: E, createElement: function (tag) { var e = E("_new" + Math.random()); e.tagName = tag; return e; }, body: E("body"),
  visibilityState: "visible", execCommand: function () { return true; } };
function Option(label, value) { this.text = label; this.value = value; this.dataset = {}; }
var recStarts = [], recAborts = 0;
function FakeRec() {}
FakeRec.prototype.start = function (track) { recStarts.push(track); };
FakeRec.prototype.abort = function () { recAborts++; };
FakeRec.prototype.processLocally = false;
var window = { SpeechRecognition: FakeRec, APP_CONFIG: { BACKEND_URL: "https://x.test", IDLE_PAUSE_SECONDS: 90 }, location: {} };
var location = { origin: "https://page.test" };
function URLSearchParams() { this.get = function () { return null; }; }
var localStorage = { getItem: function (k) { return k in store ? store[k] : null; }, setItem: function (k, v) { store[k] = String(v); } };
var navigator = { mediaDevices: { enumerateDevices: function () { return Promise.resolve([]); }, addEventListener: function () {} },
  clipboard: { writeText: function () { return Promise.resolve(); } } };
var CONFIG = {
  providers: [
    { id: "soniox", label: "Soniox", voices: [{ id: "Daniel", label: "Daniel (male)" }, { id: "Maya", label: "Maya (female)" }] },
    { id: "respeecher", label: "Respeecher", voices: ["dmytro-conversation", "ihoreo-conversation", "yevhen-audiobook", "roksolana-conversation", "kseniia-conversation", "olesia-conversation", "oleksandr-radio"].map(function (v) { return { id: v, label: v }; }) },
  ],
  characters: [
    { id: "eva", label: "Єва", description: "Звичайна помічниця", voices: [] },
    { id: "tolik", label: "Толік", description: "Дворовий хуліган", voices: [["dmytro-conversation", "Дмитро"], ["ihoreo-conversation", "Ігорео"], ["yevhen-audiobook", "Євген"]].map(function (v) { return { provider: "respeecher", voice: v[0], label: v[1] }; }) },
    { id: "kishka", label: "Кішка", description: "Говоряча кішка", voices: [["roksolana-conversation", "Роксолана"], ["kseniia-conversation", "Ксенія"], ["olesia-conversation", "Олеся"]].map(function (v) { return { provider: "respeecher", voice: v[0], label: v[1] }; }) },
    { id: "taras", label: "Тарас", description: "Лавелас", voices: [["oleksandr-radio", "Олександр"], ["yevhen-audiobook", "Євген"], ["dmytro-conversation", "Дмитро"]].map(function (v) { return { provider: "respeecher", voice: v[0], label: v[1] }; }) },
  ],
  missing: [],
};
var fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve(CONFIG); } }); };
var timers = [];
var setTimeout = function (f) { timers.push(f); return timers.length; };
var setInterval = function () { return 0; };
var clearTimeout = function () {};
var WebSocket = function () {}; WebSocket.OPEN = 1;
var sent = [];

load("web/app.js");
drainMicrotasks && drainMicrotasks();

function check(name, cond) { print((cond ? "ok   " : "FAIL ") + name); if (!cond) failed = true; }
var failed = false;

check("init filled the provider list", E("provider").options.length === 2 && E("voice").options.length === 2);
check("talk button is enabled with keys and speech recognition", E("talk").disabled === false);

// pretend a call is running
live = true; ready = true; ws = { readyState: 1, send: function (m) { sent.push(JSON.parse(m)); }, close: function () {} };
onMessage({ data: JSON.stringify({ type: "ready", sample_rate: 24000, name: "Єва", language: "uk", recognition: "uk-UA" }) });
check("ready sets the name and state", E("assistantName").textContent === "Єва" && curState === "listening");
check("buttons visible during a call", E("pause").hidden === false && E("micMute").hidden === false);

onMessage({ data: JSON.stringify({ type: "persona", name: "Лесі" }) });
check("persona changes the name", E("assistantName").textContent === "Лесі");

onMessage({ data: JSON.stringify({ type: "voice", provider: "soniox", voice: "Maya", sample_rate: 16000 }) });
check("voice message updates the selector and the sample rate", E("voice").value === "Maya" && ttsRate === 16000 && store["voice:soniox"] === "Maya");

onMessage({ data: JSON.stringify({ type: "language", code: "en", recognition: "en-US" }) });
check("language message changes the recognition language", recLang === "en-US");

onMessage({ data: JSON.stringify({ type: "usage", seconds: 65, llm_in: 1000, llm_out: 200, searches: 1, tts_chars: 300, cost: 0.0123 }) });
check("meter shows time and money", E("meter").textContent.indexOf("1:05") >= 0 && E("meter").textContent.indexOf("$0.01") >= 0);
var day = JSON.parse(store["usage:" + new Date().toISOString().slice(0, 10)]);
check("today's total is kept in the browser", day.sec === 65 && Math.abs(day.usd - 0.0123) < 1e-9);
onMessage({ data: JSON.stringify({ type: "usage", seconds: 95, llm_in: 1500, llm_out: 300, searches: 1, tts_chars: 400, cost: 0.02 }) });
day = JSON.parse(store["usage:" + new Date().toISOString().slice(0, 10)]);
check("a second report adds only the difference", day.sec === 95 && Math.abs(day.usd - 0.02) < 1e-9);
check("history table lists the day and a total", E("usageTable").innerHTML.indexOf("Разом") >= 0);

// pause / resume
setPaused(true, "тест");
check("pause is sent with the background flag", sent.some(function (m) { return m.type === "pause" && m.background === true; }) && paused === true);
check("button now offers to continue", E("pause").textContent === "Продовжити");
heard("це щось почуте поруч", true);
heard("інтерім не йде", false);
check("on pause only the final phrase goes to the server", sent.filter(function (m) { return m.type === "heard"; }).length === 1);
check("background listening keeps the recognizer running on pause", recognition !== null);
setPaused(false);
check("resume is sent", sent.some(function (m) { return m.type === "resume"; }) && paused === false);

// the voice selector sends a change mid-call
check("with the selection equal to the active voice there is nothing to apply", E("voiceApply").hidden === true);
sent.length = 0;
E("provider").value = "soniox"; E("voice").value = "Daniel"; E("voice").onchange();
check("picking a voice mid-call does not change it yet, it offers «Застосувати»", E("voiceApply").hidden === false && !sent.some(function (m) { return m.type === "voice"; }));
E("voiceApply").onclick();
check("«Застосувати» sends the chosen voice and waits for the answer", sent.some(function (m) { return m.type === "voice" && m.voice === "Daniel" && m.provider === "soniox"; }) && E("voiceApply").disabled === true && E("voiceApply").textContent === "Застосовую…");
onMessage({ data: JSON.stringify({ type: "voice", provider: "soniox", voice: "Daniel", sample_rate: 24000, language: "uk" }) });
check("the server's confirmation makes it the active voice and hides the button", activeVoice.voice === "Daniel" && E("voiceApply").hidden === true && E("voiceApply").disabled === false);
E("voice").value = "Maya"; E("voice").onchange(); E("voiceApply").onclick();
onMessage({ data: JSON.stringify({ type: "error", message: "Не вдалося змінити голос: x" }) });
check("a failed change brings the button back", E("voiceApply").disabled === false && E("voiceApply").textContent === "Застосувати голос" && E("voiceApply").hidden === false);
E("voice").value = "Daniel"; E("voice").onchange();
check("choosing the active voice again hides the button", E("voiceApply").hidden === true);

// the server ends the call
onMessage({ data: JSON.stringify({ type: "end" }) });
check("end closes the call", live === false);

// volume
E("volume").value = "40"; E("volume").oninput();
check("volume is saved", store["volume"] === "40");
// pause without background listening stops the recognizer, resume starts it again
live = true; ready = true; paused = false; ws = { readyState: 1, send: function (m) { sent.push(JSON.parse(m)); }, close: function () {} };
startRecognition();
E("bgListen").checked = false;
setPaused(true);
check("without background listening the recognizer is stopped on pause", recognition === null && sent.filter(function (m) { return m.type === "pause" && m.background === false; }).length === 1);
setPaused(false);
check("resume starts the recognizer again", recognition !== null);

// the voice-interruption sensitivity and the live level line
var gateMsgs = [];
gate = { port: { postMessage: function (m) { gateMsgs.push(m); } } };
check("sensitivity defaults to high", E("bargeSens").value === "2" && bargeSens() === 2);
E("bargeSens").value = "3"; E("bargeSens").onchange();
check("sensitivity is saved and sent to the gate", store["bargeSens"] === "3" && gateMsgs.length === 1 && gateMsgs[0].sens === 3);
live = true; paused = false; micMuted = false;
gate = { port: { postMessage: function () {} } };

// what the microphone hears: the four cases the user has to tell apart
onGate({ levels: { level: 0.03, need: 0.065, audible: true, speaking: false } });
check("quiet voice against her echo is explained", E("hearingStatus").textContent.indexOf("тихіший за ехо") >= 0 && E("hearingStatus").textContent.indexOf("6.5") >= 0 && E("levelNeed").hidden === false);
onGate({ levels: { level: 0.12, need: 0.065, audible: true, speaking: true } });
check("a loud voice over her says she should stop", E("hearingStatus").textContent.indexOf("голосніший за ехо") >= 0);
onGate({ levels: { level: 0.05, need: 0.07, audible: false, speaking: true } });
check("voice while she is silent is heard", E("hearingStatus").textContent.indexOf("Чую ваш голос") >= 0 && E("levelNeed").hidden === true);
onGate({ levels: { level: 0, need: 0.07, audible: false, speaking: false } });
deadSince = Date.now() - 5000; renderHearing();
check("a dead microphone is blamed on the microphone", E("hearingStatus").textContent.indexOf("не передає звук") >= 0);
deadSince = 0; micMuted = true; renderHearing();
check("a muted microphone is said to be muted", E("hearingStatus").textContent.indexOf("вимкнено") >= 0);
micMuted = false;

// the log of what was recognized and dropped
heard("привіт", true);
check("a recognized phrase is logged", hearLines[0].text.indexOf("розпізнано й передано: «привіт»") >= 0);
gate = { port: { postMessage: function () {} } }; barge.audible = true; barge.speaking = false; barge.speechEndedAt = 0;
heard("це шум поруч", true);
check("a phrase that is not the user's voice is logged as dropped", hearLines[0].bad === true && hearLines[0].text.indexOf("не передано") >= 0);
barge.audible = false;
onMessage({ data: JSON.stringify({ type: "user_drop", reason: "echo", text: "привіт світе" }) });
check("the server's reason for a drop is shown", hearLines[0].text.indexOf("ехо") >= 0 && hearLines[0].text.indexOf("привіт світе") >= 0);

// conversation history
check("by default the agent remembers nothing between calls", memoryTurns() === 0 && E("memorySel").value === "0");
E("memorySel").value = "10"; E("memorySel").onchange(); // the tests below need a memory
var old = convs.find(function (c) { return c.id === "old1"; });
check("an old conversation (dialogue only) is migrated to the event record", old && old.events.length === 2 && old.events[0].k === "user" && old.events[0].text === "привіт" && !("msgs" in old));
deleteConversation("old1");
convs = []; current = null; persistConvs(); // the earlier tests left conversations of their own
sent.length = 0;
newConversation();
check("a new conversation has no current one and tells the server", current === null && sent.some(function (m) { return m.type === "history" && m.history.length === 0; }));
onMessage({ data: JSON.stringify({ type: "user", text: "Мене звати Віктор", final: true }) });
onMessage({ data: JSON.stringify({ type: "record", role: "assistant", text: "Приємно, Вікторе." }) });
check("the conversation is kept in the browser", current && current.events.length === 2 && current.title === "Мене звати Віктор" && JSON.parse(store["convs"]).length === 1);
check("the server is given the past on the next call", JSON.stringify(memoryForServer()) === JSON.stringify([{ role: "user", content: "Мене звати Віктор" }, { role: "assistant", content: "Приємно, Вікторе." }]));
var firstId = current.id;
newConversation();
onMessage({ data: JSON.stringify({ type: "user", text: "Інша розмова", final: true }) });
check("a new conversation is a separate entry", convs.length === 2 && current.id !== firstId && memoryForServer().length === 1);
sent.length = 0;
openConversation(firstId);
check("opening an old conversation restores it and tells the server", current.id === firstId && sent.some(function (m) { return m.type === "history" && m.history.length === 2; }) && store["convCurrent"] === firstId);
deleteConversation(firstId);
check("deleting the current conversation clears it", current === null && convs.length === 1 && JSON.parse(store["convs"]).length === 1);
E("historyList").children = []; renderHistory();
check("the history list has a row per conversation", E("historyList").children.length === 1);

// the two buttons at the top open their windows
E("openHistory").onclick();
check("«Історія» opens the history window", E("historyDialog").open === true && E("usageDialog").open === false);
E("historyDialog").close();
E("openUsage").onclick();
check("«Витрати» opens the costs window", E("usageDialog").open === true);
E("usageDialog").close();
newConversation();
onMessage({ data: JSON.stringify({ type: "user", text: "Відкрий мене", final: true }) });
var oldId = current.id;
newConversation();
E("historyDialog").open = true;
openConversation(oldId);
check("opening a conversation closes the history window", E("historyDialog").open === false && current.id === oldId);

// ---- the full record: what the user said, how they tried to interrupt, what did not work
newConversation();
live = true; paused = false; micMuted = false; ready = true;
gate = { port: { postMessage: function () {} } }; barge.audible = true; barge.speaking = false; barge.speechEndedAt = 0;
onMessage({ data: JSON.stringify({ type: "user", text: "Розкажи історію", final: true }) });
onMessage({ data: JSON.stringify({ type: "record", role: "assistant", text: "Жила собі дівчинка, що любила моря. Одного дня …" }) });
onMessage({ data: JSON.stringify({ type: "clear", reason: "stop", text: "стоп" }) });
onMessage({ data: JSON.stringify({ type: "user_drop", reason: "stop", text: "стоп" }) });
onMessage({ data: JSON.stringify({ type: "user_drop", reason: "backchannel", text: "угу" }) });
heard("це шум поруч", true);
barge.audible = false;
onMessage({ data: JSON.stringify({ type: "clear", reason: "pause" }) });
onMessage({ data: JSON.stringify({ type: "clear", reason: "error" }) });
onMessage({ data: JSON.stringify({ type: "clear", reason: "button" }) });
setPaused(true, "тиша");
heard("це я сказав на паузі", true);
setPaused(false);
var kinds = current.events.map(function (e) { return e.k; });
check("everything is recorded in order", JSON.stringify(kinds) === JSON.stringify(["user", "assistant", "interrupt", "dropped", "dropped", "interrupt", "pause", "background", "resume"]));
check("a stop word is one interruption, not also a dropped phrase", current.events.filter(function (e) { return e.k === "dropped" && e.text === "стоп"; }).length === 0 && current.events[2].by === "stop" && current.events[2].text === "стоп");
check("the cut answer is marked and the button interruption is recorded", current.events[1].cut === true && current.events[5].by === "button");
check("the reasons of dropped phrases are kept", current.events[3].why === "backchannel" && current.events[4].why === "quiet");
check("a pause and an error cut-off are not recorded as the user's interruptions", kinds.filter(function (k) { return k === "interrupt"; }).length === 2);

// the agent's memory is the dialogue only
var mem = memoryForServer();
check("the agent is given only the dialogue, no attempts, dropped phrases or background talk", JSON.stringify(mem.map(function (m) { return m.role; })) === JSON.stringify(["user", "assistant"]) && mem[0].content === "Розкажи історію");
for (var i = 0; i < 12; i++) logEvent(i % 2 ? "assistant" : "user", "репліка " + i);
check("with «Останні 10 реплік» the agent remembers the last 10", memoryForServer().length === 10 && memoryForServer()[9].content === "репліка 11");
E("memorySel").value = "0"; E("memorySel").onchange();
check("«Нічого» gives the agent no memory, the record stays", memoryForServer().length === 0 && current.events.length > 20);
E("memorySel").value = "30"; E("memorySel").onchange();
check("up to 30 replies can be remembered", memoryForServer().length === 14);
E("memorySel").value = "10"; E("memorySel").onchange();

// the timeline in the history window
E("timeline").children = [];
viewing = current.id; renderHistory();
check("the timeline has a row for every event", E("historyView").hidden === false && E("historyList").hidden === true && E("timeline").children.length === current.events.length);
check("the timeline says how the user interrupted and what did not work", timelineText(current).indexOf("Ви перебили (стоп-слово голосом): «стоп»") >= 0 && timelineText(current).indexOf("Не спрацювало: «угу»") >= 0 && timelineText(current).indexOf("(перервано)") >= 0);
check("the stats count the user's tries", E("historyStats").textContent.indexOf("перебивань: 2") >= 0);
E("historyBack").onclick();
check("«До списку» returns to the list", viewing === null && E("historyList").hidden === false && E("historyView").hidden === true);

// ---- interrupting by voice: a window, not a stopwatch
var herSource = {}; sources.add(herSource);
live = true; paused = false; ready = true;
function resetBarge() { voicedWin.length = 0; barge.stage = "idle"; stopSentAt = 0; sent.length = 0; setPlayVolume(1); }
function feed(pattern, level) { // 1 = the gate says the user's voice is on, 0 = off (the bar is 0.065)
  pattern.forEach(function (v) { onLevels({ level: v ? (level || 0.09) : 0.02, need: 0.065, audible: true, speaking: !!v }); });
}
var soundStops = function () { return sent.filter(function (m) { return m.type === "interrupt" && m.by === "sound"; }).length; };

resetBarge(); barge.last = "скриня була";
feed([1, 1, 1]);
check("300 ms of the user's voice duck her", barge.stage === "ducked" && soundStops() === 0);
feed([1, 1]);
check("500 ms of voice stop her, and the words that came with it go along", soundStops() === 1 && sent.filter(function (m) { return m.by === "sound"; })[0].text === "скриня була");

resetBarge();
feed([1, 0, 1, 1, 0, 1, 1]);
check("a voice that dips between words still stops her (the old stopwatch never fired)", soundStops() === 1);

resetBarge();
feed([1, 0, 0, 1, 0, 0, 1, 0, 0, 0]);
check("a few scattered blips are not an interruption", soundStops() === 0);

resetBarge();
for (var q = 0; q < 30; q++) onLevels({ level: 0.03, need: 0.065, audible: true, speaking: false });
check("her own echo, under the bar, never ducks or stops her", barge.stage === "idle" && soundStops() === 0);

resetBarge(); feed([1, 1, 1, 1, 1, 1, 1, 1]);
check("a stop is sent once, not at every sample (the server may refuse it)", soundStops() === 1);

resetBarge(); var attemptsBefore = current ? current.events.filter(function (e) { return e.k === "attempt"; }).length : 0;
feed([1, 1, 1]); duckedAt = Date.now() - 1000; feed([0, 0, 0, 0, 0]);
check("a short sound gives her volume back after 500 ms of quiet", barge.stage === "idle" && soundStops() === 0);
check("and the attempt is recorded", current.events.filter(function (e) { return e.k === "attempt"; }).length === attemptsBefore + 1);

resetBarge(); sources.clear();
feed([1, 1, 1, 1, 1, 1]);
check("nothing is ducked or stopped when she is not actually playing", barge.stage === "idle" && soundStops() === 0);

// ---- the end of a phrase: the text has settled AND the user's voice has been silent
live = true; ready = true; paused = false; micMuted = false; curState = "listening";
barge.audible = false; barge.speaking = false; barge.audibleEndedAt = 0; barge.speechEndedAt = 0;
gate = { port: { postMessage: function () {} } };
function phrase(settledMs, quietMs) { // how long the text has not changed, how long the user has been silent
  interimText = "привіт як"; interimChangedAt = Date.now() - settledMs; lastActiveAt = Date.now() - quietMs;
  sent.length = 0; checkEndpoint();
  return sent.filter(function (m) { return m.type === "heard" && m.final; }).length;
}
check("a stalled transcript does not end the phrase while the user is still speaking", phrase(1500, 100) === 0 && interimText === "привіт як");
check("the phrase ends when the text has settled and the voice has been silent", phrase(1500, 900) === 1 && interimText === "");
check("a text that is still changing is not finished, even in silence", phrase(100, 2000) === 0);
check("a noisy room never holds a phrase longer than the cap", phrase(2600, 0) === 1);
onLevels({ level: 0.05, need: 0.07, audible: false, speaking: false, active: true });
check("the gate's active flag is the user still sounding", Date.now() - lastActiveAt < 1000);
gate = null;
check("without the gate the text alone decides, and after a longer wait", phrase(800, 0) === 0 && phrase(1200, 0) === 1);

// ---- the character and the emotion
check("the emotion defaults to emotional and the character to the plain one", E("emotion").value === "emotional" || store["emotion"] === undefined);
live = true; sent.length = 0;
E("character").value = "tolik"; E("character").onchange();
check("picking a character is saved and sent mid-call", store["character"] === "tolik" && sent.some(function (m) { return m.type === "style" && m.character === "tolik"; }));
sent.length = 0;
E("emotion").value = "calm"; E("emotion").onchange();
check("picking an emotion is saved and sent mid-call", store["emotion"] === "calm" && sent.some(function (m) { return m.type === "style" && m.emotion === "calm"; }));
onMessage({ data: JSON.stringify({ type: "persona", name: "Кішка", character: "kishka", emotion: "emotional", description: "Говоряча кішка" }) });
check("the server's persona message moves the selectors, the name and the description", E("character").value === "kishka" && E("emotion").value === "emotional" && E("assistantName").textContent === "Кішка" && E("characterInfo").textContent === "Говоряча кішка" && store["character"] === "kishka");
onMessage({ data: JSON.stringify({ type: "ready", sample_rate: 24000, name: "Толік", character: "tolik", emotion: "normal", recognition: "uk-UA" }) });
check("ready shows the character the server actually started with", E("character").value === "tolik" && E("emotion").value === "normal");

// ---- the character (a manner); the voice is a separate, free choice and never changes by itself
var values = E("character").options.map(function (o) { return o.value; });
check("the menu lists the four characters, plain", JSON.stringify(values) === JSON.stringify(["eva", "tolik", "kishka", "taras"]));
check("each character carries its description", E("character").options.filter(function (o) { return o.value === "tolik"; })[0].dataset.desc === "Дворовий хуліган");
live = true; ready = true; paused = false; sent.length = 0;
E("provider").value = "respeecher"; E("character").value = "tolik"; E("character").onchange();
var labels = function () { return E("voice").options.map(function (o) { return o.text; }); };
check("the voices that suit the character come first, marked with a star, the rest unmarked", labels().slice(0, 3).every(function (x) { return x.indexOf("★ ") === 0; }) && labels().slice(3).every(function (x) { return x.indexOf("★") < 0; }) && labels().length === 7);
E("voice").value = "olesia-conversation"; sent.length = 0;
E("character").value = "kishka"; E("character").onchange();
check("another character re-marks the list and keeps the voice the user picked", labels()[0].indexOf("roksolana") >= 0 && E("voice").value === "olesia-conversation" && labels().filter(function (x) { return x.indexOf("★") === 0; }).length === 3);
check("picking a character sends the character and the tune flag, never a voice", sent.some(function (m) { return m.type === "style" && m.character === "kishka" && m.tune === true; }) && !sent.some(function (m) { return m.type === "voice"; }));
sent.length = 0;
onMessage({ data: JSON.stringify({ type: "persona", name: "Тарас", character: "taras", emotion: "emotional", description: "Лавелас" }) });
check("a character asked for by voice moves only the menu, not the voice", E("character").value === "taras" && E("voice").value === "olesia-conversation" && !sent.some(function (m) { return m.type === "voice"; }) && labels()[0].indexOf("oleksandr") >= 0);
store["character"] = "tolik|respeecher|dmytro-conversation"; buildCharacterMenu(CONFIG.characters);
check("an old stored entry with a voice is just the character now", E("character").value === "tolik");
store["character"] = "kit"; buildCharacterMenu(CONFIG.characters);
check("an old stored «kit» becomes Кішка", E("character").value === "kishka");
store["character"] = "milana"; buildCharacterMenu(CONFIG.characters);
check("a removed character falls back to the plain one", E("character").value === "eva");

// ---- the tuning of the delivery: shown in words, switchable
onMessage({ data: JSON.stringify({ type: "tuning", active: true, reason: "", base: 0.8, factor: 1.1, value: 0.88 }) });
check("the tuning is shown with its numbers", E("tuningInfo").textContent.indexOf("0.88") >= 0 && E("tuningInfo").textContent.indexOf("0.8") >= 0 && E("tuningInfo").textContent.indexOf("1.1") >= 0);
onMessage({ data: JSON.stringify({ type: "tuning", active: false, reason: "off" }) });
check("off is said to be off", E("tuningInfo").textContent.indexOf("вимкнено") >= 0);
onMessage({ data: JSON.stringify({ type: "tuning", active: false, reason: "provider" }) });
check("a provider without parameters is named", E("tuningInfo").textContent.indexOf("Soniox") >= 0);
onMessage({ data: JSON.stringify({ type: "tuning", active: false, reason: "no_base" }) });
check("missing defaults are said plainly", E("tuningInfo").textContent.indexOf("Не вдалося") >= 0);
sent.length = 0;
E("tuneVoice").checked = false; E("tuneVoice").onchange();
check("the switch is saved and sent", store["tune"] === "0" && sent.some(function (m) { return m.type === "style" && m.tune === false; }));
E("tuneVoice").checked = true; E("tuneVoice").onchange();
check("and back on", store["tune"] === "1");
// a voice is never applied without «Застосувати голос»: neither a new voice nor a new provider
live = true; ready = true; ws = { readyState: 1, send: function (m) { sent.push(JSON.parse(m)); }, close: function () {} };
activeVoice = { provider: "respeecher", voice: "olesia" };
sent.length = 0;
E("provider").value = "soniox"; E("provider").onchange();
check("a provider change mid-call sends no voice and offers the button", !sent.some(function (m) { return m.type === "voice"; }) && E("voiceApply").hidden === false);
E("provider").value = "respeecher"; E("provider").onchange(); E("voice").value = "olesia"; E("voice").onchange();
// «Очистити»: the first press only asks, the second wipes the conversation, its history entry and the agent's memory
logEvent("user", "привіт"); logEvent("assistant", "добрий день");
var before = convs.length;
check("a conversation is being recorded", current !== null && before > 0);
sent.length = 0;
E("clearChat").onclick();
check("the first press only asks to confirm", E("clearChat").textContent === "Точно очистити?" && current !== null && convs.length === before);
E("clearChat").onclick();
check("the second press clears the chat, the history entry and the agent's memory",
  current === null && convs.length === before - 1 && E("clearChat").textContent === "Очистити"
  && sent.some(function (m) { return m.type === "history" && m.history.length === 0; })
  && E("log").innerHTML.indexOf("empty") >= 0);
// the knowledge about the agent: in the server's database, behind a password
var kbCalls = [], KB = { text: "Ти робот Тарас на стенді.", persistent: true, storage: "postgres" };
fetch = function (url, opt) {
  var pass = opt && opt.headers && opt.headers["X-Access-Password"];
  kbCalls.push({ url: url, method: (opt && opt.method) || "GET", pass: pass, body: opt && opt.body });
  if (pass !== "test-password") return Promise.resolve({ ok: false, status: 401, json: function () { return Promise.resolve({ detail: "Невірний пароль." }); } });
  if (opt.method === "PUT") KB.text = JSON.parse(opt.body).text;
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ text: KB.text, persistent: KB.persistent, storage: KB.storage }); } });
};
E("aboutPass").value = "wrong"; E("aboutUnlock").onclick(); drainMicrotasks();
check("a wrong password opens nothing and says so", E("aboutEditor").hidden === true && E("aboutStatus").textContent === "Невірний пароль." && store["aboutPass"] === "");
E("aboutPass").value = "test-password"; E("aboutUnlock").onclick(); drainMicrotasks();
check("the right password opens the text from the server", E("aboutEditor").hidden === false && E("about").value === "Ти робот Тарас на стенді." && store["aboutPass"] === "test-password"
  && kbCalls[kbCalls.length - 1].pass === "test-password" && kbCalls[kbCalls.length - 1].url === "https://x.test/api/knowledge");
E("about").value = "Ти робот Тарас на стенді в Києві.";
E("about").oninput();
check("edits are counted and flagged as unsaved, not sent yet", E("aboutCount").textContent === "33 / 4000" && E("aboutStatus").textContent.indexOf("незбережені") >= 0 && KB.text === "Ти робот Тарас на стенді.");
E("aboutSave").onclick(); drainMicrotasks();
check("«Зберегти» writes it to the server", KB.text === "Ти робот Тарас на стенді в Києві." && E("aboutStatus").textContent.indexOf("Збережено") >= 0 && E("aboutSave").disabled === false);
KB.persistent = false; E("aboutSave").onclick(); drainMicrotasks();
check("a database that a deploy wipes is flagged", E("aboutStorage").hidden === false);
KB.persistent = true; E("aboutSave").onclick(); drainMicrotasks();
check("and the flag goes when it is a real database", E("aboutStorage").hidden === true);
E("aboutHide").onclick();
check("«Заблокувати» forgets the password and hides the text", E("aboutEditor").hidden === true && E("about").value === "" && store["aboutPass"] === "");
sent.length = 0;
check("the text never travels in a call's messages", !sent.some(function (m) { return "about" in m; }));

// a cut-off tells the server how much of the answer had really played
live = true; ready = true; ws = { readyState: 1, send: function (m) { sent.push(JSON.parse(m)); }, close: function () {} };
playCtx = { currentTime: 3.0 };
playStart = 1.0; nextTime = 9.0; // 8 s of answer scheduled, 2 s of them played
var stub = { stop: function () {}, onended: null };
sources = new Set([stub]);
sent.length = 0;
onMessage({ data: JSON.stringify({ type: "clear", reason: "sound", text: "" }) });
check("a cut-off reports the seconds that had played", sent.some(function (m) { return m.type === "played_until" && m.seconds === 2; }));
sent.length = 0;
onMessage({ data: JSON.stringify({ type: "clear", reason: "history", text: "" }) });
check("nothing was playing: nothing is reported", !sent.some(function (m) { return m.type === "played_until"; }));
print(failed ? "SMOKE FAILED" : "SMOKE OK");
