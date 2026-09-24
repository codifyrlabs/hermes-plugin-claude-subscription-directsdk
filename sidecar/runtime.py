# sidecar/runtime.py
"""Shared state for the API and the panel: config, ledger, the one long-lived plugin Client, the busy gate."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import SidecarConfig
from .env_scrub import scrubbed_env
from .ledger import SpendLedger


class Gate:
    """Concurrency 1: a second request is refused immediately, never queued."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.busy, self.model, self.started_at = False, None, None

    def try_acquire(self, model: str) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy, self.model, self.started_at = True, model, time.time()
            return True

    def release(self) -> None:
        with self._lock:
            self.busy, self.model, self.started_at = False, None, None


@dataclass
class Runtime:
    config: SidecarConfig
    ledger: SpendLedger
    client: Any
    gate: Gate = field(default_factory=Gate)


def build_runtime(config: SidecarConfig, client: Any = None, env: Mapping[str, str] | None = None) -> Runtime:
    if client is None:
        import directsdk

        client = directsdk.Client(command=config.claude_command, env=scrubbed_env(env or os.environ),
                                  timeout=config.request_timeout)
    ledger = SpendLedger(config.state_dir / "sidecar-ledger.json", config.default_cap_usd)
    return Runtime(config=config, ledger=ledger, client=client)
