"""Synthetic installed-wheel rehearsal. Run with prepared/venv/bin/python -I -B.

All inputs/outputs belong to the supplied scratch root. Real host/module/MCP
handlers run in-process through ASGI. No socket listener, provider, service
lifetime, native model or product agent starts.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from unittest.mock import patch

from starlette.testclient import TestClient
from qorqut_operations.contracts import Principal, InvocationOptions, new_id
from bazarlyk.agentic import client as native
from bazarlyk.agentic.installation import open_installation
from bazarlyk.agentic.support import digest
from bazarlyk.bootstrap import preparation, skill_refresh
from bazarlyk.bootstrap.initialize import initialize, load_prepared, owner_context
from bazarlyk.bootstrap.mcp_reconnect import ReconnectBridge
from bazarlyk.extensions import installed_selection
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.service.runtime import build_runtime
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication

VERSIONS = {"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.153.4"}


def check(value, message):
    if not value:
        raise AssertionError(message)
    return value


def edited(layer, path, body, actor):
    return layer.authoring.apply({"actor_id": actor, "actor_kind": "agent", "reason": "Synthetic explicit owner instruction"},
        [{"op": "edit", "path": path, "expected_sha256": digest((layer.root / path).read_bytes()), "content": body}])


class OpenClient:
    """A request bridge must not close the shared test ASGI lifespan."""
    def __init__(self, client): self.client = client
    def stream(self, *args, **kwargs): return self.client.stream(*args, **kwargs)
    def close(self): pass


def call(client, registration, operation, arguments=None, context=None):
    request = {"operation": operation, "arguments": arguments or {}}
    if context is not None: request["context"] = context
    output = io.BytesIO()
    bridge = ReconnectBridge(registration, client=OpenClient(client))
    code = native.run(registration, stream=io.BytesIO(json.dumps(request).encode()), output=output, bridge=bridge)
    value = json.loads(output.getvalue())
    check(code == (0 if value["ok"] else 1), value)
    return value


def run(prepared, scratch):
    check(sys.flags.isolated and not os.environ.get("PYTHONPATH"), "Use Python -I without source PYTHONPATH")
    check(Path(sys.prefix).resolve() == prepared / "venv", "Use the prepared host interpreter")
    scratch.mkdir(parents=True, exist_ok=False)
    config = {"extensions": [
        {"factory": "qorqut_content.extension:create", "config": {"schema_version": 1, "enabled": False}},
        {"factory": "qorqut_paper.extension:create", "config": {"command": str(scratch / "absent-paper-command"), "token_root": str(scratch / "paper-tokens")}},
        {"factory": "qorqut_fastmail.extension:create", "config": {"engine_url": "http://127.0.0.1:9", "admin_token_file": str(scratch / "absent-engine-token")}}]}
    selection_file = scratch / "extensions.json"
    selection_file.write_text(json.dumps(config))
    started = time.monotonic()
    receipt = initialize(prepared, scratch / "instance", owner_name="Synthetic Owner", extensions_config=selection_file)
    (scratch / "initialization.json").write_text(json.dumps(receipt, indent=2))
    _, _, components = load_prepared(prepared)
    paths = InstancePaths(scratch / "instance", components)
    selection = installed_selection(config)
    runtime = build_runtime(prepared / "src", paths=paths, extensions=selection)
    layer = runtime.agentic_layer
    manager = Principal(receipt["agents"]["ordubashy"], "agent")
    owner = owner_context(receipt["owner_principal_id"])
    evidence = {"transport": "in-process ASGI with real MCP authentication/registration and descriptor",
                "initialization_seconds": round(time.monotonic() - started, 3), "instance_id": layer.instance_id,
                "selected_extensions": list(selection.ids), "catalog_valid": layer.catalog.validate()["valid"]}
    check(evidence["catalog_valid"], "Invalid initialized catalog")
    check(layer.catalog.read_entry("agentic-management/bootstrap"), "Missing bootstrap")
    check(layer.catalog.read_entry("agentic-management/manage"), "Missing management skill")
    inventory = json.loads((paths.instance_root / "config/agentic-layer.json").read_text())["inventory"]
    check(all(s.get("provenance_kind") == "committed_export" and s["clean"] is None for s in inventory["sources"]), "Export provenance lost")
    evidence["inventory"] = inventory["coverage"]
    evidence["module_runtime_registry"] = "No host execution adapter registered; native client owns LocalProcessRuntime"
    with runtime.owned_preparation(), patch.dict(os.environ, {"SYNTHETIC_SECRET": "not inherited by tools"}), patch("bazarlyk.installation.helper_launcher", return_value=None), \
            patch.object(preparation, "checkpoint_protocols", return_value={"claude-code": 0, "codex": 0}):
        from qorqut_agentic import util as agentic_util
        evidence["yaml_loader"] = agentic_util._StrictLoader.__mro__[1].__name__
        resolution_started = time.perf_counter()
        before = runtime.resolver.resolve_agent_config(manager).to_dict()
        first_resolution = time.perf_counter() - resolution_started
        resolution_started = time.perf_counter()
        repeated = runtime.resolver.resolve_agent_config(manager).to_dict()
        subsequent_resolution = time.perf_counter() - resolution_started
        check(before["effective_config_id"] == repeated["effective_config_id"], "Unchanged resolution changed identity")
        evidence["context_resolution_seconds"] = {"first": round(first_resolution, 4), "subsequent": round(subsequent_resolution, 4)}
        check(max(first_resolution, subsequent_resolution) < 15, "Context resolution exceeds the existing native request budget")
        check(before["mandatory_context"]["rendered"].count("## Agentic Toolkit") == 1, "Duplicate toolkit context")
        # Both a directly modified procedural skill and a new package are immediately visible.
        skill_path = "packages/imported-host-builtins/native/app/bazarlyk/agents/skills/role/SKILL.md"
        edited(layer, skill_path, (layer.root / skill_path).read_text() + "\nSynthetic new direct rule.\n", manager.id)
        fresh = runtime.resolver.resolve_agent_config(manager).to_dict()
        check(fresh["effective_config_id"] != before["effective_config_id"], "Saved skill not reflected in next resolution")
        token_root = scratch / "paper-tokens"
        token_root.mkdir(mode=0o700)
        synthetic_token = token_root / (manager.id + ".token")
        synthetic_token.write_text("synthetic-rehearsal-token-not-a-live-credential")
        synthetic_token.chmod(0o600)
        prepared_manager = preparation.prepare_agent(runtime.resolver, manager, endpoint="http://testserver", versions=VERSIONS,
            interpreter=sys.executable, interpreter_validator=lambda p: Path(p).is_file())
        workspace = Path(prepared_manager["workspace"])
        registration = workspace / ".bazarlyk/registration.json"
        from bazarlyk.bootstrap.email_guidance import home as email_home
        guide = email_home(paths, agentic_view=runtime.agentic_view)
        check(guide == paths.agentic_root / "packages/imported-email-management/guidance/email", "Incorrect canonical email pointer")
        # Safe skill refresh and explicit orientation re-preparation use real peer components.
        edited(layer, skill_path, (layer.root / skill_path).read_text() + "\nSynthetic refresh rule.\n", manager.id)
        changed = runtime.resolver.resolve_agent_config(manager)
        check(skill_refresh.refresh(runtime.resolver, manager, changed, prepared_manager["registration_id"], "codex"), "Skill refresh did not run")
        check(b"Synthetic refresh rule." in (workspace / ".agents/skills/role/SKILL.md").read_bytes(), "Skill refresh lost current body")
        note_path = "packages/imported-host-builtins/native/app/bazarlyk/agentic/assets/inbox-session-note.md"
        edited(layer, note_path, "Current synthetic session note.", manager.id)
        note = runtime.resolver.bootstrap(manager, registration_id=prepared_manager["registration_id"], session_id="synthetic-session", event="prompt", runtime="codex")
        check(note.get("inbox_session_note") == "Current synthetic session note.", "Stale bootstrap session note")
        orientation = "packages/imported-host-builtins/native/app/bazarlyk/agentic/assets/software-ownership.md"
        edited(layer, orientation, "Current synthetic software ownership rule.", manager.id)
        drift = runtime.resolver.resolve_agent_config(manager)
        check("native_guidance_changed" in drift.data["preparation"]["projection_drift"], "No explicit orientation drift")
        check(not skill_refresh.refresh(runtime.resolver, manager, drift, prepared_manager["registration_id"], "codex"), "Orientation silently refreshed")
        preparation.prepare_agent(runtime.resolver, manager, endpoint="http://testserver", versions=VERSIONS,
            interpreter=sys.executable, interpreter_validator=lambda p: Path(p).is_file())
        check(b"Current synthetic software ownership rule." in (workspace / "AGENTS.md").read_bytes(), "Explicit reprepare failed")
        created = runtime.application.invoke(owner, "agents_create", {"role": receipt["role_pins"]["global-worker"],
            "plugins": [], "name": "synthetic-worker", "display_name": "Synthetic Worker", "assignment_brief": "Synthetic verification only",
            "runtime": "codex", "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}}, InvocationOptions(idempotency_key="rehearsal-worker"))
        check(created.status == "ok", created.to_dict())
        worker = Principal(created.data["agent_id"], "agent")
        synthetic_token = token_root / (worker.id + ".token")
        synthetic_token.write_text("synthetic-rehearsal-token-not-a-live-credential")
        synthetic_token.chmod(0o600)
        ready_worker = preparation.prepare_agent(runtime.resolver, worker, endpoint="http://testserver", versions=VERSIONS,
            interpreter=sys.executable, interpreter_validator=lambda p: Path(p).is_file())
        worker_registration = Path(ready_worker["workspace"]) / ".bazarlyk/registration.json"
        runtime.freeze_registry()
        web = create_app(runtime, origins=["http://testserver"], manage_lifecycle=False,
            authentication=Authentication(runtime.database, paths, bootstrap_token="synthetic" + "x" * 40, signing_key=b"z" * 48))
        with TestClient(web) as transport:
            check(transport.post("/mcp", json={}).status_code == 401, "Unauthenticated MCP accepted")
            catalog = call(transport, registration, "catalog.list")
            check(catalog["ok"], catalog)
            authored = {"schema_version": 1, "id": "synthetic-tools", "name": "Synthetic tools", "description": "Isolated rehearsal",
                "entries": [{"id": "echo", "kind": "tool", "path": "echo.json", "description": "Echo", "when_to_use": "Synthetic check"},
                            {"id": "flow", "kind": "workflow", "path": "flow.json", "description": "Progress", "when_to_use": "Synthetic check"},
                            {"id": "new-skill", "kind": "skill", "path": "skills/rehearsal/SKILL.md", "description": "New procedure", "when_to_use": "Synthetic check"}]}
            files = {"agentic.yaml": json.dumps(authored), "skills/rehearsal/SKILL.md": "---\nname: rehearsal\ndescription: Synthetic newly authored procedure.\n---\nFollow the synthetic check.\n",
                "echo.json": json.dumps({"schema_version": 1, "runtime": "python", "code": "echo.py", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}}),
                "echo.py": "import json,sys,os\nv=json.load(sys.stdin)\nprint(json.dumps({'echo':v,'secret':os.environ.get('SYNTHETIC_SECRET')}))\n",
                "flow.json": json.dumps({"schema_version": 1, "id": "flow", "description": "Synthetic workflow", "steps": [{"id": "one", "instructions": "Synthetic first step"}, {"id": "two", "instructions": "Synthetic second step", "depends_on": ["one"]}]})}
            args = {"attribution": {"reason": "Synthetic explicit owner request", "source_refs": ["owner:synthetic"]},
                    "changes": [{"op": "create", "path": "packages/synthetic-tools/" + path, "content": text} for path, text in files.items()]}
            check(not call(transport, worker_registration, "authoring.apply", args)["ok"], "Worker gained management")
            mutation = call(transport, registration, "authoring.apply", args)
            from qorqut_agentic.errors import AgenticError
            check(mutation["ok"] and mutation["result"]["change"]["attribution"]["actor_id"] == manager.id, mutation)
            discovered = call(transport, worker_registration, "catalog.read_entry", {"ref": "synthetic-tools/new-skill"})
            check(discovered["ok"], "New procedural skill not visible on the next read")
            check(not call(transport, registration, "catalog.list", {"root": "/forged"})["ok"], "Root override accepted")
            try:
                layer.tools.invoke("synthetic-tools/echo", {}, "host-runtime-refused", {"actor_id": manager.id, "actor_kind": "agent"})
            except AgenticError as error:
                check(error.code == "runtime_unavailable", error)
            else:
                raise AssertionError("Host registered a privileged runtime")
            invoke = call(transport, worker_registration, "tools.invoke", {"ref": "synthetic-tools/echo", "arguments": {"test": True}, "invocation_id": "synthetic-invoke"})
            check(invoke["ok"] and invoke["result"]["receipt"]["output"] == {"echo": {"test": True}, "secret": None}, invoke)
            tested = call(transport, worker_registration, "tools.test", {"ref": "synthetic-tools/echo", "cases": [{"name": "echo", "input": {"x": 1}, "expect": {"status": "succeeded", "output": {"echo": {"x": 1}, "secret": None}}}]})
            check(tested["ok"] and tested["result"]["passed"], tested)
            # Exercise the configured installed-helper launcher branch with an explicit
            # bounded fixture transport. Real authentication is separately proved above.
            authority = native.NativeClient(worker_registration, bridge=ReconnectBridge(worker_registration, client=OpenClient(transport)))
            access = authority.access("tools.invoke", {})
            authority.close()
            access_path = scratch / "fixture-access.json"
            access_path.write_text(json.dumps(access))
            fixture_runner = scratch / "fixture-client.py"
            fixture_runner.write_text("""import json,sys
