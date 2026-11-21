# Deploying gremote to Proxmox (LXC) + Tailscale

This walks through putting gremote on a small always-on Proxmox box on grandad's
LAN and reaching it privately over Tailscale. Assumes you already have Proxmox
installed and a Debian 12 template available.

## Quick path (automated)

On the Proxmox host shell, as root:

    bash -c "$(curl -fsSL https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy/create-lxc.sh)"

This does steps 1-4 below (container, TUN device, app, systemd, Tailscale
install) and generates `.env` secrets. Then run `pct exec <CTID> -- tailscale up`.
Update later with `update` inside the container. The rest of this document is
the manual equivalent.

## 1. Create the container

In the Proxmox UI, create an unprivileged Debian 12 container:

- Hostname: gremote
- Template: debian-12-standard
- Disk: 8 GB (more than enough)
- Cores / RAM: 1 core, 512 MB (the app is tiny)
- Network: default bridge (vmbr0), DHCP or a static LAN IP
- DNS: default

After creating it, enable nesting (Tailscale and Python don't strictly need it,
but it avoids surprises) and give the container a TUN device for Tailscale.

Edit the container config on the Proxmox host:

    nano /etc/pve/lxc/<CTID>.conf

Add these lines:

    features: nesting=1
    lxc.cgroup2.devices.allow: c 10:200 rwm
    lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file

Then reboot the container. If you prefer to skip the TUN device entirely, use
Tailscale's userspace networking instead (see step 2) and omit the last two lines.

## 2. Install Tailscale inside the container

Start the container, open its console, and run:

    apt update && apt install -y curl
    curl -fsSL https://tailscale.com/install.sh | sh
    tailscaled --tun=userspace-networking &   # only if you skipped the TUN device
    tailscale up

Then authorize the node in your Tailscale admin console. Give it a MagicDNS name
like "gremote" so you can reach it at http://gremote (or use its 100.x.y.z IP).

Note: keep the container on the LAN bridge as well. That is what lets it reach
the TV and other devices; Tailscale is only your private way *in*.

## 3. Install the app

Still in the container console:

    apt install -y python3-venv python3-pip git
    mkdir -p /opt/gremote && cd /opt/gremote
    # copy the repo in (git clone, scp, or rsync) — e.g.:
    git clone <your-repo-url> .

    python3 -m venv .venv
    .venv/bin/pip install -e .

    cp .env.example .env
    nano .env
    # set GREMOTE_SECRET_KEY (long random string) and GREMOTE_ADMIN_PASSWORD

Generate a secret key:

    python3 -c "import secrets; print(secrets.token_urlsafe(32))"

## 4. Run as a service

Copy deploy/gremote.service to /etc/systemd/system/ and enable it:

    cp deploy/gremote.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now gremote
    systemctl status gremote

By default it binds 0.0.0.0:8000. For tighter exposure, bind to the Tailscale
interface instead by changing the ExecStart host to the container's tailnet IP
(e.g. --host 100.64.0.5). The dashboard login still protects you either way.

## 5. Prepare the Sony TV

On the TV itself, enable IP control so gremote can talk to it:

- Settings -> Network -> Home network -> IP control
  - "Authentication" = Normal and Pre-Shared Key
  - set a Pre-Shared Key (this becomes the PSK you enter in gremote)
- Settings -> Network -> Home network -> Remote start = On
  (required for Wake-on-LAN while the TV is in standby)
- Note the TV's IP address (set a DHCP reservation or static IP so it never
  changes) and its wired MAC address (for Wake-on-LAN).

Then in gremote's dashboard: Add device -> Sony BRAVIA TV, enter the IP, the
MAC, and the PSK. You should now be able to see power/volume status and send
power, volume, and wake commands.

## 6. Connect from your PC

Install Tailscale on your PC, log in to the same tailnet, and browse to:

    http://gremote:8000      (or http://100.x.y.z:8000)

Sign in with GREMOTE_ADMIN_PASSWORD and you are controlling the TV.

## Security notes

- The only way in from the internet is your Tailscale network. Do not port-
  forward the container or the TV on grandad's router.
- The dashboard has its own password (GREMOTE_ADMIN_PASSWORD); keep it strong.
- The Sony PSK is stored in gremote's SQLite database, not in the repo.
- Keep everything updated: "apt update && apt upgrade" and tailscale updates.
