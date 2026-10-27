// Runs web/app.js against a fake DOM: load-time errors (undefined names, TDZ), message handling, meter, history.
var store = {};
var made = {};
function E(id) {
  var e = made[id];
  if (e) return e;
  e = made[id] = {
    id: id, value: "", textContent: "", innerHTML: "", hidden: false, disabled: false, checked: true, title: "", className: "",
    options: [], children: [], style: {}, _attrs: {},
    classList: { add: function () {}, remove: function () {}, toggle: function () {}, contains: function () { return false; } },
    add: function (o) { this.options.push(o); if (this.options.length === 1 || !this.value) this.value = o.value; },
    setAttribute: function (k, v) { this._attrs[k] = v; }, addEventListener: function () {},
    appendChild: function (c) { this.children.push(c); }, querySelector: function () { return null; },
    querySelectorAll: function () { return []; }, remove: function () {},
    open: false, showModal: function () { this.open = true; }, close: function () { this.open = false; }, select: function () {}, scrollTop: 0, scrollHeight: 0,
  };
  return e;
}
var document = { querySelectorAll: function () { return []; }, getElementById: E, createElement: function () { return E("_new" + Math.random()); }, body: E("body"),
  visibilityState: "visible", execCommand: function () { return true; } };
function Option(label, value) { this.text = label; this.value = value; }
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
var fetch = function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ providers: [{ id: "soniox", label: "Soniox", voices: [{ id: "Daniel", label: "Daniel (male)" }] }], missing: [] }); } }); };
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

check("init filled the provider list", E("provider").options.length === 1 && E("voice").options.length === 1);
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
E("provider").value = "soniox"; E("voice").value = "Daniel"; E("voice").onchange();
check("selector change mid-call is sent", sent.some(function (m) { return m.type === "voice" && m.voice === "Daniel"; }));

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
sent.length = 0;
newConversation();
check("a new conversation has no current one and tells the server", current === null && sent.some(function (m) { return m.type === "history" && m.history.length === 0; }));
onMessage({ data: JSON.stringify({ type: "user", text: "Мене звати Віктор", final: true }) });
onMessage({ data: JSON.stringify({ type: "record", role: "assistant", text: "Приємно, Вікторе." }) });
check("the conversation is kept in the browser", current && current.msgs.length === 2 && current.title === "Мене звати Віктор" && JSON.parse(store["convs"]).length === 1);
check("the server is given the past on the next call", JSON.stringify(historyForServer()) === JSON.stringify([{ role: "user", content: "Мене звати Віктор" }, { role: "assistant", content: "Приємно, Вікторе." }]));
var firstId = current.id;
newConversation();
onMessage({ data: JSON.stringify({ type: "user", text: "Інша розмова", final: true }) });
check("a new conversation is a separate entry", convs.length === 2 && current.id !== firstId && historyForServer().length === 1);
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
print(failed ? "SMOKE FAILED" : "SMOKE OK");
