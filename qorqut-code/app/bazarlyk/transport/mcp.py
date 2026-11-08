"""The SDK's one stateful HTTP endpoint, with a fresh principal on every request."""

import hashlib
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
import json
from uuid import UUID

import anyio
from mcp import types
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from starlette.requests import Request

from qorqut_operations.contracts import DomainError, InvocationOptions, OperationResult, new_id, plain
from qorqut_operations.application.batches import NAME as BATCH_NAME
from bazarlyk.agents.diagnostics import DIAGNOSTIC_OPERATIONS
# Repair-ticket removal, phase 1: the resolver owns the one list of retained internal readers. Discovery and
# dispatch refuse them here too, including operations_batch entries and tool names a client cached before removal.
from bazarlyk.agents.resolver import INTERNAL_ONLY_OPERATIONS
from bazarlyk.browser.operations import AGENT_BROWSER_MANAGE, BROWSER_OPERATION
from .results import failure
from . import batches, mcp_presentation, mcp_reads

logger = logging.getLogger(__name__)

SLOW_TOOL_MS = 250


@contextmanager
def _phase(timing, key):
    """Record one phase's elapsed milliseconds in this call's own timing dict, including when it fails."""
    started = time.perf_counter()
    try:
        yield
    finally:
        timing[key] = (time.perf_counter() - started) * 1000


def _log_slow_tool(timing, request_id, started):
    # Best effort and fixed fields only: never arguments, responses, headers, principals or exception text. These
    # clocks cover _single_result, not HTTP authentication or the SDK's final wire serialization. The total also
    # includes worker queue wait and presentation, so configuration + execution need not equal it.
    try:
        total = (time.perf_counter() - started) * 1000
        if total >= SLOW_TOOL_MS:
            logger.warning("MCP tool slow: operation=%s request_id=%s configuration_ms=%.1f execution_ms=%.1f total_tool_ms=%.1f",
                timing["operation"], request_id, timing["configuration_ms"], timing["execution_ms"], total)
    except Exception:
        pass


OVERSEER_SESSION_OPERATIONS = frozenset({
    'overseer_evidence_list', 'overseer_episode_get', 'overseer_input_request', 'overseer_input_reconcile',
    'overseer_delta_get', 'overseer_application_get', 'overseer_proposals_list',
    'overseer_application_prepare', 'overseer_application_prepare_merge', 'overseer_application_publish',
    'overseer_application_recover', 'overseer_application_record_checks', 'overseer_application_replan',
    'overseer_application_inspect_satisfaction', 'overseer_development_task_create',
    'overseer_development_task_link', 'overseer_development_tasks_reconcile',
})

ATTENTION_SESSION_OPERATIONS = frozenset({'todo_get', 'todo_add', 'todo_remove', 'attention_get', 'attention_add', 'attention_remove',
    'attention_focus_set', 'attention_focus_clear', 'attention_workflow_update'})


def attention_binding(headers):
    """Parse only the native binding pair provided by the authenticated bridge."""
    sessions = headers.getlist('x-qorqut-native-session')
    generations = headers.getlist('x-qorqut-binding-generation')
    if len(sessions) != 1 or len(generations) != 1:
        raise DomainError('inbox_generation_stale', 'This operation requires one complete native session binding')
    session, generation = sessions[0], generations[0]
    try:
        if str(UUID(session)) != session or not generation.isascii() or not generation.isdecimal():
            raise ValueError
        number = int(generation)
        if str(number) != generation or not 1 <= number < 2**63:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DomainError('inbox_generation_stale', 'The native session binding is malformed') from None
    return session, number

OPTION_SCHEMAS = {
    "expected_revision": {"type": "integer", "minimum": 1},
    "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
    "business_id": {"type": "string", "format": "uuid"},
    "review_business_id": {"type": "string", "format": "uuid"},
    "supersedes_signoff_id": {"type": "string", "format": "uuid"},
    "expected_superseded_status_revision": {"type": "integer", "minimum": 1},
}


def split_options(arguments, *, domain_fields=()):
    if not isinstance(arguments, dict):
        raise DomainError("validation_error", "Operation arguments must be an object")
    options = {key: value for key, value in arguments.items() if key in OPTION_SCHEMAS and key not in domain_fields}
    return {key: value for key, value in arguments.items() if key not in options}, InvocationOptions.from_mapping(options)


