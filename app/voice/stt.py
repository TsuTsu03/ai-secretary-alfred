"""Speech to text with faster-whisper, on the GPU when there is one.

Two things here are load-bearing and easy to undo by accident:

1. ``app.cuda_bootstrap`` is imported **before** ``faster_whisper``. Windows has
   not resolved extension-module DLLs from ``PATH`` since Python 3.8, so without
   it CTranslate2 imports cleanly and then reports zero CUDA devices - which
   looks exactly like a driver problem that is not there.

2. The language is left to auto-detect and English-only checkpoints are refused.
   Jansen speaks Taglish. Pinning ``en`` makes Whisper *translate* rather than
   transcribe, and a ``distil-*`` or ``*.en`` model cannot handle Tagalog at all.
   Both failures produce fluent, confident, wrong English, which is worse than
   an error because nothing looks broken.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app import cuda_bootstrap  # noqa: F401  must precede faster_whisper
from app.config import Settings, get_settings
from app.hardware import ENGLISH_ONLY_MODELS, WhisperConfig, probe_hardware, select_whisper_config

logger = logging.getLogger(__name__)

# Whisper hallucinates set phrases on silence or noise - subtitle-credit lines
# it learned from its training data. A near-empty clip that returns one of
# these is silence, and passing it to Alfred would have him answer a question
# nobody asked.
_HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.",
    "please subscribe.", "subscribe to my channel.", "you", ".", "..", "...",
    "bye.", "okay.", "so", "amara.org", "subtitles by the amara.org community",
}


class TranscriptionError(RuntimeError):
    """Transcription failed. The message is safe to show the user."""


@dataclass(slots=True)
class Transcript:
    text: str
    language: str
    language_probability: float
    duration_seconds: float
    elapsed_seconds: float
    model: str
    device: str

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class _Engine:
    """Holds the loaded model. Loading costs seconds and gigabytes, so it
    happens once and is shared across requests.

    The lock matters: two concurrent voice notes would otherwise each start
    loading a multi-gigabyte model, and on a laptop GPU the second one fails
    with an out-of-memory error rather than waiting politely.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._config: WhisperConfig | None = None

    def config(self, settings: Settings) -> WhisperConfig:
        if self._config is None:
            self._config = select_whisper_config(
                mode=settings.transcription_mode,
                profile=probe_hardware(),
                model_override=settings.whisper_model,
                device_override=settings.whisper_device,
                compute_override=settings.whisper_compute_type,
            )
        return self._config

    def load(self, settings: Settings):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:  # another thread won the race
                return self._model

            config = self.config(settings)
            if config.model_size in ENGLISH_ONLY_MODELS:
                raise TranscriptionError(
                    f"Whisper model {config.model_size!r} is English-only and cannot "
                    f"transcribe Tagalog. Choose a multilingual model."
                )

            from faster_whisper import WhisperModel

            logger.info(
                "Loading Whisper %s on %s (%s)...",
                config.model_size, config.device, config.compute_type,
            )
            started = time.monotonic()
            try:
                self._model = WhisperModel(
                    config.model_size,
                    device=config.device,
                    compute_type=config.compute_type,
                    cpu_threads=config.cpu_threads,
                    download_root=str(settings.models_dir),
                )
            except Exception as exc:
                raise TranscriptionError(
                    f"Could not load the Whisper model: {exc}"
                ) from exc
            logger.info("Whisper ready in %.1fs.", time.monotonic() - started)
            return self._model

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._config = None


_engine = _Engine()


def is_hallucinated_silence(text: str) -> bool:
    """True if this looks like Whisper's response to an empty clip."""
    return text.strip().lower().strip('"“”') in _HALLUCINATIONS


def transcribe(audio_path: Path, settings: Settings | None = None) -> Transcript:
    """Transcribe a 16 kHz mono WAV. Blocking; call it off the event loop."""
    settings = settings or get_settings()
    model = _engine.load(settings)
    config = _engine.config(settings)

    started = time.monotonic()
    try:
        segments, info = model.transcribe(
            str(audio_path),
            beam_size=config.beam_size,
            # Blank means auto-detect, which is the only correct setting for
            # Taglish. Whisper switches languages mid-utterance when it is
            # allowed to, and pinning one forces a translation instead.
            language=settings.whisper_language or None,
            task="transcribe",
            # Voice-activity filtering removes the long silences at the start
            # and end of a push-to-talk clip, which is where the hallucinated
            # subtitle credits come from.
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        # `segments` is a generator; the work happens as it is consumed.
        text = "".join(segment.text for segment in segments).strip()
    except Exception as exc:
        raise TranscriptionError(f"Transcription failed: {exc}") from exc

    elapsed = time.monotonic() - started

    if is_hallucinated_silence(text):
        logger.info("Discarded a hallucinated transcript of silence: %r", text)
        text = ""

    transcript = Transcript(
        text=text,
        language=getattr(info, "language", "") or "",
        language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
        duration_seconds=float(getattr(info, "duration", 0.0) or 0.0),
        elapsed_seconds=elapsed,
        model=config.model_size,
        device=config.device,
    )
    logger.info(
        "Transcribed %.1fs of %s audio in %.1fs (%s on %s).",
        transcript.duration_seconds, transcript.language or "unknown",
        elapsed, config.model_size, config.device,
        # The text itself is private; it is logged only when explicitly enabled.
        extra={"private_content": False},
    )
    logger.debug("Transcript: %s", text, extra={"private_content": True})
    return transcript


def warm_up(settings: Settings | None = None) -> None:
    """Load the model ahead of the first request.

    Without this the first voice note pays several seconds of model load on top
    of transcription, which reads as Alfred being slow rather than busy.
    """
    settings = settings or get_settings()
    try:
        _engine.load(settings)
    except TranscriptionError as exc:
        logger.warning("Could not warm up Whisper: %s", exc)


def describe(settings: Settings | None = None) -> dict:
    """What the status rail shows for hearing."""
    settings = settings or get_settings()
    try:
        config = _engine.config(settings)
    except Exception as exc:  # selection can fail on an odd machine
        return {"ready": False, "detail": str(exc)}
    return {
        "ready": True,
        "model": config.model_size,
        "device": config.device,
        "compute_type": config.compute_type,
        "loaded": _engine._model is not None,
    }
