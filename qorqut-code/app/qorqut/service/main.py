"""Explicit service assembly. Importing this module never launches work."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import threading

from qorqut.background.providers import BackgroundModelClient, ProviderConfig
from qorqut_operations.contracts import DomainError
from qorqut.transport.auth import Authentication
from qorqut.transport.compatibility import DashboardCompatibility
from qorqut.transport.managed_launch import ManagedLaunch
from qorqut.managed_runtime import mode as runtime_mode
from .local import LocalFiles
from .instance_arguments import add_instance_arguments, instance_paths_from_arguments
from .runtime import build_runtime
from .web import create_app

_DEFAULT_MAINTENANCE = object()


def background_model_factory(local_files, defaults=None, *, client_factory=None):
    """Compose project model clients from the trusted defaults, Global settings and the project's own overrides."""
    defaults = dict(defaults or {})
    client_factory = BackgroundModelClient if client_factory is None else client_factory

    def resolve(project_id):
        trusted = {**defaults, **local_files.environment()}
        own = {row["key"]: row["value"] for row in local_files.environment_pairs(project_id)} if project_id else {}
        return client_factory(ProviderConfig.from_environment({**trusted, **own}))
    return resolve


def transcript_capture_configuration(value=None):
    if value is None:
        value = os.environ.get("QORQUT_CAPTURE_TRANSCRIPTS", os.environ.get("BAZARLYK_CAPTURE_TRANSCRIPTS", "true")).strip().lower()
        if value in {"true", "false"}:
            value = value == "true"
    if type(value) is not bool:
        raise DomainError("invalid_configuration", "QORQUT_CAPTURE_TRANSCRIPTS must be true or false")
    return value


class TerminalBroker:
    """The dashboard's tmux broker (``pty_broker.py``), loaded the first time a terminal is used.

    Composing the service in managed mode, or on a computer without tmux, must not load it, start a process or write a file; the
    module binds the tmux program it finds when it is loaded, so a tmux installed while the service runs is looked for again
    each time a call finds none."""

    def __init__(self, paths, load=None):
        self._paths, self._load, self._module, self._lock = paths, load, None, threading.Lock()

    def module(self):
        with self._lock:
            if self._module is None:
                try:
                    self._module = (self._load or load_terminal_broker)(self._paths)
                except (OSError, ImportError, AttributeError) as error:
                    raise DomainError("operation_unavailable", "Agent terminals are not available on this installation") from error
            if getattr(self._module, "TMUX", True) is None:
                from qorqut.terminal_support import find_tmux
                self._module.TMUX = find_tmux()
            return self._module

    def __getattr__(self, name):
        if name.startswith("__") or name in {"_paths", "_load", "_module", "_lock"}:
            raise AttributeError(name)
        return getattr(self.module(), name)


