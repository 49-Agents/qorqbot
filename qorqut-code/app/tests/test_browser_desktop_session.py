"""Dedicated browser desktop sessions: bounded config, collisions and lifecycle.

Fake executables prove ordering and cleanup. The real two-session smoke test runs
only with QORQUT_REAL_DESKTOP_SMOKE=1 and uses blank temporary profiles, unused
test displays/ports and loopback listeners; it never touches a live profile.
"""
import copy
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools/browser"
HELPER = TOOLS / "desktop_session.py"
spec = importlib.util.spec_from_file_location("desktop_session", HELPER)
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)
EXAMPLES = TOOLS / "desktops"


def example(name):
    return json.loads((EXAMPLES / f"{name}.json.example").read_text())


def basic_config():
    """A synthetic managed session with no keyring (the basic password store); the shipped examples have none of this mode."""
    return {"schema_version": 1, "name": "basic", "managed": True, "display": 21, "geometry": {"width": 1440, "height": 900},
            "vnc": {"interface": "10.0.0.10", "port": 5921, "password_file": "/absolute/path/to/qorqut-basic-browser/.vnc/passwd"},
            "xauthority": "/absolute/path/to/qorqut-basic-browser/.Xauthority",
            "environment": {"XDG_CONFIG_HOME": "/absolute/path/to/qorqut-basic-browser/config",
                            "XDG_CACHE_HOME": "/absolute/path/to/qorqut-basic-browser/cache",
                            "XDG_DATA_HOME": "/absolute/path/to/qorqut-basic-browser/data"},
            "keyring": "none",
            "chrome": {"user_data_dir": "/absolute/path/to/qorqut-basic-browser/profile", "cdp_port": 9230,
                       "flags": ["--class=QorqutBasic", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                                 "--disable-dev-shm-usage", "--disable-session-crashed-bubble"],
                       "start_url": "https://example.com/"}}


def test_examples_preserve_existing_chrome_arguments_and_split_displays():
    sessions = [ds.parse(example("ordubashy")), ds.parse(basic_config()), ds.parse(example("outlook"))]
    assert ds.collisions(sessions) == []
    assert [(s.display, s.vnc_port, s.cdp_port) for s in sessions] == [(20, 5920, 9225), (21, 5921, 9230), (22, 5922, 9226)]
    basic, outlook = sessions[1], sessions[2]
    # Exactly the argv of a basic-store session and of the Outlook example.
    assert ds.plan(basic)["chrome"][1:] == [
        "--user-data-dir=/absolute/path/to/qorqut-basic-browser/profile",
        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9230", "--class=QorqutBasic",
        "--no-first-run", "--no-default-browser-check", "--password-store=basic", "--disable-dev-shm-usage",
        "--disable-session-crashed-bubble", "--window-size=1440,900", "--start-maximized", "https://example.com/"]
    assert ds.plan(outlook)["chrome"][1:] == [
        "--user-data-dir=/var/lib/qorqut-outlook-browser/chrome-profile",
        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9226", "--no-first-run",
        "--no-default-browser-check", "--disable-dev-shm-usage", "--disable-session-crashed-bubble",
        "--disable-extensions", "--disable-gpu", "--window-size=1440,900", "--start-maximized",
        "https://outlook.cloud.microsoft/mail/"]
    assert basic.keyring == "none" and outlook.keyring == "gnome-keyring-credential"
    assert basic.environment["XDG_CONFIG_HOME"] == "/absolute/path/to/qorqut-basic-browser/config"
    assert outlook.environment["XDG_RUNTIME_DIR"] == "/run/qorqut-outlook-browser"
    xvnc = ds.plan(basic)["xvnc"]
    assert xvnc[1] == ":21" and xvnc[xvnc.index("-rfbport") + 1] == "5921"
    assert xvnc[xvnc.index("-auth") + 1] == basic.xauthority and "VncAuth" in xvnc
    assert all("proxy" not in arg.lower() for s in sessions for part in ds.plan(s).values() for arg in part)


def test_unit_templates_own_their_display_and_keep_credentials():
    outlook = (EXAMPLES / "qorqut-outlook-browser.service.example").read_text()
    active = lambda text: "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert "remote-browser.service" not in active(outlook)
    assert "LoadCredential=keyring-password:/etc/qorqut-email/outlook-browser-keyring-password" in outlook
    assert "After=network-online.target" in outlook
    assert "KillMode=mixed" in outlook and "RestartPreventExitStatus=78" in outlook and "dbus-run-session" not in active(outlook)
    assert "proxy" not in outlook.lower() and "DISPLAY=:20" not in outlook
    assert "outlook.json" in outlook


