"""Phone control panel. Reached only through `tailscale serve` on a 0600 socket, so the identity header can't be
forged. On top of the identity check the owner logs in with a password; the session is a signed cookie."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import os
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

GENERATION_FILE = "panel_generation"

# Light is the default; dark follows the phone's setting unless the owner picked a theme with the toggle,
# which is remembered in localStorage on that device (a blocked store just falls back to the system setting).
LIGHT = ("--bg:#f2f3f7;--card:#fff;--border:transparent;--fg:#1c1d21;--muted:#6b6f7b;--accent:#3b6cf6;"
         "--on-accent:#fff;--plain:#eef0f5;--danger-bg:#fdecec;--danger-fg:#c62828;--ok-bg:#e3f6e8;--ok-fg:#17803d;"
         "--warn-bg:#fff4d6;--warn-fg:#8a5a00;--track:#e8e9ee;--input:#fff;--input-border:#d7d9e0;--icon:'\\263E'")
DARK = ("--bg:#0f1115;--card:#181b22;--border:#262a33;--fg:#e6e8ee;--muted:#8b93a7;--accent:#f59e0b;"
        "--on-accent:#111;--plain:#232733;--danger-bg:#3a1d1d;--danger-fg:#fca5a5;--ok-bg:#12301f;--ok-fg:#4ade80;"
        "--warn-bg:#3a2e12;--warn-fg:#fbbf24;--track:#262a33;--input:#12151b;--input-border:#2f3440;--icon:'\\2600'")

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hermes Claude Console</title>
<meta name="apple-mobile-web-app-title" content="HC Console">
<script>
try { var t = localStorage.getItem("hc-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
function toggleTheme() {
  var root = document.documentElement, dark = root.dataset.theme
    ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("hc-theme", root.dataset.theme); } catch (e) {}
}
</script>
<style>:root{""" + LIGHT + """}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){""" + DARK + """}}
:root[data-theme="dark"]{""" + DARK + """}
*{box-sizing:border-box}
body{font:15px -apple-system,system-ui,sans-serif;margin:0 auto;max-width:32rem;padding:14px;background:var(--bg);color:var(--fg)}
.top{display:flex;justify-content:space-between;align-items:center;margin:4px 2px 12px}h1{font-size:19px;margin:0}
#theme-toggle{width:36px;height:36px;border-radius:50%;border:1px solid var(--input-border);background:var(--card);
color:var(--fg);font-size:17px;padding:0}#theme-toggle::after{content:var(--icon)}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:12px 14px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;gap:12px;padding:5px 0;font-size:14px}
.row span:first-child{color:var(--muted)}.row span:last-child{text-align:right;overflow-wrap:anywhere}
.pill{border-radius:99px;padding:2px 10px;font-weight:600;font-size:13px}
.ok{background:var(--ok-bg);color:var(--ok-fg)}.warn{background:var(--warn-bg);color:var(--warn-fg)}
.bad{background:var(--danger-bg);color:var(--danger-fg)}
.bar{height:8px;background:var(--track);border-radius:99px;overflow:hidden;margin:8px 0 4px}
.bar i{display:block;height:100%;background:var(--accent)}
.big{color:var(--fg);font-weight:600;font-size:18px}.muted{color:var(--muted);font-size:12px}
h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 6px}
form{margin:0}.btn,button{display:block;width:100%;font:inherit;border:0;text-align:center;padding:12px;
border-radius:12px;margin-top:8px;font-weight:600;text-decoration:none;cursor:pointer;background:var(--plain);color:var(--fg)}
.primary{background:var(--accent);color:var(--on-accent)}.danger{background:var(--danger-bg);color:var(--danger-fg)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.grid button{margin-top:8px}
input{width:100%;font:inherit;padding:10px;border:1px solid var(--input-border);background:var(--input);color:var(--fg);
border-radius:10px;margin-top:8px}label{display:block;font-size:13px;color:var(--muted);margin-top:8px}
a{color:var(--accent);overflow-wrap:anywhere}.msg{font-weight:600;margin:0 2px 12px}</style></head><body>
<div class="top"><h1>Hermes Claude Console</h1>
<button id="theme-toggle" type="button" onclick="toggleTheme()" aria-label="Switch light or dark theme"></button></div>"""

