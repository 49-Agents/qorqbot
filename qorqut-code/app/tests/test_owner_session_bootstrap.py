"""Owner-session bootstrap: orientation only, no operational inventory, no writes."""

import json

import pytest

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.owner_bootstrap import NAME
from test_owner_session_mcp import call, code, connect, enable, issue, ok, tool_names
from test_service_transports import ServiceFixture


@pytest.fixture
def full(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    enable(service.fixture)
    service.token = issue(service.fixture)
    with service.fixture.db.transaction() as uow:
        row = uow.repository("businesses").get(service.fixture.business_id)
        config = {**row["config_json"], "engineering": {"deploy": "owner"},
                  "code": [{"id": "widget-app", "path": "code/widget-app"}]}
        uow.connection.execute("UPDATE businesses SET config_json=? WHERE id=?",
                               (json.dumps(config), service.fixture.business_id))
    return service


def counts(fixture):
    with fixture.db.read() as uow:
        tables = [row[0] for row in uow.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: uow.connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in tables}


def test_bootstrap_returns_business_identity_and_engineering_references(full):
    fixture = full.fixture
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        assert NAME in tool_names(client, headers)
        data = ok(call(client, headers, NAME, {}))
    assert data["business"] == {"id": fixture.business_id, "slug": "widget", "name": "Widget",
                                "stage": "validating", "timezone": "UTC", "revision": 1}
    engineering = data["engineering"]
    assert engineering["code_root"] == str(fixture.hq.parent) + "/code"
    assert engineering["authority"] == {"deploy": "owner"}
    assert engineering["repositories"] == [{"id": "widget-app", "path": "code/widget-app"}]
    assert any(fixture.business_id in line for line in data["guidance"])
    # The authored pillar documents are ordinary scoped sources, never an injected brief: the only brief is the graph.
    assert {brief["key"] for brief in data["briefs"]} <= {"context_home"}
    assert all(brief["text"].strip() for brief in data["briefs"])
    assert data["character_count"] <= data["character_limit"]


def test_freshness_metadata_accompanies_every_included_source(full):
    with full.fixture.db.read() as uow:
        registered = {row["metadata_json"]["bootstrap_key"]: row for row in uow.repository(
            "documents", business_id=full.fixture.business_id).list(filters={"kind": "context"}, limit=50)}
    with TestClient(full.web) as client:
        data = ok(call(client, connect(client, full.token), NAME, {}))
    index = data["context_index"]
    assert index["total"] >= len(PILLARS) and index["items"]
    listed = {item["id"]: item for item in index["items"]}
    for row in registered.values():
        assert listed[row["id"]]["revision"] == row["revision"]
        assert listed[row["id"]]["content_hash"].removeprefix("sha256:") == row["content_hash"].removeprefix("sha256:")
    for item in index["items"]:
        assert set(item) == {"id", "title", "kind", "revision", "content_hash", "availability", "updated_at"}
        assert item["revision"] >= 1 and item["content_hash"]
    # Metadata only: the index never carries document bodies.
    assert all("body" not in item and "excerpt" not in item for item in index["items"])


def test_bootstrap_excludes_tasks_stories_and_signoffs(full):
    with TestClient(full.web) as client:
        agent_headers = full.initialize(client, full.agent)
        full.tool(client, agent_headers, "tasks_create",
                  {"title": "Unrelated queued task", "idempotency_key": new_id()})
        full.tool(client, agent_headers, "stories_create",
                  {"title": "Unrelated campaign story", "purpose": "Learn pricing",
                   "idempotency_key": new_id()})
        headers = connect(client, full.token)
        data = ok(call(client, headers, NAME, {}))
    with full.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert uow.connection.execute("SELECT count(*) FROM stories").fetchone()[0] == 1
    serialized = json.dumps(data)
    assert "Unrelated queued task" not in serialized and "Unrelated campaign story" not in serialized
    assert set(data) == {"business", "engineering", "briefs", "context_index", "coordination",
                         "guidance", "missing", "character_count", "character_limit"}
    assert set(data["coordination"]) == {"coordinator_id", "coordinator_name", "available", "reason", "operations"}

    def keys(value):
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        if isinstance(value, list):
            return {key for item in value for key in keys(item)}
        return set()

    assert not keys(data) & {"tasks", "task", "inbox", "signoffs", "stories", "work_briefing",
                             "assignments", "focus", "unread"}


def test_bootstrap_performs_no_database_writes(full):
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        # Snapshot after connection so ordinary service startup bookkeeping,
        # which this read never performs, is outside the comparison.
        ok(call(client, headers, NAME, {}))
        # The signoff notifier's schedule row is timer-driven service bookkeeping.
        full.runtime.signoffs.notifications.tick()
        before = counts(full.fixture)
        with full.fixture.db.read() as uow:
            documents = [(row["id"], row["revision"], row["updated_at"]) for row in uow.repository(
                "documents", business_id=full.fixture.business_id).list(limit=50)]
        ok(call(client, headers, NAME, {}))
        ok(call(client, headers, NAME, {}))
    assert counts(full.fixture) == before
    with full.fixture.db.read() as uow:
        assert [(row["id"], row["revision"], row["updated_at"]) for row in uow.repository(
            "documents", business_id=full.fixture.business_id).list(limit=50)] == documents


def test_registered_agents_cannot_call_the_owner_bootstrap(full):
    with TestClient(full.web) as client:
        agent_headers = full.initialize(client, full.agent)
        names = {tool["name"] for tool in client.post("/mcp", headers=agent_headers, json={
            "jsonrpc": "2.0", "id": new_id(), "method": "tools/list", "params": {}}).json()["result"]["tools"]}
        assert NAME not in names
        decoded = full.tool(client, agent_headers, NAME, {})["decoded"]
        assert decoded["status"] == "error" and decoded["error"]["code"] == "policy_denied", decoded


def test_absent_briefs_and_configuration_report_missing_diagnostics(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    enable(service.fixture)
    token = issue(service.fixture)
    with service.fixture.db.transaction() as uow:
        uow.connection.execute("DELETE FROM documents WHERE business_id=?", (service.fixture.business_id,))
    with TestClient(service.web) as client:
        data = ok(call(client, connect(client, token), NAME, {}))
    assert data["briefs"] == [] and data["context_index"] == {
        "items": [], "total": 0, "truncated": False, "projection": "registered_metadata"}
    reported = {(item["source"], item["reason"]) for item in data["missing"]}
    assert not {source for source, _ in reported if source.startswith("brief:")}
    assert ("context_index", "no_documents") in reported
    assert ("engineering.authority", "unconfigured") in reported
    assert ("engineering.repositories", "none_registered") in reported
    assert all(item["detail"] for item in data["missing"])


def test_an_unreadable_authored_pillar_file_does_not_affect_the_orientation(full):
    fixture = full.fixture
    (fixture.hq / "context/icp.md").unlink()
    with TestClient(full.web) as client:
        data = ok(call(client, connect(client, full.token), NAME, {}))
    assert {brief["key"] for brief in data["briefs"]} <= {"context_home"}
    assert not [item for item in data["missing"] if item["source"].startswith("brief:")]


def test_owner_bootstrap_is_denied_outside_mcp(full):
    result = TestClient(full.web).post("/api/biz/widget/operations/" + NAME,
                                       headers={"Origin": "http://testserver"}, json={})
    assert result.status_code in {403, 404}, result.text


PILLARS = ("product", "icp", "brand")      # the authored pillar documents: ordinary scoped sources, listed in the index only
STALE = "Widget is only an IDE"


def stale_pillars(fixture):
    for key in PILLARS:
        (fixture.hq / f"context/{key}.md").write_text(
            f"---\ntitle: {key}\n---\n\n## Agent Brief\n\n{STALE} ({key}).\n")


def test_the_published_graph_text_is_the_only_brief_and_authored_pillars_are_not_injected(full):
    stale_pillars(full.fixture)
    home = {"text": "Shared Context (published graph)\nRevision abc: 1 nodes.\n- node:alice \u2014 Alice Rivera"}
    calls = []
    full.runtime.owner_bootstrap.context_home = lambda business_id: calls.append(business_id) or home["text"]
    full.runtime.owner_bootstrap.context_status = lambda: {"status": "ready", "revision": "abc", "reason": None}
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        data = ok(call(client, headers, NAME, {}))
        home["text"] = "Shared Context (published graph)\nRevision def: 2 nodes."
        fresh = ok(call(client, headers, NAME, {}))
    assert calls == [full.fixture.business_id] * 2
    assert [brief["key"] for brief in data["briefs"]] == ["context_home"]
    assert data["briefs"][0] == {"key": "context_home", "source": "context:published", "characters": len(
        "Shared Context (published graph)\nRevision abc: 1 nodes.\n- node:alice \u2014 Alice Rivera"),
        "truncated": False, "text": "Shared Context (published graph)\nRevision abc: 1 nodes.\n- node:alice \u2014 Alice Rivera"}
    # The authored pillar files are ordinary scoped sources: their text is never carried beside the graph.
    assert STALE not in json.dumps(data["briefs"])
    assert not any(item["source"] == "context_home" for item in data["missing"])
    assert data["character_count"] <= data["character_limit"]
    # Read fresh on every call: no cached home survives a publication.
    assert fresh["briefs"][0]["text"] == "Shared Context (published graph)\nRevision def: 2 nodes."
    assert "context-base:" not in json.dumps(data) and "context_edit" not in json.dumps(data)


def test_absent_provider_is_reported_and_no_authored_brief_takes_its_place(full):
    full.runtime.owner_bootstrap.context_home = None
    with TestClient(full.web) as client:
        data = ok(call(client, connect(client, full.token), NAME, {}))
    assert data["briefs"] == []
    marker = [item for item in data["missing"] if item["source"] == "context_home"]
    assert len(marker) == 1 and marker[0]["reason"] == "not_configured"


@pytest.mark.parametrize("returned", [None, "", "  \n"])
def test_a_provider_without_text_is_reported_and_not_replaced_by_authored_briefs(full, returned):
    full.runtime.owner_bootstrap.context_home = lambda business_id: returned
    with TestClient(full.web) as client:
        data = ok(call(client, connect(client, full.token), NAME, {}))
    assert data["briefs"] == []
    marker = [item for item in data["missing"] if item["source"] == "context_home"]
    assert len(marker) == 1 and marker[0]["reason"] == "unavailable" and marker[0]["detail"]


@pytest.mark.parametrize("state", ["not_initialized", "unavailable"])
def test_a_graph_that_is_not_readable_is_reported_never_shown_as_empty(full, state):
    full.runtime.owner_bootstrap.context_home = lambda business_id: "Shared Context (published graph)\nNot initialized."
    full.runtime.owner_bootstrap.context_status = lambda: {"status": state, "revision": None,
                                                           "reason": {"code": "x", "message": "No revision"}}
    with TestClient(full.web) as client:
        data = ok(call(client, connect(client, full.token), NAME, {}))
    marker = [item for item in data["missing"] if item["source"] == "context_home"]
    assert len(marker) == 1 and marker[0]["reason"] == state and marker[0]["detail"] == "No revision"
    assert data["briefs"][0]["key"] == "context_home" and "empty" not in data["briefs"][0]["text"].lower()


def test_context_home_is_bounded_and_marks_provider_truncation(full):
    from bazarlyk.owner_bootstrap import MAX_CHARACTERS
    from qorqut_context.integration.reading.text import TRUNCATED
    full.runtime.owner_bootstrap.context_home = lambda business_id: "x" * (MAX_CHARACTERS + 10)
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        big = ok(call(client, headers, NAME, {}))
        full.runtime.owner_bootstrap.context_home = lambda business_id: "Home: short" + TRUNCATED
        marked = ok(call(client, headers, NAME, {}))
    brief = big["briefs"][0]
    assert brief["truncated"] is True and brief["characters"] == len(brief["text"]) == MAX_CHARACTERS
    assert big["character_count"] == MAX_CHARACTERS == big["character_limit"]
    assert marked["briefs"][0]["truncated"] is True
    # A derived graph projection claims no document provenance.
    assert set(brief) == {"key", "source", "characters", "truncated", "text"}
