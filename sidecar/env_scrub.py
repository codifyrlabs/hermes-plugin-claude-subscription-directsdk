"""The `claude` child gets an allowlisted environment, so a request can never fall through to API-key billing."""
from __future__ import annotations

from typing import Mapping

KEEP = ("HOME", "PATH", "LANG", "LC_ALL", "TZ")
KEEP_PREFIXES = ("CLAUDE_SUBSCRIPTION_DIRECTSDK_",)
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


def scrubbed_env(source: Mapping[str, str]) -> dict[str, str]:
    out = {k: v for k, v in source.items() if k in KEEP or k.startswith(KEEP_PREFIXES)}
    out.setdefault("PATH", DEFAULT_PATH)
    out["DISABLE_AUTOUPDATER"] = "1"
    return out
