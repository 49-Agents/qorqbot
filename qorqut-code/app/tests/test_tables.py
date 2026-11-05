import json
from pathlib import Path
import subprocess

import pytest

from qorqut_operations.contracts import InvocationOptions, new_id
from qorqut_operations.tables.history import HISTORY_REF
from starlette.testclient import TestClient
from test_service_transports import ServiceFixture


@pytest.fixture
def service(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    root = service.fixture.root
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return service


def call(service, name, args=None, revision=None, key=None, actor=None):
    ctx = service.runtime.resolver.invocation_context(actor) if actor else service.fixture.ctx
    result = service.runtime.application.invoke(ctx, name, args or {}, InvocationOptions(expected_revision=revision, idempotency_key=key or new_id()))
    return result.to_dict()


def ok(result):
    assert result["status"] == "ok", result
    return result["data"]


def create(service, **extra):
    return ok(call(service, "tables_create", {"title": "CRM", "columns": [
        {"id": "name", "name": "Name", "type": "text"},
        {"id": "amount", "name": "Amount", "type": "number"},
        {"id": "metadata", "name": "Details", "type": "json"}], **extra}))


def git(service, *args):
    return subprocess.run(["git", "-C", str(service.fixture.root), *args], check=True, capture_output=True, text=True).stdout.strip()


def run(service, job):
    result = service.runtime.background.run_one(job)
    assert result["state"] == "succeeded", result


def test_crm_edits_preserve_column_identity_and_atomic_rows(service):
    made = create(service)
    table = made["table"]
    args = {"table_id": table["id"], "rows": [{"cells": {"name": "A <client>", "amount": 12.5}}, {"cells": {"name": "B", "metadata": {"channels": ["email"]}}}]}
    key = new_id()
    saved = ok(call(service, "tables_rows_write", args, 1, key))
    assert saved["table"]["revision"] == 2
    assert saved["table"]["row_count"] == 2
    assert [row["cells"]["name"] for row in ok(call(service, "tables_rows_list", {"table_id": table["id"]}))["items"]] == ["A <client>", "B"]
    assert ok(call(service, "tables_rows_write", args, 1, key)) == saved
    first = saved["rows"][0]
    patched = ok(call(service, "tables_rows_write", {"table_id": table["id"], "rows": [{"id": first["id"], "cells": {"amount": None}}]}, 2))
    assert patched["rows"][0]["cells"] == {"name": "A <client>", "amount": None}
    columns = [{**column, "name": "Customer" if column["id"] == "name" else column["name"]} for column in table["columns"]]
    renamed = ok(call(service, "tables_update", {"table_id": table["id"], "changes": {"columns": columns}}, 3))
    assert renamed["table"]["revision"] == 4
    found = ok(call(service, "tables_rows_list", {"table_id": table["id"], "equals": {"name": "A <client>"}}))["items"]
    assert found[0]["id"] == first["id"] and found[0]["cells"]["name"] == "A <client>"
    assert call(service, "tables_rows_write", args, 1)["error"]["code"] == "revision_conflict"
    bad = call(service, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "Valid"}}, {"cells": {"amount": True}}]}, 4)
    assert bad["error"]["code"] == "validation_error"
    assert ok(call(service, "tables_get", {"table_id": table["id"]}))["table"]["row_count"] == 2


