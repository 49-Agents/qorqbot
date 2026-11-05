import os
import select
import socket
import subprocess
import threading

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.browser.socket import BrowserBinding, BrowserSocketConnector, _HELPER


BUSINESS = "00000000-0000-4000-8000-000000000001"
BROWSER = "a" * 64
NETNS = "c" * 64


class LocalCDP:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.received = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            connection, _ = self.listener.accept()
            with connection:
                self.received = connection.recv(64)
                connection.sendall(b"cdp-canary")
        except OSError:
            pass

    def close(self):
        self.listener.close()
        self.thread.join(2)


class Docker:
    def __init__(self, *, namespace=NETNS, role="browser", address="127.0.0.1"):
        self.namespace, self.role, self.address = namespace, role, address

    def owned(self, kind, identity, ownership):
        assert kind == "container" and identity == BROWSER
        return {"Id": BROWSER, "State": {"Running": True},
                "Config": {"Labels": {"qorqut.role": self.role, "qorqut.generation": "4"}},
                "NetworkSettings": {"SandboxKey": "/run/docker/netns/" + self.namespace,
                                     "Networks": {"agent": {"IPAddress": self.address}}}}


def binding(port, *, instance_id="chrome-1"):
    return BrowserBinding(BUSINESS, BROWSER, 4, instance_id, port)


def launcher(namespace, control, port, nonce):
    return subprocess.Popen(("/usr/bin/python3", "-I", "-c", _HELPER), stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)


def test_connector_transfers_real_loopback_fd_and_returns_nonblocking_socket():
    cdp = LocalCDP()
    current = binding(cdp.port)
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    connector = BrowserSocketConnector(Docker(), owner, current_binding=lambda: current,
                                       launcher=launcher, helper_uid=os.getuid())
    try:
        connection = connector.connect(current)
        try:
            assert connection.getblocking() is False
            connection.send(b"probe")
            readable, _, _ = select.select([connection], [], [], 3)
            assert readable and connection.recv(64) == b"cdp-canary"
        finally:
            connection.close()
    finally:
        cdp.close()
    assert cdp.received == b"probe"


def test_connector_refuses_stale_or_foreign_browser_namespace():
    cdp = LocalCDP()
    current = binding(cdp.port)
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    try:
        stale = BrowserSocketConnector(Docker(), owner,
            current_binding=lambda: BrowserBinding(BUSINESS, BROWSER, 5, "chrome-1", cdp.port),
            launcher=launcher, helper_uid=os.getuid())
        with pytest.raises(DomainError, match="stale"):
            stale.connect(current)
        wrong_role = BrowserSocketConnector(Docker(role="agent"), owner,
            current_binding=lambda: current, launcher=launcher, helper_uid=os.getuid())
        with pytest.raises(DomainError, match="current running generation"):
            wrong_role.connect(current)
    finally:
        cdp.close()


def test_connector_rejects_unverified_namespace_and_address():
    cdp = LocalCDP()
    current = binding(cdp.port)
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    try:
        invalid = BrowserSocketConnector(Docker(namespace="../../host"), owner,
            current_binding=lambda: current, launcher=launcher, helper_uid=os.getuid())
        with pytest.raises(DomainError, match="namespace"):
            invalid.connect(current)
    finally:
        cdp.close()


