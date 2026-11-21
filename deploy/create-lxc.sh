#!/usr/bin/env bash
# gremote LXC installer / updater for Proxmox VE (run as root on the PVE host).
#
# Install (one command):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy/create-lxc.sh)"
#
# Update an existing container (pulls latest code, restarts the service):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy/create-lxc.sh)" -- update [CTID]
# or from inside the container:  update
#
# Override defaults with env vars, e.g.:
#   CTID=112 RAM=1024 STORAGE=local-zfs IP=192.168.1.50/24 GATEWAY=192.168.1.1 bash -c "..."

set -euo pipefail

RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy}"
APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"
APP_BRANCH="${APP_BRANCH:-main}"

[[ "$(id -u)" -eq 0 ]] || { echo "Please run as root on the Proxmox host."; exit 1; }
command -v pct >/dev/null || { echo "pct not found - run this on a Proxmox VE host."; exit 1; }

# ---- update mode ------------------------------------------------------------
if [[ "${1:-}" == "update" ]]; then
  CTID="${2:-${CTID:-}}"
  if [[ -z "$CTID" ]]; then
    # find the container by hostname
    CTID="$(pct list | awk '$NF=="gremote"{print $1; exit}')"
  fi
  [[ -n "$CTID" ]] || { echo "Could not find a 'gremote' container. Pass the CTID: ... update 110"; exit 1; }
  echo "Updating container $CTID..."
  pct exec "$CTID" -- env APP_REPO="$APP_REPO" APP_BRANCH="$APP_BRANCH" bash /opt/gremote/deploy/setup-gremote.sh update
  exit 0
fi

# ---- install mode -----------------------------------------------------------
# NB: don't name this HOSTNAME - bash sets that to the PVE host's name.
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${CT_HOSTNAME:-gremote}"
CORES="${CORES:-1}"
RAM="${RAM:-512}"                           # MB
DISK="${DISK:-4}"                           # GB
STORAGE="${STORAGE:-local-lvm}"             # storage for the rootfs
VZTMPL_STORAGE="${VZTMPL_STORAGE:-local}"   # storage holding templates
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"                            # or e.g. 192.168.1.50/24
GATEWAY="${GATEWAY:-}"                      # required only for a static IP

pct status "$CTID" >/dev/null 2>&1 && { echo "CTID $CTID already exists. Set CTID=<free id>."; exit 1; }

# 1. Debian template (download if missing)
echo ">> Looking for a Debian template"
pveam update >/dev/null
ARCH="$(dpkg --print-architecture)"   # amd64 / arm64 - template must match the host
# Prefer Debian 12; fall back to 13 only if 12 isn't offered.
TEMPLATE_FILE="$(pveam available --section system | awk -v a="_${ARCH}." '$2 ~ /^debian-12-standard/ && index($2,a){print $2}' | sort -V | tail -1)"
[[ -n "$TEMPLATE_FILE" ]] || TEMPLATE_FILE="$(pveam available --section system | awk -v a="_${ARCH}." '$2 ~ /^debian-13-standard/ && index($2,a){print $2}' | sort -V | tail -1)"
[[ -n "$TEMPLATE_FILE" ]] || { echo "Could not find a Debian template for ${ARCH}."; exit 1; }
if ! pveam list "$VZTMPL_STORAGE" | grep -q "$TEMPLATE_FILE"; then
  pveam download "$VZTMPL_STORAGE" "$TEMPLATE_FILE"
fi
echo "   using $TEMPLATE_FILE"

# 2. Create the container
NET0="name=eth0,bridge=${BRIDGE},ip=${IP}"
[[ -n "$GATEWAY" ]] && NET0="${NET0},gw=${GATEWAY}"

echo ">> Creating container $CTID ($CT_HOSTNAME)"
pct create "$CTID" "${VZTMPL_STORAGE}:vztmpl/${TEMPLATE_FILE}" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" \
  --memory "$RAM" \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "$NET0" \
  --unprivileged 1 \
  --features nesting=1 \
  --onboot 1 \
  --tags gremote

# 3. TUN device for Tailscale, then start
cat >> "/etc/pve/lxc/${CTID}.conf" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF
pct start "$CTID"

# 4. Wait for network
echo ">> Waiting for network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break
  sleep 2
done

# 5. Run the in-container installer (local copy if present, else from GitHub)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd || true)"
if [[ -f "$SCRIPT_DIR/setup-gremote.sh" ]]; then
  pct push "$CTID" "$SCRIPT_DIR/setup-gremote.sh" /tmp/setup-gremote.sh
else
  pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null"
  pct exec "$CTID" -- curl -fsSL "$RAW_BASE/setup-gremote.sh" -o /tmp/setup-gremote.sh
fi
pct exec "$CTID" -- env APP_REPO="$APP_REPO" APP_BRANCH="$APP_BRANCH" bash /tmp/setup-gremote.sh

CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. gremote is running at http://${CT_IP:-<container-ip>}:8000"
echo "  Dashboard password:  pct exec $CTID -- grep ADMIN_PASSWORD /opt/gremote/.env"
echo "  Tailscale:           pct exec $CTID -- tailscale up"
echo "  Update later:        run 'update' inside the container, or re-run this script with: update $CTID"
