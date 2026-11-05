#!/usr/bin/env python3
"""Deploy Cloudreve to the Hetzner server.

Runs on your machine; everything that touches the server goes over ssh, so the server
needs nothing but sshd. Standard library only, Python 3.9+.

    source deploy/.envrc
    deploy/deploy.py bootstrap     one-time host setup: podman, service user, dirs, sysctls
    deploy/deploy.py deploy        push quadlet units + Caddyfile, restart, wait for health
    deploy/deploy.py upgrade       apply pending OS updates now, reboot if required
    deploy/deploy.py status        unit and container status
    deploy/deploy.py logs [unit]   follow logs (cloudreve by default, or caddy)
    deploy/deploy.py ssh [cmd...]  shell on the host

Cloud resources (server, volume, firewall, bucket, DNS) are managed in tofu/, not here.
"""

from __future__ import annotations

import argparse
import hashlib
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
# Directory-mounted into the caddy container (see caddy.container for why not the file).
CADDYFILE = f"{APP_ROOT}/caddy/conf/Caddyfile"
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

    def succeeds(self, command: str, stdin: Optional[str] = None) -> bool:
        return self.run(command, check=False, capture=True, stdin=stdin).returncode == 0

    def write_file(self, path: str, content: str, mode: str = "644",
                   owner: str = "root") -> bool:
        """Write a file atomically (temp file, then move) with the given mode and owner.
        Returns True if the content changed, so callers can restart only what they must."""
        q = shlex.quote
        unchanged = self.succeeds(f"cmp -s {q(path)} - ", stdin=content)
        if unchanged:
            return False
        tmp = f"{path}.tmp"
        self.run(
            f"cat > {q(tmp)} && chmod {mode} {q(tmp)} && chown {owner}:{owner} {q(tmp)}"
            f" && mv {q(tmp)} {q(path)}",
            stdin=content,
        )
        return True

    def write_binary(self, path: str, content: bytes, mode: str = "755",
                     owner: str = "root") -> bool:
        """Binary counterpart of write_file: compare by SHA-256, then stream the bytes over
        ssh into a temp file and move it into place. Returns True if the file changed."""
        q = shlex.quote
        digest = hashlib.sha256(content).hexdigest()
        unchanged = self.succeeds(
            f"[ -f {q(path)} ] && [ \"$(sha256sum {q(path)} | cut -d' ' -f1)\" = {digest} ]"
        )
        if unchanged:
            return False
        tmp = f"{path}.tmp"
        subprocess.run(
            [*self._ssh(),
             f"cat > {q(tmp)} && chmod {mode} {q(tmp)} && chown {owner}:{owner} {q(tmp)}"
             f" && mv {q(tmp)} {q(path)}"],
            check=True,
            input=content,
        )
        return True

    def push_dir(self, source: Path, destination: str, owner: str,
                 mode: str = "644") -> set[str]:
        """Mirror the files of a local directory (not recursive) into a remote directory,
        deleting remote files that no longer exist locally. Returns the names of files
        that were added, changed or removed."""
        files = sorted(p for p in source.iterdir() if p.is_file())
        changed = {
            path.name
            for path in files
            if self.write_file(f"{destination}/{path.name}", path.read_text(), mode, owner)
        }
        keep = " ".join(shlex.quote(p.name) for p in files)
        removed = self.output(
            f"cd {shlex.quote(destination)} && for f in *; do"
            f' case " {keep} " in *" $f "*) ;; *) echo "$f"; rm -f -- "$f";; esac;'
            " done"
        )
        return changed | set(removed.split())

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
APT::Periodic::AutocleanInterval "7";
"""

# Security updates that need a reboot (kernel, libc) get one at night rather than waiting
# for someone to notice. Containers come back on their own via linger + Restart=always.
APT_AUTO_REBOOT = """\
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
"""

# Keys only. Root has a locked password and the service user has none, so password auth
# was already unusable, but leaving it enabled still feeds the brute-force bots a
# codepath. Written as a drop-in so Ubuntu's own sshd_config stays untouched.
SSHD_HARDENING = """\
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PermitEmptyPasswords no
X11Forwarding no
AllowAgentForwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
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
        f" {APP_ROOT}/caddy/conf {APP_ROOT}/caddy/data {APP_ROOT}/caddy/config"
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

    step("Enabling unattended security upgrades with automatic reboot")
    remote.write_file("/etc/apt/apt.conf.d/20auto-upgrades", APT_AUTO_UPGRADES)
    remote.write_file("/etc/apt/apt.conf.d/52unattended-upgrades-local", APT_AUTO_REBOOT)

    step("Hardening sshd: keys only")
    remote.write_file("/etc/ssh/sshd_config.d/10-hardening.conf", SSHD_HARDENING)
    # Validate before reloading so a typo cannot lock us out; reload keeps this session.
    remote.run("sshd -t && systemctl reload ssh")
    # A fresh connection proves the key path still works after the reload.
    effective = remote.output(
        "sshd -T | grep -E '^(passwordauthentication|permitrootlogin) '"
    )
    print(effective)
    if "passwordauthentication no" not in effective:
        sys.exit("sshd still accepts passwords; check /etc/ssh/sshd_config.d/")

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
    changed_units = remote.push_dir(HERE / "host" / "quadlet", QUADLET_DIR, owner=APP_USER)
    caddyfile_changed = remote.write_file(
        CADDYFILE, (HERE / "host" / "Caddyfile").read_text(), owner=APP_USER
    )

    # Restart only what changed. A quadlet change restarts that unit (a few seconds of
    # downtime for cloudreve; caddy returns 502 meanwhile). A Caddyfile-only change is a
    # live reload with no dropped connections. Nothing changed: leave everything alone.
    # `systemctl cat` refuses generator-produced units, so ask for the load state instead.
    load_state = remote.as_user_systemctl("show", "cloudreve.service", "-p", "LoadState",
                                          "--value", capture=True).stdout.strip()
    first_run = load_state != "loaded"
    if changed_units or first_run:
        step(f"Reloading units and restarting: {', '.join(sorted(changed_units)) or 'all (first run)'}")
        remote.as_user_systemctl("daemon-reload")
        for unit in UNITS:
            if first_run or unit.replace(".service", ".container") in changed_units \
                    or "cloudreve.network" in changed_units:
                remote.as_user_systemctl("restart", unit)
    elif caddyfile_changed:
        step("Reloading caddy configuration (no restart)")
        remote.as_user_systemctl("reload", "caddy.service")
    else:
        step("Nothing changed; services left running")

    step("Waiting for cloudreve to be healthy (first start pulls a ~1 GB image)")
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


