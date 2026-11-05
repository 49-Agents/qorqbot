"""One canonical completed-action log: scope, sharing, San Juan time and legacy history."""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from bazarlyk import local_time
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from bazarlyk.domains.common import PlanBuilder
from bazarlyk.storage import Database
from test_service_transports import ORIGIN, ServiceFixture

TWENTY = " ".join(["word"] * 20)


@pytest.fixture
def s(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    fx = s.fixture
    s.app, s.home = s.runtime.application, fx.business_id
    s.second, s.third = new_id(), new_id()
    with fx.db.transaction() as uow:
        for business, slug in ((s.second, "second"), (s.third, "third")):
            (fx.root / slug / "hq").mkdir(parents=True)
            uow.repository("businesses").insert({"id": business, "slug": slug, "name": slug.title(), "stage": "validating",
                "workspace": str(fx.root / slug), "hq_path": str(fx.root / slug / "hq")})
            uow.repository("business_memberships", business_id=business).insert({"principal_id": fx.owner.id, "is_owner": True})
    everything = frozenset({s.home, s.second, s.third})
    fx.ctx = replace(fx.ctx, allowed_business_ids=everything)
    s.owner = replace(fx.ctx, business_id=None, review_business_id=None)
    # Ordubashy always spans every business; a Global Marketing agent has a partial scope.
    s.marketer = fx.create("outreach", "global-marketing", scope={"kind": "ecosystem", "allowed_business_ids": [s.home, s.second],
                                                                  "global_capabilities": ["read_global_context"]})
    s.ordu = s.runtime.resolver.invocation_context(s.marketer)
    s.biz = s.runtime.resolver.invocation_context(s.agent)
    s.activity = s.runtime.registry.get("actions_record").prepare.__self__
    return s


def call(s, ctx, op, args, *, revision=None, key=None):
    return s.app.invoke(ctx, op, args, {"idempotency_key": key or new_id(),
                                        **({"expected_revision": revision} if revision is not None else {})})


def ok(result):
    assert result.status == "ok", result.to_dict()
    return result.to_dict()["data"]


def error(result):
    assert result.status == "error", result.to_dict()
    return result.error["code"]


def count(s, table="action_logs"):
    with s.fixture.db.read() as uow:
        return uow.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def listed(s, ctx, **args):
    return ok(call(s, ctx, "actions_list", args))["actions"]


def test_business_agent_records_only_its_home_business(s):
    action = ok(call(s, s.biz, "actions_record", {"text": TWENTY, "evidence": {"url": "https://example.test/pr"}}))["action"]
    assert action["category"] == "business-operational" and [b["id"] for b in action["businesses"]] == [s.home]
    assert action["origin"] == "agent" and action["performed_by"]["id"] == action["recorded_by"]["id"] == s.agent.id
    assert action["action_id"].startswith("business/" + s.home + "/") and action["occurred"]["precision"] == "instant"
    assert error(call(s, s.biz, "actions_record", {"text": TWENTY + " more"})) == "validation_error"
    assert error(call(s, s.biz, "actions_record", {"text": "   "})) == "validation_error"
    assert error(call(s, s.biz, "actions_record", {"text": "x" * 2001})) == "validation_error"
    assert error(call(s, s.biz, "actions_record", {"text": "Done", "category": "global-operational"})) == "forbidden"
    assert error(call(s, s.biz, "actions_record", {"text": "Done", "business_ids": [s.second]})) == "forbidden"
    assert error(call(s, s.biz, "actions_record", {"text": "Done", "business_ids": [s.home, s.second]})) == "forbidden"
    assert error(call(s, s.biz, "actions_record", {"text": "Done", "task_id": "missing"})) == "validation_error"
    # Legacy logs_add is the same canonical domain and rule.
    assert error(call(s, s.biz, "logs_add", {"tag": "general", "summary": TWENTY + " more"})) == "validation_error"
    ok(call(s, s.biz, "logs_add", {"tag": "general", "summary": "Shipped onboarding email"}))
    assert count(s) == 2 and count(s, "action_businesses") == 2
    assert {a["text"] for a in listed(s, s.biz)} == {TWENTY, "Shipped onboarding email"}
    config = ok(call(s, s.biz, "actions_config_get", {}))
    assert config["scope"]["allowed_categories"] == ["business-operational"] and config["timezone"] == local_time.TIMEZONE
    assert config["agent_word_limit"] == 20 and config["scope"]["default_business_ids"] == [s.home]


def test_global_agent_shares_one_action_atomically_with_authorized_businesses(s):
    assert error(call(s, s.ordu, "actions_record", {"text": "Reviewed launch"})) == "validation_error"
    assert error(call(s, s.ordu, "actions_record", {"text": "Reviewed", "category": "global-systems"})) == "forbidden"
    assert error(call(s, s.ordu, "actions_record", {"text": "Reviewed", "category": "business-operational",
                                                    "business_ids": [s.home, s.third]})) == "forbidden"
    assert error(call(s, s.ordu, "actions_record", {"text": "Reviewed", "category": "business-operational"})) == "validation_error"
    assert error(call(s, s.ordu, "actions_record", {"text": "Reviewed", "category": "global-operational",
                                                    "business_ids": [s.home]})) == "validation_error"
    assert count(s) == 0 and count(s, "action_businesses") == 0
    shared = ok(call(s, s.ordu, "actions_record", {"text": "Aligned pricing across both stores", "category": "business-operational",
                                                   "business_ids": [s.home, s.second], "evidence": {"commit": "abc123"}}))["action"]
    assert shared["action_id"].startswith("global/") and {b["id"] for b in shared["businesses"]} == {s.home, s.second}
    ok(call(s, s.ordu, "actions_record", {"text": "Renewed shared domain", "category": "global-operational"}))
    # The business sees the shared text once, only its own membership and no Global evidence.
    seen = listed(s, s.biz)
    assert [a["text"] for a in seen] == ["Aligned pricing across both stores"]
    assert [b["id"] for b in seen[0]["businesses"]] == [s.home]
    assert seen[0]["refs"] == {} and seen[0]["legacy"] is None
    assert ok(call(s, s.biz, "actions_get", {"action_id": shared["action_id"]}))["action"]["businesses"] == seen[0]["businesses"]
    # The owner's third-business and owner-only actions are invisible to the Global agent.
    private = ok(call(s, s.owner, "actions_record", {"text": "Third-store contract", "category": "business-operational",
                                                     "business_ids": [s.third]}))["action"]
    systems = ok(call(s, s.owner, "actions_record", {"text": "Rotated backups", "category": "global-systems"}))["action"]
    visible = {a["action_id"] for a in listed(s, s.ordu)}
    assert private["action_id"] not in visible and systems["action_id"] not in visible and shared["action_id"] in visible
    assert error(call(s, s.ordu, "actions_get", {"action_id": private["action_id"]})) == "not_found"
    assert error(call(s, s.biz, "actions_get", {"action_id": systems["action_id"]})) == "not_found"
    assert error(call(s, s.ordu, "actions_list", {"business": s.third})) == "forbidden"
    assert [a["action_id"] for a in listed(s, s.owner, business=s.second)] == [shared["action_id"]]
    assert {a["action_id"] for a in listed(s, s.owner, categories=["global-systems"])} == {systems["action_id"]}
    # Owner aggregation counts the shared action once.
    with s.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT COUNT(DISTINCT action_key) FROM action_businesses").fetchone()[0] == 2


def test_revoked_business_is_rechecked_before_apply(s):
    args = {"text": "Aligned pricing", "category": "business-operational", "business_ids": [s.home, s.second]}
    with s.fixture.db.read() as uow:
        plan = s.activity.prepare_record(s.ordu, args, InvocationOptions(idempotency_key=new_id()), uow)
    revoked = replace(s.ordu, allowed_business_ids=frozenset({s.home}))
    with s.fixture.db.read() as uow, pytest.raises(DomainError) as raised:
        s.activity.validate_record(revoked, plan, uow)
    assert raised.value.code == "forbidden"


def test_owner_edits_only_own_manual_actions_with_revision_and_idempotency(s):
    key = new_id()
    first = ok(call(s, s.owner, "actions_record", {"text": " ".join(["long"] * 40), "category": "business-operational",
                                                   "business_ids": [s.home, s.second]}, key=key))["action"]
    assert ok(call(s, s.owner, "actions_record", {"text": " ".join(["long"] * 40), "category": "business-operational",
                                                  "business_ids": [s.home, s.second]}, key=key))["action"] == first
    assert count(s) == 1 and first["editable"] and first["origin"] == "manual"
    unclassified = ok(call(s, s.owner, "actions_record", {"text": "Thinking time"}))["action"]
    assert unclassified["category"] is None and unclassified["businesses"] == []
    stale = call(s, s.owner, "actions_update", {"action_id": first["action_id"], "text": "Edited"}, revision=first["revision"] + 1)
    assert error(stale) == "revision_conflict"
    assert error(call(s, s.owner, "actions_update", {"action_id": first["action_id"], "text": "Edited"})) == "revision_conflict"
    edited = ok(call(s, s.owner, "actions_update", {"action_id": first["action_id"], "text": "Edited",
                                                    "category": "global-operational"}, revision=first["revision"]))["action"]
    assert edited["text"] == "Edited" and edited["businesses"] == [] and edited["revision"] == first["revision"] + 1
    assert edited["recorded_at"] == first["recorded_at"] and edited["action_id"] == first["action_id"]
    assert count(s, "action_businesses") == 0
    agent = ok(call(s, s.biz, "actions_record", {"text": "Agent result"}))["action"]
    assert not agent["editable"]
    assert error(call(s, s.owner, "actions_update", {"action_id": agent["action_id"], "text": "Changed"}, revision=1)) == "forbidden"
    assert call(s, s.biz, "actions_update", {"action_id": agent["action_id"], "text": "Changed"}, revision=1).status == "error"
    with pytest.raises(Exception), s.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE action_logs SET action_text='tampered' WHERE origin='agent'")
    with pytest.raises(Exception), s.fixture.db.transaction() as uow:
        uow.connection.execute("DELETE FROM action_logs")


def test_san_juan_occurrence_backdating_future_and_clear(s):
    today = local_time.today(lambda: datetime.now(timezone.utc))
    yesterday = today.fromordinal(today.toordinal() - 1).isoformat()
    tomorrow = today.fromordinal(today.toordinal() + 1).isoformat()
    day = ok(call(s, s.owner, "actions_record", {"text": "Called supplier", "real_time": yesterday}))["action"]
    assert day["occurred"] == {"date": yesterday, "hour": None, "precision": "day", "time_zone": "America/Puerto_Rico"}
    hour = ok(call(s, s.biz, "actions_record", {"text": "Fixed checkout", "real_time": yesterday + "T13"}))["action"]
    assert hour["occurred"]["hour"] == 13 and hour["occurred"]["precision"] == "hour"
    for bad in (tomorrow, "2026-02-30", "2026-01-01T24", "2026-01-01T5", "2026-01-01T05:30", "2026-01-01Z"):
        assert error(call(s, s.owner, "actions_record", {"text": "Bad", "real_time": bad})) == "validation_error", bad
    assert [a["text"] for a in listed(s, s.owner, since=yesterday, until=yesterday)] == ["Fixed checkout", "Called supplier"]
    cleared = ok(call(s, s.owner, "actions_update", {"action_id": day["action_id"], "real_time": None}, revision=day["revision"]))["action"]
    assert cleared["occurred"]["precision"] == "instant" and cleared["occurred"]["instant"] == day["recorded_at"]
    assert cleared["occurred"]["date"] == local_time.local_date(day["recorded_at"])


def test_local_time_uses_san_juan_not_utc_midnight():
    clock = lambda: datetime(2026, 3, 2, 3, 30, tzinfo=timezone.utc)  # 2026-03-01 23:30 in San Juan
    assert local_time.today(clock).isoformat() == "2026-03-01"
    assert local_time.parse_real_time("2026-03-01T23", clock)["occurred_at"] == "2026-03-02T03:00:00.000000Z"
    with pytest.raises(DomainError):
        local_time.parse_real_time("2026-03-02", clock)
    assert local_time.recorded_time("2026-03-02T03:30:00Z")["real_date"] == "2026-03-01"
    assert local_time.day_bounds(local_time.today(clock)) == ("2026-03-01T04:00:00.000000Z", "2026-03-02T04:00:00.000000Z")
    assert local_time.now_view(clock)["utc_offset"] == "-0400"


def test_pages_are_stable_and_newest_first(s):
    for number in range(5):
        ok(call(s, s.owner, "actions_record", {"text": f"Result {number}", "category": "global-operational"}))
    seen, cursor = [], None
    while True:
        data = ok(call(s, s.owner, "actions_list", {"limit": 2, **({"cursor": cursor} if cursor else {})}))
        seen += [a["text"] for a in data["actions"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == [f"Result {n}" for n in range(5)] and len(set(seen)) == 5


def test_automatic_source_retries_record_one_action(s):
    worker = replace(s.biz, principal=replace(s.biz.principal, kind="system"))
    for _ in range(2):
        with s.fixture.db.transaction() as uow:
            builder = PlanBuilder(worker, "platform", {}, "Writeback")
            s.activity.append_to_plan(builder, uow, {"tag": "general", "summary": "Email sent: " + TWENTY + " tail"},
                                      source_key="signoff:one:email")
            for change in builder.build().changes:
                if change.resource.resource_type in ("action_logs", "action_businesses"):
                    uow.repository(change.resource.resource_type, business_id=change.resource.business_id).insert(change.values)
    with s.fixture.db.read() as uow:
        rows = uow.connection.execute("SELECT origin, action_text FROM action_logs").fetchall()
    assert len(rows) == 1 and rows[0][0] == "automatic" and len(rows[0][1].split()) <= 20


def test_root_http_route_and_mcp_exposure(s):
    with TestClient(s.web) as client:
        s.login(client)
        created = client.post("/api/root/operations/actions_record", json={"text": "Owner note", "category": "global-systems",
                              "idempotency_key": new_id()}, headers=ORIGIN)
        assert created.status_code == 200, created.text
        action = created.json()["data"]["action"]
        listed_ = client.post("/api/root/operations/actions_list", json={}, headers=ORIGIN)
        assert [a["action_id"] for a in listed_.json()["data"]["actions"]] == [action["action_id"]]
        updated = client.post("/api/root/operations/actions_update", json={"action_id": action["action_id"], "text": "Owner edit",
                              "idempotency_key": new_id(), "expected_revision": action["revision"]}, headers=ORIGIN)
        assert updated.status_code == 200, updated.text
        headers = s.initialize(client, s.agent)
        tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
        names = {tool["name"] for tool in tools["result"]["tools"]}
        assert {"actions_record", "actions_list", "actions_get", "actions_config_get"} <= names and "actions_update" not in names


def legacy_database(tmp_path, monkeypatch, rows):
    from foundation_support import BUSINESS, HUMAN, OTHER, database_at
    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database, "migrations", staticmethod(lambda: migrations[:100]))
        database = database_at(tmp_path)
        with database.transaction() as uow:
            for business, identity, summary, occurred in rows:
                uow.connection.execute("INSERT INTO action_logs(business_id,id,tag,summary,detail,refs_json,actor_id,occurred_at,created_at) "
                                       "VALUES (?,?,?,?,?,?,?,?,?)", (business, identity, "general", summary, "why",
                                       '{"url":"https://x.test"}', HUMAN, occurred, "2026-03-02T03:30:00Z"))
            if any(row[:2] == (BUSINESS, "same") for row in rows):
                uow.connection.execute("INSERT INTO action_log_labels VALUES (?,?,?,?)", (BUSINESS, "same", "launch", "2026-03-02T03:30:00Z"))
    return database, BUSINESS, OTHER


def test_migration_preserves_legacy_history_as_business_operational(tmp_path, monkeypatch):
    from foundation_support import BUSINESS, OTHER
    database, _, _ = legacy_database(tmp_path, monkeypatch, [(BUSINESS, "same", "First", "2026-03-02T03:30:00Z"),
        (OTHER, "same", "Second", "2026-03-02T12:00:00+00:00"), (BUSINESS, "dated", "Day only", "2026-09-11")])
    database.initialize()
    with database.read() as uow:
        rows = {r["action_key"]: dict(r) for r in uow.connection.execute("SELECT * FROM action_logs")}
        links = sorted(tuple(r) for r in uow.connection.execute("SELECT action_key, business_id FROM action_businesses"))
        assert not uow.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert uow.connection.execute("SELECT COUNT(*) FROM action_log_labels").fetchone()[0] == 1
        triggers = {r[0] for r in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'inbox_capture_action_log%'")}
    first, second = rows[f"business/{BUSINESS}/same"], rows[f"business/{OTHER}/same"]
    assert (first["occurred_date"], first["occurred_hour"], first["summary"], first["action_text"]) == ("2026-03-01", 23, "First", "First")
    assert first["origin"] == "legacy" and first["category"] == "business-operational" and first["performed_by"] is None
    assert first["refs_json"] == '{"url":"https://x.test"}' and first["detail"] == "why" and first["recorded_by"] == first["actor_id"]
    assert second["occurred_date"] == "2026-03-02"
    dated = rows[f"business/{BUSINESS}/dated"]
    assert (dated["occurred_date"], dated["occurred_hour"], dated["time_precision"]) == ("2026-09-11", None, "day")
    assert links == sorted((key, row["business_id"]) for key, row in rows.items())
    assert len(triggers) == 6
    with pytest.raises(Exception), database.transaction() as uow:
        for _ in range(2):
            uow.connection.execute("INSERT INTO action_logs(business_id,id,tag,summary,actor_id,occurred_at,created_at,action_key,category,"
                                   "action_text,recorded_by,origin,updated_at) SELECT NULL,'g','general','x',actor_id,occurred_at,created_at,"
                                   "'global/g','global-operational','x',actor_id,'manual',created_at FROM action_logs LIMIT 1")


def test_failed_migration_leaves_previous_schema(tmp_path, monkeypatch):
    from foundation_support import BUSINESS
    database, _, _ = legacy_database(tmp_path, monkeypatch, [(BUSINESS, "blank", "   ", "2026-03-02T03:30:00Z")])
    with pytest.raises(Exception):
        database.initialize()
    with database.read() as uow:
        assert uow.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 100
        columns = {r[1] for r in uow.connection.execute("PRAGMA table_info(action_logs)")}
        assert "action_key" not in columns
        assert uow.connection.execute("SELECT summary FROM action_logs").fetchall()[0][0] == "   "


def test_exports_include_global_actions_and_memberships(s):
    import json
    from bazarlyk.storage.exports import build_exports
    shared = ok(call(s, s.ordu, "actions_record", {"text": "Aligned pricing", "category": "business-operational",
                                                   "business_ids": [s.home, s.second]}))["action"]
    local = ok(call(s, s.biz, "actions_record", {"text": "Fixed checkout"}))["action"]
    files = build_exports(s.fixture.db.path, s.fixture.root).files
    records = {name: json.loads(body)["records"] for name, body in files.items() if name.endswith(("/action_logs.json", "/action_businesses.json"))}
    assert [row["action_key"] for row in records["exports/action_logs.json"]] == [shared["action_id"]]
    assert {(row["action_key"], row["business_id"]) for row in records["exports/action_businesses.json"]} == {
        (shared["action_id"], s.home), (shared["action_id"], s.second), (local["action_id"], s.home)}
    business = [rows for name, rows in records.items() if name.endswith("/action_logs.json") and name != "exports/action_logs.json"]
    assert [row["action_key"] for rows in business for row in rows] == [local["action_id"]]


def test_limited_global_reader_sees_shared_text_but_not_other_business_evidence(s):
    shared = ok(call(s, s.owner, "actions_record", {"text": "Signed joint supplier deal", "category": "business-operational",
        "business_ids": [s.home, s.third], "evidence": {"document": "business-third-private-document"}}))["action"]
    with s.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE action_logs SET detail='private evidence for third' WHERE action_key=?", (shared["action_id"],))
    owner_view = ok(call(s, s.owner, "actions_get", {"action_id": shared["action_id"]}))["action"]
    assert owner_view["refs"] == {"document": "business-third-private-document"} and owner_view["detail"]
    # The Global Marketing agent may access home but not third: text and home only.
    for action in (ok(call(s, s.ordu, "actions_get", {"action_id": shared["action_id"]}))["action"],
                   next(a for a in listed(s, s.ordu) if a["action_id"] == shared["action_id"])):
        assert action["text"] == "Signed joint supplier deal" and [b["id"] for b in action["businesses"]] == [s.home]
        assert action["refs"] == {} and action["detail"] == "" and action["legacy"] is None
        assert s.third not in str(action)
    # A reader authorized for every linked business sees the evidence; revocation hides it again.
    full = replace(s.ordu, allowed_business_ids=frozenset({s.home, s.third}))
    with s.fixture.db.read() as uow:
        activity, scope = s.activity, None
        from bazarlyk.domains.activity.operations import reader_scope
        row = activity.find(uow, shared["action_id"])
        assert activity.view(full, uow, row, reader_scope(full, uow))["refs"] == {"document": "business-third-private-document"}
        revoked = replace(full, allowed_business_ids=frozenset({s.home}))
        assert activity.view(revoked, uow, row, reader_scope(revoked, uow))["refs"] == {}
    # The legacy business Activity reader follows the same rule for a business agent.
    entry = next(e for e in ok(call(s, s.biz, "logs_list", {}))["entries"] if e["action_id"] == shared["action_id"])
    assert entry["refs_json"] == {} and entry["detail"] == "" and s.third not in str(entry)
    assert ok(call(s, s.biz, "logs_get", {"log_id": shared["action_id"]}))["entry"]["refs_json"] == {}


def test_list_pages_filters_in_sql_beyond_twenty_thousand_rows(s):
    from foundation_support import insert_action
    with s.fixture.db.transaction() as uow:
        for number in range(20_001):
            insert_action(uow, s.third, f"old-{number:05d}", summary="Old", actor_id=s.agent.id,
                          occurred_at=f"2025-01-{1 + number % 28:02d}T12:00:00Z")
        for number in range(3):
            insert_action(uow, s.home, f"slice-{number}", summary=f"Slice {number}", actor_id=s.agent.id,
                          occurred_at=f"2026-03-01T1{number}:00:00Z")
    assert listed(s, s.owner, business=s.second) == []
    assert listed(s, s.owner, since="2027-01-01") == []
    seen, cursor = [], None
    while True:
        data = ok(call(s, s.owner, "actions_list", {"since": "2026-03-01", "until": "2026-03-01", "limit": 2,
                                                    **({"cursor": cursor} if cursor else {})}))
        seen += [a["text"] for a in data["actions"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert seen == ["Slice 2", "Slice 1", "Slice 0"]
    first = ok(call(s, s.owner, "actions_list", {"business": s.home, "limit": 2}))
    assert [a["text"] for a in first["actions"]] == ["Slice 2", "Slice 1"]
    rest = ok(call(s, s.owner, "actions_list", {"business": s.home, "limit": 2, "cursor": first["next_cursor"]}))
    assert [a["text"] for a in rest["actions"]] == ["Slice 0"] and rest["next_cursor"] is None
    # A cursor stays bound to its filters.
    assert error(call(s, s.owner, "actions_list", {"business": s.second, "limit": 2, "cursor": first["next_cursor"]})) == "validation_error"
    # The business agent's page is its linked slice only.
    assert [a["text"] for a in listed(s, s.biz, limit=5)] == ["Slice 2", "Slice 1", "Slice 0"]
    # The legacy Activity reader of the business holding 20,001 actions pages and opens shared actions too.
    third = replace(s.fixture.ctx, business_id=s.third)
    assert ok(call(s, third, "logs_list", {"since": "2027-01-01"}))["entries"] == []
    first = ok(call(s, third, "logs_list", {"limit": 2, "search": "old"}))
    rest = ok(call(s, third, "logs_list", {"limit": 2, "search": "old", "cursor": first["next_cursor"]}))
    assert len(first["entries"]) == len(rest["entries"]) == 2 and not {e["id"] for e in first["entries"]} & {e["id"] for e in rest["entries"]}
    shared = ok(call(s, s.owner, "actions_record", {"text": "Shared with third", "category": "business-operational",
                                                    "business_ids": [s.third]}))["action"]
    assert ok(call(s, third, "logs_get", {"log_id": shared["action_id"]}))["entry"]["action_text"] == "Shared with third"


def test_story_items_resolve_canonical_and_legacy_action_ids_by_membership(s):
    from foundation_support import insert_action
    home, second, third = s.fixture.ctx, replace(s.fixture.ctx, business_id=s.second), replace(s.fixture.ctx, business_id=s.third)
    with s.fixture.db.transaction() as uow:
        # The same legacy ID in two businesses never collides.
        insert_action(uow, s.home, "same-old", summary="Home legacy result", actor_id=s.agent.id, occurred_at="2026-01-02T12:00:00Z")
        insert_action(uow, s.second, "same-old", summary="Second legacy result", actor_id=s.agent.id, occurred_at="2026-01-02T12:00:00Z")
    shared = ok(call(s, s.owner, "actions_record", {"text": "Shared launch checklist", "category": "business-operational",
                                                    "business_ids": [s.home, s.second]}))["action"]
    for ctx, legacy_label in ((home, "Home legacy result"), (second, "Second legacy result")):
        found = ok(call(s, ctx, "stories_related_search", {"resource_type": "action_logs"}))["items"]
        assert {item["id"] for item in found} == {"same-old", shared["action_id"]}
        assert next(item for item in found if item["id"] == "same-old")["label"] == legacy_label
        story = ok(call(s, ctx, "stories_create", {"title": "Launch", "purpose": "Ship it"}))["story"]
        for identity in ("same-old", shared["action_id"]):
            ok(call(s, ctx, "stories_link_add", {"story_id": story["id"], "resource_type": "action_logs", "resource_id": identity}))
        links = ok(call(s, ctx, "stories_get", {"story_id": story["id"]}))["story"]["links"]
        assert {link["resource_id"]: link["snapshot_json"]["label"] for link in links} == {
            "same-old": legacy_label, shared["action_id"]: "Shared launch checklist"}
        item = ok(call(s, ctx, "context_items_get", {"item": {"type": "action", "id": shared["action_id"]}}))
        assert item["item"]["resource_type"] == "action_logs"
    # An unlinked business can neither find nor link the shared action.
    assert ok(call(s, third, "stories_related_search", {"resource_type": "action_logs"}))["items"] == []
    story = ok(call(s, third, "stories_create", {"title": "Other", "purpose": "Unrelated"}))["story"]
    assert error(call(s, third, "stories_link_add", {"story_id": story["id"], "resource_type": "action_logs",
                                                     "resource_id": shared["action_id"]})) == "not_found"
    assert error(call(s, third, "context_items_get", {"item": {"type": "action", "id": shared["action_id"]}})) == "not_found"
    # Linking copied nothing: one Global action, still counted once.
    with s.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM action_logs WHERE action_text='Shared launch checklist'").fetchone()[0] == 1


def assignment(s, *, state="completed", scope=None):
    from bazarlyk.agents.operational_delegation import OperationalDelegation
    with s.fixture.db.transaction() as uow:
        created = OperationalDelegation().create(uow, parent_principal_id=s.marketer.id, business_id=None,
            scope=scope or {"kind": "global", "allowed_business_ids": [s.home]}, objective="Draft the launch FAQ",
            plan={"steps": ["draft"]}, allowed_operations=["actions_record"])
        if state == "completed":
            uow.connection.execute("UPDATE operational_assignments SET state='completed', result_json=? WHERE id=?",
                                   ('{"summary": "FAQ drafted"}', created["id"]))
    return created["id"]


def test_parent_records_verified_delegated_result_once_with_unknown_performer(s):
    done = assignment(s)
    args = {"text": "Published launch FAQ", "category": "business-operational", "business_ids": [s.home], "delegation_assignment_id": done}
    action = ok(call(s, s.ordu, "actions_record", args))["action"]
    assert action["performed_by"] is None and action["recorded_by"]["id"] == s.marketer.id and action["attribution"] == "recorded"
    assert action["refs"]["delegation_assignment"] == done
    assert error(call(s, s.ordu, "actions_record", args)) == "conflict"  # Never a second copy.
    # Unfinished, foreign, out-of-scope and unknown assignments are rejected.
    running = assignment(s, state="queued")
    assert error(call(s, s.ordu, "actions_record", {**args, "delegation_assignment_id": running})) == "validation_error"
    assert error(call(s, s.ordu, "actions_record", {**args, "delegation_assignment_id": new_id()})) == "not_found"
    other = assignment(s)
    assert error(call(s, s.ordu, "actions_record", {**args, "business_ids": [s.second], "delegation_assignment_id": other})) == "forbidden"
    assert error(call(s, s.owner, "actions_record", {**args, "delegation_assignment_id": other})) == "not_found"
    # A result the worker already recorded is not recorded again by the parent.
    worked = assignment(s, state="queued")
    worker = replace(s.ordu, delegation_binding_id=new_id(), delegation_assignment_id=worked, delegation_generation=1)
    with s.fixture.db.transaction() as uow:
        own = {"text": "Drafted FAQ answers", "category": "business-operational", "business_ids": [s.home]}
        s.activity.apply_action(worker, s.activity.prepare_record(worker, own, InvocationOptions(idempotency_key=new_id()), uow), uow)
        uow.connection.execute("UPDATE operational_assignments SET state='completed', result_json=? WHERE id=?",
                               ('{"summary": "FAQ drafted"}', worked))
    assert error(call(s, s.ordu, "actions_record", {**args, "delegation_assignment_id": worked})) == "conflict"
    # Apply rechecks: an assignment reopened after prepare cannot be recorded.
    with s.fixture.db.read() as uow:
        plan = s.activity.prepare_record(s.ordu, {**args, "delegation_assignment_id": other}, InvocationOptions(idempotency_key=new_id()), uow)
    with s.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE operational_assignments SET state='queued' WHERE id=?", (other,))
    with s.fixture.db.read() as uow, pytest.raises(DomainError) as raised:
        s.activity.validate_record(s.ordu, plan, uow)
    assert raised.value.code == "validation_error"


def test_bound_worker_records_with_parent_as_recorder_and_assignment_provenance(s):
    running = assignment(s, state="queued")
    worker = replace(s.ordu, delegation_binding_id=new_id(), delegation_assignment_id=running, delegation_generation=1)
    args = {"text": "Drafted FAQ answers", "category": "business-operational", "business_ids": [s.home]}
    with s.fixture.db.read() as uow:
        plan = s.activity.prepare_record(worker, args, InvocationOptions(idempotency_key=new_id()), uow)
        with pytest.raises(DomainError) as raised:
            s.activity.prepare_record(worker, {**args, "delegation_assignment_id": assignment(s)}, InvocationOptions(), uow)
        assert raised.value.code == "forbidden"
        with pytest.raises(DomainError) as raised:
            s.activity.prepare_record(worker, {**args, "business_ids": [s.second]}, InvocationOptions(), uow)
        assert raised.value.code == "forbidden"
    values = next(change.values for change in plan.changes if change.resource.resource_type == "action_logs")
    assert values["performed_by"] is None and values["recorded_by"] == s.marketer.id
    assert values["refs_json"] == {"delegation_assignment": running, "delegation_generation": "1"}
    # Own work stays unchanged.
    own = ok(call(s, s.ordu, "actions_record", args))["action"]
    assert own["performed_by"]["id"] == own["recorded_by"]["id"] == s.marketer.id and "delegation_assignment" not in own["refs"]


def test_business_log_route_keeps_backfilled_precision(s):
    shared = ok(call(s, s.owner, "actions_record", {"text": "Filed quarterly taxes", "category": "business-operational",
                                                    "business_ids": [s.home, s.second], "real_time": "2026-01-05"}))["action"]
    with s.fixture.db.read() as uow:
        slug = uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (s.home,)).fetchone()[0]
    with TestClient(s.web) as client:
        s.login(client)
        rows = client.get(f"/api/biz/{slug}/log?n=10", headers=ORIGIN)
        assert rows.status_code == 200, rows.text
        entry = next(row for row in rows.json() if row["action_id"] == shared["action_id"])
        assert (entry["occurred_date"], entry["time_precision"], entry["occurred_hour"]) == ("2026-01-05", "day", None)


def pre_101_append_to_plan(self, builder, uow, arguments, *, operation_name=None):
    """ActivityOperations.append_to_plan exactly as released before migration 101 (biz-core 268c7a1)."""
    from bazarlyk.domains.activity.operations import KINDS, TAGS, department_id
    from bazarlyk.domains.common import ref
    import re
    summary, tag, refs = arguments["summary"], department_id(arguments["tag"]), arguments.get("refs", {})
    if not summary.strip() or "\n" in summary or "\r" in summary or len(summary) > 200 or tag not in TAGS:
        raise DomainError("validation_error", "Activity needs a valid tag and non-empty one-line summary")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) or not isinstance(value, str) or not value for key, value in refs.items()):
        raise DomainError("validation_error", "Activity references must be named non-empty strings")
    if tag == "marketing":
        kind = refs.get("kind")
        if kind not in KINDS: raise DomainError("validation_error", "Marketing activity requires a known kind")
        if kind != "decision" and (not refs.get("channel") or not refs.get("target")):
            raise DomainError("validation_error", "Outward marketing activity requires channel and target")
        if refs.get("channel") and not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z][a-z0-9]*)*", refs["channel"]):
            raise DomainError("validation_error", "Marketing channel must be lowercase kebab-case")
    if refs.get("task"):
        task = uow.repository("tasks", business_id=builder.ctx.business_id).get(refs["task"], required=True)
        builder.expect(ref("tasks", task["id"], builder.ctx.business_id), task)
    identity = new_id()
    values = {"id": identity, "tag": tag, "summary": summary, "detail": arguments.get("detail", ""), "refs_json": refs,
              "actor_id": builder.ctx.principal_id, "occurred_at": arguments.get("occurred_at", builder.at), "created_at": builder.at}
    builder.record("action_logs", identity, None, values, operation_name=operation_name, label=summary)
    for label in arguments.get("labels", []):
        builder.record("action_log_labels", {"log_id": identity, "label": label}, None, {"log_id": identity, "label": label, "created_at": builder.at},
                       operation_name="logs_labels_update")
    cost = arguments.get("cost")
    if cost:
        if cost["amount"] >= 0: raise DomainError("validation_error", "Activity cost must be a negative minor-unit ledger amount")
        self.ledger.append_to_plan(builder, uow, {**cost, "summary": summary, "detail": arguments.get("detail", ""), "refs": {**refs, "log": identity}},
            source="activity", external_id="activity:" + identity, operation_name="ledger_add")
    return values


