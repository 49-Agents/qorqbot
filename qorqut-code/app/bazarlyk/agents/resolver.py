"""Resolve one current profile for bootstrap, transport, and policy consumers."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from bazarlyk.catalog.packages import (Catalog, content_hash, declared_plugin_tools, parse_yaml, relative_path,
                                       visible_plugin_tools)
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, canonical_hash, new_id, plain, utc_now
from qorqut_operations.policy import AgentPolicyBasis
from bazarlyk.composition.files import lock_paths
from bazarlyk.composition.locations import agent_workspace, business_hq
from bazarlyk.inbox.defaults import INBOX_INSTRUCTIONS, legacy_inbox_skill
from bazarlyk.instance_paths import InstancePaths
from . import role_workflow
from . import cleanup_workflow
from .context import MAX_CONTEXT_CHARACTERS, ContextSource, compose_context
from .global_scope import with_effective_scope
from .models import AgentProfile, EffectiveAgentConfig, RETIRED_ROLE_IDS
from .roles import LEARNING_APPLIER_ROLE, RETIRED_PLUGIN_IDS, SUPPORTED_GLOBAL_ROLES

PROFILE_CACHE_LIMIT = 128


def effective_tool_policy(profile: AgentProfile, role: Any, registry: Any, *, packages=()) -> list[dict]:
    from bazarlyk.composition.policy import GLOBAL_DISCOVERY_OPERATIONS, resolve_operation_policy
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
        if (metadata.name in GLOBAL_DISCOVERY_OPERATIONS and profile.scope.kind == "ecosystem"
                and "read_global_context" in profile.scope.global_capabilities):
            # The same tool serves the Global table namespace, and whatsapp_send is a proposal only the Global Ordubashy
            # can make. Business grants stay as they were; a Global grant makes the tool available where the business
            # one denies it.
            in_global = resolve_operation_policy(metadata.name, defaults, overrides, registry,
                                                 role_id=profile.data["role"]["id"], scope="global")
            modes = {"business": decision.mode, "global": in_global.mode}
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
        if metadata.name in {"business_email_conversations_list", "business_email_conversation_get",
                             "business_email_sync"} and visible.get("email-management"):
            # Legacy coordinator spellings stay callable for open sessions but
            # disappear from discovery once the canonical toolkit is granted.
            continue
        if (metadata.name in {"email_thread_update", "email_comment", "email_comments_list",
                              "email_decision_request", "email_decision_replace", "email_approval_attention_set"}
                and "email-management" not in granted and profile.scope.kind == "business"):
            # Workflow ops belong to the one email toolkit for business agents;
            # the ecosystem coordinator keeps its native inbox discovery.
            continue
        if metadata.name.startswith("calendar_") and "manage_calendar" not in profile.scope.global_capabilities:
            mode = "deny"
        if metadata.name.startswith("account_email_") and "manage_fastmail" not in profile.scope.global_capabilities:
            # Ruling 2A: central defaults never grant alone. Account-scope
            # spellings require the explicit Global account grant; the runtime
            # account_authority gate additionally pins the configured Ordubashy.
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


class AgentConfigResolver:
    def __init__(self, database: Any, paths: InstancePaths, registry: Any, *, catalog: Catalog | None = None,
                 plugin_handlers: Mapping[str, Any] | None = None, native_connections: Mapping[str, Any] | None = None):
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
        self._cache: OrderedDict[str, EffectiveAgentConfig] = OrderedDict()
        self._lock = RLock()
        self._profiles: OrderedDict[tuple[str, str], AgentProfile] = OrderedDict()
        self._profiles_registry: Any = None
        self._profiles_lock = RLock()

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
        if row["home_business_id"] is not None:
            # Businesses own resources only. A business-home identity is historical
            # whatever its stored status, and is never revived for execution.
            raise DomainError("inactive_agent", "Business-home agent identities are historical and cannot operate")
        profile = self._parsed_profile(principal.id, row["profile_json"])
        if profile.role.id in RETIRED_ROLE_IDS or profile.role.id not in SUPPORTED_GLOBAL_ROLES:
            raise DomainError("inactive_agent", "Agent uses a retired or unsupported role and cannot operate")
        if profile.scope.kind != "ecosystem":
            raise DomainError("profile_conflict", "An active agent must hold ecosystem scope")
        if (profile.agent_id != principal.id or row["principal_id"] != principal.id or profile.data["status"] != "active" or profile.revision != row["revision"]
            or profile.data["name"] != row["name"] or profile.scope.business_id != row["home_business_id"]
            or row["namespace"] != (profile.scope.business_id or "ecosystem")):
            raise DomainError("profile_conflict", "Indexed identity and canonical profile disagree")
        business = (None if row["home_business_id"] is None else
                    connection.execute("SELECT slug FROM businesses WHERE id=?", (row["home_business_id"],)).fetchone())
        # The registered identity and home scope select the workspace; the stored column must agree, never choose.
        if Path(row["workspace"]) != agent_workspace(self.paths, row["agent_id"], business):
            raise DomainError("registration_conflict", "Agent workspace is not the registered identity's workspace")
        # Effective access is every active business in this transaction, not the saved list.
        return with_effective_scope(profile, connection), dict(row)

    # -- AgentAuthority: what the Operations policy resolver asks of the agent registry -------------------------

    def policy_basis(self, context: InvocationContext, uow) -> AgentPolicyBasis:
        """The agent's current profile overrides and pinned role defaults, read in the caller's transaction."""
        profile, _ = self.profile(context.principal, connection=uow.connection)
        role = self.catalog.get("role", plain(profile.data["role"]), connection=uow.connection)
        return AgentPolicyBasis(defaults=role.manifest["default_tool_policy"],
                                overrides=profile.data["tool_policy"]["overrides"],
                                role=profile.data["role"], revision=profile.revision)

    def plugin_declares(self, context: InvocationContext, basis: AgentPolicyBasis, descriptor, uow) -> bool:
        """The profile holds the descriptor's plugin grant and that pinned plugin package declares this tool."""
        profile, _ = self.profile(context.principal, connection=uow.connection)
        grant = next((x for x in profile.data["plugins"] if x["id"] == descriptor.plugin_requirement), None)
        if grant is None:
            return False
        package = self.catalog.get("plugin", grant, connection=uow.connection)
        return descriptor.name in declared_plugin_tools(package)

    def invocation_context(self, principal: Principal, *, business_id: str | None = None,
                           source: str = "mcp", session_registration_id: str | None = None,
                           review_business_id: str | None = None) -> InvocationContext:
        config = self.resolve_agent_config(principal, business_id=business_id)
        data = config.data
        scope = data["scope"]
        allowed = frozenset((scope["business_id"],)) if scope["kind"] == "business" else frozenset(scope["allowed_business_ids"])
        if review_business_id and review_business_id not in allowed:
            raise DomainError("forbidden", "Review project is outside the current scope")
        return InvocationContext(principal, business_id=data.get("business_id"), home_business_id=scope.get("business_id"),
            allowed_business_ids=allowed, global_capabilities=frozenset(scope.get("global_capabilities", ())),
            profile_revision=data["profile_revision"], policy_revision=data["profile_revision"],
            role_artifact_ref=ArtifactRef(**data["role"]),
            enabled_plugin_refs=tuple(ArtifactRef(id=item["id"], version=item["version"], digest=item["digest"], kind="plugin") for item in data["plugins"]),
            source=source, session_registration_id=session_registration_id, review_business_id=review_business_id,
            configuration_revision=data["effective_config_id"])

    def runtime_context(self, principal, *, business_id=None, session_registration_id=None, review_business_id=None):
        """Identity/policy only, for trusted delivery and the diagnostic allowlist.

        Ordinary MCP operations must still use invocation_context and receive
        all mandatory context. This path cannot authorize business work itself.
        """
        with self.database.read() as uow:
            profile, _ = self.profile(principal, connection=uow.connection)
            ctx = InvocationContext(principal, business_id=business_id or profile.scope.business_id,
                home_business_id=profile.scope.business_id, allowed_business_ids=profile.scope.businesses,
                global_capabilities=frozenset(profile.scope.global_capabilities),
                profile_revision=profile.revision, policy_revision=profile.revision,
                role_artifact_ref=profile.role, source='mcp', session_registration_id=session_registration_id,
                review_business_id=review_business_id)
            return self.refresh_invocation_context(ctx, uow)

    def refresh_invocation_context(self, ctx: InvocationContext, uow) -> InvocationContext:
        """Application CAS-time identity refresh using its existing transaction."""
        if ctx.agent_id is None:
            return ctx
        profile, _ = self.profile(ctx.principal, connection=uow.connection)
        from qorqut_operations.business_lifecycle import active_business_ids, require_active_business
        target = ctx.business_id or profile.scope.business_id
        require_active_business(uow.connection, target)
        if target is not None and target not in profile.scope.businesses:
            raise DomainError("forbidden", "The selected project is no longer in the current agent scope")
        if ctx.review_business_id and ctx.review_business_id not in profile.scope.businesses:
            raise DomainError("forbidden", "The review project is no longer in the current agent scope")
        return replace(ctx, business_id=target, home_business_id=profile.scope.business_id,
            allowed_business_ids=profile.scope.businesses & active_business_ids(uow.connection), global_capabilities=frozenset(profile.scope.global_capabilities),
            profile_revision=profile.revision, policy_revision=profile.revision, role_artifact_ref=profile.role,
            enabled_plugin_refs=tuple(ArtifactRef(id=item["id"], version=item["version"], digest=item["digest"], kind="plugin") for item in profile.data["plugins"]),
            context_refresh_required=ctx.profile_revision != profile.revision)

    def _snapshot(self, principal: Principal, business_id: str | None, *, proposed: tuple[AgentProfile, dict] | None = None) -> dict:
        with self.database.read() as uow:
            connection = uow.connection
            profile, row = proposed or self.profile(principal, connection=connection)
            profile = with_effective_scope(profile, connection)
            from qorqut_operations.business_lifecycle import require_active_business
            target = business_id or profile.scope.business_id
            require_active_business(connection, target)
            if target is not None and target not in profile.scope.businesses:
                raise DomainError("forbidden", "Selected project is outside the agent's current scope")
            businesses = []
            for permitted in sorted(profile.scope.businesses):
                business = connection.execute("SELECT * FROM businesses WHERE id=?", (permitted,)).fetchone()
                if business is None:
                    raise DomainError("invalid_scope", f"Profile references an unregistered project: {permitted}")
                if business["archived_at"] is None:
                    businesses.append({**dict(business), "display_name": business["name"]})
            selected = next((item for item in businesses if item["id"] == target), None)
            registration = connection.execute("SELECT * FROM machine_registrations WHERE principal_id=? AND workspace=?", (principal.id, row["workspace"])).fetchone()
            owner_names = [item[0] for item in connection.execute(
                "SELECT p.display_name FROM business_memberships m JOIN principals p ON p.id=m.principal_id WHERE m.business_id=? AND m.is_owner=1 AND m.active=1 AND p.active=1 ORDER BY p.id", (target,))]
            if target is None:  # Global scope names the registered installation owner.
                owner_names = [item[0] for item in connection.execute(
                    "SELECT p.display_name FROM installation i JOIN principals p ON p.id=i.owner_principal_id WHERE i.id='service'")]
            docs = [dict(item) for item in connection.execute(
                "SELECT * FROM documents WHERE (business_id IS NULL AND namespace='global') OR business_id=? ORDER BY id", (target,))]
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
                from bazarlyk.composition.policy import resolve_operation_policy
                role = self.catalog.get("role", profile.data["role"], connection=connection)
                return all(resolve_operation_policy(
                    name, role.manifest["default_tool_policy"], profile.data["tool_policy"]["overrides"],
                    self.registry, role_id=role.id).mode == "allow" for name in reads)

            strategy = []
            if (selected and profile.data["role"]["id"] != LEARNING_APPLIER_ROLE
                    and allowed({"notes_list", "notes_get"})):
                from bazarlyk.domains.documents.business_notes import KIND as BUSINESS_NOTE
                # The owner's business plan is the pin convention: every active
                # pinned strategy-notes note of the selected business, never a title.
                for item in connection.execute(
                        "SELECT * FROM documents WHERE business_id=? AND kind=? AND archived=0 ORDER BY id",
                        (selected["id"], BUSINESS_NOTE)):
                    metadata = json.loads(item["metadata_json"])
                    if metadata.get("category") == "strategy-notes" and metadata.get("pinned") is True:
                        strategy.append({**dict(item), "title": metadata.get("title")})
        selected_docs, signatures = {}, []
        # Only global agents run; the retired business-agent instruction source is never required.
        needed = {"foundation", "ecosystem-agent", "ecosystem"}
        if profile.scope.kind != "ecosystem":
            raise DomainError("invalid_scope", "Only ecosystem agents receive orientation")
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
            if key not in needed:
                continue
            global_source = key in {"foundation", "ecosystem-agent", "ecosystem"}
            if (doc["business_id"] is None) != global_source:
                raise DomainError("context_scope_conflict", f"Mandatory source has the wrong registered scope: {key}")
            if key in selected_docs:
                raise DomainError("context_scope_conflict", f"Multiple mandatory sources registered for: {key}")
            selected_docs[key] = located(self.root if global_source else business_hq(self.paths, selected), doc, key)
        strategy = [located(business_hq(self.paths, selected), doc, "strategy-note:" + doc["id"]) for doc in strategy]
        for artifact in artifacts:
            path = Path(artifact["stored_path"])
            signatures.append((artifact["kind"], artifact["digest"], str(path),
                [(item.relative_to(path).as_posix(), _stat(item)) for item in sorted(path.rglob("*"))]))
        fingerprint = canonical_hash({"inbox_instructions": INBOX_INSTRUCTIONS, "role_workflow": role_workflow.fingerprint(), "cleanup_workflow": cleanup_workflow.fingerprint(), "profile": profile.data, "workspace": row["workspace"], "target": target,
            "businesses": businesses, "owners": owner_names,
            "sources": [(key, doc["id"], doc["revision"], doc["content_hash"], doc["availability"]) for key, doc in sorted(selected_docs.items())],
            "strategy_notes": [(doc["id"], doc["revision"], doc["content_hash"], doc["availability"], doc["path"], doc["title"])
                               for doc in strategy],
            "artifacts": [(item["kind"], item["artifact_id"], item["version"], item["digest"]) for item in artifacts],
            "operations": [(item.name, item.version, [(alias, self.registry.required_plugin_for(alias)) for alias in item.aliases],
                            item.plugin_requirement) for item in self.registry.installed()]})
        return {"profile": profile, "row": row, "business": selected, "businesses": businesses,
                "documents": selected_docs, "strategy_notes": strategy, "owner_name": ", ".join(owner_names) or "Owner",
                "registration": dict(registration) if registration else None, "fingerprint": fingerprint,
                "cache_key": canonical_hash({"fingerprint": fingerprint, "signatures": signatures})}

    def resolve_agent_config(self, principal: Principal, *, business_id: str | None = None,
                             session_registration: Mapping | str | None = None) -> EffectiveAgentConfig:
        snapshot = self._snapshot(principal, business_id)
        provider = getattr(self, "context_home", None)
        home_text = provider(snapshot["business"]["id"] if snapshot["business"] else None) if provider else None
        key = (canonical_hash({"key": snapshot["cache_key"], "context_home": home_text})
               if home_text else snapshot["cache_key"])
        with self._lock:
            config = self._cache.get(key)
        if config is None:
            config = self._compose(snapshot, context_home=home_text)
            with self._lock:
                self._cache[key] = config
                self._cache.move_to_end(key)
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
        data = config.to_dict()
        data["preparation"] = self.preparation_status(snapshot["registration"], data)
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
        return EffectiveAgentConfig(data)

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
        strategy = []
        from bazarlyk.domains.documents.store import split_document
        for doc in snapshot["strategy_notes"]:
            try:
                _, body = split_document(read(doc))
            except DomainError as exc:
                if exc.code != "invalid_document":
                    raise
                raise DomainError("invalid_context", f"Mandatory source has invalid frontmatter: {doc['id']}") from exc
            if not body.strip():
                continue  # an empty optional note delivers nothing; the fingerprint still tracks it
            # The authored body only; registry metadata stays out of plan prose.
            strategy.append((" ".join(str(doc["title"] or "Untitled").split()),
                             ContextSource(doc["id"], body, doc["revision"], doc["content_hash"], str(doc["absolute_path"]))))
        skills = [role_workflow.skill_metadata(), cleanup_workflow.skill_metadata()]
        for package in packages:
            for skill in package.skills:
                if legacy_inbox_skill(package.kind, skill):
                    continue
                skills.append({**skill, "owner": {"kind": package.kind, "id": package.id}, "version": package.version,
                    "digest": package.digest, "generated_path": ".claude/skills/" + skill["name"] + "/SKILL.md",
                    "expected_discoverability": "project_skill_after_preparation"})
        config_id = "sha256:" + snapshot["fingerprint"]
        tools = effective_tool_policy(profile, role, self.registry, packages=packages)
        mandatory = compose_context(profile, role, sources, effective_config_id=config_id,
            workspace=snapshot["row"]["workspace"], business=snapshot["business"],
            allowed_businesses=tuple(snapshot["businesses"]), owner_name=snapshot["owner_name"],
            available_operations=frozenset(tool["canonical_operation"] for tool in tools if tool["available"]),
            context_home=context_home,
            strategy_notes=tuple(strategy))
        visible_scope = plain(profile.to_dict()["scope"])
        if profile.scope.kind == "ecosystem":
            visible_scope["allowed_business_ids"] = [row["id"] for row in snapshot["businesses"]]
        return EffectiveAgentConfig({**profile.to_dict(), "scope": visible_scope, "workspace": snapshot["row"]["workspace"],
            "business_id": snapshot["business"]["id"] if snapshot["business"] else None,
            "business_revision": snapshot["business"]["revision"] if snapshot["business"] else None,
            "effective_config_id": config_id, "configuration_revision": config_id, "context_refresh_required": False,
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
        retired = sorted({item["id"] for item in profile.data["plugins"]} & RETIRED_PLUGIN_IDS)
        if retired:
            raise DomainError("invalid_plugin_config", f"Plugin {retired[0]} is retired; disable it for this agent")
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
            try:
                self.registry.get("signoffs_platform_submit")
            except DomainError as exc:
                raise DomainError("missing_plugin_handler", f"Granted plugin {plugin.id} requires the installed signoffs_platform_submit handler") from exc
            packages.append(plugin)
        return tuple(packages)

    def preparation_status(self, registration: dict | None, config: dict) -> dict:
        if not registration:
            return {"ready": False, "registration_id": None, "status": "unprepared", "projection_drift": [],
                    "missing_artifacts": [], "personal_sources": "not_inspected", "managed_sources": "not_inspected"}
        from bazarlyk.bootstrap.preparation import inspect_projection
        from bazarlyk.bootstrap.client import auth_headers
        projection = json.loads(registration["projection_json"])
        drift = []
        with self.database.read() as uow:
            credential = uow.repository("credentials").get(registration["credential_id"]) if registration["credential_id"] else None
        try:
            token = auth_headers({"workspace": registration["workspace"], "credential_file": ".bazarlyk/credential"})["Authorization"][7:]
        except DomainError:
            token = None
        if (not credential or credential["principal_id"] != config["agent_id"] or credential["revoked_at"]
            or credential["expires_at"] and credential["expires_at"] <= utc_now()
            or token is None or hashlib.sha256(token.encode()).hexdigest() != credential["token_hash"]):
            drift.append("scoped_credential_unavailable")
        from bazarlyk.bootstrap.diagnostics import prepared_helpers
        helpers=prepared_helpers(projection,registration["workspace"])
        if not helpers["ready"]:drift.append(helpers["reason"])
        compatibility = json.loads(registration["compatibility_json"])
        expected_skills = {item["name"]: item["materialization_hash"] for item in config["skills"]}
        if projection.get("selected_skills") != expected_skills:
            drift.append("selected_skills_changed")
        if projection.get("artifact_refs") != {"role": config["role"], "plugins": [{key: item[key] for key in ("id", "version", "digest")} for item in config["plugins"]]}:
            drift.append("artifact_pins_changed")
        from bazarlyk.bootstrap.tool_permissions import changed as tool_permissions_changed
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
            return {"rendered": "# Live Work Briefing\nWork briefing is unavailable in this configuration-only runtime. Use domain tools to retrieve current work.",
                    "available": False}
        return builder.build(principal, config)

    def bootstrap(self, principal: Principal, *, registration_id: str, session_id: str, event: str,
                  last_effective_config_id: str | None = None, runtime: str = "claude-code") -> dict:
        from bazarlyk.bootstrap.tool_permissions import preparation_lock
        with preparation_lock(self.paths, principal.id):
            return self._bootstrap(principal, registration_id=registration_id, session_id=session_id,
                event=event, last_effective_config_id=last_effective_config_id, runtime=runtime)

    def _bootstrap(self, principal: Principal, *, registration_id: str, session_id: str, event: str,
                   last_effective_config_id: str | None, runtime: str) -> dict:
        from bazarlyk.bootstrap.preparation import RUNTIMES
        if runtime not in RUNTIMES:
            raise DomainError("unsupported_runtime", "Unsupported agent CLI")
        if event not in {"startup", "resume", "clear", "compact", "fork", "prompt"} or not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
            raise DomainError("validation_error", "Invalid bootstrap lifecycle or session ID")
        config = self.resolve_agent_config(principal, session_registration={"registration_id": registration_id, "session_id": session_id})
        data = config.to_dict()
        repair_error = None
        from bazarlyk.bootstrap import skill_refresh
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
            from bazarlyk.bootstrap.tool_permissions import refresh
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
                "artifact_pins_changed": "The selected role or plugins changed",
                "tool_configuration_changed": "Generated Claude tool permissions need refreshing",
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
        changed = last_effective_config_id != config.effective_config_id or data["context_refresh_required"]
        result = {"ready": True, "effective_config_id": config.effective_config_id, "refresh_required": changed,
                  "reload_skills": event != "prompt", "preparation": data["preparation"]}
        result["inbox_available"] = any(tool["canonical_operation"] == "inbox_list" and tool["available"] for tool in data["tools"])
        if event != "prompt" or changed:
            result["mandatory_context"] = data["mandatory_context"]
            memory = getattr(self, 'working_memory', None)
            result["work_briefing"] = memory.bootstrap(principal, data) if memory else self.briefing(principal, data)
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
                        rendered = 'Before work, call agent_context_get(inventory=true), follow next_cursor, and read your saved handoff.\n\n' + team
                    if len(rendered) > reserve:
                        rendered = 'Before work, finish agent_context_get(inventory=true) and agents_activity_list continuation pages; read your saved handoff.'
                    result['work_briefing'] = {**briefing, 'rendered': rendered,
                        'character_count': len(rendered), 'bundle_hash': content_hash(rendered.encode())}
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


def resolve_agent_config(resolver: AgentConfigResolver, principal: Principal, *, business_id=None, session_registration=None) -> EffectiveAgentConfig:
    return resolver.resolve_agent_config(principal, business_id=business_id, session_registration=session_registration)
