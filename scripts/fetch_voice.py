"""Download Alfred's voice model.

Roughly 330 MB, fetched once into the data directory (not the repo). Run:

    .venv\\Scripts\\python.exe scripts\\fetch_voice.py

Kept out of startup deliberately: a server that silently downloads a third of a
gigabyte the first time it boots is a server that looks hung.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.voice import tts


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()

    if tts.models_present(settings):
        model, voices = tts.model_paths(settings)
        print(f"Voice model already installed:\n  {model}\n  {voices}")
        return 0

    print("Downloading Kokoro-82M (Apache-2.0, ~330 MB). One time only.\n")

    last = -1

    def progress(fraction: float) -> None:
        nonlocal last
        percent = int(fraction * 100)
        if percent != last and percent % 2 == 0:
            bar = "#" * (percent // 2)
            print(f"\r  [{bar:<50}] {percent:3d}%", end="", flush=True)
            last = percent

    try:
        tts.download_models(settings, on_progress=progress)
    except tts.TTSUnavailable as exc:
        print(f"\n\nFailed: {exc}")
        print("Alfred still works - he will fall back to the browser's own voice.")
        return 1

    print("\n\nDone. Restart Alfred and he will speak for himself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
