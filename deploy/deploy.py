#!/usr/bin/env python3
"""Deploy Cloudreve to the Hetzner server.

Runs on your machine; everything that touches the server goes over ssh, so the server
needs nothing but sshd. Standard library only, Python 3.9+.

    source deploy/.envrc
    deploy/deploy.py bootstrap     one-time host setup: podman, service user, dirs, sysctls
    deploy/deploy.py deploy        push quadlet units + Caddyfile, restart, wait for health
    deploy/deploy.py status        unit and container status
    deploy/deploy.py logs [unit]   follow logs (cloudreve by default, or caddy)
    deploy/deploy.py ssh [cmd...]  shell on the host

Cloud resources (server, volume, firewall, bucket, DNS) are managed in tofu/, not here.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

HERE = Path(__file__).resolve().parent

APP_USER = "cloudreve"
VOLUME_MOUNT = "/mnt/HC_Volume_107333377"
APP_ROOT = f"{VOLUME_MOUNT}/{APP_USER}"
QUADLET_DIR = f"/home/{APP_USER}/.config/containers/systemd"
SITE_URL = "https://drive.offlike.net"

SSH_OPTIONS = [
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "NumberOfPasswordPrompts=0",
    "-o", "PasswordAuthentication=no",
]


# --------------------------------------------------------------------------------------
# Remote execution
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Remote:
    """A root ssh session to the server, one command per call."""

    host: str
    key: str

    @classmethod
    def from_env(cls) -> "Remote":
        try:
            return cls(host=os.environ["DEPLOY_HOST"], key=os.environ["DEPLOY_SSH_KEY"])
        except KeyError as missing:
            sys.exit(f"{missing.args[0]} is not set; run `source deploy/.envrc` first")

    def _ssh(self, *extra: str) -> list[str]:
        return ["ssh", "-i", self.key, *SSH_OPTIONS, *extra, self.host]

    def run(self, command: str, *, check: bool = True, capture: bool = False,
            stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run a shell command on the server as root."""
        return subprocess.run(
            [*self._ssh(), command],
            check=check,
            input=stdin,
            text=True,
            capture_output=capture,
        )

    def output(self, command: str) -> str:
        return self.run(command, capture=True).stdout.strip()

    def succeeds(self, command: str) -> bool:
        return self.run(command, check=False, capture=True).returncode == 0

    def write_file(self, path: str, content: str, mode: str = "644",
                   owner: str = "root") -> None:
        """Write a file atomically (temp file, then move) with the given mode and owner."""
        tmp = f"{path}.tmp"
        q = shlex.quote
        self.run(
            f"cat > {q(tmp)} && chmod {mode} {q(tmp)} && chown {owner}:{owner} {q(tmp)}"
            f" && mv {q(tmp)} {q(path)}",
            stdin=content,
        )

    def push_dir(self, source: Path, destination: str, owner: str, mode: str = "644") -> None:
        """Mirror the files of a local directory (not recursive) into a remote directory,
        deleting remote files that no longer exist locally."""
        files = sorted(p for p in source.iterdir() if p.is_file())
        for path in files:
            self.write_file(f"{destination}/{path.name}", path.read_text(), mode, owner)
        keep = " ".join(shlex.quote(p.name) for p in files)
        self.run(
            f"cd {shlex.quote(destination)} && for f in *; do"
            f' case " {keep} " in *" $f "*) ;; *) echo "removing stale $f"; rm -f -- "$f";; esac;'
            " done"
        )

    def as_user_systemctl(self, *args: str, check: bool = True,
                          capture: bool = False) -> subprocess.CompletedProcess:
        """systemctl inside the cloudreve user's own systemd instance."""
        return self.run(
            f"systemctl --user --machine={APP_USER}@ " + shlex.join(args),
            check=check,
            capture=capture,
        )

    def interactive(self, *command: str) -> None:
        """Replace this process with an interactive ssh session."""
        os.execvp("ssh", self._ssh("-t") + list(command))


def step(title: str) -> None:
    print(f"\n==> {title}", flush=True)


# --------------------------------------------------------------------------------------
# bootstrap: one-time host preparation. Every step is idempotent; re-run freely.
# --------------------------------------------------------------------------------------

SYSCTL_UNPRIVILEGED_PORTS = """\
# Caddy runs rootless and must bind 80 and 443.
net.ipv4.ip_unprivileged_port_start = 80
"""

JOURNALD_SIZE_CAP = """\
[Journal]
SystemMaxUse=500M
"""

APT_AUTO_UPGRADES = """\
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
"""


