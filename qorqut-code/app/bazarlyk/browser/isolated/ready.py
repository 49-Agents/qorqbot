"""Readiness for the isolated mode: both the namespace side and the host gateway side must be live."""

import json
import os
from pathlib import Path
import stat

from qorqut_operations.contracts import DomainError
from ..control import owns_listener, process_identity

HOST_CDP = "127.0.0.1"


def _unavailable(cause):
    return DomainError("operation_unavailable", "The managed project browser is stopped or needs an operator restart")


def ready_target(directory, config, *, identity=process_identity, listener=owns_listener,
                 netns=lambda pid: os.readlink(f"/proc/{pid}/ns/net")):
    """Validate ``ready.json`` against the saved assignment and the live processes and sockets.

    Namespace side: manager, Chrome, relay and Xtigervnc keep their recorded boot-scoped identities, Chrome is
    still in the recorded namespace and owns the CDP listener there. Host side: the same gateway instance is
    alive in a different namespace, owns the host loopback CDP forwarder and the VNC listener, and still serves
    the runtime sockets. A restart of either side changes an identity and invalidates the receipt.
    """
    try:
        status = json.loads((directory / "ready.json").read_text())
        gateway = json.loads((directory / "gateway.json").read_text())
        recorded = status["gateway"]
        runtime = Path(gateway["runtime_dir"])
        if (status["mode"] != "isolated-desktop-v1"
                or status["generation"] != config["generation"] or gateway["generation"] != config["generation"]
                or status["user_data_dir"] != config["user_data_dir"]
                or status["cdp_port"] != config["cdp_port"] or gateway["cdp_port"] != config["cdp_port"]
                or status["business_id"] != gateway["business_id"]
                or (recorded["instance"], recorded["pid"], recorded["identity"], recorded["netns"])
                != (gateway["instance"], gateway["pid"], gateway["identity"], gateway["netns"])
                or status["netns"] == gateway["netns"]
                or identity(status["manager_pid"]) != status["manager_identity"]
                or identity(status["chrome_pid"]) != status["chrome_identity"]
                or identity(status["relay_pid"]) != status["relay_identity"]
                or identity(status["xvnc_pid"]) != status["xvnc_identity"]
                or identity(gateway["pid"]) != gateway["identity"]
                or netns(status["chrome_pid"]) != status["netns"]
                or netns(gateway["pid"]) != gateway["netns"]
                or not listener(status["chrome_pid"], config["cdp_port"], "127.0.0.1")
                or not listener(gateway["pid"], config["cdp_port"], HOST_CDP)
                or not listener(gateway["pid"], gateway["vnc_port"], gateway["vnc_interface"])
                or any(not stat.S_ISSOCK(os.lstat(runtime / name).st_mode) for name in ("upstream.sock", "cdp.sock", "vnc.sock"))):
            raise ValueError("stale service")
        return {"cdp_endpoint": f"http://127.0.0.1:{config['cdp_port']}",
                "generation": config["generation"], "instance": status["instance"] + ":" + gateway["instance"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise _unavailable(exc) from exc
