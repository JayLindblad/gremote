#!/usr/bin/env bash
# Install or update gremote as a systemd service on a Debian/Ubuntu machine
# (LXC container, VM or bare metal). Run as root.
#
#   bash deploy/install.sh          install, or update if already installed
#
# Also installed as `update` inside the container. Safe to re-run: the settings
# in /etc/gremote.env and the data in /var/lib/gremote are never overwritten.
# Tailscale is never installed, touched or restarted by this script.
set -euo pipefail

APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"
APP_BRANCH="${APP_BRANCH:-main}"
PORT="${PORT:-8000}"
GREMOTE_PASSWORD="${GREMOTE_PASSWORD:-}"
INSTALL_DIR="/opt/gremote"
ENV_FILE="/etc/gremote.env"
DATA_DIR="/var/lib/gremote"

# Everything lives in a function so that pulling a new copy of this very file
# mid-run (during an update) can't confuse bash.
main() {
  [[ "$(id -u)" -eq 0 ]] || { echo "Run as root."; return 1; }
  local fresh=0
  export DEBIAN_FRONTEND=noninteractive

  if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    fresh=1
    echo ">> Installing OS packages"
    apt-get update -qq
    apt-get install -y -qq python3-venv python3-pip git curl ca-certificates
    git clone --branch "$APP_BRANCH" "$APP_REPO" "$INSTALL_DIR"
  else
    echo ">> Pulling latest code"
    git -C "$INSTALL_DIR" fetch --quiet origin "$APP_BRANCH"
    git -C "$INSTALL_DIR" merge --ff-only "origin/$APP_BRANCH" \
      || { echo "!! Local changes in $INSTALL_DIR block a fast-forward. Check: git -C $INSTALL_DIR status"; return 1; }
  fi

  cd "$INSTALL_DIR"

  echo ">> Installing Python dependencies"
  [[ -d .venv ]] || python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .

  mkdir -p "$DATA_DIR"

  # Settings file with generated secrets, first install only
  if [[ ! -f "$ENV_FILE" ]]; then
    local secret password
    secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    password="${GREMOTE_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')}"
    umask 077
    cat > "$ENV_FILE" <<EOF
# gremote settings. After editing: systemctl restart gremote
GREMOTE_SECRET_KEY=${secret}
GREMOTE_ADMIN_PASSWORD=${password}
GREMOTE_DATABASE_PATH=${DATA_DIR}/gremote.db
GREMOTE_HOST=0.0.0.0
GREMOTE_PORT=${PORT}
EOF
    echo ">> Created $ENV_FILE (dashboard password: ${password})"
  fi

  # `update` command
  rm -f /usr/local/bin/update   # old location; not on PATH under `pct enter`
  cat > /usr/bin/update <<EOF
#!/usr/bin/env bash
exec bash $INSTALL_DIR/deploy/install.sh
EOF
  chmod 0755 /usr/bin/update

  cp deploy/gremote.service /etc/systemd/system/gremote.service
  systemctl daemon-reload
  systemctl enable gremote >/dev/null 2>&1
  systemctl restart gremote

  echo
  if [[ "$fresh" -eq 1 ]]; then echo "gremote installed"; else echo "gremote updated"; fi
  echo "  version:  $(git rev-parse --short HEAD)"
  echo "  settings: $ENV_FILE"
  echo "  data:     $DATA_DIR"
  echo "  logs:     journalctl -u gremote -f"
}

main "$@"
exit $?
