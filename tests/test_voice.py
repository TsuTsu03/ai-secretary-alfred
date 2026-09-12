"""Tests for the voice layer.

None of these load a model - that costs seconds and gigabytes. They pin the
decisions around the model instead, which is where the bugs that reach a user
actually live: the wrong language, a hallucinated transcript of silence, audio
Safari refuses to play.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from app.config import Settings
from app.hardware import ENGLISH_ONLY_MODELS, select_whisper_config
from app.voice import stt, tts


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(ALFRED_DATA_DIR=str(tmp_path / "data"))  # type: ignore[call-arg]


# ── Taglish: the failure that looks like success ─────────────────────────


def test_language_is_not_pinned_by_default(settings: Settings) -> None:
    """Pinning `en` makes Whisper translate rather than transcribe.

    The output is fluent, confident English that Jansen never said. Nothing
    looks broken, which is what makes it worth a test.
    """
    assert settings.whisper_language == ""


@pytest.mark.parametrize("model", sorted(ENGLISH_ONLY_MODELS))
def test_english_only_models_are_known(model: str) -> None:
    assert model.endswith(".en") or model.startswith("distil-")


def test_selection_never_picks_an_english_only_model(settings: Settings) -> None:
    """Every rung of every ladder has to survive Tagalog."""
    for mode in ("fast", "balanced", "accurate"):
        config = select_whisper_config(mode)  # type: ignore[arg-type]
        assert config.model_size not in ENGLISH_ONLY_MODELS


def test_an_english_only_override_is_refused_at_load(tmp_path: Path) -> None:
    """A forced English-only model must fail loudly, not translate silently."""
    forced = Settings(
        ALFRED_DATA_DIR=str(tmp_path / "d"),
        ALFRED_WHISPER_MODEL="medium.en",
    )  # type: ignore[call-arg]
    engine = stt._Engine()
    with pytest.raises(stt.TranscriptionError, match="English-only"):
        engine.load(forced)


# ── hallucinated silence ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["Thank you.", "  thanks for watching!  ", "Please subscribe.", "...", "you",
     "Subtitles by the Amara.org community", '"Thank you."'],
)
def test_silence_hallucinations_are_recognised(text: str) -> None:
    """Whisper answers an empty clip with subtitle credits it learned in
    training. Passing those to Alfred has him answer a question nobody asked."""
    assert stt.is_hallucinated_silence(text)


@pytest.mark.parametrize(
    "text",
    ["Thank you for moving the meeting, Alfred.", "Subscribe me to the newsletter",
     "What is on my calendar?", "Buksan mo yung file na thank you.txt"],
)
def test_real_speech_is_not_mistaken_for_silence(text: str) -> None:
    assert not stt.is_hallucinated_silence(text)


# ── audio format ─────────────────────────────────────────────────────────


def test_wav_is_16_bit_pcm_because_safari_will_not_play_float() -> None:
    import numpy as np

    samples = np.sin(np.linspace(0, 40, 4800)).astype("float32") * 0.5
    data = tts._to_wav(samples, tts.SAMPLE_RATE)

    with wave.open(io.BytesIO(data)) as handle:
        assert handle.getsampwidth() == 2, "float WAV does not play on Safari"
        assert handle.getnchannels() == 1
        assert handle.getframerate() == tts.SAMPLE_RATE
        assert handle.getnframes() == len(samples)


def test_wav_encoding_normalises_clipping_input() -> None:
    """Samples above 1.0 would wrap around into noise once quantised."""
    import numpy as np

    loud = (np.ones(2400) * 3.5).astype("float32")
    data = tts._to_wav(loud, tts.SAMPLE_RATE)
    with wave.open(io.BytesIO(data)) as handle:
        frames = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    assert frames.max() > 32000, "normalised to full scale"
    assert frames.min() >= 0, "a constant positive signal must not wrap negative"


# ── model files ──────────────────────────────────────────────────────────


def test_models_absent_on_a_fresh_install(settings: Settings) -> None:
    assert not tts.models_present(settings)


def test_a_truncated_download_does_not_count_as_installed(settings: Settings) -> None:
    """A `.part` rename that half-succeeded must be re-fetched, not loaded."""
    model, voices = tts.model_paths(settings)
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"x" * 1024)
    voices.write_bytes(b"x" * 1024)
    assert not tts.models_present(settings)


def test_synthesis_without_the_model_names_the_fix(settings: Settings) -> None:
    engine = tts._Engine()
    with pytest.raises(tts.TTSUnavailable) as info:
        engine.load(settings)
    message = str(info.value)
    assert "fetch_voice.py" in message
    assert "ALFRED_TTS_ENGINE=browser" in message


def test_describe_reports_not_ready_without_the_model(settings: Settings) -> None:
    status = tts.describe(settings)
    assert status["engine"] == "kokoro"
    assert status["ready"] is False


def test_browser_engine_is_always_ready(tmp_path: Path) -> None:
    """The fallback must never report itself as needing a download."""
    browser = Settings(
        ALFRED_DATA_DIR=str(tmp_path / "d"), ALFRED_TTS_ENGINE="browser"
    )  # type: ignore[call-arg]
    assert tts.describe(browser)["ready"] is True


def test_default_voice_is_british() -> None:
    """An American Alfred is a different character."""
    assert Settings(ALFRED_DATA_DIR="x").tts_voice in tts.BRITISH_VOICES  # type: ignore[call-arg]


def test_empty_text_is_refused(settings: Settings) -> None:
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("   ", settings)


def test_warming_without_the_model_is_a_no_op(settings: Settings) -> None:
    """Startup must not fail, or block, when the voice was never downloaded."""
    assert tts.warm(settings) is False


def test_warming_is_skipped_for_the_browser_engine(tmp_path: Path) -> None:
    browser = Settings(
        ALFRED_DATA_DIR=str(tmp_path / "d"), ALFRED_TTS_ENGINE="browser"
    )  # type: ignore[call-arg]
    assert tts.warm(browser) is False


def test_background_warming_starts_no_thread_without_the_model(settings: Settings) -> None:
    import threading

    before = threading.active_count()
    tts.warm_in_background(settings)
    assert threading.active_count() == before


def test_thread_count_is_capped_below_the_core_count(settings: Settings) -> None:
    """ONNX Runtime's one-thread-per-core default is slower on a hybrid CPU."""
    import os

    threads = tts.synthesis_threads(settings)
    assert 2 <= threads <= 4
    assert threads <= max(2, (os.cpu_count() or 4))


def test_an_explicit_thread_count_wins(tmp_path: Path) -> None:
    pinned = Settings(
        ALFRED_DATA_DIR=str(tmp_path / "d"), ALFRED_TTS_THREADS=7
    )  # type: ignore[call-arg]
    assert tts.synthesis_threads(pinned) == 7
