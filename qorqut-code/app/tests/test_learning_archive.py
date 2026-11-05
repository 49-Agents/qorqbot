"""The retained factual agenda and the read-only historical learning inventory.

The subject is the real registry and database of a synthetic host: the factual agenda never exposes engine/issue
projection, the historical inventory is bounded, read-only and complete for every retained original ID -- including
filed, implemented and collapsed records -- and both keep their scope authorization. No model or provider is used.
"""
import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import InvocationOptions, new_id, plain, utc_now
from host_foundation_support import build_instance, owner_global_context

BUSINESS = "first"
SECOND = "second"


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=(BUSINESS, SECOND))
    instance.runtime.freeze_registry()
    now = utc_now()

    def scope(business_id):
        return ids.get(business_id, business_id) if business_id else None

    def insert(business_id, identity, value, **columns):
        with instance.database.transaction() as uow:
            uow.repository("learning_candidates", business_id=scope(business_id)).insert(
                {"id": identity, "candidate_json": value, "created_at": now, "updated_at": now, **columns})

    def decide(business_id, identity, state, changes=None, feedback=None):
        with instance.database.transaction() as uow:
            uow.repository("learning_decisions", business_id=scope(business_id)).insert(
                {"candidate_id": identity, "state": state, "feedback": feedback, "changes_json": changes or {},
                 "decided_by": instance.owner, "created_at": now, "updated_at": now})

    history = {}

    def delta(identity, text, *, business_id=BUSINESS, decision=None, changes=None, work_state=None, filing=None,
              reconciled_into=None, created="2026-01-01T00:00:00Z"):
        value = {"text": text, "scope": "biz", "bucket": "delta", "tag": "rule", "created": created,
                 **({"reconciled_into": reconciled_into} if reconciled_into else {})}
        insert(business_id, identity, value, work_state=work_state, filing_json=filing or {},
               created_at=created, updated_at=created)
        if decision:
            decide(business_id, identity, decision, changes)
        return identity

    def fact(identity, text, *, business_id=BUSINESS, decision=None, changes=None, filing=None, resolved=None,
             created="2026-01-01T00:00:00Z"):
        value = {"text": text, "scope": "biz", "bucket": "context", "tag": "fact", "created": created,
                 **({"reconciled_into": resolved} if resolved else {})}
        insert(business_id, identity, value, filing_json=filing or {}, created_at=created, updated_at=created)
        if decision:
            decide(business_id, identity, decision, changes)
        return identity

    delta("delta-pending", "Legacy rule " * 80)
    delta("delta-implemented", "Implemented legacy rule", decision="accepted", work_state="implemented")
    delta("delta-filed", "Filed legacy rule", filing={"upload_id": "old"}, work_state="in-progress")
    delta("delta-collapsed", "Collapsed legacy rule", decision="accepted", reconciled_into="delta-pending")
    delta("delta-effective", "Original legacy text", decision="accepted",
          changes={"text": "Owner-edited legacy text", "scope": "agent", "tag": "fact", "reconciled_into": "delta-pending"})
    fact("fact-open", "An open factual item")
    fact("fact-accepted", "An accepted factual item", decision="accepted")
    fact("fact-filed", "An already filed factual item", decision="accepted", filing={"upload_id": "filed"})
    fact("fact-collapsed", "A collapsed factual item", decision="accepted", resolved="fact-accepted")
    delta("second-delta", "Another business's legacy rule", business_id=ids[SECOND])
    delta("global-delta", "A Global legacy rule", business_id=None)
    yield SimpleNamespace(instance=instance, ids=ids, insert=insert, decide=decide)
    instance.runtime.release_instance()


def call(host, name, arguments, ctx=None):
    return host.instance.runtime.application.invoke(ctx or host.instance.human(host.ids[BUSINESS]), name, arguments,
                                                   InvocationOptions(idempotency_key=new_id()))


def data(host, name, arguments, ctx=None):
    result = call(host, name, arguments, ctx)
    assert result.status == "ok", (name, result.to_dict())
    return plain(result.data)


# ---------------------------------------------------------------------------------------------- historical inventory

