"""One persistent headless Chrome for one agent's managed browser, supervised outside the application.

``python -m qorqut.browser.agent_supervisor --state <runtime>/agent-browsers/<agent>`` is started detached by
``agent_browsers.AgentBrowserManager`` with this fixed argv; everything else comes from the private ``meta.json`` the manager
wrote first. The supervisor holds the agent's stable ``supervisor.lock`` for its whole life (a second copy exits 75), launches
only the validated Chrome argv below with its normal sandbox on a loopback CDP port, records boot-bound start identity for
itself and Chrome in ``ready.json``, and publishes ``ready`` only after the CDP endpoint answers *and* that exact Chrome owns the
listener. A port someone else holds is never adopted. On SIGTERM it closes only the Chrome it launched; it never kills by port or
name and never removes Chrome's profile locks. When Chrome exits it records ``failed`` and exits: a later manager repair starts
it again. Exit codes: 0 stopped on request, 1 failed, 75 already supervised, 78 invalid configuration."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.request import ProxyHandler, build_opener

from qorqut.bootstrap.client import path_has_symlink, write_json_private
from .control import owns_listener, process_identity

EXIT_STOPPED, EXIT_FAILED, EXIT_BUSY, EXIT_CONFIG = 0, 1, 75, 78
START_TIMEOUT = 40
SOCKET_PATH_LIMIT = 104
VERSION_TIMEOUT = 10             # Seconds Chrome gets to print its version before the browser starts without a user agent of its own.
# What a visible Chrome puts in its user agent for the computer it runs on. It is the same for every processor.
USER_AGENT_PLATFORMS = {"linux": "X11; Linux x86_64", "darwin": "Macintosh; Intel Mac OS X 10_15_7"}


def load_meta(state: Path, recorded: Path | None = None) -> dict:
    """The manager's saved ownership record for this directory, or ``ValueError`` when it is not exactly what it wrote.

    ``recorded`` is the path the manager knows this directory by when it runs behind a private bind mount and that path differs
    from the supervisor's own. The record is checked against it, and the profile is then taken from ``state`` itself."""
    if not state.is_absolute() or path_has_symlink(state) or not state.is_dir():
        raise ValueError("state directory")
    if recorded is not None and (not recorded.is_absolute() or recorded.name != state.name):
        raise ValueError("recorded state directory")
    info = state.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("state directory is not private")
    meta = json.loads((state / "meta.json").read_bytes()[:16384])
    managed = meta["managed"]
    if (meta["schema_version"] != 1 or meta["mode"] != "managed" or meta["agent_id"] != state.name
            or str(uuid.UUID(meta["agent_id"])) != state.name or not isinstance(managed["port"], int)
            or not 1024 <= managed["port"] <= 65535 or managed["profile"] != str((recorded or state) / "profile")
            or not os.path.isabs(managed["chrome"])
            or meta["target"]["content"]["cdp_endpoint"] != f"http://127.0.0.1:{managed['port']}"):
        raise ValueError("ownership record")
    managed["profile"] = str(state / "profile")
    return meta


def chrome_command(meta: dict, user_agent: str | None = None) -> list[str]:
    """The only Chrome argv this supervisor ever runs: normal sandbox, loopback CDP, a profile private to the agent.

    No WebGL or other 3D: the host has no GPU, so 3D content (embedded maps, animated widgets) is drawn in software,
    and a headless browser always has one tab in front (the newest, or the one the owner watches) that it treats as
    visible, so a forgotten tab keeps redrawing and holds a CPU core. Tabs behind it are throttled like any background
    tab; ``idle_tabs`` unloads what nobody uses.
    Overlay scrollbars: the page's own scrollbar is part of the picture the owner watches in the live view, and the
    default one is a wide bar with arrows; the overlay kind is a thin thumb that fades out while the page is not scrolling.

    ``user_agent`` is ``normal_user_agent``'s answer for this Chrome. Without one the browser names itself
    ``HeadlessChrome``, as it does when left alone."""
    managed = meta["managed"]
    return [managed["chrome"], "--headless=new", *profile_flags(managed),
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={managed['port']}",
            "--disable-background-networking", "--disable-3d-apis", "--enable-features=OverlayScrollbar",
            *([f"--user-agent={user_agent}"] if user_agent else []), "about:blank"]


