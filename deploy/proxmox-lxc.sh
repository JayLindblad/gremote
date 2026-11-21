#!/usr/bin/env bash
# Create (or update) the gremote LXC container on Proxmox VE. Run as root on the PVE host.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy/proxmox-lxc.sh)"
#   bash deploy/proxmox-lxc.sh                 (from a clone of the repo)
#   bash deploy/proxmox-lxc.sh update [CTID]   (pull the latest code into an existing container)
#
# Settings are environment variables placed in front of the command, e.g.
#   IP=192.168.1.60/24 GATEWAY=192.168.1.1 PORT=80 bash deploy/proxmox-lxc.sh
#
#   CTID            next free ID     Container ID
#   CT_HOSTNAME     gremote          Container hostname (not HOSTNAME: bash sets that itself)
#   IP / GATEWAY    dhcp             Static IP in CIDR form plus gateway
#   BRIDGE / VLAN   vmbr0 / none     Must reach the same network as your devices
#   STORAGE         auto             Container disk storage (local-lvm if present)
#   PORT            8000             Use 80 for a URL without a port
#   GREMOTE_PASSWORD  generated      Dashboard password
#   MEMORY/DISK/CORES 512 / 4 / 1    Resources (MB / GB / cores)
#   INSTALL_TAILSCALE 1              Install (not configure) Tailscale on first create
set -euo pipefail

RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy}"
APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"
APP_BRANCH="${APP_BRANCH:-main}"

[[ "$(id -u)" -eq 0 ]] || { echo "Please run as root on the Proxmox host."; exit 1; }
command -v pct >/dev/null || { echo "pct not found - run this on a Proxmox VE host."; exit 1; }

install_env=(APP_REPO="$APP_REPO" APP_BRANCH="$APP_BRANCH" PORT="${PORT:-8000}" GREMOTE_PASSWORD="${GREMOTE_PASSWORD:-}")

# ---- update -----------------------------------------------------------------
if [[ "${1:-}" == "update" ]]; then
  CTID="${2:-${CTID:-}}"
  [[ -n "$CTID" ]] || CTID="$(pct list | awk '$NF=="'"${CT_HOSTNAME:-gremote}"'"{print $1; exit}')"
  [[ -n "$CTID" ]] || { echo "Could not find the container. Pass its ID: ... update <CTID>"; exit 1; }
  echo "Updating container $CTID..."
  pct exec "$CTID" -- env "${install_env[@]}" bash /opt/gremote/deploy/install.sh
  exit 0
fi

# ---- create -----------------------------------------------------------------
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${CT_HOSTNAME:-gremote}"
MEMORY="${MEMORY:-512}"
DISK="${DISK:-4}"
CORES="${CORES:-1}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"
GATEWAY="${GATEWAY:-}"
VLAN="${VLAN:-}"
PORT="${PORT:-8000}"
INSTALL_TAILSCALE="${INSTALL_TAILSCALE:-1}"
VZTMPL_STORAGE="${VZTMPL_STORAGE:-local}"

pct status "$CTID" >/dev/null 2>&1 && { echo "CTID $CTID already exists. Set CTID=<free id>."; exit 1; }

if [[ -z "${STORAGE:-}" ]]; then
  STORAGE="$(pvesm status --content rootdir | awk 'NR>1{print $1}' | grep -x local-lvm || true)"
  [[ -n "$STORAGE" ]] || STORAGE="$(pvesm status --content rootdir | awk 'NR>1{print $1; exit}')"
  [[ -n "$STORAGE" ]] || { echo "No storage supports containers. Set STORAGE=<name>."; exit 1; }
fi

# Debian template matching the host architecture (prefer 12, fall back to 13)
echo ">> Looking for a Debian template"
pveam update >/dev/null
ARCH="$(dpkg --print-architecture)"
find_template() {
  pveam available --section system \
    | awk -v v="$1" -v a="_${ARCH}." '$2 ~ "^debian-" v "-standard" && index($2,a){print $2}' | sort -V | tail -1
}
TEMPLATE_FILE="$(find_template 12)"
[[ -n "$TEMPLATE_FILE" ]] || TEMPLATE_FILE="$(find_template 13)"
[[ -n "$TEMPLATE_FILE" ]] || { echo "Could not find a Debian template for ${ARCH}."; exit 1; }
pveam list "$VZTMPL_STORAGE" | grep -q "$TEMPLATE_FILE" || pveam download "$VZTMPL_STORAGE" "$TEMPLATE_FILE"
echo "   using $TEMPLATE_FILE"

NET0="name=eth0,bridge=${BRIDGE},ip=${IP}"
[[ -n "$GATEWAY" ]] && NET0="${NET0},gw=${GATEWAY}"
[[ -n "$VLAN" ]] && NET0="${NET0},tag=${VLAN}"

echo ">> Creating container $CTID ($CT_HOSTNAME) on $STORAGE"
pct create "$CTID" "${VZTMPL_STORAGE}:vztmpl/${TEMPLATE_FILE}" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" \
  --memory "$MEMORY" \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "$NET0" \
  --unprivileged 1 \
  --features nesting=1 \
  --onboot 1 \
  --tags gremote

# TUN device for Tailscale
cat >> "/etc/pve/lxc/${CTID}.conf" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF
pct start "$CTID"

echo ">> Waiting for network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break
  sleep 2
done

# Install script: local copy if we're running from a clone, else from GitHub
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd || true)"
if [[ -f "$SCRIPT_DIR/install.sh" ]]; then
  pct push "$CTID" "$SCRIPT_DIR/install.sh" /tmp/install.sh
else
  pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null"
  pct exec "$CTID" -- curl -fsSL "$RAW_BASE/install.sh" -o /tmp/install.sh
fi
pct exec "$CTID" -- env "${install_env[@]}" bash /tmp/install.sh

# Tailscale is installed here, once, at creation - `update` never touches it.
if [[ "$INSTALL_TAILSCALE" == "1" ]]; then
  echo ">> Installing Tailscale"
  pct exec "$CTID" -- bash -c "curl -fsSL https://tailscale.com/install.sh | sh"
fi

CT_IP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
URL_PORT=":${PORT}"; [[ "$PORT" == "80" ]] && URL_PORT=""
echo
echo "Done. gremote is running at http://${CT_IP:-<container-ip>}${URL_PORT}"
echo "  Password:   pct exec $CTID -- grep ADMIN_PASSWORD /etc/gremote.env"
echo "  Tailscale:  pct exec $CTID -- tailscale up"
echo "  Update:     run 'update' inside the container, or: bash deploy/proxmox-lxc.sh update $CTID"
