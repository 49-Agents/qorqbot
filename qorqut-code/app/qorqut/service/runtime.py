"""Build one registry, policy resolver, and durable application for every adapter."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
import threading

from qorqut.agents.operations import AgentOperations
from qorqut.agents.resolver import AgentConfigResolver
from qorqut.catalog.operations import CatalogOperations
from qorqut.catalog.packages import Catalog
from qorqut.catalog.sources import load_sources
from qorqut.composition.assembly import host_assembly, open_database, require_additive_registrations, require_selected_storage
from qorqut.composition.catalog import builtin_registry, install_contribution
from qorqut.composition.delegation import (HOST_DELEGATED_CREATION_GROUPS, HOST_REVIEW_EXCEPTIONS,
                                             HostDelegationProvider)
from qorqut.composition.files import FileTargetResolver, lock_paths
from qorqut.composition.policy import HOST_POLICY_RULES
from qorqut.composition.transactions import require_change_feed
from qorqut.extensions import NO_EXTENSIONS, ExtensionSet, arrange, installed_selection
from qorqut.instance_paths import InstancePaths
from qorqut_operations.background.authority import BackgroundAuthority
from qorqut_operations.storage.quiescence import BoundedWriter, KnownZeroWriters, RecoveryQuiescer, worker_alive
from qorqut.signoffs.ports import SignoffDomains, signoff_ports
from qorqut_operations.signoffs import SignoffService
from . import stopping
from .lifetime import LifetimeState, ServiceLifetime, ServiceStep
from .ownership import InstanceOwnership
from qorqut.transport.auth import human_global_capabilities
from qorqut_operations.application.engine import Application
from qorqut_operations.contracts import DomainError
from qorqut_operations.policy import PolicyResolver
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from qorqut_operations.storage.executor import PreparedFileExecutor
from qorqut_operations.storage.files import FileApplier


# The writer classes the host's recovery barrier must account for beyond the engine's own (background, Signoffs and
# browser): each has a real controller, or a zero-writer inventory verified against the composition,
# or the barrier refuses. ``context`` and ``history`` are required
# by contract: a composition that lacks them is incomplete, never an empty class. A selected extension's services are
# further named writers (``Runtime.extension_writers``) and required while the extension is selected; an absent
# extension adds no required writer.
RECOVERY_WRITERS = ("native_chat", "inbox_nudges", "activity", "incidents", "terminal_control", "broadcast_dispatcher", "changes",
                    "context", "history")
RECOVERY_REQUIRED = RECOVERY_WRITERS
# The runtime attribute behind a lifecycle step whose name differs from it.
STEP_ATTRIBUTES = {"context": "context_services", "history": "history_observer"}


@dataclass
class Runtime:
    database: object
    root: Path
    workspace: Path
    registry: object
    resolver: object
    policy: object
    application: object
    agents: object
    catalog: object
    publisher: object
    domains: object
    file_applier: object
    signoffs: object
    locks: object = None  # the instance's explicit LockPaths
    background: object = None
    programs: object = None  # the one ProgramRegistry; frozen with the operation registry before traffic
    authority: object = None  # the one BackgroundAuthority bound to it
    signoff_domains: object = None  # the retained domains the Signoff ports resolve
    agentic_layer: object = None
    agentic_view: object = None
    agentic_inbox_bridge: object = None
    overseer_retention: object = None
    overseer: object = None
    inbox: object = None
    recovery_quiescer: object = None
    browser_access: object = None
    browser_manager: object = None
    browser_turns: object = None
    recovery_admin: object = None
    maintenance_capture: object = None  # the nightly capture orchestrator, bound once by service composition
    focus: object = None
    activity: object = None
    broadcasts: object = None
    broadcast_dispatcher: object = None
    terminal_control: object = None
    terminals: object = None  # The assembled TerminalService; installed once by service composition.
    background_operations: object = None
    checkpoints: object = None
    management: object = None
    incidents: object = None
    private_conversations: object = None
    calendar: object = None
    owner_bootstrap: object = None
    workspaces: object = None
    changes: object = None
    delegation: object = None
    delegation_operations: object = None
    native_chat: object = None
    inbox_nudges: object = None
    reminders: object = None  # the shared host reminder service (also application.reminders)
    selection: ExtensionSet = NO_EXTENSIONS  # the owner's startup selection; fixed for the process
    extensions: object = MappingProxyType({})  # extension id -> installed Extension instance (one generic mapping)
    context_services: object = None  # the one Context lifecycle owner: inert until the host lifetime starts it
    history: object = None
    history_observer: object = None
    paths: InstancePaths | None = None  # the explicit component locations this runtime was assembled from
    running: set = field(default_factory=set, repr=False)  # services a completed start left running
    ownership: object = None  # the instance's one exclusive lifetime owner (service.ownership); taken by start()
    lifetime_state: object = field(default=None, repr=False)  # cached: ownership, incomplete services and phase
    recovery_controllers: dict = field(default_factory=dict, repr=False)  # name -> (service, BoundedWriter), kept across windows
    recovery_guard: object = field(default_factory=threading.Lock, init=False, repr=False)  # one recovery window at a time

    def observed_agent_call(self, context, component, operation, call, *, readiness=False):
        if self.incidents is None:
            return call()
        return self.incidents.reporter.boundary(context, component, operation, call, readiness=readiness)

    def agent_context(self, principal, *, diagnostic=False, **options):
        """Observe context failures before an operation can reach its dispatcher.

        Only the authenticated identity is known if resolution fails. Do not
        attribute an unvalidated business selector or guess the agent's task.
        """
        from qorqut_operations.contracts import InvocationContext
        context = InvocationContext(principal)
        method = self.resolver.runtime_context if diagnostic else self.resolver.invocation_context
        return self.observed_agent_call(context, 'agent.context',
            'identity' if diagnostic else 'configuration', lambda: method(principal, **options))

    @contextmanager
    def recovery_quiescence(self, **kwargs):
        """Trusted recovery caller entrypoint; no boolean quiescence assertion. It yields the barrier's active proof
        (``RecoveryBarrierProof``).

        Only the backend that owns the instance may enter: a window drains and freezes this process's writers and takes
        the installation lock, which a process that does not own the instance must not do. Everything happens on entry,
        not when the context manager is obtained, so one obtained before a later stop is judged by the ownership of the
        moment it is entered. One window at a time: a concurrent or recursive attempt is refused, before the inventory is
        touched or anything is refused, drained or frozen, and the guard is held until the proof is revoked and the thaw
        and restart have finished. The writer inventory is re-derived from the composition as it is now, so a service
        installed after assembly joins it."""
        if self.recovery_quiescer is None:
            raise DomainError("recovery_not_quiescent", "Recovery writer barrier is not installed")
        if not self.recovery_guard.acquire(blocking=False):
            raise DomainError("recovery_not_quiescent", "Another recovery window is in progress", retryable=True)
        try:
            if self.ownership is None or not self.ownership.held:
                raise DomainError("recovery_not_quiescent", "Recovery requires this backend to own the instance")
            self.ownership.verify()
            self.sync_recovery_writers()
            with self.recovery_quiescer.quiesce(**kwargs) as proof:
                yield proof
        finally:
            self.recovery_guard.release()

    def sync_recovery_writers(self):
        """Complete the barrier's inventory from the lifetime steps: every writer class has a controller over the very
        stop adapter and start the lifetime uses, or (for a class whose service is optional and absent) a zero-writer
        inventory re-verified when the barrier runs. A required class that is not composed gets nothing, so the
        barrier refuses; a missing attribute is never read as an empty class. Controllers are kept across windows so
        what was running before a failed one is still resumed by a later one."""
        lifetime = self.lifetime()
        steps = {step.name: step for step in lifetime.steps}
        writers = self.recovery_quiescer.writers
        # The barrier drains in the lifetime's own stop order (producers of external effects first) and resumes in reverse.
        mine = (*RECOVERY_WRITERS, *self.extension_writers())
        for name in [*writers]:
            if name in mine:
                del writers[name]
        for name in (name for name in lifetime.stop_order if name in mine):
            step = steps[name]
            if step.installed:
                cached = self.recovery_controllers.get(name)
                if cached is None or cached[0] is not step.service:
                    controller = BoundedWriter(step.service, name, stop=lambda writer, timeout, step=step: step.drain(timeout),
                                               running=step.running or worker_alive, start=lambda writer, step=step: step.start())
                    cached = self.recovery_controllers[name] = (step.service, controller)
                writers[name] = cached[1]
            elif step.required:
                self.recovery_controllers.pop(name, None)
            else:
                attribute = STEP_ATTRIBUTES.get(name, name)
                writers[name] = KnownZeroWriters(name, verify=lambda attribute=attribute: getattr(self, attribute, None) is None)
        return self.recovery_quiescer

    def extension_writers(self):
        """The lifecycle names of the selected extensions' services: required recovery writers of the barrier."""
        return tuple(declaration.name for extension in self.selection.extensions for declaration in extension.services())

    def register_recovery_writers(self, *, host_writers, browser_writers=None):
        """Install trusted controller ports; caller-provided booleans are rejected."""
        if self.recovery_quiescer is None:
            raise DomainError("recovery_not_quiescent", "Recovery writer barrier is not installed")
        if host_writers is None or not callable(getattr(host_writers, "inventory_and_freeze", None)):
            raise TypeError("Recovery requires a concrete legacy-host inventory controller")
        if browser_writers is None or not callable(getattr(browser_writers, "drain", None)):
            raise TypeError("Recovery writers must implement bounded drain, including known-zero inventories")
        self.recovery_quiescer.host_writers = host_writers
        self.recovery_quiescer.browser_writers = browser_writers

    def require_authority(self):
        """Per-use gate for operational access: this is the instance's one database, and writes are not paused.

        The engine validated the stored target identity and the actual schema when the store was opened
        (``validate_target`` repeats that at start); there is no migration marker to consult."""
        if self.paths is None or self.database.path != self.paths.database:
            raise DomainError("authority_invalid", "The operational database is not the selected instance database")
        with self.database.read() as uow:
            state = uow.repository("maintenance_state").get("service")
            if state and state["state"] != "open":
                raise DomainError("maintenance", "Operational access is paused for maintenance", retryable=True)

    def validate_target(self):
        """Re-validate the stored target (identity and exact schema inventory) before serving, then the gate above."""
        self.database.open_existing()
        self.require_authority()

    def freeze_registry(self):
        """End trusted registration: no operation can be declared or installed and no program registered once the
        runtime serves traffic."""
        self.registry.freeze()
        if self.programs is not None:
            self.programs.freeze()
        return self

    def lifetime(self):
        """The one ordered owner of this runtime's services, built from what the assembly installed and sharing the
        runtime's cached ``LifetimeState`` (ownership, incomplete services, phase).

        ``Runtime.start`` and ``Runtime.stop`` are its only entrypoints. The start order is the retained required one
        (chat, activity, incidents, Signoffs, background, ...); the stop order is the retained drain order, which
        stops the owner-visible writers first and the Signoff writer last. An optional service
        that this composition does not install is recorded with its existing reason rather than silently omitted.
        The Context services (``context_services``) and the history observer (``history_observer``) are read with
        ``getattr`` and absent until composed; each stops only by its own typed evidence (``service.stopping``)."""
        def step(name, service, start=None, stop=None, absent=None, **options):
            return ServiceStep(name, service, (lambda: start(service)) if start else None, stop, absent, **options)

        def prepare_target():
            self.ownership.verify()
            self.validate_target()
            # Offline copies may use DELETE journals. Restore concurrent reads
            # before admission or any background writer, never during assembly.
            self.database.enable_wal()

        def prepare_browser_turns():
            # Expire browser turns before workers can reacquire a persisted queue. This is recovery bookkeeping
            # only; it never starts a model or watcher.
            if self.browser_turns:
                with self.database.transaction(operation="browser.turn.recover") as uow:
                    self.browser_turns.recover(uow)

        def thread_running(attribute):
            return lambda service: bool(getattr(service, attribute, None) and getattr(service, attribute).is_alive())

        stop_with_timeout = stopping.void(lambda x, timeout: x.stop(timeout=timeout))
        stop_without_timeout = stopping.void(lambda x, timeout: x.stop())

        def extension_step(declaration):
            return ServiceStep(declaration.name, declaration.service, lambda: (declaration.start or (lambda x: x.start()))(declaration.service),
                               declaration.stop or stop_with_timeout, None, declaration.timeout, True, "writer", declaration.running)
        steps = (
            step("native_chat", self.native_chat, lambda x: x.start(), stopping.void(lambda x, timeout: x.stop_observer(timeout=timeout)),
                 "no native chat", required=True, running=lambda x: worker_alive(x.backend)),
            # Advisory inbox notices to running native sessions; independent of any Chat view. It only sends through the
            # native input arbiter and never starts, resumes or stops an agent.
            step("inbox_nudges", self.inbox_nudges, lambda x: x.start(), stop_with_timeout, "no inbox nudges", required=True),
            step("activity", self.activity, lambda x: x.start(), stop_without_timeout, "no agent activity", required=True),
            step("incidents", self.incidents, lambda x: x.start(), stop_with_timeout, "no incident drain", required=True),
            step("signoffs", self.signoffs, lambda x: x.start(), stop_with_timeout, required=True, recovery="engine"),
            step("background", self.background, lambda x: x.start(), stop_with_timeout, "background service is not attached",
                 recovery="engine", running=lambda x: x.is_running()),
            # The shared Context driver loop (lane B's ``runtime.context_services``): recovers mechanically on start and
            # reports ``{"stopped", "in_flight"}`` on stop; only a stop with nothing in flight is a stop.
            step("context", getattr(self, "context_services", None), lambda x: x.start(),
                 stopping.context_loop(lambda x, timeout: x.stop(timeout=timeout)), "the shared Context services are not composed",
                 required=True, running=lambda x: bool(x.running)),
            step("terminal_control", self.terminal_control, lambda x: x.start(), stop_with_timeout, "no terminal control",
                 timeout=35, required=True, running=thread_running("thread")),
            step("broadcast_dispatcher", self.broadcast_dispatcher, lambda x: x.start(), stop_with_timeout,
                 "no broadcast dispatcher", running=thread_running("thread")),
            # Retain the old ordering boundaries for installed extensions. These
            # absent steps neither install nor start a sandbox or preview host.
            step("sandbox", None, absent="project sandbox is not installed"),
            step("previews", None, absent="private previews are not installed"),
            # Do not stop/restart other writer services beneath a capture that is still completing its verified
            # thaw and maintenance restore: recovery administration drains before the services it quiesces (``stop_order``).
            # It is the barrier's own caller, so the barrier never drains it.
            step("recovery_admin", self.recovery_admin, lambda x: x.start(), stop_with_timeout,
                 "no private recovery configuration", recovery="caller"),
            step("changes", self.changes, lambda x: x.start_external_writer_ticker(self.database.dedicated_connection),
                 stop_without_timeout, "no change feed", required=True, running=thread_running("_ticker_thread")),
            # The operational history observer (a later composition step): single worker, ``ObserverStop`` evidence.
            step("history", getattr(self, "history_observer", None), lambda x: self._start_history(),
                 stopping.history_observer(lambda x, timeout: x.stop(timeout)), "operational history is not composed",
                 required=True, running=lambda x: x.state().started and not x.state().stopping),
            # The nightly maintenance capture runs outside the background worker the barrier drains: it enters the barrier
            # from its own thread, so like recovery administration it is a caller the barrier never joins, and it reports
            # ``{"stopped", "in_flight"}`` on stop. Started last, it is unwound first.
            step("maintenance_capture", self.maintenance_capture, lambda x: x.start(),
                 stopping.capture_service(lambda x, timeout: x.stop(timeout=timeout)), "nightly capture is not bound",
                 recovery="caller", running=lambda x: bool(x.running)))
        # The recovery orchestrators drain first: an in-flight capture or thaw owns the services and resources below it.
        stop_order = ("recovery_admin", "maintenance_capture", "context", "history", "inbox_nudges",
                      "native_chat", "terminal_control", "broadcast_dispatcher", "activity", "incidents", "changes",
                      "previews", "sandbox", "background", "signoffs")
        # A selected extension's services join at the start-before and stop-before anchors they name. Each is a required
        # recovery writer: it drains and resumes through the same stop adapter and start the lifetime uses.
        steps, stop_order = arrange(steps, stop_order, (declaration for extension in self.selection.extensions
                                                        for declaration in extension.services()), extension_step)
        if self.lifetime_state is None:
            self.lifetime_state = LifetimeState(ownership=self.ownership, running=self.running)
        return ServiceLifetime(
            self.database, self.application.writer_admission, prepare_target,
            (prepare_browser_turns, lambda: self.application.file_executor.recover_pending(self.application)),
            steps, stop_order, self.lifetime_state)

    def start(self):
        """Take instance ownership, validate the target and maintenance state, admit mutations, reconcile prepared effects,
        then start the services in order. Mutation admission is refused from assembly until then, and again from the
        moment a start fails. A failed start unwinds only the services this attempt started, with proof, and keeps SQLite
        and ownership for any it cannot prove. Starting a running runtime verifies its ownership and does nothing else."""
        self.lifetime().start()
        return self

    def _start_history(self):
        from qorqut.operations_history.composition import start
        start(self)

    def own_instance(self):
        """The owned entry without services: ownership, then target validation, then mutation admission. For the CLI
        preparation and checks that need a writable instance; ``release_instance`` ends it."""
        self.lifetime().own()
        return self

    def release_instance(self):
        """End ``own_instance`` (no service was started): refuse mutations again and release ownership."""
        self.lifetime().release()

    @contextmanager
    def owned_preparation(self):
        """The explicitly owned CLI preparation path: no service is started, and the instance is given back."""
        self.own_instance()
        try:
            yield self
        finally:
            self.release_instance()

    def stop(self):
        """Refuse new work, drain the services, then seal admission and close SQLite and release ownership. Raises
        ``ShutdownIncomplete`` (database open, ownership kept) if any writer drain failed or could not be proved."""
        return self.lifetime().stop()