def test_the_inventory_lists_every_original_delta_including_filed_implemented_and_collapsed(host):
    inventory = data(host, "learnings_legacy_pending", {})
    assert inventory["read_only"] is True and inventory["schema_version"] == 1
    assert inventory["count"] == 5 and len(inventory["items"]) == 5          # this business's deltas only
    by_id = {item["learning_id"]: item for item in inventory["items"]}
    assert set(by_id) == {"delta-pending", "delta-implemented", "delta-filed", "delta-collapsed", "delta-effective"}
    assert by_id["delta-implemented"]["work_state"] == "implemented" and by_id["delta-implemented"]["state"] == "implemented"
    assert by_id["delta-filed"]["filed"] is True
    assert by_id["delta-collapsed"]["reconciled_into"] == "delta-pending"
    # The effective decision is what the summary reports, exactly like an ordinary candidate read.
    assert by_id["delta-effective"]["text"] == "Owner-edited legacy text"
    assert (by_id["delta-effective"]["scope"], by_id["delta-effective"]["tag"]) == ("agent", "fact")
    assert by_id["delta-effective"]["decision"] == "accepted" and by_id["delta-effective"]["reconciled_into"] == "delta-pending"
    assert by_id["delta-pending"]["text_truncated"] is True and len(by_id["delta-pending"]["text"]) == 500
    assert by_id["delta-effective"]["text_truncated"] is False
    assert "historical procedural records" in inventory["statement"]


def test_the_inventory_pages_without_loss_or_reordering_and_binds_its_cursor_to_scope(host):
    first = data(host, "learnings_legacy_pending", {"limit": 3})
    assert first["count"] == 5 and len(first["items"]) == 3 and first["next_cursor"]
    second = data(host, "learnings_legacy_pending", {"limit": 3, "cursor": first["next_cursor"]})
    assert second["count"] == 5 and second["next_cursor"] is None
    identities = [item["learning_id"] for item in first["items"] + second["items"]]
    assert len(identities) == len(set(identities)) == 5
    # A cursor from another business or the Global scope is refused rather than reinterpreted.
    assert call(host, "learnings_legacy_pending", {"limit": 3, "cursor": first["next_cursor"]},
                ctx=host.instance.human(host.ids[SECOND])).error["code"] == "validation_error"
    assert call(host, "learnings_legacy_pending", {"limit": 3, "cursor": first["next_cursor"]},
                ctx=owner_global_context(host.instance)).error["code"] == "validation_error"
    assert call(host, "learnings_legacy_pending", {"limit": 0}).error["code"] == "validation_error"
    assert call(host, "learnings_legacy_pending", {"cursor": "garbage"}).error["code"] == "validation_error"


