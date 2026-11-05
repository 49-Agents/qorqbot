"""Authenticated business target selection and attach-to-detach ownership."""

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import socket

from bazarlyk.bootstrap.client import path_has_symlink, write_json_private
from qorqut_operations.contracts import DomainError
from .config import browser_state_directory, configured_target


def process_identity(pid):
    """Linux start ticks + boot ID prevent mistaking a reused PID for Chrome."""
    stat = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]


def owns_listener(pid, port, address=None):
    """The process holds a listening TCP socket on ``port`` (optionally only on ``address``)."""
    wanted = None
    if address is not None:
        wanted = "".join(f"{byte:02X}" for byte in reversed(socket.inet_aton(address)))
    sockets = set()
    for path in Path(f"/proc/{int(pid)}/fd").iterdir():
        try:
            sockets.add(path.readlink().as_posix())
        except FileNotFoundError:
            continue  # Unrelated Chrome descriptors can close while inspecting.
    for protocol in ("tcp", "tcp6"):
        for line in Path(f"/proc/{int(pid)}/net/{protocol}").read_text().splitlines()[1:]:
            fields = line.split()
            host, _, number = fields[1].rpartition(":")
            if (fields[3] == "0A" and int(number, 16) == port and (wanted is None or host == wanted)
                and "socket:[" + fields[9] + "]" in sockets):
                return True
    return False


def ready_target(directory, config):
    if config.get("mode") == "isolated-desktop-v1":
        from .isolated.ready import ready_target as isolated_target
        return isolated_target(directory, config)
    try:
        status = json.loads((directory / "ready.json").read_text())
        if (status["generation"] != config["generation"]
            or status["user_data_dir"] != config["user_data_dir"]
            or process_identity(status["manager_pid"]) != status["manager_identity"]
            or process_identity(status["chrome_pid"]) != status["chrome_identity"]
            or process_identity(status["relay_pid"]) != status["relay_identity"]
            or not owns_listener(status["chrome_pid"], config["cdp_port"])):
            raise ValueError("stale service")
        # Chrome rewrites argv into a process title on Linux. Bind the managed
        # launch's profile/generation to PID start identity and socket ownership,
        # not a fragile parse of that mutable display string.
        return {"cdp_endpoint": f"http://127.0.0.1:{config['cdp_port']}",
                "generation": config["generation"], "instance": status["instance"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise DomainError("operation_unavailable", "The managed project browser is stopped or needs an operator restart") from exc


def coordinator_lease_ended(database, lease):
    """An explicit Global agent's record whose turn no longer owns the queue holds nothing (never another holder's)."""
    if not lease or lease.get("coordinator") is not True:
        return False
    with database.read() as uow:
        return uow.connection.execute("SELECT 1 FROM browser_turns WHERE id=? AND state='owning' AND lease_id=?",
                                      (lease.get("turn_id"), lease.get("turn_lease_id"))).fetchone() is None


@contextmanager
def locked_state(directory):
    if path_has_symlink(directory):
        raise DomainError("invalid_configuration", "Browser state cannot follow symlinks")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "control.lock"
    if path_has_symlink(path):
        raise DomainError("invalid_configuration", "Browser lock cannot follow symlinks")
    # Never unlink this inode: all contenders must lock the same file.
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def business_control(resolver, principal, action, token, *, owner_business_id, sandbox_target=None):
    """The owner's attach-to-detach record of one business browser, shared with every other holder through the lease."""
    if action not in {"acquire", "check", "release"} or not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
        raise DomainError("validation_error", "Invalid browser control request")
    # Only trusted host composition calls this, with one registered business ID; current human ownership is
    # still proven here on every call.
    if principal.kind != "human":
        raise DomainError("forbidden", "Private browser viewing requires the registered project owner")
    with resolver.database.read() as uow:
        business = uow.repository("businesses").get(owner_business_id, required=True)
        membership = uow.repository("business_memberships", business_id=business["id"]).get(principal.id)
        if not membership or not membership["active"] or not membership["is_owner"]:
            raise DomainError("forbidden", "Private browser viewing requires the registered project owner")
        sandbox = uow.repository("business_sandboxes").get(business["id"])
    if sandbox is not None and sandbox_target is None:
        raise DomainError("environment_unavailable", "Project sandbox browser binding is not available", retryable=True)
    directory = browser_state_directory(resolver.paths, business["id"])
    with locked_state(directory):
        path = directory / "lease.json"
        try:
            lease = json.loads(path.read_text()) if path.exists() else None
        except (ValueError, OSError) as exc:
            raise DomainError("operation_unavailable", "Browser ownership state needs operator recovery") from exc
        if coordinator_lease_ended(resolver.database, lease):
            lease = None  # The queue already gave the browser back; only the leftover record remains.
        holder_matches = bool(lease and lease.get("owner_id") == principal.id)
        owns = holder_matches and lease.get("token") == token
        if action == "release":
            if lease and not owns:
                # A true owner can explicitly release its own stale listener
                # token after a service restart, but never an agent or another
                # owner's live browser lease.
                if holder_matches:
                    write_json_private(path, {})
                    return {"released": True}
                raise DomainError("busy", "Another task owns this project browser")
            write_json_private(path, {})
            return {"released": True}
        config = configured_target(business, resolver.paths)
        target = sandbox_target(business, config) if sandbox is not None else ready_target(directory, config)
        # A new managed Chrome instance invalidates old CDP connections and old
        # ownership. Time alone never steals a live task's browser.
        if lease and lease.get("instance") != target["instance"]:
            lease, owns = None, False
        if lease and not owns and not (action == "acquire" and holder_matches):
            raise DomainError("busy", "Another task owns this project browser; wait for detach or ask the owner to recover it")
        if action == "check" and not owns:
            raise DomainError("forbidden", "Attach before running browser commands; the previous attachment is no longer valid")
        if action == "acquire" and not owns:
            write_json_private(path, {"owner_id": principal.id, "token": token, "instance": target["instance"]})
        return {**target, "business_id": business["id"]}