LONG = " ".join(f"word{n}" for n in range(30))


@pytest.fixture
def pending_pre_101(tmp_path, monkeypatch):
    """Pending logs_add and logs_labels_update Signoffs prepared by pre-101 code on a pre-101 database, then migrated."""
    from bazarlyk.domains.activity.operations import ActivityOperations
    from bazarlyk.storage import database as storage_database
    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database, "migrations", staticmethod(lambda: migrations[:100]))
        old.setattr(storage_database, "TABLES", {name: spec for name, spec in storage_database.TABLES.items() if name != "action_businesses"})
        old.setattr(ActivityOperations, "append_to_plan", pre_101_append_to_plan)
        s = ServiceFixture(tmp_path, full=True)
        fx = s.fixture
        with fx.db.read() as uow:
            assert uow.connection.execute("PRAGMA user_version").fetchone()[0] == 100
        with fx.db.transaction() as uow:
            uow.connection.execute("INSERT INTO action_logs(business_id,id,tag,summary,detail,refs_json,actor_id,occurred_at,created_at) "
                                   "VALUES (?,?,?,?,?,?,?,?,?)", (fx.business_id, "old-row", "general", "Old row", "", "{}",
                                   fx.owner.id, "2026-02-01T12:00:00Z", "2026-02-01T12:00:00Z"))
        ok(s.runtime.application.invoke(fx.ctx, "permissions_set", {"agent_id": s.agent.id,
            "overrides": {"logs_add": "signoff", "logs_labels_update": "signoff"}},
            InvocationOptions(idempotency_key=new_id(), expected_revision=1)))
        agent = s.runtime.resolver.invocation_context(s.agent)
        added = s.runtime.application.invoke(agent, "logs_add", {"tag": "general", "summary": LONG, "detail": "Frozen detail",
            "refs": {"url": "https://example.test/launch"}, "labels": ["launch"], "occurred_at": "2026-03-02T03:30:00Z",
            "cost": {"amount": -1250, "category": "tools"}}, InvocationOptions(idempotency_key=new_id()))
        labelled = s.runtime.application.invoke(agent, "logs_labels_update", {"log_id": "old-row", "add": ["kept"]},
                                                InvocationOptions(idempotency_key=new_id()))
        assert added.status == labelled.status == "signoff_required", (added.to_dict(), labelled.to_dict())
        with fx.db.read() as uow:
            frozen = {row[0]: row[1] for row in uow.connection.execute("SELECT id, payload_hash FROM signoffs")}
    fx.db.initialize()  # Migration 101, with both proposals still pending.
    with fx.db.read() as uow:
        assert uow.connection.execute("PRAGMA user_version").fetchone()[0] == len(migrations)
    s.app, s.added, s.labelled, s.frozen = s.runtime.application, added.signoff["id"], labelled.signoff["id"], frozen
    return s