STATUS = """{message}<div class="card">
<div class="row"><span>State</span><span class="pill {state_class}">{state}</span></div>
<div class="row"><span>Claude login</span><span>{login}</span></div>
<div class="row"><span>Claude Code</span><span>{version}</span></div>
<div class="row"><span>In flight</span><span>{inflight}</span></div>
<div class="row"><span>Last request</span><span>{last}</span></div></div>
<div class="card"><h3>Spend · {month}</h3>
<div class="row" style="padding:0"><span class="big">{spent}</span><span>of {cap}</span></div>
<div class="bar"><i style="width:{percent}%"></i></div>
<div class="muted">List-price estimate. Draws on your Max session pool; keep extra usage off.
{unknown} request(s) with unknown cost.</div></div>
{reauth}
<div class="card"><h3>Controls</h3>{controls}</div>"""

# The username field is only there so password managers (iOS AutoFill, LastPass) recognise a login
# form; the server ignores it and authenticates by tailnet identity plus password.
LOGIN = """{message}<div class="card"><form method="post" action="/login"><input type="hidden" name="csrf" value="{csrf}">
<label>User <input type="text" name="username" autocomplete="username" value="{username}" readonly></label>
<label>Password <input type="password" name="password" autocomplete="current-password" required></label>
<button class="primary">Log in</button></form></div>"""

DISABLED = "Login is disabled: no panel password is set. Re-run install.sh to set one."
GENERATION_BROKEN = (f"Sessions are disabled: {GENERATION_FILE} in the state directory is unreadable. "
                     "Fix or delete it (deleting logs out everywhere).")


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(HEAD + body + "</body></html>", status_code=status_code, headers={"Cache-Control": "no-store"})


def _message(text: str) -> str:
    return f'<p class="msg">{html.escape(text)}</p>' if text else ""


def _form(action: str, label: str, csrf: str, extra: str = "", cls: str = "") -> str:
    button_class = f' class="{cls}"' if cls else ""
    return (f'<form method="post" action="{action}"><input type="hidden" name="csrf" value="{csrf}">'
            f"{extra}<button{button_class}>{html.escape(label)}</button></form>")


