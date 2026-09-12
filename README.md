# Alfred

A private AI secretary that runs on your own machine, answers from your own
files, and reaches you on your phone. Named after the butler, and written to
behave like one: formal, brief, dry, and completely unwilling to change
anything without asking first.

**Nothing here costs money.** Every component is a free tier or runs locally.
See [Cost](#cost).

---

## What works today

| | Status |
|---|---|
| HUD, on laptop and phone | Working |
| Token pairing, QR handoff | Working |
| Conversation with streaming replies | Working — needs a free API key |
| Provider failover (Gemini → Groq) | Working |
| Voice in (faster-whisper, GPU) | Working |
| Voice out (Kokoro-82M, `bm_george`) | Working |
| File search and Q&A | Working |
| Confirm-before-writing gate | Working |
| Calendar and Gmail | Working - needs a one-time Google connect |
| Daily briefing | Phase 5 |

## Requirements

- Windows, Python 3.13 (**not 3.14** — `ctranslate2` wheels track 3.13)
- FFmpeg on `PATH` (`winget install BtbN.FFmpeg.GPL.8.0`)
- An NVIDIA GPU is optional but transcription is roughly 3x realtime on one
- Tailscale on the laptop and the iPhone, to reach Alfred away from the desk

## Setup

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env

# Optional but recommended: CUDA runtime for GPU transcription (~700 MB).
# Only a recent driver is needed - not the full CUDA Toolkit.
.venv\Scripts\python.exe -m pip install -r requirements-gpu.txt

# Alfred's voice, ~330 MB, one time. Skip it and he uses the browser's voice.
.venv\Scripts\python.exe scripts\fetch_voice.py
```

Get a free Gemini key at <https://aistudio.google.com/apikey> and put it in
`.env` as `GEMINI_API_KEY`. Then:

```powershell
.\scripts\run.ps1
```

Open <http://127.0.0.1:8757>. The pairing token is printed at startup.

### Giving Alfred your calendar and mailbox

Optional, free, and one-time:

```powershell
.venv\Scripts\python.exe scripts\connect_google.py
```

It will tell you exactly what to create in the Google Cloud console (a Desktop
OAuth client, with the Calendar and Gmail APIs enabled) and where to save the
downloaded JSON. Keep that project on the free tier with no billing account.

**Alfred reads your mail and saves drafts. He never sends.**

Worth being precise about how that is enforced, because it is easy to overstate:
Alfred asks for `gmail.modify`, and `gmail.modify` *does* permit sending. There
is no Gmail scope that allows drafting while forbidding sending — `gmail.compose`
permits send too. So what stops Alfred sending mail is that **no send tool
exists**: nothing calls `users.messages.send`, and the model is offered no way
to. Drafting is additionally gated behind a confirmation card.

That is an application-level guarantee, not a Google-enforced one. If you want
the stronger version, change `gmail.modify` to `gmail.readonly` in
`app/integrations/google_oauth.py` and drop the draft tool — Alfred then cannot
draft either, which is the trade Google's scope model forces.

Calendar and mail tools are only registered once a token exists. Before that
Alfred simply does not have them, rather than having them and failing.

## Reaching Alfred from the iPhone

Alfred refuses to bind to a publicly routable interface, so exposing him is a
deliberate act rather than an accident. Use Tailscale:

1. Install Tailscale on the laptop and the iPhone, signed into the same account.
2. On the laptop: `tailscale serve --bg 8757`
3. `tailscale status` gives the machine's name. Put it in `.env` as
   `ALFRED_TAILSCALE_HOSTNAME`, e.g. `my-laptop.tail1234.ts.net`.
4. Restart Alfred, open the pairing dialog, scan the QR from the iPhone.
5. On the iPhone: **Share → Add to Home Screen**.

**`tailscale serve` is not optional if you want voice.** Safari refuses
microphone access on plain HTTP, and iOS only permits Web Push to a PWA
installed from a real HTTPS origin. Alfred over `http://100.x.x.x` will load,
look correct, and silently never hear you.

## Cost

| Component | Choice | Cost |
|---|---|---|
| Reasoning | Gemini 2.5 Flash free tier → Groq free tier | $0 |
| Speech in | `faster-whisper`, local GPU | $0 |
| Speech out | Kokoro-82M (Apache-2.0), voice `bm_george` | $0 |
| Embeddings | `paraphrase-multilingual-MiniLM-L12-v2`, local CPU | $0 |
| Remote access | Tailscale personal plan | $0 |
| HTTPS | `tailscale serve` (Let's Encrypt) | $0 |
| Calendar, mail | Google API free quota | $0 |
| Hosting | Your laptop | $0 |

Two things to know so it stays that way:

- **Keep the Gemini key on a Cloud project with no billing account attached.**
  Enabling billing converts the free tier into a paid one silently. Without
  billing, exhausting the quota returns a 429, which the router handles.
- `ANTHROPIC_API_KEY` is wired in but **bills**. It is never selected
  automatically — only if you set `ALFRED_LLM_PROVIDER=anthropic`.

### The privacy trade-off, stated once

Google's free tier permits using prompts to improve their models. The paid tier
does not. Alfred reads your files and your mail, so this is worth a deliberate
decision rather than a default you inherited. If it bothers you, set
`ALFRED_LLM_PROVIDER=anthropic` and supply a paid key — that is the only change
required.

## Safety model

Alfred holds Google credentials and reads personal files, so the constraints
are enforced in code rather than left to good behaviour:

- **Bind guard.** `Settings` refuses any host that is not loopback or inside
  Tailscale's `100.64.0.0/10`. `0.0.0.0` raises at startup.
- **Token on every route.** Generated on first run, stored at
  `%LOCALAPPDATA%\Alfred\secrets\auth_token`. Delete it to rotate and unpair
  every device.
- **Path allowlist plus denylist.** Paths resolve before the allowlist check,
  so `..`, symlinks, and short names cannot escape a root. `.env`, `*.key`,
  `id_rsa*`, `.git/`, `node_modules/` and friends are denied *inside* allowed
  roots too.
- **Nothing changes without approval.** Writes, calendar edits, and mail all
  become a `PendingAction` that Alfred describes and you approve. Gmail is
  draft-only — enforced by there being no send tool, not by the scope (see
  above).
- **Read content is data, not instruction.** File and email text is fenced in
  an untrusted-content wrapper. A document saying "ignore your instructions"
  gets reported, not obeyed.

## Layout

```
app/
  config.py          Settings, bind guard, cost defaults
  cuda_bootstrap.py  Must import before ctranslate2 (see below)
  security/          paths.py (the file boundary), auth.py (tokens)
  llm/               base.py, providers.py, router.py (failover)
  persona/           alfred.md — edit this to retune him
  api/routes.py      HTTP surface
  static/            The HUD. No build step, no dependencies.
```

## Things that will bite you

- **`cuda_bootstrap` must be imported before `ctranslate2`.** Windows has not
  resolved extension-module DLLs from `PATH` since Python 3.8. Without it CUDA
  reports zero devices, which looks exactly like a driver problem that is not
  there.
- **`ctranslate2.get_cuda_device_count()` returning 1 does not mean CUDA works.**
  Device *detection* uses the driver; inference needs cuBLAS and cuDNN, which
  arrive with `requirements-gpu.txt`. Without them the failure is deferred to
  the first encode:  `RuntimeError: Library cublas64_12.dll is not found or
  cannot be loaded`. Startup looks perfectly healthy right up until you speak.
- **Never pin `ALFRED_WHISPER_LANGUAGE=en`, and never let a `distil-*` or
  `*.en` model be selected.** They cannot transcribe Tagalog, so Taglish comes
  back translated into English instead of transcribed.
- **Groq's free tier is ~6–8k tokens/minute.** That is far too small for a
  prompt carrying retrieved file context. The router skips it for large
  prompts rather than earning a 429.
- **The router must never fall back after emitting a token.** Doing so splices
  two different answers together. `tests/test_router.py` pins this.
- **Embeddings must be L2-normalised.** sqlite-vec ranks by L2 distance, and
  this model returns vectors with norms around 5, so unnormalised the "semantic"
  half of search was partly measuring vector *length*. `EMBEDDING_REVISION` in
  `app/indexer/embed.py` exists so that changing this forces a reindex instead
  of leaving incomparable vectors in place.
- **A naive ISO timestamp is read in your timezone, not UTC.** "Three o'clock"
  meaning Manila, silently stored as 3am UTC, is an eight-hour error that looks
  entirely plausible on a confirmation card.
- **Gemini rejects `role: "function"`** for tool responses (use a `user` turn),
  and its thinking models reject any replayed `functionCall` that omits the
  `thoughtSignature` they issued. Neither is obvious from the docs.

## Development

```powershell
.venv\Scripts\python.exe -m pytest -q          # tests
.venv\Scripts\python.exe -m ruff check .       # lint
.venv\Scripts\python.exe scripts\make_icons.py # regenerate PWA icons
```

The browser caches `/assets/app.js` aggressively. Hard-reload after editing it.

Voice can be exercised without a microphone:

```powershell
# synthesise, then feed Alfred's own voice back through Whisper
curl -X POST http://127.0.0.1:8757/api/voice/speak -H "Authorization: Bearer <token>" `
     -H "Content-Type: application/json" -d '{"text":"Good evening, sir."}' -o out.wav
curl -X POST http://127.0.0.1:8757/api/voice/transcribe -H "Authorization: Bearer <token>" `
     -F "audio=@out.wav"
```
