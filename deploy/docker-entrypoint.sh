#!/usr/bin/env bash
# Container entrypoint: keeps gremote running, and restarts it when `update` stops it.
#
# Settings live in /data/gremote.env (the Docker counterpart of the LXC's /etc/gremote.env),
# created on first start with generated secrets, or with the values the container was given.
# Variables passed to the container (docker run -e / env_file) take precedence over the file.
set -uo pipefail

ENV_FILE="/data/gremote.env"
PIDFILE="/run/gremote.pid"

# Everything lives in a function so that `update` rewriting this file mid-run can't confuse bash.
main() {
  mkdir -p /data

  if [[ ! -f "$ENV_FILE" ]]; then
    local secret password generated=""
    secret="${GREMOTE_SECRET_KEY:-$(python -c 'import secrets; print(secrets.token_urlsafe(32))')}"
    if [[ -n "${GREMOTE_ADMIN_PASSWORD:-}" ]]; then
      password="$GREMOTE_ADMIN_PASSWORD"
    else
      password="$(python -c 'import secrets; print(secrets.token_urlsafe(12))')"
      generated=1
    fi
    (
      umask 077
      {
        echo "# gremote settings. After editing: docker restart gremote"
        echo "GREMOTE_SECRET_KEY=${secret}"
        echo "GREMOTE_ADMIN_PASSWORD=${password}"
        [[ -n "${GREMOTE_SESSION_TTL_SECONDS:-}" ]] \
          && echo "GREMOTE_SESSION_TTL_SECONDS=${GREMOTE_SESSION_TTL_SECONDS}"
      } > "$ENV_FILE"
    )
    echo ">> Created $ENV_FILE"
    [[ -n "$generated" ]] && echo ">> Dashboard password: ${password}"
  fi

  # Load the file, without overriding anything the container was started with.
  local key value
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    [[ -n "${!key+set}" ]] || export "$key=$value"
  done < "$ENV_FILE"

  local child="" stopping=0
  trap 'stopping=1; [[ -n "$child" ]] && kill -TERM "$child" 2>/dev/null' TERM INT
  cd /opt/gremote
  while :; do
    uvicorn app.main:app --host 0.0.0.0 --port "${GREMOTE_PORT:-8000}" &
    child=$!
    echo "$child" > "$PIDFILE"
    wait "$child"            # returns early when a signal arrives ...
    wait "$child" 2>/dev/null  # ... so wait for the app to actually exit
    [[ "$stopping" -eq 1 ]] && exit 0
    echo ">> gremote stopped; starting it again"
    sleep 1
  done
}

main "$@"
