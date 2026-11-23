# gremote

A self-hosted web remote for IP-controlled devices on your home network (Sony BRAVIA TVs today, more later).
Run it on any always-on machine (Proxmox LXC, Pi, NAS, PC), reach it from your phone or computer, and
keep it private by putting it behind [Tailscale](https://tailscale.com).

## Features

- **Multiple devices**: switch between them with tabs, or pick **All** to send a command to every device at once
- **Auto-discovery** of Sony TVs (SSDP plus a direct subnet sweep that works on Wi-Fi networks filtering multicast), or add a device by IP address
- Full remote: power, D-pad, back/home/options, action/sync/audio (and STB menu when the TV has one), volume/mute, channels, playback, number pad, colour keys
- **All commands** panel listing every remote command the TV itself reports, so nothing is out of reach
- **TV settings** panel: picture, sound and power-saving settings read from the TV and changed directly (sliders, toggles, dropdowns), plus a "What does my TV expose?" listing
- **Power on** with the TV's API or Wake-on-LAN
- **Volume slider, input switching** (TV, HDMI 1-4) and an **app launcher** loaded from the TV
- Hold a button to repeat it (volume, arrows), keyboard shortcuts on desktop, works as a home-screen app on phones
- Live power status on every tab
- **Debug log** window showing every command, its result, timing and the underlying error when one fails
- **Wake-on-LAN MAC read from the TV itself** when you add it
- Edit a device (name, IP, MAC, PSK) with the pencil icon in ⚙; PSKs are never sent back to the browser
- Password login; settings and data survive updates

## Setup (local)

Requires Python 3.11 or newer.

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
```

```bash
cp .env.example .env                # then set GREMOTE_SECRET_KEY and GREMOTE_ADMIN_PASSWORD
```

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` and sign in with the password from `.env`.

### First-time TV setup (Sony)

Works with Android-based BRAVIA TVs running Android TV 9 (e.g. the 2017 **A1E** OLED) and with newer Google TV models. Older models expose fewer settings over the API; anything a TV doesn't support is listed under *What does my TV expose?* and skipped.

On the TV, enable IP control so gremote can talk to it:

1. **Settings → Network → Home network → IP control** (Android TV 9, e.g. A1E: **Settings → Network & Internet → Home network → IP control**): set *Authentication* to **Normal and Pre-Shared Key** and choose a PSK.
2. **Remote start = On** (needed for Wake-on-LAN from standby; on Android TV 9 it is under **Network & Internet**).
3. Give the TV a **fixed IP (DHCP reservation)** in your router.

Then in gremote: ⚙ → **Scan network** (or add by IP) → enter the PSK. The MAC for Wake-on-LAN is read from the TV automatically (leave it blank); use **Get MAC from TV** on a device later if it was missed.

## Configuration

Set these as environment variables, or in `.env` / `/etc/gremote.env`:

| Variable | Default | Purpose |
|---|---|---|
| `GREMOTE_SECRET_KEY` | `change-me` | Signs session cookies. Use a long random string |
| `GREMOTE_ADMIN_PASSWORD` | `change-me` | Dashboard password |
| `GREMOTE_DATABASE_PATH` | `gremote.db` | Where devices and PSKs are stored |
| `GREMOTE_SESSION_TTL_SECONDS` | `604800` | How long a login lasts (7 days) |
| `GREMOTE_SCAN_SUBNET` | your /24 | Subnets the TV scan probes directly, e.g. `192.168.1.0/24,192.168.2.0/24` |

> gremote logs a warning at startup while either secret is still `change-me`.
> Don't expose the port to the internet. Use Tailscale.

## Proxmox: LXC container (recommended)

One command, run in the Proxmox host shell as root, creates a small Debian LXC (512 MB RAM, 4 GB disk) with gremote
installed as a service. The container starts when Proxmox boots, and a secret key and dashboard password are generated for you.

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy/proxmox-lxc.sh)"
```

Or from a clone of the repo: `bash deploy/proxmox-lxc.sh`. It prints the address when it finishes,
e.g. `http://192.168.1.60:8000`.

Settings are passed as environment variables in front of the command:

```bash
IP=192.168.1.60/24 GATEWAY=192.168.1.1 PORT=80 bash deploy/proxmox-lxc.sh
```

| Variable | Default | Purpose |
|---|---|---|
| `CTID` | next free ID | Container ID |
| `CT_HOSTNAME` | `gremote` | Container hostname |
| `IP` / `GATEWAY` | `dhcp` | Static IP in CIDR form (e.g. `192.168.1.60/24`) plus gateway |
| `BRIDGE` / `VLAN` | `vmbr0` / none | Must reach the same network/VLAN as your devices |
| `STORAGE` | `local-lvm` (or first available) | Container disk storage |
| `PORT` | `8000` | Use `80` for a URL without a port |
| `GREMOTE_PASSWORD` | generated | Dashboard password |
| `MEMORY` / `DISK` / `CORES` | `512` / `4` / `1` | Resources |
| `INSTALL_TAILSCALE` | `1` | Install Tailscale in the container (not logged in) |