def _script_launcher(source, processes):
    def launch(namespace, control, port, nonce):
        process = subprocess.Popen(("/usr/bin/python3", "-I", "-c", source), stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        processes.append(process)
        return process
    return launch


_BAD_RENDEZVOUS = r'''
import array, json, socket, sys
value = json.load(sys.stdin)
rendezvous = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
rendezvous.connect(value["control"])
left, right = socket.socketpair()
try:
    descriptor = array.array("i", [right.fileno()])
    rendezvous.sendmsg([b"wrong-nonce"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor)])
finally:
    right.close(); left.close(); rendezvous.close()
'''


_MISSING_ANCILLARY = r'''
import json, socket, sys
value = json.load(sys.stdin)
rendezvous = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
rendezvous.connect(value["control"])
rendezvous.sendall(bytes.fromhex(value["nonce"]))
rendezvous.close()
'''


def test_connector_rejects_nonce_and_closes_received_ancillary_fd():
    cdp = LocalCDP()
    current = binding(cdp.port)
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    processes = []
    connector = BrowserSocketConnector(
        Docker(), owner, current_binding=lambda: current,
        launcher=_script_launcher(_BAD_RENDEZVOUS, processes), helper_uid=os.getuid(), timeout=3,
    )
    try:
        with pytest.raises(DomainError, match="rendezvous"):
            connector.connect(current)
        assert processes[0].wait(timeout=2) is not None
    finally:
        cdp.close()


def test_connector_rejects_missing_ancillary_data():
    cdp = LocalCDP()
    current = binding(cdp.port)
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    processes = []
    connector = BrowserSocketConnector(
        Docker(), owner, current_binding=lambda: current,
        launcher=_script_launcher(_MISSING_ANCILLARY, processes), helper_uid=os.getuid(), timeout=3,
    )
    try:
        with pytest.raises(DomainError, match="rendezvous"):
            connector.connect(current)
        assert processes[0].wait(timeout=2) is not None
    finally:
        cdp.close()


def test_connector_stops_and_reaps_stalled_helper():
    cdp = LocalCDP()
    current = binding(cdp.port)
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    processes = []
    stalled = "import time; time.sleep(30)"
    connector = BrowserSocketConnector(
        Docker(), owner, current_binding=lambda: current,
        launcher=_script_launcher(stalled, processes), helper_uid=os.getuid(), timeout=.1,
    )
    try:
        with pytest.raises(DomainError, match="timed out"):
            connector.connect(current)
        assert processes[0].poll() is not None
        assert processes[0].wait(timeout=0) is not None
    finally:
        cdp.close()


def test_connector_rechecks_browser_instance_before_returning_fd():
    cdp = LocalCDP()
    current = binding(cdp.port, instance_id="chrome-1")
    replacement = binding(cdp.port, instance_id="chrome-2")
    observations = iter((current, replacement))
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    connector = BrowserSocketConnector(
        Docker(), owner, current_binding=lambda: next(observations),
        launcher=launcher, helper_uid=os.getuid(), timeout=3,
    )
    try:
        with pytest.raises(DomainError, match="stale"):
            connector.connect(current)
    finally:
        cdp.close()


def test_browser_binding_rejects_privileged_cdp_ports_and_bad_instance_ids():
    with pytest.raises(DomainError, match="port"):
        binding(1023)
    with pytest.raises(DomainError, match="instance"):
        BrowserBinding(BUSINESS, BROWSER, 4, "bad instance", 1024)


_FORKED_HELPER = r'''
import array, json, socket, sys, os
value = json.load(sys.stdin)
pid = os.fork()
if pid == 0:
    rendezvous = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    rendezvous.connect(value["control"])
    target = socket.create_connection(("127.0.0.1", value["port"]), timeout=5)
    try:
        descriptor = array.array("i", [target.fileno()])
        rendezvous.sendmsg([bytes.fromhex(value["nonce"])], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor)])
    finally:
        target.close(); rendezvous.close()
    os._exit(0)
os.waitpid(pid, 0)
'''


def test_connector_accepts_trusted_forked_helper_without_pid_equality():
    cdp = LocalCDP()
    current = binding(cdp.port)
    owner = type("Ownership", (), {"business_id": BUSINESS})()
    processes = []
    connector = BrowserSocketConnector(
        Docker(), owner, current_binding=lambda: current,
        launcher=_script_launcher(_FORKED_HELPER, processes), helper_uid=os.getuid(), timeout=3,
    )
    try:
        connection = connector.connect(current)
        try:
            connection.sendall(b"forked")
            readable, _, _ = select.select([connection], [], [], 3)
            assert readable and connection.recv(64) == b"cdp-canary"
        finally:
            connection.close()
        assert processes[0].wait(timeout=2) is not None
    finally:
        cdp.close()


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in (
        "QORQUT_TEST_CDP_BROWSER_ID", "QORQUT_TEST_CDP_NETNS",
        "QORQUT_TEST_CDP_PORT", "QORQUT_TEST_CDP_INSTANCE",
    )),
    reason="requires an externally provisioned browser container namespace and loopback CDP listener",
)
def test_connector_privileged_fixed_nsenter_fixture():
    """Opt-in acceptance for the production sudo -> nsenter -> fixed helper path.

    The fixture provisions the listener in a disposable browser namespace before
    pytest starts.  It deliberately supplies no launcher override, URL, host,
    Docker argument, or helper command to the connector.
    """
    browser_id = os.environ["QORQUT_TEST_CDP_BROWSER_ID"]
    namespace = os.environ["QORQUT_TEST_CDP_NETNS"]
    port = int(os.environ["QORQUT_TEST_CDP_PORT"])
    instance = os.environ["QORQUT_TEST_CDP_INSTANCE"]
    if not re.fullmatch(r"[a-f0-9]{64}", browser_id) or not re.fullmatch(
            r"/run/docker/netns/[a-f0-9]{1,128}", namespace):
        pytest.fail("privileged CDP fixture identities are not exact Docker identities")

    class ProvisionedDocker:
        def owned(self, kind, identity, ownership):
            assert kind == "container" and identity == browser_id
            return {"Id": browser_id, "State": {"Running": True},
                    "Config": {"Labels": {"qorqut.role": "browser", "qorqut.generation": "4"}},
                    "NetworkSettings": {"SandboxKey": namespace}}

    owner = type("Ownership", (), {"business_id": BUSINESS})()
    current = BrowserBinding(BUSINESS, browser_id, 4, instance, port)
    connector = BrowserSocketConnector(
        ProvisionedDocker(), owner, current_binding=lambda: current, helper_uid=0, timeout=10,
    )
    connection = connector.connect(current)
    connection.close()
