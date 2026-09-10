"""Hardware probing and hardware-aware Whisper configuration.

The point of this module is to pick a model that will actually run well on the
machine in front of us instead of defaulting to the largest one and thrashing.
Everything here is measured at runtime; nothing is hardcoded to one PC.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from functools import lru_cache

from app.config import TranscriptionMode

logger = logging.getLogger(__name__)

# Approximate VRAM/RAM footprint of each CTranslate2 Whisper model in float16,
# including room for activations during batched inference. Used for fitting,
# not for accounting - real usage is measured by the benchmark harness.
MODEL_FOOTPRINT_MB: dict[str, int] = {
    "tiny": 400,
    "base": 600,
    "small": 1200,
    "medium": 2600,
    "large-v3-turbo": 2600,
    "large-v3": 4600,
}

# Whisper checkpoints that only understand English. Excluded from selection
# because this app must handle Tagalog and Taglish.
ENGLISH_ONLY_MODELS = frozenset(
    {"distil-large-v2", "distil-large-v3", "distil-medium.en", "tiny.en", "base.en", "small.en", "medium.en"}
)


@dataclass(slots=True)
class GpuInfo:
    name: str
    total_vram_mb: int
    free_vram_mb: int
    driver_version: str


@dataclass(slots=True)
class HardwareProfile:
    os_name: str
    os_version: str
    cpu_name: str
    physical_cores: int
    logical_cores: int
    total_ram_mb: int
    available_ram_mb: int
    gpus: list[GpuInfo] = field(default_factory=list)
    cuda_available: bool = False
    cuda_device_count: int = 0
    cuda_error: str = ""
    ffmpeg_path: str = ""
    ffmpeg_version: str = ""
    python_version: str = platform.python_version()

    @property
    def primary_gpu(self) -> GpuInfo | None:
        return self.gpus[0] if self.gpus else None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["primary_gpu"] = asdict(self.primary_gpu) if self.primary_gpu else None
        return data


@dataclass(slots=True)
class WhisperConfig:
    """Concrete settings handed to :class:`faster_whisper.WhisperModel`."""

    model_size: str
    device: str
    compute_type: str
    beam_size: int
    batch_size: int
    cpu_threads: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_mb() -> tuple[int, int]:
    """Return ``(total_mb, available_mb)`` without pulling in psutil."""
    if os.name == "nt":
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return (
                int(status.ullTotalPhys // (1024 * 1024)),
                int(status.ullAvailPhys // (1024 * 1024)),
            )
    # POSIX. os.sysconf does not exist on Windows, so it is looked up
    # dynamically to keep type checkers from flagging it on this platform.
    sysconf = getattr(os, "sysconf", None)
    if sysconf is None:
        return 0, 0
    try:
        page_size = sysconf("SC_PAGE_SIZE")
        total = sysconf("SC_PHYS_PAGES") * page_size // (1024 * 1024)
        available = sysconf("SC_AVPHYS_PAGES") * page_size // (1024 * 1024)
        return int(total), int(available)
    except (ValueError, OSError, AttributeError):
        return 0, 0


def _cpu_name() -> str:
    if os.name == "nt":
        name = os.environ.get("PROCESSOR_IDENTIFIER", "")
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            return name
    return platform.processor() or platform.machine()


def _physical_cores(logical: int) -> int:
    """Physical core count, falling back to a halved logical count."""
    try:
        # Python 3.13+ exposes this on Linux only; guard for portability.
        affinity = os.process_cpu_count()  # type: ignore[attr-defined]
        if affinity and os.name != "nt":
            return max(1, affinity // 2)
    except AttributeError:
        pass
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["wmic", "cpu", "get", "NumberOfCores"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout
            values = [int(v) for v in out.split() if v.isdigit()]
            if values:
                return sum(values)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return max(1, logical // 2)


def _probe_gpus() -> list[GpuInfo]:
    """Query NVIDIA GPUs via nvidia-smi. Absent driver simply means no GPU."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        result = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("nvidia-smi failed: %s", exc)
        return []

    gpus: list[GpuInfo] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            gpus.append(
                GpuInfo(
                    name=parts[0],
                    total_vram_mb=int(float(parts[1])),
                    free_vram_mb=int(float(parts[2])),
                    driver_version=parts[3],
                )
            )
        except ValueError:
            continue
    return gpus