def validation_hint(value, operation, *, read_instructions):
    """Point a rejected call of a known operation to its current schema; never replaces an existing hint."""
    error = value.get("error")
    if (operation is None or "agent_hint" in value or value.get("status") != "error"
            or not isinstance(error, dict) or error.get("code") != "validation_error"):
        return value
    return {**value, "agent_hint": (read_instructions().render('tool-validation-recovery', VALUE0=f'{json.dumps([operation])}'))}


def tool_schema(registry, name):
    descriptor = registry.get(name)
    schema = plain(descriptor.input_schema)
    if descriptor.transport_only:
        return schema
    options = (OPTION_SCHEMAS if descriptor.effect_kind != "read" else
               {key: OPTION_SCHEMAS[key] for key in ("business_id", "review_business_id")})
    schema["properties"] = {**options, **schema.get("properties", {})}
    if descriptor.name in mcp_presentation.COMPACT_OPERATIONS:
        schema["properties"]["response_detail"] = (
            mcp_presentation.CONFIG_DETAIL_SCHEMA if descriptor.name == "agent_config_get"
            else mcp_presentation.SUMMARY_DETAIL_SCHEMA if descriptor.name in mcp_presentation.SUMMARY_OPERATIONS
            else mcp_presentation.DETAIL_SCHEMA)
    if descriptor.name == "agent_config_get":
        schema["properties"]["tool_names"] = mcp_presentation.TOOL_NAMES_SCHEMA
        schema["properties"]["sections"] = mcp_presentation.SECTIONS_SCHEMA
    if descriptor.name in mcp_reads.OPERATIONS:
        schema["properties"]["if_read_token"] = mcp_reads.TOKEN_SCHEMA
    required = set(schema.get("required", []))
    if descriptor.effect_kind != "read":
        required.add("idempotency_key")
    for key, value in descriptor.alias_bindings.get(name, {}).items():
        schema["properties"][key] = {**schema["properties"].get(key, {}), "const": plain(value)}
        required.discard(key)
    schema["required"] = sorted(required)
    # Invocation fields belong to the same top-level object as domain selectors.
    # Strict discriminated branches must accept them too; nested schemas retain
    # their original additionalProperties/selector constraints.
    domain_properties = descriptor.input_schema.get("properties", {})
    transport_properties = {key:value for key,value in schema["properties"].items() if key not in domain_properties}
    for keyword in ("oneOf", "anyOf", "allOf"):
        for branch in schema.get(keyword, []):
            if branch.get("type") == "object" or "properties" in branch:
                branch["properties"] = {**transport_properties, **branch.get("properties", {})}
    return schema


def tool_annotations(descriptor):
    return types.ToolAnnotations(readOnlyHint=descriptor.effect_kind == "read",
        destructiveHint=descriptor.effect_kind != "read", idempotentHint=not descriptor.external_read,
        openWorldHint=descriptor.external_read or descriptor.effect_kind == "job")


