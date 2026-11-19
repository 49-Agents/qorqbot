"""Explicit service assembly. Importing this module never launches work."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import argparse
import json
import logging
import os
from pathlib import Path

from qorqut.background.providers import BackgroundModelClient, ProviderConfig
from qorqut_operations.contracts import DomainError
from qorqut.transport.auth import Authentication
from qorqut.transport.compatibility import DashboardCompatibility
from qorqut.transport.managed_launch import ManagedLaunch
from .local import LocalFiles
from .instance_arguments import add_instance_arguments, instance_paths_from_arguments
from .runtime import build_runtime
from .web import create_app

_DEFAULT_MAINTENANCE = object()


def background_model_factory(local_files, defaults=None, *, client_factory=None):
    """Compose business model clients from the trusted defaults, Global settings and the project's own overrides."""
    defaults = dict(defaults or {})
    client_factory = BackgroundModelClient if client_factory is None else client_factory

    def resolve(business_id):
        trusted = {**defaults, **local_files.environment()}
        own = {row["key"]: row["value"] for row in local_files.environment_pairs(business_id)} if business_id else {}
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


def assemble(workspace, *, paths, origins, endpoint, bootstrap_token="",
             model_client_factory=None, database=None, signing_key=None,
             nightly_maintenance=_DEFAULT_MAINTENANCE, capture_transcripts=None, recovery_config=None,
             context=None, extensions=None):
    from qorqut.background.integration import attach_background
    from qorqut_operations.background.programs import ProgramRegistry

    capture_transcripts = transcript_capture_configuration(capture_transcripts)
    programs = ProgramRegistry()
    runtime = build_runtime(workspace, paths=paths, database=database, job_programs=programs, context=context,
                            extensions=extensions)
    if nightly_maintenance is _DEFAULT_MAINTENANCE:
        from qorqut.storage.versioning import NightlyMaintenance
        nightly_maintenance = NightlyMaintenance(runtime.database, runtime.paths, locks=runtime.locks, git_enabled=True)
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
    # kills one.
    from qorqut.managed_runtime.state import state_root
    runtime.recovery_quiescer.host_writers = ManagedHostWriters(lambda: state_root(runtime.paths))
    launch = ManagedLaunch(runtime, service_url=endpoint,
        environment=local.environment, global_environment=local.environment,
        agent_environment=local.agent_environment, capture_transcripts=capture_transcripts)
    # The managed runtime prepares every session it opens through this service.
    runtime.managed_launch = launch
    from qorqut.inbox.broadcasts import BroadcastDispatcher
    runtime.broadcast_dispatcher = BroadcastDispatcher(runtime, runtime.broadcasts, launch)
    if model_client_factory is None:
        service_provider = {key: os.environ[key] for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "BIZ_BACKGROUND_MODEL") if key in os.environ}
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
            lambda: runtime.recovery_quiescence(timeout=60), background_worker=runtime.background)
    # Agent sessions are not tied to a project, so an approved archive has nothing to reset.
    runtime.domains.archive.reset_terminals = lambda business, agents: None
    compatibility = DashboardCompatibility(runtime, background=runtime.background, local_files=local)
    authentication = Authentication(runtime.database, runtime.paths, bootstrap_token=bootstrap_token, signing_key=signing_key,
                                    integration_adapter=runtime.selection.integration_adapter)
    from qorqut.installation import component
    # Trusted registration is complete: nothing can declare or install an operation once traffic is served.
    runtime.freeze_registry()
    return create_app(runtime, dashboard_root=component(paths, 'dashboard'), authentication=authentication, origins=origins,
        compatibility=compatibility)


