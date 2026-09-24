from sidecar.env_scrub import scrubbed_env


def test_keeps_only_needed_variables():
    src = {
        "HOME": "/var/lib/claudesub", "PATH": "/usr/bin", "LANG": "C.UTF-8",
        "CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/x/claude",
        "ANTHROPIC_API_KEY": "sk-ant-leak", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "http://evil",
        "ANTHROPIC_FOUNDRY_API_KEY": "f", "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1", "CLAUDE_CODE_OAUTH_TOKEN": "o", "CLAUDE_SIDECAR_API_KEY": "k" * 40,
        "AWS_SECRET_ACCESS_KEY": "a",
    }
    out = scrubbed_env(src)
    assert out == {
        "HOME": "/var/lib/claudesub", "PATH": "/usr/bin", "LANG": "C.UTF-8",
        "CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/x/claude",
        "DISABLE_AUTOUPDATER": "1",
    }


def test_missing_path_gets_safe_default():
    assert scrubbed_env({"HOME": "/h"})["PATH"] == "/usr/local/bin:/usr/bin:/bin"
