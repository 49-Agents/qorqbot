"""Isolated desktop browser: one persistent Chrome in a loopback-only network namespace.

``gateway`` (host) and ``session`` (in the namespace) are two operator-installed
supervisors joined only by 0600 Unix sockets in a private runtime directory.
See docs/BROWSER-ISOLATED.md.
"""

import errno

from qorqut_operations.contracts import DomainError

EX_OK, EX_FAILED, EX_TEMPFAIL, EX_CONFIG = 0, 1, 75, 78
PERMANENT = {"unsupported_schema", "invalid_database", "invalid_backup", "invalid_configuration", "environment_conflict"}


def exit_code(error):
    """Map a startup fault to the exit status the unit template acts on."""
    if isinstance(error, DomainError):
        if error.code in PERMANENT:
            return EX_CONFIG
        if error.code == "busy":
            return EX_TEMPFAIL
    return EX_FAILED


def unreachable(error):
    """True for the errors a routeless namespace gives instead of a connection."""
    return isinstance(error, OSError) and error.errno in {
        errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}
