"""Tests for the file-access boundary.

These are the highest-value tests in the project. Alfred reads content written
by other people - email bodies, documents, calendar invites - and a tool
argument derived from that content is attacker-influenced. If the guard in
``app.security.paths`` is wrong, everything downstream is wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Settings
from app.security.paths import (
    PathAccessError,
    is_denied_name,
    is_denied_path,
    resolve_readable,
    resolve_writable,
    walk_roots,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A small tree with the shapes the guard has to get right."""
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)

    (root / "app" / "main.py").write_text("print('hi')", encoding="utf-8")
    (root / "README.md").write_text("# readme", encoding="utf-8")
    (root / ".env").write_text("SECRET=hunter2", encoding="utf-8")
    (root / "app" / "id_rsa").write_text("-----BEGIN-----", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("//", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("not yours", encoding="utf-8")
    return root


@pytest.fixture
def settings(workspace: Path, tmp_path: Path) -> Settings:
    return Settings(
        ALFRED_FILE_ROOTS=str(workspace),
        ALFRED_DATA_DIR=str(tmp_path / "data"),
    )  # type: ignore[call-arg]


# ── the happy path ───────────────────────────────────────────────────────


def test_reads_a_file_inside_a_root(workspace: Path, settings: Settings) -> None:
    resolved = resolve_readable(workspace / "app" / "main.py", settings)
    assert resolved.path == (workspace / "app" / "main.py").resolve()
    assert resolved.relative == os.path.join("app", "main.py")


def test_relative_is_used_for_citation(workspace: Path, settings: Settings) -> None:
    assert resolve_readable(workspace / "README.md", settings).relative == "README.md"


# ── escaping a root ──────────────────────────────────────────────────────


def test_rejects_a_path_outside_every_root(tmp_path: Path, settings: Settings) -> None:
    with pytest.raises(PathAccessError):
        resolve_readable(tmp_path / "outside" / "private.md", settings)


def test_rejects_parent_traversal(workspace: Path, settings: Settings) -> None:
    """The classic. Resolution happens before the allowlist check."""
    with pytest.raises(PathAccessError):
        resolve_readable(workspace / ".." / "outside" / "private.md", settings)


def test_rejects_deep_traversal(workspace: Path, settings: Settings) -> None:
    with pytest.raises(PathAccessError):
        resolve_readable(str(workspace / "app") + "/../../../../../../etc/passwd", settings)


def test_rejects_absolute_system_path(settings: Settings) -> None:
    target = "C:/Windows/System32/config/SAM" if os.name == "nt" else "/etc/shadow"
    with pytest.raises(PathAccessError):
        resolve_readable(target, settings)


def test_rejects_nul_byte(workspace: Path, settings: Settings) -> None:
    with pytest.raises(PathAccessError):
        resolve_readable(str(workspace / "README.md") + "\x00.png", settings)


# ── the denylist, inside an allowed root ─────────────────────────────────


@pytest.mark.parametrize(
    "relative",
    [".env", "app/id_rsa", ".git/config", "node_modules/pkg/index.js"],
)
def test_denylist_applies_inside_an_allowed_root(
    workspace: Path, settings: Settings, relative: str
) -> None:
    """Being under an allowed root does not make a credential readable."""
    with pytest.raises(PathAccessError):
        resolve_readable(workspace / relative, settings)


@pytest.mark.parametrize(
    "name",
    [
        ".env", ".env.local", "prod.env", "server.key", "cert.pem", "id_rsa",
        "id_ed25519.pub", "aws-credentials.json", "my-secret.txt", "token.json",
        "vault.kdbx", ".git", "node_modules", ".venv", ".ssh", ".claude",
    ],
)
def test_denied_names(name: str) -> None:
    assert is_denied_name(name)


@pytest.mark.parametrize("name", ["main.py", "README.md", "notes.txt", "environment.md"])
def test_allowed_names(name: str) -> None:
    assert not is_denied_name(name)


def test_denial_checks_every_component_not_just_the_last() -> None:
    """``project/.git/config`` is denied by ``.git``, not by ``config``."""
    assert is_denied_path(Path("project/.git/config"))
    assert is_denied_path(Path("a/node_modules/b/index.js"))
    assert not is_denied_path(Path("app/api/routes.py"))


# ── writes ───────────────────────────────────────────────────────────────


def test_write_target_may_not_exist_yet(workspace: Path, settings: Settings) -> None:
    resolved = resolve_writable(workspace / "app" / "new_file.md", settings)
    assert not resolved.path.exists()


def test_write_refuses_a_directory(workspace: Path, settings: Settings) -> None:
    with pytest.raises(PathAccessError):
        resolve_writable(workspace / "app", settings)


def test_write_obeys_the_same_denylist(workspace: Path, settings: Settings) -> None:
    with pytest.raises(PathAccessError):
        resolve_writable(workspace / ".env", settings)


# ── walking ──────────────────────────────────────────────────────────────


def test_walk_skips_denied_trees_and_files(workspace: Path, settings: Settings) -> None:
    found = {r.relative.replace("\\", "/") for r in walk_roots(settings)}
    assert "app/main.py" in found
    assert "README.md" in found
    assert not any(part in name for name in found for part in (".git", "node_modules", ".env"))


def test_walk_skips_files_over_the_size_cap(workspace: Path, tmp_path: Path) -> None:
    (workspace / "huge.md").write_text("x" * 5000, encoding="utf-8")
    settings = Settings(
        ALFRED_FILE_ROOTS=str(workspace),
        ALFRED_DATA_DIR=str(tmp_path / "data"),
        ALFRED_MAX_FILE_READ_BYTES=1000,
    )  # type: ignore[call-arg]
    found = {r.relative for r in walk_roots(settings)}
    assert "huge.md" not in found


def test_no_configured_roots_is_an_error_not_a_free_for_all(tmp_path: Path) -> None:
    settings = Settings(
        ALFRED_FILE_ROOTS=str(tmp_path / "does-not-exist"),
        ALFRED_DATA_DIR=str(tmp_path / "data"),
    )  # type: ignore[call-arg]
    with pytest.raises(PathAccessError):
        resolve_readable(tmp_path / "anything.md", settings)
