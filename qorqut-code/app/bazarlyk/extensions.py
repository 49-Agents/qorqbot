"""Trusted startup selection and the small composition of installed extensions.

An extension is an installed Python package that the owner names in the private installation configuration:

    "extensions": [{"factory": "qorqut_paper.extension:create", "config": {...}}]

Only that list (or an explicit Python injection in tests) can name a factory. Selection is fixed for the process: nothing
here reads a factory from a request, an API, an agent profile, a Project document or a directory scan, and nothing loads,
unloads or replaces an extension afterwards. ``select`` imports only the selected factory modules, validates the
declarations they return and refuses duplicates and unknown anchors; a selected package or configuration that is
unavailable is an error, never a fallback to a host implementation.

An ``Extension`` object is the immutable declaration made by its factory (no network, worker, credential or database
access) and, after ``install(runtime)``, the one instance that ``runtime.extensions[id]`` holds. Core consumes the
declarations through a few explicit lists and maps at the call sites that used to name Paper or content directly:

* storage contributions join the host ``HostComposition`` (and so its fingerprint) before it is frozen;
* catalog metadata, plugin package validators, workspace types and typed item kinds join the registries before any
  resolver uses them; operation handlers are registered by ``install`` on the still-open registry;
* lifecycle steps name an existing start-before and stop-before anchor, and recovery drains them as named writers;
* source handlers are looked up by exact source key by the one inbox finalizer and the inbox, Focus and Task readers;
* native preparation contributions and calendar-provider adapters are ordinary objects with ordinary methods (the
  integrated Project-creation and agent-preparation code makes no extension call: an extension's workers read current
  database state, so those sites have nothing to call);
* routes, assets, framing rules and error mappings are consumed by the HTTP and web composition, which keeps its own
  authentication, origin, CSRF and bearer-scope checks: a route contribution grants no authority.

There is no marketplace, no event bus and no second policy engine here; a hook an extension does not use costs nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from dataclasses import dataclass
import importlib
from pathlib import Path
import re
from types import MappingProxyType

from qorqut_operations.contracts import DomainError

EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
FACTORY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_]*)$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _invalid(message, **details):
    return DomainError("invalid_installation", message, details=details or None)


@dataclass(frozen=True)
class SourceHandler:
    """The handlers of one exact inbox source / resource key (a table such as ``outreach_events``).

    Every callable is optional; a source without a handler stays unavailable and is never visible by default.
    ``row(uow, business, identity)`` returns the canonical record (events, Focus). ``targets(uow, data, business, actor)``
    turns one trigger capture of the source into ``(table, record, actor, event_type, recipients)`` tuples for the one
    inbox finalizer. ``subscription_visible(service, ctx, identity, business, uow)`` and
    ``event_visible(service, ctx, row, uow)`` are the current-authorization checks of a subscription and of a delivered
    event. ``pointer`` is ``(item field, callable(ctx, row, uow))`` and decorates a delivered event. ``reference(inbox,
    ctx, item, business, uow)`` resolves a typed Focus reference of ``item_type`` to its card fields."""

    source: str
    item_type: str | None = None
    subscribable: bool = False
    row: Callable | None = None
    targets: Callable | None = None
    subscription_visible: Callable | None = None
    event_visible: Callable | None = None
    pointer: tuple | None = None
    reference: Callable | None = None

    def __post_init__(self):
        if not isinstance(self.source, str) or not SOURCE_KEY.fullmatch(self.source):
            raise _invalid("A source handler needs an exact lowercase source key")
        if self.item_type is not None and (not isinstance(self.item_type, str) or not SOURCE_KEY.fullmatch(self.item_type)):
            raise _invalid("A source handler item type is a lowercase name")
        for name in ("row", "targets", "subscription_visible", "event_visible", "reference"):
            if getattr(self, name) is not None and not callable(getattr(self, name)):
                raise _invalid(f"Source handler {name} must be callable")
        if self.pointer is not None and (not isinstance(self.pointer, tuple) or len(self.pointer) != 2
                                         or not isinstance(self.pointer[0], str) or not callable(self.pointer[1])):
            raise _invalid("A source handler pointer is (field name, callable)")


class SourceHandlers:
    """Frozen exact-key lookup of the selected extensions' source handlers (empty for a core-only host)."""

    def __init__(self, handlers=()):
        by_source, by_type = {}, {}
        for handler in handlers:
            if not isinstance(handler, SourceHandler):
                raise _invalid("Only SourceHandler values can be registered")
            if handler.source in by_source:
                raise _invalid(f"Duplicate source handler: {handler.source}")
            if handler.item_type is not None:
                if handler.item_type in by_type:
                    raise _invalid(f"Duplicate typed reference: {handler.item_type}")
                by_type[handler.item_type] = handler
            by_source[handler.source] = handler
        self._by_source = MappingProxyType(by_source)
        self._by_type = MappingProxyType(by_type)

    def get(self, source):
        return self._by_source.get(source)

    def for_type(self, item_type):
        return self._by_type.get(item_type)

    @property
    def sources(self):
        return tuple(self._by_source)

    @property
    def subscribable(self):
        return tuple(key for key, handler in self._by_source.items() if handler.subscribable)

    @property
    def item_types(self):
        """``{source key: typed reference name}`` of the handlers that declare a reference type."""
        return {key: handler.item_type for key, handler in self._by_source.items() if handler.item_type}

    def __bool__(self):
        return bool(self._by_source)


