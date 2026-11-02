"""Explicit service assembly. Importing this module never launches work."""

import argparse
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys

from bazarlyk.background.providers import BackgroundModelClient, ProviderConfig
from qorqut_operations.contracts import DomainError
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.compatibility import DashboardCompatibility
from bazarlyk.transport.terminals import TerminalService
from bazarlyk.sandbox.terminal import SandboxTerminalProvider
from .local import LocalFiles
from .egress import BusinessEgress
from .instance_arguments import add_instance_arguments, instance_paths_from_arguments
from .remote import fleet_factory
from .runtime import build_runtime
from .web import create_app

_DEFAULT_MAINTENANCE = object()


def background_model_factory(local_files, sandbox, defaults=None, *, client_factory=None):
    """Compose business model clients without lending host credentials to overrides."""
    defaults = dict(defaults or {})
    client_factory = BackgroundModelClient if client_factory is None else client_factory
    egress = BusinessEgress(local_files, sandbox)

    def resolve(business_id):
        trusted = {**defaults, **local_files.environment()}
        own = {row["key"]: row["value"] for row in local_files.environment_pairs(business_id)} if business_id else {}
        values = {**trusted, **own}
        destination = values.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        proxy = egress.proxy_for(business_id, destination=destination,
                                 trusted_destination=trusted.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                                 own_values=own, token_key="DEEPSEEK_API_KEY", require_https=True)
        options = {"proxy": proxy} if proxy is not None else {}
        return client_factory(ProviderConfig.from_environment(values), **options)
    return resolve


def transcript_capture_configuration(value=None):
    if value is None:
        value = os.environ.get("BAZARLYK_CAPTURE_TRANSCRIPTS", "true").strip().lower()
        if value in {"true", "false"}:
            value = value == "true"
    if type(value) is not bool:
        raise DomainError("invalid_configuration", "BAZARLYK_CAPTURE_TRANSCRIPTS must be true or false")
    return value


