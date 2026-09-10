"""The provider contract.

Every backend Alfred can think with implements :class:`Provider`. Deliberately
tiny: a system prompt, a transcript, and a stream of text deltas out. Tool
calling is layered on top in Phase 3 rather than baked in here, so that adding
a provider stays a small job.

Raw HTTP via httpx rather than three vendor SDKs. Each SDK would drag in its
own transport, retry policy, and release cadence for what amounts to one POST
and an SSE parse per provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class ToolSpec:
    """A tool Alfred may call.

    ``parameters`` is JSON Schema, restricted to the subset every provider
    accepts: object with typed properties and a required list. Anything
    cleverer (oneOf, $ref) is silently dropped by at least one of them.
    """

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Opaque provider state that has to be echoed back verbatim when the call is
    # replayed. Gemini's thinking models return a `thoughtSignature` alongside a
    # functionCall and reject the next request without it:
    #   400 Function call is missing a thought_signature in functionCall parts.
    # Meaningless to us, mandatory to them.
    signature: str = ""


@dataclass(frozen=True)
class ToolResult:
    id: str
    name: str
    content: str


@dataclass
class Turn:
    """One entry in the neutral transcript the agent loop maintains.

    Providers disagree about how tool exchanges are represented - Gemini uses
    functionCall/functionResponse parts, the OpenAI-shaped APIs use a
    tool_calls array and a separate "tool" role. Rather than pick a winner and
    translate twice, the agent keeps a neutral record and each provider renders
    it into its own shape.
    """

    role: str  # "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass
class Completion:
    """One model response: some text, some tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ProviderError(RuntimeError):
    """A provider failed. Carries enough for the router to decide what next."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        # Retryable means "a different provider might succeed right now" -
        # rate limits, timeouts, upstream 5xx. A malformed request is not
        # retryable, because every provider would reject it identically.
        self.retryable = retryable


class NoProviderAvailable(ProviderError):
    """Nothing is configured, or everything configured has failed."""

    def __init__(self, message: str) -> None:
        super().__init__("none", message, retryable=False)


@runtime_checkable
class Provider(Protocol):
    name: str
    model: str

    def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        """Yield text deltas. Raise :class:`ProviderError` on failure."""
        ...


def estimate_tokens(system: str, messages: list[ChatMessage]) -> int:
    """Rough token count for rate-limit routing.

    Four characters per token is crude but the decision it feeds is coarse:
    "does this obviously exceed a 6,000-token-per-minute ceiling?" Being wrong
    by 20% does not change that answer, and counting exactly would mean
    shipping a tokenizer per provider.
    """
    characters = len(system) + sum(len(m.content) for m in messages)
    return characters // 4 + 32 * (len(messages) + 1)


def is_retryable_status(status: int) -> bool:
    """429 and 5xx are worth trying elsewhere; 4xx generally is not.

    408 and 409 are included because both mean "try again", not "your request
    is wrong".
    """
    return status == 429 or status in (408, 409) or 500 <= status < 600
