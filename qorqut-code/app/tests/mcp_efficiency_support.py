"""Hermetic service packages shared by transport tests and the offline benchmark.

All files, agents, credentials and records live under the supplied temporary
directory. No companion repositories, installed roles, providers or live data.
"""
from contextlib import contextmanager, ExitStack
import json
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from qorqut_operations.contracts import new_id
from inbox_support import claim


EVIDENCE = ("The pilot store reconciled 120 inventory rows against its supplier sheet. "
            "Two products still have ambiguous pack sizes; do not publish those quantities. "
            "The remaining rows match. Ask the owner to confirm the supplier unit convention "
            "before approving the rollout, and retain the original evidence for follow-up.")


@contextmanager
def isolated_service(root):
    import test_agent_configuration as configuration
    import test_service_transports as transports
    from instance_support import synthetic_paths

    workspace = root / "workspace"
    for asset in ("js", "css"):
        (workspace / "biz-dashboard" / asset).mkdir(parents=True)
    sources = {}
    policy = dict.fromkeys((
        "agent_config_get", "agents_activity_list", "agent_work_update",
        "operations_batch", "operations_describe", "tasks_create", "tasks_update",
        "tasks_get", "tasks_list", "tasks_comment", "tasks_notes_list", "inbox_list",
        "agents_message_send", "agents_messages_list", "focus_get", "focus_present",
        "focus_reply", "owner_conversation_get", "owner_conversation_send",
        "stories_create", "stories_get", "stories_link_add", "stories_event_get",
        "tables_create", "tables_rows_write", "tables_rows_list",
        "signoffs_list", "signoffs_get", "signoffs_input_create",
    ), "allow")
    for name in ("business-agent", "global-marketing", "ordubashy", "founder"):
        ecosystem = name != "business-agent"
        folder = workspace / "agent-roles" / "roles" / name
        folder.mkdir(parents=True)
        (folder / "brief.md").write_text(
            f"# {name.title()}\nOwn measurable progress with current evidence. "
            "Read the complete business instructions on entry and after refresh. "
            "Report blockers accurately and read linked evidence before deciding.\n")
        (folder / "role.yaml").write_text(json.dumps({
            "schema_version": 1, "id": name, "version": "1.0.0",
            "scope_kinds": ["ecosystem" if ecosystem else "business"], "skills": [],
            "brief": f"roles/{name}/brief.md", "scope_brief": "ecosystem-agent" if ecosystem else "business-agent",
            "default_tool_policy": policy,
        }))
        sources[name] = {"kind": "role", "path": "agent-roles", "manifest": f"roles/{name}/role.yaml"}
    source_map = workspace / "biz-core" / "package-sources.json"
    source_map.parent.mkdir(parents=True)
    source_map.write_text(json.dumps(sources))
    with ExitStack() as stack:
        paths = synthetic_paths(workspace, app=workspace / "biz-core", dashboard=workspace / "biz-dashboard",
                                roles=workspace / "agent-roles")
        for module in (configuration, transports):
            stack.enter_context(patch.object(module, "REPO", workspace))
            stack.enter_context(patch.object(module, "PATHS", paths))
        stack.enter_context(patch.dict("os.environ", {
            "QORQUT_PACKAGE_SOURCES": str(source_map),
            "QORQUT_TEST_AGENT_ROLES_ROOT": str(workspace / "agent-roles"),
            "QORQUT_TEST_FILER_LIVE": "0",
        }))
        service = transports.ServiceFixture(root, full=True)
        try:
            yield service
        finally:
            service.close()