class RegisteredAgentCaller:
    """Existing registered-agent MCP behavior; other routes install their own caller."""

    scope = "agent"

    def __init__(self, runtime, authentication):
        self.runtime, self.authentication = runtime, authentication

    def authenticate(self, authorization):
        return self.authentication.agent(authorization)

    def identity(self, actor):
        return (actor.principal.id, actor.credential_id, actor.registration_id)

    def inventory(self, actor):
        allowed = None
        if actor.delegation_assignment_id:
            with self.runtime.database.read() as uow:
                assignment = uow.repository("operational_assignments").get(actor.delegation_assignment_id)
                if (assignment is None or assignment["parent_principal_id"] != actor.principal.id
                        or assignment["execution_generation"] != actor.delegation_generation
                        or assignment["state"] != "running"):
                    raise DomainError("unauthenticated", "Delegated worker assignment is no longer active")
                from bazarlyk.agents.operational_delegation import WORKER_OPERATIONS
                allowed = set(assignment["allowed_operations_json"]) | set(WORKER_OPERATIONS)
        try:
            rows = self.runtime.resolver.resolve_agent_config(actor.principal, include_preparation=False).data["tools"]
            visible = [row for row in rows if row["canonical_operation"] not in INTERNAL_ONLY_OPERATIONS
                       and (allowed is None or row["canonical_operation"] in allowed)]
            if allowed is not None and BROWSER_OPERATION in allowed and not any(
                    row["canonical_operation"] == BROWSER_OPERATION for row in visible):
                descriptor = self.runtime.registry.get(BROWSER_OPERATION)
                visible.append({"canonical_operation": BROWSER_OPERATION, "exposed_aliases": [],
                                "available": True, "description": descriptor.description})
            return visible
        except DomainError:
            if allowed is not None:
                # A delegated connection never receives the ordinary repair
                # inventory, which would be an alternate path around its plan.
                return []
            # A broken mandatory document must not hide the small repair
            # interface too. This cannot expose ordinary business tools.
            context = self.runtime.agent_context(actor.principal, diagnostic=True)
            rows = []
            with self.runtime.database.read() as uow:
                for name in sorted(DIAGNOSTIC_OPERATIONS):
                    try:
                        descriptor = self.runtime.registry.get(name)
                        self.runtime.application.authorize(context,
                            descriptor.business_policy if context.business_id else descriptor, (), uow)
                        rows.append({'canonical_operation': name, 'exposed_aliases': [], 'available': True})
                    except DomainError:
                        continue
            return rows

    def check_operation(self, canonical_name):
        pass

    def validate_options(self, actor, canonical_name, options):
        pass

    def validate_arguments(self, actor, canonical_name, args):
        pass

    @staticmethod
    def exposes(descriptor):
        return (descriptor.name not in INTERNAL_ONLY_OPERATIONS
                and not descriptor.owner_only and "mcp" in descriptor.exposure)

    def invocation_context(self, actor, *, canonical_name, options, native_binding=None):
        context = self.runtime.agent_context(actor.principal, diagnostic=canonical_name in DIAGNOSTIC_OPERATIONS,
            business_id=options.business_id,
            session_registration_id=actor.registration_id, review_business_id=options.review_business_id)
        if actor.delegation_binding_id:
            context = replace(context, delegation_binding_id=actor.delegation_binding_id,
                delegation_assignment_id=actor.delegation_assignment_id,
                delegation_generation=actor.delegation_generation)
        if native_binding is not None:
            context = replace(context, inbox_session_id=native_binding[0], inbox_generation=native_binding[1])
        return context

    def attention_binding(self, actor, headers, meta):
        # A process-scoped bridge still supplies the complete, fenced pair.
        # Never use metadata to repair a partial or malformed explicit binding.
        if headers.getlist('x-qorqut-native-session') or headers.getlist('x-qorqut-binding-generation'):
            return attention_binding(headers)
        if headers.getlist('x-qorqut-binding-reason') != ['shared_app_server_without_thread_provenance']:
            return attention_binding(headers)
        # Codex supplies threadId in tools/call _meta, outside model arguments.
        # The shared bridge forwards it unchanged. Match that exact thread to
        # the trusted hook's current owner, not a workspace's latest observation.
        values = meta.model_dump() if hasattr(meta, 'model_dump') else meta
        thread = values.get('threadId') if isinstance(values, dict) else None
        try:
            if not isinstance(thread, str) or str(UUID(thread)) != thread:
                raise ValueError
        except (ValueError, AttributeError):
            raise DomainError('inbox_generation_stale', 'This operation requires the calling Codex thread identity') from None
        if actor.delegation_assignment_id or actor.delegation_binding_id:
            raise DomainError('forbidden', 'Delegated identities cannot use this native session authority')
        with self.runtime.database.read() as uow:
            owner = uow.connection.execute(
                'SELECT consumer_id,binding_json,generation,desired_state FROM native_session_owners WHERE agent_id=?',
                (actor.principal.id,)).fetchone()
        if (owner is None or owner['consumer_id'] != thread or owner['desired_state'] != 'running'):
            raise DomainError('inbox_generation_stale', 'The calling Codex thread is not the selected native session')
        binding = json.loads(owner['binding_json'])
        if (binding.get('registration_id') != actor.registration_id or binding.get('runtime') != 'codex'
                or binding.get('session_id') != thread):
            raise DomainError('inbox_generation_stale', 'The calling Codex registration does not own the native session')
        # Attention.context rechecks this generation in its operation transaction.
        return thread, owner['generation']

    def check_batch(self, actor):
        if actor.delegation_assignment_id:
            raise DomainError("forbidden", "Delegated workers cannot execute batches")
        config = self.runtime.resolver.resolve_agent_config(actor.principal, include_preparation=False)
        if not any(row["canonical_operation"] == BATCH_NAME and row["available"] and row["effective_mode"] == "allow"
                   for row in config.data["tools"]):
            raise DomainError("policy_denied", "Batch execution is not allowed for this agent")
        return actor


