"""The host's adapter to the Context integration (``qorqut_context.integration``).

The Context composition, driver, queue, filing, reads and lifecycle live in ``qorqut_context.integration``; this module only
supplies what is the host's own: the runner configuration file, the instance locations from ``InstancePaths``, the roles
eligible to instruct and edit, the configured coordinator lookup, the registered agents' names the filer is told about and
the change-feed binding of the lifecycle owner.
``compose_context`` and ``ContextServices`` keep the stock host's call shape and delegate to the integration; the host remains the
only owner that starts and stops the services."""

from __future__ import annotations

import json
import os
from dataclasses import fields, replace
from pathlib import Path
import stat

from qorqut_system import private_files
from qorqut_context.integration import service as integration
from qorqut_context.integration.service import ContextLocations, ContextSettings
from qorqut_context.runner import AppServerRunner, RunnerConfig
from qorqut_context.claude_runner import is_claude
from qorqut_operations.contracts import DomainError
from qorqut_operations.global_agents import REPEATABLE_GLOBAL_ROLES, current_global_data

from qorqut import agent_cli_launch
from qorqut.agents.roles import SUPPORTED_GLOBAL_ROLES
from qorqut.management.identity import configured_ordubashy
from qorqut.service.changes import CONTEXT_QUEUE_KIND

CONFIG_BYTES = 64 * 1024
# One shared Context: every supported global role is eligible to instruct and edit it, in any project or the Global target,
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
        descriptor = private_files.open_read(path.parent, path.name)
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


def agent_names_of(uow) -> list[str]:
    """The names the installation's registered active agents go by, read in the caller's own snapshot: each one's display
    name, its profile name and, for a role only one agent holds, the role's identifier. Retired, inactive and disabled
    registrations are left out. The Context library cleans, orders and caps the list."""
    names = []
    for row in uow.connection.execute(
            "SELECT p.display_name, a.name, a.profile_json FROM agent_profiles a JOIN principals p ON p.id=a.agent_id "
            "WHERE p.kind='agent' AND p.active=1 AND a.status='active' ORDER BY a.name, a.agent_id"):
        names += [row["display_name"], row["name"]]
        profile = current_global_data(row["profile_json"])
        role = profile["role"]["id"] if profile else None
        if role and role not in REPEATABLE_GLOBAL_ROLES:
            names.append(role)
    return names


class ManagedCodexRunner(AppServerRunner):
    """The library's app-server runner, started on the Codex Qorqbot selects at each run.

    The library runs its configured executable, ``codex`` by default, from this process's PATH, which does not hold Qorqbot's own
    downloaded Codex if it was downloaded after the service started. While that copy is the selected one it is used instead of
    the bare default; an executable named in the runner configuration is never replaced."""

    def run(self, **arguments):
        selected = agent_cli_launch.executable("codex") if self.config.executable == "codex" else None
        if selected is None:
            return super().run(**arguments)
        return AppServerRunner(replace(self.config, executable=selected)).run(**arguments)


def compose_context(application, paths, settings: ContextSettings | None = None) -> integration.ContextComposition:
    """The Context composition of one instance, bound to the host's locations, eligible roles and coordinator lookup."""
    settings = settings or ContextSettings()
    # A configuration that names the claude executable keeps the library's own runner selection (the Claude CLI runner):
    # the app-server runner would start claude with Codex arguments and every input would fail.
    if settings.runner is None and not is_claude(settings.config or RunnerConfig()):
        settings = replace(settings, runner=ManagedCodexRunner(settings.config or RunnerConfig()))
    return integration.compose_context(application, context_locations(paths), settings, instruct_roles=INSTRUCT_ROLES,
                                       coordinator=coordinator_of, agent_names=agent_names_of)


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
