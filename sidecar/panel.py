"""Phone control panel. Reached only through `tailscale serve` on a 0600 socket, so the identity header can't be
forged. On top of the identity check the owner logs in with a password; the session is a signed cookie."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import secrets
import threading
import time
from collections import deque
from typing import Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .ledger import LedgerCorrupt, MAX_CAP_USD
from .login import LoginError, LoginSession
from .password import verify_password
from .runtime import Runtime

logger = logging.getLogger("claude-sidecar")
IDENTITY_HEADER = "Tailscale-User-Login"
SESSION_COOKIE = "sidecar_session"
SESSION_TTL = 12 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW = 300

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Claude sidecar</title>
<style>body{font:16px system-ui,sans-serif;margin:0 auto;max-width:32rem;padding:1rem}
form{margin:.5rem 0}button,input{font:inherit;padding:.6rem;width:100%;box-sizing:border-box;margin:.2rem 0}
.note{background:#fff4d6;padding:.5rem;border-radius:.4rem}dt{font-weight:600}</style></head><body>
<h1>Claude sidecar</h1>"""

STATUS = """{message}<dl>
<dt>Login</dt><dd>{login}</dd><dt>Claude Code</dt><dd>{version}</dd>
<dt>Spend this month</dt><dd>{spent} of {cap} ({month}); {unknown} request(s) with unknown cost</dd>
<dt>In flight</dt><dd>{inflight}</dd><dt>Last request</dt><dd>{last}</dd><dt>State</dt><dd>{paused}</dd></dl>
<p class="note">The spend figure is the plugin's list-price estimate. Please check the Claude console for the real
Agent SDK credit balance, and keep extra usage OFF.</p>
{forms}"""

# The username field is only there so password managers (iOS AutoFill, LastPass) recognise a login
# form; the server ignores it and authenticates by tailnet identity plus password.
LOGIN = """{message}<form method="post" action="/login"><input type="hidden" name="csrf" value="{csrf}">
<label>User <input type="text" name="username" autocomplete="username" value="{username}" readonly></label>
<label>Password <input type="password" name="password" autocomplete="current-password" required></label>
<button>Log in</button></form>"""

DISABLED = "Login is disabled: no panel password is set. Re-run install.sh to set one."


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(HEAD + body + "</body></html>", status_code=status_code, headers={"Cache-Control": "no-store"})


def _message(text: str) -> str:
    return f"<p><strong>{html.escape(text)}</strong></p>" if text else ""


def _form(action: str, label: str, csrf: str, extra: str = "") -> str:
    return (f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{csrf}">'
            f"{extra}<button>{html.escape(label)}</button></form>")


LEDGER_FAILED = "{} failed: the spend ledger is unreadable or unwritable (the API refuses requests until it is fixed)."


def _forbidden() -> Response:
    return Response("forbidden", status_code=403)


