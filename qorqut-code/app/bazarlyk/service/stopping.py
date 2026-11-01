"""Explicit per-service stop adapters: what counts as positive evidence that a service has stopped.

A stop method returning is not proof. Each retained service has one documented stop contract, and the adapter for it
returns a ``QuiescenceProof`` only when that contract's own evidence is positive; anything else (a timeout, an
exception, a false or absent report, a result of an unexpected shape) is an unproven stop and raises. Truthiness of an
arbitrary object is never evidence. The same adapters serve the host lifetime and the recovery writer inventory.

* ``void``: ``stop(...)`` joins the worker, raises ``TimeoutError`` when the worker survived and otherwise returns
  ``None``. Returning anything else is not the documented evidence.
* ``context_loop`` and ``capture_service``: the Context driver loop and the nightly maintenance capture service report
  ``{"stopped": bool, "in_flight": bool}``; only ``stopped is True`` with ``in_flight is False`` is a stop. A step or a
  capture still in flight keeps the database and its resources open.
* ``history_observer``: the operational history observer reports ``ObserverStop``; only ``stopped is True`` is a stop,
  and a snapshot still running is not.
"""

from __future__ import annotations

from collections.abc import Callable

from qorqut_operations.storage.quiescence import QuiescenceProof, UnprovenStop

__all__ = ["capture_service", "context_loop", "drained", "history_observer", "void"]


def drained(name: str) -> QuiescenceProof:
    return QuiescenceProof(name, "drained")


def void(call: Callable[[object, float], object]) -> Callable[[str, object, float], QuiescenceProof]:
    """``call(service, timeout)`` is the service's own bounded stop (for example ``lambda x, t: x.stop(timeout=t)``)."""
    def adapter(name, service, timeout):
        result = call(service, timeout)
        if result is not None:
            raise UnprovenStop(f"{name}: stop returned {type(result).__name__}, not the documented void stop evidence")
        return drained(name)
    return adapter


def _stopped_report(call, what):
    def adapter(name, service, timeout):
        report = call(service, timeout)
        if not (isinstance(report, dict) and report.get("stopped") is True and report.get("in_flight") is False):
            raise UnprovenStop(f"{name}: the {what} did not report a completed stop ({report!r})")
        return drained(name)
    return adapter


def context_loop(call: Callable[[object, float], object]) -> Callable[[str, object, float], QuiescenceProof]:
    return _stopped_report(call, "loop")


def capture_service(call: Callable[[object, float], object]) -> Callable[[str, object, float], QuiescenceProof]:
    return _stopped_report(call, "capture service")


def history_observer(call: Callable[[object, float], object]) -> Callable[[str, object, float], QuiescenceProof]:
    def adapter(name, service, timeout):
        report = call(service, timeout)
        # ``ObserverStop`` by its attributes: a plain True, a dict or a missing report is no evidence.
        if getattr(report, "stopped", None) is not True or getattr(report, "snapshot_running", True) is not False:
            raise UnprovenStop(f"{name}: the observer did not report a completed stop ({report!r})")
        return drained(name)
    return adapter
