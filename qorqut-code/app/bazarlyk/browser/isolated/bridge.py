"""Fixed-destination byte-stream bridges. No protocol parsing, credentials or per-request targets."""

import os
import socket
import stat
import threading

from qorqut_operations.contracts import DomainError

BUFFER = 65536
CONNECT_TIMEOUT = 10


def _pump(source, target):
    try:
        while True:
            data = source.recv(BUFFER)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _identity(info):
    # ctime as well as the inode number: a freed inode can be reused by a replacement endpoint.
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IFMT(info.st_mode), info.st_ctime_ns


def claim_socket_path(path, *, replace_stale):
    """Make a runtime socket path ready for a new listener without touching anything that may be live.

    A path with nothing there (or that vanishes while it is checked) is ready. A served socket is refused. Only
    an own socket that positively answered ECONNREFUSED and is still the same inode is removed, and only when
    `replace_stale` says so. A timeout, a permission error, a full backlog or any other failure leaves liveness
    unknown: the endpoint is preserved and the claim is refused.
    """
    path = os.fspath(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise DomainError("environment_conflict", "A runtime socket path is occupied by another object")
    probe = socket.socket(socket.AF_UNIX)
    try:
        probe.settimeout(1)
        try:
            probe.connect(path)
        except FileNotFoundError:
            return
        except ConnectionRefusedError:
            pass
        except OSError:
            raise DomainError("busy", "A runtime socket could not be shown stale; it was left in place") from None
        else:
            raise DomainError("busy", "A runtime socket is already served by another process")
    finally:
        probe.close()
    if not replace_stale:
        raise DomainError("busy", "A stale runtime socket exists; its owner has not cleaned up")
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return
    if _identity(current) != _identity(info):
        raise DomainError("busy", "A runtime socket changed while it was checked; it was left in place")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def unix_listener(path, *, replace_stale=False):
    """A 0600 Unix listener. A responding or unprovable socket is never replaced; a stale one only if asked."""
    path = os.fspath(path)
    claim_socket_path(path, replace_stale=replace_stale)
    sock = socket.socket(socket.AF_UNIX)
    previous = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    sock.listen(64)
    return sock


def tcp_listener(address, port):
    """One specific address; wildcard binds are never used."""
    if address in {"", "0.0.0.0", "::"}:
        raise DomainError("invalid_configuration", "Bridges bind one specific address")
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
        sock.listen(64)
    except OSError:
        sock.close()
        raise DomainError("busy", "A bridge listener address is already in use") from None
    return sock


def dial_unix(path):
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect(os.fspath(path))
    except OSError:
        sock.close()
        raise
    sock.settimeout(None)
    return sock


def dial_tcp(address, port):
    sock = socket.create_connection((address, port), timeout=CONNECT_TIMEOUT)
    sock.settimeout(None)
    return sock


class Forwarder:
    """Accept on ``listener`` and copy bytes to one fixed destination reached by ``dial``."""

    def __init__(self, name, listener, dial, limit=64):
        self.name, self.listener, self.dial = name, listener, dial
        self._slots = threading.BoundedSemaphore(limit)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._active = set()
        self._thread = threading.Thread(target=self._accept, name="bridge-" + name, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def alive(self):
        return self._thread.is_alive() and not self._closed.is_set()

    def _accept(self):
        self.listener.settimeout(0.5)
        while not self._closed.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._serve, args=(client,), name="bridge-" + self.name + "-conn", daemon=True).start()

    def _serve(self, client):
        upstream = None
        try:
            try:
                upstream = self.dial()
            except OSError:
                return
            with self._lock:
                if self._closed.is_set():
                    return
                self._active.update((client, upstream))
            reverse = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
            reverse.start()
            _pump(client, upstream)
            reverse.join()
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    with self._lock:
                        self._active.discard(sock)
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._slots.release()

    def close(self):
        self._closed.set()
        try:
            self.listener.close()
        except OSError:
            pass
        with self._lock:
            active = list(self._active)
        for sock in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread.ident is not None:
            self._thread.join(timeout=3)
