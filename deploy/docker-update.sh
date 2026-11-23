#!/usr/bin/env bash
# `update` inside the Docker container: pull the latest code, refresh dependencies, restart.
# Settings (/data/gremote.env) and data (/data) are never touched.
#
# The update lives in the container until it is recreated. Re-running deploy/docker.sh on the
# host also updates (it rebuilds the image from the latest code and keeps /data).
set -euo pipefail

APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"
APP_BRANCH="${APP_BRANCH:-main}"
INSTALL_DIR="/opt/gremote"
PIDFILE="/run/gremote.pid"

# Everything lives in a function so that pulling a new copy of this very file can't confuse bash.
main() {
  cd "$INSTALL_DIR"
  # The image is built from a plain copy of the source; the first update makes it a checkout.
  [[ -d .git ]] || git init --quiet
  git remote set-url origin "$APP_REPO" 2>/dev/null || git remote add origin "$APP_REPO"
  local before
  before="$(git rev-parse --short HEAD 2>/dev/null || echo "image build")"

  echo ">> Pulling latest code"
  git fetch --quiet --depth 1 origin "$APP_BRANCH"
  git reset --quiet --hard FETCH_HEAD

  echo ">> Installing Python dependencies"
  pip install --quiet --no-cache-dir --disable-pip-version-check --root-user-action=ignore -e .

  echo ">> Restarting gremote"
  if [[ -f "$PIDFILE" ]] && kill -TERM "$(cat "$PIDFILE")" 2>/dev/null; then
    :
  else
    echo "!! Could not find the running app; restart the container: docker restart gremote"
  fi

  echo
  echo "gremote updated"
  echo "  version:  $before -> $(git rev-parse --short HEAD)"
  echo "  settings: /data/gremote.env"
  echo "  data:     /data"
  echo "  logs:     docker logs -f gremote   (on the host)"
}

main "$@"
exit $?
