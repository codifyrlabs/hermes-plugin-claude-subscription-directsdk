# sidecar/tests/test_service.py
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

import directsdk
from sidecar.config import SidecarConfig
from sidecar.env_scrub import scrubbed_env
from sidecar.ledger import SpendLedger
from sidecar.runtime import Gate, Runtime
from sidecar.service import create_api_app

FAKE = Path(__file__).with_name("fake_claude.py")
KEY = "k" * 40
AUTH = {"Authorization": f"Bearer {KEY}"}
TOOLS = [{"type": "function", "function": {"name": "probe", "description": "probe",
          "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}]


def _runtime(tmp_path, mode="text", extra_env=None):
    config = SidecarConfig(api_key=KEY, owner_login="owner@example.com", state_dir=tmp_path)
    env = scrubbed_env({"HOME": str(tmp_path), "PATH": os.environ["PATH"],
                        "CLAUDE_SUBSCRIPTION_DIRECTSDK_FAKE_MODE": mode, **(extra_env or {})})
    client = directsdk.Client(command=[sys.executable, str(FAKE)], env=env, timeout=30)
    return Runtime(config=config, ledger=SpendLedger(tmp_path / "ledger.json", 150.0), client=client, gate=Gate())


def _http(rt):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api_app(rt)), base_url="http://sidecar")


def _body(**over):
    body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "go"}]}
    body.update(over)
    return body


async def test_health_needs_no_auth(tmp_path):
    async with _http(_runtime(tmp_path)) as http:
        r = await http.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": KEY}])
async def test_bad_bearer_is_401(tmp_path, headers):
    async with _http(_runtime(tmp_path)) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=headers)
    assert r.status_code == 401 and r.json()["error"]["type"] == "unauthorized"


async def test_models_lists_allowlist(tmp_path):
    async with _http(_runtime(tmp_path)) as http:
        r = await http.get("/v1/models", headers=AUTH)
    assert sorted(m["id"] for m in r.json()["data"]) == ["claude-opus-5-5", "claude-sonnet-5"]


async def test_completion_records_cost(tmp_path):
    rt = _runtime(tmp_path)
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["choices"][0]["message"]["content"] == "hello"
    assert data["usage"]["native_cost"]["total_cost_usd"] == pytest.approx(0.0125)
    assert "_response" not in json.dumps(data)
    s = rt.ledger.state()
    assert s.spent_usd == pytest.approx(0.0125) and s.last_request["outcome"] == "ok"
    assert not rt.gate.busy


async def test_streaming_emits_sse_and_records_cost(tmp_path):
    rt = _runtime(tmp_path)
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(stream=True, stream_options={"include_usage": True}), headers=AUTH)
    assert r.status_code == 200
    events = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"]) == "hello"
    assert rt.ledger.state().spent_usd == pytest.approx(0.0125)
    assert not rt.gate.busy


async def test_tool_call_round_trip(tmp_path):
    rt = _runtime(tmp_path, mode="tool")
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(tools=TOOLS), headers=AUTH)
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "probe" and json.loads(call["function"]["arguments"]) == {"value": "x"}


async def test_model_not_allowed_is_400(tmp_path):
    async with _http(_runtime(tmp_path)) as http:
        r = await http.post("/v1/chat/completions", json=_body(model="claude-haiku-4-5-20251001"), headers=AUTH)
    assert r.status_code == 400 and r.json()["error"]["type"] == "model_not_allowed"


async def test_plugin_value_error_is_400_and_releases_slot(tmp_path):
    rt = _runtime(tmp_path)
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(n=2), headers=AUTH)
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request"
    assert not rt.gate.busy


async def test_busy_is_429(tmp_path):
    rt = _runtime(tmp_path)
    assert rt.gate.try_acquire("claude-sonnet-5")
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 429 and r.json()["error"]["type"] == "busy"


async def test_monthly_cap_is_429(tmp_path):
    rt = _runtime(tmp_path)
    rt.ledger.add(150.0, {})
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 429 and r.json()["error"]["type"] == "monthly_cap"


async def test_paused_is_503(tmp_path):
    rt = _runtime(tmp_path)
    rt.ledger.set_paused(True)
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["type"] == "paused"


async def test_corrupt_ledger_is_503(tmp_path):
    rt = _runtime(tmp_path)
    (tmp_path / "ledger.json").write_text("{")
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["type"] == "ledger_unreadable"


async def test_logged_out_returns_503_and_releases_slot(tmp_path):
    rt = _runtime(tmp_path, mode="logged_out")
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["type"] == "logged_out"
    assert not rt.gate.busy


async def test_missing_binary_is_503(tmp_path):
    rt = _runtime(tmp_path)
    rt.client = directsdk.Client(command="/does/not/exist", env={"PATH": "/usr/bin", "HOME": str(tmp_path)})
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["type"] == "claude_missing"
    assert not rt.gate.busy