def build_runtime(workspace, *, paths, database=None, worker_profiles=None,
                  job_consequence_verifier=None, global_capabilities_provider=human_global_capabilities,
                  job_programs=None, register_domains=None, context=None, extensions=None):
    """Compose the retained host over the Operations engine for the instance named by ``paths``.

    The operational root is ``paths.operations`` and the database only ``paths.database``: an already initialised
    target store, validated (never created or converted) by the engine. ``workspace`` is the absolute owner-controlled
    protected configuration and package-source base, not a source, database, HQ or agent-workspace locator.
    The returned registry is still open for the service assembler's last trusted registrations; whoever serves
    traffic calls ``Runtime.freeze_registry`` first.

    The Context is composed here, once and inertly (``qorqut_context.integration.service`` through the host adapter
    ``qorqut.shared_context.service``): one queue, the filing and graph-edit ports over it, and one ``Context``/``Pipeline`` over the instance's Context locations. ``context`` is the optional
    ``ContextSettings`` (an injected runner and/or the library's ``RunnerConfig``); without it the library defaults apply.
    ``runtime.context_services`` is its lifecycle owner, which the host lifetime starts only once it owns the instance.

    ``extensions`` is the owner's startup ``ExtensionSet`` (``qorqut.extensions.select``); omitted, it is read from the
    trusted installation configuration. The selected storage is part of the target fingerprint, so a supplied ``database``
    must have been opened for exactly this selection. Every selected extension is installed once, here, on the still
    open registry: ``freeze_registry`` is the caller's."""
    from qorqut_operations.background.programs import ProgramRegistry

    if not isinstance(paths, InstancePaths):
        raise DomainError("invalid_configuration", "Assembly requires the explicit instance paths")
    from qorqut.bootstrap.extension_install import require_complete
    require_complete(paths)
    if not Path(workspace).is_absolute():
        raise DomainError("invalid_configuration", "The protected configuration workspace must be an absolute path")
    job_programs = job_programs if job_programs is not None else ProgramRegistry()
    root, workspace = paths.operations, Path(workspace).resolve()
    selection = installed_selection() if extensions is None else extensions
    if not isinstance(selection, ExtensionSet):
        raise DomainError("invalid_configuration", "Extensions are the owner's startup selection")
    from qorqut.agentic.installation import open_installation
    agentic_layer, agentic_view = open_installation(paths, selection.ids)
    if database is None:
        from qorqut.service.changes import ChangeFeed
        database = open_database(paths, host_assembly(ChangeFeed(), selection))
    else:
        if database.path != paths.database:
            raise DomainError("invalid_configuration", "The database is not the selected instance database")
        database = database.open_existing()
        require_selected_storage(database.assembly, selection)
    return compose_runtime(workspace, paths=paths, database=database, selection=selection, agentic_layer=agentic_layer,
                           agentic_view=agentic_view, registry=builtin_registry(selection), worker_profiles=worker_profiles,
                           job_consequence_verifier=job_consequence_verifier,
                           global_capabilities_provider=global_capabilities_provider, job_programs=job_programs,
                           register_domains=register_domains, context=context)


