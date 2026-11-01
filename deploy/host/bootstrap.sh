#!/usr/bin/env bash
# One-time host preparation for running Cloudreve rootless under Podman + systemd.
# Run as root on the server: ./deploy.sh bootstrap   (pipes this script over ssh)
# Idempotent: safe to re-run after changes.
set -euo pipefail
# Files written below with `cat >` get 644 from this umask.
umask 022

APP_USER=cloudreve
VOLUME_MOUNT=/mnt/HC_Volume_107333377
APP_ROOT="$VOLUME_MOUNT/cloudreve"

log() { printf '\n==> %s\n' "$*"; }

log "Checking the data volume is mounted at $VOLUME_MOUNT"
mountpoint -q "$VOLUME_MOUNT" || { echo "volume not mounted; refusing to continue" >&2; exit 1; }

log "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q podman uidmap passt rsync unattended-upgrades
podman --version

log "Creating service user $APP_USER"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$APP_USER"
fi
# Rootless Podman needs a subordinate uid/gid range for the container's own users.
if ! grep -q "^$APP_USER:" /etc/subuid; then
    usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$APP_USER"
fi
# Keep the user's systemd instance (and so the containers) running without a login.
loginctl enable-linger "$APP_USER"

log "Creating data directories on the volume"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 \
    "$APP_ROOT" \
    "$APP_ROOT/data" \
    "$APP_ROOT/caddy" \
    "$APP_ROOT/caddy/data" \
    "$APP_ROOT/caddy/config"
install -d -o "$APP_USER" -g "$APP_USER" -m 700 \
    "/home/$APP_USER/.config" \
    "/home/$APP_USER/.config/containers" \
    "/home/$APP_USER/.config/containers/systemd"

log "Allowing unprivileged processes to bind ports >= 80 (Caddy runs rootless)"
cat > /etc/sysctl.d/90-unprivileged-ports.conf <<'EOF'
net.ipv4.ip_unprivileged_port_start = 80
EOF
sysctl -q --system

log "Capping journal size"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/50-size.conf <<'EOF'
[Journal]
SystemMaxUse=500M
EOF
systemctl restart systemd-journald

log "Enabling unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

log "Bootstrap complete"
echo "Podman: $(podman --version)"
echo "Linger: $(loginctl show-user "$APP_USER" -p Linger --value)"
echo "Next: ./deploy.sh deploy"
