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
    ui.sMail.title = "Alfred can read and draft. He cannot send.";
  }
  ui.sGoogle.className = "stat__v" + (google.connected && hasCalendar ? " stat__v--amber" : " stat__v--off");
  ui.sMail.className = "stat__v" + (google.connected && hasMail ? " stat__v--amber" : " stat__v--off");

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
    if (byVoice && received.trim()) {
      // speak() drives the orb through "speaking" and back to idle itself.
      speak(received);
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

/* Speak a reply. Kokoro if the server has it, otherwise the browser's own
 * voice — Alfred sounding like a satnav beats Alfred saying nothing. */
async function speak(text) {
  const spoken = text.trim();
  if (!spoken) return;

  try {
    const response = await api("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: spoken }),
    });
    if (response.status === 503) {
      speakInBrowser(spoken);
      return;
    }
    if (!response.ok) return;

    const blob = await response.blob();
    await playBlob(blob);
  } catch (error) {
    if (String(error.message) !== "unauthorised") speakInBrowser(spoken);
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
      state.audio = null;
      setOrbMode("idle");
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
  if (!("speechSynthesis" in window)) return;
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
    utterance.onend = () => setOrbMode("idle");
    utterance.onerror = () => setOrbMode("idle");
    speechSynthesis.cancel();
    speechSynthesis.speak(utterance);
  } catch { /* nothing more to try */ }
}

function stopSpeaking() {
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
    if (state.audio || state.recording) {
      // Tapping while Alfred is talking should shut him up, not start a
      // recording of him talking.
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
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}

boot();
