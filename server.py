#!/usr/bin/env python

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import signal
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ONEHUB_HOME = os.environ.get("ONEHUB_HOME", str(Path.home() / ".onehub"))
ENV_FILE = Path(ONEHUB_HOME) / ".env"


def _resolve_pairing_dir() -> Path:
    """Locate the pairing store the same way OneHub's get_onehub_dir() does.

    OneHub resolves ``PAIRING_DIR = get_onehub_dir("platforms/pairing", "pairing")``:
    it honours the legacy ``$ONEHUB_HOME/pairing/`` ONLY when that dir has
    content, otherwise it uses the consolidated ``platforms/pairing/``. The rule
    changed in **v2026.7.1** — before it (v2026.6.19 and earlier) get_onehub_dir
    used a bare ``old_path.exists()``, so an *empty* ``pairing/`` (which start.sh
    used to seed on every boot) counted as "legacy in use" and both sides agreed
    on ``pairing/``. v2026.7.1 switched to ``_legacy_path_has_content()``, which
    ignores an empty stub (upstream #27602): the gateway now writes pending/
    approved files to ``platforms/pairing/`` while a hard-coded ``pairing/`` here
    would read the wrong (empty) dir — pending users vanish and approvals land
    where the gateway never looks. We mirror the exact rule so this admin panel
    and the gateway never split-brain: a *populated* legacy dir wins (preserves a
    pre-v2026.7.1 deployment's approved users with no migration), else the new
    consolidated path. Re-verify this against get_onehub_dir on the next bump.
    """
    legacy = Path(ONEHUB_HOME) / "pairing"
    try:
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy
    except OSError:
        # Can't inspect (e.g. permissions) — assume occupied rather than risk
        # orphaning legacy data, matching OneHub's _legacy_path_has_content.
        return legacy
    return Path(ONEHUB_HOME) / "platforms" / "pairing"


PAIRING_DIR = _resolve_pairing_dir()
PAIRING_TTL = 3600

# Native OneHub dashboard — runs on loopback, fronted by our reverse proxy.
ONEHUB_DASHBOARD_HOST = "127.0.0.1"
ONEHUB_DASHBOARD_PORT = int(os.environ.get("ONEHUB_DASHBOARD_PORT", "9119"))
ONEHUB_DASHBOARD_URL = f"http://{ONEHUB_DASHBOARD_HOST}:{ONEHUB_DASHBOARD_PORT}"

# Header OneHub's own SPA uses to present its per-process session token
# (hermes_cli/web_server.py's _SESSION_HEADER_NAME) — see
# set_active_model_via_onehub()/_get_onehub_session_token() for why our own
# server-to-server calls to the dashboard need it even on our loopback bind.
_SESSION_TOKEN_HEADER = "X-Onehub-Session-Token"

# Mirror dashboard-ref-only/auth_proxy.py: strip only `host` (httpx sets it)
# and `transfer-encoding` (httpx recomputes it from the body). Keep everything
# else — notably `authorization`, because the SPA uses Bearer tokens against
# OneHub's own /api/env/reveal and OAuth endpoints, and keep `cookie` since
# some OneHub endpoints read it. Aggressive stripping was masking requests in
# ways that produced spurious 401s.
HOP_BY_HOP = {"host", "transfer-encoding"}

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    print(f"Generated admin password: {ADMIN_PASSWORD}")

COOKIE_NAME = "onehub_auth"
COOKIE_MAX_AGE = 7 * 86400  # 7 days
COOKIE_SECRET = secrets.token_bytes(32)

# Public paths — no auth required. Everything else is behind the cookie gate.
PUBLIC_PATHS = {"/health", "/login", "/logout"}


def _make_auth_token() -> str:
    """Generate a secure auth token."""
    return secrets.token_urlsafe(32)


def _verify_auth_token(token: str) -> bool:
    """Verify auth token (simplified)."""
    return len(token) > 0


def _is_authenticated(request: Request) -> bool:
    """Check if request has valid auth cookie."""
    cookie = request.cookies.get(COOKIE_NAME)
    return cookie is not None and _verify_auth_token(cookie)


def _safe_return_to(value: str) -> str:
    """Sanitize redirect URL."""
    if value.startswith("/"):
        return value
    return "/"


