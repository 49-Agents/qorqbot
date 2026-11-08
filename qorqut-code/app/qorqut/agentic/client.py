"""One bounded JSON request in the existing authenticated native terminal.

Only the read-only access lookup crosses MCP. All toolkit effects run locally,
once, after that lookup succeeds. No retry of a local effect occurs here.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import re
import sys

from qorqut_agentic import AgenticLayer
from qorqut_agentic.catalog import Selection
from qorqut_agentic.cli import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, OPERATIONS, dispatch, parse_json
from qorqut_agentic.errors import AgenticError, InvalidRequestError, UnavailableError
from qorqut_agentic.runtime import LocalProcessRuntime
from qorqut_agentic.util import check_operation_id
from qorqut_operations.contracts import DomainError

from qorqut.bootstrap.client import load_registration
from qorqut.bootstrap.mcp_reconnect import ReconnectBridge
from .access import MANAGEMENT_OPERATIONS, validate_context
from . import attention as workflow_attention


_OVERRIDES = frozenset({"actor", "actor_id", "actor_kind", "selection", "root", "state", "instance_id",
                        "runtime", "runtimes", "runtime_config", "git_executable", "rebind_root",
                        "interpreters", "env", "registration_id", "profile_revision", "can_manage"})
_HISTORICAL_TOOLS = frozenset({"tools.receipt", "tools.reconcile"})
_CONTROL_AFTER_REVOCATION = frozenset({"workflows.pause", "workflows.cancel"})
_HISTORICAL_WORKFLOWS = _CONTROL_AFTER_REVOCATION | {"workflows.get", "workflows.list"}
_ATTRIBUTED = frozenset(name for name in OPERATIONS if name.startswith("authoring.")) | {"history.restore"}
# Portable Overseer reads never write; every other allowlisted Overseer operation
# is a local mutation and must be reported as an uncertain effect if its reply is lost.
_OVERSEER_OPERATIONS = frozenset(name for name in OPERATIONS if name.startswith("overseer."))
_OVERSEER_READS = frozenset({
    "overseer.proposals.get", "overseer.proposals.list",
    "overseer.deltas.get", "overseer.deltas.list",
    "overseer.coverage.get", "overseer.materials.get",
    "overseer.inputs.get", "overseer.review.get_batch",
    "overseer.applications.get", "overseer.applications.list",
})
_OVERSEER_MUTATIONS = _OVERSEER_OPERATIONS - _OVERSEER_READS
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ATTENTION_MUTATIONS = frozenset({"workflows.start", "workflows.resume", "workflows.pause", "workflows.cancel",
                                  "workflows.complete_step"})


def request_parts(request):
    if (not isinstance(request, dict) or not {"operation", "arguments"} <= set(request)
            or set(request) - {"operation", "arguments", "context", "attention"}):
        raise InvalidRequestError("request requires operation, arguments and optional context")
    operation, arguments = request["operation"], request["arguments"]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise InvalidRequestError("unknown or installer-only operation")
    if not isinstance(arguments, dict):
        raise InvalidRequestError("arguments must be an object")
    reserved = _OVERRIDES - ({"state"} if operation == "workflows.list" else set())
    if set(arguments) & reserved:
        raise InvalidRequestError("actor, selection, paths and runtime configuration are server-derived")
    if "attribution" in arguments:
        attribution = arguments["attribution"]
        if operation not in _ATTRIBUTED or not isinstance(attribution, dict) or set(attribution) - {"reason", "source_refs"}:
            raise InvalidRequestError("attribution accepts only reason and source_refs")
    context = request.get("context", {})
    try:
        validate_context(context)
    except DomainError as exc:
        raise InvalidRequestError(exc.message) from None
    attention = request.get("attention", {})
    if (not isinstance(attention, dict) or set(attention) - {"task", "intent"}
            or (attention and operation not in {"workflows.start", "workflows.resume"})):
        raise InvalidRequestError("attention accepts only task and intent on workflow start or resume")
    return operation, dict(arguments), dict(context), dict(attention)


def _rpc_result(response, identity):
    if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
            or response.get("id") != identity or "error" in response or "result" not in response):
        # Never echo a provider/transport message, URL, header or exception.
        raise AgenticError("Authenticated MCP request failed", code="access_unavailable")
    return response["result"]


def _access_payload(result):
    if not isinstance(result, dict):
        raise AgenticError("Invalid MCP access envelope", code="access_unavailable")
    payload = result.get("structuredContent")
    if payload is None:
        blocks = result.get("content")
        if (not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict)
                or blocks[0].get("type") != "text" or not isinstance(blocks[0].get("text"), str)):
            raise AgenticError("Invalid MCP access envelope", code="access_unavailable")
        try:
            payload = parse_json(blocks[0]["text"].encode("utf-8"))
        except AgenticError:
            raise AgenticError("Invalid MCP access envelope", code="access_unavailable") from None
    if not isinstance(payload, dict):
        raise AgenticError("Invalid MCP access envelope", code="access_unavailable")
    if result.get("isError") or payload.get("status") != "ok":
        code = payload.get("error", {}).get("code") if isinstance(payload.get("error"), dict) else None
        raise AgenticError("Current native toolkit access was refused",
                           code=code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "access_unavailable")
    if not isinstance(payload.get("data"), dict):
        raise AgenticError("Invalid MCP access payload", code="access_unavailable")
    return payload["data"]


class NativeClient:
    def __init__(self, registration_path, *, bridge=None):
        self.registration_path = Path(registration_path)
        if not self.registration_path.is_absolute():
            raise InvalidRequestError("--registration must be an explicit absolute path")
        self.bridge = bridge
        self.initialized = False
        self.request_id = 0
        self.local_effect_started = False

    def _call(self, method, params=None):
        self.request_id += 1
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            message["params"] = params
        return _rpc_result(self.bridge.handle(message), self.request_id)

    def access(self, operation, context):
        registration = load_registration(self.registration_path)
        if self.bridge is None:
            self.bridge = ReconnectBridge(self.registration_path)
        if not self.initialized:
            self._call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "qorqut-agentic-native", "version": "1"}})
            self.bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.initialized = True
        value = _access_payload(self._call("tools/call", {
            "name": "agentic_access", "arguments": {"operation": operation, **context}}))
        required = {"operation", "actor", "selection", "root", "state", "instance_id", "can_manage",
                    "profile_revision", "registration_id", "attention_required"}
        if set(value) != required:
            raise AgenticError("Invalid native access binding", code="access_unavailable")
        if (value["operation"] != operation or value["registration_id"] != registration["registration_id"]
                or value["actor"] != {"actor_id": registration["agent_id"], "actor_kind": "agent"}
                or type(value["can_manage"]) is not bool or type(value["attention_required"]) is not bool or type(value["profile_revision"]) is not int
                or value["profile_revision"] < 1 or not isinstance(value["instance_id"], str)
                or re.fullmatch(r"inst_[0-9a-f]{32}", value["instance_id"]) is None):
            raise AgenticError("Native access identity does not match registration", code="access_unavailable")
        for field in ("root", "state"):
            if not isinstance(value[field], str) or not Path(value[field]).is_absolute():
                raise AgenticError("Invalid native access paths", code="access_unavailable")
        selection = value["selection"]
        if not isinstance(selection, dict) or not isinstance(selection.get("package_ids"), list):
            raise AgenticError("Native access must supply explicit eligible packages", code="access_unavailable")
        Selection.from_value(selection)
        if any(selection.get(field) != context.get(field) for field in ("project_id", "work_type")):
            raise AgenticError("Native routing does not match authorized context", code="access_unavailable")
        if operation in MANAGEMENT_OPERATIONS and not value["can_manage"]:
            raise UnavailableError("Toolkit management is not authorized")
        return value

    @staticmethod
    def _same_binding(first, current):
        if any(first[key] != current[key] for key in ("actor", "root", "state", "instance_id", "registration_id", "attention_required")):
            raise AgenticError("Native access binding changed during request", code="access_unavailable")

    def execute(self, request):
        self.local_effect_started = False
        operation, arguments, context, attention = request_parts(request)
        user_arguments = dict(arguments)
        existing_workflow = operation.startswith("workflows.") and operation != "workflows.start"
        # Existing runs carry their own routing; histories/receipts remain owned
        # after that original project's grant changes. No caller context can
        # substitute for the frozen run context before progress.
        access = self.access(operation, {} if existing_workflow or operation in _HISTORICAL_TOOLS else context)
        # Runtime construction and the store open happen only after authenticated access.
        runtime = LocalProcessRuntime({"python": sys.executable}, env={})
        layer = AgenticLayer.open(access["root"], access["state"], runtimes={"python": runtime},
                                  attribution_source="host_authenticated")
        if layer.instance_id != access["instance_id"]:
            raise AgenticError("Local toolkit instance does not match authenticated access", code="state_mismatch")
        actor, selection = access["actor"], access["selection"]
        if operation == "workflows.list":
            return self._list_runs(layer, arguments, access, context)
        if existing_workflow:
            run = layer.workflows.get(arguments.get("run_id"))["run"]
            self._own_run(run, actor)
            if any(run["selection"].get(field) != value for field, value in context.items()):
                raise InvalidRequestError("request context contradicts the workflow's saved context")
            if operation not in _HISTORICAL_WORKFLOWS:
                access = self._eligible_run(layer, run, access, operation, arguments.get("step_id"))
        if operation in _ATTRIBUTED:
            attribution = arguments.get("attribution")
            if attribution is None:
                raise InvalidRequestError("management attribution requires reason and optional source_refs")
            arguments["attribution"] = {**attribution, **actor}
        component, method = OPERATIONS[operation]
        target = layer
        if component is not None:
            # Allowlisted components may be nested groups (overseer.proposals).
            for part in component.split("."):
                target = getattr(target, part)
        target = getattr(target, method)
        parameters = inspect.signature(target).parameters
        if "actor" in parameters:
            arguments["actor"] = actor
        if "selection" in parameters:
            # Historical own receipts stay reconcilable when packages are revoked.
            arguments["selection"] = None if operation in _HISTORICAL_TOOLS else selection
        if operation == "catalog.list" and arguments.get("include_unavailable") is True:
            raise InvalidRequestError("native catalog listing cannot include unavailable packages")
        self.local_effect_started = (operation.startswith(("authoring.", "tools.", "workflows."))
                                     and operation not in {"tools.receipt", "workflows.get", "workflows.list"}
                                     or operation in {"history.commit", "history.restore", "changes.scan",
                                                      "changes.advance_cursor", "lifecycle.recover"}
                                     or operation in _OVERSEER_MUTATIONS)
        if access["attention_required"] and operation in _ATTENTION_MUTATIONS:
            result = self._attention_mutation(layer, operation, arguments, context, attention, access, user_arguments=user_arguments)
        else:
            result = dispatch(layer, {"operation": operation, "arguments": arguments})
        if operation == "catalog.list":
            # The module's catalog issues describe the entire tree. Return only
            # diagnostics belonging to currently eligible packages here.
            result["issues"] = [issue for issue in result["issues"]
                                if issue.get("package_id") in selection["package_ids"]]
        return result

    def _attention_mutation(self, layer, operation, arguments, routing, attention, access, *, user_arguments=None):
        operation_id = check_operation_id(arguments.get("operation_id"))
        registration = load_registration(self.registration_path)
        bound = workflow_attention.binding(registration)
        request_hash = workflow_attention.fingerprint({"operation": operation, "arguments": user_arguments if user_arguments is not None else arguments,
                                                        "routing": routing, "attention": attention})
        with workflow_attention.receipt(registration, access["instance_id"], access["actor"]["actor_id"], operation_id) as store:
            saved = store.read()
            if saved is not None:
                if saved.get("request_hash") != request_hash:
                    raise AgenticError("Workflow operation id was used for a different request", code="operation_conflict")
                if saved.get("binding") != bound or saved.get("registration_id") != access["registration_id"]:
                    raise AgenticError("Workflow receipt belongs to a different native binding", code="access_unavailable")
                if saved.get("selection") != access["selection"] and saved["phase"] == "prepared":
                    raise AgenticError("Workflow outcome remains uncertain; original access changed before reconciliation",
                                       code="access_unavailable", details={"outcome_unknown": True, "operation_id": operation_id})
            else:
                params = {"scope": "global", "operation": operation, "operation_id": operation_id, **bound, **attention}
                if operation != "workflows.start":
                    params["run_id"] = arguments.get("run_id")
                snapshot = _access_payload(self._call("tools/call", {"name": "attention_workflow_context", "arguments": params}))
                payload = snapshot.get("payload")
                if (snapshot.get("version") != 1 or not isinstance(payload, dict)
                        or payload.get("instance_id") != access["instance_id"]
                        or payload.get("operation") != operation or payload.get("operation_id") != operation_id):
                    raise AgenticError("Workflow attention instance does not match native access", code="state_mismatch")
                saved = {"phase": "prepared", "request_hash": request_hash, "selection": access["selection"],
                         "registration_id": access["registration_id"], "binding": bound, "context": snapshot,
                         "dispatch_arguments": arguments}
                store.write(saved)
            if saved["phase"] == "prepared":
                result = dispatch(layer, {"operation": operation, "arguments": saved["dispatch_arguments"]})
                run_id = result["run"]["run_id"]
                saved.update(phase="sync_pending", run_id=run_id)
                store.write(saved)
            else:
                run_id = saved["run_id"]
                result = {"run": layer.workflows.get(run_id)["run"], "operation_id": operation_id}
                self._own_run(result["run"], access["actor"])
            if saved["phase"] == "sync_pending":
                sync = {"scope": "global", "operation": operation, "operation_id": operation_id, "run_id": run_id,
                        "context": saved["context"], "idempotency_key": "workflow-attention-" + request_hash}
                try:
                    result["attention"] = _access_payload(self._call("tools/call", {"name": "attention_workflow_sync", "arguments": sync}))
                    saved["phase"] = "settled"
                    saved.pop("context", None)
                    saved.pop("selection", None)
                    saved.pop("dispatch_arguments", None)
                    store.write(saved)
                except Exception:
                    raise AgenticError("Workflow completed locally; attention synchronization is pending",
                                       code="attention_sync_pending", details={"local_operation_completed": True,
                                       "run_id": run_id, "operation_id": operation_id}) from None
            return result

    @staticmethod
    def _own_run(run, actor):
        if run["actor"] != actor:
            raise UnavailableError("workflow run is owned by another actor")

    def _eligible_run(self, layer, run, access, operation, step_id):
        # The public snapshot supplies original routing. Re-authorize that project
        # instead of trusting a caller's newly supplied or omitted context.
        saved = run["selection"]
        context = {field: saved[field] for field in ("project_id", "work_type") if saved.get(field) is not None}
        current = self.access(operation, context)
        self._same_binding(access, current)
        selection = current["selection"]
        if run["workflow_ref"].split("/", 1)[0] not in selection["package_ids"]:
            raise UnavailableError("the workflow's original package is no longer eligible")
        # Frozen instructions remain frozen. Check only the actual verifier of
        # the affected step; unrelated catalog edits cannot rewrite this run.
        for step in run["steps"]:
            if step["id"] == step_id and "verifier" in step:
                layer.catalog.read_entry(step["verifier"], selection=selection)
        return current

    def _list_runs(self, layer, arguments, access, context):
        if set(arguments) - {"state", "limit", "offset"}:
            raise InvalidRequestError("unknown workflow list argument")
        state, limit, offset = arguments.get("state"), arguments.get("limit", 100), arguments.get("offset", 0)
        if not context:
            return layer.workflows.list(state=state, limit=limit, offset=offset, actor=access["actor"])
        # Validate with the public API before filtering, without exposing its count.
        page = layer.workflows.list(state=state, limit=limit, offset=0, actor=access["actor"])
        if type(offset) is not int or offset < 0:
            raise InvalidRequestError("offset must be a nonnegative integer")
        visible, scanned = [], 0
        while True:
            for run in page["runs"]:
                if all(run["selection"].get(field) == value for field, value in context.items()):
                    visible.append(run)
            scanned += len(page["runs"])
            if scanned >= page["total"] or not page["runs"]:
                break
            page = layer.workflows.list(state=state, limit=1000, offset=scanned, actor=access["actor"])
        return {"runs": visible[offset:offset + limit], "total": len(visible)}

    def close(self):
        if self.bridge is not None:
            self.bridge.close()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidRequestError("Expected --registration with an explicit registration path")


def run(registration_path, *, stream, output, bridge=None):
    client = None
    try:
        raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise AgenticError("JSON input exceeds the byte limit", code="limit_exceeded")
        request = parse_json(raw)
        client = NativeClient(registration_path, bridge=bridge)
        # Unexpected local failures are conservatively uncertain. Read-only access
        # success never claims a subsequent local operation completed.
        result = client.execute(request)
        envelope, code = {"ok": True, "result": result}, 0
    except AgenticError as exc:
        envelope, code = {"ok": False, "error": exc.to_dict()}, 1
    except DomainError:
        envelope, code = {"ok": False, "error": {"code": "access_unavailable",
                           "message": "Native registration or access is unavailable", "details": {}}}, 1
    except Exception:
        envelope, code = {"ok": False, "error": {"code": "internal_error",
            "message": "Native toolkit request failed; inspect history or receipts before retrying",
            "details": {"outcome_unknown": bool(client and client.local_effect_started)}}}, 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass  # Transport cleanup cannot replace the local operation's envelope.
    try:
        data = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        data, code = b'{"ok":false,"error":{"code":"internal_error","message":"Result is not JSON; inspect receipts before retrying","details":{"outcome_unknown":true}}}', 1
    if len(data) > MAX_OUTPUT_BYTES:
        original = envelope.get("error", {})
        details = {key: value for key, value in original.get("details", {}).items()
                   if key in {"operation_completed", "attempt_recorded", "outcome_unknown"} and type(value) is bool}
        if envelope["ok"]:
            details["operation_completed"] = True
        data = json.dumps({"ok": False, "error": {
            "code": original.get("code", "limit_exceeded"),
            "message": "Result exceeds the byte limit; inspect history or receipts before retrying",
            "details": {**details, "truncated": True, "limit": MAX_OUTPUT_BYTES},
        }}).encode("utf-8")
        code = 1
    output.write(data + b"\n")
    return code


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--registration", required=True)
    try:
        args = parser.parse_args(argv)
    except InvalidRequestError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": exc.to_dict()}) + "\n")
        return 1
    return run(args.registration, stream=sys.stdin.buffer, output=sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
