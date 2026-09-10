"""Logging with hard guarantees about what never reaches disk.

Two rules are enforced by filters rather than by convention:

1. API keys are replaced with ``***REDACTED***`` everywhere, including inside
   exception tracebacks and third-party library messages.
2. Conversation and file text is not logged unless
   ``ALFRED_LOG_CONVERSATION_CONTENT=true``. Callers mark such records with
   ``extra={"private_content": True}``.

Alfred reads personal files and email, so the second rule matters more here
than it would in an ordinary service: a debug log left on by accident would
otherwise accumulate a plaintext copy of everything he has ever been shown.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from typing import Any

from app.config import Settings, get_settings

REDACTION = "***REDACTED***"

# Catches keys pasted into free-form text even when they are not the configured
# ones - e.g. a key echoed back inside an upstream provider's error body.
_KEY_PATTERNS = [
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),        # Google / Gemini (classic)
    # Newer Google AI Studio keys look like "AQ.Ab8RN6...". They do not match
    # the AIza pattern, so without this an upstream error echoing the key back
    # would be written to the log file verbatim.
    re.compile(r"AQ\.[A-Za-z0-9_\-]{20,}"),        # Google / Gemini (current)
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),            # Groq
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),      # Anthropic
    re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),       # OpenRouter
    re.compile(r"sk-[A-Za-z0-9]{32,}"),             # OpenAI-style
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),       # Google OAuth access token
    re.compile(r"1//[A-Za-z0-9_\-]{20,}"),          # Google OAuth refresh token
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
]


class SecretRedactingFilter(logging.Filter):
    """Scrubs known secrets and secret-shaped strings from every record."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._literals = settings.secret_values()

    def _scrub(self, text: str) -> str:
        for literal in self._literals:
            if literal in text:
                text = text.replace(literal, REDACTION)
        for pattern in _KEY_PATTERNS:
            if pattern.groups:
                text = pattern.sub(rf"\1{REDACTION}", text)
            else:
                text = pattern.sub(REDACTION, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record args
            message = str(record.msg)
        scrubbed = self._scrub(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        return True


class PrivateContentFilter(logging.Filter):
    """Drops records flagged as containing conversation or file text."""

    def __init__(self, allow: bool) -> None:
        super().__init__()
        self._allow = allow

    def filter(self, record: logging.LogRecord) -> bool:
        return not (getattr(record, "private_content", False) and not self._allow)


_configured = False


def setup_logging(settings: Settings | None = None) -> None:
    """Idempotent logging setup. Safe to call from tests and from uvicorn."""
    global _configured
    if _configured:
        return

    settings = settings or get_settings()
    settings.ensure_dirs()

    root = logging.getLogger()
    root.setLevel(settings.log_level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    secret_filter = SecretRedactingFilter(settings)
    private_filter = PrivateContentFilter(settings.log_conversation_content)

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.logs_dir / "alfred.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    for handler in (console, file_handler):
        handler.addFilter(secret_filter)
        handler.addFilter(private_filter)
        root.addHandler(handler)

    # These libraries are chatty at INFO and say nothing we need.
    for noisy in ("httpx", "httpcore", "faster_whisper", "urllib3", "multipart", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def reset_logging() -> None:
    """Allow a fresh setup_logging(). Used by tests."""
    global _configured
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _configured = False


def redact(text: Any, settings: Settings | None = None) -> str:
    """Scrub secrets from an arbitrary value before showing it to a user.

    Used for error messages surfaced in the UI, where an upstream provider may
    echo a request header back to us.
    """
    settings = settings or get_settings()
    result = str(text)
    for literal in settings.secret_values():
        if literal in result:
            result = result.replace(literal, REDACTION)
    for pattern in _KEY_PATTERNS:
        if pattern.groups:
            result = pattern.sub(rf"\1{REDACTION}", result)
        else:
            result = pattern.sub(REDACTION, result)
    return result