def test_each_version_is_committed_even_when_newer_job_runs_first(service):
    made = create(service)
    identity = made["table"]["id"]
    saved = ok(call(service, "tables_rows_write", {"table_id": identity, "rows": [{"cells": {"name": "One"}}]}, 1))
    row_id = saved["rows"][0]["id"]
    patched = ok(call(service, "tables_rows_write", {"table_id": identity, "rows": [{"id": row_id, "cells": {"name": "Two"}}]}, 2))
    run(service, patched["job_id"])
    assert git(service, "rev-list", "--count", HISTORY_REF) == "3"
    history = ok(call(service, "tables_history", {"table_id": identity}))["items"]
    assert [row["table_revision"] for row in history] == [3, 2, 1]
    for row, wanted in zip(history, ["Two", "One", None]):
        snapshot = json.loads(git(service, "show", row["git_commit"] + ":" + row["path"]))
        assert (snapshot["rows"][0]["cells_json"]["name"] if snapshot["rows"] else None) == wanted
        assert row["actor_id"] == service.fixture.owner.id
    run(service, made["job_id"])
    run(service, saved["job_id"])
    assert git(service, "rev-list", "--count", HISTORY_REF) == "3"
    assert json.loads((service.fixture.hq / "tables" / (identity + ".json")).read_text())["rows"][0]["cells_json"]["name"] == "Two"
    assert not (service.fixture.root / ".git/index").exists()
    with service.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM table_versions WHERE snapshot_json IS NOT NULL").fetchone()[0] == 0


def test_failed_git_keeps_snapshot_and_retry_does_not_duplicate_version(service, monkeypatch):
    made = create(service)
    history = service.runtime.domains.tables.history
    runner = history.runner
    def broken(*args, **kwargs):
        raise OSError("test Git failure")
    monkeypatch.setattr(history, "runner", broken)
    failed = service.runtime.background.run_one(made["job_id"])
    assert failed["error"]["code"] == "versioning_failed"
    with service.fixture.db.read() as uow:
        version = uow.repository("table_versions", business_id=service.fixture.business_id).get(made["table"]["versioning"]["version_id"])
        assert version["snapshot_json"] and version["git_commit"] is None
    monkeypatch.setattr(history, "runner", runner)
    retry = ok(call(service, "tables_history_retry", {"version_id": version["id"]}))
    run(service, retry["job_id"])
    again = ok(call(service, "tables_history_retry", {"version_id": version["id"]}))
    run(service, again["job_id"])
    assert git(service, "rev-list", "--count", HISTORY_REF) == "1"


def test_remove_column_drops_values_and_readd_does_not_resurrect_them(service):
    made = create(service)
    identity = made["table"]["id"]
    saved = ok(call(service, "tables_rows_write", {"table_id": identity, "rows": [{"cells": {"name": "A", "amount": 9}}]}, 1))
    columns = made["table"]["columns"]
    ok(call(service, "tables_update", {"table_id": identity, "changes": {"columns": [columns[0]]}}, 2))
    ok(call(service, "tables_update", {"table_id": identity, "changes": {"columns": columns}}, 3))
    assert ok(call(service, "tables_rows_list", {"table_id": identity}))["items"][0]["cells"] == {"name": "A"}
    removed = ok(call(service, "tables_rows_write", {"table_id": identity, "delete_ids": [saved["rows"][0]["id"]]}, 4))
    assert removed["rows"] == [] and removed["deleted_ids"] == [saved["rows"][0]["id"]]
    assert removed["table"]["row_count"] == 0


def test_tags_stories_and_archive(service):
    story = ok(call(service, "stories_create", {"title": "Campaign", "purpose": "Customer launch"}))["story"]
    made = create(service, tags={"topics": ["sales"], "campaigns": ["launch"], "departments": ["marketing"]}, story_ids=[story["id"]])
    identity = made["table"]["id"]
    assert ok(call(service, "tables_list", {"topic": "sales", "campaign": "launch", "department": "marketing"}))["items"][0]["id"] == identity
    links = ok(call(service, "stories_get", {"story_id": story["id"]}))["story"]["links"]
    assert links[0]["resource_type"] == "data_tables" and links[0]["current"]["label"] == "CRM"
    ok(call(service, "tables_update", {"table_id": identity, "changes": {"archived": True}}, 1))
    assert not ok(call(service, "tables_list"))["items"]
    assert ok(call(service, "tables_list", {"include_archived": True}))["items"]
    assert call(service, "tables_rows_write", {"table_id": identity, "rows": [{"cells": {"name": "X"}}]}, 2)["error"]["code"] == "validation_error"


