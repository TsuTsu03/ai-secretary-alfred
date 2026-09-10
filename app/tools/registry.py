"""The tool registry and the confirmation gate.

Two invariants live here, and they are the reason this file exists at all
rather than tools being called directly from the agent loop:

1. **A tool marked ``mutating`` never executes from a model tool call.** It
   writes a :class:`~app.models.PendingAction` and returns a description. Only
   an explicit approval, arriving later on its own route, runs it - and it runs
   the arguments that were *shown to Jansen*, replayed verbatim, never
   re-derived from the model.

2. **Every tool result is untrusted content.** Results carry text from files and
   (later) email, which other people wrote. The agent fences them before they
   reach the model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.llm.base import ToolSpec

logger = logging.getLogger(__name__)


class ToolError(RuntimeError):
    """A tool failed in a way worth telling the model about."""


@dataclass
class Tool:
    spec: ToolSpec
    run: Callable[..., str]
    # Mutating tools change something outside Alfred: a file, a calendar, a
    # mailbox. They are never executed directly from a model tool call.
    mutating: bool = False
    # Rendered on the confirmation card. Receives the arguments.
    summarize: Callable[[dict], str] | None = None
    describe: Callable[[dict], str] | None = None


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    _REGISTRY[tool.spec.name] = tool
    return tool


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def specs() -> list[ToolSpec]:
    return [tool.spec for tool in _REGISTRY.values()]


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def clear() -> None:
    """Used by tests."""
    _REGISTRY.clear()


def request_confirmation(
    conversation_id: int, tool: Tool, arguments: dict
) -> str:
    """Record a pending action instead of performing it.

    Returns the text handed back to the model, which is deliberately explicit
    that nothing has happened yet - otherwise the model reports success and
    Jansen believes a file was written when it was not.
    """
    from app.db import session_scope
    from app.models import PendingAction

    summary = tool.summarize(arguments) if tool.summarize else f"Run {tool.spec.name}"
    detail = tool.describe(arguments) if tool.describe else json.dumps(arguments, indent=2)

    with session_scope() as session:
        action = PendingAction(
            conversation_id=conversation_id,
            tool_name=tool.spec.name,
            summary=summary,
            detail=detail,
            arguments_json=json.dumps(arguments),
        )
        session.add(action)
        session.flush()
        action_id = action.id

    logger.info("Queued %s for approval (action %s).", tool.spec.name, action_id)
    return (
        f"NOT DONE YET. This action needs {'{user}'} to approve it first. "
        f"A confirmation card (id {action_id}) is now on his screen showing: {summary}. "
        f"Tell him what you are proposing and that you are waiting on his approval. "
        f"Do not claim it has been done."
    )


def execute_approved(action_id: int, settings: Settings) -> tuple[bool, str]:
    """Run an action Jansen has approved.

    The arguments come from the stored row, not from the model. What he saw on
    the card is exactly what runs, even if the conversation has moved on.
    """
    from datetime import UTC, datetime

    from app.db import session_scope
    from app.models import ActionStatus, PendingAction

    with session_scope() as session:
        action = session.get(PendingAction, action_id)
        if action is None:
            return False, "No such action."
        if action.status != ActionStatus.APPROVED:
            return False, f"That action is {action.status.value}, not approved."
        tool = get(action.tool_name)
        if tool is None:
            action.status = ActionStatus.FAILED
            action.result = f"Unknown tool {action.tool_name!r}."
            session.add(action)
            return False, action.result

        try:
            arguments = json.loads(action.arguments_json or "{}")
        except json.JSONDecodeError:
            action.status = ActionStatus.FAILED
            action.result = "The stored arguments were unreadable."
            session.add(action)
            return False, action.result

        try:
            result = tool.run(settings=settings, **arguments)
            action.status = ActionStatus.EXECUTED
            action.result = result[:2000]
            ok = True
        except Exception as exc:  # surfaced on the card, never swallowed
            logger.exception("Approved action %s failed", action_id)
            action.status = ActionStatus.FAILED
            action.result = str(exc)[:2000]
            ok = False

        action.resolved_at = datetime.now(UTC)
        session.add(action)
        return ok, action.result


def dispatch(
    name: str, arguments: dict, settings: Settings, conversation_id: int
) -> str:
    """Run a tool call from the model, or queue it if it mutates anything."""
    tool = get(name)
    if tool is None:
        return f"There is no tool called {name!r}."

    if tool.mutating:
        return request_confirmation(conversation_id, tool, arguments).replace(
            "{user}", settings.user_name
        )

    try:
        return tool.run(settings=settings, **arguments)
    except ToolError as exc:
        return f"That did not work: {exc}"
    except TypeError as exc:
        # A model supplying the wrong argument names should get a correction it
        # can act on, not a stack trace.
        return f"Those arguments were wrong ({exc}). Check the tool's parameters and try again."
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return f"That failed unexpectedly: {exc}"
