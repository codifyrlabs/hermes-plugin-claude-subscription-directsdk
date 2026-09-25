from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _lines(name):
    return [l.strip() for l in (DEPLOY / name).read_text().splitlines() if l.strip() and not l.strip().startswith("#")]


def test_unit_hardening_matches_spec():
    lines = _lines("claude-sidecar.service")
    for required in (
        "User=claudesub", "Group=claudesub", "SupplementaryGroups=claudesub-clients",
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "NoNewPrivileges=yes", "LimitCORE=0",
        "CPUQuota=100%", "MemoryMax=1G", "TasksMax=256",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "SystemCallFilter=@system-service", "UMask=0077",
        "ReadWritePaths=/var/lib/claudesub", "RuntimeDirectory=claude-sidecar", "RuntimeDirectoryMode=0711",
        "Restart=always", "EnvironmentFile=/etc/claude-sidecar/env", "Environment=HOME=/var/lib/claudesub",
    ):
        assert required in lines, required


def test_unit_never_passes_api_key_on_command_line():
    text = (DEPLOY / "claude-sidecar.service").read_text()
    assert "CLAUDE_SIDECAR_API_KEY" not in text


def test_install_script_is_strict_and_prints_lengths_only():
    text = (DEPLOY / "install.sh").read_text()
    assert text.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
    assert "chmod 0600 /etc/claude-sidecar/env" in text
    assert "echo \"$API_KEY\"" not in text and "cat /etc/claude-sidecar/env" not in text
    assert "${#API_KEY}" in text


def test_install_script_sets_panel_login_without_printing_it():
    text = (DEPLOY / "install.sh").read_text()
    assert "-m sidecar.password" in text and "openssl rand -hex 32" in text
    assert "${#PANEL_HASH}" in text and "${#SESSION_SECRET}" in text
    assert 'echo "$PANEL_HASH"' not in text and 'echo "$SESSION_SECRET"' not in text


def test_env_example_has_no_real_values():
    lines = _lines("env.example")
    assert "CLAUDE_SIDECAR_API_KEY=" in lines
    assert "CLAUDE_SIDECAR_PANEL_PASSWORD_HASH=" in lines and "CLAUDE_SIDECAR_PANEL_SESSION_SECRET=" in lines
    assert all(l.endswith("=") or "@" in l or l.startswith("CLAUDE_SIDECAR_ALLOWED_MODELS=") for l in lines)


def test_install_script_warns_when_owner_login_differs():
    text = (DEPLOY / "install.sh").read_text()
    assert "sed -n 's/^CLAUDE_SIDECAR_OWNER_LOGIN=//p' /etc/claude-sidecar/env" in text
    assert '"$CURRENT_OWNER" != "$OWNER"' in text and "WARNING" in text


def test_install_script_checks_both_panel_login_lines():
    text = (DEPLOY / "install.sh").read_text()
    assert "grep -q '^CLAUDE_SIDECAR_PANEL_PASSWORD_HASH=.' /etc/claude-sidecar/env" in text
    assert "grep -q '^CLAUDE_SIDECAR_PANEL_SESSION_SECRET=.' /etc/claude-sidecar/env" in text