@dataclass(frozen=True)
class CalendarProvider:
    """One calendar connection type an extension supplies to the host's ``calendar_*`` operations (``google``, ``fastmail``).
    Core ships no adapter: a connection type no selected extension supplies is ``provider_unavailable``.

    ``name`` is the ``provider`` value of a connection in the private calendar configuration; ``adapter`` is a class built
    as ``adapter(connection_id, config, transport=None)`` that is a context manager with the host's calendar provider
    methods (``bazarlyk.calendar.providers.Provider`` is its base). It grants nothing: access still requires the Global
    calendar capability and the host's calendar authority."""

    name: str
    adapter: Callable

    def __post_init__(self):
        if not isinstance(self.name, str) or not SOURCE_KEY.fullmatch(self.name):
            raise _invalid("A calendar provider is a lowercase connection type")
        if not callable(self.adapter):
            raise _invalid("A calendar provider adapter is a class")


class NativeOrientation(Protocol):
    def __call__(self, prepared: bool, ordubashy: bool, *, agentic_view) -> str: ...


class NativeGuidance(Protocol):
    def __call__(self, *, agentic_view) -> str: ...


@dataclass(frozen=True)
class NativeConnection:
    """One managed MCP server an installed extension can prepare for an agent whose role declares ``name`` among its
    ``external_connections`` (``name`` is that role-declared name, ``server`` the managed MCP server it becomes) or whose
    role id is one of ``roles`` (the general roles the selected extension serves without the role package naming it). Core
    merges it with its ordinary projection and keeps unrelated servers. ``paths`` is the instance's ``InstancePaths``.

    ``status(paths, agent_id)`` says ``{"prepared": bool, "reason": ...}`` without reading a secret;
    ``entry(paths, workspace, agent_id)`` returns the Claude/Codex server entry; ``files(paths, workspace, agent_id)`` the
    generated helper files, each under the managed integration directory; ``orientation(prepared, ordubashy, *, agentic_view)`` and
    ``guidance(*, agentic_view)`` the readiness and instruction fragments. ``retires`` names older managed server names removed on
    refresh. The injected view supplies current authored prose. Callbacks must not fall back to pinned text
    when a managed resource is absent; native readiness, helpers and credentials remain native authority."""

    name: str
    server: str
    status: Callable
    entry: Callable
    files: Callable
    orientation: NativeOrientation
    guidance: NativeGuidance
    retires: tuple = ()
    roles: tuple = ()

    def __post_init__(self):
        for name in ("name", "server"):
            if not isinstance(getattr(self, name), str) or not SOURCE_KEY.fullmatch(getattr(self, name).replace("-", "_")):
                raise _invalid(f"A native connection {name} is a bounded lowercase name")
        if not all(isinstance(role, str) and SOURCE_KEY.fullmatch(role.replace("-", "_")) for role in self.roles):
            raise _invalid("A native connection's default roles are bounded lowercase role ids")
        for name in ("status", "entry", "files", "orientation", "guidance"):
            if not callable(getattr(self, name)):
                raise _invalid(f"Native connection {name} must be callable")


