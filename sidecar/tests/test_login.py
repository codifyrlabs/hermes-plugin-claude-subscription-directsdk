import sys
from pathlib import Path

import pytest

from sidecar.login import LoginError, LoginSession, valid_code

FAKE = Path(__file__).with_name("fake_claude.py")


def _session():
    return LoginSession([sys.executable, str(FAKE)], {"PATH": "/usr/bin", "HOME": "/tmp"}, url_timeout=10)


def test_start_returns_clean_url_and_good_code_succeeds():
    s = _session()
    try:
        assert s.start() == "https://claude.ai/oauth/authorize?code=true&state=abc"
        assert s.submit("good-code#state") is True
    finally:
        s.close()


def test_bad_code_fails():
    s = _session()
    try:
        s.start()
        assert s.submit("bad-code") is False
    finally:
        s.close()


@pytest.mark.parametrize("code", ["", "a b", "x\n", "\x1b[A", "é", "x" * 2049])
def test_invalid_codes_rejected(code):
    assert not valid_code(code)
    s = _session()
    try:
        s.start()
        with pytest.raises(LoginError):
            s.submit(code)
    finally:
        s.close()


def test_no_url_times_out():
    s = LoginSession([sys.executable, "-c", "import time; time.sleep(5)"], {"PATH": "/usr/bin"}, url_timeout=0.5)
    try:
        with pytest.raises(LoginError):
            s.start()
    finally:
        s.close()
