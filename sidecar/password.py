"""scrypt password hashes for the panel login.

Format: scrypt:N:r:p:<salt>:<key>, base64url without padding. There is no `$` or `=`, so the systemd
EnvironmentFile, shells and printf leave it alone. `python -m sidecar.password` prompts twice and
prints only the hash.
"""
from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import os
import sys
from typing import Callable

N, R, P, KEY_BYTES, SALT_BYTES = 2**14, 8, 1, 32, 16
MAX_N, MAX_R, MAX_P = 2**15, 16, 4
MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 16


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=length)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = salt or os.urandom(SALT_BYTES)
    return f"scrypt:{N}:{R}:{P}:{_b64(salt)}:{_b64(_derive(password, salt, N, R, P, KEY_BYTES))}"


def verify_password(password: str, encoded: str) -> bool:
    """False for a wrong password and for any malformed hash, so a bad env file fails closed."""
    try:
        scheme, n_s, r_s, p_s, salt_s, key_s = (encoded or "").split(":")
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt, expected = _unb64(salt_s), _unb64(key_s)
    except (ValueError, binascii.Error):
        return False
    if scheme != "scrypt" or not (2 <= n <= MAX_N and n & (n - 1) == 0 and 1 <= r <= MAX_R and 1 <= p <= MAX_P):
        return False
    if len(salt) < 8 or not 16 <= len(expected) <= 64:
        return False
    try:
        actual = _derive(password, salt, n, r, p, len(expected))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def main(*, read: Callable[[str], str] = getpass.getpass) -> int:
    first = read("Panel password: ")
    if first != read("Again: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    try:
        print(hash_password(first))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
