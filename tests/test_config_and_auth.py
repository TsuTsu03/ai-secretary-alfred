"""Tests for the network bind guard and token authentication.

The bind validator is the inverse of the one in the local-meeting-assistant it
was adapted from: Alfred *must* be reachable from the phone, so "loopback only"
is not available as a safety mechanism. What replaces it is a narrow allowlist
- loopback or Tailscale's CGNAT range - plus a token on every route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.security import auth


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = {"ALFRED_DATA_DIR": str(tmp_path / "data")}
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


# ── bind guard ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "100.64.0.1", "100.101.102.103"])
def test_accepts_loopback_and_tailscale(tmp_path: Path, host: str) -> None:
    assert _settings(tmp_path, ALFRED_HOST=host).host == host


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",          # the mistake this guard exists to prevent
        "192.168.1.10",     # LAN: reachable by every device on the Wi-Fi
        "10.0.0.5",
        "8.8.8.8",
        "::",
        "example.com",
        "",
    ],
)
def test_rejects_public_and_lan_binds(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, ALFRED_HOST=host)


def test_rejection_message_names_the_alternative(tmp_path: Path) -> None:
    """A refusal that does not say what to do instead just gets worked around."""
    with pytest.raises(ValidationError) as info:
        _settings(tmp_path, ALFRED_HOST="0.0.0.0")
    assert "tailscale serve" in str(info.value)


# ── tokens ───────────────────────────────────────────────────────────────


def test_token_is_generated_once_and_reused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = settings.read_or_create_auth_token()
    assert len(first) >= 32
    assert settings.read_or_create_auth_token() == first


def test_token_lives_outside_the_repo(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.read_or_create_auth_token()
    assert settings.auth_token_path.is_relative_to(settings.data_dir)


def test_deleting_the_token_file_rotates_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = settings.read_or_create_auth_token()
    settings.auth_token_path.unlink()
    assert settings.read_or_create_auth_token() != first


def test_token_matches_only_the_real_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    token = settings.read_or_create_auth_token()
    assert auth.token_matches(token, settings)
    assert auth.token_matches(f"  {token}  ", settings)  # trimmed
    assert not auth.token_matches(token[:-1], settings)
    assert not auth.token_matches(token + "x", settings)
    assert not auth.token_matches("", settings)


def test_pairing_url_puts_the_token_in_the_fragment(tmp_path: Path) -> None:
    """A fragment never reaches the server, so it never reaches a server log."""
    settings = _settings(tmp_path, ALFRED_TAILSCALE_HOSTNAME="box.tail1234.ts.net")
    url = auth.pairing_url(settings)
    assert url.startswith("https://box.tail1234.ts.net/#token=")
    assert "?" not in url


# ── providers and cost posture ───────────────────────────────────────────


def test_no_keys_means_no_providers(tmp_path: Path) -> None:
    settings = _settings(tmp_path, GEMINI_API_KEY="", GROQ_API_KEY="", ANTHROPIC_API_KEY="")
    assert settings.configured_providers() == []


def test_free_providers_are_preferred_over_the_paid_one(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        GEMINI_API_KEY="AIza-test-key-value",
        GROQ_API_KEY="gsk_test_key_value",
        ANTHROPIC_API_KEY="sk-ant-test-key-value",
    )
    order = settings.configured_providers()
    assert order[0] == "gemini"
    assert order.index("groq") < order.index("anthropic")


def test_paid_provider_is_never_reached_unless_chosen(tmp_path: Path) -> None:
    """Anthropic costs money, so a stray key must not silently start billing."""
    settings = _settings(tmp_path, GEMINI_API_KEY="AIza-test-key-value", ANTHROPIC_API_KEY="")
    assert "anthropic" not in settings.configured_providers()


def test_groq_budget_is_far_below_gemini(tmp_path: Path) -> None:
    """The router relies on this gap to keep file context off Groq's free tier."""
    settings = _settings(tmp_path)
    assert settings.tpm_budget("groq") < settings.tpm_budget("gemini")


def test_secret_values_skips_blanks(tmp_path: Path) -> None:
    """Redacting the empty string would blank out every log line."""
    settings = _settings(tmp_path, GEMINI_API_KEY="", GROQ_API_KEY="gsk_long_enough_value")
    values = settings.secret_values()
    assert "" not in values
    assert "gsk_long_enough_value" in values
