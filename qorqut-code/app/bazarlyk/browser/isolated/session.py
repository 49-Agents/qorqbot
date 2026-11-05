"""One persistent headed Chrome in a loopback-only namespace, with proxy-only egress.

Run by the operator's ``PrivateNetwork=yes`` system unit. Nothing starts unless the
process is in a namespace whose only interface is a working loopback with no
route, the gateway is live, and the assigned proxy exits from the expected IP.
There is no direct fallback: a failed check withdraws readiness, a different exit
IP stops the browser, and only the gateway's fixed bridges reach the host.

Inside the namespace:

    Chrome --> proxy-chain relay (credentials on stdin) --> loopback TCP relay
           --> upstream.sock (gateway) --> the frozen proxy IP/port
    gateway <-- cdp.sock <-- loopback CDP bridge <-- Chrome's CDP port
    gateway <-- vnc.sock <-- Xtigervnc (private RFB socket, VNC password file)
"""

import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener

from bazarlyk.bootstrap.client import path_has_symlink, write_json_private
from qorqut_operations.contracts import DomainError, new_id
from ..config import ISOLATED
from ..control import owns_listener, process_identity
from ..service import _admitted_legacy_writer, read_credentials, credentials_path
from . import EX_OK, exit_code
from .bridge import Forwarder, claim_socket_path, dial_tcp, dial_unix, tcp_listener, unix_listener
from .deployment import Binaries, load, open_deployment_database
from .exitcheck import evaluate, require_exit
from .gateway import current_assignment, read_gateway
from .netns import probe
from .processes import OwnershipLost, identity_alive, leader_exited, netns_identity, stop_group

PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy", "socks_proxy"})
LOG = "isolated-session: "


def log(message):
    print(LOG + message, flush=True)


def xvnc_command(binaries, deployment):
    return [binaries.xvnc, deployment.display_name, "-geometry", f"{deployment.width}x{deployment.height}", "-depth", "24",
            "-rfbunixpath", str(deployment.vnc_socket), "-rfbunixmode", "384", "-rfbport", "-1",
            "-SecurityTypes", "VncAuth", "-PasswordFile", str(deployment.vnc_password_file),
            "-AlwaysShared", "-nolisten", "tcp", "-auth", str(deployment.xauthority)]


def chrome_command(binaries, deployment, config, relay_port):
    """Headed, maximized, sandboxed. The proxy, bypass, DNS, WebRTC and QUIC flags are defense in depth."""
    return [binaries.chrome, f"--user-data-dir={config['user_data_dir']}",
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={config['cdp_port']}",
            "--class=QorqutBusinessBrowser", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
            "--disable-session-crashed-bubble", "--disable-background-networking", "--disable-sync", "--disable-extensions",
            f"--proxy-server=http://127.0.0.1:{relay_port}", "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic",
            f"--window-size={deployment.width},{deployment.height}", "--start-maximized", "about:blank"]


def relay_command(deployment):
    return [deployment.node, str(deployment.tools_dir / "relay.cjs")]


def clear_stale_socket(path):
    """Remove only a socket that positively refuses connections; a served, foreign or unprovable path is kept."""
    claim_socket_path(path, replace_stale=True)


