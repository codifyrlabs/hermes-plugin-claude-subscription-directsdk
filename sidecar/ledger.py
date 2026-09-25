"""Month-to-date spend (UTC calendar months) from the plugin's native_cost estimate. Fails closed on corruption."""
from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MAX_CAP_USD = 200.0


class LedgerCorrupt(RuntimeError):
    pass


@dataclass
class LedgerState:
    month: str
    spent_usd: float
    cap_usd: float
    paused: bool
    unknown_cost_requests: int = 0
    last_request: dict | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_cost(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


class SpendLedger:
    def __init__(self, path: Path, default_cap_usd: float, now: Callable[[], datetime] = utc_now):
        self.path, self.default_cap_usd, self.now = Path(path), float(default_cap_usd), now
        self._lock = threading.Lock()

    def _month(self) -> str:
        return self.now().strftime("%Y-%m")

    def _load(self) -> LedgerState:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return LedgerState(self._month(), 0.0, self.default_cap_usd, False)
        except OSError as exc:
            raise LedgerCorrupt(f"ledger unreadable: {type(exc).__name__}") from None
        try:
            raw = json.loads(text)
            state = LedgerState(
                month=raw["month"], spent_usd=raw["spent_usd"], cap_usd=raw["cap_usd"], paused=raw["paused"],
                unknown_cost_requests=raw.get("unknown_cost_requests", 0), last_request=raw.get("last_request"),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise LedgerCorrupt(f"ledger unreadable: {type(exc).__name__}") from None
        if not (isinstance(state.month, str) and _is_cost(state.spent_usd) and _is_cost(state.cap_usd)
                and isinstance(state.paused, bool) and isinstance(state.unknown_cost_requests, int)):
            raise LedgerCorrupt("ledger unreadable: bad field types")
        if state.month != self._month():
            state = LedgerState(self._month(), 0.0, state.cap_usd, state.paused)
        return state

    def _save(self, state: LedgerState) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(state), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def state(self) -> LedgerState:
        with self._lock:
            return self._load()

    def blocked_reason(self) -> str | None:
        s = self.state()
        if s.paused:
            return "paused"
        if s.spent_usd >= s.cap_usd:
            return "monthly_cap"
        return None

    def add(self, cost_usd: object, meta: dict) -> None:
        with self._lock:
            s = self._load()
            if _is_cost(cost_usd):
                s.spent_usd = round(s.spent_usd + float(cost_usd), 6)
            else:
                s.unknown_cost_requests += 1
            s.last_request = {**meta, "at": self.now().isoformat(timespec="seconds")}
            self._save(s)

    def set_cap(self, cap_usd: float) -> None:
        if not (_is_cost(cap_usd) and cap_usd <= MAX_CAP_USD):
            raise ValueError(f"cap must be between 0 and {MAX_CAP_USD}")
        with self._lock:
            s = self._load()
            s.cap_usd = float(cap_usd)
            self._save(s)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            s = self._load()
            s.paused = bool(paused)
            self._save(s)
