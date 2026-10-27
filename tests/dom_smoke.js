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
    querySelectorAll: function () { return []; }, remove: function () {}, select: function () {}, scrollTop: 0, scrollHeight: 0,
  };
  return e;
}
var document = { getElementById: E, createElement: function () { return E("_new" + Math.random()); }, body: E("body"),
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
live = true; paused = false;
onGate({ levels: { level: 0.03, need: 0.065 } });
check("level line shows the voice against the bar", E("bargeHint").hidden === false && E("bargeHint").textContent.indexOf("3.0") >= 0 && E("bargeHint").textContent.indexOf("6.5") >= 0);
onGate({ levels: null });
check("level line hides when she is silent", E("bargeHint").hidden === true);
print(failed ? "SMOKE FAILED" : "SMOKE OK");