@pytest.mark.parametrize("mutate", [
    lambda c: c["chrome"]["flags"].append("--proxy-server=http://127.0.0.1:8080"),
    lambda c: c["chrome"]["flags"].append("--no-sandbox"),
    lambda c: c["chrome"]["flags"].append("--remote-debugging-address=0.0.0.0"),
    lambda c: c["chrome"]["flags"].append("--user-data-dir=/tmp/other"),
    lambda c: c["chrome"]["flags"].append("--no-first-run"),
    lambda c: c["chrome"]["flags"].remove("--password-store=basic"),
    lambda c: c["environment"].update(HTTPS_PROXY="/x"),
    lambda c: c["environment"].update(XDG_CONFIG_HOME="relative"),
    lambda c: c["vnc"].update(interface="0.0.0.0"),
    lambda c: c["vnc"].update(interface="8.8.8.8"),
    lambda c: c["vnc"].update(port=9230),
    lambda c: c.update(display=0),
    lambda c: c.update(command="/bin/sh"),
    lambda c: c.update(keyring="existing-unit"),
    lambda c: c.update(managed=False),
    lambda c: c["chrome"].update(start_url="javascript:alert(1)"),
    lambda c: c["chrome"].update(executable="/bin/sh"),
])
def test_unsafe_or_ambiguous_configs_are_refused(mutate):
    config = basic_config()
    mutate(config)
    with pytest.raises(ds.ConfigError):
        ds.parse(config)


def test_keyring_credential_mode_never_switches_to_basic_store():
    config = example("outlook")
    config["chrome"]["flags"].append("--password-store=basic")
    with pytest.raises(ds.ConfigError):
        ds.parse(config)
    config = example("outlook")
    del config["environment"]["XDG_RUNTIME_DIR"]
    with pytest.raises(ds.ConfigError):
        ds.parse(config)


def test_collisions_refuse_shared_display_ports_profiles_and_credentials():
    base = ds.parse(basic_config())
    for change in ({"display": 22}, {"vnc_port": 9226}, {"cdp_port": 5922},
                   {"user_data_dir": "/var/lib/qorqut-outlook-browser/chrome-profile/"},
                   {"xauthority": "/var/lib/qorqut-outlook-browser/.Xauthority"},
                   {"vnc_password_file": "/var/lib/qorqut-outlook-browser/.vnc/passwd"}, {"name": "outlook"}):
        from dataclasses import replace
        assert ds.collisions([replace(base, **change), ds.parse(example("outlook"))]), change


# ---------------------------------------------------------------- fake processes

def fake_binaries(tmp_path):
    bin_dir, log_dir = tmp_path / "bin", tmp_path / "log"
    bin_dir.mkdir()
    log_dir.mkdir()
    scripts = {
        "xvnc": 'echo "$@" > "$LOG/xvnc$1"; [ -n "$FAKE_XVNC_FAIL" ] && exit 3; touch "$LOG/ready$1"; '
                'trap \'rm -f "$LOG/ready$1"; exit 0\' TERM; while :; do sleep 0.05; done',
        "xdpyinfo": 'test -f "$LOG/ready$DISPLAY"',
        "xauth": 'echo "$@" > "$LOG/xauth-argv$DISPLAY"; cat > "$LOG/xauth-stdin$DISPLAY"',
        "xset": 'echo "$@" >> "$LOG/xset$DISPLAY"',
        "window_manager": 'exec sleep 1000',
        "chrome": 'echo "$@" > "$LOG/chrome$DISPLAY"; echo "$DBUS_SESSION_BUS_ADDRESS" > "$LOG/chrome-bus$DISPLAY"; '
                  'trap \'echo stopped > "$LOG/chrome-stopped$DISPLAY"; exit 0\' TERM; while :; do sleep 0.05; done',
        "dbus_daemon": 'echo "unix:path=$LOG/bus-$$"; exec sleep 1000',
        "dbus_update": 'echo "$@" > "$LOG/dbus-update"',
        "keyring_daemon": 'echo "$@" > "$LOG/keyring-argv"; cat > "$LOG/keyring-stdin"',
    }
    paths = {}
    for role, body in scripts.items():
        path = bin_dir / role
        path.write_text(f"#!/bin/bash\nLOG={log_dir}\n{body}\n")
        path.chmod(0o755)
        paths[role] = str(path)
    return ds.Binaries(**paths), log_dir


