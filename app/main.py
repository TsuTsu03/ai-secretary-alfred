"""FastAPI application entry point.

Security posture for an app that is reachable from a second device:

* :class:`~app.config.Settings` refuses to bind anywhere but loopback or a
  Tailscale CGNAT address, so a misconfiguration cannot put Alfred on a
  publicly routable interface.
* Every route except a small public set requires a bearer token.
* A Host-header allowlist blocks DNS-rebinding, where a malicious page resolves
  its own domain to Alfred's address and then talks to this API from the
  browser. This matters more than in a loopback-only app because Alfred also
  answers on a stable ``*.ts.net`` name.
* CORS is closed and a strict CSP blocks any exfiltration path from content
  Alfred has read out of a file or an email.
"""

from __future__ import annotations

import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import router as api_router
from app.config import TAILSCALE_CGNAT, get_settings
from app.db import init_db
from app.logging_setup import redact, setup_logging

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

LOOPBACK_HOST_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _host_allowed(host: str) -> bool:
    """True if this Host header is one Alfred legitimately answers to.

    Three shapes are valid: loopback, the Tailscale MagicDNS name configured in
    settings, and a raw Tailscale CGNAT address. Anything else is a rebinding
    attempt or a proxy pointed somewhere it should not be.
    """
    if not host:
        return True  # HTTP/1.0 clients and some health checks send no Host.
    if host in LOOPBACK_HOST_NAMES:
        return True

    settings = get_settings()
    configured = settings.tailscale_hostname.strip().lower()
    if configured and host == configured:
        return True
    # MagicDNS short name for the same machine.
    if configured and host == configured.split(".")[0]:
        return True
    if host.endswith(".ts.net"):
        return True

    try:
        return ipaddress.ip_address(host) in TAILSCALE_CGNAT
    except ValueError:
        return False


class NetworkGuardMiddleware(BaseHTTPMiddleware):
    """Rejects requests that did not originate from one of Jansen's devices."""

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        # IPv6 literals arrive bracketed; normalize before comparing.
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not _host_allowed(host):
            logger.warning("Blocked request with unexpected Host header: %r", host[:80])
            return JSONResponse(
                status_code=421,
                content={"detail": "Alfred does not answer to that hostname."},
            )

        origin = (request.headers.get("origin") or "").strip().lower()
        if origin:
            origin_host = origin.split("//", 1)[-1].split("/")[0].split(":")[0]
            if origin_host.startswith("[") and origin_host.endswith("]"):
                origin_host = origin_host[1:-1]
            if not _host_allowed(origin_host):
                logger.warning("Blocked cross-origin request from %r", origin[:120])
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-origin requests are not allowed."},
                )

        # A cross-site form post arrives with Sec-Fetch-Site: cross-site. This is
        # the modern header-based CSRF defence and costs nothing.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site and fetch_site not in ("same-origin", "same-site", "none"):
                logger.warning(
                    "Blocked %s request with Sec-Fetch-Site: %s", request.method, fetch_site
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-site requests are not allowed."},
                )

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # No remote assets are used, so a strict CSP costs nothing and closes
        # the exfiltration path from file or email content Alfred has read.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings)
    settings.ensure_dirs()
    init_db(settings)
    # Force the token to exist at startup so the pairing screen never races it,
    # and show it: pairing the *first* device has no other channel.
    #
    # print(), not logger. The log file is rotated and kept on disk, and the
    # redaction filter covers provider keys, not this token.
    token = settings.read_or_create_auth_token()
    print(f"\n  Pairing token: {token}\n", flush=True)

    providers = settings.configured_providers()
    if not providers:
        logger.warning(
            "No LLM API key is configured. Alfred will start but cannot think. "
            "Add GEMINI_API_KEY (free tier) to .env."
        )
    else:
        logger.info("LLM providers available, in order: %s", ", ".join(providers))

    # Tools have to be registered before the first turn, or the agent runs
    # with an empty toolbox and Alfred insists he cannot read anything.
    from app.tools.files import register_file_tools

    register_file_tools()

    roots = settings.file_roots
    logger.info("Readable roots: %s", ", ".join(str(r) for r in roots) or "(none)")
    logger.info("Alfred ready at %s  (data: %s)", settings.base_url, settings.data_dir)
    try:
        yield
    finally:
        logger.info("Alfred shutting down.")


app = FastAPI(
    title="Alfred",
    description="A private, local-first AI secretary.",
    version="0.1.0",
    lifespan=lifespan,
    # No public docs surface; this is a personal tool, not an API product.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(NetworkGuardMiddleware)
app.include_router(api_router)

app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a key or a full traceback to the browser."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": redact(f"Something went wrong: {exc}")},
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    # Served from the root so its scope covers the whole origin. A service
    # worker under /assets/ could only control /assets/.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    path = STATIC_DIR / "icons" / "alfred.svg"
    if path.exists():
        return FileResponse(path, media_type="image/svg+xml")
    return Response(status_code=204)


def run() -> None:
    """Console entry point used by scripts/run.ps1 and ``python -m app.main``."""
    import uvicorn

    settings = get_settings()
    setup_logging(settings)
    uvicorn.run(
        "app.main:app",
        host=settings.host,  # validated: loopback or Tailscale CGNAT only
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
        # Off deliberately: EventSource and <audio> carry the token in the query
        # string, and an access log would write it to disk on every request.
        access_log=False,
    )


if __name__ == "__main__":
    run()
