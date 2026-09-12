"""Web Push, self-hosted with VAPID.

No push service account, no Firebase, no third party holding a device token.
VAPID keys are generated once into the data directory and identify this Alfred
to the browser's own push service. That keeps the cost at zero and the
dependency list at one library.

**iOS is the constraint that shapes everything here.** Safari only delivers Web
Push to a PWA that was installed to the home screen from a genuine HTTPS
origin. A briefing will never arrive on the iPhone over `http://100.x.x.x`, no
matter how correct this module is - `tailscale serve` is not optional for this
feature.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Browsers reject a payload much larger than this after encryption overhead.
MAX_PAYLOAD_BYTES = 3000


class PushError(RuntimeError):
    """Delivery failed. The message is safe to show the user."""


@dataclass(frozen=True)
class VapidKeys:
    public_key: str
    private_pem: str


def _keys_path(settings: Settings):
    return settings.secrets_dir / "vapid_private.pem"


def ensure_keys(settings: Settings | None = None) -> VapidKeys:
    """Generate the VAPID keypair once, then reuse it.

    Regenerating invalidates every existing subscription, because the browser
    ties a subscription to the key that created it. So this writes once and
    never overwrites.
    """
    from py_vapid import Vapid01

    settings = settings or get_settings()
    settings.ensure_dirs()
    path = _keys_path(settings)

    vapid = Vapid01()
    if path.is_file():
        vapid = Vapid01.from_file(str(path))
    else:
        vapid.generate_keys()
        vapid.save_key(str(path))
        logger.info("Generated a VAPID keypair at %s", path)

    return VapidKeys(public_key=_public_key_b64(vapid), private_pem=str(path))


def _public_key_b64(vapid) -> str:
    """The applicationServerKey the browser expects: raw P-256 point, base64url.

    ``py_vapid`` exposes this in a few shapes across versions, so derive it
    from the public numbers rather than trusting one accessor to exist.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec

    public = vapid.public_key
    numbers = public.public_numbers()
    raw = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    assert isinstance(numbers.curve, ec.SECP256R1)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def public_key(settings: Settings | None = None) -> str:
    return ensure_keys(settings).public_key


def _claims(settings: Settings) -> dict:
    """VAPID claims. `sub` must be a contact URI the push service can use."""
    return {"sub": f"mailto:{settings.user_name.lower()}@localhost"}


def send(subscription: dict, payload: dict, settings: Settings | None = None) -> bool:
    """Deliver one notification. Returns False if the subscription is dead.

    A 404 or 410 means the browser has discarded the subscription - the app was
    uninstalled, or the user cleared site data. That is not an error worth
    raising; it is a signal to forget the device.
    """
    from pywebpush import WebPushException, webpush

    settings = settings or get_settings()
    keys = ensure_keys(settings)

    body = json.dumps(payload, ensure_ascii=False)
    if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        payload = dict(payload)
        payload["body"] = payload.get("body", "")[:800] + "…"
        body = json.dumps(payload, ensure_ascii=False)

    try:
        webpush(
            subscription_info=subscription,
            data=body,
            vapid_private_key=keys.private_pem,
            vapid_claims=_claims(settings),
            timeout=20,
        )
        return True
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            logger.info("Subscription is gone (%s); dropping it.", status)
            return False
        raise PushError(f"Push failed ({status}): {exc}") from exc
    except Exception as exc:
        raise PushError(f"Push failed: {exc}") from exc


def send_to_all(payload: dict, settings: Settings | None = None) -> tuple[int, int]:
    """Push to every subscribed device. Returns (delivered, dropped)."""
    from sqlmodel import select

    from app.db import session_scope
    from app.models import Device

    settings = settings or get_settings()
    delivered = 0
    dropped = 0

    with session_scope(settings) as session:
        devices = session.exec(select(Device).where(Device.push_endpoint != "")).all()
        for device in devices:
            subscription = {
                "endpoint": device.push_endpoint,
                "keys": {"p256dh": device.push_p256dh, "auth": device.push_auth},
            }
            try:
                alive = send(subscription, payload, settings)
            except PushError as exc:
                logger.warning("Could not push to %s: %s", device.label, exc)
                continue
            if alive:
                delivered += 1
            else:
                # Forget the endpoint but keep the device row, so the same
                # browser can resubscribe without creating a duplicate.
                device.push_endpoint = ""
                device.push_p256dh = ""
                device.push_auth = ""
                session.add(device)
                dropped += 1

    return delivered, dropped


def describe(settings: Settings | None = None) -> dict:
    from sqlmodel import select

    from app.db import session_scope
    from app.models import Device

    settings = settings or get_settings()
    try:
        key = public_key(settings)
    except Exception as exc:
        return {"ready": False, "detail": str(exc), "subscribers": 0}

    with session_scope(settings) as session:
        count = len(session.exec(select(Device).where(Device.push_endpoint != "")).all())

    return {
        "ready": True,
        "public_key": key,
        "subscribers": count,
        # Said plainly because it is the single most common reason a briefing
        # never arrives, and it looks like a bug rather than a requirement.
        "ios_requires_https_pwa": True,
    }