@dataclass(frozen=True)
class ServiceDeclaration:
    """One lifecycle step of an extension service (a worker with start/stop).

    ``start_before`` and ``stop_before`` name existing core steps; the step starts ahead of the first and stops ahead of
    the second, so the retained orderings hold. ``stop`` is the explicit stop adapter ``(name, service, timeout)`` that
    returns a ``QuiescenceProof`` only on positive evidence (``service.stopping``). Every declared service is a
    required recovery writer of the barrier while its extension is selected."""

    name: str
    service: object
    start_before: str
    stop_before: str
    start: Callable | None = None
    stop: Callable | None = None
    running: Callable | None = None
    timeout: float = 5

    def __post_init__(self):
        if not isinstance(self.name, str) or not SOURCE_KEY.fullmatch(self.name) or self.service is None:
            raise _invalid("A service declaration needs a lowercase name and a service")
        if not all(isinstance(value, str) and value for value in (self.start_before, self.stop_before)):
            raise _invalid(f"Service {self.name} must name its start-before and stop-before anchors")


@dataclass(frozen=True)
class OwnerRoute:
    """An owner-dashboard API route, run after the owner cookie, Origin/Host/CSRF and authority checks.

    ``path`` is the exact API path after ``/api/``. ``handler`` is ``async handler(api, request, actor, request_id)`` with
    the established ``HTTPAPI`` helpers; it returns a Starlette response."""

    method: str
    path: tuple
    handler: Callable


@dataclass(frozen=True)
class IntegrationRoute:
    """A route under ``/api/integrations/<slug>/...`` served after the generic integration bearer dispatch.

    ``path`` is the tuple after the slug; ``handler`` is ``async handler(api, request, actor, request_id)`` where
    ``actor`` is the authenticated integration identity. Its slug must own the route (checked by core)."""

    slug: str
    method: str
    path: tuple
    handler: Callable


@dataclass(frozen=True)
class AgentRoute:
    """An agent-credential route under ``/api/agent/...`` (``path`` is the exact tuple including ``agent`` and the
    extension id). ``handler`` is ``async handler(api, request)`` and authenticates the agent itself."""

    method: str | None
    path: tuple
    handler: Callable


@dataclass(frozen=True)
class OperationGuard:
    """Owner-boundary guard for dashboard operation names: ``check(request, name, args, options)`` returns the (possibly
    adjusted) invocation options or raises. ``names`` is a predicate over the operation name."""

    names: Callable
    check: Callable


@dataclass(frozen=True)
class FrameRule:
    """An exact path or prefix whose responses may be framed by ``origin`` (everything else stays unframeable)."""

    path: str
    origin: str
    prefix: bool = False

    def __post_init__(self):
        if not self.path.startswith("/") or ".." in self.path.split("/") or not self.origin:
            raise _invalid("A frame rule needs an absolute path and an origin")


@dataclass(frozen=True)
class Assets:
    """Package-owned UI assets: a directory and the file names it serves, only beneath /extensions/<id>/assets/."""

    directory: Path
    scripts: tuple = ()
    styles: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "directory", Path(self.directory))
        for kind in ("scripts", "styles"):
            names = tuple(getattr(self, kind))
            if any(not isinstance(name, str) or not ASSET_NAME.fullmatch(name) for name in names):
                raise _invalid(f"Extension {kind} are plain file names")
            object.__setattr__(self, kind, names)

    def names(self):
        return (*self.scripts, *self.styles)


