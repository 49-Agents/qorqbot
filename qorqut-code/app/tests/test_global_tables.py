"""Global general tables: the existing table operations in their own namespace, never a business's."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from qorqut_operations.tables.history import HISTORY_REF
from test_global_tables_migration import BUSINESS_VERSION_DEFINITION
from test_service_transports import ORIGIN, ServiceFixture

COLUMNS = [{"id": "name", "name": "Name", "type": "text"}, {"id": "amount", "name": "Amount", "type": "number"}]
TABLE_OPERATIONS = {"tables_list", "tables_get", "tables_rows_list", "tables_history", "tables_create", "tables_update",
                    "tables_rows_write", "tables_history_retry", "tables_archive"}


@pytest.fixture
def s(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    subprocess.run(["git", "init", "-q", str(s.fixture.root)], check=True)
    s.app = s.runtime.application
    s.owner = replace(s.fixture.ctx, business_id=None, review_business_id=None)
    s.ordu = s.manager
    return s


def add_roles(s):
    """Founder and Global Marketing are real registered ecosystem agents with MCP credentials."""
    for name, role in (("founder", "founder"), ("marketing", "global-marketing")):
        principal = s.fixture.create(name, role)
        setattr(s, name, principal)
        ready = s.fixture.prepare(principal)
        s.tokens[principal.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()


def context(s, actor):
    return s.runtime.resolver.invocation_context(actor)


def call(s, op, args=None, *, actor=None, ctx=None, revision=None, key=None):
    ctx = ctx or (context(s, actor) if actor else s.owner)
    return s.app.invoke(ctx, op, args or {}, InvocationOptions(expected_revision=revision, idempotency_key=key or new_id()))


def ok(result):
    assert result.status == "ok", result.to_dict()
    return result.to_dict()["data"]


def gcall(s, op, args=None, **kwargs):
    return call(s, op, {"scope": "global", **(args or {})}, **kwargs)


def business(s, op, args=None, **kwargs):
    return call(s, op, args or {}, ctx=s.fixture.ctx, **kwargs)


def make(s, title="Shared", **kwargs):
    return ok(gcall(s, "tables_create", {"title": title, "columns": COLUMNS}, **kwargs))


def git(s, *args):
    return subprocess.run(["git", "-C", str(s.fixture.root), *args], check=True, capture_output=True, text=True).stdout.strip()


def run(s, job):
    result = s.runtime.background.run_one(job)
    assert result["state"] == "succeeded", result


def approve(s, result):
    ref = result.signoff
    assert ref["review_scope"]["kind"] == "global", ref
    detail = ok(s.runtime.signoffs.get(s.owner, ref["id"]))["signoff"]
    accepted = s.runtime.signoffs.review(s.owner, detail["id"], decision="approve", proposal_revision=detail["proposal_revision"],
        payload_hash=detail["payload_hash"], expected_status_revision=detail["status_revision"], idempotency_key=new_id())
    if accepted.status == "accepted":
        s.runtime.signoffs.run_one(detail["id"])
    return accepted


def test_global_crud_atomicity_pagination_cas_idempotency_and_isolation(s):
    made = make(s, actor=s.ordu)
    table = made["table"]
    assert table["business_id"] is None and table["purpose"] == "general" and table["row_count"] == 0
    listed = ok(gcall(s, "tables_list"))
    assert [row["id"] for row in listed["items"]] == [table["id"]] and listed["purposes"] == [{"id": "general", "label": "General"}]
    assert ok(business(s, "tables_list"))["items"] == []
    for operation in ("tables_get", "tables_rows_list", "tables_history"):
        assert business(s, operation, {"table_id": table["id"]}).error["code"] == "not_found"

    args = {"table_id": table["id"], "rows": [{"cells": {"name": "A", "amount": 1}}, {"cells": {"name": "B"}}, {"cells": {"name": "C"}}]}
    key = new_id()
    saved = ok(gcall(s, "tables_rows_write", args, revision=1, key=key))
    assert saved["table"]["revision"] == 2 and saved["table"]["row_count"] == 3
    assert ok(gcall(s, "tables_rows_write", args, revision=1, key=key)) == saved
    assert gcall(s, "tables_rows_write", args, revision=1).error["code"] == "revision_conflict"

    broken = {"table_id": table["id"], "rows": [{"cells": {"name": "D"}}, {"cells": {"amount": "text"}}]}
    assert gcall(s, "tables_rows_write", broken, revision=2).error["code"] == "validation_error"
    assert ok(gcall(s, "tables_get", {"table_id": table["id"]}))["table"]["revision"] == 2

    first = ok(gcall(s, "tables_rows_list", {"table_id": table["id"], "limit": 2}))
    assert [row["cells"]["name"] for row in first["items"]] == ["A", "B"] and first["next_cursor"]
    rest = ok(gcall(s, "tables_rows_list", {"table_id": table["id"], "limit": 2, "cursor": first["next_cursor"]}))
    assert [row["cells"]["name"] for row in rest["items"]] == ["C"]
    ok(gcall(s, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "E"}}]}, revision=2))
    stale = gcall(s, "tables_rows_list", {"table_id": table["id"], "limit": 2, "cursor": first["next_cursor"]})
    assert stale.error["code"] == "validation_error"
    # A cursor belongs to its scope: a business list rejects a Global cursor and the reverse.
    ok(business(s, "tables_create", {"title": "Business", "columns": COLUMNS}))
    tables = ok(gcall(s, "tables_list", {"limit": 1}))
    ok(gcall(s, "tables_create", {"title": "Second", "columns": COLUMNS}))
    cursor = ok(gcall(s, "tables_list", {"limit": 1}))["next_cursor"]
    assert cursor and business(s, "tables_list", {"limit": 1, "cursor": cursor}).error["code"] == "validation_error"
    for other in (1, 2):
        ok(business(s, "tables_create", {"title": f"Business {other}", "columns": COLUMNS}))
    business_cursor = ok(business(s, "tables_list", {"limit": 1}))["next_cursor"]
    assert business_cursor and gcall(s, "tables_list", {"limit": 1, "cursor": business_cursor}).error["code"] == "validation_error"
    assert len(tables["items"]) == 1

    written = ok(gcall(s, "tables_update", {"table_id": table["id"], "changes": {"title": "Renamed", "description": "Notes"}}, revision=3))
    assert written["table"]["title"] == "Renamed"
    with s.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM data_tables WHERE business_id IS NULL").fetchone()[0] == 2
        assert uow.connection.execute("SELECT count(*) FROM data_rows WHERE business_id IS NULL").fetchone()[0] == 4
    global_table = make(s)["table"]
    business_table = ok(business(s, "tables_create", {"title": "Business", "columns": COLUMNS}))["table"]
    business_row = ok(business(s, "tables_rows_write", {"table_id": business_table["id"], "rows": [{"cells": {"name": "Private"}}]}, revision=1))["rows"][0]
    global_row = ok(gcall(s, "tables_rows_write", {"table_id": global_table["id"], "rows": [{"cells": {"name": "Shared"}}]}, revision=1))["rows"][0]
    # Neither a row nor a table is found through the other namespace, and a mixed batch never half-applies.
    mixed = {"table_id": global_table["id"], "rows": [{"cells": {"name": "New"}}, {"id": business_row["id"], "cells": {"name": "Taken"}}]}
    assert gcall(s, "tables_rows_write", mixed, revision=2).error["code"] == "not_found"
    assert gcall(s, "tables_rows_write", {"table_id": global_table["id"], "delete_ids": [business_row["id"]]}, revision=2).error["code"] == "not_found"
    assert business(s, "tables_rows_write", {"table_id": business_table["id"], "rows": [{"id": global_row["id"], "cells": {"name": "x"}}]}, revision=2).error["code"] == "not_found"
    for operation in ("tables_get", "tables_rows_list", "tables_history"):
        assert gcall(s, operation, {"table_id": business_table["id"]}).error["code"] == "not_found"
    assert business(s, "tables_update", {"table_id": global_table["id"], "changes": {"title": "x"}}, revision=2).error["code"] == "not_found"
    assert business(s, "tables_history_retry", {"version_id": global_table["versioning"]["version_id"]}).error["code"] == "not_found"
    assert gcall(s, "tables_history_retry", {"version_id": business_table["versioning"]["version_id"]}).error["code"] == "not_found"
    assert ok(gcall(s, "tables_get", {"table_id": global_table["id"]}))["table"]["row_count"] == 1
    # Global has no business to select, and the owner reaches it only by naming the scope.
    assert call(s, "tables_list", {"scope": "business"}, ctx=s.owner).error["code"] == "scope_required"
    in_global = {row["id"] for row in ok(business(s, "tables_list", {"scope": "global", "limit": 200}))["items"]}
    in_business = {row["id"] for row in ok(business(s, "tables_list", {"limit": 200}))["items"]}
    assert global_table["id"] in in_global and business_table["id"] not in in_global
    assert business_table["id"] in in_business and global_table["id"] not in in_business





def test_global_tables_are_general_only_and_archived_rows_are_read_only(s):
    story = ok(business(s, "stories_create", {"title": "Campaign", "purpose": "Customer launch"}))["story"]
    for purpose in ("metrics", "ledger", "recurring", "reconciliations"):
        assert gcall(s, "tables_create", {"title": purpose, "columns": COLUMNS, "purpose": purpose}).error["code"] == "validation_error"
    assert gcall(s, "tables_create", {"title": "Story", "columns": COLUMNS, "story_ids": [story["id"]]}).error["code"] == "validation_error"
    table = make(s)["table"]
    upload = {"table_id": table["id"], "rows": [{"cells": {"name": "A"}, "source_upload_id": new_id()}]}
    assert gcall(s, "tables_rows_write", upload, revision=1).error["code"] == "validation_error"
    assert gcall(s, "tables_rows_list", {"table_id": table["id"], "view": "metrics"}).error["code"] == "validation_error"
    assert gcall(s, "tables_create", {"title": "Ok", "columns": COLUMNS, "purpose": "general", "story_ids": []}).status == "ok"
    with s.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM data_tables WHERE business_id IS NULL").fetchone()[0] == 2
    table = make(s)["table"]
    ok(gcall(s, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "Keep"}}]}, revision=1))
    ok(gcall(s, "tables_update", {"table_id": table["id"], "changes": {"archived": True}}, revision=2))
    assert table["id"] not in {row["id"] for row in ok(gcall(s, "tables_list"))["items"]}
    assert table["id"] in {row["id"] for row in ok(gcall(s, "tables_list", {"include_archived": True}))["items"]}
    assert gcall(s, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "X"}}]}, revision=3).error["code"] == "validation_error"
    assert [row["cells"]["name"] for row in ok(gcall(s, "tables_rows_list", {"table_id": table["id"]}))["items"]] == ["Keep"]
    ok(gcall(s, "tables_update", {"table_id": table["id"], "changes": {"archived": False}}, revision=3, actor=s.ordu))
    ok(gcall(s, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "Again"}}]}, revision=4, actor=s.ordu))





def test_global_tables_keep_tags_and_the_department_filter_while_purpose_stays_general(s):
    tags = {"topics": ["Launch"], "campaigns": ["Spring"], "departments": ["marketing"]}
    tagged = ok(gcall(s, "tables_create", {"title": "Tagged", "columns": COLUMNS, "tags": tags}))["table"]
    plain = make(s, "Plain")["table"]
    other = ok(business(s, "tables_create", {"title": "Business tagged", "columns": COLUMNS, "tags": tags}))["table"]
    assert tagged["tags"] == tags and tagged["purpose"] == "general"
    assert [row["id"] for row in ok(gcall(s, "tables_list", {"department": "marketing"}))["items"]] == [tagged["id"]]
    assert [row["id"] for row in ok(business(s, "tables_list", {"department": "marketing"}))["items"]] == [other["id"]]
    edited = ok(gcall(s, "tables_update", {"table_id": plain["id"], "changes": {"tags": {"topics": [], "campaigns": [], "departments": ["sales"]}}}, revision=1))["table"]
    assert edited["tags"]["departments"] == ["sales"] and edited["purpose"] == "general"
    assert [row["id"] for row in ok(gcall(s, "tables_list", {"department": "sales"}))["items"]] == [plain["id"]]
    assert ok(business(s, "tables_list", {"department": "sales"}))["items"] == []
    assert gcall(s, "tables_create", {"title": "Typed", "columns": COLUMNS, "tags": tags, "purpose": "metrics"}).error["code"] == "validation_error"


def test_agent_archive_always_needs_owner_signoff_but_owner_and_restore_do_not(s):
    table = make(s, actor=s.ordu)["table"]
    mixed = {"table_id": table["id"], "changes": {"title": "Archived title", "archived": True}}
    proposed = gcall(s, "tables_update", mixed, actor=s.ordu, revision=1)
    assert proposed.status == "signoff_required", proposed.to_dict()
    assert ok(gcall(s, "tables_get", {"table_id": table["id"]}))["table"]["archived"] is False
    assert ok(gcall(s, "tables_get", {"table_id": table["id"]}))["table"]["title"] == "Shared"
    assert approve(s, proposed).status == "accepted"
    archived = ok(gcall(s, "tables_get", {"table_id": table["id"]}))["table"]
    assert archived["archived"] is True and archived["title"] == "Archived title" and archived["revision"] == 2
    assert len(ok(gcall(s, "tables_history", {"table_id": table["id"]}))["items"]) == 2

    # Archiving an archived table needs no approval; restoring is an ordinary allowed update.
    rename = gcall(s, "tables_update", {"table_id": table["id"], "changes": {"archived": True, "description": "Still archived"}}, actor=s.ordu, revision=2)
    assert rename.status == "ok", rename.to_dict()
    assert gcall(s, "tables_archive", {"table_id": table["id"]}, actor=s.ordu, revision=3).error["code"] == "validation_error"
    restored = gcall(s, "tables_update", {"table_id": table["id"], "changes": {"archived": False}}, actor=s.ordu, revision=3)
    assert restored.status == "ok", restored.to_dict()

    direct = gcall(s, "tables_archive", {"table_id": table["id"]}, actor=s.ordu, revision=4)
    assert direct.status == "signoff_required", direct.to_dict()
    assert approve(s, direct).status == "accepted"
    assert ok(gcall(s, "tables_get", {"table_id": table["id"]}))["table"]["archived"] is True

    # The installation owner has direct authority.
    other = make(s)["table"]
    ok(gcall(s, "tables_update", {"table_id": other["id"], "changes": {"archived": True}}, revision=1))
    ok(gcall(s, "tables_archive", {"table_id": make(s, "Third")["table"]["id"]}, revision=1))
    # A proposal made against an older revision does not apply once the owner has changed the table.
    stale = make(s, "Stale", actor=s.ordu)["table"]
    late = gcall(s, "tables_archive", {"table_id": stale["id"]}, actor=s.ordu, revision=1)
    assert late.status == "signoff_required"
    ok(gcall(s, "tables_update", {"table_id": stale["id"], "changes": {"title": "Owner edit"}}, revision=1))
    approve(s, late)
    current = ok(gcall(s, "tables_get", {"table_id": stale["id"]}))["table"]
    assert current["archived"] is False and current["title"] == "Owner edit"


def test_archived_creation_needs_the_same_approval(s):
    proposed = gcall(s, "tables_create", {"title": "Born archived", "columns": COLUMNS, "archived": True}, actor=s.ordu)
    assert proposed.status == "signoff_required", proposed.to_dict()
    assert ok(gcall(s, "tables_list", {"include_archived": True}))["items"] == []
    assert approve(s, proposed).status == "accepted"
    assert [row["archived"] for row in ok(gcall(s, "tables_list", {"include_archived": True}))["items"]] == [True]
    assert gcall(s, "tables_create", {"title": "Active", "columns": COLUMNS, "archived": False}, actor=s.ordu).status == "ok"
    assert gcall(s, "tables_create", {"title": "Owner archived", "columns": COLUMNS, "archived": True}).status == "ok"


def test_explicit_overrides_cannot_remove_archive_approval_but_deny_stays_effective(s):
    table = make(s, actor=s.ordu)["table"]
    s.fixture.update(s.ordu, {"tool_policy": {"overrides": {"tables_archive": "allow", "tables_update": "allow"}}})
    proposed = gcall(s, "tables_update", {"table_id": table["id"], "changes": {"archived": True}}, actor=s.ordu, revision=1)
    assert proposed.status == "signoff_required", proposed.to_dict()
    s.fixture.update(s.ordu, {"tool_policy": {"overrides": {"tables_archive": "deny"}}})
    assert gcall(s, "tables_update", {"table_id": table["id"], "changes": {"archived": True}}, actor=s.ordu, revision=1).error["code"] == "policy_denied"
    assert gcall(s, "tables_update", {"table_id": table["id"], "changes": {"title": "Fine"}}, actor=s.ordu, revision=1).status == "ok"
    s.fixture.update(s.ordu, {"tool_policy": {"overrides": {"tables_update": "deny"}}})
    assert gcall(s, "tables_archive", {"table_id": table["id"]}, actor=s.ordu, revision=2).error["code"] == "policy_denied"
    assert gcall(s, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "x"}}]}, actor=s.ordu, revision=2).status == "ok"
    s.fixture.update(s.ordu, {"tool_policy": {"overrides": {"tables_rows_write": "deny"}}})
    assert gcall(s, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "y"}}]}, actor=s.ordu, revision=3).error["code"] == "policy_denied"
    assert gcall(s, "tables_list", actor=s.ordu).status == "ok"





def test_active_global_roles_get_direct_allow_for_every_table_operation(s):
    add_roles(s)
    for who in ("ordu", "founder", "marketing"):
        allow_every_operation(s, who, getattr(s, who))


def allow_every_operation(s, who, actor):
    made = ok(gcall(s, "tables_create", {"title": who, "columns": COLUMNS}, actor=actor))
    table = made["table"]
    assert ok(gcall(s, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "A"}}]}, actor=actor, revision=1))["rows"]
    assert ok(gcall(s, "tables_update", {"table_id": table["id"], "changes": {"title": who + "!"}}, actor=actor, revision=2))["table"]["title"] == who + "!"
    for operation in ("tables_list", "tables_get", "tables_rows_list", "tables_history"):
        assert gcall(s, operation, {"table_id": table["id"]} if operation != "tables_list" else {}, actor=actor).status == "ok"
    retry = ok(gcall(s, "tables_history_retry", {"version_id": made["table"]["versioning"]["version_id"]}, actor=actor))
    run(s, retry["job_id"])
    # A business table belongs to a business and is unreachable from Global whatever the role.
    hidden = ok(business(s, "tables_create", {"title": "Hidden", "columns": COLUMNS}))["table"]["id"]
    assert gcall(s, "tables_get", {"table_id": hidden}, actor=actor).error["code"] == "not_found"


def test_business_agents_and_other_roles_have_no_global_table_access(s):
    add_roles(s)
    table = make(s)["table"]
    assert gcall(s, "tables_list", actor=s.agent).status == "error"
    assert gcall(s, "tables_create", {"title": "No", "columns": COLUMNS}, actor=s.agent).status == "error"
    # An override and a matching display name never confer Global access.
    s.fixture.update(s.agent, {"tool_policy": {"overrides": {"tables_list": "allow", "tables_get": "allow"}}})
    assert gcall(s, "tables_get", {"table_id": table["id"]}, actor=s.agent).status == "error"
    applier = s.fixture.create("ordubashy-two", "learning-applier", scope={"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]})
    for operation, args in (("tables_list", {}), ("tables_get", {"table_id": table["id"]}), ("tables_create", {"title": "x", "columns": COLUMNS})):
        result = gcall(s, operation, args, actor=applier)
        assert result.status == "error" and result.error["code"] == "policy_denied", result.to_dict()
    # Business grants are unchanged: the business agent still creates business tables.
    assert ok(call(s, "tables_create", {"title": "Mine", "columns": COLUMNS}, actor=s.agent))["table"]["business_id"] == s.fixture.business_id
    selected = context(s, s.marketing).select_business(s.fixture.business_id)
    assert call(s, "tables_list", ctx=selected).status == "ok"
    assert call(s, "tables_create", {"title": "No", "columns": COLUMNS}, ctx=selected).error["code"] == "policy_denied"
    assert gcall(s, "tables_create", {"title": "Yes", "columns": COLUMNS}, actor=s.marketing).status == "ok"
    # Business scope stays denied even after a Global write succeeded.
    assert call(s, "tables_create", {"title": "No", "columns": COLUMNS}, ctx=selected).error["code"] == "policy_denied"





def test_other_eligible_global_agents_need_explicit_grants_and_archive_still_needs_the_owner(s):
    add_roles(s)
    table = make(s)["table"]
    scope = {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}
    other = s.fixture.create("applier-two", "learning-applier", scope=scope)

    def denied(result):
        return result.status == "error" and result.error["code"] == "policy_denied"

    def grants(**modes):
        s.fixture.update(other, {"tool_policy": {"overrides": modes}})

    def gc(operation, args=None, revision=None):
        return gcall(s, operation, args or {}, actor=other, revision=revision)

    # Absent grants deny every operation, and role business defaults never promote: only explicit overrides count.
    for operation, args in (("tables_list", {}), ("tables_get", {"table_id": table["id"]}), ("tables_rows_list", {"table_id": table["id"]}),
                            ("tables_history", {"table_id": table["id"]}), ("tables_create", {"title": "x", "columns": COLUMNS})):
        assert denied(gc(operation, args)), operation
    from bazarlyk.policy import resolve_operation_policy
    registry = s.runtime.registry
    assert resolve_operation_policy("tables_rows_write", {"tables_rows_write": "allow"}, {}, registry, role_id="dev", scope="global").mode == "deny"
    assert resolve_operation_policy("tables_rows_write", {"tables_rows_write": "allow"}, {}, registry, role_id="dev").mode == "allow"

    grants(tables_list="allow", tables_get="allow", tables_rows_list="allow")
    for operation, args in (("tables_list", {}), ("tables_get", {"table_id": table["id"]}), ("tables_rows_list", {"table_id": table["id"]})):
        assert gc(operation, args).status == "ok", operation
    assert denied(gc("tables_create", {"title": "x", "columns": COLUMNS}))
    assert denied(gc("tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "x"}}]}, revision=1))
    assert denied(gc("tables_archive", {"table_id": table["id"]}, revision=1))

    # An explicit update grant covers ordinary edits and, through the archive effect, only an owner-approved archive.
    grants(tables_create="allow", tables_update="allow", tables_rows_write="allow")
    mine = ok(gc("tables_create", {"title": "Mine", "columns": COLUMNS}))["table"]
    assert ok(gc("tables_update", {"table_id": mine["id"], "changes": {"title": "Renamed"}}, revision=1))["table"]["title"] == "Renamed"
    proposal = gc("tables_update", {"table_id": mine["id"], "changes": {"archived": True}}, revision=2)
    assert proposal.status == "signoff_required", proposal.to_dict()
    assert ok(gcall(s, "tables_get", {"table_id": mine["id"]}))["table"]["archived"] is False
    assert approve(s, proposal).status == "accepted"
    assert ok(gcall(s, "tables_get", {"table_id": mine["id"]}))["table"]["archived"] is True
    assert denied(gc("tables_history_retry", {"version_id": mine["versioning"]["version_id"]}))

    # An explicit archive grant alone also yields a Signoff; 'allow' never removes the owner's approval.
    grants(tables_archive="allow")
    archive = gc("tables_archive", {"table_id": table["id"]}, revision=1)
    assert archive.status == "signoff_required", archive.to_dict()
    # Explicit denies win, including an update denial against the archive effect.
    grants(tables_archive="deny", tables_update="allow")
    assert denied(gc("tables_archive", {"table_id": table["id"]}, revision=1))
    assert denied(gc("tables_update", {"table_id": table["id"], "changes": {"archived": True}}, revision=1))
    grants(tables_archive="allow", tables_update="deny")
    assert denied(gc("tables_archive", {"table_id": table["id"]}, revision=1))
    assert denied(gc("tables_update", {"table_id": table["id"], "changes": {"archived": True}}, revision=1))

    # Discovery follows the same grants; the runtime guards (active status, ecosystem scope) still apply.
    grants(tables_list="allow", tables_archive="allow")
    rows = {row["canonical_operation"]: row for row in s.runtime.resolver.resolve_agent_config(other).data["tools"]}
    assert rows["tables_list"]["scope_modes"]["global"] == "allow" and rows["tables_archive"]["scope_modes"]["global"] == "signoff"
    assert rows["tables_rows_write"]["scope_modes"]["global"] == "deny"
    grants(tables_list="allow")
    with s.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (other.id,))
    with pytest.raises(DomainError) as inactive:
        gc("tables_list")
    assert inactive.value.code == "inactive_agent"
    # A business agent's explicit override never reaches Global (shown above) or another business's tables.
    assert gcall(s, "tables_list", actor=s.agent).status == "error"


def test_discovery_shows_global_availability_and_retains_business_grants(s):
    add_roles(s)
    def tools(actor):
        return {row["canonical_operation"]: row for row in s.runtime.resolver.resolve_agent_config(actor).data["tools"]}

    for actor in (s.ordu, s.founder, s.marketing):
        rows = tools(actor)
        assert TABLE_OPERATIONS <= {name for name, row in rows.items() if row["available"]}
        assert rows["tables_rows_write"]["scope_modes"]["global"] == "allow"
        assert rows["tables_archive"]["scope_modes"]["global"] == "signoff"
    assert tools(s.marketing)["tables_rows_write"]["scope_modes"] == {"business": "deny", "global": "allow"}
    assert tools(s.ordu)["tables_rows_write"]["scope_modes"]["business"] == "allow"
    assert tools(s.agent)["tables_rows_write"]["effective_mode"] == "allow" and "scope_modes" not in tools(s.agent)["tables_rows_write"]
    with TestClient(s.web) as client:
        for actor in (s.marketing, s.founder):
            headers = s.initialize(client, actor)
            listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
            assert TABLE_OPERATIONS <= {tool["name"] for tool in listed}
            made = ok_wire(s.tool(client, headers, "tables_create", {"scope": "global", "title": "By MCP", "columns": COLUMNS, "idempotency_key": new_id()}))
            assert made["table"]["business_id"] is None
        headers = s.initialize(client, s.agent)
        denied = s.tool(client, headers, "tables_list", {"scope": "global"})
        assert denied["decoded"]["status"] == "error"


def ok_wire(result):
    assert result["decoded"]["status"] == "ok", result
    return result["decoded"]["data"]


def test_owner_root_routes_are_global_only(s):
    with TestClient(s.web) as client:
        def post(operation, body):
            return client.post("/api/root/operations/" + operation, json=body, headers=ORIGIN)

        created = post("tables_create", {"scope": "global", "title": "Owner table", "columns": COLUMNS, "idempotency_key": new_id()})
        assert created.status_code == 200, created.text
        table = created.json()["data"]["table"]
        assert table["business_id"] is None
        listed = post("tables_list", {"scope": "global"})
        assert [row["id"] for row in listed.json()["data"]["items"]] == [table["id"]]
        # Omitting the scope still means Global on this route, never a business.
        assert [row["id"] for row in post("tables_list", {}).json()["data"]["items"]] == [table["id"]]
        assert post("tables_list", {"scope": "business"}).status_code == 403
        assert post("tables_list", {"scope": "global", "business_id": s.fixture.business_id}).status_code == 403
        written = post("tables_rows_write", {"scope": "global", "table_id": table["id"], "expected_revision": 1,
            "rows": [{"cells": {"name": "Row"}}], "idempotency_key": new_id()})
        assert written.status_code == 200, written.text
        assert post("tables_history", {"scope": "global", "table_id": table["id"]}).json()["data"]["items"]
        assert post("tables_archive", {"scope": "global", "table_id": table["id"], "expected_revision": 2, "idempotency_key": new_id()}).status_code == 200
        assert post("tables_list", {"include_archived": True}).json()["data"]["items"][0]["archived"] is True
        business_route = client.post("/api/biz/widget/operations/tables_list", json={}, headers=ORIGIN)
        assert business_route.status_code == 200, business_route.text
        assert business_route.json()["data"]["items"] == []


def test_global_history_uses_its_own_path_and_interleaves_with_business_versions(s):
    root = s.fixture.root
    global_made = make(s)
    business_made = ok(business(s, "tables_create", {"title": "Business", "columns": COLUMNS}))
    global_saved = ok(gcall(s, "tables_rows_write", {"table_id": global_made["table"]["id"], "rows": [{"cells": {"name": "G"}}]}, revision=1))
    business_saved = ok(business(s, "tables_rows_write", {"table_id": business_made["table"]["id"], "rows": [{"cells": {"name": "B"}}]}, revision=1))
    for job in (global_saved["job_id"], business_saved["job_id"], global_made["job_id"], business_made["job_id"]):
        run(s, job)
    assert git(s, "rev-list", "--count", HISTORY_REF) == "4"
    identity = global_made["table"]["id"]
    history = ok(gcall(s, "tables_history", {"table_id": identity}))["items"]
    assert [row["table_revision"] for row in history] == [2, 1]
    assert {row["path"] for row in history} == {"global/tables/" + identity + ".json"}
    latest = json.loads(git(s, "show", history[0]["git_commit"] + ":" + history[0]["path"]))
    assert latest["table"]["business_id"] is None and latest["rows"][0]["cells_json"]["name"] == "G"
    business_history = ok(business(s, "tables_history", {"table_id": business_made["table"]["id"]}))["items"]
    assert {row["path"] for row in business_history} == {"widget/hq/tables/" + business_made["table"]["id"] + ".json"}
    assert json.loads((root / "global/tables" / (identity + ".json")).read_text())["rows"][0]["cells_json"]["name"] == "G"
    assert not (root / ".git/index").exists()
    with s.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM table_versions WHERE snapshot_json IS NOT NULL").fetchone()[0] == 0
    with s.fixture.db.read() as uow:
        job = dict(uow.connection.execute("SELECT business_id,target_namespace,kind FROM jobs WHERE id=?", (global_made["job_id"],)).fetchone())
    assert job == {"business_id": None, "target_namespace": "global", "kind": "tables.global-version"}
    assert business(s, "jobs_get", {"job_id": global_made["job_id"]}).status == "error"
    from bazarlyk.storage.exports import build_exports
    exported = build_exports(s.fixture.db.path, root)
    projections = [json.loads(body) for path, body in exported.files.items() if path.endswith("/data_tables.json")]
    by_scope = {projection["scope"]: [row["title"] for row in projection["records"]] for projection in projections}
    assert by_scope["global"] == ["Shared"] and by_scope[s.fixture.business_id] == ["Business"]


def test_pending_global_versions_retry_only_within_global(s, monkeypatch):
    business_made = ok(business(s, "tables_create", {"title": "Business", "columns": COLUMNS}))
    global_made = make(s)
    history = s.runtime.domains.tables.history
    runner = history.runner
    def broken(*args, **kwargs):
        raise OSError("test Git failure")
    monkeypatch.setattr(history, "runner", broken)
    for job in (business_made["job_id"], global_made["job_id"]):
        assert s.runtime.background.run_one(job)["error"]["code"] == "versioning_failed"
    monkeypatch.setattr(history, "runner", runner)
    retry = ok(gcall(s, "tables_history_retry", {"version_id": global_made["table"]["versioning"]["version_id"]}))
    run(s, retry["job_id"])
    assert git(s, "rev-list", "--count", HISTORY_REF) == "1"
    with s.fixture.db.read() as uow:
        pending = [(row["business_id"], row["id"]) for row in uow.connection.execute("SELECT business_id,id FROM table_versions WHERE git_commit IS NULL")]
        assert pending == [(s.fixture.business_id, business_made["table"]["versioning"]["version_id"])]
        retained = uow.connection.execute("SELECT snapshot_json FROM table_versions WHERE git_commit IS NULL").fetchone()[0]
        assert retained
    run(s, ok(business(s, "tables_history_retry", {"version_id": business_made["table"]["versioning"]["version_id"]}))["job_id"])
    assert git(s, "rev-list", "--count", HISTORY_REF) == "2"


def test_global_history_recovery_is_idempotent_and_preserves_the_owner_repository(s, monkeypatch):
    original = s.fixture.root / "owner.txt"
    original.write_text("Owner committed content\n")
    git(s, "add", "owner.txt")
    git(s, "-c", "user.name=Owner", "-c", "user.email=owner@example.test", "commit", "-qm", "Owner baseline")
    head = git(s, "rev-parse", "HEAD")
    original.write_text("Owner working content\n")
    made = make(s)
    history, receipt = s.runtime.domains.tables.history, {}
    commit = history.commit

    def crash(version, control):
        receipt["commit"] = commit(version, control)
        raise OSError("Simulated stop after Git ref update, before SQLite receipt")

    monkeypatch.setattr(history, "commit", crash)
    with pytest.raises(OSError, match="Simulated stop"):
        s.runtime.background.run_one(made["job_id"])
    monkeypatch.setattr(history, "commit", commit)
    retry = ok(gcall(s, "tables_history_retry", {"version_id": made["table"]["versioning"]["version_id"]}))
    run(s, retry["job_id"])
    again = ok(gcall(s, "tables_history_retry", {"version_id": made["table"]["versioning"]["version_id"]}))
    run(s, again["job_id"])
    assert git(s, "rev-list", "--count", HISTORY_REF) == "1"
    assert git(s, "rev-parse", HISTORY_REF) == receipt["commit"]
    assert git(s, "rev-parse", "HEAD") == head
    assert original.read_text() == "Owner working content\n"
    with s.fixture.db.read() as uow:
        version = uow.connection.execute("SELECT git_commit,snapshot_json FROM table_versions WHERE business_id IS NULL").fetchone()
        assert version["git_commit"] == receipt["commit"] and version["snapshot_json"] is None







def definition(s, kind):
    value = s.runtime.background.programs.get(kind)
    value.pop("validate")
    return value


def test_business_publication_keeps_its_definition_and_global_uses_a_separate_internal_program(s):
    assert definition(s, "tables.version") == BUSINESS_VERSION_DEFINITION
    internal = definition(s, "tables.global-version")
    assert internal["scope_kinds"] == ["global"] and internal["global_capabilities"] == ["read_global_context"]
    assert internal["external"] is True and internal["input_schema"] == BUSINESS_VERSION_DEFINITION["input_schema"]
    assert internal["digest"] != BUSINESS_VERSION_DEFINITION["digest"]
    made = ok(business(s, "tables_create", {"title": "Business", "columns": COLUMNS}))
    shared = make(s)
    with s.fixture.db.read() as uow:
        rows = {row["id"]: row for row in uow.connection.execute("SELECT id,kind,business_id,target_namespace,definition_ref_json FROM jobs")}
    assert (rows[made["job_id"]]["kind"], rows[made["job_id"]]["business_id"]) == ("tables.version", s.fixture.business_id)
    assert json.loads(rows[made["job_id"]]["definition_ref_json"])["digest"] == BUSINESS_VERSION_DEFINITION["digest"]
    assert (rows[shared["job_id"]]["kind"], rows[shared["job_id"]]["business_id"]) == ("tables.global-version", None)
    assert json.loads(rows[shared["job_id"]]["definition_ref_json"])["digest"] == internal["digest"]
    # A business context cannot queue the internal Global program.
    with s.fixture.db.read() as uow, pytest.raises(DomainError) as refused:
        s.runtime.background.programs.job_values(s.fixture.ctx, "tables.global-version", {"version_id": new_id()}, uow)
    assert refused.value.code == "forbidden"


def test_global_and_business_publication_never_publish_each_others_pending_versions(s):
    business_made = ok(business(s, "tables_create", {"title": "Business", "columns": COLUMNS}))
    global_made = make(s)
    run(s, global_made["job_id"])
    with s.fixture.db.read() as uow:
        pending = {(row["business_id"], row["id"]) for row in uow.connection.execute("SELECT business_id,id FROM table_versions WHERE git_commit IS NULL")}
    assert pending == {(s.fixture.business_id, business_made["table"]["versioning"]["version_id"])}
    assert git(s, "rev-list", "--count", HISTORY_REF) == "1"
    second = ok(gcall(s, "tables_rows_write", {"table_id": global_made["table"]["id"], "rows": [{"cells": {"name": "G"}}]}, revision=1))
    run(s, business_made["job_id"])
    with s.fixture.db.read() as uow:
        pending = {(row["business_id"], row["id"]) for row in uow.connection.execute("SELECT business_id,id FROM table_versions WHERE git_commit IS NULL")}
    assert pending == {(None, second["table"]["versioning"]["version_id"])}
    run(s, second["job_id"])
    assert git(s, "rev-list", "--count", HISTORY_REF) == "3"


def test_global_writes_need_the_internal_program_worker_but_business_tables_do_not(s):
    with s.fixture.db.read() as uow:
        worker = s.runtime.background.authority.worker_id("tables.global-version", uow)
    with s.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (worker,))
    refused = gcall(s, "tables_create", {"title": "Unregistered", "columns": COLUMNS})
    assert refused.status == "error" and refused.error["code"] == "invalid_principal", refused.to_dict()
    assert ok(business(s, "tables_create", {"title": "Unaffected", "columns": COLUMNS}))["table"]["business_id"] == s.fixture.business_id
    with s.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM data_tables WHERE business_id IS NULL").fetchone()[0] == 0
