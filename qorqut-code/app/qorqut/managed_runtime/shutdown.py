"""Close every managed agent host of one installation, for the updater and the extension installer.

    python -m qorqut.managed_runtime.shutdown --instance-root <instance> [--timeout N]

The service is stopped when this runs (the callers hold the installation locks), so no host is being started. For each agent folder
under ``<instance>/operations/.runtime/managed-agents`` it

1. asks a live host to close gracefully through the control protocol (``HostClient.close``), all hosts at once;
2. waits until their locks are free, at most ``--timeout`` seconds (default 30);
3. as a last resort ends a host that is still there, by pid, only after checking that the process is what ``host.json`` says (boot
   identity and start time) and that its command line runs this module for exactly this agent folder, and then the recorded client
   process (checked by its recorded identity as well);
4. removes what a dead host left behind (``state.clean_stale``: token, socket, a stale record, and a client that outlived its host).

Closed sessions open again by themselves at the next input, so nothing else is needed. Safe to run twice and safe with no host,
with stale files or with no state folder at all. Nothing is ever killed by name or port, and a process that cannot be proven to be a
host of this installation is left alone and reported as remaining.

The report is one JSON object on standard output::

    {"state_root": ..., "ok": bool, "agents": [{"agent_id", "result", ...}], "remaining": [agent ids]}

``result`` is ``absent`` (nothing was running), ``closed`` (it closed when asked), ``ended`` (it had to be ended by pid),
``cleaned`` (only stale files were found, or it was gone before it could be asked) or ``remaining`` (``reason`` says why). The exit status is 0 only when no host remains.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from . import state as layout
from .client import HostClient, HostUnavailable

DEFAULT_TIMEOUT = 30.0
CLOSE_REQUEST_TIMEOUT = 5.0
POLL_SECONDS = 0.1


def runtime_directory(instance_root) -> Path:
    return Path(instance_root) / "operations" / ".runtime"


def agent_directories(state_root) -> list:
    """Every real directory under the state root (a symlink is never followed)."""
    root = Path(state_root)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if entry.is_dir() and not entry.is_symlink()]


def lock_free(directory) -> bool:
    return not layout.lock_held(Path(directory) / layout.LOCK_FILE)


def client_alive(raw) -> bool:
    pid, identity = raw.get("claude_pid"), raw.get("claude_identity")
    return isinstance(pid, int) and not isinstance(pid, bool) and bool(identity) and layout.process_alive(pid, identity)


def ask_to_close(directory, timeout=CLOSE_REQUEST_TIMEOUT) -> dict:
    """One graceful close request. ``asked`` is True when the host answered or may have received it."""
    try:
        reply = HostClient(directory, timeout=timeout).close("graceful", timeout=timeout)
    except HostUnavailable as error:
        return {"asked": error.sent, "error": error.reason}
    return {"asked": reply.get("status") in ("ok", "rejected"), "error": None if reply.get("status") == "ok" else reply.get("reason")}


def end_by_pid(directory) -> dict:
    """End the host recorded in ``host.json`` and its client, each only when the exact process (pid and start time) still runs
    and, for the host, when it runs this module for this folder. Returns what was done."""
    directory = Path(directory)
    raw = layout.read_json(directory / layout.HOST_FILE) or {}
    done = {"host": False, "client": False, "verified_host": False}
    pid, identity = raw.get("host_pid"), raw.get("host_identity")
    if isinstance(pid, int) and not isinstance(pid, bool) and identity and layout.process_alive(pid, identity):
        if layout.host_command_matches(pid, directory):
            done["verified_host"] = True
            done["host"] = layout.terminate_process(pid, identity)
    client_pid, client_identity = raw.get("claude_pid"), raw.get("claude_identity")
    if isinstance(client_pid, int) and not isinstance(client_pid, bool) and client_identity \
            and layout.process_alive(client_pid, client_identity):
        done["client"] = layout.terminate_process(client_pid, client_identity)
    return done


def finish(directory) -> dict:
    """The directory after a host is gone: ``clean_stale`` takes the recorded client as well when it is still alive."""
    cleaned = {"stale_client": False}
    try:
        cleaned["stale_client"] = bool(layout.clean_stale(directory))
    except (OSError, ValueError):
        pass
    return cleaned


def shutdown(instance_root, timeout: float = DEFAULT_TIMEOUT, *, close_timeout: float = CLOSE_REQUEST_TIMEOUT,
             sleep=time.sleep, clock=time.monotonic) -> dict:
    runtime = runtime_directory(instance_root)
    state_root = layout.state_root(SimpleNamespace(runtime=runtime))
    agents = {}
    live = []
    for directory in agent_directories(state_root):
        name = directory.name
        if lock_free(directory):
            raw = layout.read_json(directory / layout.HOST_FILE) or {}
            record = layout.read_host(directory)
            leftovers = (directory / layout.TOKEN_FILE).exists() or (directory / layout.SOCKET_FILE).exists() \
                or (record is not None and record["state"] in layout.LIVE_STATES) \
                or client_alive(raw)
            if leftovers:
                agents[name] = {"agent_id": name, "result": "cleaned", **finish(directory)}
            else:
                agents[name] = {"agent_id": name, "result": "absent"}
        else:
            live.append(directory)

    if live:
        with ThreadPoolExecutor(max_workers=min(8, len(live))) as pool:
            asked = dict(zip((d.name for d in live), pool.map(lambda directory: ask_to_close(directory, close_timeout), live)))
        deadline = clock() + max(0.0, float(timeout))
        waiting = list(live)
        while waiting and clock() < deadline:
            waiting = [d for d in waiting if not lock_free(d)]
            if waiting:
                sleep(POLL_SECONDS)
        waiting = [d for d in waiting if not lock_free(d)]
        waiting_names = {d.name for d in waiting}
        for directory in live:
            if directory.name not in waiting_names:
                # A host that never received the request and was gone by the time we looked left only files to clean.
                result = "closed" if asked[directory.name]["asked"] else "cleaned"
                agents[directory.name] = {"agent_id": directory.name, "result": result, "asked": asked[directory.name],
                                          **finish(directory)}
        if waiting:
            with ThreadPoolExecutor(max_workers=min(8, len(waiting))) as pool:
                forced = dict(zip((d.name for d in waiting), pool.map(end_by_pid, waiting)))
            for directory in waiting:
                name = directory.name
                outcome = forced[name]
                # A lock that stays held by a process that cannot be shown to be this agent's host is never forced.
                deadline = clock() + 3.0
                while not lock_free(directory) and clock() < deadline:
                    sleep(POLL_SECONDS)
                entry = {"agent_id": name, "asked": asked[name], **outcome}
                if lock_free(directory):
                    entry.update(result="ended", **finish(directory))
                else:
                    entry.update(result="remaining", reason="host_not_ended" if outcome["verified_host"] else "host_unverified")
                agents[name] = entry
    remaining = sorted(name for name, entry in agents.items() if entry["result"] == "remaining")
    return {"state_root": str(state_root), "ok": not remaining, "agents": [agents[name] for name in sorted(agents)],
            "remaining": remaining}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Close every managed agent host of one installation.")
    parser.add_argument("--instance-root", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    arguments = parser.parse_args(argv)
    if arguments.timeout < 0 or arguments.timeout > 3600:
        parser.error("--timeout must be between 0 and 3600 seconds")
    report = shutdown(arguments.instance_root, arguments.timeout)
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
