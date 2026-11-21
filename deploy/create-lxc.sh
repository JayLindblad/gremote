#!/usr/bin/env bash
# Create the gremote LXC on Proxmox and prep it for Tailscale + the app.
#
# This is equivalent to what https://community-scripts.org/generator produces
# for a Debian container, PLUS the gremote-specific bits the generator cannot
# do: unprivileged + nesting, the /dev/net/tun device Tailscale needs, and the
# app install.
#
# Usage (run AS ROOT on the Proxmox HOST):
#   bash create-lxc.sh
# Override settings with env vars, e.g.:
#   CTID=112 RAM=1024 DISK=8 bash create-lxc.sh

set -euo pipefail

CTID="${CTID:-110}"
HOSTNAME="${HOSTNAME:-gremote}"
CORES="${CORES:-1}"
RAM="${RAM:-512}"                    # MB
DISK="${DISK:-4}"                    # GB
STORAGE="${STORAGE:-local-lvm}"      # storage holding the rootfs
VZTMPL_STORAGE="${VZTMPL_STORAGE:-local}"  # storage holding templates
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"                     # or e.g. 192.168.1.50/24
GATEWAY="${GATEWAY:-}"               # required only for a static IP
APP_REPO="${APP_REPO:-https://github.com/JayLindblad/gremote.git}"

[[ "$(id -u)" -eq 0 ]] || { echo "Please run as root on the Proxmox host."; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# 1. pick a Debian template (download if missing)
TEMPLATE_FILE="$(pveam available --section system 2>/dev/null | awk '$1 ~ /^debian-1[23]-standard$/{print $2; exit}')"
if [[ -z "$TEMPLATE_FILE" ]]; then
  echo "No Debian template found; downloading..."
  pveam update
  TEMPLATE_FILE="$(pveam available --section system | awk '$1 ~ /^debian-1[23]-standard$/{print $2; exit}')"
fi
[[ -n "$TEMPLATE_FILE" ]] || { echo "Could not find a Debian template."; exit 1; }
echo "Using template: $TEMPLATE_FILE"

# 2. create the container
NET0="name=eth0,bridge=${BRIDGE},ip=${IP}"
[[ -n "$GATEWAY" ]] && NET0="${NET0},gw=${GATEWAY}"

pct create "$CTID" "${VZTMPL_STORAGE}:vztmpl/${TEMPLATE_FILE}" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" \
  --memory "$RAM" \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "$NET0" \
  --unprivileged 1 \
  --features nesting=1 \
  --onboot 1

# 3. give the container a TUN device (Tailscale) + start it
cat >> "/etc/pve/lxc/${CTID}.conf" <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF

pct start "$CTID"

# 4. optional: push + run the in-container app installer if it sits beside us
if [[ -f "$SCRIPT_DIR/setup-gremote.sh" ]]; then
  echo "Pushing app installer into the container..."
  pct push "$CTID" "$SCRIPT_DIR/setup-gremote.sh" /tmp/setup-gremote.sh
  pct exec "$CTID" -- env APP_REPO="$APP_REPO" bash /tmp/setup-gremote.sh
fi

echo
echo "Container $CTID ($HOSTNAME) created and started."
echo "Next steps (manual):"
echo "  pct enter $CTID"
echo "  tailscale up            # authorise in your Tailscale admin console"
echo "  nano /opt/gremote/.env  # set GREMOTE_SECRET_KEY + GREMOTE_ADMIN_PASSWORD"
echo "  systemctl restart gremote"
