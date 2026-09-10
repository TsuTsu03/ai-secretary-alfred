"""Application configuration.

All settings come from environment variables or the local ``.env`` file.
Secrets are held as :class:`~pydantic.SecretStr` so that an accidental repr,
log line, or API response cannot leak them.

Alfred's cost posture: every default in this file points at a free or local
provider. Paid providers exist as opt-in fields that are empty by default, so
running Alfred out of the box cannot generate a bill.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LLMProvider = Literal["gemini", "groq", "anthropic"]
TTSEngine = Literal["kokoro", "browser"]
TranscriptionMode = Literal["fast", "balanced", "accurate"]

# Tailscale hands out addresses from the CGNAT range. Binding inside this range
# reaches Jansen's own devices and nothing else on the public internet.
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _default_data_dir() -> Path:
    """Per-user application data directory.

    Uses ``%LOCALAPPDATA%`` on Windows and ``~/.local/share`` elsewhere so the
    app never writes conversations or indexes into the source tree.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "Alfred"


def _default_file_roots() -> str:
    """Folders Alfred may read, as an os.pathsep-joined string.

    Chosen to cover work and notes without reaching into system directories or
    the browser profile. Every one of these is still filtered by the denylist in
    ``app.security.paths``.
    """
    home = Path.home()
    candidates = [
        Path("C:/CodingProjects") if os.name == "nt" else home / "code",
        home / "Documents",
        home / "Downloads",
        home / "Downloads" / "JansenBrain" / "JansenBrain",
    ]
    return os.pathsep.join(str(path) for path in candidates)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- LLM providers ------------------------------------------------------
    # Both defaults are free tiers. Anthropic is present only so that swapping
    # to a paid key is a one-line change; it is never selected automatically.
    gemini_api_key: SecretStr = Field(default=SecretStr(""), alias="GEMINI_API_KEY")
    groq_api_key: SecretStr = Field(default=SecretStr(""), alias="GROQ_API_KEY")
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), alias="ANTHROPIC_API_KEY")

    llm_provider: LLMProvider = Field(default="gemini", alias="ALFRED_LLM_PROVIDER")
    llm_fallback: bool = Field(default=True, alias="ALFRED_LLM_FALLBACK")

    gemini_model: str = Field(default="gemini-flash-latest", alias="ALFRED_GEMINI_MODEL")
    groq_model: str = Field(default="moonshotai/kimi-k2-instruct", alias="ALFRED_GROQ_MODEL")
    anthropic_model: str = Field(default="claude-sonnet-5", alias="ALFRED_ANTHROPIC_MODEL")

    llm_timeout_seconds: float = Field(default=90.0, alias="ALFRED_LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=3, alias="ALFRED_LLM_MAX_RETRIES")
    llm_backoff_base_seconds: float = Field(default=1.5, alias="ALFRED_LLM_BACKOFF_BASE_SECONDS")
    # Groq's free tier allows only ~6-8k tokens/minute, far below Gemini's. The
    # router consults this before deciding whether a provider can take a request
    # carrying retrieved file context. 0 disables the check.
    groq_tpm_budget: int = Field(default=6000, alias="ALFRED_GROQ_TPM_BUDGET")
    gemini_tpm_budget: int = Field(default=250_000, alias="ALFRED_GEMINI_TPM_BUDGET")

    # ---- agent loop ---------------------------------------------------------
    max_tool_iterations: int = Field(default=8, alias="ALFRED_MAX_TOOL_ITERATIONS")
    history_turns: int = Field(default=20, alias="ALFRED_HISTORY_TURNS")

    # ---- speech to text -----------------------------------------------------
    transcription_mode: TranscriptionMode = Field(
        default="balanced", alias="ALFRED_TRANSCRIPTION_MODE"
    )
    whisper_model: str = Field(default="", alias="ALFRED_WHISPER_MODEL")
    whisper_device: str = Field(default="auto", alias="ALFRED_WHISPER_DEVICE")
    whisper_compute_type: str = Field(default="", alias="ALFRED_WHISPER_COMPUTE_TYPE")
    # Blank means auto-detect. Do not pin this to "en": Jansen speaks Taglish and
    # pinning English makes Whisper translate rather than transcribe.
    whisper_language: str = Field(default="", alias="ALFRED_WHISPER_LANGUAGE")

    # ---- text to speech -----------------------------------------------------
    # Kokoro is Apache-2.0 and runs locally, so voice output costs nothing and
    # no audio leaves the machine. "browser" falls back to the phone's own
    # speech synthesis, which is free but less convincing.
    tts_engine: TTSEngine = Field(default="kokoro", alias="ALFRED_TTS_ENGINE")
    # bm_george is Kokoro's British male voice - the closest free match to Alfred.
    tts_voice: str = Field(default="bm_george", alias="ALFRED_TTS_VOICE")
    tts_speed: float = Field(default=1.0, alias="ALFRED_TTS_SPEED")

    # ---- persona ------------------------------------------------------------
    user_address: str = Field(default="sir", alias="ALFRED_USER_ADDRESS")
    user_name: str = Field(default="Jansen", alias="ALFRED_USER_NAME")
    timezone: str = Field(default="Asia/Manila", alias="ALFRED_TIMEZONE")

    # ---- file access --------------------------------------------------------
    file_roots_raw: str = Field(default="", alias="ALFRED_FILE_ROOTS")
    max_file_read_bytes: int = Field(default=2_000_000, alias="ALFRED_MAX_FILE_READ_BYTES")
    # Multilingual on purpose: Jansen's notes are Taglish, and an English-only
    # embedding model quietly fails to match the Tagalog half of a sentence.
    # 384 dimensions at 0.22 GB runs comfortably on CPU, which matters because
    # the GPU is already holding Whisper.
    embedding_model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        alias="ALFRED_EMBEDDING_MODEL",
    )
    embedding_dimensions: int = Field(default=384, alias="ALFRED_EMBEDDING_DIMENSIONS")

    # ---- indexing -----------------------------------------------------------
    chunk_chars: int = Field(default=1200, alias="ALFRED_CHUNK_CHARS")
    chunk_overlap_chars: int = Field(default=180, alias="ALFRED_CHUNK_OVERLAP_CHARS")
    search_results: int = Field(default=6, alias="ALFRED_SEARCH_RESULTS")
    # Files bigger than this are indexed by their first N bytes rather than
    # skipped: a large log or dataset still has a useful head.
    index_max_chars: int = Field(default=400_000, alias="ALFRED_INDEX_MAX_CHARS")

    # ---- server -------------------------------------------------------------
    host: str = Field(default="127.0.0.1", alias="ALFRED_HOST")
    port: int = Field(default=8757, alias="ALFRED_PORT")
    # Hostname that `tailscale serve` terminates TLS for. Used to allow the Host
    # header and to build the pairing URL shown in the QR code.
    tailscale_hostname: str = Field(default="", alias="ALFRED_TAILSCALE_HOSTNAME")

    # ---- storage & logging --------------------------------------------------
    data_dir_override: str = Field(default="", alias="ALFRED_DATA_DIR")
    max_upload_mb: int = Field(default=64, alias="ALFRED_MAX_UPLOAD_MB")
    # Conversation text is personal. Off by default; turn on only to debug.
    log_conversation_content: bool = Field(default=False, alias="ALFRED_LOG_CONVERSATION_CONTENT")
    log_level: str = Field(default="INFO", alias="ALFRED_LOG_LEVEL")

    # ---- validators ---------------------------------------------------------
    @field_validator("host")
    @classmethod
    def _enforce_private_bind(cls, value: str) -> str:
        """Refuse to bind anywhere the public internet can reach.

        Alfred holds Google credentials and reads personal files, so a bind to
        ``0.0.0.0`` would be a serious mistake rather than a mild one. Loopback
        serves the laptop; a Tailscale CGNAT address serves the phone. Nothing
        else is legitimate, so nothing else is permitted.
        """
        candidate = value.strip().lower()
        if candidate in LOOPBACK_NAMES:
            return candidate
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as exc:
            raise ValueError(
                f"ALFRED_HOST must be a loopback address or a Tailscale 100.64.0.0/10 "
                f"address, got {value!r}."
            ) from exc
        if address in TAILSCALE_CGNAT:
            return candidate
        raise ValueError(
            f"ALFRED_HOST={value!r} is not loopback and not inside Tailscale's "
            f"100.64.0.0/10 range. Alfred must never bind to a publicly routable "
            f"interface; use `tailscale serve` to expose it instead."
        )

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    # ---- derived ------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        if self.data_dir_override:
            return Path(self.data_dir_override).expanduser()
        return _default_data_dir()

    @property
    def audio_dir(self) -> Path:
        """Uploaded voice clips and synthesized replies. Safe to delete."""
        return self.data_dir / "audio"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def secrets_dir(self) -> Path:
        """Auth token and Google refresh token. Never inside the repo."""
        return self.data_dir / "secrets"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "alfred.db"

    @property
    def auth_token_path(self) -> Path:
        return self.secrets_dir / "auth_token"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def file_roots(self) -> tuple[Path, ...]:
        """Resolved, existing folders Alfred is allowed to read."""
        raw = self.file_roots_raw or _default_file_roots()
        roots: list[Path] = []
        for entry in raw.split(os.pathsep):
            entry = entry.strip()
            if not entry:
                continue
            try:
                resolved = Path(entry).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
        return tuple(roots)

    @property
    def base_url(self) -> str:
        """URL the phone should open. Prefers the TLS name that Tailscale serves."""
        if self.tailscale_hostname:
            return f"https://{self.tailscale_hostname}"
        return f"http://{self.host}:{self.port}"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.audio_dir,
            self.models_dir,
            self.logs_dir,
            self.tmp_dir,
            self.secrets_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ---- secrets ------------------------------------------------------------
    def secret_values(self) -> list[str]:
        """Every secret string, for log redaction. Never expose this over HTTP."""
        raw = [
            self.gemini_api_key.get_secret_value(),
            self.groq_api_key.get_secret_value(),
            self.anthropic_api_key.get_secret_value(),
        ]
        # Ignore short/blank values - redacting "" would blank out all output.
        return [value for value in raw if len(value) >= 8]

    def has_provider_key(self, provider: LLMProvider) -> bool:
        mapping = {
            "gemini": self.gemini_api_key,
            "groq": self.groq_api_key,
            "anthropic": self.anthropic_api_key,
        }
        return bool(mapping[provider].get_secret_value())

    def configured_providers(self) -> list[LLMProvider]:
        """Providers with a key, preferred first.

        The order encodes the free-tier strategy: Gemini's high tokens-per-minute
        ceiling handles requests carrying file context, Groq is the fast fallback
        for short turns, and Anthropic is only ever reached if explicitly chosen.
        """
        preferred: list[LLMProvider] = [self.llm_provider]
        if self.llm_fallback:
            for name in ("gemini", "groq", "anthropic"):
                if name not in preferred:
                    preferred.append(name)  # type: ignore[arg-type]
        return [name for name in preferred if self.has_provider_key(name)]

    def tpm_budget(self, provider: LLMProvider) -> int:
        """Tokens-per-minute ceiling for a provider. 0 means unknown/unlimited."""
        if provider == "groq":
            return self.groq_tpm_budget
        if provider == "gemini":
            return self.gemini_tpm_budget
        return 0

    def read_or_create_auth_token(self) -> str:
        """The bearer token every client must present.

        Generated once on first run and stored outside the repo. Regenerating is
        as simple as deleting the file, which also unpairs every device.
        """
        self.ensure_dirs()
        path = self.auth_token_path
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        token = secrets.token_urlsafe(32)
        path.write_text(token, encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        return token


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def reload_settings() -> Settings:
    """Drop the cached settings. Used by tests and the settings screen."""
    get_settings.cache_clear()
    return get_settings()
