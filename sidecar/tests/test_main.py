import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from sidecar.__main__ import GRACEFUL_SHUTDOWN_SECONDS, _server, bind_unix_socket

FORK_ROOT = Path(__file__).resolve().parents[2]


def test_bind_sets_mode_and_replaces_stale_file():
    d = Path(tempfile.mkdtemp(prefix="sc", dir="/tmp"))
    path = d / "api.sock"
    path.write_text("stale")
    sock = bind_unix_socket(path, 0o660, None)
    try:
        st = os.stat(path)
        assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o660
    finally:
        sock.close()
        shutil.rmtree(d, ignore_errors=True)


def test_main_serves_both_sockets_and_stops_on_sigterm():
    run = Path(tempfile.mkdtemp(prefix="sc", dir="/tmp"))
    state = Path(tempfile.mkdtemp(prefix="st", dir="/tmp"))
    env = {"PATH": os.environ["PATH"], "HOME": str(state), "CLAUDE_SIDECAR_API_KEY": "k" * 40,
           "CLAUDE_SIDECAR_OWNER_LOGIN": "owner@example.com", "CLAUDE_SIDECAR_STATE_DIR": str(state),
           "CLAUDE_SIDECAR_RUNTIME_DIR": str(run), "CLAUDE_SIDECAR_CLIENT_GROUP": "",
           "CLAUDE_SIDECAR_CLAUDE_COMMAND": str(Path(__file__).with_name("fake_claude.py"))}
    proc = subprocess.Popen([sys.executable, "-m", "sidecar"], cwd=FORK_ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while not ((run / "api.sock").exists() and (run / "panel.sock").exists()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stat.S_IMODE(os.stat(run / "api.sock").st_mode) == 0o660
        assert stat.S_IMODE(os.stat(run / "panel.sock").st_mode) == 0o600
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(run / "api.sock"))) as c:
            assert c.get("http://sidecar/health").json() == {"status": "ok"}
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(run / "panel.sock"))) as c:
            assert c.get("http://panel/").status_code == 403
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(run, ignore_errors=True)
        shutil.rmtree(state, ignore_errors=True)
    out = proc.stdout.read().decode()
    assert "k" * 40 not in out


def test_servers_bound_graceful_shutdown():
    # An in-flight claude request must not hold SIGTERM (or a panel restart) until systemd's SIGKILL.
    assert GRACEFUL_SHUTDOWN_SECONDS <= 10
    assert _server(object()).config.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_SECONDS
