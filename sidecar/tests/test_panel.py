import re

import httpx
import pytest

from sidecar.config import SidecarConfig
from sidecar.ledger import SpendLedger
from sidecar.panel import SESSION_COOKIE, SESSION_TTL, create_panel_app
from sidecar.password import hash_password
from sidecar.runtime import Gate, Runtime

OWNER = "owner@example.com"
ID = {"Tailscale-User-Login": OWNER}
PASSWORD = "correct horse battery staple"
HASH = hash_password(PASSWORD)
WRONG = "wrong password, still long enough"


class FakeClient:
    def __init__(self):
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


class FakeLogin:
    def __init__(self):
        self.codes = []

    def start(self):
        return "https://claude.ai/oauth/authorize?x=<script>"

    def submit(self, code, timeout=60.0):
        self.codes.append(code)
        return code == "good-code#state"

    def close(self):
        pass


def _make(tmp_path, password_hash=HASH):
    clock = {"t": 1_000_000.0}
    config = SidecarConfig(api_key="k" * 40, owner_login=OWNER, state_dir=tmp_path,
                           panel_password_hash=password_hash, panel_session_secret="s" * 64)
    rt = Runtime(config=config, ledger=SpendLedger(tmp_path / "l.json", 150.0), client=FakeClient(), gate=Gate())
    calls = {"restart": 0}
    login = FakeLogin()
    app = create_panel_app(rt, login_factory=lambda: login,
                           status_fn=lambda: {"available": True, "logged_in": True, "plan": "max", "version": "2.1.263 (Claude Code)"},
                           restart_hook=lambda: calls.__setitem__("restart", calls["restart"] + 1),
                           now=lambda: clock["t"])
    # https, so the client's cookie jar sends the Secure cookie back.
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://panel")
    return rt, http, calls, login, clock


@pytest.fixture
def env(tmp_path):
    return _make(tmp_path)


def _token(text):
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


async def _login(http, password=PASSWORD):
    page = await http.get("/login", headers=ID)
    return await http.post("/login", data={"csrf": _token(page.text), "password": password}, headers=ID)


async def _csrf(http):
    assert (await _login(http)).status_code == 303
    return _token((await http.get("/", headers=ID)).text)


async def _post(http, path, csrf, **fields):
    return await http.post(path, data={"csrf": csrf, **fields}, headers=ID)


@pytest.mark.parametrize("headers", [{}, {"Tailscale-User-Login": "someone@example.com"}, {"Tailscale-User-Login": ""}])
async def test_panel_rejects_missing_or_foreign_identity(env, headers):
    rt, http, _, _, _ = env
    csrf = await _csrf(http)  # a valid owner session never stands in for the identity header
    for path in ("/", "/login"):
        assert (await http.get(path, headers=headers)).status_code == 403
    r = await http.post("/pause", data={"csrf": csrf}, headers=headers)
    assert r.status_code == 403 and rt.ledger.state().paused is False
    r = await http.post("/login", data={"csrf": csrf, "password": PASSWORD}, headers=headers)
    assert r.status_code == 403


@pytest.mark.parametrize("csrf", [None, "", "wrong"])
async def test_panel_post_requires_csrf(env, csrf):
    rt, http, _, _, _ = env
    await _csrf(http)
    data = {} if csrf is None else {"csrf": csrf}
    r = await http.post("/pause", data=data, headers=ID)
    assert r.status_code == 403 and rt.ledger.state().paused is False
    r = await http.post("/login", data={**data, "password": PASSWORD}, headers=ID)
    assert r.status_code == 403


async def test_index_redirects_to_login_without_session(env):
    _, http, _, _, _ = env
    r = await http.get("/", headers=ID)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = await http.get("/login", headers=ID)
    assert page.status_code == 200 and 'type="password"' in page.text


async def test_actions_require_session(env):
    rt, http, _, _, _ = env
    page = await http.get("/login", headers=ID)
    r = await _post(http, "/pause", _token(page.text))
    assert r.status_code == 403 and rt.ledger.state().paused is False


async def test_wrong_password_rejected_without_cookie(env):
    _, http, _, _, _ = env
    r = await _login(http, WRONG)
    assert r.status_code == 401 and SESSION_COOKIE not in r.headers.get("set-cookie", "")
    assert "Wrong password" in r.text
    assert (await http.get("/", headers=ID)).status_code == 303


async def test_login_sets_hardened_session_cookie(env):
    _, http, _, _, _ = env
    r = await _login(http)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", f"max-age={SESSION_TTL}", "path=/"):
        assert flag in cookie, flag
    assert PASSWORD not in r.headers["set-cookie"]
    assert (await http.get("/", headers=ID)).status_code == 200


