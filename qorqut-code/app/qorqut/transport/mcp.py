"""The SDK's one stateful HTTP endpoint, with a fresh principal on every request."""

from collections.abc import Mapping
import hashlib
import logging
import re
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
from qorqut.agents.diagnostics import DIAGNOSTIC_OPERATIONS
from qorqut.browser.operations import AGENT_BROWSER_MANAGE, BROWSER_OPERATION, WEBSITE_SIGN_IN
from qorqut.errors import domain_error_from_agentic
from qorqut_agentic.behavior.tool_help import text as instruction_text
from .results import failure
from . import batches, mcp_presentation, mcp_reads

logger = logging.getLogger(__name__)

SLOW_TOOL_MS = 250
# The Task operations whose result tells the coordinator whether the done Task they name has its Action.
CLOSURE_ACTION_HINTS = frozenset({"tasks_get", "tasks_update", "tasks_complete"})


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

def conversation_id(headers, meta):
    """The calling conversation, or None when it cannot be told. Never refuses: the bridge's native session when it sends
    exactly one valid id, else the Codex thread of a shared app-server call. A timeline delivery only dedupes by it."""
    sessions = headers.getlist('x-qorqut-native-session')
    values = meta.model_dump() if hasattr(meta, 'model_dump') else meta
    thread = values.get('threadId') if isinstance(values, dict) else None
    for candidate in (sessions[0] if len(sessions) == 1 else None, thread):
        try:
            if isinstance(candidate, str) and str(UUID(candidate)) == candidate:
                return candidate
        except ValueError:
            pass
    return None


OPTION_SCHEMAS = {
    "expected_revision": {"type": "integer", "minimum": 1,
                          "description": instruction_text('mcp-optional-for-task-writes-checked-when-given-required-when')},
    "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
    "project_id": {"type": "string", "format": "uuid"},
    "review_project_id": {"type": "string", "format": "uuid"},
    "supersedes_signoff_id": {"type": "string", "format": "uuid"},
    "expected_superseded_status_revision": {"type": "integer", "minimum": 1},
}


def split_options(arguments, *, domain_fields=()):
    if not isinstance(arguments, dict):
        raise DomainError("validation_error", "Operation arguments must be an object")
    options = {key: value for key, value in arguments.items() if key in OPTION_SCHEMAS and key not in domain_fields}
    return {key: value for key, value in arguments.items() if key not in options}, InvocationOptions.from_mapping(options)


def own_project(descriptor):
    """Whether ``project_id`` is this operation's own argument and not the scope option, so it stays in the arguments.

    A project operation that requires ``project_id`` names the Project it works on: its handler receives the value and
    the engine selects that project from it. ``agentic_access`` takes it as routing context, which it checks itself
    against the caller's native access. For every other operation ``project_id`` is the option that selects the scope."""
    return descriptor.name == "agentic_access" or (
        "project" in descriptor.scope_kinds and "project_id" in descriptor.input_schema.get("required", ()))


# Argument names agents commonly guess, per canonical operation: {given name: the operation's real name}. Only exact
# synonyms belong here (the value means the same thing and is passed through unchanged); a guessed name with no
# equivalent stays rejected, and the validation error lists what the operation accepts. Each entry is justified by
# rejected calls in agent transcripts; keep the list short.
ARGUMENT_ALIASES = {
    "tasks_list": {"query": "search"},
    "tasks_comment": {"body": "text", "note": "text"},
    "owner_conversation_send": {"message": "text", "body": "text"},
    "fastmail_messages_search": {"query": "q", "text": "q"},
    "fastmail_reply": {"message_id": "reply_message_id"},
    "tables_rows_list": {"search": "query", "q": "query"},
    "conversation_history_search": {"q": "query", "search": "query"},
}
# Transport fields that are never an operation's own argument, so never a name to rename.
TRANSPORT_ARGUMENTS = frozenset({"response_detail", "tool_names", "sections", "if_read_token"})


def rename_arguments(descriptor, arguments):
    """Replace a guessed argument name by the operation's real one, before anything validates, hashes or plans.

    Returns ``(arguments, [{"given", "used"}, ...])``. A name the operation or the transport defines is never renamed,
    and neither is one whose real name was also given (or was given under two guesses): that call is rejected as is."""
    aliases = ARGUMENT_ALIASES.get(descriptor.name)
    if not aliases or not isinstance(arguments, dict):
        return arguments, []
    defined = descriptor.input_schema.get("properties", {})
    renamed, applied = dict(arguments), []
    for given, used in aliases.items():
        if (given not in arguments or used not in defined or given in defined or given in OPTION_SCHEMAS
                or given in TRANSPORT_ARGUMENTS or used in arguments
                or any(other != given and target == used and other in arguments for other, target in aliases.items())):
            continue
        renamed[used] = renamed.pop(given)
        applied.append({"given": given, "used": used})
    return renamed, applied