def _probe_cuda() -> tuple[bool, int, str]:
    """Ask CTranslate2 itself whether it can see a usable CUDA device.

    nvidia-smi seeing a GPU is not the same as CTranslate2 being able to use
    it - cuBLAS/cuDNN may be missing. This is the check that actually matters.
    """
    try:
        import ctranslate2
    except ImportError as exc:
        return False, 0, f"ctranslate2 not importable: {exc}"
    try:
        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        return False, 0, str(exc)
    if count <= 0:
        return False, 0, "no CUDA devices reported by CTranslate2"
    return True, count, ""


def _probe_ffmpeg() -> tuple[str, str]:
    exe = shutil.which("ffmpeg")
    if not exe:
        return "", ""
    try:
        out = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=15, check=False
        ).stdout
        first = out.splitlines()[0] if out else ""
        return exe, first.strip()
    except (OSError, subprocess.SubprocessError):
        return exe, ""


@lru_cache(maxsize=1)
def probe_hardware() -> HardwareProfile:
    """Inspect the machine once per process. Cheap enough at startup."""
    logical = os.cpu_count() or 1
    total_ram, available_ram = _memory_mb()
    cuda_available, cuda_count, cuda_error = _probe_cuda()
    ffmpeg_path, ffmpeg_version = _probe_ffmpeg()

    profile = HardwareProfile(
        os_name=platform.system(),
        os_version=platform.version(),
        cpu_name=_cpu_name(),
        physical_cores=_physical_cores(logical),
        logical_cores=logical,
        total_ram_mb=total_ram,
        available_ram_mb=available_ram,
        gpus=_probe_gpus(),
        cuda_available=cuda_available,
        cuda_device_count=cuda_count,
        cuda_error=cuda_error,
        ffmpeg_path=ffmpeg_path,
        ffmpeg_version=ffmpeg_version,
    )
    logger.info(
        "Hardware: %s | %d cores (%d logical) | %d MB RAM | GPU=%s | CUDA=%s",
        profile.cpu_name,
        profile.physical_cores,
        profile.logical_cores,
        profile.total_ram_mb,
        profile.primary_gpu.name if profile.primary_gpu else "none",
        profile.cuda_available,
    )
    if profile.gpus and not profile.cuda_available:
        logger.warning(
            "NVIDIA GPU detected but CTranslate2 cannot use it (%s). "
            "Install requirements-gpu.txt for cuBLAS/cuDNN. Falling back to CPU.",
            profile.cuda_error or "unknown reason",
        )
    return profile


def refresh_hardware() -> HardwareProfile:
    probe_hardware.cache_clear()
    return probe_hardware()


# ---------------------------------------------------------------------------
# model selection
# ---------------------------------------------------------------------------

# Preference order per mode, best-first. Every entry is multilingual so that
# Tagalog and Taglish keep working regardless of which one gets chosen.
_GPU_LADDER: dict[TranscriptionMode, list[str]] = {
    "fast": ["small", "base", "tiny"],
    "balanced": ["large-v3-turbo", "medium", "small", "base"],
    "accurate": ["large-v3", "large-v3-turbo", "medium", "small"],
}
_CPU_LADDER: dict[TranscriptionMode, list[str]] = {
    "fast": ["tiny", "base"],
    "balanced": ["base", "small", "tiny"],
    "accurate": ["small", "medium", "base"],
}

_BEAM_SIZE: dict[TranscriptionMode, int] = {"fast": 1, "balanced": 3, "accurate": 5}

# CTranslate2 quantizations, best-quality first, per device.
_GPU_COMPUTE_LADDER = ["float16", "int8_float16", "float32"]
_CPU_COMPUTE_LADDER = ["int8", "float32"]

# Leave headroom so the desktop compositor and browser do not get starved.
_VRAM_HEADROOM_MB = 900
_RAM_HEADROOM_MB = 2048


