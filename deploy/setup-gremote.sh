#!/usr/bin/env bash
# Install the gremote app INSIDE the container (run within the LXC).
set -euo pipefail

APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"
INSTALL_DIR="/opt/gremote"

apt update
apt install -y python3-venv python3-pip git curl

# clone or update
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  mkdir -p "$INSTALL_DIR"
  git clone "$APP_REPO" "$INSTALL_DIR"
else
  (cd "$INSTALL_DIR" && git pull)
fi

cd "$INSTALL_DIR"

# python venv + deps
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -e .

# .env (edit it after first run)
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "!! Created .env — edit it and set GREMOTE_SECRET_KEY + GREMOTE_ADMIN_PASSWORD !!"
fi

# systemd service
cp deploy/gremote.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gremote

echo
echo "gremote installed at $INSTALL_DIR"
echo "On first run, edit $INSTALL_DIR/.env then: systemctl restart gremote"
