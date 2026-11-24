"""Resolve one current profile for bootstrap, transport, and policy consumers."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
import logging
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
from threading import Lock, RLock
import time
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from qorqut.catalog.packages import Catalog, content_hash, declared_plugin_tools, files_digest, relative_path, visible_plugin_tools
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, canonical_hash, new_id, plain, utc_now
from qorqut_operations.project_lifecycle import active_project_ids
from qorqut_operations.policy import AgentPolicyBasis
from qorqut_operations.storage.database import unit_memo
from qorqut_operations.storage.structured import parse_yaml
from qorqut.composition.files import lock_paths
from qorqut.composition.locations import agent_workspace, project_code, project_root
from qorqut.inbox.defaults import legacy_inbox_skill
from qorqut.instance_paths import InstancePaths
from . import role_workflow

# Active workers an agent may create without the owner's approval, counted in total.
FREE_WORKERS = 3
from . import cleanup_workflow
from .context import MAX_CONTEXT_CHARACTERS, ContextSource, compose_context
from .models import AgentProfile, EffectiveAgentConfig
from .roles import SUPPORTED_GLOBAL_ROLES

PROFILE_CACHE_LIMIT = 128
# How long one full verification of the instruction tree stands for later calls that find nothing changed. Changes
# through the authoring lock or the state store are seen at once; only a hand edit to the tree waits up to this long.
# Zero turns the reuse off.
SNAPSHOT_REUSE_SECONDS = 2.0
FINGERPRINT_CACHE_LIMIT = 256
# Only global agents run; these are the registered mandatory orientation sources, all in the Global namespace.
BOOTSTRAP_KEYS = frozenset({"foundation", "ecosystem-agent", "ecosystem"})
# Private account capabilities: an operation declaring one is available only to a profile holding it.
EXPLICIT_CAPABILITY_OPERATIONS = frozenset({"manage_calendar"})


def effective_tool_policy(profile: AgentProfile, role: Any, registry: Any, *, packages=()) -> list[dict]:
    from qorqut.composition.policy import GLOBAL_DISCOVERY_OPERATIONS, resolve_operation_policy
    overrides = profile.data["tool_policy"]["overrides"]
    defaults = role.manifest["default_tool_policy"]
    granted = {item["id"] for item in profile.data["plugins"]}
    # Hidden declarations stay callable through the policy resolver's full
    # declared set; discovery, briefings and generated permissions omit them.
    visible = {p.id: visible_plugin_tools(p) for p in packages if p.kind == "plugin"}
    tools = []
    for metadata in registry.entries():
        decision = resolve_operation_policy(metadata.name, defaults, overrides, registry, role_id=profile.data["role"]["id"])
        modes = None
        if (metadata.name in (GLOBAL_DISCOVERY_OPERATIONS | getattr(registry, "extension_global_discovery", frozenset()))
                and "read_global_context" in profile.scope.global_capabilities):
            # Shared table and contributed provider tools may have a distinct Global scope rule.
            # Project grants stay as they were; evaluate both namespaces without broadening either.
            in_global = resolve_operation_policy(metadata.name, defaults, overrides, registry,
                                                 role_id=profile.data["role"]["id"], scope="global")
            modes = {"project": decision.mode, "global": in_global.mode}
            if decision.mode == "deny":
                decision = in_global
        mode = decision.mode
        if decision.provenance == "agent_override":
            source = {"kind": "profile_override", "profile_revision": profile.revision}
        elif decision.provenance == "role_default":
            source = {"kind": "role_default", "role": role.ref}
        elif decision.provenance == "core_default" or decision.provenance.startswith("global_table_"):
            source = {"kind": "core_default"}
        else:
            source = {"kind": "fallback"}
        try:
            descriptor = registry.get(metadata.name)
        except DomainError:
            descriptor = None
        requirement = descriptor.plugin_requirement if descriptor else None
        if requirement and (requirement not in granted or metadata.name not in visible.get(requirement, ())):
            continue
        if (descriptor and descriptor.global_capability in (EXPLICIT_CAPABILITY_OPERATIONS | getattr(registry, "extension_global_capabilities", frozenset()))
                and descriptor.global_capability not in profile.scope.global_capabilities):
            # Central defaults never grant alone: private account tools (calendar, an optional mail provider) need
            # the explicit Global capability on the profile, whatever role default or provider default names them.
            mode = "deny"
        if (descriptor and descriptor.global_capability == "manage_delegated_assignments"
                and "manage_delegated_assignments" not in profile.scope.global_capabilities):
            denial = "capability_not_granted"
        denial = None
        if metadata.owner_only:
            mode, denial = "deny", "owner_only"
        elif "mcp" not in metadata.exposure:
            denial = "not_exposed_to_agent"
        elif descriptor is None:
            denial = "operation_unavailable"
        elif mode == "deny":
            denial = "policy_denied"
        elif descriptor.global_capability == "manage_ecosystem_agents" and "manage_ecosystem_agents" not in profile.scope.global_capabilities:
            # The engine refuses these without the capability; discovery must not offer them.
            denial = "capability_not_granted"
        aliases = [alias for alias in descriptor.aliases
                   if registry.required_plugin_for(alias) in (None, *granted)] if descriptor else []
        row = {"canonical_operation": metadata.name, "exposed_aliases": aliases,
               "input_schema_version": descriptor.version if descriptor else None, "plugin_requirement": requirement,
               "effective_mode": mode, "supports_signoff": metadata.supports_signoff, "source": source,
               "available": descriptor is not None and denial is None}
        if modes:
            row["scope_modes"] = modes
        if denial:
            row["denial_reason"] = denial
        tools.append(row)
    return tools


def _stat(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return ("missing",)
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, path.is_symlink())


def _legacy_tree_signature(path: Path) -> tuple:
    return tuple((item.relative_to(path).as_posix(), _stat(item)) for item in sorted(path.rglob("*")))


def _tree_signature(path: Path) -> tuple:
    """Every entry beneath a published artifact with its ``_stat`` identity, as one hashable value.

    The same facts as walking ``path.rglob("*")`` and calling ``_stat`` on each entry, read with ``os.scandir``
    and one ``lstat`` per entry instead of several ``pathlib`` objects and calls. A published store holds regular
    files and directories only; a tree with a symlink, a special file or an unreadable directory keeps the
    original walk, so nothing about how such a tree is followed or reported changes here.
    """
    entries = []

    def visit(directory: str, prefix: str) -> bool:
        with os.scandir(directory) as found:
            children = sorted(found, key=lambda entry: entry.name)
        for entry in children:
            info = entry.stat(follow_symlinks=False)
            relative = prefix + entry.name
            if stat_module.S_ISDIR(info.st_mode):
                entries.append((relative, (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, False)))
                if not visit(entry.path, relative + "/"):
                    return False
            elif stat_module.S_ISREG(info.st_mode):
                entries.append((relative, (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, False)))
            else:
                return False
        return True

    try:
        if visit(os.fspath(path), ""):
            return tuple(entries)
    except OSError:
        pass
    return _legacy_tree_signature(path)


class SnapshotReuse:
    """The one record of this process's last full verification of the instruction tree.

    A record is ``(revision, change marker, time)``: the revision both verifications of a read snapshot agreed on,
    and the marker and clock read while that snapshot held the authoring lock, before its body ran. It stands for a
    later call only while the marker is unchanged and the record is younger than ``seconds``.
    """

    def __init__(self, *, seconds: float = SNAPSHOT_REUSE_SECONDS, clock=time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self._mutex = Lock()
        self._record: tuple[str, Any, float] | None = None

    @property
    def enabled(self) -> bool:
        return self.seconds > 0

    def current(self, view) -> str | None:
        """The verified revision when the record still stands, else ``None``."""
        with self._mutex:
            record = self._record
        if record is None:
            return None
        revision, marker, at = record
        if not 0 <= self.clock() - at < self.seconds:
            return None
        found = view.change_marker()
        return revision if found is not None and found == marker else None

    def observe(self, view) -> tuple[Any, float] | None:
        """Marker and time, read inside a read snapshot that holds the lock, before its body."""
        at = self.clock()
        marker = view.change_marker()
        return None if marker is None else (marker, at)

    def remember(self, revision: str, observed: tuple[Any, float]) -> None:
        """Keep a record after both verifications passed; a racing older verification never replaces a newer one."""
        marker, at = observed
        with self._mutex:
            if self._record is None or self._record[2] <= at:
                self._record = (revision, marker, at)


class AgentConfigResolver:
    def __init__(self, database: Any, paths: InstancePaths, registry: Any, *, catalog: Catalog | None = None,
                 plugin_handlers: Mapping[str, Any] | None = None, native_connections: Mapping[str, Any] | None = None,
                 snapshot_reuse_seconds: float = SNAPSHOT_REUSE_SECONDS, clock=time.monotonic):
        self.database = database
        self.paths = paths
        self.root = paths.operations
        self.locks = lock_paths(paths)
        self.registry = registry
        self.catalog = catalog or Catalog(database, paths.runtime, registry)
        # Trusted domain callbacks have signature validate(package: ArtifactPackage) -> None.
        self.plugin_handlers = dict(plugin_handlers or {})
        # The installed extensions' native MCP connections, by the name a role declares in ``external_connections``.
        self.native_connections = dict(native_connections or {})
        # Installed explicitly after opening and verifying the managed layer.
        self.agentic_view = None
        self._cache: OrderedDict[str, EffectiveAgentConfig] = OrderedDict()
        self._lock = RLock()
        self._profiles: OrderedDict[tuple[str, str], AgentProfile] = OrderedDict()
        self._profiles_registry: Any = None
        self._profiles_lock = RLock()
        self._fingerprints: OrderedDict[tuple, tuple[str, str]] = OrderedDict()
        self._fingerprints_registry: Any = None
        self._snapshot_reuse = SnapshotReuse(seconds=snapshot_reuse_seconds, clock=clock)

    def _remembered_hashes(self, inputs: tuple | None, compute) -> tuple[str, str]:
        """``compute()`` for these exact inputs, once per registry; ``inputs`` of ``None`` always computes."""
        if inputs is None:
            return compute()
        with self._lock:
            if self._fingerprints_registry is not self.registry:
                self._fingerprints.clear()
                self._fingerprints_registry = self.registry
            known = self._fingerprints.get(inputs)
            if known is not None:
                self._fingerprints.move_to_end(inputs)
                return known
            registry = self.registry
        known = compute()
        with self._lock:
            if self._fingerprints_registry is registry and self.registry is registry:
                self._fingerprints[inputs] = known
                while len(self._fingerprints) > FINGERPRINT_CACHE_LIMIT:
                    self._fingerprints.popitem(last=False)
        return known

    def _parsed_profile(self, principal_id: str, profile_json: str) -> AgentProfile:
        """Parse and schema/policy-validate one stored profile, reusing the result for identical bytes.

        ``AgentProfile.from_dict`` is a pure function of the stored text and the registry's authority metadata, and the
        result is recursively frozen. Once the registry is frozen that metadata can no longer change, so a successful
        result is reusable for the exact same ``(principal, profile_json)``. While assembly can still mutate the registry
        nothing is cached. The cache is bound to the registry instance by reference: a different registry empties it.
        Only the parse is reused: ``profile`` still reads the current rows and repeats every identity, scope, role and
        consistency check on each call. Failures are never cached."""
        registry = self.registry
        if getattr(registry, "frozen", False) is not True or not isinstance(profile_json, str):
            return AgentProfile.from_dict(json.loads(profile_json), registry, stored=True)
        key = (principal_id, profile_json)
        with self._profiles_lock:
            if self._profiles_registry is not registry:
                self._profiles.clear()
                self._profiles_registry = registry
            cached = self._profiles.get(key)
            if cached is not None:
                self._profiles.move_to_end(key)
                return cached
        profile = AgentProfile.from_dict(json.loads(profile_json), registry, stored=True)
        with self._profiles_lock:
            if self._profiles_registry is registry:
                self._profiles[key] = profile
                self._profiles.move_to_end(key)
                while len(self._profiles) > PROFILE_CACHE_LIMIT:
                    self._profiles.popitem(last=False)
        return profile

    def _workspace(self, agent_id: str) -> Path:
        """The identity's workspace, selected and checked against symlinks once per unit of work.

        Selecting it resolves the path on disk and reads the fixed workspace mapping. One operation asks for the
        same identity a dozen times inside its unit; the first answer stands for the rest of that unit only (see
        ``unit_memo``), and a refusal is never kept."""
        memo = unit_memo()
        if memo is None:
            return agent_workspace(self.paths, agent_id)
        key = (self, "workspace", agent_id)
        found = memo.get(key)
        if found is None:
            found = memo[key] = agent_workspace(self.paths, agent_id)
        return found

    def profile(self, principal: Principal, *, connection=None) -> tuple[AgentProfile, dict]:
        if not isinstance(principal, Principal) or principal.kind != "agent":
            raise DomainError("invalid_principal", "An authenticated agent principal is required")
        if connection is None:
            with self.database.read() as uow:
                return self.profile(principal, connection=uow.connection)
        actor = connection.execute("SELECT * FROM principals WHERE id=?", (principal.id,)).fetchone()
        row = connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (principal.id,)).fetchone()
        if actor is None or actor["kind"] != "agent" or not actor["active"] or row is None or row["status"] != "active":
            raise DomainError("inactive_agent", "Agent identity is missing or inactive; read current configuration after preparation")
        profile = self._parsed_profile(principal.id, row["profile_json"])
        if profile.role.id not in SUPPORTED_GLOBAL_ROLES:
            raise DomainError("inactive_agent", "Agent uses an unsupported role and cannot operate")
        if (profile.agent_id != principal.id or row["principal_id"] != principal.id or profile.data["status"] != "active" or profile.revision != row["revision"]
            or profile.data["name"] != row["name"]):
            raise DomainError("profile_conflict", "Indexed identity and canonical profile disagree")
        # The registered identity selects the workspace; the stored column must agree, never choose.
        if Path(row["workspace"]) != self._workspace(row["agent_id"]):
            raise DomainError("registration_conflict", "Agent workspace is not the registered identity's workspace")
        return profile, dict(row)

    # -- AgentAuthority: what the Operations policy resolver asks of the agent registry -------------------------

    def policy_basis(self, context: InvocationContext, uow) -> AgentPolicyBasis:
        """The agent's current profile overrides and pinned role defaults, read in the caller's transaction."""
        profile, _ = self.profile(context.principal, connection=uow.connection)
        role = self.catalog.get("role", plain(profile.data["role"]), connection=uow.connection)
        defaults, overrides = role.manifest["default_tool_policy"], profile.data["tool_policy"]["overrides"]
        if overrides.get("agents_create", defaults.get("agents_create")) == "allow":
            # An agent that may create agents does so freely up to FREE_WORKERS active workers in all; past that
            # each creation is staged for the owner's approval. The count is read in the caller's transaction.
            from .global_scope import active_role_count
            from .roles import GLOBAL_WORKER_ROLE
            if active_role_count(uow.connection, GLOBAL_WORKER_ROLE) >= FREE_WORKERS:
                overrides = {**overrides, "agents_create": "signoff"}
        return AgentPolicyBasis(defaults=defaults, overrides=overrides,
                                role=profile.data["role"], revision=profile.revision)

    def plugin_declares(self, context: InvocationContext, basis: AgentPolicyBasis, descriptor, uow) -> bool:
        """The profile holds the descriptor's plugin grant and that pinned plugin package declares this tool."""
        profile, _ = self.profile(context.principal, connection=uow.connection)
        grant = next((x for x in profile.data["plugins"] if x["id"] == descriptor.plugin_requirement), None)
        if grant is None:
            return False
        package = self.catalog.get("plugin", grant, connection=uow.connection)
        return descriptor.name in declared_plugin_tools(package)

    def invocation_context(self, principal: Principal, *, project_id: str | None = None,
                           source: str = "mcp", session_registration_id: str | None = None,
                           review_project_id: str | None = None) -> InvocationContext:
        config = self.resolve_agent_config(principal, project_id=project_id, include_preparation=False)
        data = config.data
        with self.database.read() as uow:
            allowed = active_project_ids(uow.connection)
        if review_project_id and review_project_id not in allowed:
            raise DomainError("forbidden", "Review project is outside the current scope")
        return InvocationContext(principal, project_id=data.get("project_id"),
            allowed_project_ids=allowed, global_capabilities=frozenset(data["scope"].get("global_capabilities", ())),
            profile_revision=data["profile_revision"], policy_revision=data["profile_revision"],
            role_artifact_ref=ArtifactRef(**data["role"]),
            enabled_plugin_refs=tuple(ArtifactRef(id=item["id"], version=item["version"], digest=item["digest"], kind="plugin") for item in data["plugins"]),
            source=source, session_registration_id=session_registration_id, review_project_id=review_project_id,
            configuration_revision=data["effective_config_id"])

    def runtime_context(self, principal, *, project_id=None, session_registration_id=None, review_project_id=None):
        """Identity/policy only, for trusted delivery and the diagnostic allowlist.

        Ordinary MCP operations must still use invocation_context and receive
        all mandatory context. This path cannot authorize project work itself.
        """
        with self.database.read() as uow:
            profile, _ = self.profile(principal, connection=uow.connection)
            ctx = InvocationContext(principal, project_id=project_id,
                allowed_project_ids=active_project_ids(uow.connection),
                global_capabilities=frozenset(profile.scope.global_capabilities),
                profile_revision=profile.revision, policy_revision=profile.revision,
                role_artifact_ref=profile.role, source='mcp', session_registration_id=session_registration_id,
                review_project_id=review_project_id)
            return self.refresh_invocation_context(ctx, uow)

    def refresh_invocation_context(self, ctx: InvocationContext, uow) -> InvocationContext:
        """Application CAS-time identity refresh using its existing transaction."""
        if ctx.agent_id is None:
            return ctx
        profile, _ = self.profile(ctx.principal, connection=uow.connection)
        from qorqut_operations.project_lifecycle import require_active_project
        require_active_project(uow.connection, ctx.project_id)
        projects = active_project_ids(uow.connection)
        if ctx.review_project_id and ctx.review_project_id not in projects:
            raise DomainError("forbidden", "The review project is no longer in the current agent scope")
        return replace(ctx, allowed_project_ids=projects, global_capabilities=frozenset(profile.scope.global_capabilities),
            profile_revision=profile.revision, policy_revision=profile.revision, role_artifact_ref=profile.role,
            enabled_plugin_refs=tuple(ArtifactRef(id=item["id"], version=item["version"], digest=item["digest"], kind="plugin") for item in profile.data["plugins"]),
            context_refresh_required=ctx.profile_revision != profile.revision)

    def _snapshot(self, principal: Principal, project_id: str | None, *, proposed: tuple[AgentProfile, dict] | None = None,
                  agentic_revision: str | None = None) -> dict:
        """Everything a configuration is composed from, and its cache key.

        ``agentic_revision`` is given only by the reuse path, with the revision its record verified; otherwise the
        revision is read from the caller's read snapshot."""
        with self.database.read() as uow:
            connection = uow.connection
            profile, row = proposed or self.profile(principal, connection=connection)
            from qorqut_operations.project_lifecycle import require_active_project
            target = project_id
            require_active_project(connection, target)
            projects = [{**dict(project), "display_name": project["name"]} for project in connection.execute(
                "SELECT * FROM projects WHERE archived_at IS NULL ORDER BY id")]
            selected = next((item for item in projects if item["id"] == target), None)
            registration = connection.execute("SELECT * FROM machine_registrations WHERE principal_id=? AND workspace=?", (principal.id, row["workspace"])).fetchone()
            owner_names = [item[0] for item in connection.execute(
                "SELECT p.display_name FROM installation i JOIN principals p ON p.id=i.owner_principal_id WHERE i.id='service' AND p.active=1")]
            if target is None:  # Global scope names the registered installation owner.
                owner_names = [item[0] for item in connection.execute(
                    "SELECT p.display_name FROM installation i JOIN principals p ON p.id=i.owner_principal_id WHERE i.id='service'")]
            # Only rows carrying a needed bootstrap key are read; a matching key in the selected project still
            # reaches the wrong-scope rejection below.
            docs = [dict(item) for item in connection.execute(
                "SELECT * FROM documents WHERE ((project_id IS NULL AND namespace='global') OR project_id=?) "
                f"AND json_extract(metadata_json, '$.bootstrap_key') IN ({','.join('?' * len(BOOTSTRAP_KEYS))}) ORDER BY id",
                (target, *sorted(BOOTSTRAP_KEYS)))]
            artifacts = []
            for kind, ref in [("role", profile.data["role"]), *(("plugin", item) for item in profile.data["plugins"])]:
                artifact = connection.execute("SELECT * FROM artifacts WHERE kind=? AND artifact_id=? AND version=? AND digest=?",
                    (kind, ref["id"], ref["version"], ref["digest"])).fetchone()
                if artifact is None:
                    raise DomainError("missing_artifact", f"Missing pinned {kind}: {ref['id']}@{ref['version']}")
                artifacts.append(dict(artifact))
            installed_reads = {item.name for item in self.registry.installed()
                               if "mcp" in item.exposure and not item.owner_only}

            def allowed(reads):
                if not reads <= installed_reads:
                    return False
                from qorqut.composition.policy import resolve_operation_policy
                role = self.catalog.get("role", profile.data["role"], connection=connection)
                return all(resolve_operation_policy(
                    name, role.manifest["default_tool_policy"], profile.data["tool_policy"]["overrides"],
                    self.registry, role_id=role.id).mode == "allow" for name in reads)

        selected_docs, signatures = {}, []
        if "read_global_context" not in profile.scope.global_capabilities:
            raise DomainError("invalid_scope", "Ecosystem orientation requires read_global_context capability")
        def located(base, doc, key):
            path = base / relative_path(doc["path"])
            if not base.resolve().is_relative_to(self.root) or not path.resolve().is_relative_to(base.resolve()) or path.is_symlink():
                raise DomainError("context_scope_conflict", f"Mandatory source escaped its registered scope: {key}")
            signatures.append((key, doc["id"], doc["revision"], doc["content_hash"], doc["availability"], _stat(path)))
            return {**doc, "absolute_path": path}

        for doc in docs:
            key = json.loads(doc["metadata_json"]).get("bootstrap_key")
            if key not in BOOTSTRAP_KEYS:
                continue
            global_source = key in BOOTSTRAP_KEYS
            if (doc["project_id"] is None) != global_source:
                raise DomainError("context_scope_conflict", f"Mandatory source has the wrong registered scope: {key}")
            if key in selected_docs:
                raise DomainError("context_scope_conflict", f"Multiple mandatory sources registered for: {key}")
            selected_docs[key] = located(self.root if global_source else project_root(self.paths, selected), doc, key)
        for artifact in artifacts:
            path = Path(artifact["stored_path"])
            signatures.append((artifact["kind"], artifact["digest"], str(path), _tree_signature(path)))
        if agentic_revision is None:
            agentic_revision = self.require_agentic_view().catalog_revision

        def hashes():
            fingerprint = canonical_hash({"agentic_catalog_revision": agentic_revision, "profile": profile.data, "workspace": row["workspace"], "target": target,
                "projects": projects, "owners": owner_names,
                "sources": [(key, doc["id"], doc["revision"], doc["content_hash"], doc["availability"]) for key, doc in sorted(selected_docs.items())],
                "artifacts": [(item["kind"], item["artifact_id"], item["version"], item["digest"]) for item in artifacts],
                "operations": [(item.name, item.version, [(alias, self.registry.required_plugin_for(alias)) for alias in item.aliases],
                                item.plugin_requirement) for item in self.registry.installed()]})
            return fingerprint, canonical_hash({"fingerprint": fingerprint, "signatures": signatures})

        # Both hashes are pure functions of the values below and of the frozen registry's operations. An unproposed
        # profile is itself a pure function of its stored text, that registry and the active projects, so the same
        # exact inputs reuse the same hashes instead of re-serialising the profile and every
        # installed operation on each call. A proposed profile, an open registry or an unhashable value computes them.
        inputs = None
        if proposed is None and getattr(self.registry, "frozen", False) is True:
            try:
                inputs = (agentic_revision, principal.id, row["profile_json"], row["workspace"], target, tuple(tuple(item.items()) for item in projects), tuple(owner_names),
                          tuple((item["kind"], item["artifact_id"], item["version"], item["digest"]) for item in artifacts),
                          tuple(signatures))
                hash(inputs)
            except TypeError:
                inputs = None
        fingerprint, cache_key = self._remembered_hashes(inputs, hashes)
        return {"profile": profile, "row": row, "project": selected, "projects": projects,
                "documents": selected_docs, "owner_name": ", ".join(owner_names) or "Owner",
                "agentic_catalog_revision": agentic_revision,
                "registration": dict(registration) if registration else None, "fingerprint": fingerprint,
                "cache_key": cache_key}

    def resolve_agent_config(self, principal: Principal, *, project_id: str | None = None,
                             session_registration: Mapping | str | None = None,
                             include_preparation: bool = True) -> EffectiveAgentConfig:
        from qorqut.agentic.view import CONSISTENCY_ATTEMPTS
        if not include_preparation and not session_registration:
            config = self._reused_config(principal, project_id)
            if config is not None:
                return config
        for _ in range(CONSISTENCY_ATTEMPTS):
            try:
                view = self.require_agentic_view()
                reuse = self._reuse_for(view)
                # A snapshot nested in the caller's own is verified by the caller, not here: it records nothing.
                reuse = reuse if reuse is not None and not view.snapshot_active() else None
                with view.read_snapshot():
                    observed = reuse.observe(view) if reuse is not None else None
                    revision = view.catalog_revision if observed is not None else None
                    config = self._resolve_agent_config(principal, project_id=project_id,
                                                        session_registration=session_registration,
                                                        include_preparation=include_preparation)
                # Both verifications of the snapshot passed.
                if observed is not None:
                    reuse.remember(revision, observed)
                return config
            except DomainError as exc:
                if exc.code != "agentic_revision_conflict":
                    raise
        raise DomainError("agentic_revision_conflict", "Managed instructions kept changing during composition", retryable=True)

    def _reuse_for(self, view) -> SnapshotReuse | None:
        """The reuse record, when it is on and the view can report changes without walking the tree."""
        reuse = getattr(self, "_snapshot_reuse", None)
        if reuse is None or not reuse.enabled:
            return None
        if not callable(getattr(view, "change_marker", None)) or not callable(getattr(view, "snapshot_active", None)):
            return None
        return reuse

    def _reused_config(self, principal: Principal, project_id: str | None) -> EffectiveAgentConfig | None:
        """The cached configuration on the last verified revision, without a lock, a store connection or a tree walk.

        Taken only while nothing has changed the authored state through the authoring lock or the state store since
        that verification, it is younger than the reuse period, and this thread holds no read snapshot of its own.
        The profile, project, document and artifact reads are the same as on every other call, and the result is
        only ever a configuration already composed from that revision. ``None`` sends the call to the full path.
        """
        if getattr(self, "_snapshot_reuse", None) is None or self.agentic_view is None:
            return None
        view = self.agentic_view
        reuse = self._reuse_for(view)
        if reuse is None or view.snapshot_active():
            return None
        revision = reuse.current(view)
        if revision is None:
            return None
        snapshot = self._snapshot(principal, project_id, agentic_revision=revision)
        key, _ = self._config_key(snapshot)
        with self._lock:
            config = self._cache.get(key)
        if config is None or snapshot["agentic_catalog_revision"] != revision:
            return None
        return config

    def _config_key(self, snapshot: dict) -> tuple[str, str | None]:
        """The composed-configuration cache key for ``snapshot``, with the Context home text it includes."""
        provider = getattr(self, "context_home", None)
        home_text = provider(snapshot["project"]["id"] if snapshot["project"] else None) if provider else None
        key = (canonical_hash({"key": snapshot["cache_key"], "context_home": home_text})
               if home_text else snapshot["cache_key"])
        return key, home_text

    def _resolve_agent_config(self, principal: Principal, *, project_id: str | None = None,
                              session_registration: Mapping | str | None = None,
                              include_preparation: bool = True) -> EffectiveAgentConfig:
        snapshot = self._snapshot(principal, project_id)
        key, home_text = self._config_key(snapshot)
        with self._lock:
            config = self._cache.get(key)
        if config is None:
            config = self._compose(snapshot, context_home=home_text)
            self.check_agentic_revision(snapshot["agentic_catalog_revision"])
            with self._lock:
                self._cache[key] = config
                self._cache.move_to_end(key)
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
        if not include_preparation and not session_registration:
            # Invocation and inventory resolution add nothing to the composed value: it already carries the empty
            # preparation, no delivery and no refresh requirement. The composed configuration is recursively
            # immutable, so every such caller shares it instead of copying and re-freezing it on each tool call.
            self.check_agentic_revision(snapshot["agentic_catalog_revision"])
            return config
        data = config.to_dict()
        self.check_agentic_revision(snapshot["agentic_catalog_revision"])
        # Only the diagnostic projection consults readiness; invocation and
        # inventory resolution keep the composed empty preparation value and
        # skip its credential/projection inspection.
        data["preparation"] = (self.preparation_status(snapshot["registration"], data)
                               if include_preparation else {})
        delivery = None
        if session_registration:
            registration_id = session_registration if isinstance(session_registration, str) else session_registration.get("registration_id")
            if not snapshot["registration"] or snapshot["registration"]["id"] != registration_id:
                raise DomainError("registration_conflict", "Session registration belongs to another prepared agent")
            session_id = None if isinstance(session_registration, str) else session_registration.get("session_id")
            if session_id:
                with self.database.read() as uow:
                    row = uow.connection.execute("SELECT * FROM session_deliveries WHERE registration_id=? AND session_id=?", (registration_id, session_id)).fetchone()
                if row:
                    delivery = {"effective_config_id": row["effective_config_id"], "lifecycle_event": row["lifecycle_event"],
                                "emitted_at": row["emitted_at"], "bundle_hash": row["bundle_hash"],
                                "current_vs_emitted": row["effective_config_id"] != data["effective_config_id"],
                                "observation": "emitted_by_hook_not_model_compliance"}
        data["delivery"] = delivery
        data["context_refresh_required"] = bool(session_registration and (delivery is None or delivery["current_vs_emitted"]))
        self.check_agentic_revision(snapshot["agentic_catalog_revision"])
        return EffectiveAgentConfig(data)

    def require_agentic_view(self):
        if self.agentic_view is None:
            raise DomainError("agentic_unavailable", "The verified managed instruction layer has not been attached")
        return self.agentic_view

    def check_agentic_revision(self, revision):
        if self.require_agentic_view().catalog_revision != revision:
            raise DomainError("agentic_revision_conflict", "Managed instructions changed during composition", retryable=True)

    def native_guidance_digest(self, data):
        """Fingerprint only the managed prose persisted in native orientation files.

        Both readiness variants are included without probing credentials or a
        process. Selection remains the existing trusted native connection rule.
        """
        from qorqut.agentic.text import render
        from qorqut.bootstrap.preparation import external_connections
        view = self.require_agentic_view()

        def read():
            software = render(view, "host", "app/qorqut/agentic/assets/software-ownership.md")
            if not isinstance(software, str) or not software.strip():
                raise DomainError("agentic_resource_unavailable", "Managed software ownership guidance is empty")
            ordubashy = data["role"]["id"] == "ordubashy"
            connections = []
            for name in external_connections(self, data):
                connection = self.native_connections.get(name)
                if connection is None:
                    continue
                item = {"name": name, "server": connection.server,
                        "orientation_prepared": connection.orientation(True, ordubashy, agentic_view=view),
                        "orientation_unprepared": connection.orientation(False, ordubashy, agentic_view=view),
                        "guidance": connection.guidance(agentic_view=view)}
                if any(not isinstance(item[key], str) for key in item):
                    raise DomainError("agentic_resource_unavailable", "Managed native connection guidance must be text")
                connections.append(item)
            return "sha256:" + canonical_hash({"schema_version": 1, "software_ownership": software,
                                                "connections": sorted(connections, key=lambda row: (row["name"], row["server"]))})

        return view.consistent(read)

    def managed_skills(self, profile, *, selection):
        """Current instruction bodies with the immutable native names and grants."""
        view = self.require_agentic_view()
        if isinstance(profile, Mapping):
            profile = AgentProfile(profile)

        def read():
            metadata, bodies = [], {}
            for workflow in (role_workflow, cleanup_workflow):
                item = workflow.skill_metadata(agentic_view=view)
                content = workflow.skill_files(agentic_view=view)
                item = {**item, "materialization_hash": files_digest(content)}
                metadata.append(item)
                bodies[item["name"]] = content
            from qorqut.bootstrap import email_guidance
            for package in self.packages_for_profile(profile):
                for skill in package.skills:
                    if legacy_inbox_skill(package.kind, skill):
                        continue
                    if email_guidance.is_package(package):
                        content = email_guidance.skill_files(package, self.paths, agentic_view=view)
                    else:
                        content = view.skill_files(package.kind, package, skill["id"], selection=selection)
                    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", content["SKILL.md"].decode("utf-8"), re.S)
                    frontmatter = parse_yaml(match[1]) if match else {}
                    if frontmatter.get("name") != skill["name"] or not isinstance(frontmatter.get("description"), str):
                        raise DomainError("invalid_agentic_skill", "Managed native skill must retain its registered name and description")
                    if skill["name"] in bodies:
                        raise DomainError("invalid_agentic_skill", "Multiple managed skills have the same native name")
                    metadata.append({**skill, "description": frontmatter["description"],
                        "materialization_hash": files_digest(content),
                        "owner": {"kind": package.kind, "id": package.id}, "version": package.version,
                        "digest": package.digest, "generated_path": ".claude/skills/" + skill["name"] + "/SKILL.md",
                        "expected_discoverability": "project_skill_after_preparation"})
                    bodies[skill["name"]] = content
            return metadata, bodies

        return view.consistent(read)

    def code_workspace(self, project) -> str | None:
        """The project's folder when it holds a code folder, else ``None``."""
        if project is None or not project_code(self.paths, project).is_dir():
            return None
        return str(project_root(self.paths, project))

    def _compose(self, snapshot: dict, *, context_home: str | None = None) -> EffectiveAgentConfig:
        profile = snapshot["profile"]
        packages = self.packages_for_profile(profile)
        role = packages[0]
        sources = {}

        def read(doc):
            if doc["availability"] != "ready":
                raise DomainError("context_unavailable", f"Mandatory source is {doc['availability']}: {doc['id']}")
            try:
                from qorqut_operations.storage.safe_io import read_bytes
                data = read_bytes(doc["absolute_path"])
            except OSError as exc:
                raise DomainError("missing_context", f"Missing mandatory document: {doc['id']}") from exc
            if content_hash(data).removeprefix("sha256:") != (doc["content_hash"] or "").removeprefix("sha256:"):
                raise DomainError("document_conflict", f"Externally edited mandatory source requires reconciliation: {doc['id']}")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DomainError("invalid_context", f"Mandatory source is not UTF-8: {doc['id']}") from exc
            return data

        for key, doc in snapshot["documents"].items():
            sources[key] = ContextSource(doc["id"], read(doc).decode("utf-8"), doc["revision"], doc["content_hash"],
                str(doc["absolute_path"]))
        view = self.require_agentic_view()
        from .preferences import load_preferences
        sources["user-preferences/preferences"] = load_preferences(view)
        selection = view.selection(profile, project_id=snapshot["project"]["id"] if snapshot["project"] else None)
        skills, _ = self.managed_skills(profile, selection=selection)
        native_guidance_digest = self.native_guidance_digest(profile.to_dict())
        config_id = "sha256:" + canonical_hash({"fingerprint": snapshot["fingerprint"],
                                               "native_guidance_digest": native_guidance_digest})
        tools = effective_tool_policy(profile, role, self.registry, packages=packages)
        tools = view.tool_instructions().metadata(tools)
        mandatory = compose_context(profile, role, sources, effective_config_id=config_id,
            workspace=snapshot["row"]["workspace"], project=snapshot["project"],
            allowed_projects=tuple(snapshot["projects"]), owner_name=snapshot["owner_name"],
            available_operations=frozenset(tool["canonical_operation"] for tool in tools if tool["available"]),
            context_home=context_home, agentic_view=view, code_workspace=self.code_workspace(snapshot["project"]), extension_guidance=tuple(
                callback(view) for callback in dict.fromkeys(
                    callback for name, callback in getattr(self.registry, "extension_operation_guidance", {}).items()
                    if any(tool["canonical_operation"] == name and tool["available"] for tool in tools))))
        from qorqut.agentic.text import render
        session_note = render(view, "host", "app/qorqut/agentic/assets/inbox-session-note.md")
        if not session_note.strip() or len(session_note) > MAX_CONTEXT_CHARACTERS:
            raise DomainError("agentic_resource_unavailable", "Managed inbox session guidance is empty or oversized")
        self.check_agentic_revision(snapshot["agentic_catalog_revision"])
        return EffectiveAgentConfig({**profile.to_dict(), "workspace": snapshot["row"]["workspace"],
            "project_id": snapshot["project"]["id"] if snapshot["project"] else None,
            "project_revision": snapshot["project"]["revision"] if snapshot["project"] else None,
            "effective_config_id": config_id, "configuration_revision": config_id, "context_refresh_required": False,
            "agentic_catalog_revision": snapshot["agentic_catalog_revision"], "agentic_selection": selection,
            "inbox_session_note": session_note,
            "native_guidance_digest": native_guidance_digest,
            "mandatory_context": mandatory, "skills": skills, "tools": tools,
            "preparation": {}, "delivery": None})

    def packages_for_profile(self, profile: AgentProfile, *, connection=None) -> tuple:
        """Validate immutable pins and trusted domain bindings without loading context."""
        if connection is None:
            with self.database.read() as uow:
                return self.packages_for_profile(profile, connection=uow.connection)
        role = self.catalog.get("role", profile.data["role"], connection=connection)
        if profile.scope.kind not in role.manifest["scope_kinds"]:
            raise DomainError("invalid_scope", "Pinned role does not support this agent scope")
        packages = [role]
        for grant in profile.data["plugins"]:
            plugin = self.catalog.get("plugin", grant, connection=connection)
            if profile.scope.kind not in plugin.manifest["scope_kinds"]:
                raise DomainError("invalid_scope", f"Plugin {plugin.id} does not support this scope")
            schema = parse_yaml(plugin.files[plugin.manifest["config_schema"]].decode())
            errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(plain(grant["config"])))
            if errors:
                details = '; '.join(dict.fromkeys(
                    ('.'.join(map(str, e.absolute_path)) or '$') + ': ' + e.message for e in errors))
                raise DomainError("invalid_plugin_config", f"{plugin.id}: {details[:4000]}")
            handler = self.plugin_handlers.get(plugin.id)
            if handler is None:
                raise DomainError("missing_plugin_handler", f"Granted plugin {plugin.id} has no trusted application handler")
            handler(plugin)
            packages.append(plugin)
        return tuple(packages)

    def preparation_status(self, registration: dict | None, config: dict) -> dict:
        if not registration:
            return {"ready": False, "registration_id": None, "status": "unprepared", "projection_drift": [],
                    "missing_artifacts": [], "personal_sources": "not_inspected", "managed_sources": "not_inspected"}
        from qorqut.bootstrap.preparation import inspect_projection
        from qorqut.bootstrap.client import auth_headers
        projection = json.loads(registration["projection_json"])
        drift = []
        with self.database.read() as uow:
            credential = uow.repository("credentials").get(registration["credential_id"]) if registration["credential_id"] else None
        try:
            token = auth_headers({"workspace": registration["workspace"], "credential_file": ".qorqut/credential"})["Authorization"][7:]
        except DomainError:
            token = None
        if (not credential or credential["principal_id"] != config["agent_id"] or credential["revoked_at"]
            or credential["expires_at"] and credential["expires_at"] <= utc_now()
            or token is None or hashlib.sha256(token.encode()).hexdigest() != credential["token_hash"]):
            drift.append("scoped_credential_unavailable")
        from qorqut.bootstrap.diagnostics import prepared_helpers
        helpers=prepared_helpers(projection,registration["workspace"])
        if not helpers["ready"]:drift.append(helpers["reason"])
        compatibility = json.loads(registration["compatibility_json"])
        expected_skills = {item["name"]: item["materialization_hash"] for item in config["skills"]}
        if projection.get("selected_skills") != expected_skills:
            drift.append("selected_skills_changed")
        from qorqut_agentic.behavior.instructions import SNAPSHOT, HELPER
        runtime_files = projection.get("files", {})
        if SNAPSHOT not in runtime_files or HELPER not in runtime_files:
            drift.append("runtime_instructions_unprepared")
        elif runtime_files[SNAPSHOT] != content_hash(self.require_agentic_view().runtime_instructions().bytes()):
            if "selected_skills_changed" not in drift:
                drift.append("selected_skills_changed")
        native_guidance = projection.get("native_guidance_digest")
        if (not isinstance(native_guidance, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", native_guidance)
                or native_guidance != config["native_guidance_digest"]):
            drift.append("native_guidance_changed")
        if projection.get("artifact_refs") != {"role": config["role"], "plugins": [{key: item[key] for key in ("id", "version", "digest")} for item in config["plugins"]]}:
            drift.append("artifact_pins_changed")
        from qorqut.bootstrap.tool_permissions import changed as tool_permissions_changed
        tools_changed = tool_permissions_changed(registration["workspace"], projection, config["tools"])
        reports = compatibility.get("runtimes", {"claude-code": compatibility})
        runtimes = {}
        for runtime, report in reports.items():
            runtime_drift = sorted(set(drift + inspect_projection(Path(registration["workspace"]), projection, runtime=runtime)))
            # Only Claude serializes native per-tool allow/deny rules. Codex's
            # MCP permissions remain live service policy, not generated files.
            if runtime == "claude-code" and tools_changed:
                runtime_drift = sorted(set(runtime_drift + ["tool_configuration_changed"]))
            runtimes[runtime] = {"ready": not runtime_drift and report.get("supported") is True,
                                 "projection_drift": runtime_drift, "compatibility": report}
        ready = any(item["ready"] for item in runtimes.values())
        combined_drift = [] if ready else sorted({item for row in runtimes.values() for item in row["projection_drift"]})
        return {"ready": ready, "runtimes": runtimes, "helper": helpers,
                "registration_id": registration["id"], "settings_digest": registration["settings_digest"],
                "skills_digest": registration["skills_digest"], "compatibility": compatibility,
                "projection_drift": combined_drift, "missing_artifacts": [], "status": "prepared" if ready else "reprepare_required",
                "prepared_profile_revision": registration["profile_revision"], "personal_sources": "not_inspected",
                "managed_sources": "not_inspected"}

    def briefing(self, principal: Principal, config: dict) -> dict:
        builder = getattr(self, "work_briefing", None)
        if builder is None:
            return {"rendered": self.require_agentic_view().runtime_instructions().render('briefing-runtime-unavailable'),
                    "available": False}
        return builder.build(principal, config)

    def bootstrap(self, principal: Principal, *, registration_id: str, session_id: str, event: str,
                  last_effective_config_id: str | None = None, runtime: str = "claude-code") -> dict:
        from qorqut.bootstrap.tool_permissions import preparation_lock
        with preparation_lock(self.paths, principal.id):
            with self.require_agentic_view().read_snapshot():
                return self._bootstrap(principal, registration_id=registration_id, session_id=session_id,
                    event=event, last_effective_config_id=last_effective_config_id, runtime=runtime)

    def _bootstrap(self, principal: Principal, *, registration_id: str, session_id: str, event: str,
                   last_effective_config_id: str | None, runtime: str) -> dict:
        from qorqut.bootstrap.preparation import RUNTIMES
        if runtime not in RUNTIMES:
            raise DomainError("unsupported_runtime", "Unsupported agent CLI")
        if event not in {"startup", "resume", "clear", "compact", "fork", "prompt"} or not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
            raise DomainError("validation_error", "Invalid bootstrap lifecycle or session ID")
        config = self.resolve_agent_config(principal, session_registration={"registration_id": registration_id, "session_id": session_id})
        data = config.to_dict()
        repair_error = None
        from qorqut.bootstrap import skill_refresh
        try:
            if skill_refresh.refresh(self, principal, config, registration_id, runtime):
                config = self.resolve_agent_config(principal,
                    session_registration={"registration_id": registration_id, "session_id": session_id})
                data = config.to_dict()
        except DomainError as exc:
            repair_error = exc.message
        except (OSError, ValueError, TypeError, KeyError):
            repair_error = "The changed skill instructions could not be refreshed safely"
        if runtime == "claude-code" and repair_error is None:
            from qorqut.bootstrap.tool_permissions import refresh
            try:
                if refresh(self, principal, config, registration_id):
                    config = self.resolve_agent_config(principal,
                        session_registration={"registration_id": registration_id, "session_id": session_id})
                    data = config.to_dict()
            except DomainError as exc:
                repair_error = exc.message
            except (OSError, ValueError, TypeError, KeyError):
                repair_error = "The generated tool permissions could not be refreshed safely"
        selected = data["preparation"].get("runtimes", {}).get(runtime)
        if selected is not None:
            data["preparation"] = {**data["preparation"], **selected, "runtime": runtime}
            if not selected["ready"]:
                data["preparation"]["status"] = "reprepare_required"
        elif runtime != "claude-code":
            data["preparation"]["ready"] = False
        if not data["preparation"]["ready"]:
            preparation = data["preparation"]
            reasons = {
                "scoped_credential_unavailable": "The saved agent credential is missing, expired or revoked",
                "selected_skills_changed": "The selected skill instructions changed",
                "runtime_instructions_unprepared": "Managed runtime instructions require explicit agent preparation",
                "artifact_pins_changed": "The selected role or plugins changed",
                "tool_configuration_changed": "Generated Claude tool permissions need refreshing",
                "native_guidance_changed": "Managed native orientation changed; explicit agent preparation is required",
            }
            drift = preparation.get("projection_drift", [])
            if (set(drift) == {"helper_probe_unavailable"} and not repair_error
                    and selected and selected.get("compatibility", {}).get("supported")):
                preparation["status"] = "bootstrap_unavailable"
                return {"ready": False, "effective_config_id": config.effective_config_id,
                        "error": {"code": "bootstrap_unavailable", "retryable": True,
                                  "message": "The native helper readiness check is temporarily unavailable; retry automatically"},
                        "preparation": preparation}
            details = [reasons.get(item, "Generated file changed: " + item) for item in drift[:6]]
            if selected and not selected.get("compatibility", {}).get("supported"):
                details.append("The installed " + runtime + " version does not meet the preparation requirements")
            if repair_error:
                details.append(repair_error)
            message = "; ".join(details) or "This agent has not been prepared"
            message += ". Operator action: reprepare this agent from its saved profile. A new conversation or /role cannot repair this configuration"
            return {"ready": False, "effective_config_id": config.effective_config_id,
                    "error": {"code": "reprepare_required", "message": message},
                    "preparation": preparation}
        delivery = getattr(self, 'timeline_delivery', None)
        if delivery:
            try:
                delivery.start(principal.id, session_id, event, last_effective_config_id)
            except Exception:
                logging.getLogger(__name__).exception('Timeline delivery start failed for %s', principal.id)
        changed = last_effective_config_id != config.effective_config_id or data["context_refresh_required"]
        result = {"ready": True, "effective_config_id": config.effective_config_id, "agentic_catalog_revision": data["agentic_catalog_revision"], "refresh_required": changed,
                  "reload_skills": event != "prompt", "preparation": data["preparation"]}
        result["inbox_available"] = any(tool["canonical_operation"] == "inbox_list" and tool["available"] for tool in data["tools"])
        if result["inbox_available"]:
            result["inbox_session_note"] = data["inbox_session_note"]
        # The per-prompt Context lookup reminder is for agents that can act on it: all four graph reads are available.
        result["context_reads_available"] = {"context_expand", "context_find", "context_home", "context_object"} <= {
            tool["canonical_operation"] for tool in data["tools"] if tool["available"]}
        if event != "prompt" or changed:
            result["mandatory_context"] = data["mandatory_context"]
            startup = getattr(self, 'startup_context', None)
            result["work_briefing"] = startup.bootstrap(principal, data) if startup else self.briefing(principal, data)
            activity = getattr(self, 'activity', None)
            if activity:
                team = activity.startup(principal, data)
                management = getattr(self,'management',None)
                if management:
                    intake = management.startup(principal,data)
                    if intake: team = (team+'\n\n'+intake).strip()
                if team:
                    # Existing container helpers already render work_briefing.
                    # Keep the summary usable without replacing their running image.
                    briefing = result['work_briefing']
                    rendered = briefing.get('rendered', '') + '\n\n' + team
                    reserve = MAX_CONTEXT_CHARACTERS - len(data['mandatory_context']['rendered']) - 2048
                    if len(rendered) > reserve:
                        rendered = 'Before work, call agent_context_get(inventory=true) and follow next_cursor.\n\n' + team
                    if len(rendered) > reserve:
                        rendered = 'Before work, finish agent_context_get(inventory=true) and agents_activity_list continuation pages.'
                    result['work_briefing'] = {**briefing, 'rendered': rendered,
                        'character_count': len(rendered), 'bundle_hash': content_hash(rendered.encode())}
        # On every event, prompts included: the hook puts it first at startup and adds it to each prompt, so it reaches
        # the model even when a CLI shows only the start of a long startup context.
        management = getattr(self, 'management', None)
        first = management.first_conversation(principal) if management else ''
        if first:
            result["first_conversation"] = first
        return result

    def record_delivery(self, principal: Principal, *, registration_id: str, session_id: str,
                        effective_config_id: str, bundle_hash: str, event: str, emitted_at: str,
                        briefing_hash: str | None = None) -> dict:
        from qorqut_operations.application import Application
        try:
            if datetime.fromisoformat(emitted_at).utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise DomainError("validation_error", "Emission time requires an ISO timestamp with timezone") from exc
        config = self.resolve_agent_config(principal, session_registration={"registration_id": registration_id, "session_id": session_id})
        if config.effective_config_id != effective_config_id or config.data["mandatory_context"]["bundle_hash"] != bundle_hash:
            raise DomainError("stale_delivery", "Current configuration changed before the delivery receipt")
        if event not in {"startup", "resume", "clear", "compact", "fork", "prompt"} or not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
            raise DomainError("validation_error", "Invalid delivery lifecycle or session ID")
        if briefing_hash is not None and (not isinstance(briefing_hash, str) or len(briefing_hash) > 100):
            raise DomainError("validation_error", "Invalid briefing hash")
        with self.database.transaction() as uow:
            Application.ensure_writes_open(uow)
            self.profile(principal, connection=uow.connection)
            repository = uow.repository("session_deliveries")
            rows = repository.list(filters={"registration_id": registration_id, "session_id": session_id}, limit=1)
            values = {"registration_id": registration_id, "session_id": session_id, "effective_config_id": effective_config_id,
                      "bundle_hash": bundle_hash, "lifecycle_event": event, "emitted_at": emitted_at,
                      "metadata_json": {"observed": "hook_emitted", "model_compliance": "not_observed",
                                        "briefing_hash": briefing_hash}}
            if rows:
                repository.update(rows[0]["id"], values, expected_revision=rows[0]["revision"])
            else:
                repository.insert({"id": new_id(), **values})
        return {"recorded": True, "effective_config_id": effective_config_id, "observation": "hook_emitted"}


def resolve_agent_config(resolver: AgentConfigResolver, principal: Principal, *, project_id=None, session_registration=None,
                         include_preparation: bool = True) -> EffectiveAgentConfig:
    return resolver.resolve_agent_config(principal, project_id=project_id, session_registration=session_registration,
                                         include_preparation=include_preparation)
