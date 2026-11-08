"""The host's adapter to the Context integration (``qorqut_context.integration``).

The Context composition, driver, queue, filing, reads and lifecycle live in ``qorqut_context.integration``; this module only
supplies what is the host's own: the runner configuration file, the instance locations from ``InstancePaths``, the roles
eligible to instruct and edit, the configured coordinator lookup and the change-feed binding of the lifecycle owner.
``compose_context`` and ``ContextServices`` keep the stock host's call shape and delegate to the integration; the host remains the
only owner that starts and stops the services."""

from __future__ import annotations

import json
import os
from dataclasses import fields
from pathlib import Path
import stat

from qorqut_context.integration import service as integration
from qorqut_context.integration.service import ContextLocations, ContextSettings
from qorqut_context.runner import RunnerConfig
from qorqut_operations.contracts import DomainError

from qorqut.agents.roles import SUPPORTED_GLOBAL_ROLES
from qorqut.management.identity import configured_ordubashy
from qorqut.service.changes import CONTEXT_QUEUE_KIND

CONFIG_BYTES = 64 * 1024
# One shared Context: every supported global role is eligible to instruct and edit it, in any business or the Global target,
# through the same validated operations, policy and source checks. Eligibility grants nothing: a worker has no default write
# grant and needs the ordinary explicit permission, rechecked at execution.
INSTRUCT_ROLES = SUPPORTED_GLOBAL_ROLES


def runner_config_from_file(path) -> RunnerConfig:
    """The library's ``RunnerConfig`` from an explicit JSON file (only its own field names, validated by the library).

    The file is operator configuration, not a credential store: an absolute path, a regular file that is not a symlink, bounded in
    size. Anything missing keeps the library default."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise DomainError("invalid_configuration", "The Context runner configuration needs an absolute path")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise DomainError("invalid_configuration", "The Context runner configuration cannot be read") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DomainError("invalid_configuration", "The Context runner configuration is not a regular file")
        data = os.read(descriptor, CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > CONFIG_BYTES:
        raise DomainError("invalid_configuration", "The Context runner configuration is too large")
    try:
        values = json.loads(data)
    except ValueError as error:
        raise DomainError("invalid_configuration", "The Context runner configuration is not valid JSON") from error
    known = {item.name for item in fields(RunnerConfig)}
    if not isinstance(values, dict) or set(values) - known:
        raise DomainError("invalid_configuration", "The Context runner configuration names only RunnerConfig fields")
    try:
        return RunnerConfig(**values)
    except Exception as error:      # the library validates model, effort, tier and every limit
        raise DomainError("invalid_configuration", f"Invalid Context runner configuration: {error}") from error


def context_locations(paths) -> ContextLocations:
    """The Context places of one instance, from its ``InstancePaths``."""
    return ContextLocations(repository=paths.context, runtime=paths.context_runtime, questions=paths.context_questions,
                            locks=paths.locks, operations_root=paths.operations)


def coordinator_of(uow) -> str | None:
    """The configured Ordubashy's identity if it can receive notices (any runtime), read in the caller's own snapshot."""
    return configured_ordubashy(uow)


def compose_context(application, paths, settings: ContextSettings | None = None) -> integration.ContextComposition:
    """The Context composition of one instance, bound to the host's locations, eligible roles and coordinator lookup."""
    return integration.compose_context(application, context_locations(paths), settings, instruct_roles=INSTRUCT_ROLES,
                                       coordinator=coordinator_of)


def wakes_context(event: dict) -> bool:
    """Whether a committed operation can admit or control Context work (the driver's own transactions never wake its loop)."""
    operation = event.get("operation") or ""
    return CONTEXT_QUEUE_KIND in event.get("kinds", ()) and not operation.startswith(integration.DRIVER_OPERATION_PREFIX)


class ContextServices(integration.ContextServices):
    """The lifecycle owner over the host's change feed: ``changes`` (the host's commit-event feed, or None when there is none)."""

    def __init__(self, composition, database, paths, *, programs, reader, changes=None):
        self.changes = changes
        super().__init__(composition, database, context_locations(paths), programs=programs, reader=reader,
                         subscribe_commit=(lambda listener: None) if changes is None else changes.add_listener,
                         unsubscribe_commit=(lambda listener: None) if changes is None else changes.remove_listener,
                         should_wake=wakes_context)