def fixture_session(tmp_path, name, display, keyring="none"):
    home = tmp_path / name
    (home / "profile").mkdir(parents=True)
    (home / "runtime").mkdir()
    password = home / "passwd"
    password.write_text("not-a-real-password")
    password.chmod(0o600)
    config = basic_config() if keyring == "none" else example("outlook")
    config.update(name=name, display=display, xauthority=str(home / ".Xauthority"))
    config["vnc"].update(interface="127.0.0.1", port=free_port(), password_file=str(password))
    config["chrome"].update(user_data_dir=str(home / "profile"), cdp_port=free_port(), start_url="about:blank")
    config["environment"] = {"XDG_CONFIG_HOME": str(home / "config"), **(
        {"XDG_RUNTIME_DIR": str(home / "runtime")} if keyring != "none" else {})}
    config["timeouts"] = {"display": 5, "chrome_stop": 3}
    return ds.parse(config)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def alive(process):
    return process.poll() is None


def written(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.02)
    raise AssertionError(f"{path.name} was not written")


def test_fake_sessions_start_stop_and_restart_independently(tmp_path):
    binaries, log = fake_binaries(tmp_path)
    one, two = fixture_session(tmp_path, "one", 931), fixture_session(tmp_path, "two", 932)
    first, second = ds.Supervisor(one, binaries, {}), ds.Supervisor(two, binaries, {})
    first.start(), second.start()
    try:
        assert all(alive(p) for p in [*first.children.values(), *second.children.values()])
        assert written(log / "chrome:931").split()[:4] == [
            f"--user-data-dir={one.user_data_dir}", "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={one.cdp_port}", "--class=QorqutBasic"]
        assert first.environment["DISPLAY"] == ":931" and second.environment["DISPLAY"] == ":932"
        assert first.environment["XAUTHORITY"] != second.environment["XAUTHORITY"]
        assert written(log / "chrome-bus:931") != written(log / "chrome-bus:932"), "private bus per session"
        # The X cookie travels through stdin, never argv.
        cookie = (log / "xauth-stdin:931").read_text()
        assert cookie.startswith("add :931 . ") and cookie.split()[-1] not in (log / "xauth-argv:931").read_text()
        assert oct(os.stat(one.xauthority).st_mode & 0o777) == "0o600"
        assert not (log / "keyring-argv").exists()
        first.stop()
        assert not any(alive(p) for p in first.children.values())
        assert written(log / "chrome-stopped:931") and not (log / "ready:931").exists()
        assert all(alive(p) for p in second.children.values()), "stopping one session leaves the other running"
        restarted = ds.Supervisor(one, binaries, {})
        restarted.start()
        assert all(alive(p) for p in restarted.children.values())
        restarted.stop()
    finally:
        first.stop(), second.stop()
    assert not any(alive(p) for p in second.children.values())


def test_child_failure_stops_only_that_session(tmp_path):
    binaries, log = fake_binaries(tmp_path)
    session = fixture_session(tmp_path, "one", 941)
    supervisor = ds.Supervisor(session, binaries, {})
    supervisor.start()
    os.killpg(supervisor.children["chrome"].pid, signal.SIGKILL)
    assert supervisor.supervise(poll=0.05) == ds.EX_CHILD_FAILED
    assert not any(alive(p) for p in supervisor.children.values())
    supervisor = ds.Supervisor(session, binaries, {})
    supervisor.start()
    os.killpg(supervisor.children["window_manager"].pid, signal.SIGKILL)
    import threading
    result = []
    watcher = threading.Thread(target=lambda: result.append(supervisor.supervise(poll=0.05)))
    watcher.start()
    wait_for(lambda: supervisor.window_manager_restarts == 1 and alive(supervisor.children["window_manager"]), 5)
    assert alive(supervisor.children["chrome"]), "a window manager restart keeps Chrome running"
    supervisor.request_stop()
    watcher.join(30)
    assert result == [ds.EX_OK]
    assert not any(alive(p) for p in supervisor.children.values())


