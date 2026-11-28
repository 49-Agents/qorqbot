"""The local channel on Windows: a named pipe open only to the current user, and the check of who connected.

A first version, not shipped and not hardened, and not run on a Windows machine yet. The pipe's access rule names the
object's owner (``OW``) and nobody else, so another user cannot open it; the channel therefore reports no user id, which
is what ``check_caller`` documents. The control token is the same as on Unix (see ``qorqut_system.channel``).

Windows turns a path into a pipe name, so the callers keep passing the path of their state or scratch folder: no file is
created, and two different paths never share a pipe.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
import time

from .errors import UnsupportedSystem

CHANNEL_MODE = None          # a named pipe has an access rule instead of a mode; the argument is accepted and unused
BUFFER_BYTES = 65536
_ERROR_ACCESS_DENIED = 5
_ERROR_PIPE_BUSY = 231
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_CONNECTED = 535
_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_OPEN_EXISTING = 3
_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_TYPE_BYTE, _PIPE_READMODE_BYTE, _PIPE_WAIT = 0x0, 0x0, 0x0
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# A protected access rule that allows the owner of the object and nobody else.
_ACCESS_RULE = "D:P(A;;GA;;;OW)"


class _SecurityAttributes(ctypes.Structure):
    """``SECURITY_ATTRIBUTES``: the access rule a new pipe is created with (``ctypes.wintypes`` has no such type)."""
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


_kernel32 = _advapi32 = None


def _api():
    global _kernel32, _advapi32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _kernel32.CreateNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                               wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                               ctypes.POINTER(_SecurityAttributes))
        _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        _kernel32.ConnectNamedPipe.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
        _kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        _kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
        _kernel32.CreateFileW.restype = wintypes.HANDLE
        _kernel32.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        _kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        _kernel32.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        _kernel32.ReadFile.restype = wintypes.BOOL
        _kernel32.WriteFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        _kernel32.WriteFile.restype = wintypes.BOOL
        _kernel32.GetNamedPipeClientProcessId.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        _kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
        _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        _advapi32.LocalFree.argtypes = (ctypes.c_void_p,)
        _advapi32.LocalFree.restype = ctypes.c_void_p
    return _kernel32, _advapi32


def _pipe_name(path) -> str:
    """The pipe behind one path: the same path gives the same pipe, and no two paths share one."""
    digest = hashlib.sha256(os.fsencode(str(path))).hexdigest()[:32]
    return "\\\\.\\pipe\\qorqut-" + digest


def _access_rule():
    """The security attributes of a pipe that only its owner may open; the descriptor stays alive with them."""
    _kernel32, advapi32 = _api()
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(_ACCESS_RULE, 1, ctypes.byref(descriptor),
                                                                         None):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = _SecurityAttributes()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.lpSecurityDescriptor = descriptor
    attributes.bInheritHandle = False
    attributes._descriptor = descriptor              # keep the descriptor alive with the attributes
    return attributes


class _Connection:
    """One end of a connection: ``sendall``, ``recv``, ``settimeout`` and ``close`` as a socket has them."""

    def __init__(self, handle, path=None):
        self._handle, self.path, self._timeout = handle, path, None

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()

    def settimeout(self, seconds):
        """Remember the timeout. This version waits without a bound on a pipe read, as a blocking socket does."""
        self._timeout = seconds

    def sendall(self, data):
        data, offset = bytes(data), 0
        _kernel32, _ = _api()
        while offset < len(data):
            written = wintypes.DWORD(0)
            if not _kernel32.WriteFile(self._handle, data[offset:], len(data) - offset, ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not written.value:
                raise OSError("The channel accepted no data")
            offset += written.value

    def recv(self, size):
        buffer = ctypes.create_string_buffer(max(1, int(size)))
        read = wintypes.DWORD(0)
        _kernel32, _ = _api()
        if not _kernel32.ReadFile(self._handle, buffer, len(buffer), ctypes.byref(read), None):
            error = ctypes.get_last_error()
            if error == _ERROR_BROKEN_PIPE:
                return b""                              # the other end has gone, as a socket reports it
            raise ctypes.WinError(error)
        return buffer.raw[:read.value]

    def close(self):
        if self._handle is not None:
            _api()[0].CloseHandle(self._handle)
            self._handle = None


class _Listener:
    """A listening pipe: ``accept()`` gives the next connection, ``settimeout`` and ``close`` as a socket has them."""

    def __init__(self, path, backlog):
        self.path, self._backlog, self._handle, self._timeout = path, backlog, None, None
        self._open(first=True)

    def _open(self, *, first=False):
        """Listen on one more instance of the pipe. Only the first refuses to share a name another listener took."""
        _kernel32, advapi32 = _api()
        attributes = _access_rule()
        try:
            handle = _kernel32.CreateNamedPipeW(_pipe_name(self.path),
                                               _PIPE_ACCESS_DUPLEX | (_FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
                                               _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT
                                               | _PIPE_REJECT_REMOTE_CLIENTS,
                                               max(1, int(self._backlog)), BUFFER_BYTES, BUFFER_BYTES, 0,
                                               ctypes.byref(attributes))
        finally:
            advapi32.LocalFree(attributes.lpSecurityDescriptor)      # the pipe keeps its own copy of the rule
        if handle == _INVALID_HANDLE_VALUE or handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle

    def settimeout(self, seconds):
        """Remember the timeout. This version waits without a bound for the next client."""
        self._timeout = seconds

    def accept(self):
        if self._handle is None:
            raise OSError("The channel is closed")
        _kernel32, _ = _api()
        if not _kernel32.ConnectNamedPipe(self._handle, None):
            error = ctypes.get_last_error()
            if error != _ERROR_PIPE_CONNECTED:
                raise ctypes.WinError(error)
        handle, self._handle = self._handle, None
        try:
            self._open()                                 # ready for the next client
        except OSError:
            pass                                         # this connection is made; the next accept reports it
        return _Connection(handle, self.path), str(self.path)

    def close(self):
        if self._handle is not None:
            _api()[0].CloseHandle(self._handle)
            self._handle = None


def listen(path, *, backlog=16, mode=CHANNEL_MODE, timeout=None):
    """Listen on the pipe of ``path``, open to this user only; ``OSError`` when one is already listening there."""
    listener = _Listener(path, backlog)
    if timeout is not None:
        listener.settimeout(timeout)
    return listener


def listen_here(path, *, backlog=16, mode=CHANNEL_MODE, timeout=None):
    """This system has one listener form: bind the pipe, open to this user only."""
    return listen(path, backlog=backlog, timeout=timeout)


def new_socket():
    """Not available here: a named pipe is created by its server."""
    raise UnsupportedSystem("A local channel is a named pipe on Windows, not a socket the caller connects")


def connect_socket(connection, path, *, timeout=None):
    """Not available here: a named pipe is connected by opening it."""
    raise UnsupportedSystem("A local channel is a named pipe on Windows, not a socket the caller connects")


def connect(path, *, timeout=None):
    """Connect to the channel at ``path``; ``OSError`` when nobody is listening there."""
    _kernel32, _ = _api()
    name = _pipe_name(path)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        handle = _kernel32.CreateFileW(name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
        if handle != _INVALID_HANDLE_VALUE and handle is not None:
            return _Connection(handle, path)
        error = ctypes.get_last_error()
        if error != _ERROR_PIPE_BUSY:
            raise ctypes.WinError(error)
        wait = 0 if deadline is None else max(0, int((deadline - time.monotonic()) * 1000))
        if deadline is not None and wait == 0:
            raise TimeoutError("connecting to the channel timed out")
        if not _kernel32.WaitNamedPipeW(name, wait):
            error = ctypes.get_last_error()
            if error == _ERROR_ACCESS_DENIED:
                raise ctypes.WinError(error)
            raise TimeoutError("connecting to the channel timed out")


def listen_in_directory(dir_fd, name, *, mode=CHANNEL_MODE, backlog=8, timeout=0.2):
    """Not available here: a named pipe is not a name inside a folder, so it has no directory-descriptor form."""
    raise UnsupportedSystem("A local channel inside a retained directory is available on Linux only")


def connect_in_directory(dir_fd, name, *, mode=CHANNEL_MODE, timeout=None):
    """Not available here: a named pipe is not a name inside a folder, so it has no directory-descriptor form."""
    raise UnsupportedSystem("A local channel inside a retained directory is available on Linux only")


def remove_in_directory(dir_fd, name, identity):
    """Not available here: a named pipe is not a name inside a folder, so it has no directory-descriptor form."""
    raise UnsupportedSystem("A local channel inside a retained directory is available on Linux only")


def peer(connection):
    """``(pid, None)`` of the other end: Windows reports the process, and the pipe's access rule is the user check."""
    pid = None
    handle = getattr(connection, "_handle", None)
    if handle is not None:
        value = wintypes.ULONG(0)
        if _api()[0].GetNamedPipeClientProcessId(handle, ctypes.byref(value)):
            pid = int(value.value) or None
    return pid, None


def check_caller(connection, *, uid=None, pid=None, read_peer=None) -> bool:
    """Whether the other end is this user and, when ``pid`` is given, that exact process.

    The pipe's access rule admits its owner only, so a caller needs no user id here. A caller that asks for one cannot be
    answered on Windows and is refused. ``read_peer`` replaces the peer lookup for a caller that brings its own reader."""
    if uid is not None:
        return False
    found_pid, _uid = (read_peer or peer)(connection)
    return pid is None or found_pid == pid