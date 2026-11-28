"""The local channel on Linux and macOS: a Unix socket in a private folder, and the check of who connected.

The lines are today's: the bind under a temporary umask with mode 0600, the replacement of a socket a dead listener left,
the listen backlog and timeout, the connect with a timeout, and the peer credentials (``SO_PEERCRED`` on Linux,
``LOCAL_PEERPID`` and ``LOCAL_PEERCRED`` on macOS).
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import stat
import struct
import sys

from . import places
from .errors import PathChanged, UnsafePath
from .retained import require_descriptor_socket_paths

CHANNEL_MODE = 0o600
# While the socket is bound, nothing else this process creates may be readable by another user.
_BIND_UMASK = 0o177


def listen(path, *, backlog=16, mode=CHANNEL_MODE, timeout=None):
    """Listen on ``path`` for this user only; ``UnsafePath`` when the path is too long for the system's socket address.

    A socket file that a listener which is gone left behind is replaced. Anything else at the path is left alone and the
    bind fails with ``OSError``. The returned listener is a socket: ``accept()``, ``settimeout()`` and ``close()``."""
    path = Path(path)
    if not places.channel_path_fits(path):
        raise UnsafePath("The path is too long for a local channel on this system")
    try:
        if path.is_socket():
            path.unlink()
    except OSError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(_BIND_UMASK)
    try:
        server.bind(str(path))
    except BaseException:
        server.close()
        raise
    finally:
        os.umask(previous)
    try:
        os.chmod(path, mode)
        server.listen(backlog)
        if timeout is not None:
            server.settimeout(timeout)
    except BaseException:
        server.close()
        raise
    return server


def listen_here(path, *, backlog=16, mode=CHANNEL_MODE, timeout=None):
    """Bind, set the mode and listen, exactly those steps: no path-length check, no replacement of an existing socket
    and no umask change. For a caller that made the folder private itself."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        os.chmod(path, mode)
        server.listen(backlog)
        if timeout is not None:
            server.settimeout(timeout)
    except BaseException:
        server.close()
        raise
    return server


def new_socket():
    """A new local stream socket, created and nothing else: the caller connects it and closes it."""
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


def connect_socket(connection, path, *, timeout=None):
    """Set the timeout and connect an existing socket; the errors are the socket's own."""
    if timeout is not None:
        connection.settimeout(timeout)
    connection.connect(str(path))


def connect(path, *, timeout=None):
    """Connect to the channel at ``path``; ``OSError`` when nobody is listening there."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            connection.settimeout(timeout)
        connection.connect(str(path))
    except BaseException:
        connection.close()
        raise
    return connection


def _address_in(dir_fd, name):
    """The address of ``name`` inside an open private directory descriptor, through this system's descriptor path."""
    return "%s/%d/%s" % ("/proc/self/fd", int(dir_fd), name)


def _entry(dir_fd, name):
    """``((device, inode), stat result)`` of ``name`` inside an open directory descriptor, without following a link."""
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    return (info.st_dev, info.st_ino), info


def listen_in_directory(dir_fd, name, *, mode=CHANNEL_MODE, backlog=8, timeout=0.2):
    """Listen on ``name`` inside an open private directory descriptor, without replacing anything already there.

    An entry that exists already is refused with ``FileExistsError``. The socket is bound through the descriptor, so the
    caller's checked folder is the one used; its mode is then set and the bound object confirmed to be still the socket
    this call created. Returns ``(listener, identity)``, where ``identity`` is ``(device, inode)`` so the caller can remove
    that socket and nothing else. Only Linux supports descriptor paths that traverse a folder."""
    require_descriptor_socket_paths()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        try:
            os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "The path already exists", name)
        listener.bind(_address_in(dir_fd, name))
        identity, _bound = _entry(dir_fd, name)
        os.chmod(name, mode, dir_fd=dir_fd, follow_symlinks=False)
        seen, info = _entry(dir_fd, name)
        if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
            raise UnsafePath("The local channel could not be secured")
        if seen != identity:
            raise PathChanged("The local channel changed while it was being secured")
        listener.listen(backlog)
        if timeout is not None:
            listener.settimeout(timeout)
    except BaseException:
        listener.close()
        remove_in_directory(dir_fd, name, identity)
        raise
    return listener, identity


def connect_in_directory(dir_fd, name, *, mode=CHANNEL_MODE, timeout=None):
    """Connect to the channel ``name`` inside an open private directory descriptor, after checking it is that socket.

    ``UnsafePath`` when nothing is there, or when what is there is not a socket with this mode; ``OSError`` when nobody
    listens. Only Linux supports descriptor paths that traverse a folder."""
    require_descriptor_socket_paths()
    try:
        _identity, info = _entry(dir_fd, name)
    except FileNotFoundError as error:
        raise UnsafePath("The local channel is not there") from error
    if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
        raise UnsafePath("The local channel at that path is not the expected socket")
    return connect(_address_in(dir_fd, name), timeout=timeout)


def remove_in_directory(dir_fd, name, identity):
    """Remove ``name`` inside an open directory descriptor, only when it is a socket with exactly ``identity``.

    ``identity`` is what ``listen_in_directory`` returned, so a path that was replaced by anything else is left alone."""
    if identity is None:
        return
    try:
        seen, info = _entry(dir_fd, name)
    except FileNotFoundError:
        return
    if seen == tuple(identity) and stat.S_ISSOCK(info.st_mode):
        os.unlink(name, dir_fd=dir_fd)


def peer(connection):
    """``(pid, uid)`` of the other end, either ``None`` when the system cannot say.

    Linux: ``SO_PEERCRED``. macOS: ``LOCAL_PEERPID`` and ``LOCAL_PEERCRED`` (``struct xucred``)."""
    pid = uid = None
    try:
        if sys.platform == "darwin":
            pid = int.from_bytes(connection.getsockopt(0, 0x002, 4)[:4], sys.byteorder, signed=True)
            credential = connection.getsockopt(0, 0x001, 76)           # struct xucred: version, uid, ngroups, groups
            uid = int.from_bytes(credential[4:8], sys.byteorder)
        else:
            pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                       struct.calcsize("3i")))
    except (OSError, AttributeError, ValueError):
        pass
    return pid, uid


def check_caller(connection, *, uid=None, pid=None, read_peer=None) -> bool:
    """Whether the other end is this user (``uid`` when given) and, when ``pid`` is given, that exact process.

    A system that cannot report the peer is treated as unable to answer: the caller is refused. ``read_peer`` replaces
    the peer lookup for a caller that brings its own reader (the managed host with an injected one)."""
    found_pid, found_uid = (read_peer or peer)(connection)
    if found_uid is None or found_uid != (os.geteuid() if uid is None else uid):
        return False
    return pid is None or found_pid == pid