def descendant_leader(tmp_path, name):
    """A group leader that spawns a long-lived descendant and immediately exits."""
    pidfile = tmp_path / f"{name}.pid"
    script = ("import subprocess,sys;"
              f"open({str(pidfile)!r},'w').write(str(subprocess.Popen(['sleep','120']).pid))")
    return subprocess.Popen([sys.executable, "-c", script], start_new_session=True), pidfile


def test_stop_cleans_descendants_after_the_group_leader_exited(tmp_path):
    binaries, log = fake_binaries(tmp_path)
    other = ds.Supervisor(fixture_session(tmp_path, "two", 952), binaries, {})
    other.start()
    supervisor = ds.Supervisor(fixture_session(tmp_path, "one", 951), binaries, {})
    unrelated = subprocess.Popen(["sleep", "121"], start_new_session=True)
    try:
        leader, pidfile = descendant_leader(tmp_path, "leader")
        supervisor.children["chrome"] = leader
        descendant = int(written(pidfile))
        # The production path: the exit is only peeked at, so the leader stays unreaped.
        wait_for(lambda: ds.leader_exited(leader), 5)
        assert leader.returncode is None and Path(f"/proc/{leader.pid}").exists()
        assert Path(f"/proc/{descendant}").exists()
        supervisor.stop()
        wait_for(lambda: not ds.group_members(leader.pid), 5)
        assert not ds.group_members(leader.pid) and leader.returncode is not None, "group cleaned, then the leader reaped"
        supervisor.stop()  # repeated stop is a no-op
        assert unrelated.poll() is None, "processes outside the owned group are untouched"
        assert alive(other.children["chrome"]) and alive(other.children["xvnc"]), "another session keeps running"
    finally:
        unrelated.kill()
        unrelated.wait()
        other.stop()


def test_stale_or_reaped_identity_is_refused_without_signalling(tmp_path):
    binaries, log = fake_binaries(tmp_path)
    supervisor = ds.Supervisor(fixture_session(tmp_path, "one", 953), binaries, {})
    bystander = subprocess.Popen(["sleep", "122"], start_new_session=True)
    try:
        class Stale:  # a handle whose pid now belongs to an unrelated new group
            pid, returncode = bystander.pid, 0
        supervisor.children["chrome"] = Stale()
        with pytest.raises(ds.OwnershipLost):
            supervisor.stop()
        assert bystander.poll() is None and Path(f"/proc/{bystander.pid}").exists()
        # A leader something else already reaped is refused too, not recovered by group ID.
        leader, pidfile = descendant_leader(tmp_path, "reaped")
        descendant = int(written(pidfile))
        leader.wait()
        supervisor.children["chrome"] = leader
        supervisor.cleaned.discard("chrome")
        with pytest.raises(ds.OwnershipLost):
            supervisor.stop()
        assert Path(f"/proc/{descendant}").exists(), "no signal from a stale numeric ID"
        os.kill(descendant, signal.SIGKILL)
    finally:
        bystander.kill()
        bystander.wait()


def test_display_failure_cleans_up_before_chrome_starts(tmp_path):
    binaries, log = fake_binaries(tmp_path)
    session = fixture_session(tmp_path, "one", 951)
    with pytest.raises(RuntimeError):
        ds.run(session, binaries=binaries, environment={"FAKE_XVNC_FAIL": "1"}, x_directory=str(tmp_path))
    assert not (log / "chrome:951").exists()


def test_keyring_credential_unlocks_on_the_private_bus(tmp_path):
    binaries, log = fake_binaries(tmp_path)
    session = fixture_session(tmp_path, "mail", 961, keyring="gnome-keyring-credential")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "keyring-password").write_text("fixture-secret")
    missing = ds.Supervisor(session, binaries, {})
    with pytest.raises(ds.ConfigError):
        missing.start()
    missing.stop()
    supervisor = ds.Supervisor(session, binaries, {"CREDENTIALS_DIRECTORY": str(credentials)})
    supervisor.start()
    try:
        assert (log / "keyring-stdin").read_text() == "fixture-secret"
        assert "fixture-secret" not in (log / "keyring-argv").read_text()
        assert (log / "keyring-argv").read_text().split() == [
            "--daemonize", "--components=secrets", f"--control-directory={session.environment['XDG_RUNTIME_DIR']}/keyring", "--unlock"]
        assert (log / "dbus-update").read_text().split() == list(ds.ACTIVATION_VARIABLES)
        assert "--password-store" not in written(log / "chrome:961")
    finally:
        supervisor.stop()