def load_terminal_broker(paths):
    from qorqut.installation import component
    path = component(paths, 'dashboard') / 'pty_broker.py'
    spec = importlib.util.spec_from_file_location("qorqut_dashboard_pty_broker", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def compose_terminals(runtime, *, paths, endpoint, local, shell, broker, capture_transcripts, terminal_workspace, execution_provider):
    """The terminal side of the service, composed in every mode as at DUAL: it starts no process, writes no file and needs no
    tmux. Whether it may be used is decided at call time by the installation's mode (``TerminalService.require_terminals``)."""
    from qorqut.transport.agent_terminal_control import AgentTerminalBackend
    from qorqut.transport.terminals import TerminalService
    terminals = TerminalService(runtime, broker or TerminalBroker(paths), service_url=endpoint, shell=shell,
        environment=local.environment, global_environment=local.environment, agent_environment=local.agent_environment,
        capture_transcripts=capture_transcripts, workspace=terminal_workspace, execution_provider=execution_provider)
    # The native binding adapter and Ordubashy's terminal tools reach the terminals through these two.
    runtime.terminals = terminals
    runtime.terminal_control.backend = AgentTerminalBackend(terminals)
    return terminals


def recover_switch(runtime):
    """At start, after the managed runtime and the terminal side exist. A failure here is logged and never stops the service."""
    switch = getattr(runtime, "runtime_switch", None)
    if switch is None:
        return
    try:
        switch.recover()
    except Exception:  # noqa: BLE001 - recovery of an interrupted switch must not keep the service from starting
        logging.getLogger(__name__).exception("Recovering an interrupted switch of how agents run failed")


def assemble(workspace, *, paths, origins, endpoint, bootstrap_token="", shell="/bin/bash",
             model_client_factory=None, broker=None, database=None, signing_key=None,
             nightly_maintenance=_DEFAULT_MAINTENANCE, capture_transcripts=None, terminal_workspace=None,
             execution_provider=None, recovery_config=None,
             context=None, extensions=None):
    from qorqut.background.integration import attach_background
    from qorqut_operations.background.programs import ProgramRegistry

    capture_transcripts = transcript_capture_configuration(capture_transcripts)
    programs = ProgramRegistry()
    runtime = build_runtime(workspace, paths=paths, database=database, job_programs=programs, context=context,
                            extensions=extensions)
    if nightly_maintenance is _DEFAULT_MAINTENANCE:
        from qorqut.storage.versioning import NightlyMaintenance
        nightly_maintenance = NightlyMaintenance(runtime.database, runtime.paths, locks=runtime.locks,
                                                 extensions=runtime.selection, git_enabled=True)
    local = LocalFiles(runtime)
    from qorqut_context.integration.reading import PublishedContextText
    published_context = PublishedContextText(runtime.domains.shared_context)
    runtime.resolver.context_home = published_context.home_text
    runtime.owner_bootstrap.context_home = published_context.home_text
    runtime.owner_bootstrap.context_status = published_context.status
    from qorqut_operations.storage.quiescence import KnownZeroWriters
    from qorqut.storage.host_writers import ManagedHostWriters
    from qorqut.browser.access import BrowserAccess
    runtime.browser_access = BrowserAccess(runtime)
    # Browser lease mutations participate in shared admission; the browser class has no separate writer service.
    runtime.recovery_quiescer.browser_writers = KnownZeroWriters("browser")
    # A live managed agent host (a headless client session that outlives the service) refuses the window; it never closes or
    # kills one. In Terminals mode the Qorqut tmux socket is read as well; the mode is asked at each window, and managed mode
    # never runs tmux.
    from qorqut.managed_runtime.state import state_root
    from qorqut.storage.host_writers import HostTerminalWriters, ModeHostWriters
    from qorqut.terminal_support import find_tmux
    runtime.recovery_quiescer.host_writers = ModeHostWriters(
        ManagedHostWriters(lambda: state_root(runtime.paths)), HostTerminalWriters(find_tmux), runtime.paths)
    terminals = compose_terminals(runtime, paths=paths, endpoint=endpoint, local=local, shell=shell, broker=broker,
                                  capture_transcripts=capture_transcripts, terminal_workspace=terminal_workspace,
                                  execution_provider=execution_provider)
    launch = ManagedLaunch(runtime, service_url=endpoint,
        environment=local.environment, global_environment=local.environment,
        agent_environment=local.agent_environment, capture_transcripts=capture_transcripts)
    # The managed runtime prepares every session it opens through this service.
    runtime.managed_launch = launch
    from qorqut.inbox.broadcasts import BroadcastDispatcher
    runtime.broadcast_dispatcher = BroadcastDispatcher(runtime, runtime.broadcasts, launch)
    if model_client_factory is None:
        service_provider = {key: os.environ[key] for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "QORQUT_BACKGROUND_MODEL") if key in os.environ}
        model_client_factory = background_model_factory(local, service_provider)

    from qorqut.background.backups import BackupDownloader
    attach_background(runtime, programs, model_client_factory=model_client_factory, transcript_source=local.iter_transcripts,
        maintenance=nightly_maintenance,
        backup_downloader=BackupDownloader(local.environment))
    if nightly_maintenance is not None:
        runtime.maintenance_capture = nightly_maintenance.bind_runtime(runtime)
    if recovery_config is not None:
        from qorqut.storage.recovery_admin import RecoveryAdminConfig, RecoveryAdministration
        selected = RecoveryAdminConfig.load(recovery_config, paths=paths)
        runtime.recovery_admin = RecoveryAdministration(runtime.database, selected,
            lambda: runtime.recovery_quiescence(timeout=60), extensions=runtime.selection,
            background_worker=runtime.background)
    # The managed runtime and the terminal side both exist now: finish what a service that died mid-switch left behind.
    recover_switch(runtime)
    compatibility = DashboardCompatibility(runtime, terminals=terminals, background=runtime.background, local_files=local)
    authentication = Authentication(runtime.database, runtime.paths, bootstrap_token=bootstrap_token, signing_key=signing_key,
                                    integration_adapter=runtime.selection.integration_adapter)
    from qorqut.installation import component
    # Trusted registration is complete: nothing can declare or install an operation once traffic is served.
    runtime.freeze_registry()
    return create_app(runtime, dashboard_root=component(paths, 'dashboard'), authentication=authentication, origins=origins,
        compatibility=compatibility, terminals=terminals)