def bootstrap(remote: Remote) -> None:
    step(f"Checking the data volume is mounted at {VOLUME_MOUNT}")
    if not remote.succeeds(f"mountpoint -q {VOLUME_MOUNT}"):
        sys.exit("volume is not mounted; refusing to continue")

    step("Installing packages")
    remote.run(
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -q"
        " && apt-get install -y -q podman uidmap passt unattended-upgrades"
        " && podman --version"
    )

    step(f"Creating service user {APP_USER}")
    if not remote.succeeds(f"id -u {APP_USER}"):
        remote.run(f"useradd --create-home --shell /bin/bash {APP_USER}")
    # Rootless Podman needs a subordinate uid/gid range for the container's own users.
    if not remote.succeeds(f"grep -q '^{APP_USER}:' /etc/subuid"):
        remote.run(f"usermod --add-subuids 100000-165535 --add-subgids 100000-165535 {APP_USER}")
    # Keep the user's systemd instance, and so the containers, running without a login.
    remote.run(f"loginctl enable-linger {APP_USER}")

    step("Creating data directories on the volume")
    owned = f"install -d -o {APP_USER} -g {APP_USER}"
    remote.run(
        f"{owned} -m 750 {APP_ROOT} {APP_ROOT}/data {APP_ROOT}/caddy"
        f" {APP_ROOT}/caddy/data {APP_ROOT}/caddy/config"
        f" && {owned} -m 700 /home/{APP_USER}/.config /home/{APP_USER}/.config/containers"
        f" {QUADLET_DIR}"
    )

    step("Allowing unprivileged processes to bind ports >= 80")
    remote.write_file("/etc/sysctl.d/90-unprivileged-ports.conf", SYSCTL_UNPRIVILEGED_PORTS)
    remote.run("sysctl -q --system")

    step("Capping journal size")
    remote.run("install -d /etc/systemd/journald.conf.d")
    remote.write_file("/etc/systemd/journald.conf.d/50-size.conf", JOURNALD_SIZE_CAP)
    remote.run("systemctl restart systemd-journald")

    step("Enabling unattended security upgrades")
    remote.write_file("/etc/apt/apt.conf.d/20auto-upgrades", APT_AUTO_UPGRADES)

    step("Bootstrap complete")
    print("podman:", remote.output("podman --version"))
    print("linger:", remote.output(f"loginctl show-user {APP_USER} -p Linger --value"))
    print("next:   deploy/deploy.py deploy")


# --------------------------------------------------------------------------------------
# deploy: push configuration and restart
# --------------------------------------------------------------------------------------

UNITS = ["cloudreve.service", "caddy.service"]


def deploy(remote: Remote) -> None:
    step("Pushing quadlet units and Caddyfile")
    remote.push_dir(HERE / "host" / "quadlet", QUADLET_DIR, owner=APP_USER)
    remote.write_file(f"{APP_ROOT}/caddy/Caddyfile", (HERE / "host" / "Caddyfile").read_text(),
                      owner=APP_USER)

    step("Reloading and restarting units")
    remote.as_user_systemctl("daemon-reload")
    for unit in UNITS:
        remote.as_user_systemctl("restart", unit)

    step("Waiting for cloudreve to become healthy (first start pulls a ~1 GB image)")
    deadline = time.monotonic() + 10 * 60
    while time.monotonic() < deadline:
        state = remote.as_user_systemctl("is-active", "cloudreve.service",
                                         check=False, capture=True).stdout.strip()
        if state == "active":
            break
        if state == "failed":
            remote.as_user_systemctl("status", "--no-pager", "cloudreve.service", check=False)
            sys.exit("cloudreve.service failed to start; see status above and `deploy.py logs`")
        time.sleep(5)
    else:
        sys.exit("timed out waiting for cloudreve.service; check `deploy.py logs`")

    status(remote)
    print(
        f"""
Deployed. If this is the first deploy:
  1. Open {SITE_URL} now and REGISTER AN ACCOUNT. The first registered user becomes the
     administrator, so do this before anyone else can.
  2. In the admin panel, disable registration, then add the S3 storage policy
     (see README.md, "Storage policy").
"""
    )


def status(remote: Remote) -> None:
    remote.as_user_systemctl("status", "--no-pager", "--lines=0", *UNITS, check=False)
    remote.run(
        f"su - {APP_USER} -c "
        + shlex.quote("podman ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'"),
        check=False,
    )


def logs(remote: Remote, unit: str) -> None:
    remote.interactive(f"journalctl _SYSTEMD_USER_UNIT={unit}.service --follow --lines=100")


# --------------------------------------------------------------------------------------


def main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="one-time host setup")
    sub.add_parser("deploy", help="push config and restart")
    sub.add_parser("status", help="unit and container status")
    logs_p = sub.add_parser("logs", help="follow logs")
    logs_p.add_argument("unit", nargs="?", default="cloudreve", choices=["cloudreve", "caddy"])
    ssh_p = sub.add_parser("ssh", help="shell on the host")
    ssh_p.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    remote = Remote.from_env()
    try:
        if args.command == "bootstrap":
            bootstrap(remote)
        elif args.command == "deploy":
            deploy(remote)
        elif args.command == "status":
            status(remote)
        elif args.command == "logs":
            logs(remote, args.unit)
        elif args.command == "ssh":
            remote.interactive(*args.cmd)
    except subprocess.CalledProcessError as failed:
        sys.exit(f"command failed with exit code {failed.returncode}: {failed.cmd[-1]}")


if __name__ == "__main__":
    main(sys.argv[1:])
