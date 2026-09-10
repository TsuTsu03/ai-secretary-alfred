"""Bearer-token authentication and device pairing.

Alfred is reachable from the phone, which rules out the "it only listens on
loopback, so it needs no auth" posture that a purely local tool can take.
Tailscale already restricts *who can reach the port* to Jansen's own devices;
this module restricts *what can talk to the API* once reachable, so that a
stray process, another user on a shared tailnet, or a browser page on the phone
cannot drive Alfred.

The token is generated on first run and stored under the data directory. The
laptop UI renders it as a QR code; the phone scans it once and stores it in
``localStorage``. Deleting ``secrets/auth_token`` rotates it and unpairs every
device.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# auto_error=False so a missing header produces our own 401 with a useful
# message rather than FastAPI's bare 403.
_scheme = HTTPBearer(auto_error=False)

# Routes that must work before a client has a token.
PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/manifest.webmanifest",
        "/sw.js",
        "/favicon.ico",
    }
)


def _expected_token(settings: Settings) -> str:
    return settings.read_or_create_auth_token()


def token_matches(presented: str, settings: Settings | None = None) -> bool:
    """Constant-time comparison against the stored token.

    ``compare_digest`` rather than ``==`` so that response timing does not leak
    how many leading characters of a guess were correct.
    """
    settings = settings or get_settings()
    expected = _expected_token(settings)
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.strip(), expected)


def extract_token(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str:
    """Pull the token from the Authorization header or the query string.

    The query-string form exists only for two cases the browser gives us no
    choice about: ``EventSource`` (which cannot set headers) and ``<audio
    src=...>``. Those requests are read-only. Tokens in URLs can land in logs,
    so access logging is disabled in ``main.run()``.
    """
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return request.query_params.get("token", "")


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_scheme),
) -> None:
    """FastAPI dependency guarding every non-public route."""
    settings = get_settings()
    if token_matches(extract_token(request, credentials), settings):
        return

    client = request.client.host if request.client else "unknown"
    logger.warning("Rejected unauthenticated %s %s from %s", request.method, request.url.path, client)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Alfred does not recognise this device. Pair it from the laptop first.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def pairing_url(settings: Settings | None = None) -> str:
    """The URL encoded into the QR code shown on the laptop.

    Carries the token in the fragment (``#``) rather than the query string: a
    fragment is never sent to the server and never appears in a server log, so
    the token reaches the phone's JavaScript without being written down along
    the way.
    """
    settings = settings or get_settings()
    return f"{settings.base_url}/#token={_expected_token(settings)}"