def terminals_selected(args) -> bool:
    """Whether this installation's setting says Terminals right now. Anything that cannot be read is managed."""
    try:
        return runtime_mode.is_terminal(instance_paths_from_arguments(args, required=("app", "dashboard")))
    except (DomainError, OSError, ValueError):
        return False


def terminal_shell():
    """The owner's login shell for terminals, or bash when it is one a terminal does not start."""
    shell = os.environ.get("SHELL", "/bin/bash")
    return shell if Path(shell).name in {"bash", "sh", "dash", "zsh", "fish"} else "/bin/bash"


def server_class():
    """The service's ``uvicorn.Server``: a stop signal first tells open event streams to finish, and ``stop`` is the same
    stop asked for from inside (``service/restart.py``) with no signal recorded, so the process is not ended by a signal
    raised again after serving and ``main`` returns the restart code."""
    import uvicorn
    from qorqut.transport.http import STOPPING

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # Open event streams (a dashboard tab) never end by themselves: tell them to finish before the drain starts.
            STOPPING.set()
            super().handle_exit(sig, frame)

        def stop(self):
            STOPPING.set()
            self.should_exit = True
    return Server


def main(argv=None):
    parser = argparse.ArgumentParser(description=instruction_text('main-shared-qorqut-dashboard-and-http-mcp-service'))
    parser.add_argument("--workspace", type=Path, default=None)
    add_instance_arguments(parser)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--host", default=os.environ.get("QORQUT_DASH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QORQUT_DASH_PORT", "1049")))
    parser.add_argument("--origin", action="append", default=[])
    parser.add_argument("--service-url", default=os.environ.get("QORQUT_MCP_URL", os.environ.get("BAZARLYK_MCP_URL")))
    parser.add_argument("--token", default=os.environ.get("QORQUT_DASH_TOKEN", ""))
    parser.add_argument("--token-file", type=Path, help="Private (0600) file holding the owner bootstrap token, instead of --token")
    parser.add_argument("--extensions-config", type=Path, help="JSON file of the trusted extension selection "
                        "({\"extensions\": [...]}); without it the process's own installation configuration decides")
    parser.add_argument("--prepare-agent", nargs=2, metavar=("PROJECT_SLUG", "AGENT"))
    parser.add_argument("--claude-version")
    parser.add_argument("--codex-version")
    parser.add_argument("--recovery-config", type=Path, help="Private external host configuration for local encrypted recovery")
    parser.add_argument("--context-config", type=Path, help="Explicit JSON file of Context RunnerConfig fields (model, effort, limits); "
                        "the library defaults apply without it")
    # The owner's own Workspace shell opens here in Terminals mode. While agents run as managed sessions there are no terminals, so
    # the option has no effect; an earlier version's start command still passes it, and that must not end the service at start.
    parser.add_argument("--terminal-workspace", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.terminal_workspace is not None and not terminals_selected(args):
        logging.getLogger(__name__).warning("Ignoring --terminal-workspace: it has no effect while agents run as managed sessions. "
                                            "Remove it from this start command.")
    try:
        if args.workspace is None:
            raise DomainError("invalid_configuration", "Set the explicit engine workspace with --workspace")
        paths = instance_paths_from_arguments(args, required=("app", "dashboard"))
        # The one operational database of the instance; the engine validates its stored target identity and exact schema.
        from qorqut.composition.assembly import host_assembly, open_database
        from qorqut.composition.extension_tables import recorded
        from qorqut.extensions import installed_selection
        from qorqut.installation import extensions_configuration
        from qorqut.service.changes import ChangeFeed
        from qorqut.private_files import read_private_file
        # A pending extension change (install, update or removal) is applied here, before the selection is read.
        from qorqut.bootstrap.extension_change import apply_pending
        apply_pending(paths)
        # The owner's startup selection of installed extensions is fixed for this process; the store must match it.
        selection = installed_selection() if args.extensions_config is None else installed_selection(extensions_configuration(args.extensions_config))
        token = args.token
        if args.token_file is not None:
            if token:
                raise DomainError("invalid_configuration", "Give the owner token once: --token-file replaces --token and QORQUT_DASH_TOKEN")
            token = read_private_file(args.token_file, max_bytes=512).decode().strip()
            if not token:
                raise DomainError("invalid_configuration", "The token file is empty")
        # The store's own record of extension tables is part of the composition it is opened with.
        database = open_database(paths, host_assembly(ChangeFeed(), selection, tables=recorded(paths.database)))
        if args.project:
            with database.read() as uow:
                registered = {str(Path(row["workspace"]).resolve()) for row in uow.repository("projects").list()}
            if any(str(Path(path).expanduser().resolve()) not in registered for path in args.project):
                raise DomainError("invalid_configuration", "--project accepts registered project folders only; it does not import them")
        local_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        if ":" in local_host:
            local_host = "[" + local_host.strip("[]") + "]"
        origins = args.origin + [value for value in os.environ.get("QORQUT_SERVICE_ORIGINS", "").split(",") if value]
        origins += [f"http://{local_host}:{args.port}", f"http://localhost:{args.port}"]
        endpoint = args.service_url or f"http://{local_host}:{args.port}/mcp"
        context_settings = None
        if args.context_config is not None:
            from qorqut_context.integration.service import ContextSettings
            from qorqut.shared_context.service import runner_config_from_file
            context_settings = ContextSettings(config=runner_config_from_file(args.context_config))
        app = assemble(args.workspace, paths=paths, origins=origins, endpoint=endpoint, bootstrap_token=token,
            shell=terminal_shell(), database=database,
            terminal_workspace=args.terminal_workspace or os.environ.get("QORQUT_TERMINAL_WORKSPACE") or None,
            recovery_config=args.recovery_config, context=context_settings, extensions=selection)
        if args.prepare_agent:
            # Preparing an agent writes its bootstrap material, so only the backend that owns the instance may do it:
            # ownership, target validation and mutation admission for the preparation alone, and no service is started.
            with app.state.runtime.owned_preparation():
                runtime, slug, selector = app.state.runtime, *args.prepare_agent
                with database.read() as uow:
                    actor = app.state.authentication.installation_owner(connection=uow.connection)[0]
                from qorqut.transport.auth import AuthenticatedPrincipal
                context, project = app.state.http_api.context(AuthenticatedPrincipal(actor), slug)
                profile, _ = runtime.managed_launch.agent(context, selector)
                versions = ({"claude-code": args.claude_version or "", "codex": args.codex_version or ""}
                            if args.claude_version or args.codex_version else None)
                result = runtime.managed_launch.prepare(profile.agent_id, versions=versions)
                print(json.dumps(result, sort_keys=True))
                return 0
        import uvicorn
        server = server_class()(uvicorn.Config(app, host=args.host, port=args.port, proxy_headers=False, server_header=False, access_log=False,
            timeout_graceful_shutdown=10, limit_concurrency=200, log_level="info"))
        from qorqut.service import restart
        restart.attach(server)
        server.run()
        if not server.started:
            return 3
        return restart.EXIT_RESTART if restart.requested() else 0
    except (DomainError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("Service configuration rejected: %s", exc)
        return 1
