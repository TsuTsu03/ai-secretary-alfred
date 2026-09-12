/* Alfred — HUD client.
 *
 * Deliberately dependency-free: no build step, no CDN, and a Content-Security-
 * Policy of script-src 'self'. The whole client is this file.
 *
 * Three things here are less obvious than they look, all of them iOS Safari:
 *   1. Audio will not play until a user gesture has unlocked an AudioContext.
 *   2. MediaRecorder emits audio/mp4, not audio/webm.
 *   3. getUserMedia is refused entirely unless the page is on real HTTPS,
 *      which is why the server is fronted by `tailscale serve`.
 */
"use strict";

const TOKEN_KEY = "alfred.token";

const el = (id) => document.getElementById(id);
const ui = {
  log: el("log"), empty: el("empty"), emptyLine: el("emptyLine"),
  input: el("input"), send: el("send"), orb: el("orb"),
  connDot: el("connDot"), connText: el("connText"), headTitle: el("headTitle"),
  rail: el("rail"), railToggle: el("railToggle"),
  pairBtn: el("pairBtn"), pairVeil: el("pairVeil"), qrBox: el("qrBox"),
  pairUrl: el("pairUrl"), pairClose: el("pairClose"), pairNote: el("pairNote"),
  authVeil: el("authVeil"), tokenInput: el("tokenInput"), tokenSave: el("tokenSave"),
  sProvider: el("sProvider"), sStt: el("sStt"), sTts: el("sTts"),
  sGoogle: el("sGoogle"), sMail: el("sMail"), sUser: el("sUser"),
  sBriefing: el("sBriefing"), sPush: el("sPush"),
  pushBtn: el("pushBtn"), briefBtn: el("briefBtn"), pushNote: el("pushNote"),
  sTz: el("sTz"), sNet: el("sNet"), sRoots: el("sRoots"),
};

const state = {
  token: "",
  status: null,
  conversationId: null,
  busy: false,
  audioUnlocked: false,
  audioCtx: null,
  recording: null,
  meter: null,
  audio: null,
};

/* ── token ────────────────────────────────────────────────── */

/* The pairing QR encodes the token in the URL fragment. A fragment is never
 * sent to the server, so the token reaches the phone without being written
 * into any access log along the way. Consume it and scrub the address bar. */
function adoptTokenFromFragment() {
  const match = /[#&]token=([^&]+)/.exec(location.hash || "");
  if (!match) return;
  localStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1]));
  history.replaceState(null, "", location.pathname + location.search);
}

function loadToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return ""; // Private browsing can throw on access.
  }
}

function saveToken(value) {
  try {
    localStorage.setItem(TOKEN_KEY, value);
  } catch { /* nothing we can do; the session still works in memory */ }
  state.token = value;
}

function authHeaders(extra) {
  return Object.assign({ Authorization: `Bearer ${state.token}` }, extra || {});
}

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers = authHeaders(opts.headers);
  const response = await fetch(path, opts);
  if (response.status === 401) {
    showAuthVeil();
    throw new Error("unauthorised");
  }
  return response;
}

/* ── connection ───────────────────────────────────────────── */

function setConnection(mode, label) {
  ui.connDot.className = "dot" + (mode === "live" ? " dot--live" : mode === "down" ? " dot--down" : "");
  ui.connText.textContent = label;
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 5) return "You are still awake. I have elected not to comment.";
  if (hour < 12) return "Good morning. The day is, regrettably, already underway.";
  if (hour < 18) return "Good afternoon. What requires attention?";
  return "Good evening. I trust the day was survivable.";
}

async function refreshStatus() {
  try {
    const response = await api("/api/status");
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    state.status = data;
    renderStatus(data);
    setConnection("live", data.llm_ready ? "At your service" : "No mind");
    ui.headTitle.textContent = data.llm_ready
      ? "Standing by"
      : "Add GEMINI_API_KEY to .env — Alfred cannot think without one";
  } catch (error) {
    if (String(error.message) === "unauthorised") return;
    setConnection("down", "Unreachable");
    ui.headTitle.textContent = "Cannot reach the study";
  }
}

