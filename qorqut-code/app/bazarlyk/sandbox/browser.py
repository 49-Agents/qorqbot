"""Constrained Chrome lifecycle for an exact observed BusinessSandbox browser."""

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from bazarlyk.bootstrap.client import path_has_symlink
from bazarlyk.browser.config import legacy_target as configured_target
from bazarlyk.browser.socket import BrowserBinding
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.safe_io import parent as safe_parent


SECCOMP_SHA256 = "82872222996198cf07177a15c84a0fcd1c8a00f9786aa68a528888d4a6a42a2e"
CHROME_VERSION = "153.0.8010.36"


@dataclass(frozen=True)
class BrowserRuntime:
    image: str
    seccomp_profile: Path
    seccomp_sha256: str = SECCOMP_SHA256

    def profile_document(self):
        path = Path(self.seccomp_profile)
        try:
            if (not re.fullmatch(r"[a-f0-9]{64}", self.seccomp_sha256) or not path.is_absolute()
                    or path_has_symlink(path) or hashlib.sha256(path.read_bytes()).hexdigest() != self.seccomp_sha256):
                raise ValueError
            return json.loads(path.read_text())
        except OSError:
            raise DomainError("configuration_missing", "Pinned browser seccomp profile is unavailable") from None
        except (ValueError, TypeError):
            raise DomainError("configuration_missing", "Pinned browser seccomp profile checksum or JSON differs") from None

    def container_options(self):
        self.profile_document()
        return ("--security-opt", "seccomp=" + str(Path(self.seccomp_profile)), "--shm-size", "256m")


@dataclass(frozen=True)
class ChromeProcess:
    pid: int
    start_ticks: str
    cdp_port: int


@dataclass(frozen=True)
class ProfileLock:
    """One live flock tied to the registered profile lock inode."""

    business_id: str
    generation: int
    profile: Path
    lock_path: Path
    device: int
    inode: int
    descriptor: object