@dataclass(frozen=True)
class WebContribution:
    """ASGI routes inserted before the wildcard API route and the framing rules that belong to them."""

    routes: tuple = ()
    framing: tuple = ()


class Extension:
    """Base class: every hook is empty. An extension overrides only what it contributes."""

    id = ""

    # -- declarations, made before the Assembly, registry and resolvers exist --------------------------------------
    def storage(self):
        """Typed engine storage/transaction contributions (their DDL and table specs)."""
        return ()

    def catalog(self):
        """``CatalogContribution`` values with the operation metadata (and history classifications)."""
        return ()

    def history(self):
        """``HistoryContribution`` values: the extension's exported tables and explicit exclusions (no secrets)."""
        return ()

    def package_validators(self):
        """``{plugin id: validator(package)}`` for installed plugin packages this extension owns."""
        return {}

    def workspace_types(self):
        return ()

    def item_kinds(self):
        """Engine ``ItemContribution`` values for typed references (Focus, Task notes, relationships)."""
        return ()

    def sources(self):
        return ()

    def calendar_providers(self):
        """``CalendarProvider`` values: connection types the host's calendar operations can use."""
        return ()

    def native_connections(self):
        return ()

    def note_derivations(self):
        """Callables ``(ctx, uow, message) -> reference | None`` resolved on read for Task discussion messages."""
        return ()

    def error_statuses(self):
        """``{error code: HTTP status}`` for the extension's own domain errors."""
        return {}

    def operation_guards(self):
        return ()

    def assets(self):
        return None

    def integration_adapter(self):
        """Resolve a registered integration's bearer credential: ``adapter(uow, credential_row) -> identity | None``."""
        return None

    # -- runtime wiring: install runs once, on the open registry, before freeze ------------------------------------
    def install(self, runtime):
        """Build the runtime-bound services and register the operation handlers. No worker starts here."""

    def services(self):
        return ()

    def owner_routes(self):
        return ()

    def integration_routes(self):
        return ()

    def agent_routes(self):
        return ()

    def web(self, api, *, agent_only):
        return WebContribution()