function renderStatus(data) {
  const provider = data.active_provider || "";
  ui.sProvider.textContent = provider ? provider.toUpperCase() : "None";
  ui.sProvider.className = "stat__v" + (provider ? " stat__v--amber" : " stat__v--off");

  const out = data.voice_out || {};
  const inn = data.voice_in || {};

  ui.sStt.textContent = inn.ready ? `${inn.model} ${inn.device}` : "Unavailable";
  ui.sStt.className = "stat__v" + (inn.ready ? " stat__v--amber" : " stat__v--off");
  ui.sStt.title = inn.ready ? `Whisper ${inn.model} on ${inn.device}` : (inn.detail || "");

  ui.sTts.textContent = out.engine === "browser" ? "Browser" : (out.ready ? out.voice : "Not installed");
  ui.sTts.className = "stat__v" + (out.ready ? " stat__v--amber" : " stat__v--off");
  ui.sTts.title = out.ready ? "" : (out.detail || "");

  /* Calendar and mail share one connection but deserve separate lines: the
   * interesting fact about mail is that Alfred can draft and cannot send, and
   * burying that in a combined "Google: connected" hides the one thing worth
   * knowing. */
  const google = data.google || {};
  const tools = data.tools || [];
  const hasCalendar = tools.includes("list_events");
  const hasMail = tools.includes("search_mail");

  if (!google.configured) {
    ui.sGoogle.textContent = "Not set up";
    ui.sMail.textContent = "Not set up";
    ui.sGoogle.title = ui.sMail.title = "Run scripts/connect_google.py to connect.";
  } else if (!google.connected) {
    ui.sGoogle.textContent = "Not authorised";
    ui.sMail.textContent = "Not authorised";
    ui.sGoogle.title = ui.sMail.title = "Credentials found. Run scripts/connect_google.py.";
  } else {
    ui.sGoogle.textContent = hasCalendar ? "Connected" : "Unavailable";
    ui.sMail.textContent = hasMail ? "Draft only" : "Unavailable";
    ui.sGoogle.title = google.account || "";
    ui.sMail.title =
      "Reads and drafts. No send tool exists — the scope would permit it, Alfred does not.";
  }
  ui.sGoogle.className = "stat__v" + (google.connected && hasCalendar ? " stat__v--amber" : " stat__v--off");
  ui.sMail.className = "stat__v" + (google.connected && hasMail ? " stat__v--amber" : " stat__v--off");

  renderBriefing(data.briefing || {});

  ui.sUser.textContent = data.user_name || "—";
  ui.sTz.textContent = data.timezone || "—";

  ui.sNet.textContent = data.tailscale_configured ? "Tailscale" : "Local only";
  ui.sNet.className = "stat__v" + (data.tailscale_configured ? " stat__v--amber" : " stat__v--off");

  ui.sRoots.innerHTML = "";
  if (!data.roots.length) {
    const none = document.createElement("div");
    none.className = "rootline";
    none.textContent = "No folders configured";
    ui.sRoots.appendChild(none);
    return;
  }
  for (const root of data.roots) {
    const line = document.createElement("div");
    line.className = "rootline";
    line.textContent = shortenPath(root.path);
    line.title = root.exists ? root.path : root.path + " (missing)";
    if (!root.exists) line.style.opacity = "0.45";
    ui.sRoots.appendChild(line);
  }
}

function renderBriefing(briefing) {
  ui.sBriefing.textContent = briefing.running ? (briefing.at || "—") : "Off";
  ui.sBriefing.className = "stat__v" + (briefing.running ? " stat__v--amber" : " stat__v--off");
  ui.sBriefing.title = briefing.next_run ? `Next: ${briefing.next_run}` : "";

  const count = briefing.subscribers || 0;
  const permission = ("Notification" in window) ? Notification.permission : "unsupported";

  if (permission === "unsupported") {
    ui.sPush.textContent = "Unsupported";
  } else if (permission === "denied") {
    ui.sPush.textContent = "Blocked";
  } else {
    ui.sPush.textContent = count ? `${count} device${count > 1 ? "s" : ""}` : "None";
  }
  ui.sPush.className = "stat__v" + (count ? " stat__v--amber" : " stat__v--off");

  /* iOS delivers Web Push only to a PWA installed from a real HTTPS origin.
   * Saying so here turns "the briefing never arrives" from a mystery into a
   * known requirement. */
  const standalone = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  const iOS = /iPad|iPhone|iPod/.test(navigator.userAgent);

  let note = "";
  if (permission === "denied") {
    note = "Notifications are blocked for this site. Allow them in your browser settings.";
  } else if (iOS && !standalone) {
    note = "On iPhone, notifications only work once Alfred is added to the Home Screen " +
           "(Share → Add to Home Screen) from an https address.";
  } else if (!window.isSecureContext) {
    note = "Notifications need a secure origin. Reach Alfred over the https://…ts.net address.";
  }
  ui.pushNote.textContent = note;
  ui.pushNote.hidden = !note;
  ui.pushBtn.textContent = count ? "Re-enable" : "Enable";
}

/* The applicationServerKey must be a Uint8Array of the raw P-256 point, and
 * the server sends it base64url. Nothing converts that for you. */
function urlBase64ToUint8Array(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
  return output;
}

