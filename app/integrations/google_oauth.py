"""Google OAuth for Calendar and Gmail.

Scopes are the narrowest that do the job:

* Calendar gets read/write, because moving a meeting is half the point of a
  secretary. Every write still goes through the confirmation gate.
* Gmail gets ``gmail.modify``, which covers reading, labelling, and drafting.

**On sending.** ``gmail.modify`` also permits ``users.messages.send``, and there
is no Gmail scope that allows drafting while forbidding sending -
``gmail.compose`` permits send too. So the fact that Alfred never sends mail is
a property of *this application* (no send tool is registered, and drafting is
gated behind a confirmation card), not a property of the token. That is a
weaker guarantee than a scope restriction would be, and it is recorded here
plainly rather than overstated: an earlier version of this file claimed the
token made sending impossible, which was simply wrong.

If you want the stronger, token-level guarantee, switch ``gmail.modify`` to
``gmail.readonly`` below and drop the draft tool. Alfred then cannot draft
either - that is the trade Google's scope model forces.

The refresh token lives in the data directory, never in the repo. It is the
most valuable secret Alfred holds: it opens Jansen's mail.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    # Covers read, label, and draft. Note that it also covers send - see the
    # module docstring. Use gmail.readonly instead if you want sending to be
    # impossible at the token level, and accept losing drafts.
    "https://www.googleapis.com/auth/gmail.modify",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class GoogleAuthError(RuntimeError):
    """Google is not connected, or the connection has broken."""


def client_secrets_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.secrets_dir / "google_client.json"


def token_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.secrets_dir / "google_token.json"


def has_client_secrets(settings: Settings | None = None) -> bool:
    return client_secrets_path(settings).is_file()


def is_connected(settings: Settings | None = None) -> bool:
    return token_path(settings).is_file()


def _load_credentials(settings: Settings):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(settings)
    if not path.is_file():
        raise GoogleAuthError(
            "Google is not connected. Run "
            "`.venv\\Scripts\\python.exe scripts\\connect_google.py` once to authorise."
        )

    try:
        credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (OSError, ValueError) as exc:
        raise GoogleAuthError(f"The stored Google token is unreadable: {exc}") from exc

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise GoogleAuthError(
                f"Could not refresh the Google token ({exc}). Re-run connect_google.py."
            ) from exc
        # Persist the refreshed access token so the next call does not repeat
        # the round trip.
        path.write_text(credentials.to_json(), encoding="utf-8")
        return credentials

    raise GoogleAuthError("The Google token is invalid. Re-run connect_google.py.")


def service(name: str, version: str, settings: Settings | None = None):
    """Build an authorised Google API client.

    ``cache_discovery=False`` because the default file cache warns noisily on
    every call and writes into the working directory.
    """
    from googleapiclient.discovery import build

    settings = settings or get_settings()
    credentials = _load_credentials(settings)
    return build(name, version, credentials=credentials, cache_discovery=False)


def connect(settings: Settings | None = None, open_browser: bool = True) -> str:
    """Run the installed-app OAuth flow. Returns the connected account.

    Interactive by design, and therefore not reachable from the HTTP API - a
    route that pops a browser window on the server would be a strange thing for
    the phone to trigger.

    ``open_browser=False`` prints the authorisation URL instead of launching a
    browser, for when Alfred runs somewhere without one - or when the browser
    that would open is not the one you are signed into.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    settings = settings or get_settings()
    settings.ensure_dirs()

    secrets = client_secrets_path(settings)
    if not secrets.is_file():
        raise GoogleAuthError(
            f"No OAuth client credentials at {secrets}.\n\n"
            "Create them once, free:\n"
            "  1. https://console.cloud.google.com/apis/credentials\n"
            "  2. Enable the Google Calendar API and the Gmail API.\n"
            "  3. Create an OAuth client ID of type 'Desktop app'.\n"
            "  4. Download the JSON and save it at the path above.\n\n"
            "Keep the project on the free tier with no billing account attached."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    # port=0 lets the OS pick a free loopback port for the redirect.
    credentials = flow.run_local_server(
        port=0,
        prompt="consent",
        open_browser=open_browser,
        authorization_prompt_message=(
            "Open this URL to authorise Alfred:\n\n{url}\n" if not open_browser else
            "Opening a browser to authorise Alfred. If it does not appear, open:\n\n{url}\n"
        ),
    )

    destination = token_path(settings)
    destination.write_text(credentials.to_json(), encoding="utf-8")
    logger.info("Google connected; token stored at %s", destination)

    return account_email(settings) or "(unknown account)"


def account_email(settings: Settings | None = None) -> str:
    """Which account is connected. Shown in the status rail."""
    settings = settings or get_settings()
    path = token_path(settings)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if data.get("account"):
        return str(data["account"])
    try:
        profile = service("gmail", "v1", settings).users().getProfile(userId="me").execute()
        return str(profile.get("emailAddress", ""))
    except Exception:
        return ""


def disconnect(settings: Settings | None = None) -> bool:
    """Forget the token. The grant itself is revoked in the Google account."""
    path = token_path(settings)
    if path.is_file():
        path.unlink()
        return True
    return False


def describe(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    scopes_allow_send = any(
        scope.endswith(("gmail.modify", "gmail.compose", "gmail.send")) for scope in SCOPES
    )
    return {
        "configured": has_client_secrets(settings),
        "connected": is_connected(settings),
        # Honest split: what the token permits, versus what Alfred actually
        # offers. Reporting a single "can_send_mail: False" hid the difference
        # and overstated the guarantee.
        "scope_allows_send": scopes_allow_send,
        "send_tool_registered": False,
        "scopes": SCOPES,
    }