def test_preflight_refuses_live_display_listener_profile_and_proxy(tmp_path):
    session = fixture_session(tmp_path, "one", 971)
    ds.preflight(session, {}, str(tmp_path))
    with pytest.raises(ds.ConfigError):
        ds.preflight(session, {"HTTPS_PROXY": "http://proxy"}, str(tmp_path))
    (tmp_path / ".X971-lock").write_text(f"{os.getpid()}\n")
    with pytest.raises(ds.Busy):
        ds.preflight(session, {}, str(tmp_path))
    (tmp_path / ".X971-lock").write_text("999999999\n")
    ds.preflight(session, {}, str(tmp_path))  # a stale lock is left for Xtigervnc
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", session.cdp_port))
        busy.listen()
        with pytest.raises(ds.Busy):
            ds.preflight(session, {}, str(tmp_path))
    lock = Path(session.user_data_dir) / "SingletonLock"
    lock.symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    with pytest.raises(ds.Busy):
        ds.preflight(session, {}, str(tmp_path))
    assert lock.is_symlink(), "a profile lock is never removed"
    lock.unlink()
    os.chmod(session.vnc_password_file, 0o644)
    with pytest.raises(ds.ConfigError):
        ds.preflight(session, {}, str(tmp_path))
    os.chmod(session.vnc_password_file, 0o600)
    shutil.rmtree(session.user_data_dir)
    with pytest.raises(ds.ConfigError):
        ds.preflight(session, {}, str(tmp_path))


def test_cli_check_and_config_file_permissions(tmp_path):
    configs = []
    for name in ("ordubashy", "basic", "outlook"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(basic_config()) if name == "basic" else (EXAMPLES / f"{name}.json.example").read_text())
        path.chmod(0o644)
        configs.append(str(path))
    run = lambda *args: subprocess.run([sys.executable, "-I", str(HELPER), *args], capture_output=True, text=True)
    result = run("check", *configs)
    assert result.returncode == 0, result.stderr
    assert [row["display"] for row in json.loads(result.stdout)] == [":20", ":21", ":22"]
    duplicate = tmp_path / "duplicate.json"
    value = example("outlook")
    value.update(name="duplicate")
    duplicate.write_text(json.dumps(value))
    duplicate.chmod(0o644)
    assert run("check", configs[2], str(duplicate)).returncode == ds.EX_CONFIG
    os.chmod(configs[1], 0o666)
    assert run("check", configs[1]).returncode == ds.EX_CONFIG
    link = tmp_path / "link.json"
    link.symlink_to(configs[2])
    assert run("check", str(link)).returncode == ds.EX_CONFIG
    assert run("run", configs[0]).returncode == ds.EX_CONFIG, "reservations are never started"


# ---------------------------------------------------------------- real smoke

REAL = os.environ.get("QORQUT_REAL_DESKTOP_SMOKE") == "1"