def load_terminal_broker(paths):
    from bazarlyk.installation import component
    path = component(paths, 'dashboard') / 'pty_broker.py'
    spec = importlib.util.spec_from_file_location("bazarlyk_dashboard_pty_broker", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assemble(workspace, *, paths, origins, endpoint, bootstrap_token="", shell="/bin/bash",
             model_client_factory=None, broker=None, database=None, signing_key=None, reddit_adapter=None,
             nightly_maintenance=_DEFAULT_MAINTENANCE, capture_transcripts=None, terminal_workspace=None,
             execution_provider=None, sandbox_factory=None, recovery_config=None, github_app_config=None,
             integrations=None, learning_engine=None, context=None):
    from bazarlyk.background.integration import attach_background
    from qorqut_operations.background.programs import ProgramRegistry
    from bazarlyk.integrations.config import from_environment as outreach_host

    if integrations is None:
        integrations = outreach_host()

    capture_transcripts = transcript_capture_configuration(capture_transcripts)
    programs = ProgramRegistry()
    runtime = build_runtime(workspace, paths=paths, database=database, job_programs=programs, context=context)
    if integrations is not None:
        # Post-commit Outreach registration of new businesses; started with the runtime.
        from bazarlyk.bootstrap.preparation import OUTREACH_TOKEN_ROOT
        from bazarlyk.integrations.registrations import OutreachRegistrations
        runtime.outreach_registrations = OutreachRegistrations(runtime.database, integrations,
            email_client=lambda: runtime.domains.email.client, token_root=OUTREACH_TOKEN_ROOT)
    if github_app_config is not None:
        from bazarlyk.sandbox.github_app import GitHubAppConfig, GitHubAppTokenProvider
        from bazarlyk.sandbox.provider_tokens import BusinessProviderTokens
        selected = GitHubAppConfig.read(github_app_config, operations_root=paths.operations)
        runtime.provider_tokens = BusinessProviderTokens(runtime, GitHubAppTokenProvider(selected))
    if nightly_maintenance is _DEFAULT_MAINTENANCE:
        from bazarlyk.storage.versioning import NightlyMaintenance
        nightly_maintenance = NightlyMaintenance(runtime.database, runtime.paths, locks=runtime.locks, git_enabled=True)
    local = LocalFiles(runtime)
    from bazarlyk.shared_context import PublishedContextText
    published_context = PublishedContextText(runtime.domains.shared_context)
    runtime.resolver.context_home = published_context.home_text
    runtime.owner_bootstrap.context_home = published_context.home_text
    runtime.owner_bootstrap.context_status = published_context.status
    if os.environ.get("EMAIL_ENGINE_URL"):
        from .email import EmailClient
        runtime.domains.email.client = EmailClient(os.environ["EMAIL_ENGINE_URL"], os.environ.get("EMAIL_READ_TOKEN", ""),
            admin_token=os.environ.get("EMAIL_ADMIN_TOKEN", ""), send_token=Path(os.environ["EMAIL_SEND_TOKEN_FILE"]).read_text().strip() if os.environ.get("EMAIL_SEND_TOKEN_FILE") else "")
    broker = broker or load_terminal_broker(paths)
    from bazarlyk.sandbox.service import BusinessSandboxService
    from qorqut_operations.storage.quiescence import BoundedWriter
    from bazarlyk.sandbox.quiescence import DockerFreezer
    runtime.sandbox = (sandbox_factory or BusinessSandboxService)(runtime)
    runtime.recovery_quiescer.sandbox_writers = BoundedWriter(runtime.sandbox.worker, "sandbox")
    runtime.recovery_quiescer.container_writers.freezer = DockerFreezer(runtime.sandbox.docker)
    from bazarlyk.sandbox.preview_service import BusinessPreviews
    from bazarlyk.sandbox.legacy_writers import HostTerminalWriters, LegacyBrowserWriters, LegacyHostWriters
    runtime.previews = BusinessPreviews(runtime, runtime.sandbox.docker, origins)
    from bazarlyk.browser.access import BusinessBrowserAccess
    runtime.browser_access = BusinessBrowserAccess(runtime)
    # Browser lifecycle and lease mutations participate in shared admission;
    # legacy browser processes are inventoried separately, and confined Chrome
    # is covered by the exact-container freezer. Preview listeners have their
    # own bounded service thread and must drain before container capture.
    runtime.recovery_quiescer.browser_writers = BoundedWriter(runtime.previews, "private_previews")
    host_terminals = HostTerminalWriters(getattr(broker, "TMUX", None) or "/usr/bin/tmux")
    host_browsers = LegacyBrowserWriters(runtime.database, runtime.paths,
        owns_sandbox_lock=lambda identity: runtime.sandbox.owns_browser_lock(identity))
    host_writers = LegacyHostWriters(host_terminals, host_browsers)
    runtime.recovery_quiescer.host_writers = host_writers
    session_prefix = getattr(broker, "session_prefix", None)
    if callable(session_prefix):
        def adoption_inventory(business, paths, *, timeout):
            # The broker owns terminal naming; do not infer a prefix from a
            # mutable display field while moving this business to sandbox mode.
            runtime.require_authority()
            host_writers.adoption_inactive(business, paths, timeout=timeout,
                                           session_prefix=session_prefix(business["slug"]))
        runtime.sandbox.lifecycle.set_adoption_inventory(adoption_inventory)
    if execution_provider is None:
        execution_provider = SandboxTerminalProvider(runtime.database, runtime.paths, runtime.sandbox.docker, broker,
            local_files=local)
    terminals = TerminalService(runtime, broker, service_url=endpoint, shell=shell,
        environment=local.environment, global_environment=local.environment,
        agent_environment=local.agent_environment, capture_transcripts=capture_transcripts,
        workspace=terminal_workspace, execution_provider=execution_provider)
    # The native binding adapter selects its execution target through this same service.
    runtime.terminals = terminals
    from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend
    runtime.terminal_control.backend = AgentTerminalBackend(terminals)
    from bazarlyk.inbox.broadcasts import BroadcastDispatcher
    runtime.broadcast_dispatcher = BroadcastDispatcher(runtime, runtime.broadcasts, terminals)
    egress = BusinessEgress(local, runtime.sandbox)
    if reddit_adapter is None:
        defaults = {key: os.environ[key] for key in ("BIZ_SERVICE_URL", "BIZ_SERVICE_TOKEN") if key in os.environ}
        reddit_adapter = fleet_factory(local, defaults, egress=egress)
    if model_client_factory is None:
        service_provider = {key: os.environ[key] for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "BIZ_BACKGROUND_MODEL") if key in os.environ}
        model_client_factory = background_model_factory(local, runtime.sandbox, service_provider)

    from bazarlyk.background.backups import BackupDownloader
    from bazarlyk.background.metrics import DailyJSONClient
    from bazarlyk.background.stripe_revenue import Key as StripeRevenueKey, StripeRevenueProgram
    attach_background(runtime, programs, model_client_factory=model_client_factory, transcript_source=local.iter_transcripts,
        reddit_adapter=reddit_adapter, maintenance=nightly_maintenance,
        metric_collector=DailyJSONClient(egress.metrics_environment, proxy_resolver=runtime.sandbox.account_proxy),
        backup_downloader=BackupDownloader(egress.metrics_environment, proxy_resolver=runtime.sandbox.account_proxy),
        stripe_revenue=StripeRevenueProgram(StripeRevenueKey(workspace, runtime.root), local), learning_engine=learning_engine)
    if nightly_maintenance is not None:
        runtime.maintenance_capture = nightly_maintenance.bind_runtime(runtime)
    if recovery_config is not None:
        from bazarlyk.sandbox.recovery_admin import RecoveryAdminConfig, RecoveryAdministration
        selected = RecoveryAdminConfig.load(recovery_config, paths=paths)
        runtime.recovery_admin = RecoveryAdministration(runtime.database, selected,
            lambda: runtime.recovery_quiescence(timeout=60), background_worker=runtime.background)
    runtime.domains.archive.reset_terminals = terminals.reset_business_for_archive
    runtime.domains.archive.prepare_agent = terminals.prepare_restored_agent
    compatibility = DashboardCompatibility(runtime, terminals=terminals, background=runtime.background, local_files=local)
    authentication = Authentication(runtime.database, runtime.paths, bootstrap_token=bootstrap_token, signing_key=signing_key)
    from bazarlyk.installation import component
    # Trusted registration is complete: nothing can declare or install an operation once traffic is served.
    runtime.freeze_registry()
    return create_app(runtime, dashboard_root=component(paths, 'dashboard'), authentication=authentication, origins=origins,
        compatibility=compatibility, terminals=terminals, integrations=integrations)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared Qorqut dashboard and HTTP MCP service")
    parser.add_argument("--workspace", type=Path, default=None)
    add_instance_arguments(parser)
    parser.add_argument("--terminal-workspace", type=Path, default=os.environ.get("QORQUT_TERMINAL_WORKSPACE"))
    parser.add_argument("--biz", action="append", default=[])
    parser.add_argument("--host", default=os.environ.get("BIZ_DASH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BIZ_DASH_PORT", "1049")))
    parser.add_argument("--origin", action="append", default=[])
    parser.add_argument("--service-url", default=os.environ.get("BAZARLYK_MCP_URL"))
    parser.add_argument("--token", default=os.environ.get("BIZ_DASH_TOKEN", ""))
    parser.add_argument("--prepare-agent", nargs=2, metavar=("BUSINESS_SLUG", "AGENT"))
    parser.add_argument("--claude-version")
    parser.add_argument("--codex-version")
    parser.add_argument("--recovery-config", type=Path, help="Private external host configuration for local encrypted recovery")
    parser.add_argument("--context-config", type=Path, help="Explicit JSON file of Context RunnerConfig fields (model, effort, limits); "
                        "the library defaults apply without it")
    parser.add_argument("--github-app-config", type=Path,
                        default=os.environ.get("QORQUT_GITHUB_APP_CONFIG") or None,
                        help="Private external host configuration for business GitHub access (or QORQUT_GITHUB_APP_CONFIG)")
    args = parser.parse_args(argv)
    try:
        if args.workspace is None:
            raise DomainError("invalid_configuration", "Set the explicit engine workspace with --workspace")
        paths = instance_paths_from_arguments(args, required=("app", "dashboard", "template"))
        # The one operational database of the instance; the engine validates its stored target identity and exact schema.
        from bazarlyk.composition.assembly import host_assembly, open_database
        from bazarlyk.service.changes import ChangeFeed
        database = open_database(paths, host_assembly(ChangeFeed()))
        if args.biz:
            with database.read() as uow:
                registered = {str(Path(row["workspace"]).resolve()) for row in uow.repository("businesses").list()}
            if any(str(Path(path).expanduser().resolve()) not in registered for path in args.biz):
                raise DomainError("invalid_configuration", "--biz accepts registered business folders only; it does not import them")
        local_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        if ":" in local_host:
            local_host = "[" + local_host.strip("[]") + "]"
        origins = args.origin + [value for value in os.environ.get("BIZ_SERVICE_ORIGINS", "").split(",") if value]
        origins += [f"http://{local_host}:{args.port}", f"http://localhost:{args.port}"]
        endpoint = args.service_url or f"http://{local_host}:{args.port}/mcp"
        context_settings = None
        if args.context_config is not None:
            from bazarlyk.shared_context.service import ContextSettings, runner_config_from_file
            context_settings = ContextSettings(config=runner_config_from_file(args.context_config))
        app = assemble(args.workspace, paths=paths, origins=origins, endpoint=endpoint, bootstrap_token=args.token,
            shell=os.environ.get("SHELL", "/bin/bash"), database=database, terminal_workspace=args.terminal_workspace,
            recovery_config=args.recovery_config, github_app_config=args.github_app_config, context=context_settings)
        if args.prepare_agent:
            # Preparing an agent writes its bootstrap material, so only the backend that owns the instance may do it:
            # ownership, target validation and mutation admission for the preparation alone, and no service is started.
            with app.state.runtime.owned_preparation():
                runtime, slug, selector = app.state.runtime, *args.prepare_agent
                with database.read() as uow:
                    actor = app.state.authentication.installation_owner(connection=uow.connection)[0]
                from bazarlyk.transport.auth import AuthenticatedPrincipal
                context, business = app.state.http_api.context(AuthenticatedPrincipal(actor), slug, owner=True)
                profile, _ = app.state.http_api.terminals.agent(context, selector)
                from qorqut_operations.contracts import Principal
                terminals = app.state.http_api.terminals
                execution = terminals.execution(business)
                versions = execution.version_provider()
                if execution.target_identity is None and (args.claude_version or args.codex_version):
                    versions = {"claude-code": args.claude_version or "", "codex": args.codex_version or ""}
                preparation = {"endpoint": execution.endpoint,
                    "versions": versions,
                    "capture_transcripts": terminals.capture_transcripts}
                if execution.interpreter is not None:
                    preparation["interpreter"] = execution.interpreter
                if execution.interpreter_validator is not None:
                    preparation["interpreter_validator"] = execution.interpreter_validator
                result = terminals.preparation(runtime.resolver, Principal(profile.agent_id, "agent"), **preparation)
                print(json.dumps(result, sort_keys=True))
                return 0
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False, server_header=False, access_log=False,
            timeout_graceful_shutdown=10, limit_concurrency=200, log_level="info")
        return 0
    except (DomainError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("Service configuration rejected: %s", exc)
        return 1