def upgrade(remote: Remote) -> None:
    """Apply all pending OS updates now and reboot if the kernel or libc changed."""
    step("Upgrading packages")
    remote.run(
        "export DEBIAN_FRONTEND=noninteractive && apt-get update -q"
        " && apt-get dist-upgrade -y -q && apt-get autoremove -y -q"
    )
    if not remote.succeeds("test -e /var/run/reboot-required"):
        print("no reboot required")
        return

    step("Rebooting (reboot required by the upgrade)")
    remote.run("systemctl reboot", check=False)
    time.sleep(15)
    deadline = time.monotonic() + 5 * 60
    while time.monotonic() < deadline:
        if remote.succeeds("true"):
            break
        time.sleep(5)
    else:
        sys.exit("server did not come back within 5 minutes")

    step("Waiting for cloudreve after reboot")
    deadline = time.monotonic() + 5 * 60
    while time.monotonic() < deadline:
        state = remote.as_user_systemctl("is-active", "cloudreve.service",
                                         check=False, capture=True).stdout.strip()
        if state == "active":
            break
        time.sleep(5)
    status(remote)


# basement: push the binary and config, restart only basement
# --------------------------------------------------------------------------------------

BASEMENT_ROOT = f"{APP_ROOT}/basement"
BASEMENT_UNIT = "basement.service"
# Default location of the static musl build; see README.md, "basement".
BASEMENT_BINARY = (HERE.parent.parent / "basement" / "target"
                   / "x86_64-unknown-linux-musl" / "release" / "basement")


