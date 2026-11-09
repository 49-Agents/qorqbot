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
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from urllib.request import ProxyHandler, build_opener

from qorqut.bootstrap.client import path_has_symlink, write_json_private
from .control import owns_listener, process_identity

EXIT_STOPPED, EXIT_FAILED, EXIT_BUSY, EXIT_CONFIG = 0, 1, 75, 78
START_TIMEOUT = 40


def load_meta(state: Path) -> dict:
    """The manager's saved ownership record for this directory, or ``ValueError`` when it is not exactly what it wrote."""
    if not state.is_absolute() or path_has_symlink(state) or not state.is_dir():
        raise ValueError("state directory")
    info = state.lstat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("state directory is not private")
    meta = json.loads((state / "meta.json").read_bytes()[:16384])
    managed = meta["managed"]
    if (meta["schema_version"] != 1 or meta["mode"] != "managed" or meta["agent_id"] != state.name
            or str(uuid.UUID(meta["agent_id"])) != state.name or not isinstance(managed["port"], int)
            or not 1024 <= managed["port"] <= 65535 or managed["profile"] != str(state / "profile")
            or not os.path.isabs(managed["chrome"])
            or meta["target"]["content"]["cdp_endpoint"] != f"http://127.0.0.1:{managed['port']}"):
        raise ValueError("ownership record")
    return meta


def chrome_command(meta: dict) -> list[str]:
    """The only Chrome argv this supervisor ever runs: normal sandbox, loopback CDP, a profile private to the agent."""
    managed = meta["managed"]
    return [managed["chrome"], "--headless=new", *profile_flags(managed),
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={managed['port']}",
            "--disable-background-networking", "about:blank"]


def profile_flags(managed: dict) -> list[str]:
    """The headless browser and the owner's sign-in window must read the same saved cookies, so they share these exactly."""
    keychain = ["--use-mock-keychain"] if sys.platform == "darwin" else []
    return ["--no-first-run", "--no-default-browser-check", "--password-store=basic", *keychain,
            f"--user-data-dir={managed['profile']}", "--disable-sync", "--disable-extensions"]


def visible_chrome_command(meta: dict, url: str, debug_port: int) -> list[str]:
    """A normal window on the agent's profile, with a loopback debugging port used only to notice the window closing."""
    managed = meta["managed"]
    return [managed["chrome"], *profile_flags(managed), "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={debug_port}", url]


def chrome_environment(state: Path) -> dict:
    """Chrome's environment for this agent's private state directory."""
    from qorqut import native_paths
    folder, native = native_paths.folder_root(), native_paths.root()
    search_path = os.environ.get('PATH', os.defpath) if folder is not None else "/usr/bin:/bin"
    env = native_paths.environment({"PATH": search_path, "HOME": str(state), "LANG": "C.UTF-8"})
    for key, name in (("TMPDIR", "tmp"), ("XDG_CACHE_HOME", "cache"), ("XDG_CONFIG_HOME", "config"), ("XDG_RUNTIME_DIR", "run")):
        installation = folder or (native.parent.parent if native is not None else None)
        directory = installation / ".run/c" if key == "TMPDIR" and installation is not None else state / name
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


def stop_child(child: subprocess.Popen) -> None:
    """Close the one Chrome this process launched: its leader first (profile flush), then only its own process group."""
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


def serve(state: Path, stop: threading.Event) -> int:
    try:
        meta = load_meta(state)
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
        child = subprocess.Popen(chrome_command(meta), cwd=state, env=chrome_environment(state), stdin=subprocess.DEVNULL,
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
                stop_child(child)
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
    arguments = parser.parse_args(argv)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    return serve(arguments.state, stop)


if __name__ == "__main__":
    raise SystemExit(main())