@dataclass(frozen=True)
class ExtensionSet:
    """The immutable selection: validated extensions in id order, and the lookups core consumes."""

    extensions: tuple = ()

    def __post_init__(self):
        ordered = []
        for extension in self.extensions:
            if not isinstance(extension, Extension) or not EXTENSION_ID.fullmatch(extension.id or ""):
                raise _invalid("A selected extension must be an Extension with a lowercase id")
            ordered.append(extension)
        if len({extension.id for extension in ordered}) != len(ordered):
            raise _invalid("Duplicate extension id: " + ", ".join(sorted(
                {e.id for e in ordered if sum(1 for o in ordered if o.id == e.id) > 1})))
        object.__setattr__(self, "extensions", tuple(sorted(ordered, key=lambda extension: extension.id)))
        object.__setattr__(self, "sources", SourceHandlers(handler for extension in self.extensions
                                                          for handler in extension.sources()))
        validators, calendars, connections, statuses = {}, {}, {}, {}
        for extension in self.extensions:
            for plugin, validator in dict(extension.package_validators()).items():
                if plugin in validators or not callable(validator):
                    raise _invalid(f"Duplicate or invalid package validator: {plugin}")
                validators[plugin] = validator
            for provider in extension.calendar_providers():
                if not isinstance(provider, CalendarProvider) or provider.name in calendars:
                    raise _invalid("Duplicate or invalid calendar provider")
                calendars[provider.name] = provider
            for connection in extension.native_connections():
                if not isinstance(connection, NativeConnection) or connection.name in connections:
                    raise _invalid("Duplicate or invalid native connection")
                connections[connection.name] = connection
            for code, status in dict(extension.error_statuses()).items():
                if code in statuses or type(status) is not int or not 400 <= status <= 599:
                    raise _invalid(f"Duplicate or invalid error mapping: {code}")
                statuses[code] = status
        adapters = [extension.integration_adapter() for extension in self.extensions if extension.integration_adapter()]
        if len(adapters) > 1:
            raise _invalid("Only one integration credential adapter can be selected")
        object.__setattr__(self, "package_validators", MappingProxyType(validators))
        object.__setattr__(self, "calendar_providers", MappingProxyType({name: item.adapter for name, item in calendars.items()}))
        object.__setattr__(self, "native_connections", MappingProxyType(connections))
        object.__setattr__(self, "error_statuses", MappingProxyType(statuses))
        object.__setattr__(self, "integration_adapter", adapters[0] if adapters else None)
        assets = {}
        for extension in self.extensions:
            declared = extension.assets()
            if declared is not None:
                if not isinstance(declared, Assets):
                    raise _invalid(f"Extension {extension.id} assets must be an Assets value")
                assets[extension.id] = declared
        object.__setattr__(self, "assets", MappingProxyType(assets))
        self.routes()  # reserved namespaces and collisions are refused at selection, not at the first request

    def __bool__(self):
        return bool(self.extensions)

    @property
    def ids(self):
        return tuple(extension.id for extension in self.extensions)

    def storage(self):
        return tuple(contribution for extension in self.extensions for contribution in extension.storage())

    def catalog(self):
        return tuple(contribution for extension in self.extensions for contribution in extension.catalog())

    def history(self):
        return tuple(contribution for extension in self.extensions for contribution in extension.history())

    def workspace_types(self):
        return tuple(value for extension in self.extensions for value in extension.workspace_types())

    def item_kinds(self):
        return tuple(value for extension in self.extensions for value in extension.item_kinds())

    def operation_guards(self):
        return tuple(value for extension in self.extensions for value in extension.operation_guards())

    def note_derivations(self):
        return tuple(value for extension in self.extensions for value in extension.note_derivations())

    def instances(self):
        return MappingProxyType({extension.id: extension for extension in self.extensions})

    def routes(self):
        """``(owner, integration, agent)`` exact-key route maps. Core reserves its own routes: an extension may only
        register owner paths under ``root/integrations`` or ``root/extensions/<id>``, agent paths under ``agent/<id>`` and
        integration paths under its own slug, and two registrations of one key are refused."""
        owner, integration, agent = {}, {}, {}
        for extension in self.extensions:
            for route in extension.owner_routes():
                path = tuple(route.path)
                if not (path[:2] == ("root", "integrations") or path[:3] == ("root", "extensions", extension.id)) or len(path) < 3:
                    raise _invalid(f"Extension {extension.id} owner route {'/'.join(path)} is not in its reserved namespace")
                if (route.method, path) in owner:
                    raise _invalid(f"Duplicate owner route: {route.method} {'/'.join(path)}")
                owner[(route.method, path)] = route.handler
            for route in extension.integration_routes():
                key = (route.slug, route.method, tuple(route.path))
                if key in integration or not SOURCE_KEY.fullmatch(route.slug):
                    raise _invalid(f"Duplicate or invalid integration route: {route.slug}/{'/'.join(route.path)}")
                integration[key] = route.handler
            for route in extension.agent_routes():
                path = tuple(route.path)
                if path[:2] != ("agent", extension.id) or len(path) < 3:
                    raise _invalid(f"Extension {extension.id} agent route {'/'.join(path)} is not in its reserved namespace")
                if (route.method, path) in agent:
                    raise _invalid(f"Duplicate agent route: {'/'.join(path)}")
                agent[(route.method, path)] = route.handler
        return MappingProxyType(owner), MappingProxyType(integration), MappingProxyType(agent)

    def shell_assets(self):
        """The owner-readable list of enabled UI assets (``GET /api/root/extensions``); never configuration."""
        return {"extensions": [{"id": extension_id, "assets": {
            "scripts": [f"/extensions/{extension_id}/assets/{name}" for name in assets.scripts],
            "styles": [f"/extensions/{extension_id}/assets/{name}" for name in assets.styles]}}
            for extension_id, assets in self.assets.items()]}

    def asset_file(self, extension_id, name):
        """The regular file behind ``/extensions/<id>/assets/<name>``, or None. Only the declared file names of a
        selected extension are served, with no path traversal and no symlink out of the asset directory."""
        assets = self.assets.get(extension_id)
        if assets is None or name not in assets.names():
            return None
        root = assets.directory.resolve()
        path = (root / name).resolve()
        return path if path.parent == root and path.is_file() else None

    def install(self, runtime):
        """Install every selected extension once, in id order, on the open registry. Called by ``build_runtime``."""
        installed = []
        for extension in self.extensions:
            extension.install(runtime)
            installed.append(extension.id)
        return tuple(installed)