def test_tables_and_row_ids_cannot_cross_business_or_table(service):
    a, b = create(service), create(service)
    saved = ok(call(service, "tables_rows_write", {"table_id": a["table"]["id"], "rows": [{"cells": {"name": "A"}}]}, 1))
    result = call(service, "tables_rows_write", {"table_id": b["table"]["id"], "rows": [{"id": saved["rows"][0]["id"], "cells": {"name": "B"}}]}, 1)
    assert result["error"]["code"] == "not_found"
    other = new_id()
    private = new_id()
    with service.fixture.db.transaction() as uow:
        uow.repository("businesses").insert({"id": other, "name": "Elsewhere", "slug": "elsewhere", "workspace": "/elsewhere", "hq_path": "/elsewhere/hq"})
        uow.repository("data_tables", business_id=other).insert({"id": private, "title": "Private", "columns_json": a["table"]["columns"], "created_by": service.fixture.owner.id})
    assert len(ok(call(service, "tables_list"))["items"]) == 2
    for operation in ("tables_get", "tables_rows_list", "tables_history"):
        assert call(service, operation, {"table_id": private})["error"]["code"] == "not_found"


def test_pagination_rejects_stale_cursor_and_json_search_is_type_exact(service):
    made = create(service)
    identity = made["table"]["id"]
    ok(call(service, "tables_rows_write", {"table_id": identity, "rows": [{"cells": {"metadata": True}}, {"cells": {"metadata": 1}}, {"cells": {"metadata": "1"}}]}, 1))
    first = ok(call(service, "tables_rows_list", {"table_id": identity, "limit": 2}))
    assert len(ok(call(service, "tables_rows_list", {"table_id": identity, "limit": 2, "cursor": first["next_cursor"]}))["items"]) == 1
    assert len(ok(call(service, "tables_rows_list", {"table_id": identity, "equals": {"metadata": True}}))["items"]) == 1
    ok(call(service, "tables_rows_write", {"table_id": identity, "rows": [{"cells": {"name": "Later"}}]}, 2))
    stale = call(service, "tables_rows_list", {"table_id": identity, "limit": 2, "cursor": first["next_cursor"]})
    assert stale["error"]["code"] == "validation_error"


def test_agents_share_tables_through_mcp_without_plugins(service):
    with TestClient(service.web) as client:
        agent = service.initialize(client, service.agent)
        manager = service.initialize(client, service.manager)
        made = ok(service.tool(client, agent, "tables_create", {"title": "Shared pipeline",
            "columns": [{"id": "name", "name": "Name", "type": "text"}], "idempotency_key": new_id()})["decoded"])
        identity = made["table"]["id"]
        saved = ok(service.tool(client, manager, "tables_rows_write", {"table_id": identity, "business_id": service.fixture.business_id,
            "expected_revision": 1, "rows": [{"cells": {"name": "Shared lead"}}], "idempotency_key": new_id()})["decoded"])
        assert saved["rows"][0]["updated_by"] == service.manager.id
        found = ok(service.tool(client, agent, "tables_rows_list", {"table_id": identity})["decoded"])
        assert found["items"][0]["cells"]["name"] == "Shared lead"
        service.fixture.update(service.agent, {"tool_policy": {"overrides": {"tables_rows_write": "deny"}}})
        denied = service.tool(client, agent, "tables_rows_write", {"table_id": identity,
            "expected_revision": 2, "rows": [{"cells": {"name": "Denied"}}], "idempotency_key": new_id()})["decoded"]
        assert denied["error"]["code"] == "policy_denied"


