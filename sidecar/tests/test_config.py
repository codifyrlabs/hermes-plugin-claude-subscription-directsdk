import pytest

from sidecar.config import ConfigError, SidecarConfig

KEY = "k" * 40


def _env(**over):
    base = {"CLAUDE_SIDECAR_API_KEY": KEY, "CLAUDE_SIDECAR_OWNER_LOGIN": "owner@example.com"}
    base.update(over)
    return base


def test_defaults():
    c = SidecarConfig.from_env(_env())
    assert c.allowed_models == frozenset({"claude-sonnet-5", "claude-opus-5-5"})
    assert c.default_cap_usd == 150.0
    assert str(c.state_dir) == "/var/lib/claudesub"
    assert str(c.runtime_dir) == "/run/claude-sidecar"
    assert c.client_group == "claudesub-clients"
    assert c.claude_command == "claude"


def test_allowed_models_override_is_trimmed():
    c = SidecarConfig.from_env(_env(CLAUDE_SIDECAR_ALLOWED_MODELS=" claude-sonnet-5 , claude-fable-5-1 "))
    assert c.allowed_models == frozenset({"claude-sonnet-5", "claude-fable-5-1"})


@pytest.mark.parametrize("key", ["", "short", "x" * 31])
def test_short_api_key_rejected_without_echo(key):
    with pytest.raises(ConfigError) as exc:
        SidecarConfig.from_env(_env(CLAUDE_SIDECAR_API_KEY=key))
    assert key not in str(exc.value) or key == ""
    assert "32" in str(exc.value)


def test_owner_login_required():
    env = _env()
    del env["CLAUDE_SIDECAR_OWNER_LOGIN"]
    with pytest.raises(ConfigError):
        SidecarConfig.from_env(env)


def test_panel_login_fields_default_to_disabled():
    c = SidecarConfig.from_env(_env())
    assert c.panel_password_hash == "" and c.panel_session_secret == ""


def test_panel_login_fields_read_from_env():
    c = SidecarConfig.from_env(_env(CLAUDE_SIDECAR_PANEL_PASSWORD_HASH="scrypt:16384:8:1:a:b",
                                    CLAUDE_SIDECAR_PANEL_SESSION_SECRET="s" * 64))
    assert c.panel_password_hash == "scrypt:16384:8:1:a:b" and c.panel_session_secret == "s" * 64


@pytest.mark.parametrize("secret", ["short", "s" * 31])
def test_short_session_secret_rejected_without_echo(secret):
    with pytest.raises(ConfigError) as exc:
        SidecarConfig.from_env(_env(CLAUDE_SIDECAR_PANEL_SESSION_SECRET=secret))
    assert secret not in str(exc.value) and "32" in str(exc.value)
