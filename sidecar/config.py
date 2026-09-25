"""Sidecar settings from the environment (systemd EnvironmentFile). Secrets are never echoed."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .ledger import MAX_CAP_USD

DEFAULT_MODELS = frozenset({"claude-sonnet-5", "claude-opus-5-5"})
MIN_KEY_LENGTH = 32


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SidecarConfig:
    api_key: str
    owner_login: str
    state_dir: Path = Path("/var/lib/claudesub")
    runtime_dir: Path = Path("/run/claude-sidecar")
    claude_command: str = "claude"
    allowed_models: frozenset[str] = DEFAULT_MODELS
    default_cap_usd: float = 150.0
    client_group: str = "claudesub-clients"
    request_timeout: int = 600
    panel_password_hash: str = ""
    panel_session_secret: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SidecarConfig":
        key = env.get("CLAUDE_SIDECAR_API_KEY", "")
        if len(key) < MIN_KEY_LENGTH:
            raise ConfigError(f"CLAUDE_SIDECAR_API_KEY must be at least {MIN_KEY_LENGTH} characters (got {len(key)})")
        owner = env.get("CLAUDE_SIDECAR_OWNER_LOGIN", "").strip()
        if not owner:
            raise ConfigError("CLAUDE_SIDECAR_OWNER_LOGIN is required")
        secret = env.get("CLAUDE_SIDECAR_PANEL_SESSION_SECRET", "")
        if secret and len(secret) < MIN_KEY_LENGTH:
            raise ConfigError(f"CLAUDE_SIDECAR_PANEL_SESSION_SECRET must be at least {MIN_KEY_LENGTH} characters "
                              f"(got {len(secret)})")
        try:
            cap = float(env.get("CLAUDE_SIDECAR_DEFAULT_CAP_USD", "150"))
        except ValueError:
            cap = math.nan
        if not (math.isfinite(cap) and 0 <= cap <= MAX_CAP_USD):
            raise ConfigError(f"CLAUDE_SIDECAR_DEFAULT_CAP_USD must be a number from 0 to {MAX_CAP_USD:.0f}")
        models_raw = env.get("CLAUDE_SIDECAR_ALLOWED_MODELS", "")
        models = frozenset(m.strip() for m in models_raw.split(",") if m.strip()) or DEFAULT_MODELS
        return cls(
            api_key=key,
            owner_login=owner,
            state_dir=Path(env.get("CLAUDE_SIDECAR_STATE_DIR", "/var/lib/claudesub")),
            runtime_dir=Path(env.get("CLAUDE_SIDECAR_RUNTIME_DIR", "/run/claude-sidecar")),
            claude_command=env.get("CLAUDE_SIDECAR_CLAUDE_COMMAND", "claude"),
            allowed_models=models,
            default_cap_usd=cap,
            client_group=env.get("CLAUDE_SIDECAR_CLIENT_GROUP", "claudesub-clients"),
            panel_password_hash=env.get("CLAUDE_SIDECAR_PANEL_PASSWORD_HASH", "").strip(),
            panel_session_secret=secret,
        )