async function enablePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    addTurn("system", "This browser cannot receive notifications.");
    return;
  }

  ui.pushBtn.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      addTurn("system", "Without permission I cannot reach you in the morning, sir.");
      return;
    }

    const registration = await navigator.serviceWorker.ready;
    const keyResponse = await api("/api/push/key");
    const { public_key: publicKey } = await keyResponse.json();

    // An existing subscription made with a different key must go, or the push
    // service keeps accepting it and the server can never decrypt for it.
    const existing = await registration.pushManager.getSubscription();
    if (existing) await existing.unsubscribe();

    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    });

    const json = subscription.toJSON();
    const response = await api("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        endpoint: json.endpoint,
        p256dh: json.keys.p256dh,
        auth: json.keys.auth,
        label: navigator.platform || "Device",
      }),
    });
    if (!response.ok) throw new Error(`server said ${response.status}`);

    addTurn("system", "Very good. I shall wake you with the briefing.");
    await refreshStatus();
  } catch (error) {
    if (String(error.message) !== "unauthorised") {
      addTurn("system", `I could not arrange notifications: ${error.message}`);
    }
  } finally {
    ui.pushBtn.disabled = false;
  }
}

async function showBriefing(generate) {
  ui.briefBtn.disabled = true;
  ui.headTitle.textContent = generate ? "Preparing your briefing" : "Fetching the briefing";
  try {
    const response = generate
      ? await api("/api/briefing/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ deliver: false }),
        })
      : await api("/api/briefing");
    const data = await response.json();
    if (!data.content) {
      addTurn("system", "There is no briefing yet, sir.");
      return;
    }
    addTurn("alfred", data.content);
  } catch (error) {
    if (String(error.message) !== "unauthorised") {
      addTurn("system", `I could not fetch the briefing: ${error.message}`);
    }
  } finally {
    ui.briefBtn.disabled = false;
    ui.headTitle.textContent = "Standing by";
  }
}

/* Two roots under Downloads truncate to the same string if the tail is cut,
 * which is exactly backwards: the tail is the part that distinguishes them.
 * Keep the drive and the last two segments, elide the middle. */
function shortenPath(path) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 3) return path;
  const sep = path.includes("\\") ? "\\" : "/";
  return [parts[0], "…", parts[parts.length - 2], parts[parts.length - 1]].join(sep);
}

/* ── transcript ───────────────────────────────────────────── */

function clearEmpty() {
  if (ui.empty && ui.empty.parentNode) ui.empty.remove();
}

function addTurn(who, text) {
  clearEmpty();
  const turn = document.createElement("div");
  turn.className = `turn turn--${who}`;

  const label = document.createElement("div");
  label.className = "turn__who";
  label.textContent = who === "alfred" ? "Alfred" : who === "user" ? "You" : "System";

  const body = document.createElement("div");
  body.className = "turn__body";
  body.textContent = text;

  turn.append(label, body);
  ui.log.appendChild(turn);
  scrollLog();
  return body;
}

/* A one-line trace of what Alfred just did. Without this, a turn that reads
 * six files is ten silent seconds that look like a hang. */
function addToolLine(event) {
  clearEmpty();
  const line = document.createElement("div");
  line.className = "trace" + (event.queued ? " trace--queued" : "");

  const label = TOOL_LABELS[event.name] || event.name;
  const detail = event.arguments && (event.arguments.query || event.arguments.path || "");
  line.textContent = detail ? `${label} — ${detail}` : label;

  ui.log.appendChild(line);
  scrollLog();
}

const TOOL_LABELS = {
  search_files: "Searched your files",
  read_file: "Read",
  list_directory: "Listed",
  write_file: "Proposed a change to",
};

/* An approval card. Amber, because it needs Jansen — the same meaning the
 * listening ring carries, so attention never has two colours. */
