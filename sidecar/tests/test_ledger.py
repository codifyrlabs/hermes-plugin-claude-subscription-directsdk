import json
import os
from datetime import datetime, timezone

import pytest

from sidecar.ledger import LedgerCorrupt, MAX_CAP_USD, SpendLedger


class Clock:
    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when


def _ledger(tmp_path, when=datetime(2026, 9, 23, tzinfo=timezone.utc)):
    clock = Clock(when)
    return SpendLedger(tmp_path / "ledger.json", 150.0, now=clock), clock


def test_fresh_ledger_defaults(tmp_path):
    ledger, _ = _ledger(tmp_path)
    s = ledger.state()
    assert (s.month, s.spent_usd, s.cap_usd, s.paused, s.unknown_cost_requests) == ("2026-09", 0.0, 150.0, False, 0)
    assert ledger.blocked_reason() is None


def test_add_accumulates_and_blocks_at_cap(tmp_path):
    ledger, _ = _ledger(tmp_path)
    ledger.add(149.99, {"model": "claude-sonnet-5"})
    assert ledger.blocked_reason() is None
    ledger.add(0.01, {"model": "claude-sonnet-5"})
    assert ledger.blocked_reason() == "monthly_cap"
    assert ledger.state().last_request["model"] == "claude-sonnet-5"


@pytest.mark.parametrize("bad", [None, "1.0", -1, float("nan"), float("inf"), True])
def test_unknown_cost_is_counted_not_added(tmp_path, bad):
    ledger, _ = _ledger(tmp_path)
    ledger.add(bad, {"model": "m"})
    s = ledger.state()
    assert s.spent_usd == 0.0 and s.unknown_cost_requests == 1


def test_month_rollover_keeps_cap_and_pause(tmp_path):
    ledger, clock = _ledger(tmp_path)
    ledger.set_cap(120.0)
    ledger.add(120.0, {"model": "m"})
    ledger.set_paused(True)
    clock.when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    s = ledger.state()
    assert (s.month, s.spent_usd, s.cap_usd, s.paused) == ("2026-10", 0.0, 120.0, True)
    assert ledger.blocked_reason() == "paused"


def test_pause_wins_over_cap(tmp_path):
    ledger, _ = _ledger(tmp_path)
    ledger.add(150.0, {})
    ledger.set_paused(True)
    assert ledger.blocked_reason() == "paused"


@pytest.mark.parametrize("cap", [-1, MAX_CAP_USD + 0.01, float("nan")])
def test_cap_bounds(tmp_path, cap):
    ledger, _ = _ledger(tmp_path)
    with pytest.raises(ValueError):
        ledger.set_cap(cap)


@pytest.mark.parametrize("content", ["", "{", "[]", '{"month": 5}', '{"month":"2026-09","spent_usd":"x","cap_usd":150,"paused":false}'])
def test_corrupt_ledger_fails_closed(tmp_path, content):
    (tmp_path / "ledger.json").write_text(content)
    ledger, _ = _ledger(tmp_path)
    with pytest.raises(LedgerCorrupt):
        ledger.state()
    with pytest.raises(LedgerCorrupt):
        ledger.blocked_reason()
    with pytest.raises(LedgerCorrupt):
        ledger.add(1.0, {})
    assert (tmp_path / "ledger.json").read_text() == content


def test_writes_are_atomic_and_private(tmp_path):
    ledger, _ = _ledger(tmp_path)
    ledger.add(1.5, {"model": "m"})
    path = tmp_path / "ledger.json"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text())["spent_usd"] == 1.5
    assert not list(tmp_path.glob("*.tmp"))


def test_unreadable_ledger_file_fails_closed(tmp_path):
    (tmp_path / "ledger.json").mkdir()  # reading raises IsADirectoryError (an OSError), whoever runs the test
    ledger, _ = _ledger(tmp_path)
    with pytest.raises(LedgerCorrupt):
        ledger.state()
    with pytest.raises(LedgerCorrupt):
        ledger.blocked_reason()
