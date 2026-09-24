# sidecar/tests/test_upstream_integrity.py
"""Upstream plugin files stay byte-identical to the pinned commit, so `git merge upstream` stays clean.

On a deliberate upstream merge: update the hashes in the same commit as the merge, after the suite passes.
"""
import hashlib

import pytest

from .conftest import FORK_ROOT

UPSTREAM_COMMIT = "602393b6d3ea148bc618cd23da1a13fa332e1433"
UPSTREAM_SHA256 = {
    "directsdk.py": "8bc967bc084dfd0968e4e93894d8ac791bb407f562f07e113003af55b2c8644b",
    "admission.py": "3a1b30cd15017ae5faa9424d32280c4194e5a280d632884da80c029f36ee846c",
    "model_catalog.py": "3cbb588caa3838aa2e5aa9039fc8c52e6f58a84ea80702aa2a413fcd10a34ff7",
    "inert_mcp.py": "bf20a09b2358600981b115c32845ec24722cb66adfc18d247d1c35ed999a0e0a",
    "directsdk_setup.py": "0e78787d7771ca83e81c4e2d6f9b224bcf7bae175441c08d7aef2f50864084ab",
}


@pytest.mark.parametrize("name,expected", sorted(UPSTREAM_SHA256.items()))
def test_upstream_file_is_byte_identical(name, expected):
    digest = hashlib.sha256((FORK_ROOT / name).read_bytes()).hexdigest()
    assert digest == expected, f"{name} differs from upstream {UPSTREAM_COMMIT}; never edit upstream files"
