from datetime import datetime, timezone

from qorqut_operations.contracts import new_id
from bazarlyk.inbox.broadcasts import BroadcastDispatcher
from test_service_transports import ServiceFixture


NOW = datetime(2031, 2, 3, 9, tzinfo=timezone.utc)


def call(service, ctx, name, args, revision=None):
    options = {"idempotency_key": new_id()}
    if revision is not None:
        options["expected_revision"] = revision
    result = service.runtime.application.invoke(ctx, name, args, options)
    assert result.status == "ok", result.to_dict()
    return result.data


def definition(recipient, **extra):
    return {"name": "daily check", "delivery": "inbox", "message": "Check this now.",
            "recipients": [recipient], "timing": {"kind": "interval", "every": 15, "unit": "minutes", "at": NOW.isoformat()}, **extra}


def test_schedule_update_pause_resume_and_cancel(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    service.runtime.broadcasts.clock = lambda: NOW
    owner = service.fixture.ctx
    created = call(service, owner, "broadcasts_create", {"definition": definition(service.agent.id)})["records"][0]
    updated = call(service, owner, "broadcasts_update", {"broadcast_id": created["id"],
        "definition": definition(service.agent.id, max_occurrences=2)}, created["revision"])["records"][0]
    paused = call(service, owner, "broadcasts_pause", {"broadcast_id": created["id"]}, updated["revision"])["records"][0]
    resumed = call(service, owner, "broadcasts_resume", {"broadcast_id": created["id"]}, paused["revision"])["records"][0]
    cancelled = call(service, owner, "broadcasts_cancel", {"broadcast_id": created["id"]}, resumed["revision"])["records"][0]
    denied = service.runtime.application.invoke(owner, "broadcasts_resume", {"broadcast_id": created["id"]},
        {"expected_revision": cancelled["revision"], "idempotency_key": new_id()})
    assert denied.status == "error"


def test_mixed_global_discovery_and_manager_visibility(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    owner = service.fixture.ctx
    global_agent = service.fixture.create("coordinator", "ordubashy")
    discovered = call(service, owner, "broadcast_recipients", {"scope": "global", "all_businesses": True})
    assert any(item["business_id"] is None for item in discovered["items"])
    assert any(item["business_id"] == service.fixture.business_id for item in discovered["items"])
    global_definition = definition(global_agent.id)
    global_definition["recipients"].append(service.agent.id)
    created = call(service, owner, "broadcasts_create", {"scope": "global", "definition": global_definition})["records"][0]
    # The original author always sees its own durable management record.
    listed = call(service, owner, "broadcasts_list", {"scope": "global"})
    assert created["id"] in {item["id"] for item in listed["items"]}