FIELD_LIMITS = ("type", "enum", "const", "format", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems")
ENUM_LIMIT = 50


def _field_limits(schema, path, properties):
    """The limits the schema states for one failing path, or for the deepest part of it the schema describes."""
    node, found = {"properties": properties}, {}
    for part in path.split("."):
        if not isinstance(node, dict):
            break
        node = node.get("items") if part.isdigit() else (node.get("properties") or {}).get(part)
        if isinstance(node, dict):
            found = node
    limits = {key: plain(found[key]) for key in FIELD_LIMITS if key in found}
    if isinstance(limits.get("enum"), list) and len(limits["enum"]) > ENUM_LIMIT:
        limits["enum"], limits["enum_truncated"] = limits["enum"][:ENUM_LIMIT], True
    return limits


def accepted_arguments(schema, field_errors):
    """What a tool accepts, small enough to ride on its validation error: every top-level argument name, the required
    ones, and the stated limits of only the fields that failed (or were required and missing)."""
    properties = dict(schema.get("properties") or {})
    branches = [branch for keyword in ("oneOf", "anyOf") for branch in schema.get(keyword, []) if isinstance(branch, dict)]
    for branch in branches:
        properties = {**(branch.get("properties") or {}), **properties}
    summary = {"accepted": sorted(properties), "required": sorted(schema.get("required", []))}
    alternatives = [sorted(branch["required"]) for branch in branches if branch.get("required")]
    if alternatives:
        summary["required_one_of"] = alternatives
    fields = {}
    for path, message in field_errors.items():
        # Missing required names are reported on the object itself ("$"), inside the message.
        names = re.findall(r"'([^']+)' is a required property", str(message)) if path == "$" else [path]
        for name in names:
            limits = _field_limits(schema, name, properties) if name.split(".")[0] in properties else {}
            if limits:
                fields[name] = limits
    if fields:
        summary["fields"] = fields
    return summary


def argument_guidance(value, read_schema):
    """Add what the tool accepts to a rejected call's error (``error.details.arguments``), so the caller can correct
    the call from the error alone. Only schema-level rejections (those with ``field_errors``) are described."""
    error = value.get("error")
    if (value.get("status") != "error" or not isinstance(error, dict) or error.get("code") != "validation_error"
            or not isinstance(error.get("field_errors"), dict)):
        return value
    try:
        summary = accepted_arguments(read_schema(), error["field_errors"])
    except Exception:
        # Guidance is optional; the original validation error is the result.
        return value
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    return {**value, "error": {**error, "details": {**details, "arguments": summary}}}


def written_task(context, arguments, result):
    """The context and arguments that name the Task a write changed, as stored: a ``task_id`` given alone runs where the
    engine found it (``locate_scope``), in the id's stored case, which the transport's own context does not know."""
    task = result.data.get("task") if isinstance(result.data, Mapping) else None
    if not isinstance(task, Mapping) or not isinstance(task.get("id"), str) or "project_id" not in task:
        return context, arguments
    project_id = task["project_id"]
    return (replace(context, project_id=project_id, review_project_id=project_id),
            {**arguments, "task_id": task["id"]})


async def validation_hint(value, operation, *, read_instructions):
    """Tell the caller of a rejected known operation how to recover; never replaces an existing hint."""
    error = value.get("error")
    if (operation is None or "agent_hint" in value or value.get("status") != "error"
            or not isinstance(error, dict) or error.get("code") != "validation_error"):
        return value
    try:
        instructions = await anyio.to_thread.run_sync(read_instructions)
        return {**value, "agent_hint": instructions.render('tool-validation-recovery', VALUE0=json.dumps([operation]))}
    except Exception:
        # Optional guidance cannot replace the original validation error. Its
        # filesystem work also must not block unrelated requests on the loop.
        return value


def tool_schema(registry, name):
    descriptor = registry.get(name)
    schema = plain(descriptor.input_schema)
    if descriptor.transport_only:
        return schema
    options = (OPTION_SCHEMAS if descriptor.effect_kind != "read" else
               {key: OPTION_SCHEMAS[key] for key in ("project_id", "review_project_id")})
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


class _Lazy:
    """A membership test over names that are only worked out when a result is actually over the size limit."""

    def __init__(self, names):
        self._read, self._names = names, None

    def __contains__(self, name):
        if self._names is None:
            self._names = frozenset(self._read())
        return name in self._names


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
                from qorqut.agents.operational_delegation import WORKER_OPERATIONS
                allowed = set(assignment["allowed_operations_json"]) | set(WORKER_OPERATIONS)
        try:
            rows = self.runtime.resolver.resolve_agent_config(actor.principal, include_preparation=False).data["tools"]
            visible = [row for row in rows if allowed is None or row["canonical_operation"] in allowed]
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
            # interface too. This cannot expose ordinary project tools.
            context = self.runtime.agent_context(actor.principal, diagnostic=True)
            rows = []
            with self.runtime.database.read() as uow:
                for name in sorted(DIAGNOSTIC_OPERATIONS):
                    try:
                        descriptor = self.runtime.registry.get(name)
                        self.runtime.application.authorize(context,
                            descriptor.project_policy if context.project_id else descriptor, (), uow)
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
        from qorqut.composition.policy import HOST_STAGED_OPERATIONS
        return (not descriptor.owner_only and "mcp" in descriptor.exposure
                and descriptor.name not in HOST_STAGED_OPERATIONS)

    def invocation_context(self, actor, *, canonical_name, options, native_binding=None):
        context = self.runtime.agent_context(actor.principal, diagnostic=canonical_name in DIAGNOSTIC_OPERATIONS,
            project_id=options.project_id,
            session_registration_id=actor.registration_id, review_project_id=options.review_project_id)
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
        self.server = Server("qorqut", version="2.0.0")
        self.manager = StreamableHTTPSessionManager(self.server, json_response=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
            session_idle_timeout=3600, max_request_body_size=2 * 1024 * 1024)

        @self.server.list_tools()
        async def list_tools():
            try:
                actor = self._request_actor(authority=False)
                def discover():
                    self.runtime.require_authority()
                    return self._discover_tools(actor)
                return await anyio.to_thread.run_sync(discover)
            except DomainError as exc:
                raise McpError(types.ErrorData(code=-32000, message=exc.message, data=exc.to_dict())) from exc

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            if name == BATCH_NAME:
                try:
                    await self._authorize_batch()
                    arguments = self.runtime.registry.bind_arguments(name, arguments)
                    batches.validate_entries(arguments)
                    value = await batches.execute(arguments, invoke=self._single_result, authorize=self._authorize_batch)
                except DomainError as exc:
                    value = OperationResult.failure(exc, request_id=new_id()).to_dict()
                except Exception:
                    request_id = new_id()
                    logger.exception("MCP batch failed: %s", request_id)
                    value = OperationResult.failure(DomainError("internal_error", "The batch failed; use its correlation ID for diagnosis"), request_id=request_id).to_dict()
            else:
                value = await self._single_result(name, arguments)
            wire = mcp_presentation.finalize(value)
            return types.CallToolResult(content=self._content_blocks(wire),
                isError=value["status"] == "error")

        # The SDK resolves every called tool in one definition cache shared by all callers of this server and, on a
        # miss, runs the whole tools/list handler inside the tools/call, then replaces the cache with that caller's
        # inventory. Inventories differ per agent and aliases are not always listed, so ordinary calls kept
        # re-running discovery for a definition that is only used for SDK input validation, which is disabled above:
        # every operation validates its own arguments. Dispatch still refuses unexposed and unknown operations.
        async def no_cached_definition(tool_name):
            return None
        self.server._get_cached_tool_definition = no_cached_definition

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
                excluded = {"_mcp_images"}
                if isinstance(private, list):
                    images.extend(item for item in private if isinstance(item, dict))
                    # Only browser image results duplicate their bytes in image blocks.
                    # Other tools, including attachment reads, need content_base64.
                    excluded.add("content_base64")
                return {key: strip(item) for key, item in value.items()
                        if key not in excluded}
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

    async def _single_result(self, name, arguments, actor=None):
        request_id = new_id()
        timing = {"operation": "unknown", "configuration_ms": 0.0, "execution_ms": 0.0}
        started = time.perf_counter()
        try:
            value = await self._single_result_timed(name, arguments, actor, request_id, timing)
            value = self._bounded(name, value, request_id)
            if isinstance(self.caller, RegisteredAgentCaller) and isinstance(value, dict) and value.get("status") == "ok":
                value = await self._extension_notes(name, timing["operation"], value, actor, request_id)
            return value
        finally:
            _log_slow_tool(timing, request_id, started)

    async def _extension_notes(self, name, operation, value, actor, request_id):
        """The result with the ``after_tool`` notes of the agent's extensions beside it (``extension_hooks``). The result
        itself is never changed, and a failure here leaves it as it was. With no extension in effect for the agent, this
        asks the registry once and returns the result as it is."""
        registry = getattr(self.runtime, "extension_registry", None)
        if registry is None or not registry.found:
            return value  # no extension was found at start: nothing is read
        try:
            actor = actor or self._request_actor(authority=False)
            request = self.server.request_context
            headers, meta = request.request.headers, request.meta
        except (DomainError, LookupError, AttributeError):
            return value
        agent_id = actor.principal.id

        def notes():
            from qorqut import extension_hooks
            entries = extension_hooks.tool_entries(extension_hooks.entries_for(registry, agent_id, "after_tool"),
                                                   (name, operation))
            if not entries:
                return value
            used = None
            if any(entry.if_used or entry.unless_used for entry in entries):
                from qorqut.agents.checkpoints import native_checkpoint
                session = conversation_id(headers, meta)
                native = native_checkpoint(self.runtime, agent_id, session) if session else None
                # This call counts as used in the turn; the activity hook notes it only after the result.
                used = [*native.get("used", ()), operation, name] if native else None
            instructions = self.runtime.resolver.require_agentic_view().runtime_instructions()
            noted, composition = extension_hooks.tool_notes(
                value, entries, used, instructions, budget=mcp_presentation.RESULT_CHARACTER_BUDGET,
                size=lambda result: len(mcp_presentation.serialize(mcp_presentation._unprotected(result))))
            if composition is None:
                return value
            from qorqut.composition.locations import agent_workspace_of
            extension_hooks.write_rest(agent_workspace_of(self.runtime.paths, agent_id), composition)
            try:
                from qorqut_operations.contracts import utc_now
                with self.runtime.database.transaction(operation="mcp.extension_deliveries") as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    extension_hooks.record(uow.connection, agent_id, "after_tool", extension_hooks.deliveries(composition),
                                           at=utc_now(), session_id=conversation_id(headers, meta), request_id=request_id)
            except Exception:
                logger.exception("The delivery of extension notes could not be recorded: %s", request_id)
            return noted

        try:
            return await anyio.to_thread.run_sync(notes)
        except Exception:
            logger.exception("Extension notes failed; the result is returned without them: %s", request_id)
            return value

    def _bounded(self, name, value, request_id):
        """The result cut to the size a harness accepts, each cut named in it. Every entry of a batch is bounded by
        itself, so a batch never loses a whole entry's receipt. A failure here must not hide a finished operation."""
        try:
            def narrowing():
                try:
                    return tuple(tool_schema(self.runtime.registry, name).get("properties", {}))
                except Exception:
                    return ()
            bounded, notice = mcp_presentation.bound(value, narrowing=_Lazy(narrowing))
        except Exception:
            logger.exception("MCP result bounding failed: %s", request_id)
            return value
        if notice is not None:
            logger.warning("MCP result cut to fit: operation=%s request=%s full_characters=%s lists=%s texts=%s",
                name, request_id, notice.get("full_characters"),
                [(row["path"], row["returned"], row["of"]) for row in notice.get("lists", [])],
                [(row["path"], row["returned_characters"], row["of_characters"]) for row in notice.get("texts", [])])
        return bounded

    async def _single_result_timed(self, name, arguments, actor, request_id, timing):
        detail = "full"
        canonical_name = name
        known_operation = None
        context = None
        renamed = []
        try:
            # A batch entry arrives with the actor its batch authorization already admitted; a single call binds
            # the request's actor here and passes the per-use authority gate on its worker thread below.
            gate_authority = not actor
            actor = actor or self._request_actor(authority=False)
            descriptor = self.runtime.registry.get(name)
            timing["operation"] = descriptor.name
            if not self.caller.exposes(descriptor):
                raise DomainError("forbidden", "This operation is not exposed to agent MCP")
            from qorqut.overseer.operations import WRITE as OVERSEER_WRITES
            bridge_operation = descriptor.transport_only and descriptor.name in OVERSEER_WRITES
            if descriptor.transport_only and not bridge_operation:
                raise DomainError("validation_error", "Nested transport operations are not supported")
            canonical_name = descriptor.name
            self.caller.check_operation(canonical_name)
            known_operation = canonical_name
            # Guessed argument names become the real ones here, before any validation: request hashing, idempotency
            # receipts and prepared plans below only ever see the operation's own names.
            arguments, renamed = rename_arguments(descriptor, arguments)
            arguments, detail = mcp_presentation.split_detail(canonical_name, arguments)
            arguments, tool_names = mcp_presentation.split_tool_names(canonical_name, arguments, detail)
            arguments, sections = mcp_presentation.split_sections(canonical_name, arguments, detail)
            arguments, read_token = mcp_reads.split(canonical_name, arguments, detail)
            # A Global-only operation may target a project without selecting its
            # scope (for example project_archive). Preserve its declared inputs.
            # An operation that takes project_id as its own argument keeps that
            # one field; ordinary project selectors, including agent_config_get,
            # still route.
            domain_fields = (descriptor.input_schema.get("properties", {}) if descriptor.scope_kinds == {"global"}
                             else ("project_id",) if own_project(descriptor) else ())
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
                # The per-use authority gate reads the database; it runs here, on the worker thread that is about
                # to use that authority, instead of blocking the event loop that serves every other request.
                if gate_authority:
                    self.runtime.require_authority()
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
                            raise domain_error_from_agentic(error) from error
                        return OperationResult.ok(value, request_id=request_id)
                    if canonical_name == WEBSITE_SIGN_IN:
                        self.runtime.registry.bind_arguments(canonical_name, args)
                        namespace = self.runtime.application.namespace(context, descriptor, args)
                        with self.runtime.database.read() as uow:
                            self.runtime.application.authorize(context, descriptor, (), uow, namespace=namespace, arguments=args)
                        from qorqut.browser import sign_in
                        return OperationResult.ok(sign_in.agent_call(self.runtime, actor.principal.id, args), request_id=request_id)
                    if canonical_name == AGENT_BROWSER_MANAGE:
                        self.runtime.registry.bind_arguments(canonical_name, args)
                        namespace = self.runtime.application.namespace(context, descriptor, args)
                        with self.runtime.database.read() as uow:
                            self.runtime.application.authorize(context, descriptor, (), uow, namespace=namespace, arguments=args)
                        # The manager rechecks who is calling under the browser's lock; host effects run here, outside
                        # every database transaction, under the instance's writer admission.
                        from qorqut.browser import setup as browser_setup
                        value = browser_setup.agent_manage(self.runtime, actor.principal.id, args, options.idempotency_key)
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
            if canonical_name in {"tasks_update", "tasks_complete", "attention_add", "todo_add",
                                  "attention_focus_set"} and result.status == "ok":
                # Guidance is optional presentation after commit, outside the
                # database writer. A failed read cannot undo a saved operation.
                try:
                    attention = self.runtime.application.ordubashy_attention
                    hint_context, hint_args = context, args
                    if canonical_name in {"tasks_update", "tasks_complete"}:
                        advisory = attention.task_update_hint
                        hint_context, hint_args = written_task(context, args, result)
                    else:
                        advisory = attention.mutation_hint
                    hint = await anyio.to_thread.run_sync(advisory, hint_context, hint_args, canonical_name)
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
                    "detail": detail, "project_id": context.project_id,
                    "review_project_id": context.review_project_id,
                    "allowed_project_ids": sorted(context.allowed_project_ids),
                    "global_capabilities": sorted(context.global_capabilities),
                    "profile_revision": context.profile_revision, "policy_revision": context.policy_revision,
                    "owner_session_project_id": context.owner_session_project_id,
                    "delegation_binding_id": context.delegation_binding_id,
                    "delegation_assignment_id": context.delegation_assignment_id,
                    "delegation_generation": context.delegation_generation}
                presented = self.read_revalidation.present(requested=read_token, binding=binding,
                    canonical=result.to_dict(), presented=presented)
            if canonical_name == "tasks_get" and result.status == "ok" and isinstance(self.caller, RegisteredAgentCaller):
                # The timelines above the Task ride with the read: the Project, each parent Task, the Task. Agent
                # reads only; the dashboard's and internal reads of a Task stay as they are. Best effort after the read.
                try:
                    request = self.server.request_context
                    task = (result.to_dict().get("data") or {}).get("task")
                    delivery = getattr(self.runtime, "timeline_delivery", None)
                    if delivery and isinstance(task, dict) and isinstance(presented.get("data"), dict):
                        # A not_modified re-read gets the chain only when some level changed for this conversation.
                        send = delivery.chain_if_changed if presented["data"].get("not_modified") else delivery.chain
                        chain = await anyio.to_thread.run_sync(send, actor.principal.id,
                            conversation_id(request.request.headers, request.meta), task)
                        if chain is not None:
                            presented = {**presented, "data": {**presented["data"], "timeline": chain}}
                except Exception:
                    logger.exception("Timeline delivery failed for %s", request_id)
            if canonical_name in CLOSURE_ACTION_HINTS and result.status == "ok":
                presented = await self._closure_action_hint(presented, context, result, request_id)
            return await self._recoverable(presented, known_operation, name, renamed)
        except DomainError as exc:
            result = OperationResult.failure(exc, request_id=request_id)
        except Exception:
            logger.exception("MCP operation failed: %s", request_id)
            result = OperationResult.failure(DomainError("internal_error", "The operation failed; use its correlation ID for diagnosis"), request_id=request_id)
        return await self._recoverable(result.to_dict(), known_operation, name, renamed)

    async def _closure_action_hint(self, presented, context, result, request_id):
        """Tell the coordinator whether a done Task he read or wrote has its Action, after any hint already given.

        Optional presentation after the call's own work, like the hints above: only a done Task is looked at, the
        lookup is a few small reads off the event loop, and a failure leaves the result as it was."""
        try:
            task = result.data.get("task") if isinstance(result.data, Mapping) else None
            if (not isinstance(task, Mapping) or task.get("status") != "done" or not isinstance(task.get("id"), str)
                    or "project_id" not in task or context.principal_kind != "agent"):
                return presented
            from qorqut.domains.tasks.closure_actions import coordinator_hint
            hint = await anyio.to_thread.run_sync(coordinator_hint, self.runtime, context, task)
        except Exception:
            logger.exception("Closure Action hint failed for %s", request_id)
            return presented
        if not hint:
            return presented
        earlier = presented.get("agent_hint")
        return {**presented, "agent_hint": earlier + " " + hint if earlier else hint}

    async def _recoverable(self, value, operation, name, renamed):
        """The result as the caller needs it to correct or learn from this call: which guessed argument names were
        accepted as which real ones, and, for rejected arguments, what the called tool accepts plus the recovery hint."""
        if renamed:
            value = {**value, "renamed_arguments": renamed}
        if operation is not None:
            # ``name`` is the tool as it was called, so an alias is described with its own fixed bindings.
            value = argument_guidance(value, lambda: tool_schema(self.runtime.registry, name))
        return await validation_hint(value, operation,
            read_instructions=lambda: self.runtime.resolver.require_agentic_view().runtime_instructions())

    async def _authorize_batch(self):
        bound = self._request_actor(authority=False)
        authorization = self.server.request_context.request.headers.get("authorization")
        def authorize():
            self.runtime.require_authority()
            actor = self.caller.authenticate(authorization)
            if self.caller.identity(actor) != self.caller.identity(bound):
                raise DomainError("unauthenticated", "Batch authentication no longer matches the request")
            return self.caller.check_batch(actor)
        return await anyio.to_thread.run_sync(authorize)

    def _request_actor(self, *, authority=True):
        """The request's authenticated actor. ``authority=False`` is for a caller that performs the same per-use
        authority check itself on the worker thread it is about to hand the actor to."""
        if authority:
            self.runtime.require_authority()
        request = self.server.request_context.request
        if request is None or "qorqut.auth" not in request.scope:
            raise DomainError("unauthenticated", "No authenticated transport request is bound")
        return request.scope["qorqut.auth"]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        try:
            self.origins.validate(request)
            if request.query_params:
                raise DomainError("invalid_request", "MCP does not accept query credentials or identity selectors")
            authorization = request.headers.get("authorization")
            def admit():
                self.runtime.require_authority()
                return self.caller.authenticate(authorization)
            actor = await anyio.to_thread.run_sync(admit)
            scope["qorqut.auth"] = actor
            transport_identity = actor.delegation_binding_id or actor.credential_id
            scope["user"] = AuthenticatedUser(AccessToken(token=transport_identity, client_id=transport_identity,
                scopes=[self.caller.scope], subject=actor.principal.id, claims={"iss": "qorqut"}))
            await self.manager.handle_request(scope, receive, send)
        except DomainError as exc:
            await failure(exc, new_id())(scope, receive, send)
        except Exception:
            request_id = new_id()
            logger.exception("MCP transport failed: %s", request_id)
            await failure(DomainError("internal_error", "The request failed; use its correlation ID for diagnosis"), request_id)(scope, receive, send)
