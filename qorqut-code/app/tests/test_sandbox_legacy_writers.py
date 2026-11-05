"""Isolated, read-only legacy host-writer inventory checks."""

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.sandbox.legacy_writers import (CommandResult, HostTerminalWriters,
    LegacyBrowserWriters, LegacyHostWriters)
from qorqut_operations.storage.quiescence import QuiescenceProof
from bazarlyk.sandbox.state import SandboxStateStore
from bazarlyk.storage import Database


OWNER = "00000000-0000-4000-8000-000000000001"
BUSINESS = "00000000-0000-4000-8000-000000000002"


def database_with_business(root):
    database = Database(root / ".bazarlyk/state.sqlite3").initialize()
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": OWNER, "kind": "human", "display_name": "Owner"})
        uow.repository("businesses").insert({"id": BUSINESS, "slug": "first", "name": "First",
                                               "workspace": str(root / "first"), "hq_path": str(root / "first/hq")})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": OWNER, "is_owner": True})
    return database


def inactive_systemd(arguments, *, timeout):
    assert arguments[1:8] == ("show", "--property=LoadState", "--property=ActiveState", "--property=MainPID",
                               "--property=ControlPID", "--property=ControlGroup", "--property=TasksCurrent")
    assert timeout >= 1
    # Deliberately shuffled: property order is not a systemd contract.
    return CommandResult(0, "TasksCurrent=0\nControlGroup=/system.slice/qorqut-browser.service\nControlPID=0\n"
                            "LoadState=loaded\nMainPID=0\nActiveState=inactive\n")


def never_started_systemd(*_args, **_kwargs):
    # Read-only observed host fixture for
    # qorqut-business-browser@00000000-0000-4000-8000-000000000002.service.
    return CommandResult(0, "MainPID=0\nControlPID=0\nControlGroup=\nTasksCurrent=[not set]\n"
                            "LoadState=loaded\nActiveState=inactive\n")


def active_systemd(*_args, **_kwargs):
    return CommandResult(0, "LoadState=loaded\nActiveState=active\nMainPID=42\nControlPID=0\n"
                            "ControlGroup=/system.slice/qorqut-browser.service\nTasksCurrent=2\n")


def test_host_terminal_inventory_refuses_live_isolated_tmux_canary_then_proves_empty(tmp_path):
    executable = shutil.which("tmux")
    if executable is None:
        pytest.skip("tmux is required for the isolated host-terminal inventory fixture")
    socket, session, canary = "legacy-writers-" + new_id(), "canary", tmp_path / "canary"
    controller = HostTerminalWriters.isolated_for_test(executable, socket)
    try:
        subprocess.run([executable, "-L", socket, "-f", "/dev/null", "new-session", "-d", "-s", session,
                        "sh", "-c", "while :; do printf x >> \"$1\"; sleep .02; done", "sh", str(canary)],
                       check=True, timeout=10, capture_output=True, text=True)
        for _ in range(100):
            if canary.exists() and canary.stat().st_size:
                break
            time.sleep(.02)
        assert canary.exists() and canary.stat().st_size
        with pytest.raises(DomainError, match="Live host terminal sessions"):
            controller.inventory_and_freeze(timeout=5)
        before = canary.read_bytes()
        deadline = time.monotonic() + 5
        while len(canary.read_bytes()) <= len(before) and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(canary.read_bytes()) > len(before)  # Inventory never signals the shell or child.
        subprocess.run([executable, "-L", socket, "-f", "/dev/null", "kill-server"], check=True,
                       timeout=10, capture_output=True, text=True)
        assert controller.inventory_and_freeze(timeout=5) == QuiescenceProof("registered_host_terminals", "frozen")
    finally:
        subprocess.run([executable, "-L", socket, "-f", "/dev/null", "kill-server"], timeout=10,
                       capture_output=True, text=True, check=False)


def test_host_terminal_inventory_accepts_only_known_no_server_and_never_uses_broker_config():
    calls = []
    def runner(arguments, *, timeout):
        calls.append((arguments, timeout))
        return CommandResult(1, "no server running on /tmp/tmux-1000/bazarlyk\n")
    controller = HostTerminalWriters("/trusted/tmux", command_runner=runner)
    assert controller.inventory_and_freeze(timeout=5) == QuiescenceProof("registered_host_terminals", "frozen")
    assert calls == [(("/trusted/tmux", "-L", "bazarlyk", "-f", "/dev/null", "list-sessions",
                      "-F", "#{session_id}\t#{session_name}"), 5)]
    with pytest.raises(ValueError, match="fixed Bazarlyk"):
        HostTerminalWriters("/trusted/tmux", socket="other")
    no_socket = HostTerminalWriters("/trusted/tmux", command_runner=lambda *_args, **_kwargs: CommandResult(
        1, "error connecting to /tmp/tmux-1000/bazarlyk (No such file or directory)\n"))
    assert no_socket.inventory_and_freeze(timeout=5) == QuiescenceProof("registered_host_terminals", "frozen")
    refusing = HostTerminalWriters("/trusted/tmux", command_runner=lambda *_args, **_kwargs: CommandResult(1, "permission denied"))
    with pytest.raises(DomainError, match="unavailable"):
        refusing.inventory_and_freeze(timeout=5)


def test_selected_terminal_adoption_inventory_ignores_foreign_sessions_but_refuses_target_or_malformed_rows():
    controller = HostTerminalWriters("/trusted/tmux", command_runner=lambda *_args, **_kwargs: CommandResult(
        0, "$1\tsecond__agent\n"))
    controller.business_inactive("first__", timeout=5)
    active = HostTerminalWriters("/trusted/tmux", command_runner=lambda *_args, **_kwargs: CommandResult(
        0, "$1\tfirst__agent\n$2\tsecond__agent\n"))
    with pytest.raises(DomainError, match="Selected project"):
        active.business_inactive("first__", timeout=5)
    malformed = HostTerminalWriters("/trusted/tmux", command_runner=lambda *_args, **_kwargs: CommandResult(
        0, "$1 first__agent\n"))
    with pytest.raises(DomainError, match="malformed"):
        malformed.business_inactive("first__", timeout=5)


