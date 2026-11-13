"""One authenticated invocation path for adapters, workers, and prepared approvals."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
import json

from qorqut_operations.contracts import (ChangeSet, DomainError, InvocationContext, InvocationOptions,
    OperationResult, PolicyDecision, canonical_hash, plain)
from qorqut_operations.global_agents import effective_business_ids, global_profile
from qorqut_operations.storage.directory_layout import ACTION as DIRECTORY_ACTION
from qorqut_operations.storage.receipts import ApplyReceipts, IdempotencyReceipts
from .delegation import authorize_delegation, creation_groups
from .registry import OperationRegistry, capability_not_granted
from .review import ReviewScopeException, review_scope


@dataclass(frozen=True)
class ApprovalBinding:
    signoff_id: str
    payload_hash: str


class Application:
    def __init__(self, database, registry: OperationRegistry, *, policy=None, signoffs=None,
                 context_resolver=None, file_executor=None, writer_admission=None,
                 delegation=None, review_exceptions=(), delegated_creation_groups=()):
        self.database, self.registry = database, registry
        self.policy, self.signoffs = policy, signoffs
        self.context_resolver, self.file_executor = context_resolver, file_executor
        self.writer_admission = writer_admission
        # Typed trusted ports. Delegated execution is refused without a provider.
        self.delegation = delegation
        self.review_exceptions = tuple(review_exceptions)
        # Typed trusted contributions: the only operations whose complete new-record group an assignment need not name.
        self.delegated_creation_groups = creation_groups(delegated_creation_groups)
        if any(type(exception) is not ReviewScopeException for exception in self.review_exceptions):
            raise TypeError("Review exceptions are typed contributions made by trusted assembly code")
        # Optional model-free technical incident observer. Runtime assembly
        # installs this; ordinary applications remain completely unchanged.
        self.incident_reporter = None
        self._target_resolvers = {}

    def register_target_resolver(self, resource_type, resolver):
        if resource_type in self._target_resolvers:
            raise ValueError(f"Target resolver already registered: {resource_type}")
        self._target_resolvers[resource_type] = resolver

    @staticmethod
    def result(context, value):
        metadata = dict(request_id=context.request_id, configuration_revision=context.configuration_revision,
                        context_refresh_required=context.context_refresh_required)
        if isinstance(value, OperationResult):
            return replace(value, **metadata)
        return OperationResult(status="ok", data=value, **metadata)

    def invoke(self, context: InvocationContext, operation_name: str, arguments=None, options=None):
        """Return the same typed envelope on success, denial, validation, or conflict."""
        try:
            with self.database.observe(operation_name, getattr(context, 'request_id', None), job_id=getattr(context, 'job_id', None)):
                result = self.invoke_or_raise(context, operation_name, arguments, options)
                self.observe_result(context,operation_name,result,arguments,options)
                return result
        except DomainError as exc:
            result = OperationResult.failure(exc)
            self.observe_result(context,operation_name,result,arguments,options)
            return (self.result(context, result) if isinstance(context, InvocationContext) else result)
        except Exception as exc:
            observed = self.observe_result(context,operation_name,OperationResult.failure(
                DomainError('internal_error','Operation failed at the application boundary')),arguments,options)
            # A transport boundary may catch this same exception. The
            # application observer already recorded it when configured.
            try:
                setattr(exc, '_qorqut_incident_reported', bool(observed))
            except Exception:
                pass
            raise

    def observe_result(self, context, operation, result, arguments=None, options=None):
        if self.incident_reporter is not None:
            try:
                options = options if isinstance(options, InvocationOptions) else InvocationOptions.from_mapping(options)
                context = self._options_context(context, options)
                namespace = self.namespace(context, self.registry.get(operation), arguments or {})
                if namespace == 'global':
                    context = replace(context, business_id=None, review_business_id=None)
                outcome = self.incident_reporter.observe_result(context,operation,result)
                return isinstance(outcome, dict) and outcome.get('status') != 'unavailable'
            except Exception:
                pass  # A failed observer must never change a business operation's result.
        return False

    def current_context(self, context, uow):
        current = self.context_resolver(context, uow) if self.context_resolver else context
        if not isinstance(current, InvocationContext) or current.principal_id != context.principal_id:
            raise DomainError("invalid_principal", "Context refresh cannot replace the authenticated principal")
        if current.source != context.source or current.request_id != context.request_id:
            raise DomainError("invalid_principal", "Context refresh cannot replace transport provenance")
        if current.owner_session_business_id != context.owner_session_business_id:
            raise DomainError("invalid_principal", "Context refresh cannot replace owner-session provenance")
        if (current.owner_session_business_id is not None
                and (current.owner_session_business_id not in current.allowed_business_ids
                     or current.business_id not in (None, current.owner_session_business_id))):
            raise DomainError("invalid_principal", "Owner-session provenance is outside its bound project")
        trusted_binding = (context.delegation_binding_id, context.delegation_assignment_id,
                           context.delegation_generation)
        refreshed_binding = (current.delegation_binding_id, current.delegation_assignment_id,
                             current.delegation_generation)
        if any(value is not None for value in refreshed_binding) and refreshed_binding != trusted_binding:
            raise DomainError("invalid_principal", "Context refresh cannot replace a delegated worker binding")
        if any(value is not None for value in trusted_binding):
            current = replace(current, delegation_binding_id=context.delegation_binding_id,
                delegation_assignment_id=context.delegation_assignment_id,
                delegation_generation=context.delegation_generation)
        if (current.inbox_session_id, current.inbox_generation) != (context.inbox_session_id, context.inbox_generation):
            current = replace(current, inbox_session_id=context.inbox_session_id, inbox_generation=context.inbox_generation)
        actor = uow.repository("principals").get(current.principal_id)
        if actor is None or not actor["active"] or actor["kind"] != current.principal_kind:
            raise DomainError("invalid_principal", "Principal is missing or inactive")
        if current.principal_kind == "agent":
            profile = uow.connection.execute("SELECT status, revision FROM agent_profiles WHERE agent_id = ?",
                                             (current.principal_id,)).fetchone()
            if profile is None or profile["status"] != "active":
                raise DomainError("inactive_agent", "Agent profile is missing or inactive")
            found = global_profile(uow.connection, current.principal_id)
            # Only current supported globals act; a business-home, unsupported-role, inactive or malformed profile is
            # history, never an executable registration.
            if found is None or current.home_business_id is not None:
                raise DomainError("forbidden", "Only active global agents may invoke operations")
            scope = found["profile"]["scope"]
            if profile["revision"] != current.profile_revision:
                raise DomainError("context_refresh_required", "Agent configuration changed", retryable=True,
                                  current_revision=profile["revision"])
            # A global agent's business access is the active directory in this transaction, never its stored list.
            if not current.allowed_business_ids <= effective_business_ids(uow.connection, current.principal_id):
                raise DomainError("forbidden", "Invocation scope is not granted by the current profile")
            if not current.global_capabilities <= set(scope.get("global_capabilities", ())):
                raise DomainError("forbidden", "Invocation global capabilities are not granted by the current profile")
        uow.actor_id = current.principal_id
        return current.select_business(current.business_id)

    @staticmethod
    def _options_context(context, options):
        current = context.select_business(options.business_id)
        review = options.review_business_id or current.review_business_id or current.business_id
        if review is not None and review not in current.allowed_business_ids:
            raise DomainError("forbidden", "Review project is outside the principal's scope")
        return replace(current, review_business_id=review)

    def namespace(self, context, descriptor, arguments):
        if descriptor.resolve_namespace:
            namespace = descriptor.resolve_namespace(context, arguments)
        elif descriptor.scope_kinds == {"global"}:
            namespace = "global"
        elif context.business_id and "business" in descriptor.scope_kinds:
            namespace = "business:" + context.business_id
        elif "agent_workspace" in descriptor.scope_kinds and context.agent_id:
            namespace = "agent:" + context.agent_id
        else:
            raise DomainError("scope_required", "Operation requires a selected project")
        if namespace == "global":
            if "global" not in descriptor.scope_kinds or not descriptor.global_capability:
                raise DomainError("forbidden", "Operation has no registered global capability")
            if descriptor.global_capability_selected(context.global_capabilities) is None:
                raise capability_not_granted(descriptor)
        elif namespace.startswith("business:"):
            if "business" not in descriptor.scope_kinds:
                raise DomainError("forbidden", "Operation target differs from the selected project")
            if context.business_id:
                if namespace != "business:" + context.business_id:
                    raise DomainError("forbidden", "Operation target differs from the selected project")
            elif not (descriptor.resolve_namespace and namespace.removeprefix("business:") in context.allowed_business_ids):
                raise DomainError("scope_required", "Operation requires a selected project")
        elif namespace.startswith("agent:"):
            if "agent_workspace" not in descriptor.scope_kinds or namespace != "agent:" + (context.agent_id or ""):
                raise DomainError("forbidden", "Agent workspace is outside the principal's scope")
        else:
            raise DomainError("invalid_plan", "Unknown target namespace")
        return namespace

    def authorize(self, context, descriptor, targets, uow, *, namespace=None, arguments=None):
        if context.delegation_assignment_id is not None:
            self.authorize_delegation(context, descriptor, targets, uow,
                namespace=namespace, arguments=arguments)
        owner_session_route = (context.owner_session_business_id is not None
                               and getattr(descriptor, "owner_session", False))
        if owner_session_route and context.business_id != context.owner_session_business_id:
            raise DomainError("forbidden", "Owner-only route is outside the owner-session project")
        if context.source not in descriptor.exposure and not owner_session_route:
            raise DomainError("forbidden", "Operation is not exposed to this caller")
        if descriptor.owner_only:
            membership = (uow.repository("business_memberships", business_id=context.business_id).get(context.principal_id)
                          if context.business_id else None)
            installation = uow.repository("installation").get("service") if namespace == "global" else None
            global_owner = bool(installation and installation["owner_principal_id"] == context.principal_id)
            if context.principal_kind != "human" or not (global_owner or (membership and membership["active"] and membership["is_owner"])):
                raise DomainError("forbidden", "This operation requires the owning human")
            return PolicyDecision("allow", "owner")
        if context.business_id and context.principal_kind == "human":
            membership = uow.repository("business_memberships", business_id=context.business_id).get(context.principal_id)
            if not membership or not membership["active"]:
                raise DomainError("forbidden", "Principal is not an active member of this project")
        if descriptor.plugin_requirement and context.principal_kind == "agent" and namespace != "global":
            if descriptor.plugin_requirement not in {ref.id for ref in context.enabled_plugin_refs}:
                raise DomainError("forbidden", "Required plugin is not enabled")
        if self.policy is None:
            raise DomainError("policy_denied", "No authority policy is installed")
        effective = (descriptor.business_policy
                     if namespace not in (None, "global") and descriptor.global_capability else descriptor)
        decision = self.policy.resolve(context, effective, tuple(targets), uow)
        if not isinstance(decision, PolicyDecision):
            raise DomainError("invalid_policy", "Policy resolver returned an invalid decision")
        if decision.mode == "deny":
            raise DomainError("policy_denied", "Operation is denied by the active policy")
        if decision.mode == "signoff" and not descriptor.supports_signoff:
            raise DomainError("invalid_policy", "Operation cannot stage its exact effects")
        return decision

    def authorize_delegation(self, context, descriptor, targets, uow, *, namespace=None, arguments=None):
        """Intersect live parent authority with one exact saved assignment binding."""
        return authorize_delegation(self.delegation, context, descriptor, targets, uow,
                                    namespace=namespace, arguments=arguments, creation_groups=self.delegated_creation_groups)

    @staticmethod
    def ensure_writes_open(uow):
        state = uow.repository("maintenance_state").get("service")
        if state and state["state"] != "open":
            raise DomainError("maintenance", state["message"] or "Writes are unavailable during maintenance", retryable=True)

    @staticmethod
    def _validate_replay(context, descriptor, arguments, replay, uow):
        """Saved receipts keep exact-once behaviour but never outlive the authority they were issued under."""
        if descriptor.replay_validator is not None and replay.status != "error":
            descriptor.replay_validator(context, arguments, replay, uow)

    def current_decisions(self, context, plan, uow):
        """Current policy decisions for a stored plan's operation and composed effects, without target checks."""
        return self._decisions(context, self.registry.get(plan.operation_name), plan.expected_targets, plan.changes, uow,
                               namespace=plan.target_namespace)

    def _decisions(self, context, descriptor, targets, changes, uow, *, namespace=None):
        names = {descriptor.name, *descriptor.derived_effects}
        names.update(change.operation_name for change in changes if change.operation_name)
        decisions = {}
        for name in sorted(names):
            effect = self.registry.get(name)
            if namespace == "global" and effect.global_capability and effect.global_capability not in context.global_capabilities:
                raise DomainError("forbidden", "Composed effect requires an ungranted global capability")
            decisions[effect.name] = self.authorize(context, effect, targets, uow, namespace=namespace)
        return decisions

    # Stored for the record, never compared: a revision counter or artifact version
    # moves whenever anything about a profile, role or plugin is rewritten, and an
    # installation republishes roles many times a day.  Comparing them invalidated
    # every proposal the owner had not yet reached, for reasons unrelated to the
    # change under review.  What authority actually granted is compared: the mode,
    # and the rest of each decision's binding.  Provenance names the code branch
    # that granted the mode, so renaming one must not strand stored proposals.
    @staticmethod
    def _authority_facts(binding):
        binding = plain(binding)
        if not isinstance(binding, dict):
            return binding
        binding.pop("profile_revision", None)
        binding.pop("policy_revision", None)
        operations = binding.get("operations")
        for entry in operations.values() if isinstance(operations, dict) else ():
            if not isinstance(entry, dict):
                continue
            entry.pop("provenance", None)
            inner = entry.get("binding")
            if isinstance(inner, dict):
                inner.pop("profile_revision", None)
                role = inner.get("role")
                if isinstance(role, dict) and "id" in role:
                    inner["role"] = {"id": role["id"]}
        return binding

    @classmethod
    def _binding_matches(cls, current, prepared):
        return (canonical_hash(current) == canonical_hash(prepared)
                or canonical_hash(cls._authority_facts(current)) == canonical_hash(cls._authority_facts(prepared)))

    @staticmethod
    def _binding(context, decisions):
        return {**({"job_id": context.job_id} if context.job_id is not None else {}),
                "profile_revision": context.profile_revision, "policy_revision": context.policy_revision,
                "operations": {name: {"mode": decision.mode, "provenance": decision.provenance,
                                      "binding": plain(decision.binding)} for name, decision in decisions.items()}}

    def _scope_plan(self, context, plan):
        references = {target.resource for target in plan.expected_targets}
        written = {change.resource for change in plan.changes}
        if len(references) != len(plan.expected_targets):
            raise DomainError("invalid_plan", "Prepared targets must be unique")
        for change in plan.changes:
            if change.resource not in references:
                raise DomainError("invalid_plan", "Every effect must have an expected target")
        tables = self.database.assembly.tables
        file_resources = [change.resource for change in plan.changes if change.kind == "file"]
        if len(set(file_resources)) != len(file_resources):
            raise DomainError("invalid_plan", "A file can have only one exact prepared effect")
        for resource in references:
            if resource.business_id is not None:
                if (resource.business_id not in context.allowed_business_ids
                        or (resource in written and plan.target_namespace != "global"
                            and resource.business_id != context.business_id)):
                    raise DomainError("forbidden", "Prepared target is outside the selected project")
                if resource.namespace not in (None, "business:" + resource.business_id, resource.business_id):
                    raise DomainError("invalid_plan", "Conflicting resource namespace")
            elif resource.namespace == "global":
                if resource in written and plan.target_namespace != "global":
                    raise DomainError("forbidden", "Project operations cannot mutate global resources")
            elif resource.namespace == "ecosystem" and (plan.target_namespace == "global" or resource not in written):
                pass  # A business plan may pin, never write, an ecosystem record such as a global assignee's profile.
            elif resource.namespace and resource.namespace != plan.target_namespace:
                raise DomainError("forbidden", "Prepared resource namespace differs from operation scope")
            elif resource.resource_type in tables and tables[resource.resource_type].scope_column:
                if plan.target_namespace.startswith("business:"):
                    raise DomainError("invalid_plan", "Project resource references require an explicit project ID")

    def _review_scope(self, context, plan):
        return review_scope(context, plan, self.review_exceptions)

    def resolve_target(self, context, resource, uow):
        custom = self._target_resolvers.get(resource.resource_type)
        if custom:
            row = custom(context, resource, uow)
            return dict(row) if row is not None else None
        tables = self.database.assembly.tables
        if resource.resource_type not in tables:
            raise DomainError("invalid_plan", "Prepared resource type has no registered resolver")
        spec = tables[resource.resource_type]
        repository = uow.repository(resource.resource_type, business_id=resource.business_id)
        keys = tuple(key for key in spec.keys if key != spec.scope_column)
        if len(keys) == 1:
            return repository.get(resource.resource_id)
        try:
            key = json.loads(resource.resource_id)
        except (ValueError, TypeError) as exc:
            raise DomainError("invalid_plan", "Composite resource references require a canonical JSON key") from exc
        if not isinstance(key, dict) or set(key) != set(keys):
            raise DomainError("invalid_plan", "Invalid composite resource key")
        return repository.get(key)

    def _rederived_targets(self, plan):
        """Targets whose revision must not be compared because the operation re-derives them.

        A profile, principal or membership row that an operation merely read while
        deciding advances with every unrelated rewrite, and an installation
        republishes and re-adopts roles many times a day.  Where the operation
        re-checks those same facts from current state at approval, comparing the
        counter adds nothing and strands proposals the owner has not yet reached.
        Only the rows the plan does not write, and only where the operation says so.
        """
        declared = self.registry.get(plan.operation_name).rederived_basis
        if not declared:
            return frozenset()
        written = {change.resource for change in plan.changes}
        return {target.resource for target in plan.expected_targets
                if target.resource.resource_type in declared and target.resource not in written}

    def _directory_applier(self):
        applier = getattr(self.file_executor, "file_applier", None) or getattr(self.signoffs, "file_applier", None)
        if applier is None or not hasattr(applier, "verify_directory"):
            raise DomainError("operation_unavailable", "Directory effects need the installed file applier")
        return applier

    def check_targets(self, context, plan, uow, *, receipt_id=None, allow_unverified_files=False):
        files_verified = set()
        recovering_files = {}
        if allow_unverified_files and not receipt_id:
            raise DomainError("invalid_plan", "File recovery requires an exact durable application receipt")
        if receipt_id:
            receipt = ApplyReceipts(uow).get(receipt_id)
            if receipt["payload_hash"] != plan.payload_hash or receipt["change_set"] != plan.canonical:
                raise DomainError("invalid_plan", "Application receipt does not bind this change set")
            for change in plan.changes:
                if change.kind == "file":
                    progress = receipt["progress_json"].get(ApplyReceipts.progress_key(change.resource), {})
                    if not progress.get("verified") or progress.get("after_hash") != change.values.get("after_hash"):
                        if not allow_unverified_files:
                            raise DomainError("apply_incomplete", "Prepared file effects are not verified")
                        recovering_files[change.resource] = change.values
                    else:
                        files_verified.add(change.resource)
                        if change.action == DIRECTORY_ACTION:
                            # A directory is re-proven from its recorded identity each time it is relied on, never from its hash.
                            self._directory_applier().verify_directory(change, progress)
        rederived = self._rederived_targets(plan)
        tables = self.database.assembly.tables
        for target in plan.expected_targets:
            claim = uow.repository("resource_claims").get(ApplyReceipts.resource_key(plan, target.resource))
            if claim and claim["apply_id"] != receipt_id:
                raise DomainError("busy", "Resource has an unfinished application", retryable=True)
            current = self.resolve_target(context, target.resource, uow)
            revision_key = (tables[target.resource.resource_type].revision_column
                            if target.resource.resource_type in tables else "revision")
            revision = current.get(revision_key) if current and revision_key else None
            if target.resource in recovering_files:
                values = recovering_files[target.resource]
                actual = current.get("content_hash") if current else None
                actual = actual.removeprefix("sha256:") if actual is not None else None
                if actual not in (values.get("before_hash"), values.get("after_hash")):
                    raise DomainError("revision_conflict", "Recoverable file target has unexpected content", current_revision=revision)
            elif target.resource not in files_verified:
                if (current is not None) != target.exists:
                    raise DomainError("revision_conflict", "Prepared target existence changed", current_revision=revision)
                if target.content_hash is not None:
                    actual_hash = current.get("content_hash") if current else None
                    if actual_hash is None or actual_hash.removeprefix("sha256:") != target.content_hash.removeprefix("sha256:"):
                        raise DomainError("revision_conflict", "Prepared target content changed", current_revision=revision)
            if (target.revision is not None and revision != target.revision
                    and target.resource not in rederived):
                raise DomainError("revision_conflict", "Prepared target revision changed", current_revision=revision)

    @staticmethod
    def _check_approval(plan, approval, uow):
        if not isinstance(approval, ApprovalBinding) or approval.payload_hash != plan.payload_hash:
            raise DomainError("approval_required", "Exact owner approval is required")
        row = uow.repository("signoffs", business_id=plan.review_business_id).get(approval.signoff_id)
        if (row is None or row["state"] != "approved" or row["payload_hash"] != plan.payload_hash
                or row["change_set"] != plan.canonical or row["proposer_principal_id"] != plan.principal_id):
            raise DomainError("approval_required", "No active approval binds this exact prepared change")

    def validate_prepared(self, context, plan, uow, *, approval=None, receipt_id=None, allow_unverified_files=False,
                          require_approval=True):
        """Revalidate stored authority and targets without executing a domain handler."""
        self.ensure_writes_open(uow)
        context = self.current_context(context, uow)
        descriptor = self.registry.get(plan.operation_name)
        if (plan.principal_id != context.principal_id or descriptor.version != plan.operation_version
                or self.namespace(context, descriptor, plan.arguments) != plan.target_namespace):
            raise DomainError("invalid_plan", "Prepared change is not bound to this caller and operation version")
        self._scope_plan(context, plan)
        if plan.encoding_version == 2 and plan.review_scope != self._review_scope(context, plan):
            raise DomainError("invalid_plan", "Prepared review destination differs from target ownership")
        descriptor.validate(plan.arguments)
        if self.policy is not None and hasattr(self.policy, "validate_plan"):
            self.policy.validate_plan(context, descriptor, plan, uow)
        decisions = self._decisions(context, descriptor, plan.expected_targets, plan.changes, uow, namespace=plan.target_namespace)
        if not self._binding_matches(self._binding(context, decisions), plan.policy_binding):
            raise DomainError("revision_conflict", "Authority configuration changed after preparation")
        if require_approval and (descriptor.signoff_category == "platform_action" or any(decision.mode == "signoff" for decision in decisions.values())):
            self._check_approval(plan, approval, uow)
        if require_approval and any(change.kind == "file" for change in plan.changes) and not receipt_id:
            raise DomainError("apply_incomplete", "File effects require a verified durable application receipt")
        self.check_targets(context, plan, uow, receipt_id=receipt_id, allow_unverified_files=allow_unverified_files)
        if descriptor.validate_targets:
            descriptor.validate_targets(context, plan, uow)
        return context, descriptor

    def apply_prepared(self, context, plan, uow, *, approval=None, receipt_id=None):
        """Finalize only exact validated effects inside the caller's short transaction."""
        # This transaction already owns SQLite's write lock. The outer invocation
        # or file executor takes recovery admission before opening it; taking a
        # filesystem lock here would invert recovery's lock order.
        uow.require_write()
        context, descriptor = self.validate_prepared(context, plan, uow, approval=approval, receipt_id=receipt_id)
        result = self.result(context, descriptor.apply(context, plan, uow))
        if result.status in ("error", "signoff_required"):
            raise DomainError("invalid_result", "An apply handler must complete or enqueue its exact prepared effects")
        return descriptor.validate_output(result)

    def invoke_or_raise(self, context, operation_name, arguments=None, options=None):
        """Run mutations under the installed recovery admission gate."""
        descriptor = self.registry.get(operation_name)
        admission = (self.writer_admission.writer()
                     if descriptor.effect_kind != "read" and self.writer_admission is not None
                     else nullcontext())
        with admission:
            result = self._invoke_or_raise(context, operation_name, arguments, options)
            return descriptor.project_result(result) if descriptor.project_result else result

    def _invoke_or_raise(self, context, operation_name, arguments=None, options=None):
        if not isinstance(context, InvocationContext):
            raise DomainError("invalid_principal", "An authenticated invocation context is required")
        options = options if isinstance(options, InvocationOptions) else InvocationOptions.from_mapping(options)
        descriptor = self.registry.get(operation_name)
        if descriptor.transport_only:
            raise DomainError("operation_unavailable", "This tool requires its authenticated MCP dispatcher")
        arguments = self.registry.bind_arguments(operation_name, {} if arguments is None else arguments)
        if descriptor.effect_kind != "read" and options.idempotency_key is None:
            raise DomainError("validation_error", "Mutations require an idempotency_key")
        with self.database.read() as uow:
            context = self._options_context(self.current_context(context, uow), options)
            required_plugin = self.registry.required_plugin_for(operation_name)
            if (required_plugin and context.principal_kind == "agent"
                    and required_plugin not in {ref.id for ref in context.enabled_plugin_refs}):
                raise DomainError("forbidden", "Requested tool alias requires an enabled plugin")
            namespace = self.namespace(context, descriptor, arguments)
            if namespace == "global":
                context = replace(context, business_id=None, review_business_id=None)
            elif namespace.startswith("business:") and context.business_id is None:
                # A resolver-supplied business target (identity-routed email
                # sends) selects that business for the whole invocation.
                options = replace(options, business_id=namespace.removeprefix("business:"))
                context = self._options_context(context, options)
            self.authorize(context, descriptor, (), uow, namespace=namespace,
                           arguments=arguments if context.delegation_assignment_id is not None else None)
            if descriptor.effect_kind == "read":
                return descriptor.validate_output(self.result(context, descriptor.read(context, arguments, options, uow)))
            input_hash = canonical_hash({"operation": descriptor.name, "version": descriptor.version,
                "arguments": arguments, "options": {**plain(options), "business_id": context.business_id,
                "review_business_id": context.review_business_id, "idempotency_key": None}})
            replay = IdempotencyReceipts(uow).lookup(context.principal_id, namespace, descriptor.name,
                                                     options.idempotency_key, input_hash)
            if replay:
                self._validate_replay(context, descriptor, arguments, replay, uow)
                return self.result(context, replay)
            pending = (self.file_executor.pending(context, descriptor, options, input_hash, uow, namespace=namespace)
                       if self.file_executor and hasattr(self.file_executor, "pending") else None)
            if context.delegation_assignment_id is not None and pending is not None:
                raise DomainError("forbidden", "Delegated operations cannot resume out-of-transaction file effects")
            if pending is None:
                plan = descriptor.prepare(context, arguments, options, uow)
                if (not isinstance(plan, ChangeSet) or plan.operation_name != descriptor.name
                        or plan.operation_version != descriptor.version or plan.principal_id != context.principal_id
                        or plan.target_namespace != namespace or canonical_hash(plan.arguments) != canonical_hash(arguments)):
                    raise DomainError("invalid_plan", "Prepare handler returned an incorrectly bound change set")
                self._scope_plan(context, plan)
                if (context.delegation_assignment_id is not None
                        and any(change.kind == "file" for change in plan.changes)):
                    raise DomainError("forbidden", "Delegated workers are limited to transactionally receipted record effects")
                decisions = self._decisions(context, descriptor, plan.expected_targets, plan.changes, uow, namespace=namespace)
                needs_signoff = descriptor.signoff_category == "platform_action" or any(decision.mode == "signoff" for decision in decisions.values())
                if options.supersedes_signoff_id and not needs_signoff:
                    raise DomainError("validation_error", "Replacing a signoff requires the current policy to stage a new proposal; policy now permits immediate execution")
                review_business = namespace.removeprefix("business:") if namespace.startswith("business:") else None
                if options.review_business_id and options.review_business_id != review_business:
                    raise DomainError("validation_error", "Review destination is determined by resource ownership")
                review_scope = self._review_scope(context, plan)
                plan = replace(plan, policy_binding=self._binding(context, decisions),
                               review_business_id=review_business, review_scope=review_scope, encoding_version=2,
                               proposer_agent_id=context.agent_id,
                               initiated_by_principal_id=context.initiated_by_principal_id)
                if self.policy is not None and hasattr(self.policy, "validate_plan"):
                    self.policy.validate_plan(context, descriptor, plan, uow)
                self.check_targets(context, plan, uow)
                if descriptor.validate_targets:
                    descriptor.validate_targets(context, plan, uow)
        if pending is not None:
            return self.result(context, self.file_executor.execute(self, context, descriptor, pending, options, input_hash))
        if not needs_signoff and any(change.kind == "file" for change in plan.changes):
            if self.file_executor is None:
                raise DomainError("operation_unavailable", "Durable file execution is not installed")
            return self.result(context, self.file_executor.execute(self, context, descriptor, plan, options, input_hash))
        if descriptor.external is not None and not needs_signoff:
            descriptor.external(context, plan)  # Slow provider calls never hold SQLite's write lock.
        with self.database.transaction() as uow:
            self.ensure_writes_open(uow)
            context = self._options_context(self.current_context(context, uow), options)
            if namespace == "global":
                context = replace(context, business_id=None, review_business_id=None)
            receipts = IdempotencyReceipts(uow)
            replay = receipts.lookup(context.principal_id, namespace, descriptor.name, options.idempotency_key, input_hash)
            if replay:
                self.authorize(context, descriptor, (), uow, namespace=namespace)
                self._validate_replay(context, descriptor, arguments, replay, uow)
                return self.result(context, replay)
            if needs_signoff:
                current = self._decisions(context, descriptor, plan.expected_targets, plan.changes, uow, namespace=namespace)
                if not self._binding_matches(self._binding(context, current), plan.policy_binding):
                    raise DomainError("revision_conflict", "Authority configuration changed after preparation")
                self.check_targets(context, plan, uow)
                if self.signoffs is None:
                    raise DomainError("operation_unavailable", "Owner signoff staging is not installed")
                result = self.result(context, self.signoffs.stage(context, descriptor, plan, options, uow))
                if result.status != "signoff_required":
                    raise DomainError("invalid_result", "Signoff staging must return a proposal reference")
            else:
                result = self.apply_prepared(context, plan, uow)
            receipts.save(context.principal_id, namespace, descriptor.name, options.idempotency_key, input_hash, result)
            if context.delegation_assignment_id is not None:
                if self.delegation is None:
                    raise DomainError("forbidden", "Delegated execution is not installed")
                if descriptor.name not in self.delegation.worker_operations:
                    assignment = self.delegation.assignment(uow, context.delegation_assignment_id)
                    if assignment is None:
                        raise DomainError("forbidden", "Delegated assignment is missing")
                    self.delegation.record_committed_action(uow, context.delegation_assignment_id,
                        actor_principal_id=context.principal_id,
                        generation=context.delegation_generation,
                        lease_id=assignment["lease_id"],
                        target_namespace=namespace, action_key=options.idempotency_key,
                        operation_name=descriptor.name, payload_hash=input_hash,
                        delegation_binding_id=context.delegation_binding_id,
                        plan_revision=assignment["plan_revision"],
                        effect_ref={"target_namespace": namespace, "operation_name": descriptor.name,
                                    "input_hash": input_hash})
            return result
