"""Alfred's voice: Kokoro-82M, synthesized locally.

Kokoro is Apache-2.0 and runs on CPU at several times realtime, so voice output
costs nothing and no audio ever leaves the machine. ``bm_george`` is its British
male voice, which is as close to Alfred as a free model gets.

**CPU on purpose.** The GPU is busy holding Whisper. Putting both on the same
laptop GPU means the reply cannot start synthesizing until transcription has
released memory, which shows up as a pause exactly where a conversation can
least afford one.

If the model files are absent the API says so plainly and the browser falls back
to its own speech synthesis. Alfred then sounds like a satnav, but he still
talks, which is the right failure.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_URL = f"{_RELEASE}/{MODEL_FILE}"
VOICES_URL = f"{_RELEASE}/{VOICES_FILE}"

# Kokoro emits 24 kHz mono float32.
SAMPLE_RATE = 24_000

# Kokoro's British voices. Anything else in the pack is American or another
# language, and Alfred with an American accent is a different character.
BRITISH_VOICES = ("bm_george", "bm_lewis", "bm_daniel", "bf_emma", "bf_isabella")


class TTSUnavailable(RuntimeError):
    """Voice output cannot run. The message is safe to show the user."""


@dataclass(slots=True)
class Speech:
    audio_wav: bytes
    sample_rate: int
    voice: str
    elapsed_seconds: float
    characters: int


def model_paths(settings: Settings | None = None) -> tuple[Path, Path]:
    settings = settings or get_settings()
    root = settings.models_dir / "kokoro"
    return root / MODEL_FILE, root / VOICES_FILE


def models_present(settings: Settings | None = None) -> bool:
    model, voices = model_paths(settings)
    # A partial download leaves a small file behind; size-check rather than
    # exists-check so a half-finished fetch is retried instead of loaded.
    return (
        model.is_file() and model.stat().st_size > 50_000_000
        and voices.is_file() and voices.stat().st_size > 1_000_000
    )


def download_models(settings: Settings | None = None, on_progress=None) -> None:
    """Fetch the Kokoro weights (~330 MB total) into the data directory.

    Downloads to a ``.part`` file and renames on success, so an interrupted
    fetch cannot leave something that looks complete.
    """
    import httpx

    settings = settings or get_settings()
    model, voices = model_paths(settings)
    model.parent.mkdir(parents=True, exist_ok=True)

    for url, destination in ((MODEL_URL, model), (VOICES_URL, voices)):
        if destination.is_file() and destination.stat().st_size > 1_000_000:
            continue
        partial = destination.with_suffix(destination.suffix + ".part")
        logger.info("Downloading %s ...", destination.name)
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))
                done = 0
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        handle.write(chunk)
                        done += len(chunk)
                        if on_progress and total:
                            on_progress(done / total)
            partial.replace(destination)
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise TTSUnavailable(f"Could not download {destination.name}: {exc}") from exc
        logger.info("Downloaded %s (%.0f MB).", destination.name, destination.stat().st_size / 1e6)


class _Engine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kokoro = None
        self._voices: tuple[str, ...] = ()

    def load(self, settings: Settings):
        if self._kokoro is not None:
            return self._kokoro
        with self._lock:
            if self._kokoro is not None:
                return self._kokoro

            model, voices = model_paths(settings)
            if not models_present(settings):
                raise TTSUnavailable(
                    "Alfred's voice model is not installed. Run "
                    "`.venv\\Scripts\\python.exe scripts\\fetch_voice.py` to download it "
                    "(about 330 MB, one time), or set ALFRED_TTS_ENGINE=browser."
                )

            from kokoro_onnx import Kokoro

            logger.info("Loading Kokoro voice model...")
            started = time.monotonic()
            try:
                self._kokoro = Kokoro(str(model), str(voices))
                self._voices = tuple(sorted(self._kokoro.get_voices()))
            except Exception as exc:
                raise TTSUnavailable(f"Could not load the voice model: {exc}") from exc
            logger.info(
                "Kokoro ready in %.1fs, %d voices available.",
                time.monotonic() - started, len(self._voices),
            )
            return self._kokoro

    def voices(self, settings: Settings) -> tuple[str, ...]:
        self.load(settings)
        return self._voices

    @property
    def loaded(self) -> bool:
        return self._kokoro is not None


_engine = _Engine()


def _to_wav(samples, sample_rate: int) -> bytes:
    """Encode float32 samples as a WAV container.

    16-bit PCM rather than float: Safari will not play a float WAV, and this is
    a phone-first application.
    """
    import numpy as np
    import soundfile as sf

    audio = np.asarray(samples, dtype="float32")
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak

    buffer = io.BytesIO()
    sf.write(buffer, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def synthesize(text: str, settings: Settings | None = None, voice: str = "") -> Speech:
    """Speak `text` in Alfred's voice. Blocking; call it off the event loop."""
    settings = settings or get_settings()
    spoken = text.strip()
    if not spoken:
        raise TTSUnavailable("There is nothing to say.")

    kokoro = _engine.load(settings)
    chosen = voice or settings.tts_voice
    available = _engine.voices(settings)
    if available and chosen not in available:
        fallback = next((v for v in BRITISH_VOICES if v in available), available[0])
        logger.warning("Voice %r is not in the pack; using %r instead.", chosen, fallback)
        chosen = fallback

    started = time.monotonic()
    try:
        samples, sample_rate = kokoro.create(
            spoken,
            voice=chosen,
            speed=settings.tts_speed,
            # en-gb, not en-us: the voice is British and the phonemizer has to
            # agree with it, or the vowels come out mid-Atlantic.
            lang="en-gb",
        )
    except Exception as exc:
        raise TTSUnavailable(f"Speech synthesis failed: {exc}") from exc

    return Speech(
        audio_wav=_to_wav(samples, sample_rate),
        sample_rate=int(sample_rate),
        voice=chosen,
        elapsed_seconds=time.monotonic() - started,
        characters=len(spoken),
    )


def describe(settings: Settings | None = None) -> dict:
    """What the status rail shows for voice."""
    settings = settings or get_settings()
    if settings.tts_engine == "browser":
        return {"engine": "browser", "ready": True, "voice": "device", "installed": True}
    installed = models_present(settings)
    return {
        "engine": "kokoro",
        "ready": installed,
        "voice": settings.tts_voice,
        "installed": installed,
        "loaded": _engine.loaded,
        "detail": "" if installed else "Voice model not downloaded.",
    }
