# Deploying Cloudreve to Hetzner

Single-server deployment of this fork at <https://drive.offlike.net>: Cloudreve with its
built-in SQLite database and in-memory cache, run rootless under Podman as a systemd
(Quadlet) unit, fronted by Caddy for automatic TLS.

```
deploy/
  .envrc.example        credentials and target, copy to .envrc (gitignored)
  tofu/                 OpenTofu: Hetzner firewall + Porkbun DNS records
  host/bootstrap.sh     one-time host setup, run over ssh
  host/quadlet/         systemd units for the containers
  host/Caddyfile        reverse proxy config
  deploy.sh             push config, restart, status, logs
```

## What exists in the Hetzner project

| Resource | Purpose |
|---|---|
| Server `ubuntu-4gb-nbg1-1` (cx23, Ubuntu 26.04) | runs everything |
| Volume `volume-nbg1-1` (10 GB) at `/mnt/HC_Volume_107333377` | all persistent state: SQLite DB, uploads, Caddy certs |
| Storage box `storage-box-1` (1 TiB) | backup target (not wired up yet) |

The server and volume were created in the console and then imported into tofu, so the
whole cloud layer is in code. Both carry `prevent_destroy` plus Hetzner-side delete and
rebuild protection: a plan that would replace or delete them fails instead of running.
State is local (`tofu/terraform.tfstate`, gitignored); back it up or move it to a remote
backend before a second machine needs to run tofu.

## First-time setup

```sh
cp deploy/.envrc.example deploy/.envrc   # fill in Porkbun keys
source deploy/.envrc

# 1. Cloud: firewall (22/80/443 only) and DNS A/AAAA for drive.offlike.net
tofu -chdir=deploy/tofu init
tofu -chdir=deploy/tofu apply

# 2. Host: podman, the `cloudreve` user, data dirs, sysctls
deploy/deploy.sh bootstrap

# 3. App: units, Caddyfile, start
deploy/deploy.sh deploy
```

Then open <https://drive.offlike.net> and register: the first account becomes the
administrator. Disable registration in the admin panel right after.

## Day-to-day

```sh
deploy/deploy.sh deploy     # after editing anything under host/
deploy/deploy.sh status
deploy/deploy.sh logs       # or: logs caddy
deploy/deploy.sh ssh
```

Upgrading Cloudreve: change the image tag in `host/quadlet/cloudreve.container`, then
`deploy.sh deploy`. Database migrations run automatically on start. Take a copy of
`/mnt/HC_Volume_107333377/cloudreve/data/cloudreve.db` first (stop the service, copy,
start; or use `sqlite3 ... ".backup"`).

## Design notes

- **SQLite + memory cache**: Cloudreve's default when no `[Database]`/`[Redis]` section is
  set. Right size for one household; the DB is one file on the volume.
- **No shipped conf.ini**: Cloudreve generates `data/conf.ini` with a random
  `SessionSecret` on first start. The settings we need are passed as `CR_CONF_*` env
  overrides in the unit, so we never overwrite a file the app owns.
- **Rootless**: containers run as the unprivileged `cloudreve` user; container root maps to
  that user on the host. Caddy binds 80/443 thanks to
  `net.ipv4.ip_unprivileged_port_start=80`.
- **Port 5212 is never published.** Only Caddy is reachable, on the private container
  network, so `ProxyHeader=X-Forwarded-For` is safe.
- **Full image** (`ghcr.io/dvorinka/cloudreve:<tag>-amd64`) includes ffmpeg, vips,
  LibreOffice, libraw for thumbnails and media metadata. The `-slim-amd64` variant drops
  them; swap the tag if 1 GB of image is unwelcome.

## Not done yet

- Backups to the storage box (restic over SFTP; the box has SSH enabled).
- Hetzner server backups / volume snapshots.
- Remote state backend for tofu (state is a local gitignored file today).
- Restricting SSH in the firewall to a fixed source address (`ssh_allowed_cidrs`).
