"""Tests for provider selection and failover.

The rule these exist to protect is the one that is easy to break and hard to
notice: **once a token has been emitted, the router must not fall back.**
Falling back mid-stream splices half of one answer onto half of another, and
the result reads as a model malfunction rather than a routing bug.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.config import Settings
from app.llm import router as router_module
from app.llm.base import ChatMessage, NoProviderAvailable, ProviderError, estimate_tokens
from app.llm.router import Router, friendly_error


class FakeProvider:
    """A provider whose behaviour each test dictates."""

    def __init__(
        self,
        name: str,
        *,
        chunks: list[str] | None = None,
        fail_with: ProviderError | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.name = name
        self.model = f"{name}-model"
        self._chunks = chunks or [f"hello from {name}"]
        self._fail_with = fail_with
        self._fail_after = fail_after
        self.calls = 0

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        self.calls += 1
        if self._fail_with is not None and self._fail_after is None:
            raise self._fail_with
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index == self._fail_after:
                assert self._fail_with is not None
                raise self._fail_with
            yield chunk


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ALFRED_DATA_DIR=str(tmp_path / "data"),
        GEMINI_API_KEY="AIza-test-key-value",
        GROQ_API_KEY="gsk_test_key_value",
        ALFRED_LLM_MAX_RETRIES=1,
        ALFRED_LLM_BACKOFF_BASE_SECONDS=0.0,
    )  # type: ignore[call-arg]


def install(monkeypatch, providers: dict[str, FakeProvider]) -> None:
    monkeypatch.setattr(router_module, "build", lambda name, _settings: providers[name])


async def drain(router: Router, text: str = "hello") -> str:
    return "".join([c async for c in router.stream("system", [ChatMessage("user", text)])])


# ── the happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uses_the_preferred_provider(settings, monkeypatch) -> None:
    gemini = FakeProvider("gemini", chunks=["Good ", "evening."])
    groq = FakeProvider("groq")
    install(monkeypatch, {"gemini": gemini, "groq": groq})

    router = Router(settings)
    assert await drain(router) == "Good evening."
    assert router.used == "gemini"
    assert groq.calls == 0


# ── failover before any output ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_falls_back_when_the_first_provider_is_rate_limited(settings, monkeypatch) -> None:
    gemini = FakeProvider(
        "gemini", fail_with=ProviderError("gemini", "429", status=429, retryable=True)
    )
    groq = FakeProvider("groq", chunks=["Groq answered."])
    install(monkeypatch, {"gemini": gemini, "groq": groq})

    router = Router(settings)
    assert await drain(router) == "Groq answered."
    assert router.used == "groq"
    assert [a.ok for a in router.attempts] == [False, True]


@pytest.mark.asyncio
async def test_does_not_fall_back_on_a_non_retryable_error(settings, monkeypatch) -> None:
    """A malformed request fails identically everywhere; trying again wastes quota."""
    gemini = FakeProvider(
        "gemini", fail_with=ProviderError("gemini", "bad request", status=400, retryable=False)
    )
    groq = FakeProvider("groq")
    install(monkeypatch, {"gemini": gemini, "groq": groq})

    with pytest.raises(ProviderError):
        await drain(Router(settings))
    assert groq.calls == 0


# ── the rule that matters ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_never_falls_back_after_the_first_token(settings, monkeypatch) -> None:
    """Half an answer is on screen. A second provider must not append to it."""
    gemini = FakeProvider(
        "gemini",
        chunks=["Good evening, ", "sir."],
        fail_with=ProviderError("gemini", "died", status=500, retryable=True),
        fail_after=1,
    )
    groq = FakeProvider("groq", chunks=[" COMPLETELY DIFFERENT ANSWER"])
    install(monkeypatch, {"gemini": gemini, "groq": groq})

    router = Router(settings)
    collected: list[str] = []
    with pytest.raises(ProviderError):
        async for chunk in router.stream("system", [ChatMessage("user", "hi")]):
            collected.append(chunk)

    assert "".join(collected) == "Good evening, "
    assert groq.calls == 0, "fell back mid-stream and spliced two answers together"


@pytest.mark.asyncio
async def test_does_not_retry_the_same_provider_after_the_first_token(
    settings, monkeypatch
) -> None:
    """Retrying mid-stream would repeat the text already delivered."""
    gemini = FakeProvider(
        "gemini",
        # Two chunks so index 1 is actually reached and the failure fires
        # *after* the first has already been delivered.
        chunks=["Partial", "never sent"],
        fail_with=ProviderError("gemini", "429", status=429, retryable=True),
        fail_after=1,
    )
    install(monkeypatch, {"gemini": gemini, "groq": FakeProvider("groq")})

    settings.llm_max_retries = 3
    router = Router(settings)
    collected: list[str] = []
    with pytest.raises(ProviderError):
        async for chunk in router.stream("system", [ChatMessage("user", "hi")]):
            collected.append(chunk)

    assert collected == ["Partial"]
    assert gemini.calls == 1, "retried after emitting, which would duplicate delivered text"


# ── retry before output ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retries_the_same_provider_when_nothing_was_emitted(
    settings, monkeypatch
) -> None:
    """Free tiers 429 under normal use; a short wait is cheaper than failing over."""

    class FlakyThenFine:
        name = "gemini"
        model = "gemini-model"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, system, messages):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("gemini", "429", status=429, retryable=True)
            yield "Second attempt."

    flaky = FlakyThenFine()
    settings.llm_max_retries = 3
    install(monkeypatch, {"gemini": flaky, "groq": FakeProvider("groq")})

    router = Router(settings)
    assert await drain(router) == "Second attempt."
    assert flaky.calls == 2


# ── rate-limit aware routing ─────────────────────────────────────────────


def test_a_large_prompt_skips_the_low_ceiling_provider(settings) -> None:
    """Groq's ~6k TPM cannot take a prompt carrying file context."""
    router = Router(settings)
    huge = estimate_tokens("s" * 40_000, [ChatMessage("user", "x" * 40_000)])
    assert "groq" not in router._candidates(huge)
    assert "gemini" in router._candidates(huge)


def test_a_small_prompt_keeps_every_provider(settings) -> None:
    router = Router(settings)
    assert set(router._candidates(estimate_tokens("hi", []))) == {"gemini", "groq"}


def test_filter_never_empties_the_candidate_list(tmp_path: Path) -> None:
    """A likely 429 still beats refusing to answer at all."""
    groq_only = Settings(
        ALFRED_DATA_DIR=str(tmp_path / "d"),
        GROQ_API_KEY="gsk_test_key_value",
        ALFRED_LLM_PROVIDER="groq",
    )  # type: ignore[call-arg]
    router = Router(groq_only)
    assert router._candidates(999_999) == ["groq"]


# ── nothing configured ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_keys_raises_a_useful_error(tmp_path: Path) -> None:
    bare = Settings(ALFRED_DATA_DIR=str(tmp_path / "d"))  # type: ignore[call-arg]
    with pytest.raises(NoProviderAvailable):
        await drain(Router(bare))


def test_friendly_errors_name_the_remedy() -> None:
    """Alfred apologising is fine; Alfred hiding the fix is not."""
    assert "GEMINI_API_KEY" in friendly_error(NoProviderAvailable("none"))
    assert "limit" in friendly_error(ProviderError("gemini", "x", status=429)).lower()
    assert "key" in friendly_error(ProviderError("gemini", "x", status=401)).lower()