def normal_user_agent(chrome: str, environment: dict) -> str | None:
    """The user agent a visible window of this Chrome sends, or ``None`` when that cannot be told.

    A headless Chrome writes ``HeadlessChrome`` where a visible one writes ``Chrome``, and websites refuse a browser for
    that word alone. Chrome's user agent is otherwise only the computer's platform and Chrome's major version, so the
    browser is told to send exactly what a visible Chrome on the same profile sends. The version is asked from
    the program that is about to start (``--version`` prints it and opens no profile), so it is right after every Chrome
    update. Chrome answers less about its version to a page that asks for the details while this is set; see
    ``docs/BROWSER-MANAGEMENT.md``."""
    platform = USER_AGENT_PLATFORMS.get(sys.platform)
    if platform is None:
        return None
    try:
        child = subprocess.Popen([chrome, "--version"], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
    except OSError:
        return None
    try:
        answer = child.communicate(timeout=VERSION_TIMEOUT)[0]
    except subprocess.TimeoutExpired:
        answer = ""
        try:
            os.killpg(child.pid, signal.SIGKILL)       # the group it made for itself, so nothing it started is left behind
        except (ProcessLookupError, PermissionError):
            pass
        child.wait()
        child.stdout.close()
    version = re.search(r"(?<![\d.])(\d{2,4})\.\d+\.\d+\.\d+(?![\d.])", answer)
    return version and f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version[1]}.0.0.0 Safari/537.36"


def profile_flags(managed: dict) -> list[str]:
    """The flags that decide which profile and cookie store the headless browser reads."""
    keychain = ["--use-mock-keychain"] if sys.platform == "darwin" else []
    return ["--no-first-run", "--no-default-browser-check", "--password-store=basic", *keychain,
            f"--user-data-dir={managed['profile']}", "--disable-sync", "--disable-extensions"]


def socket_fits(temporary: Path) -> bool:
    """Chrome keeps its singleton socket in a fresh folder under TMPDIR and aborts when that path exceeds a Unix socket address
    (108 bytes on Linux, 104 on macOS)."""
    return len(os.fsencode(temporary / "org.chromium.Chromium.XXXXXX/SingletonSocket")) < SOCKET_PATH_LIMIT


def short_temporary(state: Path) -> Path:
    """A private temporary directory for one agent's Chrome when its state directory is too deep for the singleton socket.

    The user's runtime directory is private by construction. The shared temporary directory is the fallback, and there the
    directory must be ours alone: anything else is refused, never adopted."""
    runtime = Path(f"/run/user/{os.getuid()}")
    try:
        private = not runtime.is_symlink() and runtime.is_dir() and runtime.stat().st_uid == os.geteuid()
    except OSError:
        private = False
    directory = ((runtime if private else Path(tempfile.gettempdir()))
                 / ("qorqut-browser-" + hashlib.sha256(os.fsencode(state)).hexdigest()[:12]))
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or not socket_fits(directory):
        raise RuntimeError("temporary_directory_unsafe")
    return directory


def chrome_environment(state: Path) -> dict:
    """Chrome's environment for this agent's private state directory."""
    from qorqut import native_paths
    folder, native = native_paths.folder_root(), native_paths.root()
    search_path = os.environ.get('PATH', os.defpath) if folder is not None else "/usr/bin:/bin"
    env = native_paths.environment({"PATH": search_path, "HOME": str(state), "LANG": "C.UTF-8"})
    for key, name in (("TMPDIR", "tmp"), ("XDG_CACHE_HOME", "cache"), ("XDG_CONFIG_HOME", "config"), ("XDG_RUNTIME_DIR", "run")):
        installation = folder or (native.parent.parent if native is not None else None)
        directory = installation / ".run/c" if key == "TMPDIR" and installation is not None else state / name
        if key == "TMPDIR" and installation is None and not socket_fits(directory):
            directory = short_temporary(state)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[key] = str(directory)
    return env


def port_free(port: int) -> bool:
    """Nobody is listening on the loopback port. Connections left in TIME_WAIT by an earlier browser do not count (Chrome binds
    with SO_REUSEADDR too), so a restart is not mistaken for a foreign listener."""
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _responds(port: int) -> bool:
    try:
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            return bool(json.load(response).get("webSocketDebuggerUrl"))
    except (OSError, ValueError, AttributeError):
        return False


def request_close(port: int, timeout: float = 2, pid: int | None = None) -> bool:
    """Ask the Chrome on this loopback DevTools port to close itself; True when the request was delivered.

    With ``pid``, the process that launched it, only a listener that process owns is asked: after a crash another program
    may hold the port, and ``Browser.close`` would end that one."""
    try:
        if pid is not None and not owns_listener(pid, port, "127.0.0.1"):
            return False
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            identity = json.load(response)["webSocketDebuggerUrl"].rsplit("/", 1)[1]
        from websockets.sync.client import connect
        with connect(f"ws://127.0.0.1:{port}/devtools/browser/{identity}", proxy=None, open_timeout=timeout,
                     close_timeout=1) as devtools:
            devtools.send(json.dumps({"id": 1, "method": "Browser.close"}))
            devtools.recv(timeout=timeout)
        return True
    except Exception:      # any failure leaves the ordinary termination below
        return False


