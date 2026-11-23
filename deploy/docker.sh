#!/usr/bin/env bash
# Install or update gremote in Docker (Docker Desktop on a Mac, or Docker on Linux).
# Run on the machine that runs Docker:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy/docker.sh)"
#   bash deploy/docker.sh               (from a clone of the repo: builds that copy as it is)
#
# Re-running it updates: rebuilds the image from the latest code and recreates the container.
# Inside the container, `update` does the same without leaving the shell.
# Settings (/data/gremote.env) and data live in the gremote-data volume and are always kept.
#
#   PORT                8000            Port on this machine
#   GREMOTE_PASSWORD    generated       Dashboard password (first install only)
#   GREMOTE_SCAN_SUBNET this LAN        Subnets the TV scan probes (Mac: detected automatically)
#   GREMOTE_DIR         ~/gremote       Where the code is cloned when run through curl
#   NAME / VOLUME       gremote / gremote-data
set -euo pipefail

APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"
APP_BRANCH="${APP_BRANCH:-main}"
PORT="${PORT:-8000}"
NAME="${NAME:-gremote}"
VOLUME="${VOLUME:-gremote-data}"
IMAGE="${IMAGE:-gremote}"
GREMOTE_DIR="${GREMOTE_DIR:-$HOME/gremote}"

# The LAN this machine is on, as a CIDR (macOS). Docker Desktop containers cannot see it.
mac_lan_subnet() {
  local iface ip mask bits=0 a b c d m1 m2 m3 m4 i
  iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
  [[ -n "$iface" ]] || return 0
  ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
  mask="$(ifconfig "$iface" 2>/dev/null | awk '/inet /{print $4; exit}')"   # e.g. 0xffffff00
  [[ -n "$ip" && "$mask" == 0x* ]] || return 0
  mask=$((mask))
  for ((i = 31; i >= 0; i--)); do (( (mask >> i) & 1 )) && bits=$((bits + 1)) || break; done
  (( bits >= 22 )) || bits=24          # keep the sweep small on big networks
  IFS=. read -r a b c d <<< "$ip"
  mask=$(( (0xffffffff << (32 - bits)) & 0xffffffff ))
  m1=$(( (mask >> 24) & 255 )); m2=$(( (mask >> 16) & 255 )); m3=$(( (mask >> 8) & 255 )); m4=$(( mask & 255 ))
  echo "$((a & m1)).$((b & m2)).$((c & m3)).$((d & m4))/${bits}"
}

main() {
  command -v docker >/dev/null || { echo "Docker is not installed."; return 1; }
  docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop and try again."; return 1; }

  # Source: the clone this script sits in, else a clone in $GREMOTE_DIR (cloned or pulled).
  local here src
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd || true)"
  if [[ -n "$here" && -f "$here/../Dockerfile" && -f "$here/docker-entrypoint.sh" ]]; then
    src="$(cd "$here/.." && pwd)"
    echo ">> Building from $src"
  else
    src="$GREMOTE_DIR"
    if [[ -d "$src/.git" ]]; then
      echo ">> Pulling latest code into $src"
      git -C "$src" fetch --quiet origin "$APP_BRANCH"
      git -C "$src" merge --ff-only "origin/$APP_BRANCH" \
        || { echo "!! Local changes in $src block a fast-forward. Check: git -C $src status"; return 1; }
    else
      echo ">> Cloning into $src"
      git clone --quiet --branch "$APP_BRANCH" "$APP_REPO" "$src"
    fi
  fi

  echo ">> Building the image"
  docker build --quiet -t "$IMAGE" "$src" >/dev/null

  local args=(-d --name "$NAME" --restart unless-stopped -v "$VOLUME:/data")
  local mode url_host="localhost" inner_port=8000
  if [[ "$(uname -s)" == "Linux" ]] && ! docker info 2>/dev/null | grep -qi "docker desktop"; then
    # Same network as the host: SSDP discovery and Wake-on-LAN broadcasts work.
    mode="host network"
    args+=(--network host -e "GREMOTE_PORT=$PORT")
    inner_port="$PORT"
    url_host="$(hostname -I 2>/dev/null | awk '{print $1}')"
  else
    # Docker Desktop: the container sits behind a NAT, so tell the TV scan which LAN to sweep.
    mode="port $PORT"
    args+=(-p "$PORT:8000")
    local subnet="${GREMOTE_SCAN_SUBNET:-}"
    [[ -n "$subnet" || "$(uname -s)" != "Darwin" ]] || subnet="$(mac_lan_subnet)"
    if [[ -n "$subnet" ]]; then
      args+=(-e "GREMOTE_SCAN_SUBNET=$subnet")
      echo ">> TV scan will sweep $subnet"
    fi
  fi
  [[ -n "${GREMOTE_SCAN_SUBNET:-}" && "$mode" == "host network" ]] \
    && args+=(-e "GREMOTE_SCAN_SUBNET=$GREMOTE_SCAN_SUBNET")

  # First run with the new image: keep the secret/password of an older container (or the
  # password asked for). They are written to /data/gremote.env on first start.
  local fresh=1
  if docker run --rm --entrypoint test -v "$VOLUME:/data" "$IMAGE" -f /data/gremote.env; then
    fresh=0
  else
    local var line
    if docker container inspect "$NAME" >/dev/null 2>&1; then
      for var in GREMOTE_SECRET_KEY GREMOTE_ADMIN_PASSWORD GREMOTE_SESSION_TTL_SECONDS; do
        line="$(docker container inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' \
                | grep "^${var}=" || true)"
        [[ -n "$line" ]] && args+=(-e "$line") && fresh=0
      done
    fi
    [[ -n "${GREMOTE_PASSWORD:-}" ]] && args+=(-e "GREMOTE_ADMIN_PASSWORD=$GREMOTE_PASSWORD")
  fi

  if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo ">> Replacing the $NAME container (data and settings are kept)"
    docker rm -f "$NAME" >/dev/null
  fi
  docker run "${args[@]}" "$IMAGE" >/dev/null

  echo ">> Waiting for gremote to start"
  local i
  for i in $(seq 1 30); do
    docker exec "$NAME" python -c "import urllib.request; urllib.request.urlopen(
      'http://127.0.0.1:${inner_port}/static/manifest.webmanifest', timeout=2)" >/dev/null 2>&1 && break
    sleep 1
  done

  echo
  if [[ "$fresh" -eq 1 ]]; then echo "gremote installed"; else echo "gremote updated"; fi
  echo "  running at: http://${url_host:-localhost}:${PORT}   ($mode)"
  echo "  password:   docker exec $NAME grep ADMIN_PASSWORD /data/gremote.env"
  echo "  update:     docker exec -it $NAME update   (or run this script again)"
  echo "  shell:      docker exec -it $NAME bash"
  echo "  logs:       docker logs -f $NAME"
}

main "$@"
exit $?
