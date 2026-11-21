#!/usr/bin/env bash
# Install or update gremote INSIDE the container.
#
#   setup-gremote.sh            install (first run) or update (re-run)
#   setup-gremote.sh update     same; also installed as `update` in the CT
#
# Safe to re-run: .env and the database are never overwritten.
set -euo pipefail

APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"
APP_BRANCH="${APP_BRANCH:-main}"
INSTALL_TAILSCALE="${INSTALL_TAILSCALE:-1}"
INSTALL_DIR="/opt/gremote"

# Everything lives in a function so that pulling a new copy of this very file
# mid-run (during an update) can't confuse bash.
main() {
  local first_run=0
  export DEBIAN_FRONTEND=noninteractive

  if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    first_run=1
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

  # .env with generated secrets on first run
  if [[ ! -f .env ]]; then
    cp .env.example .env
    local secret admin_pw
    secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    admin_pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
    sed -i "s|^GREMOTE_SECRET_KEY=.*|GREMOTE_SECRET_KEY=${secret}|" .env
    sed -i "s|^GREMOTE_ADMIN_PASSWORD=.*|GREMOTE_ADMIN_PASSWORD=${admin_pw}|" .env
    chmod 600 .env
    echo ">> Generated .env (dashboard password: ${admin_pw})"
  fi

  # `update` command inside the container
  install -m 0755 deploy/update.sh /usr/local/bin/update

  # systemd service
  cp deploy/gremote.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable gremote >/dev/null 2>&1
  systemctl restart gremote

  # Tailscale (first install only)
  if [[ "$first_run" -eq 1 && "$INSTALL_TAILSCALE" == "1" ]] && ! command -v tailscale >/dev/null; then
    echo ">> Installing Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh
  fi

  echo
  if [[ "$first_run" -eq 1 ]]; then echo "gremote installed"; else echo "gremote updated"; fi
  echo "  $INSTALL_DIR @ $(git rev-parse --short HEAD)"
}

main "$@"
exit $?
