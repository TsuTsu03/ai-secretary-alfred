"""Concrete providers: Gemini, Groq, Anthropic.

All three are one POST and an SSE parse, so they live together rather than in
three near-identical modules. What differs between them is only the request
shape and where the text sits in each event.

Cost posture: Gemini and Groq are free tiers. Anthropic bills, so it is only
ever constructed when a key is explicitly configured and selected.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.llm.base import ChatMessage, ProviderError, is_retryable_status

logger = logging.getLogger(__name__)


async def _sse_lines(response: httpx.Response) -> AsyncIterator[str]:
    """Yield the payload of each ``data:`` line in an SSE stream."""
    async for raw in response.aiter_lines():
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload and payload != "[DONE]":
            yield payload


async def _raise_for_status(response: httpx.Response, provider: str) -> None:
    """Turn an error response into a ProviderError the router can act on.

    The body is read and included because provider error messages are the only
    way to tell "quota exhausted" from "model name is wrong", and both arrive
    as a 429 or a 400 with no other distinguishing feature.
    """
    if response.status_code < 400:
        return
    try:
        body = (await response.aread()).decode("utf-8", "replace")[:400]
    except Exception:  # pragma: no cover - body already consumed
        body = ""
    raise ProviderError(
        provider,
        f"{provider} returned {response.status_code}: {body}",
        status=response.status_code,
        retryable=is_retryable_status(response.status_code),
    )


class GeminiProvider:
    """Google AI Studio. Free tier: high tokens-per-minute, ~1,500 requests/day.

    The high TPM ceiling is why this is the default: it is the only free option
    that can absorb a prompt carrying retrieved file context.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, settings: Settings) -> None:
        self.model = settings.gemini_model
        self._key = settings.gemini_api_key.get_secret_value()
        self._timeout = settings.llm_timeout_seconds

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        body = {
            "contents": [
                # Gemini calls the assistant "model"; everything else is "user".
                {"role": "model" if m.role == "assistant" else "user",
                 "parts": [{"text": m.content}]}
                for m in messages
            ],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
        }
        url = f"{self.BASE}/models/{self.model}:streamGenerateContent"

        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream(
                    "POST",
                    url,
                    params={"alt": "sse"},
                    # Key in a header, not the query string, so it cannot be
                    # captured by an intermediary's URL logging.
                    headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
                    json=body,
                ) as response,
            ):
                await _raise_for_status(response, self.name)
                async for payload in _sse_lines(response):
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for candidate in event.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            text = part.get("text")
                            if text:
                                yield text
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"Gemini transport error: {exc}", retryable=True) from exc


class GroqProvider:
    """Groq's OpenAI-compatible endpoint. Free tier: very fast, ~6-8k TPM.

    That TPM ceiling is low enough that the router avoids Groq for anything
    carrying file context, and uses it for short conversational turns.
    """

    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, settings: Settings) -> None:
        self.model = settings.groq_model
        self._key = settings.groq_api_key.get_secret_value()
        self._timeout = settings.llm_timeout_seconds

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 1536,
        }
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream(
                    "POST",
                    self.URL,
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response,
            ):
                await _raise_for_status(response, self.name)
                async for payload in _sse_lines(response):
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for choice in event.get("choices", []):
                        text = choice.get("delta", {}).get("content")
                        if text:
                            yield text
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"Groq transport error: {exc}", retryable=True) from exc


class AnthropicProvider:
    """Anthropic. PAID - only constructed when a key is explicitly configured.

    Present so that swapping away from the free tiers is a one-line change in
    ``.env``, which matters because the free tiers may train on prompts and
    Alfred reads personal files and mail.
    """

    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, settings: Settings) -> None:
        self.model = settings.anthropic_model
        self._key = settings.anthropic_api_key.get_secret_value()
        self._timeout = settings.llm_timeout_seconds

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": 2048,
            "stream": True,
        }
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream(
                    "POST",
                    self.URL,
                    headers={
                        "x-api-key": self._key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response,
            ):
                await _raise_for_status(response, self.name)
                async for payload in _sse_lines(response):
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        text = event.get("delta", {}).get("text")
                        if text:
                            yield text
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name, f"Anthropic transport error: {exc}", retryable=True
            ) from exc


BUILDERS = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "anthropic": AnthropicProvider,
}


def build(name: str, settings: Settings):
    builder = BUILDERS.get(name)
    if builder is None:
        raise ProviderError("none", f"Unknown provider {name!r}", retryable=False)
    return builder(settings)
