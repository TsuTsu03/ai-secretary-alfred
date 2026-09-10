"""Make pip-installed CUDA libraries visible to CTranslate2 on Windows.

Since Python 3.8, Windows extension modules no longer resolve their DLL
dependencies from ``PATH``; the directory must be registered with
``os.add_dll_directory``. The ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12``
wheels drop their DLLs inside site-packages, so without this step CTranslate2
imports fine and then reports zero CUDA devices - which looks exactly like
"no GPU" and sends you hunting for a driver problem that does not exist.

Import this module *before* ``ctranslate2`` or ``faster_whisper``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_applied = False
_registered_dirs: list[str] = []


def _candidate_dirs() -> list[Path]:
    """Directories inside installed nvidia-* wheels that contain DLLs."""
    dirs: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        nvidia_root = Path(entry) / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for package in nvidia_root.iterdir():
            if not package.is_dir():
                continue
            # Windows wheels use bin/, Linux wheels use lib/.
            for sub in ("bin", "lib"):
                candidate = package / sub
                if candidate.is_dir():
                    dirs.append(candidate)
    return dirs


def apply() -> list[str]:
    """Register CUDA DLL directories. Idempotent; safe on non-Windows."""
    global _applied
    if _applied:
        return _registered_dirs

    if os.name != "nt":
        _applied = True
        return _registered_dirs

    for directory in _candidate_dirs():
        has_dll = any(directory.glob("*.dll"))
        if not has_dll:
            continue
        try:
            os.add_dll_directory(str(directory))
            _registered_dirs.append(str(directory))
        except (OSError, AttributeError) as exc:
            logger.debug("Could not register DLL directory %s: %s", directory, exc)

    # Some CTranslate2 builds still consult PATH for transitive loads.
    if _registered_dirs:
        os.environ["PATH"] = os.pathsep.join(_registered_dirs) + os.pathsep + os.environ.get("PATH", "")
        logger.info("Registered %d CUDA DLL director(ies) for CTranslate2.", len(_registered_dirs))
    else:
        logger.debug("No pip-installed CUDA libraries found; CPU or system CUDA will be used.")

    _applied = True
    return _registered_dirs


def registered_directories() -> list[str]:
    return list(_registered_dirs)


# Applied on import so that a plain `import app.cuda_bootstrap` is enough.
apply()