def create_panel_app(rt: Runtime, *, login_factory: Callable[[], LoginSession], status_fn: Callable[[], dict],
                     restart_hook: Callable[[], None], now: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    csrf_token = secrets.token_urlsafe(32)
    state = {"login": None, "url": None, "message": ""}
    lock = threading.Lock()
    reauth_starting = asyncio.Lock()
    failures: deque[float] = deque()

    def owner(request: Request) -> bool:
        login = request.headers.get(IDENTITY_HEADER, "")
        return bool(login) and hmac.compare_digest(login.encode(), rt.config.owner_login.encode())

    def login_enabled() -> bool:
        return bool(rt.config.panel_password_hash) and bool(rt.config.panel_session_secret)

    def sign(expiry: int) -> str:
        message = f"{expiry}|{rt.config.owner_login}".encode()
        return hmac.new(rt.config.panel_session_secret.encode(), message, hashlib.sha256).hexdigest()

    def session_ok(request: Request) -> bool:
        if not login_enabled():
            return False
        expiry_text, _, mac = request.cookies.get(SESSION_COOKIE, "").partition(".")
        try:
            expiry = int(expiry_text)
        except ValueError:
            return False
        t = now()
        return t < expiry <= t + SESSION_TTL and hmac.compare_digest(mac.encode(), sign(expiry).encode())

    async def fields_of(request: Request) -> dict[str, str]:
        body = (await request.body()).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}

    def csrf_ok(fields: dict[str, str]) -> bool:
        return hmac.compare_digest(fields.get("csrf", "").encode(), csrf_token.encode())

    async def form(request: Request) -> dict[str, str] | None:
        if not (owner(request) and session_ok(request)):
            return None
        fields = await fields_of(request)
        return fields if csrf_ok(fields) else None

    def rate_limited() -> bool:
        cutoff = now() - FAILURE_WINDOW
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return len(failures) >= MAX_FAILURES

    def login_page(message: str, status_code: int = 200) -> HTMLResponse:
        return _page(LOGIN.format(message=_message(message), csrf=csrf_token,
                                  username=html.escape(rt.config.owner_login)), status_code)

    def done(message: str = "") -> Response:
        state["message"] = message
        return RedirectResponse("/", status_code=303)

    @app.get("/login")
    async def login_form(request: Request):
        if not owner(request):
            return _forbidden()
        if session_ok(request):
            return RedirectResponse("/", status_code=303)
        return login_page("" if login_enabled() else DISABLED)

    @app.post("/login")
    async def login_submit(request: Request):
        if not owner(request):
            return _forbidden()
        fields = await fields_of(request)
        if not csrf_ok(fields):
            return _forbidden()
        if not login_enabled():
            return login_page(DISABLED, 403)
        if rate_limited():
            return login_page("Too many failed logins. Wait 5 minutes.", 429)
        # Count the attempt before the slow hash check, so parallel guesses can't slip past the limit.
        failures.append(now())
        ok = await asyncio.to_thread(verify_password, fields.get("password", ""), rt.config.panel_password_hash)
        if not ok:
            logger.warning("panel login failed")
            return login_page("Wrong password.", 401)
        failures.clear()
        logger.info("panel login ok")
        expiry = int(now()) + SESSION_TTL
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, f"{expiry}.{sign(expiry)}", max_age=SESSION_TTL, path="/",
                            secure=True, httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        if await form(request) is None:
            return _forbidden()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/")
    async def index(request: Request):
        if not owner(request):
            return _forbidden()
        if not session_ok(request):
            return RedirectResponse("/login", status_code=303)
        status = await asyncio.to_thread(status_fn)
        try:
            s = rt.ledger.state()
            spent, cap, month, unknown, paused = (f"${s.spent_usd:.2f}", f"${s.cap_usd:.2f}", s.month,
                                                  s.unknown_cost_requests, "paused" if s.paused else "running")
            last = ", ".join(f"{k}={v}" for k, v in (s.last_request or {}).items()) or "none"
        except LedgerCorrupt:
            spent = cap = month = unknown = last = "ledger unreadable"
            paused = "ledger unreadable (API refuses all requests)"
        inflight = f"{rt.gate.model} since {rt.gate.started_at:.0f}" if rt.gate.busy else "idle"
        forms = [_form("/resume" if paused == "paused" else "/pause", "Resume" if paused == "paused" else "Pause", csrf_token),
                 _form("/cancel", "Cancel in-flight request", csrf_token),
                 _form("/restart", "Restart sidecar", csrf_token),
                 _form("/cap", "Set monthly cap (USD)", csrf_token,
                       f'<input name="cap_usd" inputmode="decimal" placeholder="max {MAX_CAP_USD:.0f}">')]
        if state["url"]:
            forms.append(f'<p>Open <a href="{html.escape(state["url"])}">{html.escape(state["url"])}</a>, approve, '
                         f"then paste the code:</p>"
                         + _form("/reauth/code", "Submit code", csrf_token, '<input name="code" autocomplete="off">'))
        else:
            forms.append(_form("/reauth/start", "Re-auth Claude login", csrf_token))
        forms.append(_form("/logout", "Log out", csrf_token))
        values = {
            "message": _message(state["message"]),
            "login": html.escape("logged in" if status.get("logged_in") else "LOGGED OUT") + html.escape(f" ({status.get('plan') or '?'})"),
            "version": html.escape(str(status.get("version") or "unknown")),
            "spent": html.escape(spent), "cap": html.escape(cap), "month": html.escape(month),
            "unknown": html.escape(str(unknown)), "inflight": html.escape(inflight), "last": html.escape(last),
            "paused": html.escape(paused), "forms": "".join(forms),
        }
        return _page(STATUS.format(**values))

    @app.post("/pause")
    async def pause(request: Request):
        if await form(request) is None:
            return _forbidden()
        try:
            rt.ledger.set_paused(True)
        except (LedgerCorrupt, OSError):
            return done(LEDGER_FAILED.format("Pause"))
        return done("Paused.")

    @app.post("/resume")
    async def resume(request: Request):
        if await form(request) is None:
            return _forbidden()
        try:
            rt.ledger.set_paused(False)
        except (LedgerCorrupt, OSError):
            return done(LEDGER_FAILED.format("Resume"))
        return done("Resumed.")

    @app.post("/cancel")
    async def cancel(request: Request):
        if await form(request) is None:
            return _forbidden()
        rt.client.cancel()
        return done("Cancel sent.")

    @app.post("/restart")
    async def restart(request: Request):
        if await form(request) is None:
            return _forbidden()
        restart_hook()
        return done("Restarting.")

    @app.post("/cap")
    async def cap(request: Request):
        fields = await form(request)
        if fields is None:
            return _forbidden()
        try:
            rt.ledger.set_cap(float(fields.get("cap_usd", "")))
        except ValueError:
            return Response(f"cap must be a number from 0 to {MAX_CAP_USD:.0f}", status_code=400)
        except (LedgerCorrupt, OSError):
            return done(LEDGER_FAILED.format("Setting the cap"))
        return done("Cap updated.")

    @app.post("/reauth/start")
    async def reauth_start(request: Request):
        if await form(request) is None:
            return _forbidden()
        # One start at a time, so an overlapping post can never orphan a half-started session.
        if reauth_starting.locked():
            return done("Re-auth is already starting; wait a moment.")
        async with reauth_starting:
            with lock:
                previous, state["login"], state["url"] = state["login"], None, None
            if previous is not None:
                await asyncio.to_thread(previous.close)
            session = login_factory()
            try:
                url = await asyncio.to_thread(session.start)
            except LoginError:
                return done("Re-auth could not start: no login URL appeared.")
            with lock:
                state["login"], state["url"] = session, url
        logger.info("panel reauth started")
        return done()

    @app.post("/reauth/code")
    async def reauth_code(request: Request):
        fields = await form(request)
        if fields is None:
            return _forbidden()
        with lock:
            session, state["login"], state["url"] = state["login"], None, None
        if session is None:
            return done("No re-auth in progress.")
        try:
            ok = await asyncio.to_thread(session.submit, fields.get("code", ""))
        except LoginError:
            ok = False
        logger.info("panel reauth finished ok=%s", ok)
        return done("Re-auth succeeded." if ok else "Re-auth failed. Start again.")

    return app