def stop_child(child: subprocess.Popen, port: int | None = None) -> None:
    """Close the one Chrome this process launched, then only its own process group.

    With its DevTools ``port`` Chrome is first asked to close itself. A terminated headless Chrome exits at once and drops
    what it has not written yet: a cookie set in its last half minute, so a sign-in made just before a stop or restart was
    lost. Closing writes the profile first (measured with Chrome 153)."""
    if child.poll() is None and port is not None and request_close(port, pid=child.pid):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)       # the group Chrome created for itself at launch; never found by port or name
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def attend(state: Path, port: int, stop: threading.Event) -> None:
    """What this browser gets for as long as it runs, from here because the supervisor lives exactly as long as its Chrome
    and outlives the application: cookie banners refused and idle tabs unloaded. Neither can stop the browser.

    Both import what they talk to Chrome with only when they first need it, inside loops that survive every error. So a
    supervisor whose Python lacks one of those modules would run a browser with neither, and say nothing. They are
    imported here first, and the supervisor's log gets one line either way."""
    try:
        import websockets.sync.client          # beyond the standard library, the one thing the two need
        from . import cookie_banners, idle_tabs
    except ImportError as error:
        print("Browser supervisor: cookie banners are not refused and idle tabs are not unloaded in this browser, because "
              f"this Python ({sys.executable}) cannot import what they need: {error}", file=sys.stderr)
        return
    for watch, arguments in ((cookie_banners.watch, ()), (idle_tabs.watch, (state / idle_tabs.WATCHED,))):
        threading.Thread(target=watch, args=(port, stop, *arguments), daemon=True).start()
    print("Browser supervisor: refusing cookie banners and unloading idle tabs in this browser.", file=sys.stderr)


def serve(state: Path, stop: threading.Event, recorded: Path | None = None) -> int:
    try:
        meta = load_meta(state, recorded)
    except (OSError, ValueError, KeyError, TypeError):
        print("Browser supervisor: invalid ownership record; nothing was started.", file=sys.stderr)
        return EXIT_CONFIG
    managed = meta["managed"]
    descriptor = os.open(state / "supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return EXIT_BUSY
    record = {"schema_version": 1, "agent_id": meta["agent_id"], "port": managed["port"], "state": "starting",
              "supervisor_pid": os.getpid(), "supervisor_identity": process_identity(os.getpid())}
    ready, child, code = state / "ready.json", None, EXIT_STOPPED

    def publish(**fields):
        record.update(fields)
        write_json_private(ready, record)

    publish()
    try:
        profile = Path(managed["profile"])
        if path_has_symlink(profile):
            raise RuntimeError("profile_path_unsafe")
        profile.mkdir(mode=0o700, exist_ok=True)
        info = profile.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError("profile_not_owned")
        if not port_free(managed["port"]):
            raise RuntimeError("port_in_use")
        environment = chrome_environment(state)
        user_agent = normal_user_agent(managed["chrome"], environment)
        if user_agent is None:
            print("Browser supervisor: this Chrome did not say its version, so the browser names itself HeadlessChrome and "
                  "some websites will refuse it.", file=sys.stderr)
        child = subprocess.Popen(chrome_command(meta, user_agent), cwd=state, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        publish(chrome_pid=child.pid, chrome_identity=process_identity(child.pid))
        deadline = time.monotonic() + START_TIMEOUT
        while True:
            if stop.is_set():
                return EXIT_STOPPED
            if child.poll() is not None:
                raise RuntimeError("chrome_exited_during_start")
            if _responds(managed["port"]) and owns_listener(child.pid, managed["port"], "127.0.0.1"):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("start_timeout")
            time.sleep(0.1)
        publish(state="ready", instance=str(uuid.uuid4()))
        attend(state, managed["port"], stop)
        while not stop.wait(0.5):
            if child.poll() is not None:
                raise RuntimeError("chrome_exited")
        return EXIT_STOPPED
    except (RuntimeError, OSError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        code = EXIT_FAILED
        try:
            publish(state="failed", detail=detail)
        except OSError:
            pass
        return code
    finally:
        if child is not None:
            try:
                stop_child(child, managed["port"])
            except (OSError, subprocess.SubprocessError):
                pass
        if code == EXIT_STOPPED:
            try:
                publish(state="stopped")
            except OSError:
                pass
        os.close(descriptor)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--recorded-state", type=Path)
    arguments = parser.parse_args(argv)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    return serve(arguments.state, stop, arguments.recorded_state)


if __name__ == "__main__":
    raise SystemExit(main())