from pathlib import Path
from bazarlyk.agentic import client
access=json.loads(Path(sys.argv[1]).read_text())
class Bridge:
 def handle(self,message):
  method=message['method']
  if method.startswith('notifications/'): return None
  if method=='initialize': result={'protocolVersion':'2025-03-26'}
  else:
   args=message['params']['arguments']
   assert args=={'operation':'tools.invoke'}
   result={'isError':False,'content':[{'type':'text','text':json.dumps({'status':'ok','data':access})}]}
  return {'jsonrpc':'2.0','id':message['id'],'result':result}
 def close(self): pass
client.ReconnectBridge=lambda path:Bridge()
raise SystemExit(client.main(sys.argv[3:]))
""")
            fixture_helper = scratch / "fixture-agent-helper"
            fixture_helper.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, '-I', '-B', str(fixture_runner), str(access_path)]) + ' "$@"\n')
            fixture_helper.chmod(0o700)
            with patch("bazarlyk.installation.helper_launcher", return_value=str(fixture_helper)):
                projection = preparation.build_projection(runtime.resolver, runtime.resolver.resolve_agent_config(worker),
                    registration_id=ready_worker["registration_id"], endpoint="http://testserver", interpreter=sys.executable,
                    interpreter_validator=lambda p: Path(p).is_file())
            fixture_launcher = scratch / "fixture-agentic"
            fixture_launcher.write_bytes(projection.files['.bazarlyk/agentic'])
            fixture_launcher.chmod(0o700)
            child = subprocess.run([str(fixture_launcher)], input=json.dumps({"operation":"tools.invoke", "arguments":{
                "ref":"synthetic-tools/echo","arguments":{"launcher":True},"invocation_id":"launcher-echo"}}),
                text=True,capture_output=True,timeout=30,cwd=scratch,
                env={"PATH":os.defpath,"TMPDIR":str(scratch),"PYTHONDONTWRITEBYTECODE":"1"})
            check(child.returncode==0 and json.loads(child.stdout)['result']['receipt']['output']['echo']=={'launcher':True}, child.stdout+child.stderr)
            evidence['configured_launcher']={'installed_client':True,'fixture_transport':True,'http_roundtrip':False,'invocation_id':'launcher-echo'}
            flow = call(transport, worker_registration, "workflows.start", {"ref": "synthetic-tools/flow", "operation_id": "synthetic-flow"})
            check(flow["ok"], flow)
            run_id = flow["result"]["run"]["run_id"]
            progress = call(transport, worker_registration, "workflows.complete_step", {"run_id": run_id, "step_id": "one", "operation_id": "step-one"})
            check(progress["ok"], progress)
            # Remove the package through public authoring; saved progress remains owned.
            layer.authoring.apply({"actor_id": manager.id, "actor_kind": "agent", "reason": "Synthetic revoke"},
                [{"op": "delete", "path": "packages/synthetic-tools/" + path,
                  "expected_sha256": digest((layer.root / "packages/synthetic-tools" / path).read_bytes())} for path in files])
            reopened = call(transport, worker_registration, "workflows.get", {"run_id": run_id})
            check(reopened["ok"] and reopened["result"]["run"]["steps"][0]["state"] == "completed", reopened)
            denied = call(transport, worker_registration, "workflows.complete_step", {"run_id": run_id, "step_id": "two", "operation_id": "step-two"})
            check(not denied["ok"], "Revoked package still executed")
            evidence["native"] = {"authenticated_catalog": True, "authoring_identity": manager.id, "worker_management_denied": True,
                "tool_invocation": invoke["result"]["receipt"]["invocation_id"], "tool_test": tested["result"],
                "saved_run": run_id, "reopened_after_revocation": True, "revoked_execution_denied": True}
        # The real generated default launcher is tested as an isolated subprocess.
        # A malformed override fails before transport, so it contacts no HTTP server.
        launcher = subprocess.run([str(workspace / ".bazarlyk/agentic")], input=json.dumps({"operation": "catalog.list", "arguments": {"root": "/forged"}}),
            text=True, capture_output=True, timeout=20, env={"PATH": os.defpath, "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}, cwd=scratch)
        check(launcher.returncode == 1 and json.loads(launcher.stdout)["error"]["code"] == "invalid_request", launcher.stdout + launcher.stderr)
        evidence["launcher"] = {"default_installed_python": True, "override_refused_before_transport": True, "http_roundtrip": False}
        def notice_count():
            with runtime.database.read() as uow:
                return uow.connection.execute("SELECT count(*) FROM inbox_events WHERE source_type='agentic_changes'").fetchone()[0]
        runtime.inbox_nudges._run_hook()
        count = notice_count()
        edited(layer, note_path, "Known manager edit for notice.", manager.id)
        runtime.inbox_nudges._run_hook()
        check(notice_count() == count + 1, "Known edit notice missing/duplicated")
        (layer.root / "external-rehearsal.md").write_text("Synthetic external edit")
        runtime.inbox_nudges._run_hook()
        check(notice_count() == count + 2, "External notice missing/duplicated")
        runtime.inbox_nudges._run_hook()
        check(notice_count() == count + 2, "Repeated reconcile duplicated notices")
        check(layer.changes.list()["changes"][-1]["attribution"]["actor_id"] == "external/unknown", "Invented external attribution")
        evidence["notices"] = {"known": 1, "external": 1, "repeat_duplicates": 0}
        check(open_installation(paths, selection.ids)[0].instance_id == layer.instance_id, "Legitimate edits refused")
        from bazarlyk.operations_history.catalog import catalog as history_catalog
        from qorqut_operations.history_exports import TECHNICAL_EXCLUDED
        history = history_catalog(selection.history())
        check(history.classifications["agentic_inbox_cursor"][1].kind == TECHNICAL_EXCLUDED, "Technical cursor leaked into public history")
    runtime.domains.shared_context.close()
    runtime.database.close()
    runtime = build_runtime(prepared / "src", paths=paths, extensions=selection)
    with runtime.owned_preparation():
        runtime.inbox_nudges._run_hook()
        with runtime.database.read() as uow:
            actual = uow.connection.execute("SELECT count(*) FROM inbox_events WHERE source_type='agentic_changes'").fetchone()[0]
        check(actual == count + 2, "Reopen duplicated inbox notices")
    evidence["notices"]["reopen_duplicates"] = 0
    runtime.domains.shared_context.close()
    runtime.database.close()
    evidence["seconds"] = round(time.monotonic() - started, 3)
    (scratch / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    args = parser.parse_args()
    run(args.prepared.resolve(), args.scratch.absolute())
