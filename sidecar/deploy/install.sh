#!/usr/bin/env bash
set -euo pipefail
# Usage: sudo bash install.sh <fork-commit-sha> <owner-tailnet-login>
COMMIT="${1:?fork commit sha}"
OWNER="${2:?owner tailnet login}"
REPO="https://github.com/codifyrlabs/hermes-plugin-claude-subscription-directsdk.git"

getent group claudesub-clients >/dev/null || groupadd --system claudesub-clients
id claudesub >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/claudesub --shell /usr/sbin/nologin claudesub
chmod 0700 /var/lib/claudesub

install -d -m 0755 /opt/claude-sidecar
if [ ! -d /opt/claude-sidecar/src/.git ]; then
  git clone --quiet "$REPO" /opt/claude-sidecar/src
fi
git -C /opt/claude-sidecar/src fetch --quiet origin
git -C /opt/claude-sidecar/src checkout --quiet --detach "$COMMIT"
[ -x /opt/claude-sidecar/venv/bin/python ] || python3 -m venv /opt/claude-sidecar/venv
/opt/claude-sidecar/venv/bin/pip install --quiet -r /opt/claude-sidecar/src/sidecar/requirements.txt

install -d -m 0700 /etc/claude-sidecar
if [ ! -f /etc/claude-sidecar/env ]; then
  API_KEY="$(openssl rand -hex 32)"
  umask 077
  printf 'CLAUDE_SIDECAR_API_KEY=%s\nCLAUDE_SIDECAR_OWNER_LOGIN=%s\nCLAUDE_SIDECAR_ALLOWED_MODELS=claude-sonnet-5,claude-opus-5-5\n' \
    "$API_KEY" "$OWNER" > /etc/claude-sidecar/env
  echo "wrote /etc/claude-sidecar/env (api key length ${#API_KEY})"
fi
# Panel login. To change the password later, delete both CLAUDE_SIDECAR_PANEL_* lines and re-run.
if ! grep -q '^CLAUDE_SIDECAR_PANEL_PASSWORD_HASH=.' /etc/claude-sidecar/env; then
  echo "Choose the control panel password (at least 16 characters; keep it in your password manager)."
  PANEL_HASH="$(env -C /opt/claude-sidecar/src /opt/claude-sidecar/venv/bin/python -m sidecar.password)"
  SESSION_SECRET="$(openssl rand -hex 32)"
  sed -i '/^CLAUDE_SIDECAR_PANEL_\(PASSWORD_HASH\|SESSION_SECRET\)=/d' /etc/claude-sidecar/env
  printf 'CLAUDE_SIDECAR_PANEL_PASSWORD_HASH=%s\nCLAUDE_SIDECAR_PANEL_SESSION_SECRET=%s\n' \
    "$PANEL_HASH" "$SESSION_SECRET" >> /etc/claude-sidecar/env
  echo "panel login set (hash length ${#PANEL_HASH}, session secret length ${#SESSION_SECRET})"
fi
chmod 0600 /etc/claude-sidecar/env

install -m 0644 /opt/claude-sidecar/src/sidecar/deploy/claude-sidecar.service /etc/systemd/system/claude-sidecar.service
systemctl daemon-reload
echo "installed; start with: systemctl enable --now claude-sidecar"
