"""FFmpeg wrappers: probing, normalization, and track mixing.

Every subprocess call here passes a fixed argument list (never a shell string)
so a filename can never become a command. Uploads are decoded, never executed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Whisper is trained on 16 kHz mono audio; anything else gets resampled inside
# the model anyway, so we do it once up front and reuse the result.
TARGET_SAMPLE_RATE = 16_000
TARGET_CHANNELS = 1

ProgressCallback = Callable[[float], None]


class FFmpegError(RuntimeError):
    """FFmpeg failed. Message contains the tail of stderr, which is safe."""


class FFmpegNotFound(FFmpegError):
    pass


@dataclass(slots=True)
class MediaInfo:
    duration_seconds: float
    has_audio: bool
    has_video: bool
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    format_name: str
    size_bytes: int


def _extra_search_dirs() -> list[Path]:
    """Places FFmpeg commonly lands on Windows when PATH has not refreshed."""
    if os.name != "nt":
        return []
    local = os.environ.get("LOCALAPPDATA", "")
    dirs: list[Path] = []
    if local:
        dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            try:
                for entry in packages.iterdir():
                    if "FFmpeg" in entry.name:
                        dirs.extend(p.parent for p in entry.rglob("bin/ffmpeg.exe"))
            except OSError:
                pass
    dirs.extend(
        [
            Path(r"C:\ffmpeg\bin"),
            Path(r"C:\Program Files\ffmpeg\bin"),
        ]
    )
    return dirs


def _find_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    exe = f"{name}.exe" if os.name == "nt" else name
    for directory in _extra_search_dirs():
        candidate = directory / exe
        if candidate.is_file():
            return str(candidate)
    raise FFmpegNotFound(
        f"{name} was not found. Install it with:  winget install BtbN.FFmpeg.GPL.8.0  "
        "then restart the application so PATH is picked up."
    )


def ffmpeg_path() -> str:
    return _find_tool("ffmpeg")


def ffprobe_path() -> str:
    return _find_tool("ffprobe")


def ffmpeg_available() -> bool:
    try:
        _find_tool("ffmpeg")
        _find_tool("ffprobe")
        return True
    except FFmpegNotFound:
        return False


def probe_media(path: Path) -> MediaInfo:
    """Authoritative media check. If ffprobe cannot parse it, we reject it."""
    command = [
        ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("ffprobe timed out reading this file.") from exc
    except OSError as exc:
        raise FFmpegError(f"Could not run ffprobe: {exc}") from exc

    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise FFmpegError(
            "This file is not readable as audio or video. " + " ".join(tail)
        )

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe returned malformed output.") from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)

    duration = 0.0
    for source in (fmt.get("duration"), (audio or {}).get("duration")):
        if source is None:
            continue
        try:
            duration = float(source)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break

    return MediaInfo(
        duration_seconds=duration,
        has_audio=audio is not None,
        has_video=video is not None,
        audio_codec=(audio or {}).get("codec_name"),
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        format_name=str(fmt.get("format_name", "unknown")),
        size_bytes=int(fmt.get("size") or (path.stat().st_size if path.exists() else 0)),
    )


def _run_with_progress(
    command: list[str],
    total_duration: float,
    on_progress: ProgressCallback | None,
    cancel_check: Callable[[], bool] | None,
    timeout: float,
) -> None:
    """Run FFmpeg, translating ``-progress`` output into a 0..1 fraction."""
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=creation_flags,
    )

    stderr_tail: list[str] = []

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-40]  # keep only the tail

    drainer = threading.Thread(target=drain_stderr, daemon=True)
    drainer.start()

    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if line.startswith("out_time_ms=") and total_duration > 0 and on_progress:
                try:
                    microseconds = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                fraction = min(1.0, max(0.0, (microseconds / 1_000_000) / total_duration))
                on_progress(fraction)
            if cancel_check and cancel_check():
                process.terminate()
                raise FFmpegError("Cancelled.")
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise FFmpegError("FFmpeg timed out.") from exc
    finally:
        drainer.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()

    if process.returncode != 0:
        raise FFmpegError("FFmpeg failed: " + " | ".join(stderr_tail[-5:]))


def normalize_for_transcription(
    source: Path,
    destination: Path,
    total_duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    apply_loudnorm: bool = True,
) -> Path:
    """Decode any input to 16 kHz mono 16-bit PCM WAV for Whisper.

    ``loudnorm`` evens out the very common case where the microphone track is
    much quieter than the meeting audio, which otherwise makes Whisper drop
    quiet speech entirely. It is single-pass (not the two-pass measured mode)
    because the accuracy gain of two-pass does not justify doubling the decode
    time for speech.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    filters = ["aresample=resampler=soxr"]
    if apply_loudnorm:
        filters.insert(0, "loudnorm=I=-18:TP=-2:LRA=11")

    command = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i", str(source),
        "-vn",                       # never decode video; we only want audio
        "-map", "0:a:0",             # first audio stream only
        "-af", ",".join(filters),
        "-ar", str(TARGET_SAMPLE_RATE),
        "-ac", str(TARGET_CHANNELS),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        "-progress", "pipe:1",
        "-loglevel", "error",
        str(destination),
    ]
    _run_with_progress(command, total_duration, on_progress, cancel_check, timeout=7200)

    if not destination.exists() or destination.stat().st_size < 1024:
        raise FFmpegError("Normalization produced an empty file - the source has no usable audio.")
    return destination


def mix_tracks(
    microphone: Path,
    system_audio: Path,
    destination: Path,
    mic_gain: float = 1.0,
    system_gain: float = 1.0,
) -> Path:
    """Combine the mic and system-audio tracks into one transcription track.

    ``amix`` with ``normalize=0`` keeps each source at its own level; the
    default would halve both, which buries whichever side is quieter. The
    duration is the longer of the two so a late-starting mic is not truncated.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path(),
        "-hide_banner", "-nostdin", "-y",
        "-i", str(microphone),
        "-i", str(system_audio),
        "-filter_complex",
        (
            f"[0:a]volume={mic_gain}[a0];"
            f"[1:a]volume={system_gain}[a1];"
            "[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[mixed];"
            "[mixed]loudnorm=I=-18:TP=-2:LRA=11,aresample=resampler=soxr[out]"
        ),
        "-map", "[out]",
        "-ar", str(TARGET_SAMPLE_RATE),
        "-ac", str(TARGET_CHANNELS),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        "-loglevel", "error",
        str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("Mixing timed out.") from exc
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise FFmpegError("Could not mix the microphone and system tracks. " + " ".join(tail))
    return destination


def repair_wav(path: Path) -> bool:
    """Rewrite a WAV whose RIFF header was never finalized.

    A crash mid-recording leaves the length fields at their placeholder values.
    FFmpeg can still read the PCM payload, so we remux it into a valid file and
    keep the audio instead of throwing the meeting away.
    """
    if not path.exists() or path.stat().st_size < 1024:
        return False
    repaired = path.with_suffix(".repaired.wav")
    command = [
        ffmpeg_path(), "-hide_banner", "-nostdin", "-y",
        "-err_detect", "ignore_err",
        "-i", str(path),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        "-loglevel", "error",
        str(repaired),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("WAV repair failed for %s: %s", path.name, exc)
        return False

    if result.returncode != 0 or not repaired.exists() or repaired.stat().st_size < 1024:
        repaired.unlink(missing_ok=True)
        return False

    repaired.replace(path)
    logger.info("Repaired truncated WAV: %s", path.name)
    return True