class MCPTransport:
    def __init__(self, runtime, authentication, origins, caller=None):
        self.runtime, self.authentication, self.origins = runtime, authentication, origins
        self.caller = caller or RegisteredAgentCaller(runtime, authentication)
        self.read_revalidation = mcp_reads.ReadRevalidation()
        self._discovery_lock = threading.Lock()
        self._discovery_sequence = 0
        self._discovery_generation_sequence = 0
        self._discovery_generation = None
        self._discovery_definitions = {}
        self.server = Server("bazarlyk", version="2.0.0")
        self.manager = StreamableHTTPSessionManager(self.server, json_response=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            session_idle_timeout=3600, max_request_body_size=2 * 1024 * 1024)

        @self.server.list_tools()
        async def list_tools():
            try:
                actor = self._request_actor()
                return await anyio.to_thread.run_sync(lambda: self._discover_tools(actor))
            except DomainError as exc:
                raise McpError(types.ErrorData(code=-32000, message=exc.message, data=exc.to_dict())) from exc

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            if name == BATCH_NAME:
                actor = None
                try:
                    actor = await self._authorize_batch()
                    arguments = self.runtime.registry.bind_arguments(name, arguments)
                    batches.validate_entries(arguments)
                    value = await batches.execute(arguments, invoke=self._single_result, authorize=self._authorize_batch)
                    if value.get("status") == "ok":
                        self._report_transport_success(operation=BATCH_NAME, request_id=value.get("request_id"), actor=actor)
                except DomainError as exc:
                    value = OperationResult.failure(exc, request_id=new_id()).to_dict()
                except Exception as exc:
                    request_id = new_id()
                    self.server.request_context.request.scope["bazarlyk.transport_failed"] = True
                    if not getattr(exc, "_qorqut_incident_reported", False):
                        self._report_transport_failure(operation=BATCH_NAME, request_id=request_id, actor=actor)
                    logger.exception("MCP batch failed: %s", request_id)
                    value = OperationResult.failure(DomainError("internal_error", "The batch failed; use its correlation ID for diagnosis"), request_id=request_id).to_dict()
            else:
                value = await self._single_result(name, arguments)
            wire = mcp_presentation.finalize(value)
            return types.CallToolResult(content=self._content_blocks(wire),
                isError=value["status"] == "error")

    def _discover_tools(self, actor):
        # Sequence requests before taking the snapshot so an older reader cannot
        # replace a generation published by a newer concurrent reader.
        with self._discovery_lock:
            self._discovery_sequence += 1
            sequence = self._discovery_sequence
        view = self.runtime.resolver.require_agentic_view()
        with view.read_snapshot():
            inventory = self.caller.inventory(actor)
            instructions = view.tool_instructions()
            registry = self.runtime.registry
            digest = hashlib.sha256(json.dumps(instructions.messages, sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode("utf-8")).digest()
            generation = (registry, digest)
            frozen = registry.frozen
            def current_generation():
                return (self._discovery_generation is not None
                    and self._discovery_generation[0] is registry
                    and self._discovery_generation[1] == digest)
            with self._discovery_lock:
                if sequence >= self._discovery_generation_sequence:
                    self._discovery_generation_sequence = sequence
                    if not frozen or not current_generation():
                        self._discovery_generation = generation if frozen else None
                        self._discovery_definitions.clear()
            tools = []
            for row in inventory:
                if not row["available"]:
                    continue
                descriptor = registry.get(row["canonical_operation"])
                for name in (row["canonical_operation"], *row["exposed_aliases"]):
                    with self._discovery_lock:
                        definition = (self._discovery_definitions.get(name)
                            if frozen and current_generation() else None)
                    if definition is None:
                        definition = types.Tool(name=name,
                            description=instructions.description((descriptor.description or name.replace("_", " "))
                                + mcp_presentation.description(descriptor.name)),
                            inputSchema=instructions.metadata(tool_schema(registry, name)),
                            annotations=tool_annotations(descriptor))
                        with self._discovery_lock:
                            if frozen and current_generation():
                                definition = self._discovery_definitions.setdefault(name, definition)
                    tools.append(definition.model_copy(deep=True))
            return tools

    @staticmethod
    def _content_blocks(wire):
        """Project private browser image payloads into real MCP content blocks."""
        images = []

        def strip(value):
            if isinstance(value, dict):
                private = value.get("_mcp_images")
                if isinstance(private, list):
                    images.extend(item for item in private if isinstance(item, dict))
                return {key: strip(item) for key, item in value.items()
                        if key not in {"_mcp_images", "content_base64"}}
            if isinstance(value, list):
                return [strip(item) for item in value]
            return value

        text = mcp_presentation.serialize(strip(wire))
        blocks = [types.TextContent(type="text", text=text)]
        for image in images:
            data = image.get("data")
            mime_type = image.get("mime_type")
            if isinstance(data, str) and isinstance(mime_type, str) and mime_type.startswith("image/"):
                blocks.append(types.ImageContent(type="image", data=data, mimeType=mime_type))
        return blocks

    def _report_transport_failure(self, *, operation, request_id, actor=None, context=None):
        reporter = getattr(getattr(self.runtime, "incidents", None), "reporter", None)
        if reporter is None:
            reporter = getattr(getattr(self.runtime, "application", None), "incident_reporter", None)
        if reporter is None:
            return
        principal = getattr(actor, "principal", None)
        try:
            reporter.transport_failure(component="mcp", operation=operation,
                request_id=request_id, context=context,
                agent_id=(principal.id if getattr(principal, "kind", None) == "agent" else None))
        except Exception:
            # Reporting must never replace or recurse into the transport error.
            pass

    def _report_transport_success(self, *, operation, request_id, actor=None, context=None):
        reporter = getattr(getattr(self.runtime, "incidents", None), "reporter", None)
        if reporter is None:
            reporter = getattr(getattr(self.runtime, "application", None), "incident_reporter", None)
        if reporter is None:
            return
        principal = getattr(actor, "principal", None)
        try:
            reporter.transport_success(component="mcp", operation=operation,
                request_id=request_id, context=context,
                agent_id=(principal.id if getattr(principal, "kind", None) == "agent" else None))
        except Exception:
            pass

    async def _single_result(self, name, arguments, actor=None):
        request_id = new_id()
        timing = {"operation": "unknown", "configuration_ms": 0.0, "execution_ms": 0.0}
        started = time.perf_counter()
        try:
            return await self._single_result_timed(name, arguments, actor, request_id, timing)
        finally:
            _log_slow_tool(timing, request_id, started)

    async def _single_result_timed(self, name, arguments, actor, request_id, timing):
        detail = "full"
        canonical_name = name
        known_operation = None
        context = None
        try:
            actor = actor or self._request_actor()
            descriptor = self.runtime.registry.get(name)
            timing["operation"] = descriptor.name
            if not self.caller.exposes(descriptor):
                raise DomainError("forbidden", "This operation is not exposed to agent MCP")
            from bazarlyk.overseer.operations import WRITE as OVERSEER_WRITES
            bridge_operation = descriptor.transport_only and descriptor.name in OVERSEER_WRITES
            if descriptor.transport_only and not bridge_operation:
                raise DomainError("validation_error", "Nested transport operations are not supported")
            canonical_name = descriptor.name
            self.caller.check_operation(canonical_name)
            known_operation = canonical_name
            arguments, detail = mcp_presentation.split_detail(canonical_name, arguments)
            arguments, tool_names = mcp_presentation.split_tool_names(canonical_name, arguments, detail)
            arguments, sections = mcp_presentation.split_sections(canonical_name, arguments, detail)
            arguments, read_token = mcp_reads.split(canonical_name, arguments, detail)
            # A Global-only operation may target a business without selecting its
            # scope (for example business_archive). Preserve its declared inputs;
            # ordinary business selectors, including agent_config_get, still route.
            domain_fields = descriptor.input_schema.get("properties", {}) if descriptor.scope_kinds == {"global"} else ()
            args, options = split_options(arguments, domain_fields=domain_fields)
            self.caller.validate_options(actor, canonical_name, options)
            self.caller.validate_arguments(actor, canonical_name, args)
            requires_binding = (isinstance(self.caller, RegisteredAgentCaller)
                                and canonical_name in (ATTENTION_SESSION_OPERATIONS | OVERSEER_SESSION_OPERATIONS))
            request_context = self.server.request_context if requires_binding else None
            if canonical_name == "agent_config_get" and detail in {"compact", "summary", "context"}:
                # These projections never return the live briefing. Prevent its
                # potentially expensive domain reads from delaying the mandatory
                # context that the caller explicitly requested.
                options = replace(options, omit_work_briefing=True)
            def invoke():
                nonlocal context
                with _phase(timing, "configuration_ms"):
                    binding = (self.caller.attention_binding(actor, request_context.request.headers, request_context.meta)
                               if requires_binding else None)
                    if binding is not None:
                        context = self.caller.invocation_context(actor, canonical_name=canonical_name, options=options,
                                                                 native_binding=binding)
                    else:
                        context = self.caller.invocation_context(actor, canonical_name=canonical_name, options=options)
                # Bind application observation to this transport call's
                # correlation ID after trusted context resolution.
                context = replace(context, request_id=request_id)
                with _phase(timing, "execution_ms"):
                    if bridge_operation:
                        from qorqut_agentic.errors import AgenticError
                        self.runtime.registry.bind_arguments(canonical_name, args)
                        with self.runtime.database.read() as uow:
                            current = self.runtime.application.current_context(context, uow)
                            self.runtime.application.authorize(current, descriptor, (), uow, namespace='global')
                        try:
                            value = self.runtime.overseer.mutate(canonical_name, context, args)
                        except AgenticError as error:
                            raise DomainError(error.code, str(error), details=error.details) from error
                        return OperationResult.ok(value, request_id=request_id)
                    if canonical_name == AGENT_BROWSER_MANAGE:
                        self.runtime.registry.bind_arguments(canonical_name, args)
                        namespace = self.runtime.application.namespace(context, descriptor, args)
                        with self.runtime.database.read() as uow:
                            self.runtime.application.authorize(context, descriptor, (), uow, namespace=namespace, arguments=args)
                        # The manager decides the exact worker target and rechecks it under the agent's lock; host effects run
                        # here, outside every database transaction, under the instance's writer admission.
                        value = self.runtime.browser_manager.manage(actor.principal.id, args, options.idempotency_key)
                        return OperationResult.ok(value, request_id=request_id)
                    if canonical_name == BROWSER_OPERATION:
                        self.runtime.registry.bind_arguments(canonical_name, args)
                        namespace = self.runtime.application.namespace(context, descriptor, args)
                        with self.runtime.database.read() as uow:
                            self.runtime.application.authorize(context, descriptor, (), uow,
                                namespace=namespace, arguments=args)
                        # Projects have no browsers: a call's project context never selects one.
                        body = dict(args)
                        body["idempotency_key"] = options.idempotency_key
                        value = self.runtime.browser_access.tool(actor, body["action"], body)
                        return OperationResult.ok(value, request_id=request_id)
                    return self.runtime.application.invoke(context, name, args, options)
            result = await anyio.to_thread.run_sync(invoke)
            presented = mcp_presentation.present(canonical_name, result.to_dict(), detail,
                tool_names=tool_names, sections=sections, arguments=args)
            if canonical_name in {"tasks_update", "tasks_complete"} and result.status == "ok":
                # A failed advisory read must not turn a committed task update into a tool error.
                try:
                    hint = await anyio.to_thread.run_sync(
                        self.runtime.application.ordubashy_attention.task_update_hint, context, args, canonical_name)
                except Exception:
                    hint = None
                if hint:
                    presented = {**presented, "agent_hint": hint}
            if read_token is not None and result.status == "ok":
                # Authentication, argument binding, scope and policy checks, and
                # the domain read have ALL completed before token comparison.
                binding = {"caller_route": self.caller.scope, "identity": self.caller.identity(actor),
                    "session": self.server.request_context.request.headers.get("mcp-session-id"),
                    "operation": name, "operation_version": descriptor.version, "arguments": args,
                    "detail": detail, "business_id": context.business_id,
                    "review_business_id": context.review_business_id,
                    "allowed_business_ids": sorted(context.allowed_business_ids),
                    "global_capabilities": sorted(context.global_capabilities),
                    "profile_revision": context.profile_revision, "policy_revision": context.policy_revision,
                    "owner_session_business_id": context.owner_session_business_id,
                    "delegation_binding_id": context.delegation_binding_id,
                    "delegation_assignment_id": context.delegation_assignment_id,
                    "delegation_generation": context.delegation_generation}
                presented = self.read_revalidation.present(requested=read_token, binding=binding,
                    canonical=result.to_dict(), presented=presented)
            if presented.get("status") == "ok":
                self._report_transport_success(operation=canonical_name, request_id=request_id,
                    actor=actor, context=context)
            return validation_hint(presented, known_operation,
                read_instructions=lambda: self.runtime.resolver.require_agentic_view().runtime_instructions())
        except DomainError as exc:
            result = OperationResult.failure(exc, request_id=request_id)
        except Exception as exc:
            try:
                self.server.request_context.request.scope["bazarlyk.transport_failed"] = True
            except Exception:
                pass
            if not getattr(exc, "_qorqut_incident_reported", False):
                self._report_transport_failure(operation=canonical_name, request_id=request_id,
                    actor=actor, context=context)
            logger.exception("MCP operation failed: %s", request_id)
            result = OperationResult.failure(DomainError("internal_error", "The operation failed; use its correlation ID for diagnosis"), request_id=request_id)
        return validation_hint(result.to_dict(), known_operation,
            read_instructions=lambda: self.runtime.resolver.require_agentic_view().runtime_instructions())

    async def _authorize_batch(self):
        bound = self._request_actor()
        authorization = self.server.request_context.request.headers.get("authorization")
        def authorize():
            actor = self.caller.authenticate(authorization)
            if self.caller.identity(actor) != self.caller.identity(bound):
                raise DomainError("unauthenticated", "Batch authentication no longer matches the request")
            return self.caller.check_batch(actor)
        return await anyio.to_thread.run_sync(authorize)

    def _request_actor(self):
        self.runtime.require_authority()
        request = self.server.request_context.request
        if request is None or "bazarlyk.auth" not in request.scope:
            raise DomainError("unauthenticated", "No authenticated transport request is bound")
        return request.scope["bazarlyk.auth"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        actor = None
        scope["bazarlyk.request_id"] = new_id()
        scope["bazarlyk.transport_failed"] = False
        try:
            self.origins.validate(request)
            if request.query_params:
                raise DomainError("invalid_request", "MCP does not accept query credentials or identity selectors")
            self.runtime.require_authority()
            actor = await anyio.to_thread.run_sync(lambda: self.caller.authenticate(request.headers.get("authorization")))
            scope["bazarlyk.auth"] = actor
            transport_identity = actor.delegation_binding_id or actor.credential_id
            scope["user"] = AuthenticatedUser(AccessToken(token=transport_identity, client_id=transport_identity,
                scopes=[self.caller.scope], subject=actor.principal.id, claims={"iss": "bazarlyk"}))
            await self.manager.handle_request(scope, receive, send)
            if not scope["bazarlyk.transport_failed"]:
                self._report_transport_success(operation="request", request_id=scope["bazarlyk.request_id"], actor=actor)
        except DomainError as exc:
            await failure(exc, new_id())(scope, receive, send)
        except Exception as exc:
            request_id = new_id()
            if not getattr(exc, "_qorqut_incident_reported", False):
                self._report_transport_failure(operation="request", request_id=request_id, actor=actor)
            logger.exception("MCP transport failed: %s", request_id)
            await failure(DomainError("internal_error", "The request failed; use its correlation ID for diagnosis"), request_id)(scope, receive, send)
