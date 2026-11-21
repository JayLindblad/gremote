# gremote

A self-hosted dashboard for remotely troubleshooting IP devices on a home network
(Sony TVs, and more later), designed to run in a Proxmox LXC container on a small
always-on box and be reached over Tailscale.

## What it does

- Serves a small web dashboard (FastAPI + vanilla JS frontend).
- Keeps a registry of IP devices in SQLite.
- Talks to Sony BRAVIA TVs via [pybravia](https://github.com/Drafteed/pybravia)
  (REST API + Wake-on-LAN + IRCC) — the same library Home Assistant's
  `bravia_tv` integration uses.
- Requires a login (simple password session) before anything is reachable.

## Architecture

    Browser (you, over Tailscale)
        |
        v
    gremote (LXC container) -- FastAPI app
        |  \_________________
        |                    \
        v                     v
    SQLite registry      Sony TV REST API + WOL
    (device list)        (and future device drivers)

## Quickstart (dev)

    python -m venv .venv
    .venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"
    cp .env.example .env                    # then edit secrets
    .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Open http://127.0.0.1:8000 and log in with the password you set in .env.

## Deploying to Proxmox

See deploy/lxc-setup.md for the full LXC + Tailscale walkthrough.

## License

Private / personal use.