def alive_pid(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Session:
    def __init__(self, database, deployment, config, directory, *, binaries=Binaries(), stop=None, environment=None,
                 x_directory="/tmp", exit_check=evaluate):
        self.database, self.deployment, self.config, self.directory = database, deployment, config, directory
        self.binaries, self.stop_event = binaries, stop or threading.Event()
        self.base_environment = dict(os.environ if environment is None else environment)
        self.x_directory, self.exit_check = Path(x_directory), exit_check
        self.recheck_requested = threading.Event()
        self.instance = new_id()
        self.children, self.forwarders, self.sockets, self.cleaned = {}, [], [], set()
        self.relay_port = self.relay_client_port = None
        self.gateway, self.netns, self.published, self.checks = None, None, None, 0
        self.window_manager_restarts = 0

    # -- preconditions -----------------------------------------------------------------------

    def preflight(self):
        proxies = sorted(key for key in self.base_environment if key.lower() in PROXY_VARIABLES)
        if proxies:
            raise DomainError("invalid_configuration", "Refusing to start with proxy environment variables")
        self.netns = probe()["netns"]
        self.gateway = self.verify_gateway()
        lock = self.x_directory / f".X{self.deployment.display}-lock"
        try:
            pid = int(lock.read_text().strip())
            if pid > 0 and alive_pid(pid):
                raise DomainError("busy", f"Display {self.deployment.display_name} is already running")
        except (FileNotFoundError, ValueError):
            pass
        clear_stale_socket(self.x_directory / ".X11-unix" / f"X{self.deployment.display}")
        for path in (self.deployment.cdp_socket, self.deployment.vnc_socket):
            clear_stale_socket(path)
        password = self.deployment.vnc_password_file
        try:
            info = os.lstat(password)
        except FileNotFoundError:
            raise DomainError("invalid_configuration", "The VNC password file has not been provisioned") from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DomainError("invalid_configuration", "The VNC password file must be a private regular file owned by the service user")
        if not (self.deployment.tools_dir / "relay.cjs").is_file():
            raise DomainError("invalid_configuration", "The installed browser tools do not include the relay")
        profile = Path(self.config["user_data_dir"])
        try:
            host, _, owner = os.readlink(profile / "SingletonLock").rpartition("-")
            if host == socket.gethostname() and owner.isdigit() and alive_pid(int(owner)):
                raise DomainError("busy", "The browser profile is already open by another process")
        except OSError:
            pass

    def verify_gateway(self):
        """The recorded gateway is the live host process serving exactly this assignment."""
        deployment, config = self.deployment, self.config
        try:
            gateway = read_gateway(deployment)
            checks = (gateway["business_id"] == deployment.business_id and gateway["generation"] == config["generation"]
                      and gateway["cdp_port"] == config["cdp_port"] and gateway["runtime_dir"] == str(deployment.runtime_dir)
                      and gateway["vnc_port"] == deployment.vnc_port and gateway["vnc_interface"] == deployment.vnc_interface
                      and identity_alive(gateway["pid"], gateway["identity"])
                      and gateway["netns"] != self.netns
                      and netns_identity(gateway["pid"]) == gateway["netns"]
                      and owns_listener(gateway["pid"], config["cdp_port"], "127.0.0.1")
                      and owns_listener(gateway["pid"], deployment.vnc_port, deployment.vnc_interface)
                      and stat.S_ISSOCK(os.lstat(deployment.upstream_socket).st_mode))
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            checks = False
        if not checks:
            raise DomainError("operation_unavailable", "The isolated browser gateway is not running for this assignment")
        return gateway

    # -- start -------------------------------------------------------------------------------

    def environment(self):
        xdg = self.directory / "xdg"
        env = {key: self.base_environment[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "USER", "LOGNAME")
               if key in self.base_environment}
        env.update(DISPLAY=self.deployment.display_name, XAUTHORITY=str(self.deployment.xauthority),
                   XDG_CONFIG_HOME=str(xdg / "config"), XDG_CACHE_HOME=str(xdg / "cache"), XDG_DATA_HOME=str(xdg / "data"))
        return env

    def spawn(self, role, argv, **options):
        # Each child leads its own process group so cleanup can reach its helpers and nothing else.
        options.setdefault("stdin", subprocess.DEVNULL)
        process = subprocess.Popen(argv, env=self.environment(), start_new_session=True, **options)
        self.children[role] = process
        self.cleaned.discard(role)
        return process

    def prepare_paths(self):
        for name in ("user_data_dir", "downloads_dir"):
            path = Path(self.config[name])
            if path_has_symlink(path):
                raise DomainError("invalid_configuration", "Browser paths cannot follow symlinks")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        ignore = Path(self.config["user_data_dir"]).parent / ".gitignore"
        if not ignore.exists() and not path_has_symlink(ignore):
            ignore.write_text("*\n")
        for name in ("config", "cache", "data"):
            path = self.directory / "xdg" / name
            if path_has_symlink(path):
                raise DomainError("invalid_configuration", "Browser paths cannot follow symlinks")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        preferences = Path(self.config["user_data_dir"]) / "Default/Preferences"
        if path_has_symlink(preferences):
            raise DomainError("invalid_configuration", "Chrome preferences cannot follow symlinks")
        settings = json.loads(preferences.read_text()) if preferences.exists() else {}
        settings.setdefault("download", {}).update(default_directory=self.config["downloads_dir"], prompt_for_download=False)
        write_json_private(preferences, settings)

    def start_relay(self):
        username, password = read_credentials(credentials_path(self.deployment.paths, self.deployment.business_id, self.config))
        if not username:
            raise DomainError("invalid_configuration", "The isolated browser requires proxy credentials")
        # The proxy-chain relay believes the loopback relay is its authenticated upstream. The bytes
        # reach the real proxy only through upstream.sock; credentials never touch argv or the gateway.
        upstream_socket = str(self.deployment.upstream_socket)
        listener = tcp_listener("127.0.0.1", 0)
        self.relay_client_port = listener.getsockname()[1]
        self.forwarders.append(Forwarder("upstream-loopback", listener, lambda: dial_unix(upstream_socket)).start())
        url = f"http://{quote(username, safe='')}:{quote(password, safe='')}@127.0.0.1:{self.relay_client_port}"
        relay = self.spawn("relay", relay_command(self.deployment), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True)
        relay.stdin.write(json.dumps({"upstreamProxyUrl": url}) + "\n")
        relay.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(relay.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                raise DomainError("operation_unavailable", "Project proxy relay startup timed out")
            self.relay_port = json.loads(relay.stdout.readline())["port"]

    def start_display(self):
        deployment = self.deployment
        xauthority = deployment.xauthority
        if path_has_symlink(xauthority):
            raise DomainError("invalid_configuration", "Xauthority cannot follow symlinks")
        xauthority.unlink(missing_ok=True)
        os.close(os.open(xauthority, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        command = f"add {deployment.display_name} . {secrets.token_hex(16)}\n"
        subprocess.run([self.binaries.xauth, "-q", "-f", str(xauthority), "source", "-"], input=command.encode(),
                       env=self.environment(), check=True, stdout=subprocess.DEVNULL, timeout=15)
        self.sockets.append(deployment.vnc_socket)
        self.spawn("xvnc", xvnc_command(self.binaries, deployment), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if leader_exited(self.children["xvnc"]):
                raise DomainError("operation_unavailable", "Xtigervnc exited before the display was ready")
            if subprocess.run([self.binaries.xdpyinfo], env=self.environment(), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise DomainError("operation_unavailable", "The isolated display was not ready in time")
        for argument in (["s", "off"], ["-dpms"]):
            subprocess.run([self.binaries.xset, *argument], env=self.environment(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        self.spawn("window_manager", [self.binaries.window_manager], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_chrome(self):
        config = self.config
        self.sockets.append(self.deployment.cdp_socket)
        self.forwarders.append(Forwarder("cdp", unix_listener(self.deployment.cdp_socket),
                                         lambda: dial_tcp("127.0.0.1", config["cdp_port"])).start())
        chrome = self.spawn("chrome", chrome_command(self.binaries, self.deployment, config, self.relay_port),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 45
        opener = build_opener(ProxyHandler({}))
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return False
            if any(leader_exited(self.children[role]) for role in ("relay", "xvnc", "chrome")):
                raise DomainError("operation_unavailable", "Managed browser, display or proxy relay exited during startup")
            try:
                with opener.open(f"http://127.0.0.1:{config['cdp_port']}/json/version", timeout=1) as response:
                    if not json.load(response).get("webSocketDebuggerUrl"):
                        raise ValueError("missing CDP endpoint")
                if not owns_listener(chrome.pid, config["cdp_port"], "127.0.0.1"):
                    raise ValueError("CDP endpoint is not owned by the managed Chrome")
                if netns_identity(chrome.pid) != self.netns:
                    raise DomainError("invalid_configuration", "Chrome is not in the isolated network namespace")
                return True
            except (OSError, ValueError):
                time.sleep(0.1)
        raise DomainError("operation_unavailable", "Managed Chrome startup timed out")

    def start(self):
        self.prepare_paths()
        self.write_session()
        self.start_relay()
        self.recheck(initial=True)  # No display, Chrome or readiness before the proxy proves the expected exit.
        self.start_display()
        if self.start_chrome():
            self.publish()

    # -- readiness and exit checks -----------------------------------------------------------

    def write_session(self):
        write_json_private(self.directory / "session.json", {
            "instance": self.instance, "manager_pid": os.getpid(), "manager_identity": process_identity(os.getpid()),
            "generation": self.config["generation"]})

    def receipt(self):
        chrome, relay, xvnc = (self.children[role] for role in ("chrome", "relay", "xvnc"))
        return {"instance": self.instance, "mode": ISOLATED, "business_id": self.deployment.business_id,
                "generation": self.config["generation"], "user_data_dir": self.config["user_data_dir"],
                "cdp_port": self.config["cdp_port"], "display": self.deployment.display, "netns": self.netns,
                "manager_pid": os.getpid(), "manager_identity": process_identity(os.getpid()),
                "chrome_pid": chrome.pid, "chrome_identity": process_identity(chrome.pid),
                "relay_pid": relay.pid, "relay_identity": process_identity(relay.pid),
                "xvnc_pid": xvnc.pid, "xvnc_identity": process_identity(xvnc.pid),
                "gateway": {key: self.gateway[key] for key in ("instance", "pid", "identity", "netns")},
                "exit_ip": self.config["expected_exit_ip"]}

    def publish(self):
        write_json_private(self.directory / "ready.json", self.receipt())
        self.published = True

    def withdraw(self):
        self.published = False
        (self.directory / "ready.json").unlink(missing_ok=True)

    def recheck(self, initial=False):
        """One proxy exit check. Failure withdraws readiness; a different exit IP is fatal."""
        result, observed = self.exit_check(self.relay_port, self.deployment.exit_check, self.config["expected_exit_ip"])
        self.checks += 1
        write_json_private(self.directory / "recheck.json", {
            "instance": self.instance, "sequence": self.checks, "result": result,
            "observed_exit_ip": observed, "expected_exit_ip": self.config["expected_exit_ip"],
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        if result == "mismatch":
            raise DomainError("invalid_configuration", "The proxy exits from a different IP than the assigned expected_exit_ip")
        if result == "failed":
            if initial:
                raise DomainError("operation_unavailable", "The assigned proxy did not answer the exit check")
            if self.published:
                log("proxy exit check failed; readiness withdrawn")
            self.withdraw()
            return False
        if not self.published and not initial:
            log("proxy exit check recovered; readiness restored")
            self.publish()
        return True

    # -- supervision -------------------------------------------------------------------------

    def gateway_lost(self):
        try:
            gateway = read_gateway(self.deployment)
        except (OSError, ValueError):
            return True
        return (gateway.get("instance") != self.gateway["instance"]
                or not identity_alive(self.gateway["pid"], self.gateway["identity"]))

    def supervise(self, poll=1.0):
        next_check = time.monotonic() + self.deployment.recheck_seconds
        while not self.stop_event.wait(poll):
            for role in ("relay", "xvnc", "chrome"):
                if leader_exited(self.children[role]):
                    log(f"{role} exited; stopping this browser")
                    raise DomainError("operation_unavailable", "A managed browser component exited")
            manager = self.children["window_manager"]
            if leader_exited(manager):
                if self.window_manager_restarts >= 3:
                    raise DomainError("operation_unavailable", "The window manager keeps exiting")
                self.window_manager_restarts += 1
                with suppress(OwnershipLost):
                    stop_group(manager, 5, log)
                self.spawn("window_manager", [self.binaries.window_manager], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not all(forwarder.alive() for forwarder in self.forwarders):
                raise DomainError("operation_unavailable", "A namespace bridge stopped")
            if current_assignment(self.database, self.deployment) != self.config:
                return EX_OK  # Config changes stop the old identity; the operator restarts explicitly.
            if self.gateway_lost():
                if current_assignment(self.database, self.deployment) != self.config:
                    return EX_OK  # The gateway stopped for the same config change.
                log("the host gateway changed or stopped; stopping this browser")
                raise DomainError("operation_unavailable", "The isolated browser gateway is gone")
            if self.recheck_requested.is_set() or time.monotonic() >= next_check:
                self.recheck_requested.clear()
                ok = self.recheck()
                next_check = time.monotonic() + (self.deployment.recheck_seconds if ok else min(30, self.deployment.recheck_seconds))
        return EX_OK

    # -- shutdown ----------------------------------------------------------------------------

    def close_browser(self):
        """Orderly profile flush through Browser.close on the CDP endpoint this Chrome owns."""
        chrome = self.children.get("chrome")
        if chrome is None or chrome.poll() is not None:
            return
        try:
            if not owns_listener(chrome.pid, self.config["cdp_port"], "127.0.0.1"):
                return
            with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{self.config['cdp_port']}/json/version", timeout=3) as response:
                url = json.load(response)["webSocketDebuggerUrl"]
            from websockets.sync.client import connect
            with connect(url, open_timeout=3, close_timeout=2, max_size=1 << 20) as websocket:
                websocket.send(json.dumps({"id": 1, "method": "Browser.close"}))
            # Peek without reaping: the leader must stay unreaped until its process group is clean.
            deadline = time.monotonic() + 15
            while not leader_exited(chrome) and time.monotonic() < deadline:
                time.sleep(0.1)
        except Exception:
            pass  # Bounded signal fallback follows; cleanup must not raise.

    def shutdown(self):
        # Readiness first; then Chrome (flushing the profile while its display and relay still exist), then the rest.
        with suppress(OSError):
            self.withdraw()
        self.close_browser()
        lost = []
        for role, timeout in (("chrome", 20), ("window_manager", 5), ("xvnc", 10), ("relay", 5)):
            process = self.children.get(role)
            if process is None or role in self.cleaned:
                continue
            self.cleaned.add(role)
            try:
                stop_group(process, timeout, log)
            except OwnershipLost as error:
                log(f"{role}: {error}")
                lost.append(role)
        for forwarder in self.forwarders:
            forwarder.close()
        for path in self.sockets:
            with suppress(OSError):
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    os.unlink(path)
        for name in ("session.json",):
            with suppress(OSError):
                (self.directory / name).unlink()
        with suppress(OSError):
            self.deployment.xauthority.unlink()
        if lost:
            raise OwnershipLost("; ".join(lost))


def serve(database, deployment, *, stop=None, binaries=Binaries(), environment=None, x_directory="/tmp",
          exit_check=evaluate, on_session=None):
    stop = stop or threading.Event()
    with _admitted_legacy_writer(database, deployment.paths, deployment.business_id, isolated=True) as (config, directory):
        session = Session(database, deployment, config, directory, binaries=binaries, stop=stop,
                          environment=environment, x_directory=x_directory, exit_check=exit_check)
        if on_session is not None:
            on_session(session)
        try:
            session.preflight()
            session.start()
            return session.supervise()
        finally:
            session.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment", required=True, type=Path)
    args = parser.parse_args(argv)
    stop = threading.Event()
    holder = []
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGUSR1, lambda *_: holder and holder[0].recheck_requested.set())
    try:
        deployment = load(args.deployment)
        database = open_deployment_database(deployment)
        return serve(database, deployment, stop=stop, on_session=holder.append)
    except OwnershipLost:
        print("Isolated browser could not clean up all of its processes; inspect the unit's control group.", flush=True)
        return 1
    except (DomainError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # Static domain messages are safe to show; other errors print only their type (never an exception
        # repr, which could carry a proxy URL or credential).
        reason = error.message if isinstance(error, DomainError) else type(error).__name__
        print(f"Isolated project browser unavailable: {reason}", flush=True)
        return exit_code(error)


if __name__ == "__main__":
    raise SystemExit(main())
