"""Sidecar tests are Hermes-free: only the fork root (plugin files + vendored shims) is on the path."""
import sys
from pathlib import Path

FORK_ROOT = Path(__file__).resolve().parents[2]
if str(FORK_ROOT) not in sys.path:
    sys.path.insert(0, str(FORK_ROOT))
