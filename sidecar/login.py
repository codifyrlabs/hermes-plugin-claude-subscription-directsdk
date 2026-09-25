"""Drives `claude auth login` on a pty: shows the URL, then feeds the code the owner pasted. Never logs either."""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
URL = re.compile(r"https://[^\s\x1b\x07]+")
CODE = re.compile(r"^[\x21-\x7e]{1,2048}$")
# A wide window, so the CLI never wraps the long OAuth URL (URL stops at the first line break).
PTY_ROWS, PTY_COLS = 50, 500


class LoginError(RuntimeError):
    pass


def valid_code(code: str) -> bool:
    return bool(CODE.fullmatch(code or ""))


class LoginSession:
    term_grace = 5.0  # seconds to wait after SIGTERM (and again after SIGKILL)

    def __init__(self, command: list[str], env: dict[str, str], url_timeout: float = 30.0):
        self.command, self.env, self.url_timeout = list(command), dict(env), url_timeout
        self.proc: subprocess.Popen | None = None
        self.master: int | None = None

    def _read(self, deadline: float) -> str:
        chunks = []
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if not ready:
                if self.proc.poll() is not None:
                    break
                if chunks:
                    return "".join(chunks)
                continue
            try:
                data = os.read(self.master, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", "replace"))
        return "".join(chunks)

    def start(self) -> str:
        master, slave = pty.openpty()
        self.master = master
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        self.proc = subprocess.Popen(self.command + ["auth", "login"], stdin=slave, stdout=slave, stderr=slave,
                                     env=self.env, start_new_session=True, close_fds=True)
        os.close(slave)
        deadline, seen = time.monotonic() + self.url_timeout, ""
        while time.monotonic() < deadline:
            seen += self._read(deadline)
            match = URL.search(ANSI.sub("", seen))
            if match:
                return match.group(0)
            if self.proc.poll() is not None:
                break
        self.close()
        raise LoginError("claude auth login did not show a URL")

    def submit(self, code: str, timeout: float = 60.0) -> bool:
        if not valid_code(code):
            raise LoginError("code has invalid characters or length")
        if self.proc is None or self.master is None:
            raise LoginError("login not started")
        os.write(self.master, (code + "\r").encode())
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None and time.monotonic() < deadline:
            self._read(min(deadline, time.monotonic() + 0.5))
        ok = self.proc.poll() == 0
        self.close()
        return ok

    def close(self) -> None:
        try:
            if self.proc is not None and self.proc.poll() is None:
                self._signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=self.term_grace)
                except subprocess.TimeoutExpired:
                    self._signal(signal.SIGKILL)
                    self.proc.wait(timeout=self.term_grace)
        finally:
            if self.master is not None:
                try:
                    os.close(self.master)
                except OSError:
                    pass
                self.master = None

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass
