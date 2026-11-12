"""Shared-password login for a public demo instance (ported from localdesk/app/auth.py).

Off by default: local runs stay open. The deployed box sets LOGIN_ENABLED=1 in its .env; then
every route needs a signed session cookie from the /login form, with one of two accounts:

    guest  / APP_PASSWORD     (handed to the customer)
    robert / ADMIN_PASSWORD

A cookie rather than Caddy basic auth, the same as localdesk: one login page that works the same
in every browser and on corporate proxies. The signing key is derived from the two passwords, so
rotating a password logs everyone out. Passwords come from the environment or .env and are never
logged or returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import time
from http.cookies import SimpleCookie
from typing import Optional
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import env_value

COOKIE = "sd_session"
MAX_AGE_S = 12 * 3600
PUBLIC_PATHS = {"/login", "/logout", "/static/riso.css"}


def login_enabled() -> bool:
    return env_value("LOGIN_ENABLED").lower() in ("1", "true", "yes")


def _accounts() -> dict[str, str]:
    return {u: p for u, p in (("guest", env_value("APP_PASSWORD")), ("robert", env_value("ADMIN_PASSWORD"))) if p}


def _key() -> bytes:
    return hashlib.sha256(("signal-desk-login|" + env_value("APP_PASSWORD") + "|"
                           + env_value("ADMIN_PASSWORD")).encode()).digest()


def _sign(user: str, expires: int) -> str:
    return hmac.new(_key(), f"{user}.{expires}".encode(), hashlib.sha256).hexdigest()


def make_token(user: str) -> str:
    expires = int(time.time()) + MAX_AGE_S
    return f"{user}.{expires}.{_sign(user, expires)}"


def token_user(token: str) -> Optional[str]:
    """The user name for a valid, unexpired token, else None."""
    try:
        user, expires_s, sig = token.split(".")
        expires = int(expires_s)
    except ValueError:
        return None
    if expires < time.time() or user not in _accounts():
        return None
    return user if hmac.compare_digest(sig, _sign(user, expires)) else None


def _scope_user(scope) -> Optional[str]:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:
                return None
            if COOKIE in jar:
                return token_user(jar[COOKIE].value)
    return None


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Desk · sign in</title>
<link rel="stylesheet" href="/static/riso.css">
<style>
body{display:flex;align-items:center;justify-content:center;padding:16px}
form{width:100%;max-width:340px;padding:28px 26px}
h1{font-size:1.6rem;color:var(--blue);text-shadow:.045em .04em 0 var(--pink);margin-bottom:4px}
p{color:var(--muted);font-weight:600;font-size:14px;margin-bottom:18px}
label{display:block;font-weight:700;font-size:14px;margin:12px 0 4px}
input{width:100%;padding:9px 10px;border:2px solid var(--navy);background:#fff;font-size:16px}
.btn{width:100%;margin-top:20px}
.err{color:var(--danger);font-weight:700;font-size:14px;margin-top:12px;min-height:20px}
</style></head><body>
<form class="card" method="post" action="/login">
 <h1>Signal Desk</h1>
 <p>Sign in to the demo</p>
 <label for="u">Username</label>
 <input id="u" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" value="__USER__" required autofocus>
 <label for="p">Password</label>
 <input id="p" name="password" type="password" autocomplete="current-password" required>
 <button class="btn" type="submit">Sign in</button>
 <div class="err">__ERROR__</div>
</form></body></html>"""


def _page(error: str = "", user: str = "", status: int = 200) -> HTMLResponse:
    body = LOGIN_PAGE.replace("__ERROR__", html.escape(error)).replace("__USER__", html.escape(user, quote=True))
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


class LoginMiddleware:
    """Pure ASGI middleware (so the long-lived SSE stream is covered like any route)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not login_enabled():
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in ("/login", "/logout"):
            response = await self._login_routes(Request(scope, receive))
            return await response(scope, receive, send)
        if path in PUBLIC_PATHS or _scope_user(scope):
            return await self.app(scope, receive, send)
        if path.startswith("/api/"):
            response = JSONResponse({"error": "not signed in"}, status_code=401)
        else:
            response = RedirectResponse("/login", status_code=303)
        await response(scope, receive, send)

    async def _login_routes(self, request: Request):
        if request.url.path == "/logout":
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(COOKIE, path="/")
            return response
        if request.method != "POST":
            if _scope_user(request.scope):
                return RedirectResponse("/", status_code=303)
            return _page()
        form = parse_qs((await request.body())[:4096].decode("utf-8", "replace"))
        user = (form.get("username", [""])[0]).strip().lower()
        password = form.get("password", [""])[0].strip()
        expected = _accounts().get(user)
        if not expected or not hmac.compare_digest(password.encode(), expected.encode()):
            await asyncio.sleep(1.0)                     # blunt brute-force brake
            return _page("Wrong username or password.", user, status=401)
        response = RedirectResponse("/", status_code=303)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, make_token(user), max_age=MAX_AGE_S, path="/",
                            httponly=True, samesite="lax", secure=secure)
        return response
