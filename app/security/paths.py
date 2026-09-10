"""The boundary around Alfred's file access.

Every path Alfred touches passes through :func:`resolve_readable` or
:func:`resolve_writable`. Nothing else in the codebase is permitted to open a
file from a model-supplied path.

The threat model is not only "the model makes a mistake". Alfred reads email
and documents written by other people, so a tool argument can be attacker
influenced. The guard therefore assumes every incoming path is hostile:

* Paths are resolved *before* the allowlist check, so ``..`` traversal,
  symlinks, and Windows short names (``PROGRA~1``) cannot escape a root.
* The denylist applies inside allowed roots too - being under
  ``C:/CodingProjects`` does not make ``.env`` readable.
* Failures raise :class:`PathAccessError` with a message safe to show a user;
  they never fall through to a permissive default.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings

# Directory names that are never worth indexing and frequently hold secrets or
# thousands of machine-generated files.
DENIED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".next",
        ".nuxt",
        ".turbo",
        "dist",
        "build",
        "target",
        ".gradle",
        ".idea",
        ".vscode",
        ".claude",
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        "AppData",
        "Application Data",
        "$RECYCLE.BIN",
        "System Volume Information",
    }
)

# Filename patterns that are denied wherever they appear. Credentials do not
# become safe by sitting inside an allowed project folder.
DENIED_NAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    "*.key",
    "*.pem",
    "*.pfx",
    "*.p12",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.ppk",
    "token.json",
    "*.token",
    "service-account*.json",
    "*.kdbx",
    "*.keychain",
    "shadow",
    "*.sqlite-wal",
    "*.sqlite-shm",
)

# Words that mark a file as sensitive, matched as whole tokens rather than as
# substrings.
#
# A plain ``*secret*`` glob looks equivalent and is not: it also denies
# "secretary", "secretariat", and "credentialing". Alfred's own project folder
# is called Alfred-AI-Secretary, and that glob quietly excluded it from his own
# index - a denial with no error message and no obvious symptom, just an
# assistant that could not find a thing it had written. Token boundaries keep
# the protection and drop the false positives.
SENSITIVE_WORD = re.compile(
    r"(?:^|[^a-z0-9])(secret|secrets|credential|credentials|passwd|password|passwords|apikey|api_key)(?:[^a-z0-9]|$)"
)

# Machine-generated files. Not a security matter - these are simply worthless
# to index and ruinously expensive to embed.
#
# A single package-lock.json is a few hundred chunks of nothing anybody will
# ever ask about, and there is one in most project folders. Left in, they
# dominated the index: the first twenty seconds of a real run covered five
# files, all of them lockfiles. Excluding them is the difference between an
# index that finishes in minutes and one that finishes in hours.
GENERATED_PATTERNS = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "*.pyc",
    "*.pot",
    "*.mo",
    "requirements.lock",
)


def is_generated_name(name: str) -> bool:
    """True for machine-generated files that are not worth indexing."""
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in GENERATED_PATTERNS)


# Only text-ish files are read. This is a readability limit, not a security
# one - the denylist above is what keeps secrets out.
READABLE_SUFFIXES = frozenset(
    {
        ".txt", ".md", ".markdown", ".rst", ".org",
        ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
        ".java", ".kt", ".kts", ".swift", ".go", ".rs", ".rb", ".php",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".m", ".mm", ".dart", ".lua",
        ".sh", ".ps1", ".bat", ".sql", ".graphql", ".proto",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".xml", ".svg",
        ".csv", ".tsv", ".log", ".gitignore", ".dockerignore", ".editorconfig",
    }
)


class PathAccessError(PermissionError):
    """A path was rejected. The message is safe to show the user."""


@dataclass(frozen=True)
class ResolvedPath:
    """A path that has passed every check, with the root it belongs to."""

    path: Path
    root: Path

    @property
    def relative(self) -> str:
        """Path relative to its root - what Alfred should cite back to the user."""
        return str(self.path.relative_to(self.root))

    @property
    def display(self) -> str:
        return str(self.path)


def _roots(settings: Settings | None) -> tuple[Path, ...]:
    settings = settings or get_settings()
    roots = settings.file_roots
    if not roots:
        raise PathAccessError(
            "No readable folders are configured. Set ALFRED_FILE_ROOTS to a list of "
            "directories separated by the platform path separator."
        )
    return roots


def is_denied_name(name: str) -> bool:
    """True if this bare file or directory name is denied anywhere."""
    lowered = name.lower()
    if name in DENIED_DIR_NAMES or lowered in {d.lower() for d in DENIED_DIR_NAMES}:
        return True
    if any(fnmatch.fnmatch(lowered, pattern) for pattern in DENIED_NAME_PATTERNS):
        return True
    return bool(SENSITIVE_WORD.search(lowered))


def is_denied_path(path: Path) -> bool:
    """True if any component of the path is denied.

    Checking every component - not just the final name - is what stops
    ``project/.git/config`` and ``project/node_modules/pkg/.env``.
    """
    return any(is_denied_name(part) for part in path.parts)


def _resolve_strictly(raw: str | Path) -> Path:
    """Resolve a user-supplied path without trusting any part of it."""
    try:
        candidate = Path(raw).expanduser()
    except (RuntimeError, ValueError) as exc:
        raise PathAccessError(f"That path could not be understood: {raw!r}") from exc

    # A path with a NUL byte can truncate inside the OS layer.
    if "\x00" in str(candidate):
        raise PathAccessError("That path contains an illegal character.")

    try:
        # strict=False so a not-yet-existing write target still normalizes.
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathAccessError(f"That path could not be resolved: {raw!r}") from exc


def _containing_root(resolved: Path, roots: tuple[Path, ...]) -> Path:
    for root in roots:
        # is_relative_to compares the *resolved* forms, so symlinks and ".."
        # have already been collapsed and cannot smuggle a path out of a root.
        if resolved == root or resolved.is_relative_to(root):
            return root
    raise PathAccessError(
        f"{resolved} is outside the folders Alfred is allowed to read. "
        f"Allowed roots: {', '.join(str(r) for r in roots)}"
    )


def resolve_readable(raw: str | Path, settings: Settings | None = None) -> ResolvedPath:
    """Resolve a path Alfred may read, or raise :class:`PathAccessError`."""
    roots = _roots(settings)
    resolved = _resolve_strictly(raw)
    root = _containing_root(resolved, roots)

    # Only check the portion below the root: a root may itself legitimately sit
    # under a directory whose name is on the denylist (e.g. a folder in AppData
    # that the user explicitly configured).
    relative = resolved.relative_to(root)
    if is_denied_path(relative):
        raise PathAccessError(
            f"{resolved.name} is on Alfred's permanent denylist "
            f"(credentials, keys, VCS metadata, and dependency folders are never read)."
        )
    return ResolvedPath(path=resolved, root=root)


def resolve_writable(raw: str | Path, settings: Settings | None = None) -> ResolvedPath:
    """Resolve a path Alfred may write to.

    Identical to :func:`resolve_readable`, plus a refusal to overwrite anything
    that is not a regular file. This does **not** authorize the write: writes
    are still gated on an approved PendingAction. This only ensures that an
    approved write cannot land somewhere unexpected.
    """
    resolved = resolve_readable(raw, settings)
    if resolved.path.exists() and not resolved.path.is_file():
        raise PathAccessError(f"{resolved.path} exists and is not a regular file.")
    return resolved


def is_readable_file(path: Path, settings: Settings | None = None) -> bool:
    """Cheap check used by the indexer while walking directories."""
    if path.suffix.lower() not in READABLE_SUFFIXES:
        return False
    if is_generated_name(path.name):
        return False
    settings = settings or get_settings()
    try:
        return path.is_file() and path.stat().st_size <= settings.max_file_read_bytes
    except OSError:
        return False


def walk_roots(settings: Settings | None = None):
    """Yield every readable file under every allowed root.

    Prunes denied directories in place so the walk never descends into
    ``node_modules`` or ``.git`` - which is the difference between indexing a
    few thousand files and a few million.
    """
    settings = settings or get_settings()
    for root in _roots(settings):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not is_denied_name(d)]
            current = Path(dirpath)
            for filename in filenames:
                if is_denied_name(filename):
                    continue
                candidate = current / filename
                if is_readable_file(candidate, settings):
                    yield ResolvedPath(path=candidate, root=root)
