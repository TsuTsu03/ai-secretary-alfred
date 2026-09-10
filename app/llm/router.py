"""Chooses which provider answers, and what happens when one fails.

Two rules do most of the work here.

**Never fall back after the first token has been emitted.** Once text has
reached the browser, switching providers would splice a second, unrelated
answer onto a half-finished one. From that point a failure is an error, not a
retry. This is the single most important behaviour in this module and the
easiest to get wrong.

**Route by tokens-per-minute, not by preference alone.** Groq's free tier
allows roughly 6,000 tokens a minute. A prompt carrying retrieved file context
blows through that and returns a 429 every time, so the router skips providers
whose ceiling the request obviously exceeds rather than burning a round trip to
discover it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.llm.base import (
    ChatMessage,
    NoProviderAvailable,
    ProviderError,
    estimate_tokens,
)
from app.llm.providers import build

logger = logging.getLogger(__name__)


@dataclass
class Attempt:
    """What actually happened, for the status rail and for debugging."""

    provider: str
    model: str
    ok: bool
    error: str = ""


class Router:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.attempts: list[Attempt] = []
        self.used: str = ""
        self.used_model: str = ""

    def _candidates(self, estimated: int) -> list[str]:
        """Configured providers, minus any whose per-minute ceiling this
        request clearly exceeds.

        The filter is skipped entirely if it would leave nothing: a likely 429
        is still better than refusing to answer.
        """
        configured = self.settings.configured_providers()
        if not configured:
            return []

        affordable = []
        for name in configured:
            budget = self.settings.tpm_budget(name)
            # A budget of 0 means unknown/unlimited.
            if budget and estimated > budget * 0.8:
                logger.debug(
                    "Skipping %s: ~%d tokens exceeds its %d/min ceiling.",
                    name, estimated, budget,
                )
                continue
            affordable.append(name)
        return affordable or configured

    async def stream(
        self, system: str, messages: list[ChatMessage]
    ) -> AsyncIterator[str]:
        """Stream a reply, moving to the next provider only before first token."""
        estimated = estimate_tokens(system, messages)
        candidates = self._candidates(estimated)

        if not candidates:
            raise NoProviderAvailable(
                "No language model is configured. Add a free GEMINI_API_KEY to .env "
                "(https://aistudio.google.com/apikey) and restart."
            )

        last_error: ProviderError | None = None

        for index, name in enumerate(candidates):
            provider = build(name, self.settings)
            emitted = False
            try:
                async for delta in self._with_retries(provider, system, messages):
                    emitted = True
                    yield delta
            except ProviderError as exc:
                self.attempts.append(
                    Attempt(name, provider.model, ok=False, error=str(exc))
                )
                if emitted:
                    # Half an answer is already on screen. Splicing a second
                    # provider's attempt onto it would produce nonsense, so
                    # stop here and let the caller surface the failure.
                    logger.error("%s failed mid-stream; not falling back.", name)
                    raise
                last_error = exc
                is_last = index == len(candidates) - 1
                if not exc.retryable or is_last:
                    if is_last:
                        continue
                    raise
                logger.warning("%s unavailable (%s); trying the next provider.", name, exc)
                continue

            self.attempts.append(Attempt(name, provider.model, ok=True))
            self.used = name
            self.used_model = provider.model
            return

        raise last_error or NoProviderAvailable("Every configured provider failed.")

    async def _with_retries(
        self, provider, system: str, messages: list[ChatMessage]
    ) -> AsyncIterator[str]:
        """Retry one provider with backoff, but only while nothing was emitted.

        Free tiers 429 under perfectly normal use, and a short wait usually
        clears it - which is cheaper than failing over to a provider with a
        lower ceiling.
        """
        attempts = max(1, self.settings.llm_max_retries)
        for attempt in range(attempts):
            emitted = False
            try:
                async for delta in provider.stream(system, messages):
                    emitted = True
                    yield delta
                return
            except ProviderError as exc:
                if emitted or not exc.retryable or attempt == attempts - 1:
                    raise
                delay = self.settings.llm_backoff_base_seconds * (2**attempt)
                logger.info(
                    "%s: %s - retrying in %.1fs (attempt %d/%d).",
                    provider.name, exc, delay, attempt + 2, attempts,
                )
                await asyncio.sleep(delay)


def friendly_error(exc: Exception) -> str:
    """An error message in Alfred's register rather than a stack trace.

    Alfred apologising is better product behaviour than a raw 429, but the
    underlying cause still has to be recoverable by the reader - so the
    remedy is named, not hidden behind politeness.
    """
    if isinstance(exc, NoProviderAvailable):
        return (
            "I am afraid I have no mind to think with, sir. Add a free GEMINI_API_KEY "
            "to the .env file and restart me."
        )
    if isinstance(exc, ProviderError):
        if exc.status == 429:
            return (
                "The free tier has reached its limit for the moment, sir. "
                "It will reset shortly - or add a GROQ_API_KEY as a second option."
            )
        if exc.status in (401, 403):
            return "My credentials appear to have been refused, sir. The API key may be wrong."
        return f"I could not reach my faculties, sir. ({exc})"
    return f"Something went wrong, sir. ({exc})"
