"""One supervised Chrome + proxy relay per business; never started by bootstrap."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

from bazarlyk.bootstrap.client import path_has_symlink, write_json_private
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.admission import InstallationRecoveryLock
from bazarlyk.composition.assembly import host_assembly, open_database
from bazarlyk.composition.files import lock_paths
from .config import ISOLATED, browser_state_directory, configured_target, port_available
from .control import process_identity, owns_listener


def credentials_directory(paths):
    """The private per-instance directory of business browser proxy credentials."""
    return paths.runtime / "secrets" / "browser-proxy"


def credentials_path(paths, business_id, config=None):
    """Fixed per-business credential file; the isolated mode has its own, the legacy one is preserved."""
    suffix = "-isolated" if (config or {}).get("mode") == ISOLATED else ""
    return credentials_directory(paths) / (business_id + suffix + ".json")


def read_credentials(path):
    """Private 0600 credential file: exactly a username and password, or both empty."""
    try:
        if path_has_symlink(path) or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 16384:
            raise ValueError("invalid credential file")
        credentials = json.loads(path.read_text())
        if set(credentials) != {"username", "password"} or not all(isinstance(x, str) for x in credentials.values()):
            raise ValueError("invalid credentials")
        username, password = credentials["username"], credentials["password"]
        if (bool(username) != bool(password) or ":" in username
            or any(ord(char) < 32 or ord(char) == 127 for char in username + password)):
            raise ValueError("incomplete credentials")
        return username, password
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DomainError("invalid_configuration", "The project proxy credential file is missing or invalid (requires mode 0600)") from exc


def upstream_url(paths, business_id, config):
    username, password = read_credentials(credentials_path(paths, business_id, config))
    try:
        parsed = urlsplit(config["proxy_server"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError("invalid_configuration", "The project proxy credential file is missing or invalid (requires mode 0600)") from exc
    auth = quote(username, safe="") + ":" + quote(password, safe="") + "@" if username else ""
    return urlunsplit((parsed.scheme, auth + parsed.netloc, "", "", ""))


def chrome_command(executable, config, relay_port):
    return [str(executable), "--headless=new", "--no-first-run", "--no-default-browser-check",
        "--password-store=basic",
        "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={config['cdp_port']}",
        f"--user-data-dir={config['user_data_dir']}", f"--proxy-server=http://127.0.0.1:{relay_port}",
        "--proxy-bypass-list=<-loopback>", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic",
        "--disable-background-networking", "--disable-sync", "--disable-extensions", "about:blank"]


def stop_child(child):
    if child is None:
        return
    # Each child was created in its own process group; never search/kill by port
    # or profile substring. systemd's cgroup is the final cleanup boundary.
    # Let the leader flush persistent profile state before touching its children.
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def close_chrome(child, config, runtime):
    # SIGTERM is not Chrome's orderly profile flush. Request a browser shutdown
    # through its verified managed endpoint, then retain bounded signal fallback.
    if child is None or child.poll() is not None:
        return
    try:
        if owns_listener(child.pid, config["cdp_port"]):
            subprocess.run(["node", str(runtime / "close.cjs"), f"http://127.0.0.1:{config['cdp_port']}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            child.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def serve(database, paths, business_id, runtime, chrome, *, stop=None):
    return _serve(database, paths, business_id, Path(runtime).resolve(), chrome, stop=stop)


@contextmanager
def _admitted_legacy_writer(database, paths, business_id, *, isolated=False):
    """Acquire the durable per-business lock while holding installation SH.

    The headless host service admits only legacy blocks and the isolated desktop
    session only isolated ones; both contend for this one ``service.lock``.

    The shared lock spans only admission, not Chrome's lifetime.  Once SH is
    released, an adopter can acquire EX, but its selected legacy inventory sees
    this held ``service.lock`` and refuses rather than commit an intent behind a
    running host writer.
    """
    with InstallationRecoveryLock(lock_paths(paths).recovery).writer():
        with database.read() as uow:
            if uow.repository("business_sandboxes").get(business_id):
                raise DomainError("environment_conflict", "This project browser is assigned to its sandbox")
            config = configured_target(uow.repository("businesses").get(business_id, required=True), paths)
        if (config.get("mode") == ISOLATED) is not isolated:
            raise DomainError("invalid_configuration", "This project browser is configured for a different browser mode")
        directory = browser_state_directory(paths, business_id)
        if path_has_symlink(directory):
            raise DomainError("invalid_configuration", "Browser state cannot follow symlinks")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path_has_symlink(directory / "service.lock"):
            raise DomainError("invalid_configuration", "Browser lock cannot follow symlinks")
        lock = (directory / "service.lock").open("a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DomainError("busy", "This project browser service is already running") from exc
            # This recheck and the durable flock occur under SH.  Adoption's EX
            # either commits before us (this rejects), or can only run after it
            # observes this held lock in its selected legacy inventory.
            with database.read() as uow:
                if uow.repository("business_sandboxes").get(business_id):
                    raise DomainError("environment_conflict", "This project browser is assigned to its sandbox")
        except BaseException:
            lock.close()
            raise
    try:
        yield config, directory
    finally:
        lock.close()


def _serve(database, paths, business_id, runtime, chrome, *, stop=None):
    with _admitted_legacy_writer(database, paths, business_id) as (config, directory):
        upstream = upstream_url(paths, business_id, config)  # Before launching anything.
        # A sandbox adoption can commit while this legacy service waits for the
        # per-business writer lock.  Check again before allocating a listener
        # or launching Chrome; the lock remains held after SH is released.
        with database.read() as uow:
            if uow.repository("business_sandboxes").get(business_id):
                raise DomainError("environment_conflict", "This project browser is assigned to its sandbox")
        if not port_available(config["cdp_port"]):
            raise DomainError("busy", "The assigned CDP port is occupied; refusing to attach to or kill its occupant")
        for name in ("user_data_dir", "downloads_dir"):
            path = Path(config[name])
            if path_has_symlink(path):
                raise DomainError("invalid_configuration", "Browser paths cannot follow symlinks")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        preferences = Path(config["user_data_dir"]) / "Default/Preferences"
        if path_has_symlink(preferences):
            raise DomainError("invalid_configuration", "Chrome preferences cannot follow symlinks")
        settings = json.loads(preferences.read_text()) if preferences.exists() else {}
        settings.setdefault("download", {}).update(default_directory=config["downloads_dir"], prompt_for_download=False)
        write_json_private(preferences, settings)
        relay = browser = None
        ready = directory / "ready.json"
        try:
            relay = subprocess.Popen(["node", str(runtime / "relay.cjs")], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, text=True)
            relay.stdin.write(json.dumps({"upstreamProxyUrl": upstream}) + "\n")
            relay.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(relay.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=10):
                    raise DomainError("operation_unavailable", "Project proxy relay startup timed out")
                relay_port = json.loads(relay.stdout.readline())["port"]
            if stop is not None and stop.is_set():
                return
            browser = subprocess.Popen(chrome_command(chrome, config, relay_port),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if stop is not None and stop.is_set():
                    return
                if relay.poll() is not None or browser.poll() is not None:
                    raise DomainError("operation_unavailable", "Managed browser or proxy relay exited during startup")
                try:
                    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{config['cdp_port']}/json/version", timeout=1) as response:
                        if not json.load(response).get("webSocketDebuggerUrl"):
                            raise ValueError("missing CDP endpoint")
                    if not owns_listener(browser.pid, config["cdp_port"]):
                        raise ValueError("CDP endpoint is not owned by the managed Chrome")
                    break
                except (OSError, ValueError):
                    time.sleep(0.1)
            else:
                raise DomainError("operation_unavailable", "Managed Chrome startup timed out")
            write_json_private(ready, {"instance": new_id(), "generation": config["generation"],
                "user_data_dir": config["user_data_dir"],
                "manager_pid": os.getpid(), "manager_identity": process_identity(os.getpid()),
                "chrome_pid": browser.pid, "chrome_identity": process_identity(browser.pid),
                "relay_pid": relay.pid, "relay_identity": process_identity(relay.pid)})
            while stop is None or not stop.wait(1):
                if stop is None:
                    time.sleep(1)
                if relay.poll() is not None or browser.poll() is not None:
                    raise DomainError("operation_unavailable", "Managed browser or proxy relay exited")
                with database.read() as uow:
                    if uow.repository("business_sandboxes").get(business_id):
                        return  # Profile handoff waits for this service's orderly shutdown and writer lock.
                    current = uow.repository("businesses").get(business_id, required=True)["config_json"].get("browser")
                if current != config:
                    return  # Config changes stop the old identity; operator explicitly restarts.
        finally:
            try:
                ready.unlink(missing_ok=True)
            finally:
                try:
                    try:
                        close_chrome(browser, config, runtime)
                    finally:
                        stop_child(browser)
                finally:
                    stop_child(relay)


def main():
    import threading
    from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
    parser = argparse.ArgumentParser(description=__doc__)
    add_instance_arguments(parser)
    parser.add_argument("--business-id", required=True)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--chrome", default="/usr/bin/google-chrome-stable")
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        from bazarlyk.service.changes import ChangeFeed
        paths = instance_paths_from_arguments(args, required=())
        database = open_database(paths, host_assembly(ChangeFeed()))
        serve(database, paths, args.business_id, args.runtime, args.chrome, stop=stop)
        return 0
    except DomainError as exc:
        # Permanent startup faults need operator repair, not another full startup.
        if exc.code in {"unsupported_schema", "invalid_database", "invalid_backup", "invalid_configuration", "environment_conflict"}:
            print("Project browser requires operator repair (" + exc.code + ").")
            return 78
        print("Project browser unavailable; check configuration, credentials, port and runtime.")
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # No exception repr: dependency errors may contain the upstream URL.
        print("Project browser unavailable; check configuration, credentials, port and runtime.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