NO_EXTENSIONS = ExtensionSet(())
NO_SOURCES = NO_EXTENSIONS.sources


def _factory(entry, position):
    if not isinstance(entry, Mapping) or set(entry) - {"factory", "config"} or "factory" not in entry:
        raise _invalid(f"Extension entry {position} must be an object with a factory and an optional config")
    match = FACTORY.fullmatch(entry["factory"]) if isinstance(entry["factory"], str) else None
    config = entry.get("config", {})
    if match is None or not isinstance(config, Mapping):
        raise _invalid(f"Extension entry {position} names an invalid factory or configuration")
    return match.group(1), match.group(2), dict(config)


def validate_entries(entries):
    """Structure of the installation ``extensions`` list: object entries, valid factory names, no repeats. No import."""
    if entries is None:
        return ()
    if not isinstance(entries, (list, tuple)):
        raise _invalid("The extensions setting must be a list")
    seen, parsed = set(), []
    for position, entry in enumerate(entries):
        module_name, attribute, config = _factory(entry, position)
        if (module_name, attribute) in seen:
            raise _invalid(f"Extension factory selected twice: {module_name}:{attribute}")
        seen.add((module_name, attribute))
        parsed.append((module_name, attribute, config))
    return tuple(parsed)


def select(entries=None, *, instances=None):
    """The process's extension selection.

    ``entries`` is the ``extensions`` list of the trusted installation configuration (absent means none); ``instances``
    is explicit Python injection of ready ``Extension`` objects (tests). Both can be given; ids must stay unique."""
    chosen = list(instances or ())
    for module_name, attribute, config in validate_entries(entries):
        try:
            factory = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as exc:
            raise _invalid(f"Selected extension {module_name}:{attribute} is not installed or has no such factory") from exc
        if not callable(factory):
            raise _invalid(f"Selected extension {module_name}:{attribute} is not callable")
        chosen.append(factory(config))
    return ExtensionSet(tuple(chosen)) if chosen else NO_EXTENSIONS


def installed_selection(configuration=None, *, instances=None):
    """``select`` over the trusted installation configuration (``None`` reads the process's own)."""
    if configuration is None:
        from bazarlyk.installation import installation_config
        configuration = installation_config() or {}
    return select(configuration.get("extensions"), instances=instances)


def arrange(steps, stop_order, declarations, make_step):
    """Insert extension lifecycle steps at their named anchors; returns the new ``(steps, stop_order)``.

    ``make_step(declaration)`` builds the ``ServiceStep``. Anchors must exist among the steps and stop order as they
    stand, names must be new, and declarations keep their order ahead of a shared anchor."""
    steps, stop_order = list(steps), list(stop_order)
    for declaration in declarations:
        if declaration.name in {step.name for step in steps} or declaration.name in stop_order:
            raise _invalid(f"Duplicate lifecycle step: {declaration.name}")
        start = next((index for index, step in enumerate(steps) if step.name == declaration.start_before), None)
        if start is None or declaration.stop_before not in stop_order:
            raise _invalid(f"Service {declaration.name} names an unknown anchor: "
                           f"{declaration.start_before if start is None else declaration.stop_before}")
        steps.insert(start, make_step(declaration))
        stop_order.insert(stop_order.index(declaration.stop_before), declaration.name)
    return tuple(steps), tuple(stop_order)
