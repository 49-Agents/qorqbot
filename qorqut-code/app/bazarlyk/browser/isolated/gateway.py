"""Host gateway: the only path between a business's isolated namespace and the host.

Three fixed byte-stream bridges under one private runtime directory:

* ``upstream.sock`` -> the frozen numeric proxy IP/port (never sees credentials);
* host ``127.0.0.1:<cdp_port>`` -> the namespace's ``cdp.sock``;
* host ``<vnc interface>:<vnc port>`` -> the namespace's ``vnc.sock``.

It is not a proxy, resolves no names, and accepts no destination from a client.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import threading
import time

from bazarlyk.bootstrap.client import path_has_symlink, write_json_private
from qorqut_operations.contracts import DomainError, new_id
from ..config import ISOLATED, configured_target
from ..control import process_identity
from . import EX_OK, exit_code
from .bridge import Forwarder, dial_tcp, dial_unix, tcp_listener, unix_listener
from .deployment import load, open_deployment_database, proxy_endpoint
from .processes import identity_alive, netns_identity


def gateway_file(deployment):
    return deployment.state_dir / "gateway.json"


def read_gateway(deployment):
    return json.loads(gateway_file(deployment).read_text())


def check_runtime_dir(deployment):
    """The unit creates this directory (RuntimeDirectory=, mode 0700); this never creates or chmods it."""
    path = deployment.runtime_dir
    if path_has_symlink(path):
        raise DomainError("invalid_configuration", "The isolated runtime directory cannot follow symlinks")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise DomainError("invalid_configuration", "The isolated runtime directory does not exist; the unit must create it (RuntimeDirectory=)") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DomainError("invalid_configuration", "The isolated runtime directory must be a private directory owned by the service user")


def load_assignment(database, deployment):
    """The saved isolated block, checked against this deployment, and the wrong-mode/sandbox refusals."""
    with database.read() as uow:
        if uow.repository("business_sandboxes").get(deployment.business_id):
            raise DomainError("environment_conflict", "This project browser is assigned to its sandbox")
        row = uow.repository("businesses").get(deployment.business_id, required=True)
        config = configured_target(row, deployment.paths)
    if config.get("mode") != ISOLATED:
        raise DomainError("invalid_configuration", "This project browser is not configured for the isolated desktop mode")
    return config


def current_assignment(database, deployment):
    """The saved block right now, or None when it disappeared, moved to a sandbox or became invalid."""
    with database.read() as uow:
        if uow.repository("business_sandboxes").get(deployment.business_id):
            return None
        row = uow.repository("businesses").get(deployment.business_id, required=True)
        return (row["config_json"] or {}).get("browser")


def serve(database, deployment, *, stop=None, log=lambda message: None):
    stop = stop or threading.Event()
    if path_has_symlink(deployment.state_dir):
        raise DomainError("invalid_configuration", "Browser state cannot follow symlinks")
    deployment.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = deployment.state_dir / "gateway.lock"
    if path_has_symlink(lock_path):
        raise DomainError("invalid_configuration", "Browser lock cannot follow symlinks")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError("busy", "This project browser gateway is already running") from None
        config = load_assignment(database, deployment)
        check_runtime_dir(deployment)
        host, port = proxy_endpoint(config["proxy_server"])
        forwarders, listeners = [], []
        published = gateway_file(deployment)
        instance = new_id()
        try:
            gateway_file(deployment).unlink(missing_ok=True)  # only ever ours: the lock proves no live gateway
            upstream = unix_listener(deployment.upstream_socket, replace_stale=True)
            listeners.append(deployment.upstream_socket)
            forwarders.append(Forwarder("upstream", upstream, lambda: dial_tcp(host, port)))
            forwarders.append(Forwarder("cdp", tcp_listener("127.0.0.1", config["cdp_port"]),
                                        lambda: dial_unix(deployment.cdp_socket)))
            forwarders.append(Forwarder("vnc", tcp_listener(deployment.vnc_interface, deployment.vnc_port),
                                        lambda: dial_unix(deployment.vnc_socket)))
            for forwarder in forwarders:
                forwarder.start()
            write_json_private(published, {
                "instance": instance, "business_id": deployment.business_id, "generation": config["generation"],
                "pid": os.getpid(), "identity": process_identity(os.getpid()), "netns": netns_identity(),
                "runtime_dir": str(deployment.runtime_dir), "cdp_port": config["cdp_port"],
                "vnc_interface": deployment.vnc_interface, "vnc_port": deployment.vnc_port,
                "upstream": {"host": host, "port": port, "socket": str(deployment.upstream_socket)}})
            log(f"gateway {instance} serving CDP 127.0.0.1:{config['cdp_port']} and VNC "
                f"{deployment.vnc_interface}:{deployment.vnc_port}")
            while not stop.wait(1):
                if not all(forwarder.alive() for forwarder in forwarders):
                    raise DomainError("operation_unavailable", "A gateway bridge stopped")
                if current_assignment(database, deployment) != config:
                    return  # Config changes stop the gateway; the operator restarts explicitly.
        finally:
            try:
                if published.exists() and json.loads(published.read_text()).get("instance") == instance:
                    published.unlink()
            except (OSError, ValueError):
                pass
            for forwarder in forwarders:
                forwarder.close()
            for path in listeners:
                try:
                    if stat.S_ISSOCK(os.lstat(path).st_mode):
                        os.unlink(path)
                except OSError:
                    pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment", required=True, type=Path)
    args = parser.parse_args(argv)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    try:
        deployment = load(args.deployment)
        database = open_deployment_database(deployment)
        serve(database, deployment, stop=stop, log=lambda message: print("isolated-gateway: " + message, flush=True))
        return EX_OK
    except (DomainError, OSError, ValueError, KeyError, TypeError) as error:
        # No exception repr: nothing here should print addresses or credentials.
        reason = error.message if isinstance(error, DomainError) else type(error).__name__
        print(f"Isolated browser gateway not started: {reason}", flush=True)
        return exit_code(error)


if __name__ == "__main__":
    raise SystemExit(main())
