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

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.agent import Agent
from app.config import Settings, get_settings
from app.llm.base import ChatMessage
from app.llm.router import friendly_error
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
    voice_out: dict
    voice_in: dict
    google: dict
    tools: list[str]
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
        voice_out=_voice_out_status(settings),
        voice_in=_voice_in_status(settings),
        google=_google_status(settings),
        tools=_tool_names(),
        roots=roots,
        base_url=settings.base_url,
        tailscale_configured=bool(settings.tailscale_hostname),
    )


def _google_status(settings: Settings) -> dict:
    """Whether the calendar and mailbox are reachable.

    Reports the connected account but never the token, and states plainly that
    sending is not possible - that is a property of the granted scopes, not a
    promise this code makes.
    """
    try:
        from app.integrations import google_oauth

        status = google_oauth.describe(settings)
        status["account"] = (
            google_oauth.account_email(settings) if status["connected"] else ""
        )
        return status
    except Exception as exc:
        return {"configured": False, "connected": False, "detail": str(exc)}


def _tool_names() -> list[str]:
    from app.tools import registry as tool_registry

    return sorted(tool.spec.name for tool in tool_registry.all_tools())


def _voice_out_status(settings: Settings) -> dict:
    """Whether Alfred can speak, without loading the model to find out."""
    try:
        from app.voice import tts

        return tts.describe(settings)
    except Exception as exc:  # a missing optional dependency must not 500
        return {"engine": settings.tts_engine, "ready": False, "detail": str(exc)}


def _voice_in_status(settings: Settings) -> dict:
    """Whether Alfred can hear. Reports the chosen model and device only -
    probing is cheap, loading the model is not, so this never loads it."""
    try:
        from app.media import ffmpeg
        from app.voice import stt

        info = stt.describe(settings)
        info["ffmpeg"] = ffmpeg.ffmpeg_available()
        if not info["ffmpeg"]:
            info["ready"] = False
            info["detail"] = "FFmpeg is not installed."
        return info
    except Exception as exc:
        return {"ready": False, "detail": str(exc)}


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
# voice
# ---------------------------------------------------------------------------


class TranscribeResponse(BaseModel):
    text: str
    language: str
    duration_seconds: float
    elapsed_seconds: float
    model: str
    device: str
    empty: bool


@router.post(
    "/api/voice/transcribe",
    response_model=TranscribeResponse,
    dependencies=[Depends(auth.require_auth)],
)
async def transcribe_voice(
    audio: UploadFile = File(...), settings: Settings = Depends(get_settings)
) -> TranscribeResponse:
    """Turn a recorded clip into text.

    The browser decides the container: Safari sends ``audio/mp4`` and Chrome
    sends ``audio/webm``. Rather than special-case either, everything goes
    through FFmpeg and comes out as the 16 kHz mono WAV Whisper wants.
    """
    import uuid

    from starlette.concurrency import run_in_threadpool

    from app.media import ffmpeg
    from app.voice import stt

    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The recording was empty.")
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"That clip is larger than the {settings.max_upload_mb} MB limit.",
        )

    if not ffmpeg.ffmpeg_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "FFmpeg is not installed, so I cannot decode the recording. "
                "Install it with:  winget install BtbN.FFmpeg.GPL.8.0"
            ),
        )

    settings.ensure_dirs()
    token = uuid.uuid4().hex
    # Extension is ignored by FFmpeg (it sniffs the container), but keeping the
    # upload's own suffix out of the filename means a hostile name cannot reach
    # the filesystem at all.
    source = settings.tmp_dir / f"{token}.upload"
    target = settings.tmp_dir / f"{token}.wav"

    try:
        source.write_bytes(raw)
        await run_in_threadpool(ffmpeg.normalize_for_transcription, source, target)
        transcript = await run_in_threadpool(stt.transcribe, target, settings)
    except ffmpeg.FFmpegError as exc:
        logger.warning("Could not decode an uploaded clip: %s", exc)
        raise HTTPException(status_code=400, detail="I could not decode that recording.") from exc
    except stt.TranscriptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)

    return TranscribeResponse(
        text=transcript.text,
        language=transcript.language,
        duration_seconds=transcript.duration_seconds,
        elapsed_seconds=transcript.elapsed_seconds,
        model=transcript.model,
        device=transcript.device,
        empty=transcript.is_empty,
    )


class SpeakRequest(BaseModel):
    text: str