def deploy_basement(remote: Remote, binary: Path) -> None:
    """Deploy basement (design doc, section 7) next to Cloudreve without touching it.

    Pushes the quadlet unit, the Caddyfile route, the config and the binary; restarts only
    basement.service if any of those changed, and live-reloads Caddy if the Caddyfile did.
    Cloudreve is never restarted by this command."""
    if not binary.is_file():
        sys.exit(f"basement binary not found at {binary}\n"
                 "build it with: cargo zigbuild --release --target x86_64-unknown-linux-musl")

    step("Pushing basement unit, config and binary")
    remote.run(f"install -d -m 750 -o {APP_USER} -g {APP_USER} "
               f"{BASEMENT_ROOT} {BASEMENT_ROOT}/bin {BASEMENT_ROOT}/data")
    unit_changed = remote.write_file(
        f"{QUADLET_DIR}/basement.container",
        (HERE / "host" / "quadlet" / "basement.container").read_text(), owner=APP_USER,
    )
    config_changed = remote.write_file(
        f"{BASEMENT_ROOT}/basement.toml", (HERE / "host" / "basement.toml").read_text(),
        owner=APP_USER,
    )
    binary_changed = remote.write_binary(
        f"{BASEMENT_ROOT}/bin/basement", binary.read_bytes(), owner=APP_USER,
    )
    caddyfile_changed = remote.write_file(
        CADDYFILE, (HERE / "host" / "Caddyfile").read_text(),
        owner=APP_USER,
    )

    load_state = remote.as_user_systemctl("show", BASEMENT_UNIT, "-p", "LoadState",
                                          "--value", capture=True).stdout.strip()
    first_run = load_state != "loaded"
    if unit_changed or first_run:
        remote.as_user_systemctl("daemon-reload")
    if unit_changed or config_changed or binary_changed or first_run:
        what = [n for n, c in (("unit", unit_changed), ("config", config_changed),
                               ("binary", binary_changed)) if c] or ["first run"]
        step(f"Restarting basement ({', '.join(what)})")
        remote.as_user_systemctl("restart", BASEMENT_UNIT)
    else:
        step("basement unchanged; left running")

    # Reload Caddy if the file changed now OR an earlier run wrote it but aborted before
    # this step; the running config is the truth, not this run's diff.
    route_live = remote.succeeds(
        f"su - {APP_USER} -c " + shlex.quote(
            # busybox wget resolves localhost to ::1 but the admin API is v4-only; use curl + 127.0.0.1
            "podman exec caddy curl -sf http://127.0.0.1:2019/config/ | grep -q basement:8080"
        )
    )
    if caddyfile_changed or not route_live:
        step("Reloading caddy configuration (no restart)")
        remote.as_user_systemctl("reload", "caddy.service")

    step("Waiting for basement to be healthy")
    deadline = time.monotonic() + 2 * 60
    while time.monotonic() < deadline:
        state = remote.as_user_systemctl("is-active", BASEMENT_UNIT,
                                         check=False, capture=True).stdout.strip()
        if state == "active":
            break
        if state == "failed":
            remote.as_user_systemctl("status", "--no-pager", BASEMENT_UNIT, check=False)
            sys.exit("basement.service failed to start; see `deploy.py logs basement`")
        time.sleep(3)
    else:
        sys.exit("timed out waiting for basement.service; check `deploy.py logs basement`")

    health = remote.output(
        f"su - {APP_USER} -c " + shlex.quote(
            "podman exec caddy wget -q -O - http://basement:8080/api/basement/health"
        )
    )
    print(f"\nbasement health via the private network: {health}")
    status(remote)


def status(remote: Remote) -> None:
    remote.as_user_systemctl("status", "--no-pager", "--lines=0", *UNITS, BASEMENT_UNIT,
                             check=False)
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
    sub.add_parser("upgrade", help="apply pending OS updates, reboot if required")
    basement_p = sub.add_parser("basement", help="push the basement binary + config, restart only it")
    basement_p.add_argument("--binary", type=Path, default=BASEMENT_BINARY,
                            help=f"static musl build (default: {BASEMENT_BINARY})")
    sub.add_parser("status", help="unit and container status")
    logs_p = sub.add_parser("logs", help="follow logs")
    logs_p.add_argument("unit", nargs="?", default="cloudreve",
                        choices=["cloudreve", "caddy", "basement"])
    ssh_p = sub.add_parser("ssh", help="shell on the host")
    ssh_p.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    remote = Remote.from_env()
    try:
        if args.command == "bootstrap":
            bootstrap(remote)
        elif args.command == "deploy":
            deploy(remote)
        elif args.command == "upgrade":
            upgrade(remote)
        elif args.command == "basement":
            deploy_basement(remote, args.binary)
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