class SandboxBrowser:
    """Hold service.lock through an uncertain start/readiness/close until Chrome exits."""

    def __init__(self, docker, runtime, *, monotonic=time.monotonic, sleep=time.sleep):
        self.docker, self.runtime = docker, runtime
        self.monotonic, self.sleep, self._locks, self._roots = monotonic, sleep, {}, {}

    @staticmethod
    def _key(environment):
        return environment.ownership.business_id, environment.generation

    def _container(self, paths, config, environment):
        if self.runtime.image != config.browser_image:
            raise DomainError("configuration_missing", "Browser runtime image does not match host sandbox configuration")
        current = self.docker.owned("container", environment.browser_id, environment.ownership)
        labels, host = (current or {}).get("Config", {}).get("Labels", {}), (current or {}).get("HostConfig", {})
        profile, downloads = Path(paths.browser) / "profile", Path(paths.browser) / "downloads"
        mounts = {(Path(m["Source"]), Path(m["Destination"]), m["RW"])
                  for m in (current or {}).get("Mounts", ()) if m.get("Type") == "bind"}
        documents = []
        for option in host.get("SecurityOpt") or ():
            if option.startswith("seccomp="):
                try:
                    documents.append(json.loads(option.removeprefix("seccomp=")))
                except (TypeError, ValueError):
                    pass
        if (current is None or not current.get("State", {}).get("Running")
                or labels.get("qorqut.role") != "browser" or labels.get("qorqut.generation") != str(environment.generation)
                or current.get("Config", {}).get("Image") != self.runtime.image or current.get("Config", {}).get("User") != "1000:1000"
                or not host.get("ReadonlyRootfs") or host.get("Privileged") or host.get("CapDrop") != ["ALL"] or host.get("CapAdd")
                or "no-new-privileges" not in (host.get("SecurityOpt") or ()) or self.runtime.profile_document() not in documents
                or host.get("ShmSize") != 256 * 1024 * 1024
                or (profile, profile, True) not in mounts or (downloads, downloads, True) not in mounts):
            raise DomainError("environment_conflict", "Browser container no longer matches its trusted confinement")
        return current

    @staticmethod
    def _writer_lock(paths, environment):
        directory = Path(paths.browser)
        lock_path = directory / "service.lock"
        try:
            with safe_parent(lock_path, create=True) as (parent, name):
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                     0o600, dir_fd=parent)
                info = os.fstat(descriptor)
        except OSError:
            raise DomainError("environment_conflict", "Browser writer lock is not a safe regular registered file") from None
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise DomainError("environment_conflict", "Browser writer lock is not a regular registered file")
        lock = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise DomainError("environment_conflict", "Existing host browser writer must be drained before profile handoff") from None
        try:
            with safe_parent(lock_path) as (parent, name):
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
                raise ValueError
        except (OSError, ValueError, DomainError):
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
            raise DomainError("environment_conflict", "Browser writer lock changed while it was acquired") from None
        return ProfileLock(environment.ownership.business_id, environment.generation, directory / "profile",
                           lock_path, info.st_dev, info.st_ino, lock)

    @staticmethod
    def profile_lock_unheld(paths) -> bool:
        """Prove the registered profile lock has no writer without creating it.

        This is used only by the explicit stopped-sandbox rollback boundary.
        A missing lock is harmless; a swapped, special, or unreadable path is
        ambiguous and must keep the sandbox intent in place.
        """
        lock_path = Path(paths.browser) / "service.lock"
        try:
            with safe_parent(lock_path) as (parent, name):
                try:
                    descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                         dir_fd=parent)
                except FileNotFoundError:
                    return True
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode):
                        return False
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return False
                    finally:
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                        except OSError:
                            return False
                    return True
                finally:
                    os.close(descriptor)
        except FileNotFoundError:
            # No profile was ever materialized.  ``safe_parent`` walked every
            # existing component without following links; check the absent tail
            # as well before treating a never-provisioned stopped row as clear.
            return not path_has_symlink(lock_path)
        except (DomainError, OSError):
            return False

    def _lock(self, paths, environment):
        key = self._key(environment)
        if key not in self._locks:
            self._locks[key] = self._writer_lock(paths, environment)
        elif not self.owns_profile_lock(paths, environment):
            raise DomainError("environment_conflict", "Stored browser profile writer lock no longer matches this environment")
        return key

    def owns_profile_lock(self, paths, environment):
        """Return whether this controller still owns this exact registered flock.

        The cache is only a descriptor record.  Re-stat both sides on every
        answer so an unlinked/replaced ``service.lock`` or another generation's
        descriptor can never be treated as browser ownership.
        """
        record = self._locks.get(self._key(environment))
        directory = Path(paths.browser)
        lock_path, profile = directory / "service.lock", directory / "profile"
        if (record is None or record.business_id != environment.ownership.business_id
                or record.generation != environment.generation or record.profile != profile
                or record.lock_path != lock_path):
            return False
        try:
            descriptor = record.descriptor
            stored = os.fstat(descriptor.fileno())
            with safe_parent(lock_path) as (parent, name):
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (descriptor.closed or not stat.S_ISREG(stored.st_mode) or not stat.S_ISREG(current.st_mode)
                    or (stored.st_dev, stored.st_ino) != (record.device, record.inode)
                    or (current.st_dev, current.st_ino) != (record.device, record.inode)):
                return False
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (AttributeError, OSError, ValueError, BlockingIOError, DomainError):
            return False

    def _exec(self, paths, config, environment, *arguments):
        self._container(paths, config, environment)
        return self.docker.exec(environment.browser_id, environment.ownership, *arguments, timeout=30)

    def _observed_container(self, paths, environment):
        current = self.docker.owned("container", environment.browser_id, environment.ownership)
        labels = (current or {}).get("Config", {}).get("Labels", {})
        profile, downloads = Path(paths.browser) / "profile", Path(paths.browser) / "downloads"
        mounts = {(Path(m["Source"]), Path(m["Destination"]), m["RW"])
                  for m in (current or {}).get("Mounts", ()) if m.get("Type") == "bind"}
        if (current is None or current.get("Id") != environment.browser_id
                or labels.get("qorqut.role") != "browser"
                or labels.get("qorqut.generation") != str(environment.generation)
                or current.get("Config", {}).get("User") != "1000:1000"
                or (profile, profile, True) not in mounts or (downloads, downloads, True) not in mounts):
            raise DomainError("environment_conflict", "Observed browser container does not own this registered profile")
        return current

    def _observed_exec(self, paths, environment, *arguments):
        current = self._observed_container(paths, environment)
        if not current.get("State", {}).get("Running"):
            raise DomainError("environment_conflict", "Observed browser container is no longer running")
        return self.docker.exec(environment.browser_id, environment.ownership, *arguments, timeout=30)

    def _detached(self, paths, config, environment, *arguments):
        self._container(paths, config, environment)
        profile = str(Path(paths.browser) / "profile")
        return self.docker.call("exec", "-d", "--user", "1000:1000",
                                "--env", "HOME=" + profile, "--env", "XDG_CONFIG_HOME=" + profile,
                                "--env", "XDG_CACHE_HOME=/tmp", environment.browser_id, *arguments, timeout=30)

    @staticmethod
    def _parse_root(value):
        rows = [row.split() for row in value.splitlines()]
        if any(len(row) != 4 or not all(part.isdecimal() for part in row)
               or int(row[0]) < 1 or not 1024 <= int(row[2]) <= 65535 for row in rows):
            raise DomainError("environment_conflict", "Browser profile has an ambiguous Chrome writer")
        candidates = {int(row[0]): row for row in rows}
        roots = [row for row in rows if int(row[3]) not in candidates]
        if len(candidates) != len(rows) or (rows and len(roots) != 1):
            raise DomainError("environment_conflict", "Browser profile has an ambiguous Chrome writer")
        # Before exec/argv rewriting, a Chrome fork inherits the browser's exact
        # profile arguments. It is a descendant, not another browser root. Only
        # accept a complete, acyclic ancestry to the one observed root; unrelated
        # candidates, inconsistent ages and different CDP ports still refuse.
        for row in rows:
            seen = {int(row[0])}
            while int(row[3]) in candidates:
                parent = candidates[int(row[3])]
                if (int(parent[0]) in seen or int(parent[1]) > int(row[1])
                        or parent[2] != row[2]):
                    raise DomainError("environment_conflict", "Browser profile has an ambiguous Chrome writer")
                seen.add(int(parent[0]))
                row = parent
        return ChromeProcess(int(roots[0][0]), roots[0][1], int(roots[0][2])) if roots else None

    def _root_with(self, execute, paths):
        script = '''profile="$1"
for proc in /proc/[0-9]*; do
  [ "$(readlink "$proc/exe" 2>/dev/null)" = /opt/google/chrome/chrome ] || continue
  grep -azqxF -- "--user-data-dir=$profile" "$proc/cmdline" || continue
  grep -azq -- '^--type=' "$proc/cmdline" && continue
  port=$(grep -aoE -- '--remote-debugging-port=[0-9]+' "$proc/cmdline" | sed 's/.*=//')
  stat=$(cat "$proc/stat") || continue
  start=$(printf '%s\\n' "$stat" | sed 's/^.*) //' | awk '{print $20}')
  parent=$(printf '%s\\n' "$stat" | sed 's/^.*) //' | awk '{print $2}')
  case "$start" in ''|*[!0-9]*) continue;; esac
  case "$parent" in ''|*[!0-9]*) continue;; esac
  case "$port" in ''|*[!0-9]*) continue;; esac
  printf '%s %s %s %s\\n' "$(basename "$proc")" "$start" "$port" "$parent"
done'''
        return self._parse_root(execute("/bin/sh", "-c", script, "browser-process", str(Path(paths.browser) / "profile")))

    def _root(self, paths, config, environment):
        return self._root_with(lambda *arguments: self._exec(paths, config, environment, *arguments), paths)

    def _observed_root(self, paths, environment):
        return self._root_with(lambda *arguments: self._observed_exec(paths, environment, *arguments), paths)

    @staticmethod
    def _parse_pids(value):
        values = value.splitlines()
        if any(not value.isdecimal() for value in values):
            raise DomainError("environment_conflict", "Chrome process inventory is invalid")
        return values

    def _chrome_pids_with(self, execute):
        script = '''for proc in /proc/[0-9]*; do
  [ "$(readlink "$proc/exe" 2>/dev/null)" = /opt/google/chrome/chrome ] || continue
  basename "$proc"
done'''
        return self._parse_pids(execute("/bin/sh", "-c", script))

    def _chrome_pids(self, paths, config, environment):
        return self._chrome_pids_with(lambda *arguments: self._exec(paths, config, environment, *arguments))

    def _observed_chrome_pids(self, paths, environment):
        return self._chrome_pids_with(lambda *arguments: self._observed_exec(paths, environment, *arguments))

    def _remember_root(self, environment, root):
        key, previous = self._key(environment), self._roots.get(self._key(environment))
        if previous is not None and root != previous:
            raise DomainError("environment_conflict", "Chrome profile root changed during this lifecycle lease")
        if root is not None:
            self._roots[key] = root

    def _diagnostics(self, paths, config, environment):
        script = '''for proc in /proc/[0-9]*; do
  [ "$(readlink "$proc/exe" 2>/dev/null)" = /opt/google/chrome/chrome ] || continue
  pid=$(basename "$proc")
  userns=$(readlink "$proc/ns/user") || continue
  cap=$(awk '/^CapEff:/{print $2}' "$proc/status")
  nnp=$(awk '/^NoNewPrivs:/{print $2}' "$proc/status")
  seccomp=$(awk '/^Seccomp:/{print $2}' "$proc/status")
  filters=$(awk '/^Seccomp_filters:/{print $2}' "$proc/status")
  renderer=0
  grep -aq -- '--type=renderer' "$proc/cmdline" && renderer=1
  printf '%s %s %s %s %s %s %s\\n' "$pid" "$userns" "$cap" "$nnp" "$seccomp" "$filters" "$renderer"
done'''
        rows = [row.split() for row in self._exec(paths, config, environment, "/bin/sh", "-c", script).splitlines()]
        root = self._root(paths, config, environment)
        host_row = next((row for row in rows if root is not None and row[0] == str(root.pid)), None)
        renderers = [row for row in rows if len(row) == 7 and row[6] == "1"]
        if (len(rows) < 2 or host_row is None or any(len(row) != 7 or row[3:5] != ["1", "2"] or not row[5].isdecimal() for row in rows)
                or len({row[1] for row in rows}) < 2
                or any(row[1] == host_row[1] and row[2] != "0000000000000000" for row in rows)
                or not renderers or any(row[2] != "0000000000000000" or int(row[5]) <= int(host_row[5]) for row in renderers)):
            raise DomainError("environment_unhealthy", "Chrome renderer sandbox diagnostics did not pass")

    def begin(self, business, paths, config, environment):
        target = configured_target(business, paths.instance)
        if target["user_data_dir"] != str(Path(paths.browser) / "profile") or target["downloads_dir"] != str(Path(paths.browser) / "downloads"):
            raise DomainError("environment_conflict", "Registered browser profile paths differ from sandbox mounts")
        self._container(paths, config, environment)
        key = self._lock(paths, environment)
        if not self.owns_profile_lock(paths, environment):
            raise DomainError("environment_conflict", "Browser profile writer lock changed before Chrome startup")
        root = self._root(paths, config, environment)
        if root is not None:
            self._remember_root(environment, root)
            return root
        if self._chrome_pids(paths, config, environment):
            raise DomainError("environment_conflict", "Unidentified Chrome process must exit before profile handoff")
        # A same-generation Chrome crash/restart gets a new PID/start identity.
        # We may forget the prior identity only after this held writer lock has
        # observed that no Chrome process remains for this profile.
        self._roots.pop(key, None)
        self._detached(paths, config, environment, "google-chrome-stable", "--headless=new", "--no-first-run",
                       "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(target["cdp_port"]),
                       "--password-store=basic", "--user-data-dir=" + target["user_data_dir"],
                       "--download-default-directory=" + target["downloads_dir"],
                       "data:text/html,<body>BusinessSandbox%20readiness</body>")
        return None

    def readiness(self, business, paths, config, environment, *, timeout=30):
        target, deadline, key = configured_target(business, paths.instance), self.monotonic() + timeout, self._key(environment)
        while self.monotonic() < deadline:
            root = self._root(paths, config, environment)
            if root is not None:
                self._remember_root(environment, root)
                try:
                    version = json.loads(self._exec(paths, config, environment, "/bin/sh", "-c",
                        "curl --fail --silent http://127.0.0.1:" + str(target["cdp_port"]) + "/json/version || :"))
                except (TypeError, ValueError):
                    version = {}
                if version.get("Browser") == "Chrome/" + CHROME_VERSION:
                    self._diagnostics(paths, config, environment)
                    return True
            self.sleep(.1)
        raise DomainError("environment_unhealthy", "Confined Chrome did not become ready")

    def browser_readiness(self, business, paths, config, environment):
        self.begin(business, paths, config, environment)
        return self.readiness(business, paths, config, environment)

    def observed_browser_binding(self, business, paths, config, environment):
        """Return only the current, lock-owned Chrome socket identity.

        This performs no launch, restart, network connection or listener setup.
        The caller gives this binding to the fixed host CDP connector; Chrome's
        loopback endpoint is deliberately absent from this public value.
        """
        if (business.get("id") != environment.ownership.business_id
                or getattr(paths, "business_id", business.get("id")) != business.get("id")):
            raise DomainError("environment_conflict", "Browser binding business identity is not registered")
        target = configured_target(business, paths.instance)
        current = self._container(paths, config, environment)
        if not self.owns_profile_lock(paths, environment):
            raise DomainError("operation_unavailable", "Sandbox browser profile lock is not currently owned", retryable=True)
        root = self._root(paths, config, environment)
        if root is None:
            raise DomainError("operation_unavailable", "Sandbox browser is not running", retryable=True)
        self._remember_root(environment, root)
        if root.cdp_port != target["cdp_port"]:
            raise DomainError("environment_conflict", "Observed Chrome CDP port differs from its registered browser")
        return BrowserBinding(business["id"], current["Id"], environment.generation,
                              f"{current['Id']}:{root.pid}:{root.start_ticks}", root.cdp_port)

    def close(self, business, paths, config, environment):
        """Run the image's loopback-only CDP Browser.close helper."""
        self._container(paths, config, environment)
        return self.close_observed(business, paths, environment)

    def close_observed(self, business, paths, environment):
        """Close an exact prior browser generation without current launch config.

        Replacement and emergency-stop paths may have a changed image or missing
        configuration. They still need an orderly profile handoff, so this accepts
        only the recorded owned container identity and derives its private CDP
        port from the exact root Chrome process rather than new configuration.
        """
        key, current = self._key(environment), self._observed_container(paths, environment)
        if not current.get("State", {}).get("Running"):
            self._release(key)
            return True
        root = self._observed_root(paths, environment)
        self._remember_root(environment, root)
        if root is None and not self._observed_chrome_pids(paths, environment):
            self._release(key)
            return True
        if root is None:
            raise DomainError("environment_conflict", "Chrome profile root disappeared before orderly close")
        self._observed_exec(paths, environment, "/usr/local/bin/qorqut-browser-close", str(root.cdp_port))
        deadline = self.monotonic() + 30
        while self.monotonic() < deadline:
            if not self._observed_chrome_pids(paths, environment):
                self._release(key)
                return True
            self.sleep(.1)
        raise DomainError("environment_conflict", "Chrome did not exit after Browser.close")

    def _release(self, key):
        self._roots.pop(key, None)
        lock = self._locks.pop(key, None)
        if lock is not None:
            fcntl.flock(lock.descriptor, fcntl.LOCK_UN)
            lock.descriptor.close()
