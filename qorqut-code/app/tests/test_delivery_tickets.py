"""Scoped System Dev follow-up, resolution evidence and reporter delivery."""

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, utc_now
from bazarlyk.domains.dev_tickets import DevTickets
from test_global_agents import global_agent
from test_service_transports import ServiceFixture


def business_coordinator(service):
    with service.runtime.database.read() as uow:
        return service.runtime.policy.context_for(service.manager.id, business_id=service.fixture.business_id,
            review_business_id=service.fixture.business_id, uow=uow)


def prepared_domain(service, domain, ctx, operation, args):
    options = InvocationOptions(idempotency_key=new_id(), expected_revision=args.pop("expected_revision", None))
    with service.runtime.database.read() as uow:
        plan = getattr(domain, "prepare_" + operation)(ctx, args, options, uow)
    with service.runtime.database.transaction() as uow:
        return getattr(domain, "apply_" + operation)(ctx, plan, uow)


def test_agent_ticket_scope_follow_up_resolution_history_and_notification(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    app = service.runtime.application
    reporter = service.runtime.resolver.invocation_context(service.agent)
    coordinator = business_coordinator(service)
    domain = DevTickets()

    created = app.invoke(reporter, "dev_tickets_create", {"title": "Inbox delivery stalls", "details": "The watcher reports a lease but no delivery."},
                         {"idempotency_key": new_id()})
    assert created.status == "ok", created.to_dict()
    ticket_id = created.data["ticket_id"]

    # The business agent sees its own report; the business-scoped coordinator sees it by scope.
    assert {row["id"] for row in app.invoke(reporter, "dev_tickets_agent_list", {"scope": "business", "status": "open"}).data["items"]} == {ticket_id}
    assert {row["id"] for row in app.invoke(coordinator, "dev_tickets_agent_list", {"scope": "business", "status": "open"}).data["items"]} == {ticket_id}
    assert app.invoke(reporter, "dev_tickets_get", {"ticket_id": ticket_id}).data["ticket"]["details"] == "The watcher reports a lease but no delivery."

    followed = app.invoke(coordinator, "dev_tickets_follow_up", {"ticket_id": ticket_id, "details": "Reproduced after a fresh startup; the pending event remains unread."},
                          {"idempotency_key": new_id()})
    assert followed.status == "ok", followed.to_dict()
    resolved = app.invoke(coordinator, "dev_tickets_resolve", {
        "ticket_id": ticket_id,
        "resolution": "Renewed the active watcher lease before polling and retained the current consumer identity.",
        "verification": "Isolated watcher test completed with one delivered event and no duplicate delivery.",
        "deployment_ref": "core-fix-test",
    }, {"idempotency_key": new_id()})
    assert resolved.status == "ok", resolved.to_dict()
    assert resolved.data["status"] == "closed" and resolved.data["resolved_by"] == service.manager.id

    report_result = app.invoke(reporter, "dev_tickets_get", {"ticket_id": ticket_id})
    assert report_result.status == "ok", report_result.to_dict()
    report = report_result.data
    with service.runtime.database.read() as uow:
        assert report["ticket"]["status"] == "closed"
        assert report["ticket"]["resolution"].startswith("Renewed the active watcher")
        assert [event["event_kind"] for event in report["events"]] == ["follow_up", "resolved"]
        assert report["events"][-1]["verification"].startswith("Isolated watcher test")
        assert report["events"][-1]["deployment_ref"] == "core-fix-test"
        message = uow.connection.execute("SELECT * FROM agent_messages WHERE recipient_id=? AND body LIKE ?",
                                         (service.agent.id, "System Dev report resolved.%")).fetchone()
        assert message is not None
        assert uow.connection.execute("SELECT count(*) FROM inbox_deliveries WHERE event_id=?", (message["event_id"],)).fetchone()[0] == 1

    first_page = app.invoke(reporter, "dev_tickets_get", {"ticket_id": ticket_id, "history_limit": 1})
    assert [event["event_kind"] for event in first_page.data["events"]] == ["follow_up"]
    assert first_page.data["events_next_offset"] == 1
    second_page = app.invoke(reporter, "dev_tickets_get", {
        "ticket_id": ticket_id, "history_limit": 1, "history_offset": 1,
    })
    assert [event["event_kind"] for event in second_page.data["events"]] == ["resolved"]
    assert second_page.data["events_next_offset"] is None

    already_closed = app.invoke(reporter, "dev_tickets_resolve", {"ticket_id": ticket_id, "resolution": "again", "verification": "again"},
                                {"idempotency_key": new_id()})
    assert already_closed.status == "error" and already_closed.error["code"] == "ticket_closed"


def test_agent_reports_do_not_leak_across_business_or_global_scope(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    app = service.runtime.application
    reporter = service.runtime.resolver.invocation_context(service.agent)
    coordinator = business_coordinator(service)
    global_principal = global_agent(service, "global-reporter")
    global_ctx = service.runtime.resolver.invocation_context(global_principal)
    ordubashy_principal = service.fixture.create("coordinator", "ordubashy")
    ordubashy = service.runtime.resolver.invocation_context(ordubashy_principal)
    domain = DevTickets()

    business_ticket = app.invoke(reporter, "dev_tickets_create", {"title": "Business report"}, {"idempotency_key": new_id()}).data["ticket_id"]
    global_ticket = app.invoke(global_ctx, "dev_tickets_create", {"title": "Global report"}, {"idempotency_key": new_id()}).data["ticket_id"]
    prior_business = new_id()
    prior_business_ticket = new_id()
    with service.runtime.database.transaction() as uow:
        uow.repository("businesses").insert({
            "id": prior_business, "slug": "prior-business", "workspace": str(service.fixture.root / "prior-business"),
            "hq_path": str(service.fixture.root / "prior-business" / "hq"), "name": "Prior Business", "stage": "validating",
        })
        uow.repository("dev_tickets").insert({
            "id": prior_business_ticket, "title": "Prior business report", "details": "private",
            "kind": "bug", "reporter_id": service.manager.id, "business_id": prior_business,
            "created_at": utc_now(), "updated_at": utc_now(),
        })
        # Ordubashy scope covers every registered business.
        for principal in (ordubashy_principal, service.manager):
            profile = uow.repository("agent_profiles", business_id=None).get(principal.id)
            profile_json = profile["profile_json"]
            profile_json["scope"]["allowed_business_ids"].append(prior_business)
            profile_json["profile_revision"] = profile["revision"] + 1
            uow.repository("agent_profiles", business_id=None).update(
                principal.id, {"profile_json": profile_json}, expected_revision=profile["revision"])
    ordubashy = service.runtime.resolver.invocation_context(ordubashy_principal)
    assert {row["id"] for row in app.invoke(reporter, "dev_tickets_agent_list", {"scope": "business", "status": "all"}).data["items"]} == {business_ticket}
    assert {row["id"] for row in app.invoke(coordinator, "dev_tickets_agent_list", {"scope": "business", "status": "all"}).data["items"]} == {business_ticket}
    assert app.invoke(coordinator, "dev_tickets_get", {"ticket_id": prior_business_ticket}).status == "error"
    global_list = app.invoke(ordubashy, "dev_tickets_agent_list", {"scope": "global", "status": "all"})
    assert global_list.status == "ok", global_list.error
    assert {row["id"] for row in global_list.data["items"]} >= {business_ticket, global_ticket}
    selected_ordubashy = ordubashy.select_business(service.fixture.business_id)
    assert {row["id"] for row in app.invoke(selected_ordubashy, "dev_tickets_agent_list", {"scope": "business", "status": "all"}).data["items"]} == {business_ticket}
    assert app.invoke(reporter, "dev_tickets_get", {"ticket_id": global_ticket}).status == "error"
    assert app.invoke(coordinator, "dev_tickets_get", {"ticket_id": global_ticket}).status == "error"

    # A revoked business membership removes even an Ordubashy-authored report
    # from the authorized scope; reporter identity is not a scope bypass.
    revoked_ticket = new_id()
    with service.runtime.database.transaction() as uow:
        uow.repository("dev_tickets").insert({
            "id": revoked_ticket, "title": "Revoked business report", "details": "private",
            "kind": "bug", "reporter_id": ordubashy_principal.id, "business_id": service.fixture.business_id,
            "created_at": utc_now(), "updated_at": utc_now(),
        })
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (service.fixture.business_id, ordubashy_principal.id))
    with service.runtime.database.read() as uow:
        assert revoked_ticket not in {row["id"] for row in domain.agent_list(ordubashy, {"scope": "global", "status": "all"}, None, uow)["items"]}
        with pytest.raises(DomainError, match="outside your authorized scope"):
            domain.get(ordubashy, {"ticket_id": revoked_ticket}, None, uow)

    no_delivery_ticket = app.invoke(reporter, "dev_tickets_create", {"title": "Membership removed before repair"},
                                    {"idempotency_key": new_id()}).data["ticket_id"]
    with service.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (service.fixture.business_id, reporter.principal_id))
    prepared_domain(service, domain, coordinator, "resolve", {
        "ticket_id": no_delivery_ticket,
        "resolution": "Repaired the scoped delivery check using the current owner path.",
        "verification": "The isolated verification completed without an agent membership.",
    })
    with service.runtime.database.read() as uow:
        assert not uow.connection.execute("SELECT 1 FROM agent_messages WHERE recipient_id=? AND body LIKE ?",
                                          (reporter.principal_id, "System Dev report resolved.%")).fetchone()

    resolved = prepared_domain(service, domain, coordinator, "resolve", {
        "ticket_id": business_ticket,
        "resolution": "Updated the watcher delivery path with the current consumer lease.",
        "verification": "Focused delivery test confirms the event arrives exactly once.",
    })
    # A closed report is retained and a follow-up cannot silently reopen it.
    with pytest.raises(DomainError, match="already closed"):
        with service.runtime.database.read() as uow:
            domain.prepare_resolve(coordinator, {"ticket_id": business_ticket, "resolution": "fixed", "verification": "checked"},
                                   InvocationOptions(idempotency_key=new_id()), uow)