def cdp(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
        return json.load(response)


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except OSError:
            pass
        time.sleep(0.25)
    raise AssertionError("condition was not met in time")


def unused_display(start):
    for display in range(start, start + 50):
        if not Path(f"/tmp/.X{display}-lock").exists() and not Path(f"/tmp/.X11-unix/X{display}").exists():
            return display
    raise AssertionError("no unused test display")


@pytest.mark.skipif(not REAL or not all(Path(p).exists() for p in (ds.Binaries.xvnc, ds.Binaries.chrome, "/usr/bin/vncpasswd", "/usr/bin/wmctrl")),
                    reason="set QORQUT_REAL_DESKTOP_SMOKE=1 on a host with TigerVNC and Chrome")
def test_real_two_session_smoke(tmp_path):
    processes, sessions, shots = {}, {}, os.environ.get("QORQUT_DESKTOP_SMOKE_EVIDENCE")
    first = unused_display(181)
    for name, display in (("smoke-a", first), ("smoke-b", unused_display(first + 1))):
        home = tmp_path / name
        for part in ("profile", "config", "cache", "data"):
            (home / part).mkdir(parents=True)
        password = home / "passwd"
        password.write_bytes(subprocess.run(["/usr/bin/vncpasswd", "-f"], input=b"smoke-only\n", capture_output=True, check=True).stdout)
        password.chmod(0o600)
        config = {"schema_version": 1, "name": name, "managed": True, "display": display,
                  "geometry": {"width": 1024, "height": 640},
                  "vnc": {"interface": "127.0.0.1", "port": free_port(), "password_file": str(password)},
                  "xauthority": str(home / ".Xauthority"),
                  "environment": {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                                  "XDG_CACHE_HOME": str(home / "cache"), "XDG_DATA_HOME": str(home / "data")},
                  "keyring": "none",
                  "chrome": {"user_data_dir": str(home / "profile"), "cdp_port": free_port(),
                             "flags": ["--no-first-run", "--no-default-browser-check", "--password-store=basic",
                                       "--disable-dev-shm-usage", "--disable-session-crashed-bubble"],
                             "start_url": "about:blank"}}
        path = home / "session.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        sessions[name] = (path, ds.parse(config))
    environment = {key: value for key, value in os.environ.items() if key.lower() not in ds.PROXY_VARIABLES}

    def start(name):
        path, _ = sessions[name]
        other = [str(p) for key, (p, _) in sessions.items() if key != name]
        processes[name] = subprocess.Popen([sys.executable, "-I", str(HELPER), "run", str(path), *sum((["--sibling", o] for o in other), [])],
                                           env=environment, stderr=subprocess.PIPE, text=True)

    def chrome_display(session):
        """CDP identity plus the CDP port of the Chrome owning the display's only window."""
        version = wait_for(lambda: cdp(session.cdp_port))
        x = {**environment, "DISPLAY": session.display_name, "XAUTHORITY": session.xauthority}
        windows = wait_for(lambda: subprocess.run(["/usr/bin/wmctrl", "-lpx"], env=x, capture_output=True, text=True).stdout.strip(), 20)
        assert len(windows.splitlines()) == 1 and "google-chrome" in windows.lower(), windows
        # Chrome rewrites its process title, so match the argument text itself.
        command = Path(f"/proc/{windows.split()[2]}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        ports = set(re.findall(r"--remote-debugging-port=\d+", command))
        assert session.user_data_dir in windows, "the window belongs to this session's profile"
        if shots:
            Path(f"{shots}/real-{session.name}-windows.txt").write_text(
                f"display {session.display_name} VNC 127.0.0.1:{session.vnc_port}\n{windows}\nwindow owner: {sorted(ports)}\n")
            subprocess.run(["/usr/bin/scrot", "-o", f"{shots}/real-{session.name}-display.png"], env=x, check=True)
        return version, ports

    try:
        for name in sessions:
            start(name)
        a, b = sessions["smoke-a"][1], sessions["smoke-b"][1]
        (version_a, displays_a), (version_b, displays_b) = chrome_display(a), chrome_display(b)
        assert displays_a == {f"--remote-debugging-port={a.cdp_port}"} and displays_b == {f"--remote-debugging-port={b.cdp_port}"}
        assert version_a["webSocketDebuggerUrl"] != version_b["webSocketDebuggerUrl"]
        for session in (a, b):
            listeners = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
            assert f"127.0.0.1:{session.vnc_port}" in listeners and f"127.0.0.1:{session.cdp_port}" in listeners
        processes["smoke-a"].send_signal(signal.SIGTERM)
        assert processes["smoke-a"].wait(40) == 0
        wait_for(lambda: not Path(f"/tmp/.X{a.display}-lock").exists(), 10)
        with pytest.raises(OSError):
            cdp(a.cdp_port)
        assert cdp(b.cdp_port)["Browser"] == version_b["Browser"], "the other session survives"
        assert processes["smoke-b"].poll() is None
        start("smoke-a")
        assert chrome_display(a)[1] == {f"--remote-debugging-port={a.cdp_port}"}
        assert cdp(b.cdp_port)["webSocketDebuggerUrl"] == version_b["webSocketDebuggerUrl"], "restart leaves the other browser untouched"
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for name, process in processes.items():
            try:
                process.wait(45)
            except subprocess.TimeoutExpired:
                process.kill()
    for _, session in sessions.values():
        assert not subprocess.run(["pgrep", "-f", f"--user-data-dir={session.user_data_dir}"], capture_output=True).stdout, "no orphaned Chrome"
        assert not Path(f"/tmp/.X{session.display}-lock").exists(), "no orphaned X server"
