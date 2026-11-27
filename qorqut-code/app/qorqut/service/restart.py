"""Asking for the service to be started again.

``request_restart(reason)`` stops the running server as a stop signal does, but without recording a signal (``stop`` of
the server in ``service/main.py``): uvicorn raises every signal it recorded again once it has stopped, which would end the
process by that signal before the service could exit with ``EXIT_RESTART``. The service then exits with ``EXIT_RESTART``
instead of 0. Whoever started the service starts it again on that code: the native launcher does
(``tools/distribution/native_launch.py`` ``run_service``), and a service manager does when it is told to treat the code
as a restart. Qorqbot itself never starts a second copy of the service.

The code is sysexits' ``EX_TEMPFAIL`` (75). The service otherwise exits with 0, 1 (configuration refused) or 3 (never
started), and the launcher reports a signal as 128 plus its number, so 75 means nothing else to either.
"""

from __future__ import annotations

import logging
import os
import threading

EXIT_RESTART = os.EX_TEMPFAIL

_lock = threading.Lock()
_server = None
_reason: str | None = None


def attach(server) -> None:
    """The running server that a restart request stops: it has ``stop()``, which ends serving without a signal (the
    ``uvicorn.Server`` of ``service/main.py``). Called once, before it runs."""
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
        # The service's log holds warnings and above only (it sets no level of its own), so this line is a warning.
        logging.getLogger(__name__).warning("Restart requested: %s", reason)
        _server.stop()
    return True


def requested() -> str | None:
    """The reason of the restart asked for in this process, or None."""
    with _lock:
        return _reason