def test_legacy_browser_inventory_requires_inactive_unit_and_no_managed_state(tmp_path):
    root, database = tmp_path / "businesses", None
    database = database_with_business(root)
    writer = LegacyBrowserWriters(database, root, command_runner=inactive_systemd)
    assert writer.inventory_and_freeze(timeout=5) == QuiescenceProof("registered_legacy_browsers", "frozen")
    never_started = LegacyBrowserWriters(database, root, command_runner=never_started_systemd)
    assert never_started.inventory_and_freeze(timeout=5) == QuiescenceProof("registered_legacy_browsers", "frozen")
    active = LegacyBrowserWriters(database, root, command_runner=active_systemd)
    with pytest.raises(DomainError, match="active or its state is uncertain"):
        active.inventory_and_freeze(timeout=5)
    lingering = LegacyBrowserWriters(database, root, command_runner=lambda *_args, **_kwargs: CommandResult(
        0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\n"
           "ControlGroup=/system.slice/qorqut-browser.service\nTasksCurrent=1\n"))
    with pytest.raises(DomainError, match="active or its state is uncertain"):
        lingering.inventory_and_freeze(timeout=5)
    directory = root / ".bazarlyk/browsers" / BUSINESS
    directory.mkdir(parents=True)
    (directory / "ready.json").write_text('{"manager_pid":12,"manager_identity":"boot:1","chrome_pid":13,"chrome_identity":"boot:2","relay_pid":14,"relay_identity":"boot:3"}')
    live = LegacyBrowserWriters(database, root, command_runner=inactive_systemd,
                                identity_reader=lambda pid: {12: "boot:1", 13: "gone", 14: "gone"}[pid])
    with pytest.raises(DomainError, match="process remains active"):
        live.inventory_and_freeze(timeout=5)


def test_legacy_browser_lock_needs_exact_trusted_sandbox_owner_even_when_selected(tmp_path):
    root = tmp_path / "businesses"
    database = database_with_business(root)
    with database.transaction() as uow:
        SandboxStateStore().insert_intent(uow, BUSINESS)  # Selection alone is never an ownership proof.
    lock_path = root / ".bazarlyk/browsers" / BUSINESS / "service.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        unknown = LegacyBrowserWriters(database, root, command_runner=inactive_systemd)
        with pytest.raises(DomainError, match="lock remains held"):
            unknown.inventory_and_freeze(timeout=5)
        truthy = LegacyBrowserWriters(database, root, command_runner=inactive_systemd,
                                      owns_sandbox_lock=lambda _business_id: "claimed")
        with pytest.raises(DomainError, match="lock remains held"):
            truthy.inventory_and_freeze(timeout=5)
        trusted = LegacyBrowserWriters(database, root, command_runner=inactive_systemd,
                                       owns_sandbox_lock=lambda business_id: business_id == BUSINESS)
        assert trusted.inventory_and_freeze(timeout=5) == QuiescenceProof("registered_legacy_browsers", "frozen")
        active = LegacyBrowserWriters(database, root, command_runner=active_systemd,
                                      owns_sandbox_lock=lambda business_id: business_id == BUSINESS)
        with pytest.raises(DomainError, match="active or its state is uncertain"):
            active.inventory_and_freeze(timeout=5)


def test_legacy_host_composition_requires_both_conservative_inventories(tmp_path):
    root, database = tmp_path / "businesses", None
    database = database_with_business(root)
    terminals = HostTerminalWriters("/trusted/tmux", command_runner=lambda *_args, **_kwargs: CommandResult(0, ""))
    browsers = LegacyBrowserWriters(database, root, command_runner=inactive_systemd)
    combined = LegacyHostWriters(terminals, browsers)
    assert combined.inventory_and_freeze(timeout=5) == QuiescenceProof("legacy_host_writers", "frozen")


def test_legacy_browser_inventory_rejects_nonregular_ready_lock_and_symlink_ancestors(tmp_path):
    root = tmp_path / "businesses"
    database = database_with_business(root)
    directory = root / ".bazarlyk/browsers" / BUSINESS
    directory.mkdir(parents=True)
    os.mkfifo(directory / "ready.json")
    writer = LegacyBrowserWriters(database, root, command_runner=inactive_systemd)
    with pytest.raises(DomainError, match="state cannot be read"):
        writer.inventory_and_freeze(timeout=5)
    (directory / "ready.json").unlink()
    os.mkfifo(directory / "service.lock")
    with pytest.raises(DomainError, match="lock is not an ordinary file"):
        writer.inventory_and_freeze(timeout=5)
    (directory / "service.lock").unlink()
    directory.rmdir()
    browsers = root / ".bazarlyk/browsers"
    outside = tmp_path / "outside"
    outside.mkdir()
    browsers.rmdir()
    browsers.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError, match="lock cannot be inspected"):
        writer.inventory_and_freeze(timeout=5)


def test_legacy_browser_inventory_accepts_exact_known_missing_unit(tmp_path):
    root = tmp_path / "businesses"
    database = database_with_business(root)
    unit = "qorqut-business-browser@" + BUSINESS + ".service"
    writer = LegacyBrowserWriters(database, root, command_runner=lambda *_args, **_kwargs: CommandResult(
        1, "Unit " + unit + " could not be found.\n"))
    assert writer.inventory_and_freeze(timeout=5) == QuiescenceProof("registered_legacy_browsers", "frozen")
