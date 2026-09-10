"""HTTP API.

Phase 0 surface: liveness, runtime status, and device pairing. Chat, voice,
files, calendar, and briefings attach to this router in later phases.

Every route here except ``/health`` depends on :func:`~app.security.auth.require_auth`.
"""

from __future__ import annotations

import base64
import io
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.llm.base import ChatMessage
from app.llm.router import Router, friendly_error
from app.persona.prompt import build_system_prompt
from app.security import auth
from app.security.paths import PathAccessError

logger = logging.getLogger(__name__)

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str


class RootStatus(BaseModel):
    path: str
    exists: bool


class StatusResponse(BaseModel):
    """What the HUD's status rail renders.

    Deliberately says nothing secret: provider *names*, never keys; root paths,
    never their contents.
    """

    version: str
    user_name: str
    user_address: str
    timezone: str
    providers: list[str]
    active_provider: str
    llm_ready: bool
    tts_engine: str
    tts_voice: str
    roots: list[RootStatus]
    base_url: str
    tailscale_configured: bool


class PairResponse(BaseModel):
    url: str
    qr_svg: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Unauthenticated liveness probe.

    Returns nothing an unauthenticated caller could not already infer from the
    port being open, so leaving it public is safe and makes `tailscale serve`
    easy to verify.
    """
    return HealthResponse(status="ok", version="0.1.0")


@router.get("/api/status", response_model=StatusResponse, dependencies=[Depends(auth.require_auth)])
def status(settings: Settings = Depends(get_settings)) -> StatusResponse:
    providers = settings.configured_providers()
    try:
        roots = [RootStatus(path=str(p), exists=p.is_dir()) for p in settings.file_roots]
    except PathAccessError:
        # No roots configured yet. The HUD renders this as a setup prompt rather
        # than an error, so a fresh install is not a broken-looking one.
        roots = []
    return StatusResponse(
        version="0.1.0",
        user_name=settings.user_name,
        user_address=settings.user_address,
        timezone=settings.timezone,
        providers=providers,
        active_provider=providers[0] if providers else "",
        llm_ready=bool(providers),
        tts_engine=settings.tts_engine,
        tts_voice=settings.tts_voice,
        roots=roots,
        base_url=settings.base_url,
        tailscale_configured=bool(settings.tailscale_hostname),
    )


def _qr_svg(payload: str) -> str:
    """Render a QR code as inline SVG.

    SVG rather than PNG so it stays crisp at any size and needs no data-URI
    image, which the CSP would otherwise have to allow.
    """
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:  # pragma: no cover - qrcode is a declared dependency
        return ""

    factory = qrcode.image.svg.SvgPathImage
    image = qrcode.make(payload, image_factory=factory, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


@router.get("/api/pair", response_model=PairResponse, dependencies=[Depends(auth.require_auth)])
def pair(settings: Settings = Depends(get_settings)) -> PairResponse:
    """The pairing payload shown on the laptop and scanned by the phone.

    Requires auth: the laptop browser is already paired (it can read the token
    from the local UI), so this cannot be used to bootstrap a device that has
    no token. Pairing a *first* device happens through the printed token in the
    console at startup, not through this route.
    """
    url = auth.pairing_url(settings)
    return PairResponse(url=url, qr_svg=_qr_svg(url))


class PairCheckResponse(BaseModel):
    paired: bool


@router.get("/api/pair/check", response_model=PairCheckResponse)
def pair_check(request: Request) -> PairCheckResponse:
    """Lets the client test a token without tripping the 401 error path.

    The HUD calls this on load to decide between the console and the pairing
    screen. Unauthenticated by design - it reports only a boolean, and a
    wrong-token answer is the same 'false' an absent token gets.
    """
    from fastapi.security import HTTPAuthorizationCredentials

    header = request.headers.get("authorization", "")
    credentials: HTTPAuthorizationCredentials | None = None
    if header.lower().startswith("bearer "):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=header.split(" ", 1)[1]
        )
    token = auth.extract_token(request, credentials)
    return PairCheckResponse(paired=auth.token_matches(token))


class DeviceRegisterRequest(BaseModel):
    label: str = ""


class DeviceRegisterResponse(BaseModel):
    ok: bool
    label: str


@router.post(
    "/api/devices",
    response_model=DeviceRegisterResponse,
    dependencies=[Depends(auth.require_auth)],
)
def register_device(
    payload: DeviceRegisterRequest, request: Request
) -> DeviceRegisterResponse:
    """Record a paired device so push notifications have somewhere to go.

    Push subscription details are attached in Phase 5; this records the device
    itself so the first briefing has a target list to work from.
    """
    from datetime import UTC, datetime

    from sqlmodel import select

    from app.db import session_scope
    from app.models import Device

    user_agent = (request.headers.get("user-agent") or "")[:400]
    label = payload.label.strip()[:80] or "Unnamed device"

    with session_scope() as session:
        existing = session.exec(select(Device).where(Device.user_agent == user_agent)).first()
        if existing:
            existing.last_seen_at = datetime.now(UTC)
            existing.label = label
            session.add(existing)
        else:
            session.add(Device(label=label, user_agent=user_agent))
    return DeviceRegisterResponse(ok=True, label=label)


def _b64(value: str) -> str:
    """Small helper kept for future push payload signing."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# conversation
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    voice: bool = False


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _load_history(session, conversation_id: int, limit: int) -> list[ChatMessage]:
    """The last N turns, oldest first.

    Tool rows are excluded: they are bookkeeping, and replaying them would both
    confuse the model and waste a free tier's tokens-per-minute budget.
    """
    from sqlmodel import select

    from app.models import Message, Role

    rows = session.exec(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .where(Message.role.in_([Role.USER, Role.ALFRED]))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    ).all()
    return [
        ChatMessage(role="assistant" if row.role == Role.ALFRED else "user", content=row.content)
        for row in reversed(rows)
    ]


