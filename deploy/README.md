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
  host/quadlet/         systemd units for the containers (cloudreve, caddy, basement)
  host/Caddyfile        reverse proxy config: /api/basement/* -> basement, the rest -> cloudreve
  host/basement.toml    basement's server-side config
  deploy.py             bootstrap the host, push config, restart, status, logs (Python 3, stdlib)
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
deploy/deploy.py bootstrap

# 3. App: units, Caddyfile, start
deploy/deploy.py deploy
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
deploy/deploy.py deploy     # after editing anything under host/
deploy/deploy.py status
deploy/deploy.py upgrade    # apply pending OS updates now; reboots only if required
deploy/deploy.py logs       # or: logs caddy
deploy/deploy.py ssh
```

### Android builds for the phone

```sh
deploy/deploy.py apk --build                                    # assembleRelease in ../android, then publish
deploy/deploy.py apk --build --android-dir <android dir>         # build and publish from another checkout
deploy/deploy.py apk --apk <path> --android-dir <android dir>   # publish an APK you built elsewhere
```

The commit in the file name is the android dir's `HEAD`; `-dirty` is appended only for
tracked, uncommitted changes (untracked files such as emulator data don't count).

Uploads the release APK as `cloudreve-<versionName>-<commit>.apk` to
`/mnt/HC_Volume_107333377/cloudreve/apk/`, which Caddy serves read-only at
[drive.offlike.net/apk/](https://drive.offlike.net/apk/): a phone-sized page with the newest
build on top, `latest.json` for scripts, and the last five APKs. Nothing restarts; Caddy
reads the directory from disk. The route is public (the APK is a build of this public repo,
debug-signed) and has no directory listing. Caddy sends
`application/vnd.android.package-archive` so Chrome offers Install directly.

### Release artifacts on Tangled

```sh
deploy/deploy.py tangled-app-password dxts.tngl.sh   # once; prompts for the main password, prints an app password
export TANGLED_HANDLE=dxts.tngl.sh TANGLED_APP_PASSWORD=...   # the app password, never the login password
git -C ../android tag -a v1.2.0 -m "Android 1.2.0" && git -C ../android push tangled v1.2.0
deploy/deploy.py tangled-release v1.2.0 --build --android-dir <android dir>
```

Attaches the release APK to a tag on
[tangled.org/dxts.tngl.sh/cloudreve](https://tangled.org/dxts.tngl.sh/cloudreve), where it
shows next to the tag. The tag must already be pushed to the `tangled` remote and point at
the same commit locally; the command refuses otherwise. Nothing on the server is touched:
the APK goes to the knot (`knot1.tangled.sh`) as a blob and becomes an
`org.tangled.repo.artifact` record in the repository's own AT Protocol account, written with
per-call service tokens from your PDS. The knot accepts up to 50 MiB per file. A second run
for the same tag stops unless you pass `--replace`.

Why not Tangled CI: the hosted spindle's workflow timeout is 5 minutes, too short for an
Android build, so builds stay local and only the upload goes to Tangled.

`deploy/deploy.py tangled-set-fork tdvorak.tngl.sh/cloudreve` records the author's Tangled
repo as our source, so the page shows "forked from". Tangled keeps that relation in the
`source` field of the repository manifest and offers no UI for it after creation; the
command rewrites the manifest through the knot with every other field carried over and
`swapRecord` pinned to the version it read. Idempotent.

Upgrading Cloudreve: change the image tag in `host/quadlet/cloudreve.container`, then
`deploy.py deploy`. `deploy` restarts only units whose files changed; a Caddyfile-only change
is a live reload, and an unchanged tree touches nothing. Database migrations run automatically on start. Take a copy of
`/mnt/HC_Volume_107333377/cloudreve/data/cloudreve.db` first (stop the service, copy,
start; or use `sqlite3 ... ".backup"`).

## basement

[basement](https://github.com/dxts/basement) runs next to Cloudreve as a third rootless
container, same user, same private network (design doc, section 7). Caddy routes
`/api/basement/*` to it; it validates the SPA's Cloudreve token by introspection against
`http://cloudreve:5212/api/v4/user/me`. Nothing about the Cloudreve deployment changes.

```sh
# in the basement repo, once per machine:
brew install zig && cargo install --locked cargo-zigbuild
rustup target add x86_64-unknown-linux-musl
# build a static binary (no libc dependency, ~9 MB) and ship it:
cargo zigbuild --release --target x86_64-unknown-linux-musl
cd ../cloudreve && deploy/deploy.py basement    # or --binary <path>
deploy/deploy.py logs basement
```

`basement` pushes the unit, config and binary, restarts **only** `basement.service` when one
of them changed, and live-reloads Caddy if its running config lacks the route. There is no
registry and no image build on the server: the container is `distroless/static` with the
binary bind-mounted in, so a redeploy is a 9 MB upload and a two-second restart. The
binary and `basement.toml` live under `/mnt/HC_Volume_107333377/cloudreve/basement/`; the
index database under `basement/data/`, which the backups must cover alongside Cloudreve's.

## Design notes

- **SQLite + memory cache**: Cloudreve's default when no `[Database]`/`[Redis]` section is
  set. Right size for one household; the DB is one file on the volume.
- **One tool, nothing installed on the server**: `deploy.py` runs locally and sends every
  step over ssh, including the bootstrap, so the host only ever needs sshd. Each bootstrap
  step is idempotent; re-run it after changing anything.
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
- **Caddyfile is directory-mounted, not file-mounted.** `deploy.py` replaces it atomically
  (temp file, then `mv`), which gives it a new inode. A single-file bind mount is pinned to
  the old inode, so `caddy reload` kept re-reading stale config and the first basement route
  silently never applied. Mounting `caddy/conf/` as `/etc/caddy` follows the rename.
- **Quadlet `HealthCmd=` on a shell-less image must be the JSON array form.** A plain string
  is wrapped in `/bin/sh -c`, which does not exist in `distroless/static`, so the probe fails
  with exit 1 and no output before the binary runs.

## Exposure and hardening

What is reachable from the internet, and nothing else: `22/tcp` (sshd, keys only), `80/tcp`
(Caddy, redirects to HTTPS and serves ACME challenges), `443/tcp+udp` (Caddy, HSTS).
Enforced at two layers: the Hetzner firewall (tofu) drops everything else before it reaches
the host, and on the host only Caddy publishes ports. Cloudreve's 5212, aria2's 6888 and
Caddy's admin API 2019 exist only on the private container network.

Host: root login by key only (`PasswordAuthentication no`, `PermitRootLogin
prohibit-password`, drop-in `/etc/ssh/sshd_config.d/10-hardening.conf`), security updates
applied daily by unattended-upgrades with an automatic reboot at 04:30 when a kernel or libc
update needs one, journal capped. Containers are rootless under the `cloudreve` user.

App: registration disabled after the first account. Still on you: enable two-factor
authentication on the admin account (user menu -> Settings -> Security), and consider the
login captcha under Settings -> Captcha if the login endpoint starts seeing abuse.

Not done, by choice: no host firewall (ufw) on top of the Hetzner one, no fail2ban (nothing
to brute-force with keys only), SSH open to any source address. To restrict SSH to a fixed
address set `ssh_allowed_cidrs` in `tofu/variables.tf`.

## Not done yet

- Backups: SQLite DB to the storage box (restic over SFTP; the box has SSH enabled), and a
  second copy of the bucket to the storage box (rclone sync) so one Hetzner product failing
  does not take both copies.
- Hetzner server backups / volume snapshots.
- Remote state backend for tofu (state is a local gitignored file today).
- Restricting SSH in the firewall to a fixed source address (`ssh_allowed_cidrs`).
