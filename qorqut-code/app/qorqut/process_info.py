"""Local process observations on Linux and macOS; unavailable evidence raises OSError.

The operations live in the system layer (``qorqut_system.processes``). This module keeps their names for the files
that import them during the move and passes on to the layer; their importers do not change.
"""

from __future__ import annotations

from qorqut_system import processes as _layer

CLAUDE_VERSIONED = _layer.CLAUDE_VERSIONED


def darwin():
    return _layer.darwin()


def fields(pid, terminal=False):
    return _layer.fields(pid, terminal=terminal)


def start_token(pid):
    return _layer.start_token(pid)


def is_zombie(pid):
    return _layer.is_zombie(pid)


def pid_identity(pid):
    return _layer.pid_identity(pid)


def boot_id():
    return _layer.boot_id()


def namespace_id():
    return _layer.namespace_id()


def machine_id():
    return _layer.machine_id()


def identity(pid):
    return _layer.identity(pid)


def cmdline(pid):
    return _layer.cmdline(pid)


def name(pid):
    return _layer.name(pid)


def exe(pid):
    return _layer.exe(pid)


def cwd(pid):
    return _layer.cwd(pid)


def environ(pid):
    return _layer.environ(pid)


def uids(pid):
    return _layer.uids(pid)


def parent_pid(pid):
    return _layer.parent_pid(pid)


def pids():
    return _layer.pids()


def owns_listener(pid, port, address=None):
    return _layer.owns_listener(pid, port, address)


def owned(root, *, exclude=()):
    return _layer.owned(root, exclude=exclude)


def main(argv=None):
    return _layer.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())