@router.post("/api/chat", dependencies=[Depends(auth.require_auth)])
async def chat(payload: ChatRequest, settings: Settings = Depends(get_settings)):
    """Stream one reply as Server-Sent Events.

    SSE rather than a WebSocket because the traffic is one-directional once the
    turn starts, and SSE survives the phone locking and waking far better.
    """
    from datetime import UTC, datetime

    from app.db import session_scope
    from app.models import Conversation, InputMode, Message, Role

    text = payload.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Say something, sir.")
    if len(text) > 16000:
        raise HTTPException(status_code=413, detail="That is rather more than I can take in at once.")

    # Persist the user's turn and gather history before streaming starts, so a
    # dropped connection cannot lose what he actually said.
    with session_scope() as session:
        if payload.conversation_id:
            conversation = session.get(Conversation, payload.conversation_id)
            if conversation is None:
                raise HTTPException(status_code=404, detail="No such conversation.")
        else:
            conversation = Conversation(title=text[:60])
            session.add(conversation)
            session.flush()

        conversation.updated_at = datetime.now(UTC)
        conversation_id = conversation.id
        assert conversation_id is not None

        session.add(
            Message(
                conversation_id=conversation_id,
                role=Role.USER,
                content=text,
                input_mode=InputMode.VOICE if payload.voice else InputMode.TEXT,
            )
        )
        session.flush()
        history = _load_history(session, conversation_id, settings.history_turns)

    system = build_system_prompt(settings)
    router_ = Router(settings)

    async def generate():
        yield _sse({"type": "meta", "conversation_id": conversation_id})
        collected: list[str] = []
        failed = ""
        try:
            async for delta in router_.stream(system, history):
                collected.append(delta)
                yield _sse({"type": "delta", "text": delta})
        except Exception as exc:  # surfaced to the user below, never swallowed
            failed = friendly_error(exc)
            logger.error("Chat turn failed: %s", exc)
            yield _sse({"type": "error", "message": failed})

        answer = "".join(collected)
        if answer or failed:
            with session_scope() as session:
                session.add(
                    Message(
                        conversation_id=conversation_id,
                        role=Role.ALFRED,
                        content=answer or failed,
                        provider=router_.used,
                        model=router_.used_model,
                    )
                )
        if router_.used:
            yield _sse(
                {"type": "meta", "conversation_id": conversation_id,
                 "provider": router_.used, "model": router_.used_model}
            )
        yield _sse({"type": "done"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Without this a reverse proxy will buffer the whole stream and
            # deliver it as one lump, which looks exactly like a hang.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