@router.post("/api/voice/speak", dependencies=[Depends(auth.require_auth)])
async def speak(payload: SpeakRequest, settings: Settings = Depends(get_settings)) -> Response:
    """Return Alfred's reply as spoken WAV audio.

    POST rather than GET so the token travels in a header. An ``<audio src>``
    would have forced it into the query string, where it would end up in
    history and any intermediary's logs.
    """
    from starlette.concurrency import run_in_threadpool

    from app.voice import tts

    spoken = payload.text.strip()
    if not spoken:
        raise HTTPException(status_code=400, detail="There is nothing to say.")
    # Long replies are chunked by the client; this is a backstop against a
    # runaway generation turning into a minutes-long synthesis job.
    if len(spoken) > 4000:
        spoken = spoken[:4000]

    try:
        speech = await run_in_threadpool(tts.synthesize, spoken, settings)
    except tts.TTSUnavailable as exc:
        # 503 tells the client to fall back to the browser's own voice rather
        # than to give up on speaking.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return Response(
        content=speech.audio_wav,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Alfred-Voice": speech.voice,
        },
    )


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
    agent = Agent(settings)

    async def generate():
        yield _sse({"type": "meta", "conversation_id": conversation_id})
        collected: list[str] = []
        failed = ""
        seen_steps = 0
        try:
            async for delta in agent.run(system, history, conversation_id):
                # Surface tool activity as it happens; a silent ten-second
                # pause while Alfred reads files looks like a hang.
                while seen_steps < len(agent.outcome.steps):
                    step = agent.outcome.steps[seen_steps]
                    seen_steps += 1
                    yield _sse({
                        "type": "tool",
                        "name": step.tool,
                        "arguments": step.arguments,
                        "queued": step.queued,
                    })
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
                        provider=agent.outcome.provider,
                        model=agent.outcome.model,
                    )
                )
        while seen_steps < len(agent.outcome.steps):
            step = agent.outcome.steps[seen_steps]
            seen_steps += 1
            yield _sse({
                "type": "tool", "name": step.tool,
                "arguments": step.arguments, "queued": step.queued,
            })
        if agent.outcome.provider:
            yield _sse(
                {"type": "meta", "conversation_id": conversation_id,
                 "provider": agent.outcome.provider, "model": agent.outcome.model}
            )
        # Any card raised this turn, so the UI can render it immediately.
        for action in _pending_for(conversation_id):
            yield _sse({"type": "pending", **action})
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


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


class IndexStatusResponse(BaseModel):
    files: int
    chunks: int
    semantic: bool
    progress: dict


@router.get(
    "/api/index",
    response_model=IndexStatusResponse,
    dependencies=[Depends(auth.require_auth)],
)
def index_status() -> IndexStatusResponse:
    from app.db import session_scope
    from app.indexer import scanner, store

    with session_scope() as session:
        counts = store.stats(session)
    return IndexStatusResponse(**counts, progress=scanner.progress().as_dict())


class ReindexRequest(BaseModel):
    rebuild: bool = False


class ReindexResponse(BaseModel):
    started: bool
    detail: str


@router.post(
    "/api/index/rebuild",
    response_model=ReindexResponse,
    dependencies=[Depends(auth.require_auth)],
)
def start_index(
    payload: ReindexRequest, settings: Settings = Depends(get_settings)
) -> ReindexResponse:
    """Kick off an indexing run on a background thread.

    Read-only with respect to Jansen's files - it only ever opens them - so it
    needs no confirmation.
    """
    from app.indexer import scanner

    started = scanner.index_in_background(settings, rebuild=payload.rebuild)
    return ReindexResponse(
        started=started,
        detail="Indexing started." if started else "An indexing run is already going.",
    )


# ---------------------------------------------------------------------------
# pending actions
# ---------------------------------------------------------------------------


def _pending_for(conversation_id: int) -> list[dict]:
    from sqlmodel import select

    from app.db import session_scope
    from app.models import ActionStatus, PendingAction

    with session_scope() as session:
        rows = session.exec(
            select(PendingAction)
            .where(PendingAction.conversation_id == conversation_id)
            .where(PendingAction.status == ActionStatus.PENDING)
            .order_by(PendingAction.created_at)
        ).all()
        return [
            {
                "id": row.id,
                "tool": row.tool_name,
                "summary": row.summary,
                "detail": row.detail,
            }
            for row in rows
        ]


class PendingListResponse(BaseModel):
    actions: list[dict]


@router.get(
    "/api/actions",
    response_model=PendingListResponse,
    dependencies=[Depends(auth.require_auth)],
)
def list_actions() -> PendingListResponse:
    from sqlmodel import select

    from app.db import session_scope
    from app.models import ActionStatus, PendingAction

    with session_scope() as session:
        rows = session.exec(
            select(PendingAction)
            .where(PendingAction.status == ActionStatus.PENDING)
            .order_by(PendingAction.created_at)
        ).all()
        return PendingListResponse(
            actions=[
                {
                    "id": row.id,
                    "tool": row.tool_name,
                    "summary": row.summary,
                    "detail": row.detail,
                    "conversation_id": row.conversation_id,
                }
                for row in rows
            ]
        )


class DecisionRequest(BaseModel):
    approve: bool


class DecisionResponse(BaseModel):
    ok: bool
    status: str
    result: str


@router.post(
    "/api/actions/{action_id}",
    response_model=DecisionResponse,
    dependencies=[Depends(auth.require_auth)],
)
async def decide_action(
    action_id: int, payload: DecisionRequest, settings: Settings = Depends(get_settings)
) -> DecisionResponse:
    """Approve or decline a queued action.

    Approval runs the arguments stored on the row, not anything the model says
    afterwards, so what Jansen saw on the card is exactly what executes.
    """
    from datetime import UTC, datetime

    from starlette.concurrency import run_in_threadpool

    from app.db import session_scope
    from app.models import ActionStatus, PendingAction
    from app.tools import registry as tool_registry

    with session_scope() as session:
        action = session.get(PendingAction, action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="No such action.")
        if action.status != ActionStatus.PENDING:
            raise HTTPException(
                status_code=409, detail=f"That action is already {action.status.value}."
            )
        if not payload.approve:
            action.status = ActionStatus.DECLINED
            action.resolved_at = datetime.now(UTC)
            session.add(action)
            return DecisionResponse(ok=True, status="declined", result="Very good, sir.")
        action.status = ActionStatus.APPROVED
        session.add(action)

    ok, result = await run_in_threadpool(tool_registry.execute_approved, action_id, settings)
    return DecisionResponse(ok=ok, status="executed" if ok else "failed", result=result)
