"""The scripting-and-automation extension: ``qorqut_automation.extension:create``.

The factory validates configuration and returns the immutable declaration; it opens nothing. ``install(runtime)`` registers the
``automations_*`` operations and the one ``automation.run`` program on the still-open registries, binds the central policy gate
(and the narrow typed-grant capability source) and installs the read-only ``application.automations`` reference helper. The extension
owns exactly two tables (``automations``, ``automation_steps``); a run is an ordinary ``jobs`` row of the program, scheduled by an
ordinary routine that this extension's tools compose.

It starts no thread or service of its own. Bounded reconciliation and diagnostic-log expiry ride on the one existing background
service's ``before_tick`` through :meth:`Automation.background_tick`; every write the extension makes (bridge steps, waits, failure
records, maintenance) is one short transaction inside the existing writer-admission fence and the maintenance gate
(:meth:`Automation.writing`), never held across a script, a child process or a provider call.

Configuration (``qorqut_automation.config``): optional ``python``, ``state_dir``, ``invocation_seconds``, ``max_run_seconds``,
``max_calls``, ``max_step_bytes``. Not an operating-system sandbox: a script keeps the host user's OS access; what the extension limits
is its *host authority*."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path

from qorqut_agentic.runtime import LocalProcessRuntime

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.history_exports import PRIVATE_EXCLUDED, TECHNICAL_EXCLUDED, HistoryContribution, TableClassification

from bazarlyk.extensions import Extension

from . import operations, program, reference, schema
from .config import from_config
from .guard import Guard
from .maintenance import Maintenance
from .records import SCRIPT_PACKAGE

ID = "automation"
STATUSES = {"automation_run_pending": 409, "automation_paused": 409, "automation_archived": 409, "step_key_conflict": 409,
            "automation_forbidden_operation": 403, "automation_restricted": 403, "automation_scope": 403,
            "automation_capability_not_granted": 403, "automation_action_signoff_unsupported": 403,
            "automation_approval_unsupported": 403, "automation_not_active": 409, "management_unavailable": 409,
            "script_unavailable": 409, "script_changed": 409, "admission_missing": 409}
HISTORY = (
    TableClassification("automations", PRIVATE_EXCLUDED,
                        "Grants, restrictions, the saved cursor and the frozen admission are private operating state; authored scripts "
                        "live in the plugin area's own history."),
    TableClassification("automation_steps", TECHNICAL_EXCLUDED,
                        "Per-attempt request bindings, wait bindings and results are run evidence kept in protected recovery storage."))


def plugin_root() -> Path:
    """The bundled Agentic plugin package (skills, template and manifest) that ships inside this distribution's package data."""
    return Path(__file__).with_name("plugins") / SCRIPT_PACKAGE


class Automation(Extension):
    id = ID

    def __init__(self, config):
        self.config = config
        self.references = reference.AutomationReferences(None)
        self.runtime = self.guard = self.operations = self.program = self.maintenance = self.state_root = None
        self.live = {}              # automation id -> (run id, bridge thread, bridge) of an invocation that has not ended yet
        self._runtimes = {}

    # -- declarations ----------------------------------------------------------------------------------------------
    def storage(self):
        return (schema.storage_contribution(),)

    def catalog(self):
        return (CatalogContribution(ID, entries=operations.catalog_entries()),)

    def history(self):
        return (HistoryContribution(ID, classifications=HISTORY),)

    def sources(self):
        return (reference.source(self.references),)

    def error_statuses(self):
        return dict(STATUSES)

    # -- runtime wiring --------------------------------------------------------------------------------------------
    def install(self, runtime):
        self.runtime = runtime
        paths = getattr(runtime, "paths", None)
        root = Path(self.config.state_dir) if self.config.state_dir else Path(getattr(paths, "root", None) or runtime.root) / "automation"
        self.state_root = root
        self.guard = Guard(runtime)
        self.operations = operations.AutomationOperations(runtime, self)
        self.operations.register(runtime.registry)
        self.program = program.register(runtime.programs, self)
        runtime.policy.bind_automation_gate(self.guard.gate, self.guard.capabilities)
        self.references.runtime = runtime
        runtime.application.automations = self.references
        self.maintenance = Maintenance(self)

    def background_tick(self):
        """The bounded callable the one background service runs before each tick (its ``before_tick``)."""
        return self.maintenance.tick if self.maintenance is not None else None

    @contextmanager
    def writing(self):
        """One short, writer-admitted write unit: the existing maintenance fence, then one transaction. Never hold it across a
        child process, a provider call or file staging."""
        app = self.runtime.application
        admission = getattr(app, "writer_admission", None)
        with (admission.writer() if admission is not None else nullcontext()):
            with self.runtime.database.transaction() as uow:
                app.ensure_writes_open(uow)
                yield uow

    def process_runtime(self, alias):
        """The child-process runtime for one script runtime alias: an empty environment, bounded output, no inherited secrets."""
        runtime = self._runtimes.get(alias)
        if runtime is None:
            runtime = self._runtimes[alias] = LocalProcessRuntime(
                {alias: self.config.python}, env={}, max_timeout_seconds=self.config.invocation_seconds,
                max_output_bytes=64 * 1024 * 1024, max_stderr_bytes=1024 * 1024)
        return runtime


def create(config=None, **injected):
    """Validate the configuration and return the declaration (a startup selection passes no injected values)."""
    return Automation(from_config(config))
