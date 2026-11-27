"""Asking for the service to be started again.

``request_restart(reason)`` stops the running server the way a stop signal does, and the service then exits with
``EXIT_RESTART`` instead of 0. Whoever started the service starts it again on that code: the native launcher does
(``tools/distribution/native_launch.py`` ``run_service``), and a service manager does when it is told to treat the code
as a restart. Qorqbot itself never starts a second copy of the service.

The code is sysexits' ``EX_TEMPFAIL`` (75). The service otherwise exits with 0, 1 (configuration refused) or 3 (never
started), and the launcher reports a signal as 128 plus its number, so 75 means nothing else to either.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

EXIT_RESTART = os.EX_TEMPFAIL

_lock = threading.Lock()
_server = None
_reason: str | None = None


def attach(server) -> None:
    """The running server that a restart request stops (a ``uvicorn.Server``); called once, before it runs."""
    global _server, _reason
    with _lock:
        _server, _reason = server, None


def request_restart(reason: str) -> bool:
    """Stop the attached server so the service exits with ``EXIT_RESTART``. False when no server runs in this process.

    A second request while the first is under way changes nothing: the server is stopped once.
    """
    global _reason
    with _lock:
        if _server is None:
            return False
        first = _reason is None
        if first:
            _reason = reason
    if first:
        logging.getLogger(__name__).info("Restart requested: %s", reason)
        _server.handle_exit(signal.SIGTERM, None)
    return True


def requested() -> str | None:
    """The reason of the restart asked for in this process, or None."""
    with _lock:
        return _reason
