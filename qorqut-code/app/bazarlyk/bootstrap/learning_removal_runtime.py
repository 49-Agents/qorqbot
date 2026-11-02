"""The private offline compositions of the ``v1.3-legacy-learning-removal-1`` preparation coordinator.

Role publication and profile adoption run the ordinary host Application, so they need the ordinary host composition, but
over a store that is still the recognised predecessor. ``compose_runtime`` (the inert common body of ``build_runtime``)
is reused unchanged; only what is genuinely different is supplied here:

* ``Database`` is opened for the exact reconstructed predecessor assembly of the active attempt, never a relaxed target
  (``require_selected_storage`` is untouched: a normal startup still rejects the predecessor);
* the ownership is the attempt's own held ``InstanceOwnership``: it is bound before the first ``lifetime()`` call and is
  never released by this module (``close_borrowed`` only withholds admission and closes the database handles);
* ``historical=True`` additionally declares the ten retired operations' metadata (never a handler, never importable as an
  engine): an old published role still grants them, so it can be loaded, replaced and adopted away from while the
  predecessor is still the store. The strict composition (``historical=False``) is the target registry, in which no
  retired tool is declared and none appears supported.

Nothing here starts a service, a recovery loop or a worker, calls ``writer_admission.admit`` directly or fabricates
maintenance state: the ordinary owned entry (``Runtime.own_instance``) validates the predecessor store and admits writes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path

from qorqut_operations.contracts import DomainError

from bazarlyk.bootstrap import learning_removal as removal
from bazarlyk.composition.catalog import builtin_registry
from bazarlyk.service.runtime import compose_runtime


def _require_active(attempt) -> None:
    """The factory belongs to one active, source-bound attempt: held ownership, the exact recognised predecessor."""
    if not isinstance(attempt, removal.PlanAttempt) or attempt.finished is not None:
        raise DomainError("invalid_configuration", "The offline preparation runtime needs an active migration attempt")
    if not attempt.owner.held:
        raise DomainError("ownership_lost", "The migration attempt no longer holds the instance ownership")
    attempt.owner.verify()
    if attempt.baseline_a.get("digest") is None or attempt.attempt_directory is None:
        raise DomainError("invalid_configuration", "The offline preparation runtime needs durable backup and baseline A")
    connection = removal._connect(removal._database_path(attempt.paths), "ro")
    try:
        state = removal._recognise(connection, attempt.transition)[0]
    finally:
        connection.close()
    if state != "predecessor":
        raise DomainError("unsupported_schema", "The store is not the exact recognised predecessor", details={"state": state})


def _defer_inbox_finalizer(assembly):
    from bazarlyk.inbox.events import finalize_events
    matches = [hook for hook in assembly.finalizers
               if hook is finalize_events or (isinstance(hook, partial) and hook.func is finalize_events)]
    if len(matches) != 1:
        raise DomainError("invalid_configuration", "The offline runtime cannot identify the host inbox finalizer")
    return replace(assembly, finalizers=tuple(hook for hook in assembly.finalizers if hook is not matches[0]))


def _compose(paths, transition, workspace, *, historical, agentic, context=None, defer_inbox=False):
    from qorqut_operations.background.programs import ProgramRegistry
    from qorqut_operations.storage.database import Database
    workspace = Path(workspace)
    if not workspace.is_absolute():
        raise DomainError("invalid_configuration", "The protected configuration workspace must be an absolute path")
    selection = transition.extensions
    registry = builtin_registry(selection)
    if historical:
        for entry in removal.retired_operation_entries():
            registry.declare(entry)
    assembly = _defer_inbox_finalizer(transition.predecessor) if defer_inbox else transition.predecessor
    database = Database(paths.database, assembly=assembly)
    layer, view = agentic
    return compose_runtime(workspace, paths=paths, database=database, selection=selection, agentic_layer=layer,
                           agentic_view=view, registry=registry, job_programs=ProgramRegistry(), context=context)


def inert_runtime(paths, transition, workspace=None, *, historical=False, context=None):
    """The composed (never owned, never started) runtime: the one authoritative composition of the target operations, the
    policy and every domain validator, optionally with the metadata-only historical declarations. Read-only: it opens the
    installed agentic layer with the same validation and an immutable offline reader, never opens the Operations
    database and writes nothing. A nonempty agentic WAL requires prior offline checkpointing."""
    from bazarlyk.agentic.offline import open_installation
    workspace = Path(workspace) if workspace is not None else Path(paths.instance_root)
    return _compose(paths, transition, workspace, historical=historical,
                    agentic=open_installation(paths, transition.extensions.ids), context=context)


def inert_registry(paths, transition, workspace=None, *, historical=False):
    return inert_runtime(paths, transition, workspace, historical=historical).registry


def close_borrowed(runtime) -> None:
    """End a preparation runtime without releasing the ownership it borrowed: new writer units are refused for good and
    every database handle is closed. Safe to repeat."""
    admission = runtime.application.writer_admission
    if admission is not None:
        admission.begin_shutdown()
        admission.withhold()
    runtime.database.close()


@contextmanager
def offline_runtime(attempt, workspace, *, historical, context=None):
    """The owned preparation runtime for ``attempt``: constructed only after durable backup/A, ownership bound to the
    attempt's owner before the first ``lifetime()`` call, entered through the ordinary owned entry, closed (not released)
    on every exit path. Role/profile publication and native projection only read the
    agentic module; its separate managed-update stage owns all module writes."""
    _require_active(attempt)
    from bazarlyk.agentic.offline import open_installation
    paths, transition = attempt.paths, attempt.transition
    agentic = open_installation(paths, transition.extensions.ids)
    runtime = _compose(paths, transition, workspace, historical=historical, agentic=agentic, context=context,
                       defer_inbox=True)
    try:
        runtime.ownership = attempt.owner
        runtime.own_instance()
        if runtime.ownership is not attempt.owner or not attempt.owner.held:
            raise DomainError("ownership_lost", "The preparation runtime does not hold the attempt's ownership")
        yield runtime
    finally:
        close_borrowed(runtime)
        if not attempt.owner.held:
            raise DomainError("ownership_lost", "The preparation runtime released the instance ownership")