async def test_env_scrub_reaches_child(tmp_path, monkeypatch):
    dump = tmp_path / "env.json"
    rt = _runtime(tmp_path, extra_env={"CLAUDE_SUBSCRIPTION_DIRECTSDK_FAKE_ENV_DUMP": str(dump),
                                       "ANTHROPIC_API_KEY": "sk-ant-must-not-pass", "SIDECAR_SENTINEL": "x"})
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(), headers=AUTH)
    assert r.status_code == 200
    child = json.loads(dump.read_text())
    assert "sk-ant-must-not-pass" not in json.dumps(child)
    assert "ANTHROPIC_AUTH_TOKEN" not in child and "SIDECAR_SENTINEL" not in child


async def test_stream_disconnect_releases_slot_and_kills_child(tmp_path):
    # NOTE (deviation from the brief): httpx.ASGITransport.handle_async_request runs the whole ASGI
    # app to completion in one shot before it hands any bytes back to the httpx client (verified in
    # sidecar/.venv/lib/python3.12/site-packages/httpx/_transports/asgi.py: `await self.app(scope,
    # receive, send)` populates `body_parts` fully, then `receive()` only ever returns
    # `http.disconnect` after the request body is exhausted and `response_complete` is already set).
    # A "hang" mode fake `claude` sleeps 60s inside the app, so ASGITransport would block the whole
    # test for 60s and could never deliver a real mid-stream disconnect. Per the brief's own escape
    # hatch, this one test runs the app on a real uvicorn server bound to a unix socket instead, so the
    # httpx client can read partial SSE bytes off the wire and then really disconnect mid-stream.
    import psutil

    pid_file = tmp_path / "pid"
    rt = _runtime(tmp_path, mode="hang", extra_env={"CLAUDE_SUBSCRIPTION_DIRECTSDK_FAKE_PID_FILE": str(pid_file)})

    socket_dir = tempfile.mkdtemp(prefix="sc", dir="/tmp")
    socket_path = os.path.join(socket_dir, "s")
    config = uvicorn.Config(create_api_app(rt), uds=socket_path, log_level="critical", lifespan="off")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)

        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://sidecar") as http:
            async with http.stream("POST", "/v1/chat/completions", json=_body(stream=True), headers=AUTH) as r:
                assert r.status_code == 200
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        break
    finally:
        server.should_exit = True
        await serve_task

    child = int(pid_file.read_text())

    def child_alive():
        try:
            return psutil.Process(child).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    deadline = time.monotonic() + 15
    while (rt.gate.busy or child_alive()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not rt.gate.busy
    assert not child_alive()
    assert rt.ledger.state().last_request["outcome"] == "client_disconnected"


async def test_logs_hold_no_secrets(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    rt = _runtime(tmp_path)
    async with _http(rt) as http:
        await http.post("/v1/chat/completions", json=_body(messages=[{"role": "user", "content": "PROMPT-MARKER"}]), headers=AUTH)
        await http.post("/v1/chat/completions", json=_body(), headers={"Authorization": "Bearer sk-ant-oauth-guess"})
    text = caplog.text
    for needle in ("Bearer", "sk-ant-", "oauth", KEY, "PROMPT-MARKER", "hello"):
        assert needle not in text, needle
    assert "outcome=ok" in text


@pytest.mark.parametrize("stream", [False, True])
async def test_ledger_write_oserror_still_releases_slot(tmp_path, stream):
    rt = _runtime(tmp_path)

    def full_disk(cost, meta):
        raise OSError(28, "No space left on device")

    rt.ledger.add = full_disk
    async with _http(rt) as http:
        first = await http.post("/v1/chat/completions", json=_body(stream=stream), headers=AUTH)
        assert not rt.gate.busy
        second = await http.post("/v1/chat/completions", json=_body(stream=stream), headers=AUTH)
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    assert not rt.gate.busy


async def test_stream_logged_out_is_503_before_any_bytes(tmp_path):
    rt = _runtime(tmp_path, mode="logged_out")
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(stream=True), headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["type"] == "logged_out"
    assert not rt.gate.busy
    assert rt.ledger.state().last_request["outcome"] == "logged_out"


async def test_stream_missing_binary_is_503_before_any_bytes(tmp_path):
    rt = _runtime(tmp_path)
    rt.client = directsdk.Client(command="/does/not/exist", env={"PATH": "/usr/bin", "HOME": str(tmp_path)})
    async with _http(rt) as http:
        r = await http.post("/v1/chat/completions", json=_body(stream=True), headers=AUTH)
    assert r.status_code == 503 and r.json()["error"]["type"] == "claude_missing"
    assert not rt.gate.busy


async def test_stream_response_settles_even_if_body_never_starts():
    from sidecar.service import _SettlingStreamingResponse

    settled, started = [], []

    async def body():
        started.append(True)
        yield "data: x\n\n"

    async def settle(outcome, usage):
        settled.append(outcome)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise OSError("client went away before headers")

    response = _SettlingStreamingResponse(body(), settle, media_type="text/event-stream")
    with pytest.raises(Exception):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert settled == ["client_disconnected"] and not started