def seed_reads(service):
    """Build modest, real records. Never pad producer results to improve savings."""
    from test_owner_private_conversations import create_ordubashy

    fixture, app = service.fixture, service.runtime.application
    service.ordu = create_ordubashy(fixture, [fixture.business_id])
    with fixture.db.transaction() as uow:
        installation = uow.repository("installation").get("service")
        uow.repository("installation").update("service", {"management_coordinator_id": service.ordu.id},
            expected_revision=installation["revision"])
    ready = fixture.prepare(service.ordu)
    service.tokens[service.ordu.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()
    service.registrations[service.ordu.id] = ready["registration_id"]
    coordinator = service.runtime.resolver.invocation_context(service.ordu)
    with fixture.db.read() as uow:
        manager = service.runtime.policy.context_for(service.manager.id, business_id=fixture.business_id,
                                                     review_business_id=fixture.business_id, uow=uow)
    owner_global = replace(fixture.ctx, business_id=None, review_business_id=None)

    def call(ctx, operation, args, **options):
        result = app.invoke(ctx, operation, args, {"idempotency_key": new_id(), **options}).to_dict()
        assert result["status"] == "ok", result
        return result["data"]

    tasks = []
    for title in ("Confirm supplier units", "Publish stock preview", "Review pilot feedback"):
        task = call(manager, "tasks_create", {"title": title})["task"]
        call(manager, "tasks_comment", {"task_id": task["id"], "text": EVIDENCE}, expected_revision=task["revision"])
        tasks.append(task)
    call(manager, "tasks_update", {"task_id": tasks[1]["id"], "changes": {"blocked_by": [tasks[0]["id"]]}}, expected_revision=2)
    sources = [{"type": "task", "id": task["id"], "business_id": fixture.business_id} for task in tasks]
    cards = []
    for index, source in enumerate(sources):
        cards.append(call(owner_global, "focus_present", {"scope": "global", "source": source,
            "display_name": tasks[index]["title"], "reason": "Pilot rollout needs the owner's review.",
            "requested_response": "Confirm the supplier unit convention.", "blocker": index == 0,
            "urgent": False, "references": [sources[(index + 1) % len(sources)]]})["item"])
    cards.append(call(owner_global, "focus_present", {"scope": "global",
        "reference": {"key": "preview", "title": "Stock preview", "target": {"type": "url", "url": "https://example.test/preview"}},
        "context": sources[1], "reason": "Check the customer-facing preview against its task evidence.",
        "requested_response": "Review the preview."})["item"])
    call(coordinator, "focus_reply", {"scope": "global", "item_id": cards[-1]["id"], "text": EVIDENCE,
                                      "expected_source_revision": cards[-1]["source_revision"]})
    call(manager, "agents_message_send", {"to_agent_id": service.agent.id, "message": EVIDENCE})
    sessions = []
    for principal in (service.manager, service.agent):
        ctx = service.runtime.resolver.invocation_context(principal)
        for index in range(2):
            sid = new_id()
            assert service.runtime.activity.register_session(ctx, service.registrations[principal.id], sid, "codex")
            call(ctx, "agent_work_update", {"session_id": sid, "expected_work_revision": 1,
                "items": [{"business_id": fixture.business_id, "task_id": tasks[1]["id"],
                    "summary": "Validate pack sizes before publishing the stock preview."},
                    {"business_id": fixture.business_id, "summary": "Collect the pilot store's feedback."}]})
            sessions.append(sid)
        # Persist a normal checkpoint observation; the real summary producer
        # intentionally exposes no authoritative identity for deduplication.
        with fixture.db.transaction() as uow:
            uow.connection.execute("INSERT INTO agent_checkpoint_findings "
                "(agent_id,check_key,code,business_id,session_id,episode,state,message,created_at,updated_at) "
                "VALUES(?,?,?,?,?,1,'open',?,?,?)", (principal.id, "work-review", "work_review_missing",
                fixture.business_id, sid, "Review current work after the latest completed turn.",
                "2026-09-22T12:00:00Z", "2026-09-22T12:00:00Z"))
    consumer = new_id()
    claim(service.runtime.inbox, service.runtime.resolver.invocation_context(service.agent), consumer)
    return {"tasks": tasks, "cards": cards, "sessions": sessions, "sources": sources, "consumer": consumer}
