"""Test isolation.

``Settings`` reads ``.env`` by design, which means that the moment a real
``.env`` exists on the developer's machine the tests stop testing defaults and
start testing *that machine's configuration*. Two tests caught this the day a
Gemini key was added: they asserted "no providers configured" and got one.

Detaching the env file for the whole session makes the suite deterministic and,
more importantly, keeps a real API key from reaching a test run at all.
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings


@pytest.fixture(autouse=True, scope="session")
def _detach_dotenv() -> None:
    Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Also drop any provider keys exported into the environment itself."""
    for name in os.environ:
        if name.endswith("_API_KEY") or name.startswith("ALFRED_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fresh_database() -> None:
    """Give every test its own SQLite engine.

    ``app.db`` caches the engine in a module global, so without this a test
    that builds Settings pointing at its own tmp_path still talks to whichever
    database the *first* test happened to open. The symptom is order-dependent
    failures that vanish when a test is run alone - "latest briefing is None"
    failing because a previous test stored one.
    """
    from app.db import reset_engine

    reset_engine()
    yield
    reset_engine()