def _last_request(last: dict) -> str:
    if not last:
        return "None"
    if "model" in last and "outcome" in last:
        parts = [str(last["model"]), str(last["outcome"])]
        if isinstance(last.get("duration_ms"), (int, float)):
            parts.append(f"{last['duration_ms'] / 1000:.1f} s")
        return " · ".join(parts)
    return ", ".join(f"{k}={v}" for k, v in last.items() if k != "at")


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

    generation_path = rt.config.state_dir / GENERATION_FILE

    def generation() -> int | None:
        """Every session cookie is signed over this number; logging out bumps it, which revokes them all.
        Missing means 0 (first run). Anything unreadable is None, and then no session is accepted."""
        try:
            text = generation_path.read_text()
        except FileNotFoundError:
            return 0
        except OSError:
            return None
        return int(text) if text.strip().isdigit() else None

    def bump_generation() -> bool:
        current = generation()
        if current is None:
            return False
        tmp = generation_path.with_name(GENERATION_FILE + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(str(current + 1))
            os.chmod(tmp, 0o600)
            os.replace(tmp, generation_path)
        except OSError:
            return False
        return True

    def sign(expiry: int, gen: int) -> str:
        message = f"{expiry}|{gen}|{rt.config.owner_login}".encode()
        return hmac.new(rt.config.panel_session_secret.encode(), message, hashlib.sha256).hexdigest()

    def session_ok(request: Request) -> bool:
        if not login_enabled():
            return False
        gen = generation()
        if gen is None:
            return False
        expiry_text, _, mac = request.cookies.get(SESSION_COOKIE, "").partition(".")
        try:
            expiry = int(expiry_text)
        except ValueError:
            return False
        t = now()
        return t < expiry <= t + SESSION_TTL and hmac.compare_digest(mac.encode(), sign(expiry, gen).encode())

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
        if not login_enabled():
            return login_page(DISABLED)
        return login_page("" if generation() is not None else GENERATION_BROKEN)

    @app.post("/login")
    async def login_submit(request: Request):
        if not owner(request):
            return _forbidden()
        fields = await fields_of(request)
        if not csrf_ok(fields):
            return _forbidden()
        if not login_enabled():
            return login_page(DISABLED, 403)
        gen = generation()
        if gen is None:
            return login_page(GENERATION_BROKEN, 503)
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
        response.set_cookie(SESSION_COOKIE, f"{expiry}.{sign(expiry, gen)}", max_age=SESSION_TTL, path="/",
                            secure=True, httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        if await form(request) is None:
            return _forbidden()
        if bump_generation():
            logger.info("panel logout: all sessions revoked")
            response = RedirectResponse("/login", status_code=303)
        else:
            logger.warning("panel logout could not update %s", GENERATION_FILE)
            response = login_page(f"Log out failed to update {GENERATION_FILE}: other sessions are still valid.", 500)
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
            spent, cap, month, unknown = f"${s.spent_usd:.2f}", f"${s.cap_usd:.2f}", s.month, s.unknown_cost_requests
            percent = min(100, round(100 * s.spent_usd / s.cap_usd)) if s.cap_usd else 100
            paused = s.paused
            run_state, state_class = ("Paused", "warn") if paused else ("Running", "ok")
            last = _last_request(s.last_request or {})
        except LedgerCorrupt:
            spent = cap = month = unknown = last = "ledger unreadable"
            percent, paused = 0, False
            run_state, state_class = "ledger unreadable (API refuses all requests)", "bad"
        inflight = f"{rt.gate.model} since {rt.gate.started_at:.0f}" if rt.gate.busy else "Idle"
        if state["url"]:
            url = state["url"]
            # googlechromes:// hands the link to Chrome on iOS; a home-screen shortcut otherwise can't open it.
            chrome = (f'<a class="btn primary" href="{html.escape("googlechromes://" + url[len("https://"):])}">'
                      "Open in Chrome</a>" if url.startswith("https://") else "")
            reauth = (f'<div class="card"><h3>Claude login</h3>{chrome}'
                      f'<p class="muted">Or open in this browser: <a href="{html.escape(url)}">{html.escape(url)}</a>. '
                      "Approve, then paste the code.</p>"
                      + _form("/reauth/code", "Submit code", csrf_token,
                              '<input name="code" autocomplete="off" placeholder="Paste the code">') + "</div>")
        else:
            reauth = ('<div class="card"><h3>Claude login</h3>'
                      + _form("/reauth/start", "Re-auth Claude login", csrf_token, cls="primary") + "</div>")
        controls = (
            '<div class="grid">'
            + _form("/resume" if paused else "/pause", "Resume" if paused else "Pause", csrf_token)
            + _form("/cancel", "Cancel request", csrf_token) + "</div>"
            + _form("/cap", "Set cap", csrf_token,
                    f'<input name="cap_usd" inputmode="decimal" placeholder="Monthly cap in USD, max {MAX_CAP_USD:.0f}">')
            + _form("/restart", "Restart sidecar", csrf_token, cls="danger")
            + _form("/logout", "Log out everywhere", csrf_token))
        login_text = "Logged in" if status.get("logged_in") else "Logged out"
        version = str(status.get("version") or "unknown").removesuffix(" (Claude Code)")
        values = {
            "message": _message(state["message"]),
            "state": html.escape(run_state), "state_class": state_class,
            "login": html.escape(f"{login_text} · {status.get('plan') or '?'}"),
            "version": html.escape(version), "inflight": html.escape(inflight), "last": html.escape(last),
            "spent": html.escape(spent), "cap": html.escape(cap), "month": html.escape(month), "percent": percent,
            "unknown": html.escape(str(unknown)), "reauth": reauth, "controls": controls,
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