def test_signoff_captures_history_only_after_approval_and_stale_proposal_cannot_overwrite(service):
    service.fixture.update(service.agent, {"tool_policy": {"overrides": {"tables_rows_write": "signoff"}}})
    table = create(service)["table"]
    args = {"table_id": table["id"], "rows": [{"cells": {"name": "Proposed lead"}}]}
    proposed = call(service, "tables_rows_write", args, 1, actor=service.agent)
    assert proposed["status"] == "signoff_required", proposed
    assert len(ok(call(service, "tables_history", {"table_id": table["id"]}))["items"]) == 1
    assert ok(call(service, "tables_get", {"table_id": table["id"]}))["table"]["row_count"] == 0

    def approve(proposal):
        ref = proposal["signoff"]
        result = service.runtime.signoffs.review(service.fixture.ctx, ref["id"], decision="approve",
            proposal_revision=ref["proposal_revision"], payload_hash=ref["payload_hash"],
            expected_status_revision=ref["status_revision"], idempotency_key=new_id())
        if result.status == "accepted":
            service.runtime.signoffs.run_one(ref["id"])
        return result

    assert approve(proposed).status == "accepted"
    assert len(ok(call(service, "tables_history", {"table_id": table["id"]}))["items"]) == 2
    second = call(service, "tables_rows_write", args, 2, actor=service.agent)
    ok(call(service, "tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "Owner edit"}}]}, 2))
    approve(second)
    assert len(ok(call(service, "tables_rows_list", {"table_id": table["id"]}))["items"]) == 2
    assert len(ok(call(service, "tables_history", {"table_id": table["id"]}))["items"]) == 3


def test_git_recovery_preserves_owner_head_index_and_working_files(service, monkeypatch):
    original = service.fixture.root / "owner.txt"
    original.write_text("Owner committed content\n")
    git(service, "add", "owner.txt")
    git(service, "-c", "user.name=Owner", "-c", "user.email=owner@example.test", "commit", "-qm", "Owner baseline")
    head = git(service, "rev-parse", "HEAD")
    original.write_text("Owner staged content\n")
    git(service, "add", "owner.txt")
    original.write_text("Owner working content\n")
    index = (service.fixture.root / ".git/index").read_bytes()
    made = create(service)
    history, receipt = service.runtime.domains.tables.history, {}
    commit = history.commit

    def crash(version, control):
        receipt["commit"] = commit(version, control)
        raise OSError("Simulated stop after Git ref update, before SQLite receipt")

    monkeypatch.setattr(history, "commit", crash)
    with pytest.raises(OSError, match="Simulated stop"):
        service.runtime.background.run_one(made["job_id"])
    with service.fixture.db.read() as uow:
        assert uow.repository("jobs", business_id=service.fixture.business_id).get(made["job_id"])["state"] == "failed"
    monkeypatch.setattr(history, "commit", commit)
    retry = ok(call(service, "tables_history_retry", {"version_id": made["table"]["versioning"]["version_id"]}))
    run(service, retry["job_id"])
    assert git(service, "rev-list", "--count", HISTORY_REF) == "1"
    assert git(service, "rev-parse", HISTORY_REF) == receipt["commit"]
    assert git(service, "rev-parse", "HEAD") == head
    assert (service.fixture.root / ".git/index").read_bytes() == index
    assert original.read_text() == "Owner working content\n"


def test_numeric_filtering_and_failed_snapshot_roll_back_the_whole_batch(service, monkeypatch):
    table = create(service)["table"]
    args = {"table_id": table["id"]}
    ok(call(service, "tables_rows_write", {**args, "rows": [{"cells": {"name": "00123", "amount": 1.0}}]}, 1))
    found = ok(call(service, "tables_rows_list", {**args, "equals": {"amount": 1}}))["items"]
    assert found[0]["cells"]["name"] == "00123"
    assert not ok(call(service, "tables_rows_list", {**args, "query": "amount"}))["items"]
    monkeypatch.setattr("qorqut_operations.tables.operations.MAX_SNAPSHOT", 500)
    bad = call(service, "tables_rows_write", {**args, "rows": [{"cells": {"name": "Oversized"}}]}, 2)
    assert bad["error"]["code"] == "validation_error"
    current = ok(call(service, "tables_get", args))["table"]
    assert current["revision"] == 2 and current["row_count"] == 1
    assert len(ok(call(service, "tables_history", args))["items"]) == 2