async def test_session_expires_after_12_hours(env):
    _, http, _, _, clock = env
    await _login(http)
    clock["t"] += SESSION_TTL + 1
    assert (await http.get("/", headers=ID)).status_code == 303


@pytest.mark.parametrize("mangle", [
    lambda v: v[:-1] + ("0" if v[-1] != "0" else "1"),       # MAC changed
    lambda v: f"{int(v.split('.')[0]) - 60}.{v.split('.')[1]}",  # expiry changed, old MAC
    lambda v: "garbage",
    lambda v: "",
])
async def test_tampered_session_rejected(env, mangle):
    _, http, _, _, _ = env
    value = (await _login(http)).cookies[SESSION_COOKIE]
    http.cookies.clear()
    r = await http.get("/", headers={**ID, "Cookie": f"{SESSION_COOKIE}={mangle(value)}"})
    assert r.status_code == 303


async def test_login_rate_limited_then_recovers(env):
    _, http, _, _, clock = env
    for _ in range(5):
        assert (await _login(http, WRONG)).status_code == 401
    assert (await _login(http)).status_code == 429  # even the right password waits
    clock["t"] += 301
    assert (await _login(http)).status_code == 303


async def test_login_disabled_without_password_hash(tmp_path):
    _, http, _, _, _ = _make(tmp_path, password_hash="")
    page = await http.get("/login", headers=ID)
    assert "Login is disabled" in page.text
    r = await http.post("/login", data={"csrf": _token(page.text), "password": ""}, headers=ID)
    assert r.status_code == 403 and SESSION_COOKIE not in r.headers.get("set-cookie", "")


async def test_malformed_password_hash_fails_closed(tmp_path):
    _, http, _, _, _ = _make(tmp_path, password_hash="scrypt:broken")
    assert (await _login(http)).status_code == 401


async def test_logout_clears_session(env):
    _, http, _, _, _ = env
    csrf = await _csrf(http)
    r = await _post(http, "/logout", csrf)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await http.get("/", headers=ID)).status_code == 303


async def test_status_page_shows_state(env):
    rt, http, _, _, _ = env
    await _csrf(http)
    rt.ledger.add(12.5, {"model": "claude-sonnet-5"})
    page = await http.get("/", headers=ID)
    assert page.status_code == 200
    for text in ("2.1.263", "$12.50", "$150.00", "logged in", "idle", "check the Claude console", "Log out"):
        assert text in page.text, text


async def test_pause_resume_cap(env):
    rt, http, _, _, _ = env
    csrf = await _csrf(http)
    assert (await _post(http, "/pause", csrf)).status_code == 303
    assert rt.ledger.state().paused is True
    await _post(http, "/resume", csrf)
    assert rt.ledger.state().paused is False
    await _post(http, "/cap", csrf, cap_usd="120")
    assert rt.ledger.state().cap_usd == 120.0
    r = await _post(http, "/cap", csrf, cap_usd="999")
    assert r.status_code == 400 and rt.ledger.state().cap_usd == 120.0


async def test_cancel_and_restart(env):
    rt, http, calls, _, _ = env
    csrf = await _csrf(http)
    await _post(http, "/cancel", csrf)
    assert rt.client.cancelled == 1
    await _post(http, "/restart", csrf)
    assert calls["restart"] == 1


async def test_reauth_flow_escapes_url_and_passes_code(env):
    _, http, _, login, _ = env
    csrf = await _csrf(http)
    r = await _post(http, "/reauth/start", csrf)
    assert r.status_code == 303
    page = await http.get("/", headers=ID)
    assert "&lt;script&gt;" in page.text and "<script>" not in page.text
    await _post(http, "/reauth/code", csrf, code="good-code#state")
    assert login.codes == ["good-code#state"]
    page = await http.get("/", headers=ID)
    assert "Re-auth succeeded" in page.text


@pytest.mark.parametrize("path, fields", [("/pause", {}), ("/resume", {}), ("/cap", {"cap_usd": "120"})])
async def test_ledger_actions_show_message_when_ledger_corrupt(env, tmp_path, path, fields):
    rt, http, *_ = env
    csrf = await _csrf(http)
    (tmp_path / "l.json").write_text("{")
    r = await _post(http, path, csrf, **fields)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "spend ledger" in (await http.get("/", headers=ID)).text


@pytest.mark.parametrize("path, fields", [("/pause", {}), ("/resume", {}), ("/cap", {"cap_usd": "120"})])
async def test_ledger_actions_show_message_when_ledger_unwritable(env, path, fields):
    rt, http, *_ = env
    csrf = await _csrf(http)

    def read_only(state):
        raise OSError(30, "Read-only file system")

    rt.ledger._save = read_only
    r = await _post(http, path, csrf, **fields)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "spend ledger" in (await http.get("/", headers=ID)).text
