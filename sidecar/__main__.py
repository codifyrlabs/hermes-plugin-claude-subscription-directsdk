"""Entry point: two pre-bound unix sockets (API 0660 group-readable, panel 0600), one event loop."""
from __future__ import annotations

import asyncio
import grp
import logging
import os
import signal
import socket
from pathlib import Path

import uvicorn

from .config import SidecarConfig
from .env_scrub import scrubbed_env
from .login import LoginSession
from .panel import create_panel_app
from .runtime import build_runtime
from .service import create_api_app


def bind_unix_socket(path: Path, mode: int, group: str | None) -> socket.socket:
    path = Path(path)
    if path.exists() or path.is_symlink():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    if group:
        os.chown(path, -1, grp.getgrnam(group).gr_gid)
    os.chmod(path, mode)
    sock.listen(64)
    return sock


def _server(app) -> uvicorn.Server:
    return uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="off"))


async def serve_both(api_app, api_sock, panel_app, panel_sock) -> None:
    api, panel = _server(api_app), _server(panel_app)
    tasks = {asyncio.create_task(api.serve(sockets=[api_sock])), asyncio.create_task(panel.serve(sockets=[panel_sock]))}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    api.should_exit = panel.should_exit = True
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = SidecarConfig.from_env(os.environ)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    rt = build_runtime(config, env=os.environ)
    child_env = scrubbed_env(os.environ)
    command = [config.claude_command]

    def status() -> dict:
        from directsdk_setup import setup_status
        import subprocess

        s = setup_status(command=command, env=child_env)
        try:
            s["version"] = subprocess.run(command + ["--version"], env=child_env, capture_output=True,
                                          text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            s["version"] = "unknown"
        return s

    def restart() -> None:
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    api_app = create_api_app(rt)
    panel_app = create_panel_app(rt, login_factory=lambda: LoginSession(command, child_env),
                                 status_fn=status, restart_hook=restart)
    api_sock = bind_unix_socket(config.runtime_dir / "api.sock", 0o660, config.client_group or None)
    panel_sock = bind_unix_socket(config.runtime_dir / "panel.sock", 0o600, None)
    asyncio.run(serve_both(api_app, api_sock, panel_app, panel_sock))


if __name__ == "__main__":
    main()
