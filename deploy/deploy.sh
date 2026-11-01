#!/usr/bin/env bash
# Deploy Cloudreve to the Hetzner server. Run from your machine after `source deploy/.envrc`.
#
#   ./deploy.sh bootstrap   one-time host setup (podman, service user, dirs, sysctl)
#   ./deploy.sh deploy      push quadlet units + Caddyfile, reload, restart, wait for health
#   ./deploy.sh status      systemd + container status
#   ./deploy.sh logs [unit] follow logs (default: cloudreve; or caddy)
#   ./deploy.sh ssh [cmd]   shell on the host
#
# Cloud resources (firewall, DNS) are managed separately: see tofu/.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${DEPLOY_HOST:?set DEPLOY_HOST (source deploy/.envrc)}"
: "${DEPLOY_SSH_KEY:?set DEPLOY_SSH_KEY (source deploy/.envrc)}"

APP_USER=cloudreve
APP_ROOT=/mnt/HC_Volume_107333377/cloudreve
QUADLET_DIR="/home/$APP_USER/.config/containers/systemd"

ssh_opts=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
          -o NumberOfPasswordPrompts=0 -o PasswordAuthentication=no)
remote() { ssh "${ssh_opts[@]}" "$DEPLOY_HOST" "$@"; }
# Run `systemctl --user` inside the cloudreve user's systemd instance, from root.
user_systemctl() { remote systemctl --user --machine="$APP_USER@" "$@"; }
push() { # push <local> <remote-path> <mode>
    rsync -e "ssh ${ssh_opts[*]}" --recursive --links --times --chmod="$3" \
        --chown="$APP_USER:$APP_USER" --delete "$1" "$DEPLOY_HOST:$2"
}

cmd_bootstrap() {
    remote bash -s < "$here/host/bootstrap.sh"
}

cmd_deploy() {
    echo "==> Pushing quadlet units and Caddyfile"
    push "$here/host/quadlet/" "$QUADLET_DIR/" "F644,D700"
    push "$here/host/Caddyfile" "$APP_ROOT/caddy/Caddyfile" "F644"

    echo "==> Reloading units"
    user_systemctl daemon-reload
    user_systemctl restart cloudreve.service
    user_systemctl restart caddy.service

    echo "==> Waiting for cloudreve to become healthy"
    for _ in $(seq 1 60); do
        if [ "$(user_systemctl is-active cloudreve.service)" = "active" ]; then
            break
        fi
        sleep 5
    done
    user_systemctl --no-pager --lines=0 status cloudreve.service caddy.service || true

    cat <<EOF

Deployed. If this is the first deploy:
  1. Open https://drive.offlike.net as soon as DNS resolves and REGISTER AN ACCOUNT.
     The first registered user becomes the administrator; do this before anyone else can.
  2. In the admin panel, disable registration (Settings -> Registration).
EOF
}

cmd_status() {
    user_systemctl --no-pager status cloudreve.service caddy.service || true
    remote su - "$APP_USER" -c "podman ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'"
}

cmd_logs() {
    local unit="${1:-cloudreve}"
    remote journalctl _SYSTEMD_USER_UNIT="$unit.service" --follow --lines=100
}

cmd_ssh() {
    ssh -t "${ssh_opts[@]}" "$DEPLOY_HOST" "$@"
}

case "${1:-}" in
    bootstrap) cmd_bootstrap ;;
    deploy)    cmd_deploy ;;
    status)    cmd_status ;;
    logs)      shift; cmd_logs "$@" ;;
    ssh)       shift; cmd_ssh "$@" ;;
    *) sed -n '2,10p' "$0"; exit 1 ;;
esac