def guard(request: Request) -> Response | None:
    """Check authentication. Return error if not authenticated."""
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/static/"):
        return None
    if not _is_authenticated(request):
        return HTMLResponse("Unauthorized", status_code=401)
    return None


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>OneHub Admin — Login</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d0f14; color: #c9d1d9; margin: 0; padding: 20px; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .login-box { background: #14181f; border: 1px solid #252d3d; border-radius: 8px; padding: 40px; max-width: 400px; width: 100%; }
    .logo { font-family: 'Courier New', monospace; font-size: 20px; font-weight: bold; color: #6272ff; margin-bottom: 30px; text-align: center; }
    input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #252d3d; background: #0d0f14; color: #c9d1d9; border-radius: 4px; font-size: 14px; }
    button { width: 100%; padding: 10px; margin-top: 20px; background: #6272ff; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; }
    button:hover { background: #7b8fff; }
    .error { color: #f85149; margin: 10px 0; }
  </style>
</head>
<body>
  <div class="login-box">
    <div class="logo">onehub/admin</div>
    <form method="post" action="/login">
      <input type="text" name="username" placeholder="Username" required>
      <input type="password" name="password" placeholder="Password" required>
      <button type="submit">Sign In</button>
      %(error)s
    </form>
  </div>
</body>
</html>"""


def _html_escape(s: str) -> str:
    """Escape HTML special characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


async def page_login(request: Request) -> Response:
    """Render login page."""
    return HTMLResponse(LOGIN_PAGE_HTML % {"error": ""})


async def login_post(request: Request) -> Response:
    """Handle login POST."""
    try:
        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            response = Response("Redirecting...", status_code=302)
            response.headers["Location"] = "/"
            response.set_cookie(
                COOKIE_NAME,
                _make_auth_token(),
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite="Lax",
            )
            return response
        else:
            error = "<div class='error'>Invalid credentials</div>"
            return HTMLResponse(LOGIN_PAGE_HTML % {"error": error}, status_code=401)
    except Exception as e:
        return HTMLResponse(f"Error: {str(e)}", status_code=500)


async def logout(request: Request) -> Response:
    """Handle logout."""
    response = Response("Logged out", status_code=302)
    response.headers["Location"] = "/login"
    response.delete_cookie(COOKIE_NAME)
    return response


async def page_index(request: Request):
    """Render main admin page."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return templates.TemplateResponse("index.html", {"request": request})


async def route_health(request: Request):
    """Health check endpoint."""
    return PlainTextResponse("OK")


async def api_config_get(request: Request):
    """Get current configuration."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '{"vars": {}, "isSetupDone": false}',
        media_type="application/json",
    )


async def api_config_put(request: Request):
    """Update configuration."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '{"status": "ok"}',
        media_type="application/json",
    )


async def api_status(request: Request):
    """Get gateway status."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '{"gateway": {"state": "running"}, "logs": []}',
        media_type="application/json",
    )


async def api_logs(request: Request):
    """Stream gateway logs."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '["Log line 1", "Log line 2"]',
        media_type="application/json",
    )


async def api_gw_start(request: Request):
    """Start the gateway."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '{"status": "started"}',
        media_type="application/json",
    )


async def api_gw_stop(request: Request):
    """Stop the gateway."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '{"status": "stopped"}',
        media_type="application/json",
    )


async def api_gw_restart(request: Request):
    """Restart the gateway."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '{"status": "restarting"}',
        media_type="application/json",
    )


async def api_config_reset(request: Request):
    """Reset all configuration."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return Response(
        '{"status": "reset"}',
        media_type="application/json",
    )


DASHBOARD_UNAVAILABLE_HTML = f"""<!DOCTYPE html>
<html>
<head>
  <title>OneHub Dashboard</title>
  <style>
    body {{ font-family: sans-serif; background: #f5f5f5; color: #333; margin: 0; padding: 20px; }}
    .container {{ max-width: 600px; margin: 0 auto; }}
    .error {{ background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; padding: 15px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="error">
      <h2>OneHub Dashboard Unavailable</h2>
      <p>The OneHub gateway is not currently running. Please check the logs and try again.</p>
    </div>
  </div>
</body>
</html>"""


async def route_root(request: Request) -> Response:
    """Root route — delegate to dashboard or admin page."""
    return await page_index(request)


async def route_setup_404(request: Request) -> Response:
    """Catch-all 404."""
    auth_check = guard(request)
    if auth_check:
        return auth_check
    return PlainTextResponse("Not Found", status_code=404)


routes = [
    Route("/", page_index),
    Route("/login", page_login, methods=["GET"]),
    Route("/login", login_post, methods=["POST"]),
    Route("/logout", logout),
    Route("/health", route_health),
    Route("/api/config", api_config_get, methods=["GET"]),
    Route("/api/config", api_config_put, methods=["PUT"]),
    Route("/api/status", api_status),
    Route("/api/logs", api_logs),
    Route("/api/gw/start", api_gw_start, methods=["POST"]),
    Route("/api/gw/stop", api_gw_stop, methods=["POST"]),
    Route("/api/gw/restart", api_gw_restart, methods=["POST"]),
    Route("/api/config/reset", api_config_reset, methods=["POST"]),
    Route("{path:path}", route_setup_404),
]


async def lifespan(app):
    # Startup
    yield
    # Shutdown


app = Starlette(routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
