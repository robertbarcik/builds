"""Shared-password login for the public demo instance.

Off by default (local runs stay open). The deployed box sets LOGIN_ENABLED=1 in
its .env; then every HTTP route and the /ws WebSocket need a signed session
cookie, obtained from the /login form with one of two accounts:

    student / APP_PASSWORD     (handed to the room)
    robert  / ADMIN_PASSWORD

A cookie (not HTTP basic auth) on purpose: Safari does not send basic-auth
credentials on WebSocket handshakes, and without /ws the ops room is dead and
the human gate fails closed. The signing key is derived from the two passwords,
so rotating a password logs everyone out.
"""

import asyncio
import hashlib
import hmac
import html
import os
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = "mu_session"
MAX_AGE_S = 12 * 3600
PUBLIC_PATHS = {"/login", "/logout"}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip().strip('"').strip("'")


def login_enabled() -> bool:
    return _env("LOGIN_ENABLED").lower() in ("1", "true", "yes")


def _accounts() -> dict:
    return {u: p for u, p in (("student", _env("APP_PASSWORD")), ("robert", _env("ADMIN_PASSWORD"))) if p}


def _key() -> bytes:
    return hashlib.sha256(("mu-login|" + _env("APP_PASSWORD") + "|" + _env("ADMIN_PASSWORD")).encode()).digest()


def _sign(user: str, expires: int) -> str:
    return hmac.new(_key(), f"{user}.{expires}".encode(), hashlib.sha256).hexdigest()


def make_token(user: str) -> str:
    expires = int(time.time()) + MAX_AGE_S
    return f"{user}.{expires}.{_sign(user, expires)}"


def token_user(token: str):
    """Return the user name for a valid, unexpired token, else None."""
    try:
        user, expires, sig = token.split(".")
        expires = int(expires)
    except ValueError:
        return None
    if expires < time.time() or user not in _accounts():
        return None
    return user if hmac.compare_digest(sig, _sign(user, expires)) else None


def _scope_user(scope):
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


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>mu · sign in</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:linear-gradient(150deg,#0f1117 0%,#131620 100%);color:#e0e0e0;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;padding:16px}
form{width:100%;max-width:320px;background:#1a1d25;border:1px solid rgba(255,255,255,.06);
 border-radius:14px;padding:32px 28px;box-shadow:0 4px 32px rgba(0,0,0,.45)}
.mu{font-size:54px;line-height:1;color:#b0bda7;text-align:center;margin-bottom:6px}
p{text-align:center;color:#777;font-size:13px;margin-bottom:24px}
label{display:block;font-size:12px;color:#888;margin:14px 0 6px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid rgba(255,255,255,.1);
 background:#252830;color:#e0e0e0;font-size:16px;outline:none}
input:focus{border-color:#b0bda7}
button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:9px;background:#b0bda7;
 color:#0f1117;font-size:15px;font-weight:600;cursor:pointer}
.err{color:#e0a05a;font-size:13px;text-align:center;margin-top:14px;min-height:18px}
</style></head><body>
<form method="post" action="/login">
 <div class="mu">&mu;</div>
 <p>LocalDesk ops room</p>
 <label for="u">Username</label>
 <input id="u" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" value="__USER__" required autofocus>
 <label for="p">Password</label>
 <input id="p" name="password" type="password" autocomplete="current-password" required>
 <button type="submit">Sign in</button>
 <div class="err">__ERROR__</div>
</form></body></html>"""


def _page(error: str = "", user: str = "", status: int = 200) -> HTMLResponse:
    body = LOGIN_PAGE.replace("__ERROR__", html.escape(error)).replace("__USER__", html.escape(user, quote=True))
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


class LoginMiddleware:
    """Pure ASGI middleware so the WebSocket handshake is covered too."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not login_enabled():
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if scope["type"] == "websocket":
            if _scope_user(scope):
                return await self.app(scope, receive, send)
            await receive()  # websocket.connect
            return await send({"type": "websocket.close", "code": 4401})

        if path in PUBLIC_PATHS:
            response = await self._login_routes(Request(scope, receive))
            return await response(scope, receive, send)

        if _scope_user(scope):
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
            await asyncio.sleep(1.0)  # blunt brute-force brake
            return _page("Wrong username or password.", user, status=401)

        response = RedirectResponse("/", status_code=303)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE, make_token(user), max_age=MAX_AGE_S, path="/",
                            httponly=True, samesite="lax", secure=secure)
        return response