def compose_runtime(workspace, *, paths, database, selection, agentic_layer, agentic_view, registry, worker_profiles=None,
                    job_consequence_verifier=None, global_capabilities_provider=human_global_capabilities,
                    job_programs, register_domains=None, context=None):
    """The inert common composition body of ``build_runtime``: everything after the explicit inputs are validated and the
    store is opened. ``registry`` is the still open operation registry the host then registers every handler on; the
    ordinary runtime passes the strict target registry, and only the offline learning-removal preparation passes one
    that also carries metadata-only historical declarations. ``database`` is whatever store the caller opened for its own
    exact composition (``build_runtime`` validates it against the selected target; the offline preparation opens the
    exact reconstructed predecessor). Nothing here starts a service, takes ownership or admits a mutation."""
    root, workspace = paths.operations, Path(workspace).resolve()
    from qorqut.agents.terminal_control import validate_package as validate_terminal_package
    from qorqut.bootstrap.email_guidance import validate_package as validate_email_guidance_package
    core_validators = {"agent-terminal-control": validate_terminal_package, "email-management": validate_email_guidance_package}
    require_additive_registrations(database.assembly, selection, core_validators)
    changes = require_change_feed(database.assembly.change_publisher)
    locks = lock_paths(paths)
    catalog = Catalog(database, paths.runtime, registry)
    resolver = AgentConfigResolver(database, paths, registry, catalog=catalog, native_connections=selection.native_connections,
        plugin_handlers={**core_validators, **selection.package_validators})
    resolver.agentic_view = agentic_view
    from qorqut.agentic.access import AgenticAccess
    AgenticAccess(resolver, agentic_view).register(registry)
    if global_capabilities_provider is human_global_capabilities:
        from functools import partial
        global_capabilities_provider = partial(human_global_capabilities, additional=selection.global_capabilities)
    policy = PolicyResolver(registry, resolver, rules=HOST_POLICY_RULES, worker_profiles=worker_profiles,
        job_consequence_verifier=job_consequence_verifier, human_global_capabilities=global_capabilities_provider)
    installation_recovery_lock = InstallationRecoveryLock(locks.recovery)
    # Mutations are refused from assembly: only the owned startup path (ownership, then target validation) admits them.
    recovery_admission = RecoveryAdmission(installation_lock=installation_recovery_lock, serving=False)
    def environment_file(resource):
        from .environment_write import file_target
        return file_target(runtime, resource)

    # One ProgramRegistry and the one BackgroundAuthority that derives its worker identities and bounds their output.
    authority = BackgroundAuthority(database, job_programs)
    # File targets are resolved lazily: the document, environment, artifact and agent-bootstrap resolvers exist only
    # once the Application and its domains do, but the Signoff service applies through the sealed applier.
    file_targets = FileTargetResolver(lambda resource: domains.resolve_file_target(resource))
    file_targets.register("business_environment_files", lambda resource: environment_file(resource))
    file_targets.register("artifact_files", lambda resource: publisher.resolve_file_target(resource))
    file_targets.register("agent_bootstrap_files", lambda resource: agents.resolve_file_target(resource))
    file_applier = FileApplier(database, file_targets.seal(), locks=locks)
    # The Signoff service names itself the Application's stager and binds to it once. Its ports resolve the retained
    # management and Focus domains at call time; they are bound as soon as those exist (below).
    signoff_domains = SignoffDomains()
    signoffs = SignoffService(database, signoff_ports(signoff_domains, private_signoffs=selection.private_signoffs, outcomes=selection.signoff_outcomes), locks=locks, file_applier=file_applier)
    application = Application(database, registry, policy=policy, signoffs=signoffs,
                              context_resolver=policy.refresh_context, writer_admission=recovery_admission,
                              delegation=HostDelegationProvider(), review_exceptions=HOST_REVIEW_EXCEPTIONS,
                              delegated_creation_groups=HOST_DELEGATED_CREATION_GROUPS)
    signoffs.bind(application)
    application.file_executor = PreparedFileExecutor(database, file_applier, writer_admission=recovery_admission)
    install_contribution(registry, signoffs.contribution())
    agents = AgentOperations(resolver)
    agents.register(registry, application)
    from qorqut.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
    delegation = OperationalDelegation()
    application.operational_delegation = delegation
    delegation_operations = OperationalDelegationOperations(delegation).register(registry, application)
    publisher = CatalogOperations(catalog, sources=load_sources(workspace, paths=paths))
    publisher.register(registry, application)
    from qorqut.shared_context.service import ContextServices, compose_context
    composed = compose_context(application, paths, context)
    if register_domains is None:
        from qorqut.domains import register_business_domains
        register_domains = register_business_domains
    domains = register_domains(registry, application, paths, catalog=catalog, agent_resolver=resolver, job_programs=job_programs,
                               filing=composed.filing, graph_edits=composed.graph_edits, queue=composed.queue,
                               on_revision=composed.on_revision, extensions=selection)

    from qorqut.inbox.service import InboxService
    inbox = InboxService(application, domains, resolver=resolver, sources=selection.sources).register()
    delegation_operations.inbox = inbox
    from qorqut.management import ManagementService
    management = ManagementService(application, inbox=inbox).register()
    resolver.management = management
    from qorqut.browser.turns import BrowserTurnQueue
    runtime = Runtime(database, root, workspace, registry, resolver, policy, application,
                   agents, catalog, publisher, domains, file_applier, signoffs, inbox=inbox, management=management,
                   agentic_layer=agentic_layer, agentic_view=agentic_view,
                   programs=job_programs, authority=authority, signoff_domains=signoff_domains,
                   delegation=delegation, delegation_operations=delegation_operations, paths=paths, locks=locks,
                   browser_turns=BrowserTurnQueue(delegation=delegation),
                   ownership=InstanceOwnership(paths),
                   # The background slot is a zero-writer inventory only while no background service is attached (the
                   # check is made each time the barrier runs); attaching one replaces it with a real controller.
                   recovery_quiescer=RecoveryQuiescer(database, admission=recovery_admission,
                                                     background=KnownZeroWriters("background", verify=lambda: runtime.background is None),
                                                     signoffs=BoundedWriter(signoffs, "signoffs"),
                                                     installation_lock=installation_recovery_lock,
                                                     required=RECOVERY_REQUIRED),
                   selection=selection, extensions=selection.instances())
    from qorqut.browser.access import BrowserAccess
    runtime.browser_access = BrowserAccess(runtime)
    from qorqut.browser.operations import register_agent_browser_management, register_browser_operation
    register_browser_operation(runtime)
    register_agent_browser_management(runtime)
    from qorqut.browser.agent_browsers import AgentBrowserManager
    runtime.browser_manager = AgentBrowserManager(runtime)
    runtime.changes = changes
    from qorqut.operations_history.composition import bind as bind_history
    bind_history(runtime)
    runtime.context_services = ContextServices(composed, database, paths, programs=job_programs, reader=domains.shared_context,
                                               changes=changes)
    from qorqut.inbox.broadcasts import Broadcasts
    runtime.broadcasts = Broadcasts(inbox).register()
    from qorqut.agents.terminal_control import TerminalControl
    runtime.terminal_control = TerminalControl(runtime).register()
    from qorqut.native_chat.chat import NativeChat
    from qorqut.overseer.retention import Retention
    runtime.overseer_retention = Retention(runtime)
    runtime.native_chat = NativeChat.build(runtime)
    from qorqut.overseer.operations import OverseerOperations
    runtime.overseer = OverseerOperations(runtime).register()
    from qorqut.native_chat.nudges import NudgeBackend, delivery_view
    # The shared reminder service is registered (its operations, legacy wrappers, delivery adapter and runtime.reminders /
    # application.reminders) before the nudge pass can call its reconcile; it needs only the application and inbox; owner delivery uses a selected extension.
    from qorqut.reminders.service import ReminderService
    ReminderService(runtime).register()
    # Snooze expiry runs in the nudge pass's own write unit over the Task service's routing ports and this database: it
    # emits one durable event per due generation and responsibility, changes no Task, acknowledges nothing and starts no
    # agent. Without a Task service there are no snoozes to reconcile.
    tasks = getattr(domains, "tasks", None)
    from qorqut.overseer.due import reconcile as reconcile_due

    def reconcile_inbox(uow):
        # The one bounded inbox reconcile hook: the retained Task snoozes, the shared reminders' due boundaries and pending
        # deliveries, then the durable due-extraction membership and its ordinary inbox event in this same host transaction.
        # The reminder pass isolates its own failures with savepoints, so a bad reminder never blocks the rest.
        if tasks:
            tasks.reconcile_snoozes(uow)
        runtime.reminders.reconcile(uow)
        return reconcile_due(runtime, uow)

    runtime.inbox_nudges = NudgeBackend.build(runtime, inbox, reconcile=reconcile_inbox)
    from qorqut.agentic.inbox import attach_agentic_inbox
    attach_agentic_inbox(runtime, agentic_layer, inbox)
    inbox.delivery = lambda agent_id: delivery_view(runtime.inbox_nudges, agent_id)
    from qorqut.calendar.operations import CalendarOperations
    runtime.calendar = CalendarOperations(runtime, providers=selection.calendar_providers).register()

    from .local import LocalFiles
    from qorqut.inbox.focus import FocusService
    runtime.focus = FocusService(inbox, signoffs).register()
    from qorqut.native_chat.user_input import UserInput
    UserInput(runtime).register()
    # Retained Signoff domains are management routing and the Focus attention board. Selected connectors
    # supply their own private visibility and outcome callbacks through the explicit extension declarations.
    signoff_domains.bind(management=management, focus_board=runtime.focus.board).require()
    from qorqut.inbox.private_conversations import PrivateConversations
    runtime.private_conversations = PrivateConversations(inbox).register()
    # The six Project record operations: one existing Project's inventory, item reads and explicit cross-scope links.
    from qorqut.domains.projects import ProjectRecords
    runtime.projects = ProjectRecords(application, domains.items).register(registry)
    # Move to project: three operations over the engine's task relocation, plus the host adapter for private conversations,
    # Focus cards and unsupported bindings. The adapter is attached here, before the registry freezes.
    from qorqut.domains.tasks.moves import TaskMoves
    runtime.task_moves = TaskMoves(application, domains.tasks).register(registry)
    # The aggregate CRM activity read: the Domain's local activity merged with the directly linked sources, which are read
    # through the composed Task, Signoff, Action and document readers. It owns no storage and writes nothing.
    from qorqut.domains.crm import CRMActivityOperations
    runtime.crm_activity = CRMActivityOperations(application, domains.crm, domains.tasks, domains.items, signoffs).register(registry)
    from qorqut.workspaces import WorkspaceService
    runtime.workspaces = WorkspaceService(application, types=selection.workspace_types()).register(registry)
    from qorqut.owner_bootstrap import OwnerSessionBootstrap
    runtime.owner_bootstrap = OwnerSessionBootstrap(domains.documents).register(registry)
    from qorqut.agents.activity import AgentActivity
    runtime.activity = AgentActivity(runtime).register()
    resolver.activity = runtime.activity
    from qorqut.agents.checkpoints import AgentCheckpoints
    runtime.checkpoints = AgentCheckpoints(runtime)
    from qorqut.agents.attention import register_attention
    register_attention(runtime)
    from qorqut.agentic.attention import register_workflow_attention
    register_workflow_attention(runtime)
    from qorqut.agents.attention_delivery import attach_attention_runtime
    attach_attention_runtime(runtime)
    from qorqut.incidents import IncidentReporter, IncidentDrain, INCIDENT_PROGRAM_ID
    from qorqut_operations.background.programs import Program
    job_programs.register(Program(INCIDENT_PROGRAM_ID,1,{'type':'object','additionalProperties':False},
        None,unavailable_reason='Internal deterministic incident drain; not a schedulable agent job',
        scope_kinds=('global',)))
    reporter = IncidentReporter(paths.runtime / 'incidents')
    application.incident_reporter = reporter
    runtime.incidents = IncidentDrain(reporter,database,
        authority=authority,writer=runtime.activity.writer)
    runtime.focus.desktop_url = lambda: next((pair['value'] for pair in LocalFiles(runtime).environment_pairs(None)
        if pair['key'] == 'DESKTOP_CONTROL_URL'), None)
    from .environment import register_environment
    register_environment(runtime)
    from .environment_write import register_environment_write
    register_environment_write(runtime)
    from qorqut.background.integration import register_operations
    register_operations(runtime, job_programs, authority)
    from qorqut.agents.diagnostics import AgentDiagnostics
    AgentDiagnostics(runtime).register()
    # The selected extensions are installed once, on the open registry, before any caller freezes it or serves traffic.
    selection.install(runtime)
    runtime.recovery_quiescer.required = (*RECOVERY_REQUIRED, *runtime.extension_writers())
    return runtime