def test_inventory_pages_reach_missing_equal_and_mixed_creation_metadata_exactly_once(host):
    now = utc_now()
    with host.instance.database.transaction() as uow:
        repository = uow.repository("learning_candidates", business_id=host.ids[BUSINESS])
        for identity, value in (
                ("no-created", {"text": "No creation metadata", "scope": "biz", "bucket": "delta", "tag": "rule"}),
                ("null-created", {"text": "Explicit null creation", "scope": "biz", "bucket": "delta", "tag": "rule",
                                  "created": None}),
                ("same-one", {"text": "Equal date one", "scope": "biz", "bucket": "delta", "tag": "rule",
                              "created": "2026-02-01T00:00:00Z"}),
                ("same-two", {"text": "Equal date two", "scope": "biz", "bucket": "delta", "tag": "rule",
                              "created": "2026-02-01T00:00:00Z"}),
                ("mixed-dated", {"text": "Dated record", "scope": "biz", "bucket": "delta", "tag": "rule",
                                 "created": "2026-03-01T00:00:00Z"})):
            repository.insert({"id": identity, "candidate_json": value, "created_at": now, "updated_at": now})
    expected = {"delta-pending", "delta-implemented", "delta-filed", "delta-collapsed", "delta-effective",
                "no-created", "null-created", "same-one", "same-two", "mixed-dated"}
    seen, cursor, pages, count = [], None, 0, None
    while True:
        arguments = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        page = data(host, "learnings_legacy_pending", arguments)
        count = page["count"]
        seen.extend(item["learning_id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        assert pages < 20, "inventory pagination did not terminate"
        if not cursor:
            break
    assert count == len(expected) == 10 and len(seen) == len(set(seen)) == 10 and set(seen) == expected
    by_id = {item["learning_id"]: item for item in data(host, "learnings_legacy_pending", {"limit": 50})["items"]}
    assert by_id["no-created"]["created"] is None and by_id["null-created"]["created"] is None
    assert by_id["same-one"]["created"] == by_id["same-two"]["created"] == "2026-02-01T00:00:00Z"


def test_the_global_inventory_reads_the_global_scope_only(host):
    global_items = data(host, "learnings_legacy_pending", {}, ctx=owner_global_context(host.instance))
    assert [item["learning_id"] for item in global_items["items"]] == ["global-delta"]
    assert global_items["read_only"] is True


# ---------------------------------------------------------------------------------------------- factual agenda

def test_the_factual_agenda_excludes_filed_collapsed_deltas_and_every_issue_projection(host):
    agenda = data(host, "learnings_list", {})
    # Filed and collapsed records leave the agenda; the default selection is factual Context candidates only.
    assert {item["id"] for item in agenda["items"]} == {"fact-open", "fact-accepted"}
    assert "suppressed" not in agenda and all("issue" not in item for item in agenda["items"])
    assert agenda["counts"] == {"open": 1, "accepted": 1, "in-progress": 0, "implemented": 0, "rejected": 0,
                               "rejected-in-work": 0}
    assert {item["id"] for item in data(host, "learnings_list", {"bucket": "context"})["items"]} == {"fact-open", "fact-accepted"}
    assert {item["id"] for item in data(host, "learnings_list", {"state": "accepted"})["items"]} == {"fact-accepted"}
    # A historical delta is available only through the explicit historical selection the specification allows.
    assert {item["id"] for item in data(host, "learnings_list", {"bucket": "delta"})["items"]} == {"delta-pending"}
    assert {item["id"] for item in data(host, "learnings_list", {"bucket": "delta", "state": "implemented"})["items"]} == {
        "delta-implemented"}
    assert {item["id"] for item in data(host, "learnings_list", {"tag": "fact"})["items"]} == {"fact-open", "fact-accepted"}
    assert set(data(host, "learnings_list", {"tag": "fact"})["items"][0]) == {
        "id", "business_id", "revision", "state", "agent_note", "bucket", "tag", "scope", "text",
        "created", "origin_scope", "targets", "outcome", "filing"}
    assert not ({"issue", "issue_id", "package_revision", "suppressed"} & set(data(host, "learnings_list", {"tag": "fact"})["items"][0]))
    assert call(host, "learnings_list", {"issue_view": "all"}).error["code"] == "validation_error"


def test_the_review_agenda_defaults_to_factual_open_and_keeps_explicit_historical_selection(host):
    agenda = data(host, "learnings_review_list", {})
    assert {item["id"] for item in agenda["items"]} == {"fact-open"}
    assert agenda["counts"] == {"open": 1} and "suppressed" not in agenda
    assert all("issue" not in item for item in agenda["items"])
    accepted = data(host, "learnings_review_list", {"states": ["accepted"]})
    assert {item["id"] for item in accepted["items"]} == {"fact-accepted"}
    assert accepted["counts"] == {"accepted": 1}
    # Explicit historical selection keeps every reachable delta, including an effective edited decision.
    historical = data(host, "learnings_review_list", {"states": ["open", "accepted", "implemented"], "bucket": "delta"})
    assert {item["id"] for item in historical["items"]} == {"delta-pending", "delta-effective", "delta-implemented"}
    first = data(host, "learnings_review_list", {"states": ["open", "accepted"], "limit": 1})
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = data(host, "learnings_review_list", {"states": ["open", "accepted"], "limit": 1,
                                                  "cursor": first["next_cursor"]})
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {"fact-open", "fact-accepted"}
    assert call(host, "learnings_review_list", {"limit": 1, "cursor": first["next_cursor"]},
                ctx=host.instance.human(host.ids[SECOND])).error["code"] == "validation_error"


def test_ordinary_detail_reads_keep_the_item_cores_and_reasoning_without_an_issue(host):
    detail = data(host, "learnings_get", {"learning_id": "fact-accepted"})
    assert detail["item"]["state"] == "accepted" and detail["cores"] == {}
    assert "issue" not in detail
    with host.instance.database.transaction() as uow:
        uow.repository("learning_runs", business_id=host.ids[BUSINESS]).insert(
            {"id": "run-1", "result_json": {"cores": {"session-1": {"markdown": "Frozen"}}}, "created_at": utc_now()})
        row = uow.repository("learning_candidates", business_id=host.ids[BUSINESS]).get("fact-accepted")
        uow.repository("learning_candidates", business_id=host.ids[BUSINESS]).update(
            "fact-accepted", {"candidate_json": {**row["candidate_json"], "run": "run-1",
                                                "evidence": {"session": "session-1"}}}, expected_revision=1)
        uow.connection.execute(
            "INSERT INTO learning_history(business_id,id,candidate_id,event,actor_id,before_json,after_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (host.ids[BUSINESS], "history-1", "fact-accepted", "learnings_review", host.instance.owner, "{}",
             json.dumps({"decision": {"note": None, "reasoning_source": "owner_direct"}}), utc_now()))
    detail = data(host, "learnings_get", {"learning_id": "fact-accepted"})
    assert detail["cores"] == {"session-1": {"markdown": "Frozen"}}
    assert [event["id"] for event in data(host, "learnings_review_get", {"learning_id": "fact-accepted"})["history"]] == ["history-1"]