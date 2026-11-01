# Deploying Cloudreve to Hetzner

Single-server deployment of this fork at <https://drive.offlike.net>: Cloudreve with its
built-in SQLite database and in-memory cache, run rootless under Podman as a systemd
(Quadlet) unit, fronted by Caddy for automatic TLS. File blobs live in a Hetzner Object
Storage bucket (S3 policy, presigned direct uploads), so the server never proxies file
bytes and the volume only holds the database.

```
deploy/
  .envrc.example        credentials and target, copy to .envrc (gitignored)
  tofu/                 OpenTofu: server, volume, firewall, S3 bucket, Porkbun DNS
  host/bootstrap.sh     one-time host setup, run over ssh
  host/quadlet/         systemd units for the containers
  host/Caddyfile        reverse proxy config
  deploy.sh             push config, restart, status, logs
```

## What exists in the Hetzner project

| Resource | Purpose |
|---|---|
| Server `ubuntu-4gb-nbg1-1` (cx23, Ubuntu 26.04) | runs everything |
| Volume `volume-nbg1-1` (10 GB) at `/mnt/HC_Volume_107333377` | SQLite DB, conf.ini, temp, Caddy certs |
| Object Storage bucket `drive-offlike-net` (nbg1) | all file blobs, via Cloudreve's S3 storage policy |
| Storage box `storage-box-1` (1 TiB) | second backup target (not wired up yet) |

The server and volume were created in the console and then imported into tofu, so the
whole cloud layer is in code. Both carry `prevent_destroy` plus Hetzner-side delete and
rebuild protection: a plan that would replace or delete them fails instead of running.
State is local (`tofu/terraform.tfstate`, gitignored); back it up or move it to a remote
backend before a second machine needs to run tofu.

## First-time setup

```sh
cp deploy/.envrc.example deploy/.envrc   # fill in Porkbun keys and Hetzner S3 keys
source deploy/.envrc

# 1. Cloud: firewall (22/80/443 only), S3 bucket with CORS, DNS A/AAAA
tofu -chdir=deploy/tofu init
tofu -chdir=deploy/tofu apply

# 2. Host: podman, the `cloudreve` user, data dirs, sysctls
deploy/deploy.sh bootstrap

# 3. App: units, Caddyfile, start
deploy/deploy.sh deploy
```

Then open <https://drive.offlike.net> and register: the first account becomes the
administrator. Disable registration in the admin panel right after.

### Storage policy (once, in the admin panel)

Cloudreve starts with a "local" policy writing under `data/uploads` on the volume. Switch
blobs to the bucket:

1. Admin -> Storage Policies -> Add -> **S3**. Fill from `tofu output s3_policy`:
   endpoint `https://nbg1.your-objectstorage.com`, bucket `drive-offlike-net`, region
   `nbg1`, path-style off, private bucket on. Access key and secret: the second S3
   credential pair created for Cloudreve in the Hetzner Console (not tofu's).
2. Do **not** press "Set CORS" in the policy editor; tofu already set a rule scoped to the
   site origin, and the button would replace it with a wildcard.
3. Admin -> Groups -> Admin and Default: set the S3 policy as the storage policy.

The local policy stays as a fallback; nothing should be written to it after this.

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
- **Object Storage for blobs**: Cloudreve's S3 driver presigns uploads and downloads, so
  browsers and the Android client move bytes to the bucket directly. The server handles
  metadata, thumbnails and previews. Traffic between nbg1 compute and nbg1 object storage
  is inside Hetzner's eu-central zone and free; egress to clients is billed per TB above
  the included quota.
- **Port 5212 is never published.** Only Caddy is reachable, on the private container
  network, so `ProxyHeader=X-Forwarded-For` is safe.
- **Full image** (`ghcr.io/dvorinka/cloudreve:<tag>-amd64`) includes ffmpeg, vips,
  LibreOffice, libraw for thumbnails and media metadata. The `-slim-amd64` variant drops
  them; swap the tag if 1 GB of image is unwelcome.

## Not done yet

- Backups: SQLite DB to the storage box (restic over SFTP; the box has SSH enabled), and a
  second copy of the bucket to the storage box (rclone sync) so one Hetzner product failing
  does not take both copies.
- Hetzner server backups / volume snapshots.
- Remote state backend for tofu (state is a local gitignored file today).
- Restricting SSH in the firewall to a fixed source address (`ssh_allowed_cidrs`).