Afterwards, authorise Tailscale once: `pct exec <CTID> -- tailscale up`.

To **update** later, run `update` inside the container, or from the Proxmox host:

```bash
bash deploy/proxmox-lxc.sh update <CTID>
```

Updating pulls the latest code from GitHub, refreshes dependencies and restarts the service. Your devices, PSKs,
settings and Tailscale login are kept; Tailscale itself is never touched.

Inside the container: settings live in `/etc/gremote.env` (run `systemctl restart gremote` after editing),
logs via `journalctl -u gremote -f`, and data in `/var/lib/gremote/`.

> The container must be on the same LAN/VLAN as the devices. Discovery uses multicast and Wake-on-LAN uses broadcast,
> and neither crosses subnets.

**Already have a Debian/Ubuntu LXC or VM?** Run `bash deploy/install.sh` as root. It sets up the same service
(without Tailscale).

## Docker (Mac or Linux)

One command, run on the machine with Docker (Docker Desktop on a Mac, or Docker on Linux), installs gremote
as a container that starts with Docker. A secret key and dashboard password are generated for you.

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/JayLindblad/gremote/main/deploy/docker.sh)"
```

Or from a clone of the repo: `bash deploy/docker.sh` (builds that copy as it is). It prints the address when it
finishes, e.g. `http://localhost:8000`.

To **update** later, open the container's shell and run `update`, just like in the LXC:

```bash
docker exec -it gremote bash     # then: update
docker exec -it gremote update   # or in one go
```

Running the install command again also updates (it rebuilds the image from the latest code). Your devices,
PSKs, ADB key and settings are kept in the `gremote-data` volume either way.

Settings live in `/data/gremote.env` inside the container (run `docker restart gremote` after editing),
logs via `docker logs -f gremote`. Settings: `PORT=8080`, `GREMOTE_PASSWORD=...` and
`GREMOTE_SCAN_SUBNET=192.168.1.0/24` can be put in front of the command, as with the LXC script.

**Docker Desktop on a Mac** runs containers behind a NAT, so the installer looks up your Mac's network (e.g.
`10.0.0.0/24`) and tells the TV scan to sweep it; the scan also checks the subnets of TVs you've already added.
If your Mac moves to another network, run the install command again. Wake-on-LAN broadcasts can't leave Docker
Desktop, so power on with the TV's API (keep **Remote start** on).
**On Linux** the container uses host networking, so discovery and Wake-on-LAN work as in the LXC.

`docker compose up -d --build` (with a `.env` from `.env.example`) still works on Linux as well.

## Keyboard shortcuts (desktop)

Arrows · Enter = OK · Backspace/Esc = Back · H = Home · +/− = volume · M = mute · PgUp/PgDn = channel · S = Input · P = power · Space = play/pause

## Troubleshooting

| Problem | Fix |
|---|---|
| Scan finds nothing | Add the TV by IP. Discovery needs the server and TV on the same subnet. |
| "Authentication failed" | The PSK doesn't match. Re-check it under the TV's IP control settings. |
| Power On does nothing | Add the TV's *wired* MAC, and turn on *Remote start*. Use **Wake** for Wake-on-LAN. |
| Something fails, reason unknown | Open the Debug log (top right). Each command shows the error and the underlying exception; **Copy** gives you text to paste. |
| Status shows "unreachable" | The TV is in deep standby or the IP changed. Use a DHCP reservation. |
| Apps list is empty | Some models don't expose it. Use Home, or send an IRCC name such as `Netflix`. |
| Some buttons do nothing | Not every model supports every IRCC command. |

## API

All endpoints are JSON and need a session cookie from `POST /api/auth/login`.

```
GET    /api/devices                       list devices
GET    /api/devices/types                 supported device types
POST   /api/devices   {name,type,host,mac?,config?}   add a device
PUT    /api/devices/:id                   edit a device (blank PSK keeps the stored one)
DELETE /api/devices/:id
GET    /api/devices/:id/status            power, volume, mute
POST   /api/devices/:id/action {action, params?}
POST   /api/devices/:id/fetch-mac        read the MAC from the TV and save it
POST   /api/discovery/scan                scan for Sony TVs
GET    /api/debug/log?after=N             recent events (DELETE clears)
```

Actions: `commands` (list the TV supported IRCC names), `settings`, `set_setting`, `methods`, `adb_info`, `adb_open`, `adb_activities`, `adb_supported`, `power_on`, `power_off`, `toggle_power`, `wake`, `volume_up`, `volume_down`, `set_volume`, `toggle_mute`,
`set_input`, `apps`, `launch_app`, `ircc`, `playing`, `system_info`. For example:

```bash
curl -b cookies -X POST localhost:8000/api/devices/<id>/action \
  -H 'Content-Type: application/json' -d '{"action":"ircc","params":{"code":"home"}}'
```

## License

Private / personal use.