def select_whisper_config(
    mode: TranscriptionMode,
    profile: HardwareProfile | None = None,
    model_override: str = "",
    device_override: str = "auto",
    compute_override: str = "",
) -> WhisperConfig:
    """Choose model size, device, and quantization for the given mode.

    Overrides always win so the settings screen can force a configuration, but
    an override that cannot possibly fit is logged loudly rather than silently
    "corrected" - the user asked for it and deserves the real failure.
    """
    profile = profile or probe_hardware()

    if device_override not in ("auto", "cuda", "cpu"):
        raise ValueError(f"device must be auto|cuda|cpu, got {device_override!r}")

    use_cuda = profile.cuda_available if device_override == "auto" else device_override == "cuda"
    if device_override == "cuda" and not profile.cuda_available:
        logger.warning(
            "CUDA was explicitly requested but is unavailable (%s). Using CPU.",
            profile.cuda_error or "no device",
        )
        use_cuda = False

    device = "cuda" if use_cuda else "cpu"
    reasons: list[str] = []

    if use_cuda:
        gpu = profile.primary_gpu
        # free_vram_mb is a snapshot; total is the stable planning number, but
        # never plan past what is free right now on a shared laptop GPU.
        budget = min(gpu.total_vram_mb, max(gpu.free_vram_mb, 1024)) if gpu else 4096
        budget -= _VRAM_HEADROOM_MB
        ladder = _GPU_LADDER[mode]
        compute_ladder = _GPU_COMPUTE_LADDER
        reasons.append(f"CUDA on {gpu.name if gpu else 'GPU'} with ~{budget} MB usable VRAM")
    else:
        budget = max(profile.available_ram_mb, 2048) - _RAM_HEADROOM_MB
        ladder = _CPU_LADDER[mode]
        compute_ladder = _CPU_COMPUTE_LADDER
        reasons.append(f"CPU with ~{budget} MB usable RAM, {profile.physical_cores} physical cores")

    if model_override:
        if model_override in ENGLISH_ONLY_MODELS:
            logger.warning(
                "Model %r is English-only and will not transcribe Tagalog or Taglish correctly.",
                model_override,
            )
        model = model_override
        reasons.append(f"model forced to {model_override} by configuration")
    else:
        model = ladder[-1]
        for candidate in ladder:
            need = MODEL_FOOTPRINT_MB.get(candidate, 1500)
            if device == "cpu":
                # int8 on CPU roughly halves the float16 footprint.
                need = int(need * 0.55)
            if need <= budget:
                model = candidate
                reasons.append(f"{candidate} fits in budget (needs ~{need} MB)")
                break
        else:
            reasons.append(f"nothing fit the budget, falling back to smallest option {model}")

    if compute_override:
        compute_type = compute_override
        reasons.append(f"compute type forced to {compute_override}")
    else:
        compute_type = compute_ladder[0]
        if device == "cuda":
            need = MODEL_FOOTPRINT_MB.get(model, 1500)
            if need > budget:
                # Quantize rather than drop to a weaker model - int8_float16
                # costs far less accuracy than stepping down a model size.
                compute_type = "int8_float16"
                reasons.append("quantized to int8_float16 to fit available VRAM")

    # Batch size drives BatchedInferencePipeline throughput. Larger batches use
    # proportionally more VRAM, so scale it with what is left after weights.
    if device == "cuda":
        spare = budget - MODEL_FOOTPRINT_MB.get(model, 1500)
        batch_size = 16 if spare > 2000 else 8 if spare > 800 else 4
    else:
        batch_size = min(8, max(1, profile.physical_cores // 2))

    cpu_threads = 0 if device == "cuda" else max(1, min(profile.physical_cores, 8))

    config = WhisperConfig(
        model_size=model,
        device=device,
        compute_type=compute_type,
        beam_size=_BEAM_SIZE[mode],
        batch_size=batch_size,
        cpu_threads=cpu_threads,
        reason="; ".join(reasons),
    )
    logger.info("Whisper config for mode=%s -> %s", mode, config.to_dict())
    return config
