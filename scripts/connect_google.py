"""Connect Alfred to Google Calendar and Gmail.

Interactive and one-time. Opens a browser, asks Google for consent, and stores
the refresh token in the data directory - never in the repo.

    .venv\\Scripts\\python.exe scripts\\connect_google.py

Not exposed as an HTTP route on purpose: a route that pops a browser window on
the laptop would be a peculiar thing for the phone to be able to trigger.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.integrations import google_oauth


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()

    if google_oauth.is_connected(settings):
        account = google_oauth.account_email(settings) or "(unknown)"
        print(f"Already connected as {account}.")
        print(f"To reconnect, delete {google_oauth.token_path(settings)} and run this again.")
        return 0

    if not google_oauth.has_client_secrets(settings):
        print("Google is not set up yet.\n")
        print("One-time, and free:")
        print("  1. Open https://console.cloud.google.com/apis/credentials")
        print("  2. Create a project (keep it on the free tier, no billing account).")
        print("  3. Enable the Google Calendar API and the Gmail API.")
        print("  4. Create an OAuth client ID, application type 'Desktop app'.")
        print("  5. Download the JSON and save it as:")
        print(f"       {google_oauth.client_secrets_path(settings)}")
        print("\nThen run this script again.")
        return 1

    print("Opening a browser for Google consent...\n")
    print("Alfred is asking for calendar access and gmail.modify.")
    print("He is NOT asking for permission to send mail - that scope is left out")
    print("deliberately, so he can draft replies but never send one.\n")

    try:
        account = google_oauth.connect(settings)
    except google_oauth.GoogleAuthError as exc:
        print(f"Failed: {exc}")
        return 1

    print(f"\nConnected as {account}.")
    print(f"Token stored at {google_oauth.token_path(settings)}")
    print("Restart Alfred and he will have your schedule.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