def approve(s, identity):
    owner = replace(s.fixture.ctx, source="dashboard")
    detail = s.runtime.signoffs.get(owner, identity).data["signoff"]
    decided = s.runtime.signoffs.review(owner, identity, decision="approve", proposal_revision=detail["proposal_revision"],
        payload_hash=detail["payload_hash"], expected_status_revision=detail["status_revision"], idempotency_key=new_id())
    assert decided.status == "accepted", decided.to_dict()
    s.runtime.signoffs.run_one(identity)
    return s.runtime.signoffs.get(owner, identity).data["signoff"]


def test_pre_101_frozen_action_proposals_apply_after_migration(pending_pre_101):
    s, fx = pending_pre_101, pending_pre_101.fixture
    detail = approve(s, s.added)
    assert detail["state"] == "applied", detail
    assert detail["payload_hash"] == s.frozen[s.added]  # The frozen proposal is applied as prepared.
    with fx.db.read() as uow:
        rows = [dict(row) for row in uow.connection.execute("SELECT * FROM action_logs WHERE summary=?", (LONG,))]
        assert len(rows) == 1
        row = rows[0]
        links = uow.connection.execute("SELECT business_id FROM action_businesses WHERE action_key=?", (row["action_key"],)).fetchall()
        labels = [r[0] for r in uow.connection.execute("SELECT label FROM action_log_labels WHERE log_id=?", (row["id"],))]
        ledger = [json.loads(r[0]) for r in uow.connection.execute("SELECT cells_json FROM data_rows WHERE cells_json LIKE ?",
                                                                    ('%activity:' + row["id"] + '%',))]
    # Grandfathered verbatim: 30-word summary, detail, refs, labels, cost, actor and times.
    assert row["action_key"] == f"business/{fx.business_id}/{row['id']}" and row["action_text"] == LONG
    assert (row["origin"], row["category"], row["performed_by"], row["recorded_by"], row["actor_id"]) == (
        "legacy", "business-operational", None, s.agent.id, s.agent.id)
    assert (row["detail"], json.loads(row["refs_json"]), row["occurred_at"]) == ("Frozen detail", {"url": "https://example.test/launch"},
                                                                                  "2026-03-02T03:30:00Z")
    assert (row["occurred_date"], row["occurred_hour"], row["time_precision"], row["updated_at"]) == ("2026-03-01", 23, "instant", row["created_at"])
    assert [tuple(link) for link in links] == [(fx.business_id,)] and labels == ["launch"]
    assert len(ledger) == 1 and ledger[0]["amount"] == -1250
    # Visible through memberships to the business and its agent.
    agent = s.runtime.resolver.invocation_context(s.agent)
    assert [a["action_id"] for a in ok(call(s, agent, "actions_list", {"since": "2026-03-01", "until": "2026-03-01"}))["actions"]] == [row["action_key"]]
    # An old label-only proposal still applies; its existence-only pin is unchanged.
    assert approve(s, s.labelled)["state"] == "applied"
    with fx.db.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM action_log_labels WHERE log_id='old-row' AND label='kept'").fetchone()[0] == 1
    # Running the applied Signoff again changes nothing.
    s.runtime.signoffs.run_one(s.added)
    with fx.db.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM action_logs WHERE summary=?", (LONG,)).fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM action_businesses").fetchone()[0] == 2


