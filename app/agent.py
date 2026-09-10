"""The agent loop: think, call tools, then answer.

Shape of a turn:

1. Ask the model, with tools available. Not streamed - a tool round trip has
   nothing to show until it resolves.
2. Run whatever it asked for. Mutating tools queue a confirmation instead.
3. Repeat until it stops asking, or the iteration budget runs out.
4. Stream the final answer, with tools switched off so it cannot start another
   round mid-sentence.

Step 4 costs one extra request when tools were used. That is worth paying:
without it the answer arrives as a single block after several seconds of
silence, which reads as a hang.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.llm import toolcalls
from app.llm.base import (
    ChatMessage,
    NoProviderAvailable,
    ProviderError,
    ToolResult,
    Turn,
)
from app.llm.providers import build
from app.llm.router import Router
from app.persona.prompt import wrap_untrusted
from app.tools import registry

logger = logging.getLogger(__name__)


@dataclass
class AgentStep:
    tool: str
    arguments: dict
    result: str
    queued: bool = False


@dataclass
class AgentOutcome:
    provider: str = ""
    model: str = ""
    steps: list[AgentStep] = field(default_factory=list)
    # Set when the loop hit its ceiling, so the caller can say so honestly
    # instead of presenting a truncated investigation as a finished one.
    exhausted: bool = False


class Agent:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.outcome = AgentOutcome()

    def _provider(self):
        names = self.settings.configured_providers()
        if not names:
            raise NoProviderAvailable(
                "No language model is configured. Add a free GEMINI_API_KEY to .env "
                "(https://aistudio.google.com/apikey) and restart."
            )
        # Tool calling uses the preferred provider only. Failing over
        # mid-investigation would restart the reasoning on a different model
        # with a partially built transcript, which is worse than failing.
        provider = build(names[0], self.settings)
        self.outcome.provider = provider.name
        self.outcome.model = provider.model
        return provider

    async def _complete(self, provider, system: str, transcript, tools):
        """One tool-calling round trip, with backoff.

        Free tiers return 429 under ordinary use and 503 when the model is busy,
        and both clear in seconds. Without a retry here a transient upstream
        blip ends the turn - the streaming path already retries, and this path
        is the one that does the actual work.
        """
        attempts = max(1, self.settings.llm_max_retries)
        for attempt in range(attempts):
            try:
                return await toolcalls.complete(provider, system, transcript, tools)
            except ProviderError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
                delay = self.settings.llm_backoff_base_seconds * (2**attempt)
                logger.info(
                    "%s: %s - retrying in %.1fs (attempt %d/%d).",
                    provider.name, exc, delay, attempt + 2, attempts,
                )
                await asyncio.sleep(delay)
        raise ProviderError(provider.name, "Exhausted retries.", retryable=True)

    async def run(
        self, system: str, history: list[ChatMessage], conversation_id: int
    ) -> AsyncIterator[str]:
        """Run the turn and yield the final answer as text deltas."""
        provider = self._provider()
        tools = registry.specs()

        transcript: list[Turn] = [
            Turn(role="assistant" if m.role == "assistant" else "user", content=m.content)
            for m in history
        ]

        if not tools:
            async for delta in provider.stream(system, history):
                yield delta
            return

        used_tools = False

        for _iteration in range(self.settings.max_tool_iterations):
            completion = await self._complete(provider, system, transcript, tools)

            if not completion.wants_tools:
                if not used_tools:
                    # Answered without touching a tool. The text is already in
                    # hand, so stream it rather than paying for a second call.
                    for piece in _as_deltas(completion.text):
                        yield piece
                    return
                break

            used_tools = True
            transcript.append(
                Turn(role="assistant", content=completion.text, tool_calls=completion.tool_calls)
            )

            results: list[ToolResult] = []
            for call in completion.tool_calls:
                logger.info("Tool call: %s(%s)", call.name, _brief(call.arguments))
                raw = registry.dispatch(call.name, call.arguments, self.settings, conversation_id)
                tool = registry.get(call.name)
                queued = bool(tool and tool.mutating)

                self.outcome.steps.append(
                    AgentStep(
                        tool=call.name,
                        arguments=call.arguments,
                        result=raw[:2000],
                        queued=queued,
                    )
                )
                # Tool output carries text other people wrote - file contents
                # now, email later. Fence it so an instruction inside a document
                # cannot pose as an instruction from Jansen. A queued-action
                # notice is Alfred's own text, so it is not fenced.
                content = (
                    raw
                    if queued
                    else wrap_untrusted(raw, f"the {call.name} tool", self.settings)
                )
                results.append(ToolResult(id=call.id, name=call.name, content=content))

            transcript.append(Turn(role="tool", tool_results=results))
        else:
            self.outcome.exhausted = True
            transcript.append(
                Turn(
                    role="user",
                    content=(
                        "You have used your tool budget for this turn. Answer now with what "
                        "you have, and say plainly what you could not finish looking into."
                    ),
                )
            )

        # Final pass: tools off, streamed. Routed through the Router rather than
        # the raw provider so this call gets the same retry and failover as an
        # ordinary chat turn - a transient 503 here would otherwise throw away a
        # completed investigation.
        final_history = _to_messages(transcript)
        try:
            router = Router(self.settings)
            async for delta in router.stream(system, final_history):
                yield delta
            if router.used:
                self.outcome.provider = router.used
                self.outcome.model = router.used_model
        except ProviderError:
            # The investigation succeeded; only the phrasing call failed. Fall
            # back to a plain summary rather than losing the work entirely.
            for piece in _as_deltas(_summarise(self.outcome)):
                yield piece


def _brief(arguments: dict) -> str:
    parts = []
    for key, value in arguments.items():
        text = str(value)
        parts.append(f"{key}={text[:60]}{'...' if len(text) > 60 else ''}")
    return ", ".join(parts)


def _as_deltas(text: str, size: int = 24):
    """Chunk a finished string so the UI still animates."""
    for offset in range(0, len(text), size):
        yield text[offset : offset + size]


def _to_messages(transcript: list[Turn]) -> list[ChatMessage]:
    """Flatten the tool transcript for the final, tool-free streaming call.

    Tool results become user-visible context rather than assistant text, so the
    model treats them as material to answer from rather than as something it
    already said.
    """
    messages: list[ChatMessage] = []
    for turn in transcript:
        if turn.role == "user" and turn.content:
            messages.append(ChatMessage(role="user", content=turn.content))
        elif turn.role == "assistant" and turn.content:
            messages.append(ChatMessage(role="assistant", content=turn.content))
        elif turn.role == "tool":
            for result in turn.tool_results:
                messages.append(
                    ChatMessage(
                        role="user",
                        content=f"[result of {result.name}]\n{result.content}",
                    )
                )
    if messages and messages[-1].role == "assistant":
        messages.append(
            ChatMessage(role="user", content="Now answer, in your own voice, briefly.")
        )
    return messages


def _summarise(outcome: AgentOutcome) -> str:
    if not outcome.steps:
        return "I could not complete that, sir."
    lines = ["I looked into it, sir, but could not phrase a reply. What I found:"]
    for step in outcome.steps:
        lines.append(f"\n{step.tool}: {step.result[:600]}")
    return "\n".join(lines)
