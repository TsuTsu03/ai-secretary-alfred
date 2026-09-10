"""Tool calling across three providers that disagree about everything.

Each provider renders the neutral :class:`~app.llm.base.Turn` transcript into
its own shape and parses its own response back into a
:class:`~app.llm.base.Completion`. Keeping that translation here rather than in
``providers.py`` keeps the streaming path - which needs none of it - readable.

These calls are **not** streamed. A tool round trip has nothing to show the user
until it resolves, and parsing partially-delivered tool calls across three
different wire formats buys nothing but bugs. The final answer is streamed
separately, once the tools are done.
"""

from __future__ import annotations

import json
import logging
import uuid

import httpx

from app.llm.base import Completion, ProviderError, ToolCall, ToolSpec, Turn, is_retryable_status

logger = logging.getLogger(__name__)


async def _post(url: str, headers: dict, body: dict, timeout: float, provider: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            if response.status_code >= 400:
                detail = response.text[:400]
                raise ProviderError(
                    provider,
                    f"{provider} returned {response.status_code}: {detail}",
                    status=response.status_code,
                    retryable=is_retryable_status(response.status_code),
                )
            return response.json()
    except httpx.HTTPError as exc:
        raise ProviderError(provider, f"{provider} transport error: {exc}", retryable=True) from exc


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _gemini_contents(transcript: list[Turn]) -> list[dict]:
    contents: list[dict] = []
    for turn in transcript:
        if turn.role == "user":
            contents.append({"role": "user", "parts": [{"text": turn.content}]})
        elif turn.role == "assistant":
            parts: list[dict] = []
            if turn.content:
                parts.append({"text": turn.content})
            for call in turn.tool_calls:
                part: dict = {"functionCall": {"name": call.name, "args": call.arguments}}
                if call.signature:
                    # Required by Gemini's thinking models; the request is
                    # rejected outright without it.
                    part["thoughtSignature"] = call.signature
                parts.append(part)
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif turn.role == "tool":
            contents.append(
                {
                    # Not "function". Some Gemini docs show a `function` role,
                    # but the live v1beta endpoint rejects it outright:
                    #   400 Role 'function' is not supported.
                    # A functionResponse part belongs on a user turn.
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": result.name,
                                # Gemini requires an object here, not a string.
                                "response": {"result": result.content},
                            }
                        }
                        for result in turn.tool_results
                    ],
                }
            )
    return contents


async def gemini_complete(
    provider, system: str, transcript: list[Turn], tools: list[ToolSpec]
) -> Completion:
    body: dict = {
        "contents": _gemini_contents(transcript),
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 2048},
    }
    if tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {"name": t.name, "description": t.description, "parameters": t.parameters}
                    for t in tools
                ]
            }
        ]

    url = f"{provider.BASE}/models/{provider.model}:generateContent"
    data = await _post(
        url,
        {"x-goog-api-key": provider._key, "Content-Type": "application/json"},
        body,
        provider._timeout,
        "gemini",
    )

    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("text"):
                text_parts.append(part["text"])
            call = part.get("functionCall")
            if call:
                calls.append(
                    ToolCall(
                        # Gemini does not issue call ids; results are matched by
                        # name, so a local id is enough for our own bookkeeping.
                        id=uuid.uuid4().hex[:8],
                        name=call.get("name", ""),
                        arguments=call.get("args") or {},
                        signature=part.get("thoughtSignature", "") or "",
                    )
                )
    return Completion(text="".join(text_parts).strip(), tool_calls=calls)


# ---------------------------------------------------------------------------
# Groq (OpenAI-shaped)
# ---------------------------------------------------------------------------


def _openai_messages(system: str, transcript: list[Turn]) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system}]
    for turn in transcript:
        if turn.role == "user":
            messages.append({"role": "user", "content": turn.content})
        elif turn.role == "assistant":
            message: dict = {"role": "assistant", "content": turn.content or None}
            if turn.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in turn.tool_calls
                ]
            messages.append(message)
        elif turn.role == "tool":
            for result in turn.tool_results:
                messages.append(
                    {"role": "tool", "tool_call_id": result.id, "content": result.content}
                )
    return messages


async def groq_complete(
    provider, system: str, transcript: list[Turn], tools: list[ToolSpec]
) -> Completion:
    body: dict = {
        "model": provider.model,
        "messages": _openai_messages(system, transcript),
        "temperature": 0.6,
        "max_tokens": 1536,
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    data = await _post(
        provider.URL,
        {"Authorization": f"Bearer {provider._key}", "Content-Type": "application/json"},
        body,
        provider._timeout,
        "groq",
    )

    choices = data.get("choices") or [{}]
    message = choices[0].get("message", {})
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        raw = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            # A model that emits malformed JSON arguments should not crash the
            # turn; the tool will reject the empty call and say so.
            logger.warning("Discarding malformed tool arguments: %r", raw[:200])
            arguments = {}
        calls.append(
            ToolCall(id=call.get("id") or uuid.uuid4().hex[:8],
                     name=function.get("name", ""),
                     arguments=arguments)
        )
    return Completion(text=(message.get("content") or "").strip(), tool_calls=calls)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _anthropic_messages(transcript: list[Turn]) -> list[dict]:
    messages: list[dict] = []
    for turn in transcript:
        if turn.role == "user":
            messages.append({"role": "user", "content": turn.content})
        elif turn.role == "assistant":
            blocks: list[dict] = []
            if turn.content:
                blocks.append({"type": "text", "text": turn.content})
            for call in turn.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        elif turn.role == "tool":
            # Anthropic carries tool results on a *user* turn, not a tool one.
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result.id,
                            "content": result.content,
                        }
                        for result in turn.tool_results
                    ],
                }
            )
    return messages


async def anthropic_complete(
    provider, system: str, transcript: list[Turn], tools: list[ToolSpec]
) -> Completion:
    body: dict = {
        "model": provider.model,
        "system": system,
        "messages": _anthropic_messages(transcript),
        "max_tokens": 2048,
    }
    if tools:
        body["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    data = await _post(
        provider.URL,
        {
            "x-api-key": provider._key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body,
        provider._timeout,
        "anthropic",
    )

    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append(
                ToolCall(
                    id=block.get("id") or uuid.uuid4().hex[:8],
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                )
            )
    return Completion(text="".join(text_parts).strip(), tool_calls=calls)


COMPLETERS = {
    "gemini": gemini_complete,
    "groq": groq_complete,
    "anthropic": anthropic_complete,
}


async def complete(
    provider, system: str, transcript: list[Turn], tools: list[ToolSpec]
) -> Completion:
    completer = COMPLETERS.get(provider.name)
    if completer is None:
        raise ProviderError(provider.name, f"{provider.name} cannot call tools.", retryable=False)
    return await completer(provider, system, transcript, tools)