def main(argv=None):
    parser = argparse.ArgumentParser(description=instruction_text('main-shared-qorqut-dashboard-and-http-mcp-service'))
    parser.add_argument("--workspace", type=Path, default=None)
    add_instance_arguments(parser)
    parser.add_argument("--biz", action="append", default=[])
    parser.add_argument("--host", default=os.environ.get("BIZ_DASH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BIZ_DASH_PORT", "1049")))
    parser.add_argument("--origin", action="append", default=[])
    parser.add_argument("--service-url", default=os.environ.get("QORQUT_MCP_URL", os.environ.get("BAZARLYK_MCP_URL")))
    parser.add_argument("--token", default=os.environ.get("BIZ_DASH_TOKEN", ""))
    parser.add_argument("--token-file", type=Path, help="Private (0600) file holding the owner bootstrap token, instead of --token")
    parser.add_argument("--extensions-config", type=Path, help="JSON file of the trusted extension selection "
                        "({\"extensions\": [...]}); without it the process's own installation configuration decides")
    parser.add_argument("--prepare-agent", nargs=2, metavar=("BUSINESS_SLUG", "AGENT"))
    parser.add_argument("--claude-version")
    parser.add_argument("--codex-version")
    parser.add_argument("--recovery-config", type=Path, help="Private external host configuration for local encrypted recovery")
    parser.add_argument("--context-config", type=Path, help="Explicit JSON file of Context RunnerConfig fields (model, effort, limits); "
                        "the library defaults apply without it")
    # Removed with the agent terminals. An earlier version's start command (a hand-made service unit) still passes it, so it is
    # accepted and ignored, with one warning, instead of ending the service at start.
    parser.add_argument("--terminal-workspace", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.terminal_workspace is not None:
        logging.getLogger(__name__).warning("Ignoring --terminal-workspace: agents no longer run in terminals, so the option has no "
                                            "effect. Remove it from this start command.")
    try:
        if args.workspace is None:
            raise DomainError("invalid_configuration", "Set the explicit engine workspace with --workspace")
        paths = instance_paths_from_arguments(args, required=("app", "dashboard"))
        # The one operational database of the instance; the engine validates its stored target identity and exact schema.
        from qorqut.composition.assembly import host_assembly, open_database
        from qorqut.extensions import installed_selection
        from qorqut.installation import extensions_configuration
        from qorqut.service.changes import ChangeFeed
        from qorqut.private_files import read_private_file
        # The owner's startup selection of installed extensions is fixed for this process; the store must match it.
        selection = installed_selection() if args.extensions_config is None else installed_selection(extensions_configuration(args.extensions_config))
        token = args.token
        if args.token_file is not None:
            if token:
                raise DomainError("invalid_configuration", "Give the owner token once: --token-file replaces --token and BIZ_DASH_TOKEN")
            token = read_private_file(args.token_file, max_bytes=512).decode().strip()
            if not token:
                raise DomainError("invalid_configuration", "The token file is empty")
        database = open_database(paths, host_assembly(ChangeFeed(), selection))
        if args.biz:
            with database.read() as uow:
                registered = {str(Path(row["workspace"]).resolve()) for row in uow.repository("businesses").list()}
            if any(str(Path(path).expanduser().resolve()) not in registered for path in args.biz):
                raise DomainError("invalid_configuration", "--biz accepts registered project folders only; it does not import them")
        local_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        if ":" in local_host:
            local_host = "[" + local_host.strip("[]") + "]"
        origins = args.origin + [value for value in os.environ.get("BIZ_SERVICE_ORIGINS", "").split(",") if value]
        origins += [f"http://{local_host}:{args.port}", f"http://localhost:{args.port}"]
        endpoint = args.service_url or f"http://{local_host}:{args.port}/mcp"
        context_settings = None
        if args.context_config is not None:
            from qorqut_context.integration.service import ContextSettings
            from qorqut.shared_context.service import runner_config_from_file
            context_settings = ContextSettings(config=runner_config_from_file(args.context_config))
        app = assemble(args.workspace, paths=paths, origins=origins, endpoint=endpoint, bootstrap_token=token,
            database=database,
            recovery_config=args.recovery_config, context=context_settings, extensions=selection)
        if args.prepare_agent:
            # Preparing an agent writes its bootstrap material, so only the backend that owns the instance may do it:
            # ownership, target validation and mutation admission for the preparation alone, and no service is started.
            with app.state.runtime.owned_preparation():
                runtime, slug, selector = app.state.runtime, *args.prepare_agent
                with database.read() as uow:
                    actor = app.state.authentication.installation_owner(connection=uow.connection)[0]
                from qorqut.transport.auth import AuthenticatedPrincipal
                context, business = app.state.http_api.context(AuthenticatedPrincipal(actor), slug, owner=True)
                profile, _ = runtime.managed_launch.agent(context, selector)
                versions = ({"claude-code": args.claude_version or "", "codex": args.codex_version or ""}
                            if args.claude_version or args.codex_version else None)
                result = runtime.managed_launch.prepare(profile.agent_id, versions=versions)
                print(json.dumps(result, sort_keys=True))
                return 0
        import uvicorn
        from qorqut.transport.http import STOPPING

        class Server(uvicorn.Server):
            def handle_exit(self, sig, frame):
                # Open event streams (a dashboard tab) never end by themselves: tell them to finish before the drain starts.
                STOPPING.set()
                super().handle_exit(sig, frame)

        server = Server(uvicorn.Config(app, host=args.host, port=args.port, proxy_headers=False, server_header=False, access_log=False,
            timeout_graceful_shutdown=10, limit_concurrency=200, log_level="info"))
        server.run()
        if not server.started:
            return 3
        return 0
    except (DomainError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("Service configuration rejected: %s", exc)
        return 1