def test_pre_101_frozen_action_rolls_back_when_a_composed_change_fails(pending_pre_101, monkeypatch):
    s, fx = pending_pre_101, pending_pre_101.fixture
    tables = s.runtime.registry.get("logs_add").apply.__self__.ledger.tables
    def fail(*args, **kwargs):
        raise DomainError("storage_error", "Table history is unavailable")
    monkeypatch.setattr(tables.history, "capture", fail)
    detail = approve(s, s.added)
    assert (detail["state"], detail["error"]["retryable"]) == ("failed", True), detail
    with fx.db.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM action_logs WHERE summary=?", (LONG,)).fetchone()[0] == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM action_businesses").fetchone()[0] == 1  # old-row only
        assert uow.connection.execute("SELECT COUNT(*) FROM action_log_labels WHERE label='launch'").fetchone()[0] == 0
    # With the failure gone, the owner's retry applies the same frozen proposal exactly once.
    monkeypatch.undo()
    owner = replace(fx.ctx, source="dashboard")
    retried = s.runtime.signoffs.retry(owner, s.added, proposal_revision=detail["proposal_revision"], payload_hash=detail["payload_hash"],
                                       expected_status_revision=detail["status_revision"], idempotency_key=new_id())
    assert retried.status == "accepted", retried.to_dict()
    s.runtime.signoffs.run_one(s.added)
    assert s.runtime.signoffs.get(owner, s.added).data["signoff"]["state"] == "applied"
    with fx.db.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM action_logs WHERE summary=?", (LONG,)).fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM action_businesses").fetchone()[0] == 2


def test_pre_101_adapter_accepts_only_the_exact_old_shape():
    from qorqut_operations.contracts import ResourceRef
    from bazarlyk.domains.activity import compatibility
    old = dict.fromkeys(compatibility.PRE_101_FIELDS, "x")
    business = ResourceRef("action_logs", "x", new_id())
    assert compatibility.is_pre_101_insert(business, old)
    assert not compatibility.is_pre_101_insert(business, {**old, "origin": "agent"})
    assert not compatibility.is_pre_101_insert(business, {key: old[key] for key in list(old)[1:]})
    assert not compatibility.is_pre_101_insert(ResourceRef("action_logs", "x", None), old)
    assert compatibility.occurrence("2026-09-11") == {"occurred_date": "2026-09-11", "occurred_hour": None, "time_precision": "day"}
    assert compatibility.occurrence("2026-02-30")["time_precision"] == "instant"
