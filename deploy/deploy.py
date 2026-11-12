#!/usr/bin/env python3
"""Deploy Cloudreve to the Hetzner server.

Runs on your machine; everything that touches the server goes over ssh, so the server
needs nothing but sshd. Standard library only, Python 3.9+.

    source deploy/.envrc
    deploy/deploy.py bootstrap     one-time host setup: podman, service user, dirs, sysctls
    deploy/deploy.py deploy        push quadlet units + Caddyfile, restart, wait for health
    deploy/deploy.py upgrade       apply pending OS updates now, reboot if required
    deploy/deploy.py status        unit and container status
    deploy/deploy.py apk [--build]  publish the Android release APK at /apk/ for sideloading
    deploy/deploy.py tangled-release TAG [--build]
                                   attach the release APK to a pushed tag on tangled.org
    deploy/deploy.py tangled-set-fork OWNER/NAME
                                   show our tangled.org repo as a fork of another
    deploy/deploy.py logs [unit]   follow logs (cloudreve by default, or caddy)
    deploy/deploy.py ssh [cmd...]  shell on the host

Cloud resources (server, volume, firewall, bucket, DNS) are managed in tofu/, not here.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
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

# Android builds, served read-only by Caddy at SITE_URL/apk/ (see host/Caddyfile).
APK_ROOT = f"{APP_ROOT}/apk"
APK_KEEP = 5
ANDROID_DIR = HERE.parent / "android"


def release_apk(android_dir: Path) -> Path:
    return android_dir / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"



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
    # The caddy unit bind-mounts this; podman would create it root-owned otherwise.
    remote.run(f"install -d -m 755 -o {APP_USER} -g {APP_USER} {APK_ROOT}")
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


# --------------------------------------------------------------------------------------
# Android APK publishing
# --------------------------------------------------------------------------------------


def apk_identity(apk: Path, android_dir: Path) -> tuple[str, int, str]:
    """(versionName, versionCode, short commit) for the build. aapt2 reads the manifest
    when the SDK is installed; otherwise fall back to parsing build.gradle.kts."""
    sdk = Path(os.environ.get("ANDROID_HOME") or Path.home() / "Library/Android/sdk")
    tools = sorted((sdk / "build-tools").glob("*/aapt2"), reverse=True)
    name, code = None, None
    if tools:
        out = subprocess.run([str(tools[0]), "dump", "badging", str(apk)],
                             capture_output=True, text=True).stdout
        m = re.search(r"versionCode='(\d+)' versionName='([^']+)'", out)
        if m:
            code, name = int(m.group(1)), m.group(2)
    if name is None:
        gradle = (android_dir / "app" / "build.gradle.kts").read_text()
        name = re.search(r'versionName\s*=\s*"([^"]+)"', gradle).group(1)
        code = int(re.search(r"versionCode\s*=\s*(\d+)", gradle).group(1))
    commit = subprocess.run(["git", "-C", str(android_dir), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    # Only tracked changes count: untracked files (emulator data, local.properties) don't ship.
    dirty = subprocess.run(["git", "-C", str(android_dir), "status", "--porcelain", "--untracked-files=no", "--", "."],
                           capture_output=True, text=True).stdout.strip()
    return name, code, commit + ("-dirty" if dirty else "")


def render_index(builds: list[dict]) -> str:
    """Phone-friendly page: newest build first, one tap to download."""
    rows = []
    for b in builds:
        rows.append(
            f'<li><a href="{html.escape(b["file"])}" download>'
            f'<strong>{html.escape(b["versionName"])}</strong> '
            f'<code>{html.escape(b["commit"])}</code></a>'
            f'<small>{b["size"] / 1048576:.1f} MB · {html.escape(b["builtAt"][:16].replace("T", " "))} UTC'
            f' · sha256 {b["sha256"][:12]}…</small></li>'
        )
    items = "\n".join(rows) or "<li>No builds yet.</li>"
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Cloudreve Android builds</title>
<style>
  body {{ font: 17px/1.5 system-ui, sans-serif; margin: 0; padding: 24px; background: #111318; color: #e3e2e6; }}
  h1 {{ font-size: 22px; font-weight: 500; margin: 0 0 16px; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ margin: 0 0 12px; }}
  a {{ display: block; padding: 16px 20px; border-radius: 20px; background: #1e2128; color: #adc6ff; text-decoration: none; }}
  li:first-child a {{ background: #adc6ff; color: #002e6a; }}
  li:first-child a::after {{ content: "  latest"; font-size: 13px; opacity: .8; }}
  code {{ font-size: 14px; opacity: .8; }}
  small {{ display: block; padding: 6px 20px 0; color: #8e9099; font-size: 13px; }}
  p {{ color: #8e9099; font-size: 14px; }}
</style>
<h1>Cloudreve Android builds</h1>
<ul>
{items}
</ul>
<p>Debug-signed: uninstall a Play Store copy before installing. Built from
<code>master</code> (upstream Dvorinka/cloudreve); the commit is in each name.</p>
"""


