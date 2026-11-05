"""One Project read guard protects the item resolver, fingerprints, Focus, private Threads and link targets."""

from __future__ import annotations

from dataclasses import replace

import pytest
from qorqut_operations.contracts import DomainError, new_id

from test_project_records import (call, context, context_global, host, ok, seed_project)


def reference(project):
    return {"type": "project", "id": project, "business_id": project}


def resolve(host, ctx, project):
    with host.db.read() as uow:
        return host.runtime.projects.items.resolve(ctx, reference(project), uow)


def test_direct_project_resolution_and_fingerprint_use_the_one_guard(host):
    project = seed_project(host, name="Guard Court", slug="guard-court")
    owner = context(host, project)
    assert resolve(host, owner, project)["label"] == "Guard Court"
    with host.db.read() as uow:
        assert host.runtime.projects.items.fingerprint(owner, reference(project), uow)
    # A real ordinary member of the Project (not the installation owner, who is allowed by identity).
    teammate_principal = new_id()
    with host.db.transaction() as uow:
        uow.repository("principals").insert({"id": teammate_principal, "kind": "human", "display_name": "Teammate"})
        uow.repository("business_memberships", business_id=project).insert(
            {"principal_id": teammate_principal, "is_owner": True})
    member = host.owner_context(project)
    member = replace(member, principal=replace(member.principal, id=teammate_principal),
                     allowed_business_ids=frozenset({project}))
    with pytest.raises(DomainError) as refused:
        resolve(host, member, project)
    assert refused.value.code in {"forbidden", "policy_denied", "not_found"}
    with host.db.read() as uow:
        with pytest.raises(DomainError):
            host.runtime.projects.items.fingerprint(member, reference(project), uow)
    # A Project reference must carry its own exact scope on every public surface.
    with host.db.read() as uow:
        with pytest.raises(DomainError):
            host.runtime.projects.items.resolve(member, {"type": "project", "id": project, "business_id": None}, uow)


def test_the_private_project_thread_and_directory_hide_a_revoked_or_archived_project(host):
    project = seed_project(host, name="Thread Guard", slug="thread-guard")
    owner = context(host, project)
    sent = ok(call(host, owner, "owner_conversation_send", {
        "source": reference(project), "text": "A synthetic private project note."}))
    assert sent["message"]["body"] == "A synthetic private project note."
    directory = ok(call(host, context_global(host), "owner_conversations_list", {"scope": "global"}))
    entry = next(item for item in directory["items"] if item["source"]["id"] == project)
    assert entry["resolved_source"]["label"] == "Thread Guard"
    stale = context(host, project)
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (project, host.instance.owner))
    denied = call(host, stale, "owner_conversation_get", {"source": reference(project)})
    assert denied.status == "error"
    after = ok(call(host, context_global(host), "owner_conversations_list", {"scope": "global"}))
    assert project not in {item["source"]["id"] for item in after["items"]}
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM owner_conversations WHERE business_id=?", (project,)) \
            .fetchone()[0] == 1

    archived = seed_project(host, name="Archived Thread", slug="archived-thread")
    ok(call(host, context(host, archived), "owner_conversation_send", {
        "source": reference(archived), "text": "Another synthetic note."}))
    with host.db.transaction() as uow:
        current = uow.repository("businesses").get(archived)
        uow.repository("businesses").update(archived, {"archived_at": "2026-01-01T00:00:00Z"},
                                            expected_revision=current["revision"])
    archived_directory = ok(call(host, context_global(host), "owner_conversations_list", {"scope": "global"}))
    assert archived not in {item["source"]["id"] for item in archived_directory["items"]}


def test_focus_presentation_of_a_project_uses_the_same_authority(host):
    project = seed_project(host, name="Focus Guard", slug="focus-guard")
    owner = context_global(host)
    presented = ok(call(host, owner, "focus_present", {"source": reference(project), "reason": "Watch the pilot"}))
    card = presented["item"]
    focus = ok(call(host, owner, "focus_get", {"scope": "global"}))
    shown = next(item for item in focus["items"] if item["id"] == card["id"])
    assert shown["label"] == "Focus Guard" and shown["note_count"] == 0
    refused = call(host, owner, "focus_present", {"source": reference(project), "display_name": "My own name"})
    assert refused.status == "error"
    missing_scope = call(host, owner, "focus_present",
                         {"source": {"type": "project", "id": project, "business_id": None}})
    assert missing_scope.status == "error"
    with host.db.transaction() as uow:
        current = uow.repository("businesses").get(project)
        uow.repository("businesses").update(project, {"archived_at": "2026-01-01T00:00:00Z"},
                                            expected_revision=current["revision"])
    after = ok(call(host, owner, "focus_get", {"scope": "global"}))
    denied = next((item for item in after["items"] if item["id"] == card["id"]), None)
    if denied is not None:
        assert "label" not in {key: value for key, value in denied.items() if value} or denied.get("unavailable")


def test_a_project_target_must_name_the_project_as_its_own_scope(host):
    project = seed_project(host, name="Target Guard", slug="target-guard")
    other = seed_project(host, name="Target Other", slug="target-other")
    owner = context(host, project)
    for bad in ({"type": "project", "id": other, "business_id": None},
                {"type": "project", "id": other, "business_id": project}):
        for operation in ("project_items_link", "project_item_get", "project_items_unlink"):
            args = {"project_id": project, "item": bad} if operation != "project_items_unlink" else {
                "project_id": project, "link_id": "00000000-0000-4000-8000-000000000000", "expected_revision": 1}
            result = call(host, owner, operation, args)
            assert result.status == "error", (operation, bad)


def test_a_project_self_link_and_unknown_target_are_refused(host):
    project = seed_project(host, name="Self Guard", slug="self-guard")
    owner = context(host, project)
    self_link = call(host, owner, "project_items_link", {"project_id": project,
                                                         "item": reference(project)})
    assert self_link.status == "error"
    unknown = call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "task", "id": "00000000-0000-4000-8000-000000000000",
                                        "business_id": project}})
    assert unknown.status == "error"
