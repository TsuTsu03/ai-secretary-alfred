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
  sProvider: el("sProvider"), sTts: el("sTts"), sUser: el("sUser"),
  sTz: el("sTz"), sNet: el("sNet"), sRoots: el("sRoots"),
};

const state = {
  token: "",
  status: null,
  conversationId: null,
  busy: false,
  audioUnlocked: false,
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

  ui.sTts.textContent = data.tts_engine === "kokoro" ? data.tts_voice : "Browser";
  ui.sTts.className = "stat__v stat__v--off";

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

async function send(text) {
  const message = (text || "").trim();
  if (!message || state.busy) return;

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
      body: JSON.stringify({ message, conversation_id: state.conversationId }),
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
    setOrbMode("idle");
    ui.headTitle.textContent = "Standing by";
    autoGrow();
    scrollLog();
  }
}

/* ── voice (Phase 2 attaches the recorder here) ───────────── */

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

function voiceUnavailable() {
  const secure = window.isSecureContext;
  setOrbMode("denied");
  setTimeout(() => setOrbMode("idle"), 1400);
  addTurn(
    "system",
    secure
      ? "Voice arrives in the next phase, sir."
      : "Voice needs a secure connection. Reach Alfred over the https://…ts.net address that " +
        "`tailscale serve` provides — Safari refuses microphone access on plain HTTP."
  );
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

  ui.orb.addEventListener("pointerdown", () => {
    unlockAudio();
    voiceUnavailable();
  });

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