def publish_apk(remote: Remote, apk: Path, android_dir: Path, build: bool) -> None:
    """Upload a release APK to the server and regenerate the /apk/ page.

    Keeps the last APK_KEEP builds, names each after its version and commit so the phone
    always knows what it installed, and never touches cloudreve or caddy: Caddy serves the
    directory straight from disk, so a new file is live the moment the move lands."""
    if build:
        step("Building release APK")
        subprocess.run(["./gradlew", ":app:assembleRelease", "--console=plain", "-q"],
                       cwd=android_dir, check=True)
    if not apk.is_file():
        sys.exit(f"APK not found at {apk}\nbuild it with: ./gradlew :app:assembleRelease (or pass --build)")

    name, code, commit = apk_identity(apk, android_dir)
    content = apk.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    filename = f"cloudreve-{name}-{commit}.apk"
    step(f"Uploading {filename} ({len(content) / 1048576:.1f} MB)")
    remote.run(f"install -d -m 755 -o {APP_USER} -g {APP_USER} {APK_ROOT}")
    changed = remote.write_binary(f"{APK_ROOT}/{filename}", content, mode="644", owner=APP_USER)
    if not changed:
        print("identical file already published; refreshing the index only")

    # Rebuild the manifest from what is actually on disk, newest first, and prune.
    listing = remote.output(
        f"cd {APK_ROOT} && for f in *.apk; do [ -f \"$f\" ] && "
        f"printf '%s\\t%s\\t%s\\n' \"$f\" \"$(stat -c %s \"$f\")\" \"$(stat -c %Y \"$f\")\"; done"
    )
    on_disk = []
    for line in listing.splitlines():
        f, size, mtime = line.split("\t")
        on_disk.append({"file": f, "size": int(size), "mtime": int(mtime)})
    on_disk.sort(key=lambda b: b["mtime"], reverse=True)
    for stale in on_disk[APK_KEEP:]:
        print(f"pruning {stale['file']}")
        remote.run(f"rm -f {shlex.quote(APK_ROOT + '/' + stale['file'])}")
    on_disk = on_disk[:APK_KEEP]

    # Carry over metadata for builds already published; both files are JSON we wrote ourselves.
    previous = {}
    for path in (f"{APK_ROOT}/builds.json", f"{APK_ROOT}/latest.json"):
        raw = remote.run(f"cat {path} 2>/dev/null", check=False, capture=True).stdout
        try:
            loaded = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            continue
        for b in loaded if isinstance(loaded, list) else [loaded]:
            if isinstance(b, dict) and b.get("file") and b.get("sha256"):
                previous.setdefault(b["file"], b)

    builds = []
    for b in on_disk:
        if b["file"] == filename:
            builds.append({"file": filename, "versionName": name, "versionCode": code, "commit": commit,
                           "size": len(content), "sha256": digest,
                           "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        elif b["file"] in previous:
            builds.append(previous[b["file"]])
        else:
            m = re.match(r"cloudreve-(.+?)-([0-9a-f]{7,}(?:-dirty)?)\.apk$", b["file"])
            digest_on_disk = remote.output(f"sha256sum {shlex.quote(APK_ROOT + '/' + b['file'])}").split()[0]
            builds.append({"file": b["file"], "versionName": m.group(1) if m else "?", "versionCode": 0,
                           "commit": m.group(2) if m else "?", "size": b["size"], "sha256": digest_on_disk,
                           "builtAt": datetime.fromtimestamp(b["mtime"], timezone.utc).isoformat(timespec="seconds")})

    step("Writing index")
    remote.write_file(f"{APK_ROOT}/builds.json", json.dumps(builds, indent=1) + "\n", owner=APP_USER)
    remote.write_file(f"{APK_ROOT}/latest.json", json.dumps(builds[0], indent=1) + "\n", owner=APP_USER)
    remote.write_file(f"{APK_ROOT}/index.html", render_index(builds), owner=APP_USER)
    print(f"\nOpen {SITE_URL}/apk/ on the phone; direct link: {SITE_URL}/apk/{filename}")


# --------------------------------------------------------------------------------------
# Tangled release artifacts
#
# Tangled shows files attached to a tag on the repository page. An attachment is an
# org.tangled.repo.artifact record in the *repository's* own AT Protocol account, which
# lives on the knot that hosts the git data. Writing there needs a service-auth token
# minted by the owner's PDS for that knot, one per XRPC call. This is the same dance the
# official release workflow does with `goat`, done here with urllib so the release can
# be cut from a laptop. Needs TANGLED_HANDLE and TANGLED_APP_PASSWORD (an app password,
# not the account password) in the environment.
# --------------------------------------------------------------------------------------

TANGLED_REMOTE = "tangled"
ARTIFACT_COLLECTION = "org.tangled.repo.artifact"
ARTIFACT_LIMIT = 50 * 1024 * 1024  # documented knot limit per file
APK_MIME = "application/vnd.android.package-archive"
USER_AGENT = "cloudreve-deploy/1 (+https://tangled.org/dxts.tngl.sh/cloudreve)"


def xrpc(base: str, method: str, *, params: Optional[dict] = None, body: Optional[bytes] = None,
         content_type: str = "application/json", token: Optional[str] = None) -> dict:
    """One XRPC call. GET when there is no body, POST otherwise. Errors carry the
    server's JSON message so an auth or validation failure is readable."""
    url = f"{base}/xrpc/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    # The knot sits behind Cloudflare, which rejects urllib's default user agent (error 1010).
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as failed:
        detail = failed.read().decode("utf-8", "replace")
        sys.exit(f"{method} failed: HTTP {failed.code} {detail.strip()}")


def did_document(did: str) -> dict:
    request = urllib.request.Request(f"https://plc.directory/{did}",
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pds_endpoint(did: str) -> str:
    """The #atproto_pds endpoint from a did:plc document. For a repository DID this is the
    knot that hosts it."""
    for service in did_document(did).get("service", []):
        if service.get("id") == "#atproto_pds":
            return service["serviceEndpoint"].rstrip("/")
    sys.exit(f"{did} has no #atproto_pds service in its DID document")


def resolve_handle(handle: str) -> str:
    return xrpc("https://public.api.bsky.app", "com.atproto.identity.resolveHandle",
                params={"handle": handle})["did"]


def tangled_repo_did(repo_dir: Path) -> str:
    """The repository DID, read from the `tangled` git remote
    (dxts.tngl.sh@tangled.org:did:plc:...)."""
    url = subprocess.run(["git", "-C", str(repo_dir), "remote", "get-url", TANGLED_REMOTE],
                         capture_output=True, text=True).stdout.strip()
    m = re.search(r"(did:plc:[a-z2-7]+)", url)
    if not m:
        sys.exit(f"git remote {TANGLED_REMOTE!r} is missing or does not use a did:plc URL: {url!r}")
    return m.group(1)


class TangledSession:
    """Owner credentials on their PDS, plus per-call service tokens for the knot."""

    def __init__(self, handle: str, app_password: str, repo_did: str) -> None:
        self.did = resolve_handle(handle)
        self.pds = pds_endpoint(self.did)
        self.repo_did = repo_did
        self.knot = pds_endpoint(repo_did)
        session = xrpc(self.pds, "com.atproto.server.createSession",
                       body=json.dumps({"identifier": handle, "password": app_password}).encode())
        self.access = session["accessJwt"]

    def knot_token(self, method: str) -> str:
        """Each knot write needs a fresh token scoped to one method (lxm) and the repo."""
        return xrpc(self.pds, "com.atproto.server.getServiceAuth",
                    params={"aud": self.repo_did, "lxm": method}, token=self.access)["token"]

    def knot_call(self, method: str, **kwargs) -> dict:
        return xrpc(self.knot, method, token=self.knot_token(method), **kwargs)


def tangled_app_password(handle: str, label: str) -> None:
    """Create an app password on the account's PDS. tangled.org signs in with OAuth and has
    no page for this, but the PDS speaks the standard API. The main password is read from
    a hidden prompt and sent only to the PDS; the new app password is printed once."""
    import getpass

    did = resolve_handle(handle)
    pds = pds_endpoint(did)
    print(f"{handle} -> {did} on {pds}")
    main_password = getpass.getpass(f"main password for {handle}: ")
    session = xrpc(pds, "com.atproto.server.createSession",
                   body=json.dumps({"identifier": handle, "password": main_password}).encode())
    created = xrpc(pds, "com.atproto.server.createAppPassword",
                   body=json.dumps({"name": label}).encode(), token=session["accessJwt"])
    print(f"\napp password {created['name']!r} created; it is shown only now:\n")
    print(f"  export TANGLED_HANDLE={handle} TANGLED_APP_PASSWORD={created['password']}")
    print("\nRevoke it later with com.atproto.server.revokeAppPassword, or list with listAppPasswords.")


def tangled_set_fork(upstream: str, repo_dir: Path) -> None:
    """Record `upstream` (owner-handle/name on tangled.org) as the source of our
    repository, which the web UI renders as "forked from".

    The fork relation is the `source` field of the repository's own
    org.tangled.repo.manifest record; the web UI has no control for it after
    creation, but the knot accepts a putRecord of the manifest from the owner with
    every other field carried over. swapRecord pins the version we read, so a
    concurrent change (a rename in the UI) fails instead of being overwritten."""
    try:
        handle, app_password = os.environ["TANGLED_HANDLE"], os.environ["TANGLED_APP_PASSWORD"]
    except KeyError as missing:
        sys.exit(f"{missing.args[0]} is not set; see `deploy.py tangled-app-password`")
    owner_handle, _, name = upstream.partition("/")
    if not name:
        sys.exit("upstream must be owner-handle/repo-name, e.g. tdvorak.tngl.sh/cloudreve")

    step(f"Resolving {upstream}")
    owner_did = resolve_handle(owner_handle)
    declaration = xrpc(pds_endpoint(owner_did), "com.atproto.repo.getRecord",
                       params={"repo": owner_did, "collection": "org.tangled.repo.declaration",
                               "rkey": name.lower()})
    upstream_did = declaration["value"]["repo"]
    print(f"{upstream} -> {upstream_did}")

    repo_did = tangled_repo_did(repo_dir)
    session = TangledSession(handle, app_password, repo_did)
    current = xrpc(session.knot, "com.atproto.repo.getRecord",
                   params={"repo": repo_did, "collection": "org.tangled.repo.manifest", "rkey": "self"})
    manifest = dict(current["value"])
    previous = manifest.get("source")
    manifest["source"] = {"$type": "org.tangled.repo.defs#sourceTangled", "did": upstream_did}
    manifest.pop("x-tngl-editor", None)  # the knot stamps this itself
    if previous == manifest["source"]:
        print("already recorded as a fork of that repository; nothing to do")
        return

    step("Updating the repository manifest")
    written = session.knot_call(
        "com.atproto.repo.putRecord",
        body=json.dumps({"repo": repo_did, "collection": "org.tangled.repo.manifest", "rkey": "self",
                         "swapRecord": current["cid"], "record": manifest}).encode(),
    )
    print(f"{written.get('uri')} -> {written.get('cid')}")
    print(f"previous source: {previous or 'none'}")
    ours = manifest["declaration"]["name"]
    print(f"Open https://tangled.org/{handle}/{ours} to see \"forked from {upstream}\".")


def tag_commit(repo_dir: Path, tag: str) -> str:
    """The commit a tag points at, verified to match what the knot has for that tag."""
    local = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "--verify", f"{tag}^{{commit}}"],
                           capture_output=True, text=True)
    if local.returncode != 0:
        sys.exit(f"tag {tag!r} does not exist locally; create it with: git tag -a {tag} -m '...'")
    oid = local.stdout.strip()
    remote_refs = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-remote", "--tags", TANGLED_REMOTE,
         f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        capture_output=True, text=True, check=True).stdout
    remote_oids = {line.split()[0] for line in remote_refs.splitlines() if line.strip()}
    if not remote_oids:
        sys.exit(f"tag {tag!r} is not on {TANGLED_REMOTE}; push it with: git push {TANGLED_REMOTE} {tag}")
    if oid not in remote_oids:
        sys.exit(f"tag {tag!r} points at {oid[:12]} locally but {TANGLED_REMOTE} has "
                 f"{', '.join(o[:12] for o in sorted(remote_oids))}; push the tag first")
    return oid