function addConfirmCard(action) {
  if (document.querySelector(`[data-action="${action.id}"]`)) return;
  clearEmpty();

  const card = document.createElement("div");
  card.className = "confirm";
  card.dataset.action = String(action.id);

  const head = document.createElement("div");
  head.className = "confirm__head";
  head.textContent = "Awaiting your approval";

  const summary = document.createElement("div");
  summary.className = "confirm__summary";
  summary.textContent = action.summary;

  const detail = document.createElement("pre");
  detail.className = "confirm__detail";
  detail.textContent = action.detail || "";

  const row = document.createElement("div");
  row.className = "confirm__row";

  const approve = document.createElement("button");
  approve.className = "btn btn--go";
  approve.textContent = "Approve";

  const decline = document.createElement("button");
  decline.className = "btn";
  decline.textContent = "Decline";

  const decide = async (ok) => {
    approve.disabled = decline.disabled = true;
    try {
      const response = await api(`/api/actions/${action.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve: ok }),
      });
      const data = await response.json().catch(() => ({}));
      row.remove();
      head.textContent = ok ? "Done" : "Declined";
      card.classList.remove("confirm--danger");
      const outcome = document.createElement("div");
      outcome.className = "confirm__summary";
      outcome.textContent = data.result || (ok ? "Done, sir." : "Very good, sir.");
      card.appendChild(outcome);
      if (!ok) detail.remove();
    } catch (error) {
      approve.disabled = decline.disabled = false;
      if (String(error.message) !== "unauthorised") {
        addTurn("system", `That did not go through: ${error.message}`);
      }
    }
  };

  approve.addEventListener("click", () => decide(true));
  decline.addEventListener("click", () => decide(false));

  row.append(approve, decline);
  card.append(head, summary, detail, row);
  ui.log.appendChild(card);
  scrollLog();
}

function scrollLog() {
  ui.log.scrollTop = ui.log.scrollHeight;
}

function setOrbMode(mode) {
  ui.orb.dataset.mode = mode;
  if (mode !== "listening" && mode !== "speaking") {
    ui.orb.style.removeProperty("--amp");
  }
}

/* ── sending ──────────────────────────────────────────────── */

function autoGrow() {
  ui.input.style.height = "auto";
  ui.input.style.height = Math.min(ui.input.scrollHeight, 132) + "px";
  ui.send.disabled = !ui.input.value.trim() || state.busy;
}

async function send(text, options) {
  const message = (text || "").trim();
  if (!message || state.busy) return;
  const byVoice = Boolean(options && options.voice);

  stopSpeaking();
  state.busy = true;
  ui.send.disabled = true;
  addTurn("user", message);
  ui.input.value = "";
  autoGrow();
  setOrbMode("thinking");
  ui.headTitle.textContent = "Considering";

  const body = addTurn("alfred", "");
  let received = "";
  // Voice replies are synthesized sentence by sentence as the text arrives,
  // so the speaking starts with the first full stop rather than the last one.
  if (byVoice) beginSpeech();

  try {
    const response = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: state.conversationId, voice: byVoice }),
    });

    if (response.status === 404) {
      body.parentElement.remove();
      addTurn("system", "Conversation is not wired up yet — that arrives in the next phase.");
      return;
    }
    if (!response.ok || !response.body) {
      const detail = await response.text().catch(() => "");
      body.parentElement.remove();
      addTurn("system", `Alfred could not answer (${response.status}). ${detail.slice(0, 300)}`);
      return;
    }

    /* Server-sent events over fetch rather than EventSource, because
     * EventSource cannot set an Authorization header. */
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        let payload;
        try {
          payload = JSON.parse(line.slice(5).trim());
        } catch {
          continue;
        }
        if (payload.type === "delta") {
          received += payload.text;
          body.textContent = received;
          if (byVoice) feedSpeech(received);
          scrollLog();
        } else if (payload.type === "meta") {
          state.conversationId = payload.conversation_id ?? state.conversationId;
          if (payload.provider) {
            ui.sProvider.textContent = String(payload.provider).toUpperCase();
            ui.sProvider.className = "stat__v stat__v--amber";
          }
        } else if (payload.type === "tool") {
          addToolLine(payload);
        } else if (payload.type === "pending") {
          addConfirmCard(payload);
        } else if (payload.type === "error") {
          body.textContent = received || payload.message;
          if (received) addTurn("system", payload.message);
        }
      }
    }

    if (!received.trim()) body.textContent = "…I appear to have nothing useful to say.";
  } catch (error) {
    if (String(error.message) !== "unauthorised") {
      body.parentElement.remove();
      addTurn("system", `Alfred could not answer: ${error.message}`);
    }
  } finally {
    state.busy = false;
    ui.headTitle.textContent = "Standing by";
    autoGrow();
    scrollLog();
    if (byVoice) {
      // The speech pipeline owns the orb until the last clip has played.
      endSpeech(received);
    } else {
      setOrbMode("idle");
    }
  }
}

/* ── voice ────────────────────────────────────────────────── */

/* iOS will not play audio that was not started from a user gesture. Unlocking
 * an AudioContext on the first tap buys the right to speak later, when Alfred
 * answers on his own schedule. */
function unlockAudio() {
  if (state.audioUnlocked) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    if (ctx.state === "suspended") ctx.resume();
    state.audioCtx = ctx;
    state.audioUnlocked = true;
  } catch { /* no audio on this device; text still works */ }
}

/* Safari refuses getUserMedia outright on a non-secure origin, and it does so
 * by rejecting rather than by any signal you can check in advance. Detect the
 * cause ourselves so the message names the real fix. */
function micUnavailableReason() {
  if (!window.isSecureContext) {
    return (
      "Voice needs a secure connection, sir. Reach me at the https://…ts.net address " +
      "`tailscale serve` provides — Safari refuses microphone access on plain HTTP."
    );
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return "This browser will not give me access to a microphone.";
  }
  if (typeof MediaRecorder === "undefined") {
    return "This browser cannot record audio.";
  }
  return "";
}

/* Safari produces audio/mp4; Chrome produces audio/webm. Rather than assume,
 * ask the browser what it can actually record. The server sends whatever
 * arrives through FFmpeg, so any of these is fine. */
function pickMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/mp4;codecs=mp4a.40.2",
    "audio/ogg;codecs=opus",
  ];
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return "";
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

async function startRecording() {
  if (state.recording || state.busy) return;

  const reason = micUnavailableReason();
  if (reason) {
    setOrbMode("denied");
    setTimeout(() => setOrbMode("idle"), 1400);
    addTurn("system", reason);
    return;
  }

  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    setOrbMode("denied");
    setTimeout(() => setOrbMode("idle"), 1400);
    addTurn(
      "system",
      error && error.name === "NotAllowedError"
        ? "You have not granted me the microphone, sir."
        : `I could not open the microphone: ${error.message}`
    );
    return;
  }

  const mimeType = pickMimeType();
  let recorder;
  try {
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  } catch {
    recorder = new MediaRecorder(stream);
  }

  const chunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  });
  recorder.addEventListener("stop", () => {
    stopMeter();
    stream.getTracks().forEach((track) => track.stop());
    const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
    state.recording = null;
    // Anything this short is a mis-tap, not speech.
    if (blob.size < 2000 || Date.now() - startedAt < 350) {
      setOrbMode("idle");
      ui.headTitle.textContent = "Standing by";
      return;
    }
    sendRecording(blob);
  });

  const startedAt = Date.now();
  state.recording = recorder;
  recorder.start();
  startMeter(stream);
  setOrbMode("listening");
  ui.headTitle.textContent = "Listening";
}

function stopRecording() {
  if (state.recording && state.recording.state !== "inactive") {
    state.recording.stop();
  }
}

/* Drive the orb's core from live microphone amplitude. The ring answers the
 * room rather than performing at it, which is the whole point of putting a
 * meter here instead of a canned pulse. */
function startMeter(stream) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = state.audioCtx && state.audioCtx.state !== "closed" ? state.audioCtx : new Ctx();
    state.audioCtx = ctx;
    if (ctx.state === "suspended") ctx.resume();

    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.75;
    source.connect(analyser);

    const buffer = new Uint8Array(analyser.frequencyBinCount);
    state.meter = { source, analyser, raf: 0 };

    const tick = () => {
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i++) {
        const v = (buffer[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buffer.length);
      // Speech RMS sits well below 1. Map it to a restrained 1.0–1.35 scale:
      // a ring that doubles in size reads as a toy, not an instrument.
      const amp = Math.min(1.35, 1 + rms * 2.2);
      ui.orb.style.setProperty("--amp", amp.toFixed(3));
      state.meter.raf = requestAnimationFrame(tick);
    };
    tick();
  } catch { /* the meter is decoration; recording continues without it */ }
}

function stopMeter() {
  if (!state.meter) return;
  cancelAnimationFrame(state.meter.raf);
  try { state.meter.source.disconnect(); } catch { /* already gone */ }
  state.meter = null;
  ui.orb.style.removeProperty("--amp");
}

async function sendRecording(blob) {
  setOrbMode("thinking");
  ui.headTitle.textContent = "Transcribing";

  const form = new FormData();
  const extension = (blob.type || "").includes("mp4") ? "m4a" : "webm";
  form.append("audio", blob, `clip.${extension}`);

  try {
    const response = await api("/api/voice/transcribe", { method: "POST", body: form });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      setOrbMode("idle");
      ui.headTitle.textContent = "Standing by";
      addTurn("system", detail.detail || `I could not make that out (${response.status}).`);
      return;
    }
    const data = await response.json();
    if (data.empty || !data.text.trim()) {
      setOrbMode("idle");
      ui.headTitle.textContent = "Standing by";
      addTurn("system", "I heard nothing, sir.");
      return;
    }
    send(data.text, { voice: true });
  } catch (error) {
    setOrbMode("idle");
    ui.headTitle.textContent = "Standing by";
    if (String(error.message) !== "unauthorised") {
      addTurn("system", `I could not make that out: ${error.message}`);
    }
  }
}

/* Speak a reply *while it is still being written*.
 *
 * Waiting for the whole reply before synthesizing it meant the voice trailed
 * the text by however long the model took to finish — several seconds on a
 * long answer. Instead the text stream is cut into sentences as it arrives,
 * each sentence is synthesized on its own, and playback starts on the first
 * one. Alfred begins talking roughly when the first full stop appears.
 *
 * Two requests are kept in flight at once: enough that the next clip is ready
 * before the current one ends, not so many that a long answer floods a CPU
 * that is also running the model. */
const SPEECH_CONCURRENCY = 2;
/* …and once the opening sentence passes this length with no full stop in it,
 * it is cut at the last comma before SPEECH_FIRST_CLAUSE_MAX instead. */
const SPEECH_FIRST_CLAUSE = 70;
const SPEECH_FIRST_CLAUSE_MAX = 110;
/* Later clips get longer. Each synthesis request costs about 1.4s before it
 * produces a sample, measured against the local Kokoro build, so a reply cut
 * into many small clips pays that toll many times and the queue falls behind
 * the playback. Short at the start where latency is felt, long afterwards
 * where throughput is. */
const SPEECH_CHUNK_RAMP = [16, 110, 200, 260];
/* A sentence that never ends still has to be spoken eventually. */
const SPEECH_MAX_CHUNK = 420;
/* A full stop in "Mr." or "e.g." is not the end of a sentence. "Sir." is not
 * on the list: Alfred ends sentences with it constantly and abbreviates it
 * never. */
const ABBREVIATION = /\b(mr|mrs|ms|dr|st|prof|no|vs|approx|e\.g|i\.e|etc)\.$/i;

const speech = {
  gen: 0,        // bumped on every stop; stale callbacks check it and bail
  active: false,
  items: [],     // { text, state: idle|loading|ready|failed, blob, controller }
  cursor: 0,     // index of the next item to play
  buffer: "",    // reply text not yet cut into a chunk
  consumed: 0,   // characters of the reply already moved into `buffer`
  done: false,   // the text stream has finished
  playing: false,
  browser: false, // server voice unavailable; fall back to the device's
};

function beginSpeech() {
  stopSpeaking();
  speech.active = true;
  speech.done = false;
  speech.browser = false;
  speech.items = [];
  speech.cursor = 0;
  speech.buffer = "";
  speech.consumed = 0;
}

/* Hand the pipeline everything received so far; it takes what is new. */
function feedSpeech(fullText) {
  if (!speech.active) return;
  speech.buffer += fullText.slice(speech.consumed);
  speech.consumed = fullText.length;
  drainSpeech(false);
}

function endSpeech(fullText) {
  if (!speech.active) return;
  speech.buffer += fullText.slice(speech.consumed);
  speech.consumed = fullText.length;
  speech.done = true;
  drainSpeech(true);
  if (!speech.items.length) finishSpeech();
  else playSpeechQueue();
}

function finishSpeech() {
  speech.active = false;
  speech.playing = false;
  setOrbMode("idle");
}

function drainSpeech(flush) {
  for (;;) {
    const first = speech.items.length === 0;
    const min = SPEECH_CHUNK_RAMP[Math.min(speech.items.length, SPEECH_CHUNK_RAMP.length - 1)];
    const cut = findSpeechCut(speech.buffer, min, first ? SPEECH_FIRST_CLAUSE : 0);
    if (cut <= 0) break;
    queueSpeech(speech.buffer.slice(0, cut));
    speech.buffer = speech.buffer.slice(cut);
  }
  if (flush) {
    queueSpeech(speech.buffer);
    speech.buffer = "";
  }
}

/* Where the first speakable chunk of `text` ends, or 0 if it is not there yet.
 * Boundaries are sentence-final punctuation followed by whitespace, so "3.5"
 * and "notes.txt" do not split.
 *
 * `clause`, when non-zero, is the length past which the opening clip may be
 * cut at a comma instead. Only the opening clip does this, and only when no
 * full stop has arrived yet: a long first sentence costs as much to synthesize
 * as it does to say, and every second of it is silence. */
function findSpeechCut(text, min, clause) {
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch !== "." && ch !== "!" && ch !== "?" && ch !== "…" && ch !== "\n") continue;
    let end = i + 1;
    while (end < text.length && /["')\]]/.test(text[end])) end += 1;
    const next = text[end];
    if (next !== undefined && !/\s/.test(next)) continue;
    if (end < min) continue;
    if (ABBREVIATION.test(text.slice(0, i + 1))) continue;
    while (end < text.length && /\s/.test(text[end])) end += 1;
    return end;
  }
  // The opening sentence is running long. Break at its last comma so Alfred
  // starts talking on the first clause instead of the finished thought.
  if (clause && text.length >= clause) {
    const head = text.slice(0, SPEECH_FIRST_CLAUSE_MAX);
    const mark = Math.max(head.lastIndexOf(", "), head.lastIndexOf("; "));
    if (mark + 2 >= min) return mark + 2;
  }
  // No full stop in sight and the buffer is long: break at the last space so
  // the listener is not left waiting on a run-on sentence.
  if (text.length >= SPEECH_MAX_CHUNK) {
    const space = text.slice(0, SPEECH_MAX_CHUNK).lastIndexOf(" ");
    if (space >= min) return space + 1;
  }
  return 0;
}

function queueSpeech(text) {
  const spoken = text.trim();
  if (!spoken) return;
  speech.items.push({ text: spoken, state: "idle", blob: null, controller: null });
  pumpSpeech();
  playSpeechQueue();
}

function pumpSpeech() {
  let loading = speech.items.filter((item) => item.state === "loading").length;
  for (const item of speech.items) {
    if (loading >= SPEECH_CONCURRENCY) break;
    if (item.state !== "idle") continue;
    loading += 1;
    synthesizeChunk(item);
  }
}

async function synthesizeChunk(item) {
  const gen = speech.gen;
  if (speech.browser) {
    item.state = "failed";
    playSpeechQueue();
    return;
  }
  item.state = "loading";
  const controller = new AbortController();
  item.controller = controller;

  try {
    const response = await api("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: item.text }),
      signal: controller.signal,
    });
    if (gen !== speech.gen) return;
    if (response.status === 503) {
      // The model is not installed. Say the rest in the device's own voice.
      speech.browser = true;
      item.state = "failed";
    } else if (!response.ok) {
      item.state = "failed";
    } else {
      item.blob = await response.blob();
      item.state = "ready";
    }
  } catch (error) {
    if (gen !== speech.gen || error.name === "AbortError") return;
    item.state = "failed";
    if (String(error.message) !== "unauthorised") speech.browser = true;
  }
  if (gen !== speech.gen) return;
  item.controller = null;
  pumpSpeech();
  playSpeechQueue();
}

/* One player, walking the queue in order. It stops when it reaches a clip that
 * is still synthesizing, and synthesizeChunk() starts it again on arrival. */
async function playSpeechQueue() {
  if (speech.playing || !speech.active) return;
  const gen = speech.gen;
  speech.playing = true;
  try {
    for (;;) {
      const item = speech.items[speech.cursor];
      if (!item) break;
      if (item.state === "idle" || item.state === "loading") break;
      speech.cursor += 1;
      if (item.state === "ready" && item.blob) await playBlob(item.blob);
      else if (speech.browser) await speakInBrowser(item.text);
      if (gen !== speech.gen) return;
    }
  } finally {
    if (gen === speech.gen) {
      speech.playing = false;
      if (speech.done && speech.cursor >= speech.items.length) finishSpeech();
    }
  }
}

function playBlob(blob) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    state.audio = audio;
    setOrbMode("speaking");

    const done = () => {
      URL.revokeObjectURL(url);
      if (state.audio === audio) state.audio = null;
      resolve();
    };
    audio.addEventListener("ended", done, { once: true });
    audio.addEventListener("error", done, { once: true });

    audio.play().catch(() => {
      // Autoplay was refused because no gesture has unlocked audio yet. Not
      // worth an error card; the text is already on screen.
      done();
    });
  });
}

function speakInBrowser(text) {
  return new Promise((resolve) => {
    if (!("speechSynthesis" in window)) {
      resolve();
      return;
    }
    try {
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en-GB";
      // Daniel is Safari's British male; anything en-GB beats the default.
      const voices = speechSynthesis.getVoices();
      const british = voices.find((v) => /en[-_]GB/i.test(v.lang) && /daniel|male|george/i.test(v.name))
        || voices.find((v) => /en[-_]GB/i.test(v.lang));
      if (british) utterance.voice = british;
      utterance.rate = 1.0;
      setOrbMode("speaking");
      utterance.onend = () => resolve();
      utterance.onerror = () => resolve();
      speechSynthesis.speak(utterance);
    } catch {
      resolve(); // nothing more to try
    }
  });
}

function stopSpeaking() {
  speech.gen += 1;
  speech.active = false;
  speech.playing = false;
  speech.done = false;
  speech.cursor = 0;
  speech.buffer = "";
  speech.consumed = 0;
  for (const item of speech.items) {
    if (item.controller) item.controller.abort();
  }
  speech.items = [];
  if (state.audio) {
    state.audio.pause();
    state.audio = null;
  }
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  setOrbMode("idle");
}

/* ── pairing ──────────────────────────────────────────────── */

async function openPairing() {
  ui.pairVeil.dataset.open = "true";
  ui.qrBox.innerHTML = "";
  ui.pairUrl.textContent = "…";
  try {
    const response = await api("/api/pair");
    if (!response.ok) throw new Error(String(response.status));
    const data = await response.json();
    ui.qrBox.innerHTML = data.qr_svg || "";
    ui.pairUrl.textContent = data.url;
    const local = !state.status || !state.status.tailscale_configured;
    ui.pairNote.textContent = local
      ? "This is a local address — your phone cannot reach it yet. Set ALFRED_TAILSCALE_HOSTNAME " +
        "and run `tailscale serve` so the QR points at an address the phone can open."
      : "Scan from the iPhone camera, then use Share → Add to Home Screen so notifications work.";
  } catch (error) {
    if (String(error.message) !== "unauthorised") ui.pairUrl.textContent = "Could not build a pairing code.";
  }
}

function showAuthVeil() {
  ui.authVeil.dataset.open = "true";
  setConnection("down", "Not recognised");
}

/* ── boot ─────────────────────────────────────────────────── */

function wire() {
  ui.input.addEventListener("input", autoGrow);
  ui.input.addEventListener("keydown", (event) => {
    // Enter sends on a keyboard; on touch the on-screen return key should
    // insert a newline instead, so only intercept when there is no shift.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send(ui.input.value);
    }
  });
  ui.send.addEventListener("click", () => send(ui.input.value));

  /* Press and hold to talk. pointer* rather than mouse/touch so one code path
   * covers the trackpad and the phone. */
  ui.orb.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    unlockAudio();
    if (state.audio || speech.active || state.recording) {
      // Tapping while Alfred is talking should shut him up, not start a
      // recording of him talking. `speech.active` covers the gap between two
      // clips, where no audio element exists but more is queued.
      stopSpeaking();
      return;
    }
    // Capture so a finger that slides off the button still ends the recording
    // on release instead of leaving the microphone open.
    try { ui.orb.setPointerCapture(event.pointerId); } catch { /* not supported */ }
    startRecording();
  });

  const endHold = (event) => {
    if (event) {
      try { ui.orb.releasePointerCapture(event.pointerId); } catch { /* fine */ }
    }
    stopRecording();
  };
  ui.orb.addEventListener("pointerup", endHold);
  ui.orb.addEventListener("pointercancel", endHold);
  // A pointerup that lands outside the button still has to stop the recorder.
  window.addEventListener("pointerup", () => { if (state.recording) stopRecording(); });

  // Holding the button is a gesture the browser would otherwise treat as a
  // text selection or a long-press menu on iOS.
  ui.orb.addEventListener("contextmenu", (event) => event.preventDefault());

  ui.railToggle.addEventListener("click", () => {
    const open = ui.rail.dataset.open === "true";
    ui.rail.dataset.open = String(!open);
    ui.railToggle.setAttribute("aria-expanded", String(!open));
  });

  ui.pushBtn.addEventListener("click", enablePush);
  ui.briefBtn.addEventListener("click", () => showBriefing(true));

  ui.pairBtn.addEventListener("click", openPairing);
  ui.pairClose.addEventListener("click", () => { ui.pairVeil.dataset.open = "false"; });
  ui.pairVeil.addEventListener("click", (event) => {
    if (event.target === ui.pairVeil) ui.pairVeil.dataset.open = "false";
  });

  ui.tokenSave.addEventListener("click", async () => {
    const value = ui.tokenInput.value.trim();
    if (!value) return;
    saveToken(value);
    ui.authVeil.dataset.open = "false";
    await refreshStatus();
  });
  ui.tokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") ui.tokenSave.click();
  });

  document.addEventListener("pointerdown", unlockAudio, { once: true });
}

async function boot() {
  adoptTokenFromFragment();
  state.token = loadToken();
  ui.emptyLine.textContent = greeting();
  wire();
  autoGrow();

  const check = await fetch("/api/pair/check", { headers: authHeaders() })
    .then((r) => r.json())
    .catch(() => ({ paired: false }));

  if (!check.paired) {
    showAuthVeil();
    return;
  }

  await refreshStatus();
  // Record this device so a later briefing has somewhere to arrive.
  api("/api/devices", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label: navigator.platform || "Device" }),
  }).catch(() => {});

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch((error) => {
      // Not fatal - chat and voice work without it - but push and install do
      // not, so record why rather than swallowing it.
      console.warn("Service worker registration failed:", error);
    });
  }

  // Opened from a briefing notification: show it rather than an empty console.
  if (new URLSearchParams(location.search).get("briefing")) {
    showBriefing(false);
  }
}

boot();