def tangled_release(tag: str, apk: Path, android_dir: Path, build: bool, replace: bool) -> None:
    """Attach the release APK to TAG on tangled.org.

    Builds the APK if asked, checks the tag is pushed and points at the same commit here
    and on the knot, uploads the file as a blob and creates the artifact record. Nothing
    on the Hetzner server is touched."""
    try:
        handle, app_password = os.environ["TANGLED_HANDLE"], os.environ["TANGLED_APP_PASSWORD"]
    except KeyError as missing:
        sys.exit(f"{missing.args[0]} is not set; create an app password in your PDS settings "
                 "and export TANGLED_HANDLE and TANGLED_APP_PASSWORD")

    if build:
        step("Building release APK")
        subprocess.run(["./gradlew", ":app:assembleRelease", "--console=plain", "-q"],
                       cwd=android_dir, check=True)
    if not apk.is_file():
        sys.exit(f"APK not found at {apk}\nbuild it with: ./gradlew :app:assembleRelease (or pass --build)")
    content = apk.read_bytes()
    if len(content) > ARTIFACT_LIMIT:
        sys.exit(f"{apk} is {len(content) / 1048576:.1f} MB; the knot accepts at most 50 MiB per artifact")

    step(f"Checking tag {tag} on {TANGLED_REMOTE}")
    repo_did = tangled_repo_did(android_dir)
    oid = tag_commit(android_dir, tag)
    version, _code, commit = apk_identity(apk, android_dir)
    if not oid.startswith(commit.removesuffix("-dirty")):
        print(f"warning: the APK was built from {commit}, but {tag} is {oid[:12]}")
    if commit.endswith("-dirty"):
        print("warning: the android tree has uncommitted changes")
    name = f"cloudreve-{tag}.apk"  # tags are android-vX.Y.Z, so: cloudreve-android-v1.3.0.apk
    print(f"repo {repo_did}\ntag  {tag} -> {oid}\nfile {name} ({len(content) / 1048576:.1f} MB, {version})")

    step(f"Signing in as {handle}")
    session = TangledSession(handle, app_password, repo_did)
    print(f"knot {session.knot}")

    existing = [
        r for r in xrpc(session.knot, "com.atproto.repo.listRecords",
                        params={"repo": repo_did, "collection": ARTIFACT_COLLECTION, "limit": 100})
        .get("records", [])
        if r["value"].get("name") == name and r["value"].get("subject", {}).get("oid") == oid
    ]
    if existing and not replace:
        sys.exit(f"{name} is already attached to {tag} ({existing[0]['uri']}); pass --replace to re-upload")
    for record in existing:
        rkey = record["uri"].rsplit("/", 1)[1]
        print(f"deleting previous artifact {rkey}")
        session.knot_call("com.atproto.repo.deleteRecord",
                          body=json.dumps({"repo": repo_did, "collection": ARTIFACT_COLLECTION,
                                           "rkey": rkey}).encode())

    step("Uploading blob to the knot")
    blob = session.knot_call("com.atproto.repo.uploadBlob", body=content, content_type=APK_MIME)["blob"]

    step("Creating artifact record")
    record = {
        "$type": ARTIFACT_COLLECTION,
        "subject": {"$type": "org.tangled.git.oid", "oid": oid},
        "name": name,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifact": blob,
    }
    created = session.knot_call(
        "com.atproto.repo.createRecord",
        body=json.dumps({"repo": repo_did, "collection": ARTIFACT_COLLECTION, "record": record}).encode(),
    )
    print(f"\n{created['uri']}\nsha256 {hashlib.sha256(content).hexdigest()}")
    print(f"Open https://tangled.org/{handle}/cloudreve/tags to see it next to {tag}.")


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
    apk_p = sub.add_parser("apk", help="publish the Android release APK at /apk/")
    apk_p.add_argument("--apk", type=Path, default=None,
                       help="APK to publish (default: the release build under --android-dir)")
    apk_p.add_argument("--android-dir", type=Path, default=ANDROID_DIR,
                       help="android project dir, for the version and commit (default: ../android)")
    apk_p.add_argument("--build", action="store_true", help="run ./gradlew :app:assembleRelease first")
    rel_p = sub.add_parser("tangled-release", help="attach the release APK to a pushed tag on tangled.org")
    rel_p.add_argument("tag", help="an annotated or lightweight tag already pushed to the tangled remote")
    rel_p.add_argument("--apk", type=Path, default=None,
                       help="APK to attach (default: the release build under --android-dir)")
    rel_p.add_argument("--android-dir", type=Path, default=ANDROID_DIR,
                       help="android project dir, also the git repo the tag lives in (default: ../android)")
    rel_p.add_argument("--build", action="store_true", help="run ./gradlew :app:assembleRelease first")
    rel_p.add_argument("--replace", action="store_true",
                       help="delete an artifact of the same name already attached to the tag")
    fork_p = sub.add_parser("tangled-set-fork",
                            help="record another tangled.org repo as this one's source (\"forked from\")")
    fork_p.add_argument("upstream", help="owner-handle/name, e.g. tdvorak.tngl.sh/cloudreve")
    fork_p.add_argument("--repo-dir", type=Path, default=HERE.parent,
                        help="a checkout with the `tangled` remote (default: this repo)")
    pw_p = sub.add_parser("tangled-app-password",
                          help="create an app password on your PDS for tangled-release")
    pw_p.add_argument("handle", help="your handle, e.g. dxts.tngl.sh")
    pw_p.add_argument("--label", default="cloudreve-release", help="name shown in the PDS")
    sub.add_parser("status", help="unit and container status")
    logs_p = sub.add_parser("logs", help="follow logs")
    logs_p.add_argument("unit", nargs="?", default="cloudreve",
                        choices=["cloudreve", "caddy", "basement"])
    ssh_p = sub.add_parser("ssh", help="shell on the host")
    ssh_p.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if getattr(args, "apk", "absent") is None:  # apk / tangled-release: default follows --android-dir
        args.apk = release_apk(args.android_dir)

    if args.command == "tangled-release":  # talks to tangled.org only, no ssh
        tangled_release(args.tag, args.apk, args.android_dir, args.build, args.replace)
        return
    if args.command == "tangled-app-password":
        tangled_app_password(args.handle, args.label)
        return
    if args.command == "tangled-set-fork":
        tangled_set_fork(args.upstream, args.repo_dir)
        return

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
        elif args.command == "apk":
            publish_apk(remote, args.apk, args.android_dir, args.build)
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
