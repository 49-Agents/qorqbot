"""CRM activity merged from the record's discussion and direct links.

Runs against the real composed CRM Domain and Host over a disposable SQLite store: local activity, Task, Signoff, Action
and document readers with current authorization, discovery, ordering, cursors, cutoff and read purity. Timestamps are
synthetic and explicit; no provider, live conversation or native session is read."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, canonical_hash, new_id, plain

from host_foundation_support import (build_instance, create_registered_global, create_worker, register_sources, select_coordinator,
                                     set_agent_overrides)

T = "2026-10-{day:02d}T{hour:02d}:00:00.000000Z"
DOCUMENT_PATH = "context/product.md"


def stamp(day, hour=10):
    return T.format(day=day, hour=hour)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("crm-activity"), businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    worker = create_worker(instance, "worker-a")
    set_agent_overrides(instance, worker, {"tasks_get": "allow", "crm_activity_list": "allow", "tasks_update": "signoff",
                                           "signoffs_get": "allow"}, (first, second))
    person = new_id()
    with instance.database.transaction() as uow:
        uow.repository("principals").insert({"id": person, "kind": "human", "display_name": "Synthetic Member"})
        uow.repository("business_memberships", business_id=first).insert({"principal_id": person, "is_owner": False})
    instance.runtime.freeze_registry()
    state = SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=first, second=second,
                            coordinator=coordinator, worker=worker, person=person)
    seed(state)
    try:
        yield state
    finally:
        instance.runtime.release_instance()
        instance.database.close()


# ----- helpers ----------------------------------------------------------------------------------------------------------


def owner_ctx(world, business):
    """The installation owner with full reach, as the dashboard resolves it (cross-business reads work)."""
    with world.database.read() as uow:
        return world.runtime.policy.context_for(world.instance.owner, business_id=business, review_business_id=business, uow=uow, source="dashboard")


def agent_ctx(world, identity, business):
    return world.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def person_ctx(world, business):
    return InvocationContext(Principal(world.person, "human"), business_id=business, allowed_business_ids=frozenset({world.first}), source="dashboard")


def call(world, ctx, name, arguments, revision=None):
    return world.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def make_task(world, business, title):
    created = call(world, owner_ctx(world, business), "tasks_create", {"scope": "business" if business else "global", "title": title})
    return ok(created)["task"]


def link(world, source, source_business, target, target_business):
    current = ok(call(world, owner_ctx(world, source_business), "tasks_get",
                      {"scope": "business" if source_business else "global", "task_id": source}))["task"]
    ok(call(world, owner_ctx(world, source_business), "tasks_update", {
        "scope": "business" if source_business else "global", "task_id": source,
        "changes": {"related_task": {"task_id": target, "business_id": target_business, "linked": True}}}, current["revision"]))


def comment(world, business, task, identifier, at, text, refs=()):
    with world.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,refs_json,author_id,recorder_id,created_at) "
            "VALUES (?,?,'task',?,'comment',?,?,?,?,?)",
            (business, identifier, task, text, json.dumps(list(refs)), world.instance.owner, world.instance.owner, at))


def recorded(world, business, task, identifier, occurred, recorded_at, summary, key):
    """A stored observation exactly as the real Domain's ``crm_activity_record`` writes one, including its content digest."""
    body = "Synthetic recorded body"
    source = {"kind": "email", "provider": "synthetic", "message_id": identifier, "title": "Synthetic mailbox"}
    with world.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO crm_activity_events(business_id,id,task_id,kind,occurred_at,recorded_at,recorded_by,summary,body,source_json,event_key,content_hash) "
            "VALUES (?,?,?,'email',?,?,?,?,?,?,?,?)",
            (business, identifier, task, occurred, recorded_at, world.instance.owner, summary, body, json.dumps(source), key,
             canonical_hash({"kind": "email", "summary": summary, "body": body, "occurred_at": occurred, "source": source,
                             "corrects_event_id": None})))


def edge(world, business, source, target_type, target, role="concerns"):
    with world.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,created_at,updated_at,created_by) "
            "VALUES (?,?,'task',?,?,?,?,?,?,?)",
            (business, new_id(), source, target_type, target, role, stamp(1), stamp(1), world.instance.owner))


def seed(world):
    first, second = world.first, world.second
    world.record = make_task(world, first, "Synthetic record")["id"]
    world.linked = make_task(world, first, "Linked task")["id"]
    world.incoming = make_task(world, second, "Incoming from another business")["id"]
    world.decoy = make_task(world, first, "Unrelated task")["id"]
    world.referenced = make_task(world, first, "Only referenced in a note")["id"]
    world.global_task = make_task(world, None, "Global task")["id"]
    link(world, world.record, first, world.linked, first)                    # outgoing Task link
    link(world, world.incoming, second, world.record, first)                  # incoming Task link, from another business
    link(world, world.record, first, world.global_task, None)                # a Global Task
    with world.database.read() as uow:
        world.document = uow.connection.execute("SELECT id FROM documents WHERE business_id=? AND path=?", (first, DOCUMENT_PATH)).fetchone()[0]
    action = ok(call(world, owner_ctx(world, first), "actions_record", {
        "text": "Sent the synthetic proposal", "category": "business-operational", "business_ids": [first], "real_time": "2026-09-01T10"}))
    world.action = action["action"]["action_id"]
    edge(world, first, world.record, "task", world.linked)                   # the same Task again, as a concerns edge ...
    edge(world, first, world.record, "action", world.action)
    # ... and a third time as a typed reference in the record's own comment, with a second Task and a document.
    comment(world, first, world.record, "r1", stamp(10), "Own note one", refs=[{"type": "task", "id": world.linked}, {"type": "task", "id": world.referenced},
                                                                              {"type": "material", "id": world.document}])
    comment(world, first, world.record, "r2", stamp(12), "Own note two")
    comment(world, first, world.record, "r3", stamp(14), "Own note three")
    comment(world, first, world.linked, "a1", stamp(11), "Linked note one")
    comment(world, first, world.linked, "a2", stamp(13), "Linked note two")
    comment(world, first, world.linked, "a3", stamp(15), "Linked note three")
    comment(world, second, world.incoming, "b1", stamp(12, 12), "Incoming note one")
    comment(world, second, world.incoming, "b2", stamp(16), "Incoming note two")
    comment(world, first, world.referenced, "e1", stamp(9), "Referenced note")
    comment(world, None, world.global_task, "g1", stamp(8), "Global note")
    comment(world, first, world.decoy, "c1", stamp(17), "Decoy note")
    recorded(world, first, world.record, "x1", stamp(5), stamp(20), "Synthetic email received", "key-1")
    recorded(world, first, world.record, "x2", stamp(18), stamp(18), "Synthetic message sent", "key-2")
    # A Signoff the record concerns: proposed by a worker, then approved by the owner.
    current = ok(call(world, owner_ctx(world, first), "tasks_get", {"scope": "business", "task_id": world.record}))["task"]
    staged = call(world, agent_ctx(world, world.worker, first), "tasks_update", {
        "scope": "business", "task_id": world.record, "changes": {"title": "Synthetic record, renamed"}}, current["revision"])
    assert staged.status == "signoff_required", staged.error
    world.signoff = staged.signoff["id"]
    edge(world, first, world.record, "signoff", world.signoff)
    proposal = staged.signoff
    world.runtime.signoffs.review(owner_ctx(world, first), world.signoff, decision="approve", proposal_revision=proposal["proposal_revision"],
                                  payload_hash=proposal["payload_hash"], expected_status_revision=proposal["status_revision"],
                                  idempotency_key=new_id())


def listing(world, ctx, task=None, **arguments):
    return call(world, ctx, "crm_activity_list", {"scope": "business" if ctx.business_id else "global", "task_id": task or world.record, **arguments})


def everything(world, ctx, task=None, **arguments):
    return ok(listing(world, ctx, task, limit=100, **arguments))


def pages(world, ctx, limit, task=None, **arguments):
    collected, cursor = [], None
    while True:
        page = ok(listing(world, ctx, task, limit=limit, **({"cursor": cursor} if cursor else {}), **arguments))
        assert len(page["items"]) <= limit
        collected.append(page)
        cursor = page["next_cursor"]
        if not cursor:
            return collected


def ids(page):
    return [item["id"] for item in page["items"]]


def counts(world):
    with world.database.read() as uow:
        tables = [row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
        return {name: uow.connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}


# ----- merge, identity and deduplication --------------------------------------------------------------------------------


def test_the_listing_merges_own_and_linked_events_newest_first_with_authentic_identity_time_and_actor(world):
    page = everything(world, owner_ctx(world, world.first))
    assert page["record_ref"] == {"type": "task", "id": world.record, "business_id": world.first} and page["next_cursor"] is None
    items = page["items"]
    keys = [(item["occurred_at"], item["id"]) for item in items]
    assert keys == sorted(keys, reverse=True) and len(set(ids(page))) == len(items)
    by_summary = {item["summary"]: item for item in items}
    own = by_summary["Own note two"]
    assert own["origin"] == "own" and own["occurred_at"] == stamp(12) and own["actor"]["id"] == world.instance.owner
    assert {key: own["source"][key] for key in ("type", "id", "business_id")} == \
        {"type": "task", "id": world.record, "business_id": world.first} and own["source"]["label"]
    linked = by_summary["Linked note two"]
    assert linked["origin"] == "linked" and linked["occurred_at"] == stamp(13) and linked["body"] == "Linked note two"
    assert linked["source"]["type"] == "task" and linked["source"]["id"] == world.linked and linked["source"]["business_id"] == world.first
    assert linked["actor"]["id"] == world.instance.owner
    foreign = by_summary["Incoming note two"]
    assert foreign["source"]["business_id"] == world.second and foreign["source"]["id"] == world.incoming
    assert by_summary["Global note"]["source"]["business_id"] is None
    # Notes arrive in their own time order across sources.
    notes = [item["summary"] for item in items if item["summary"].endswith(("one", "two", "three")) or item["summary"] == "Referenced note"]
    assert notes[:5] == ["Incoming note two", "Linked note three", "Own note three", "Linked note two", "Incoming note one"]
    assert "Decoy note" not in json.dumps(items)                               # an unlinked Task contributes nothing


def test_a_source_reached_by_several_direct_links_appears_once(world):
    items = everything(world, owner_ctx(world, world.first))["items"]
    mine = [item for item in items if item["source"]["id"] == world.linked]
    # Task link + concerns edge + typed note reference all name the same scoped target: three comments, each exactly once.
    assert sorted(item["summary"] for item in mine) == ["Linked note one", "Linked note three", "Linked note two"]
    assert len({item["id"] for item in items}) == len(items)


def test_the_links_are_followed_in_both_directions_and_never_recursively(world):
    items = everything(world, owner_ctx(world, world.first))["items"]
    summaries = {item["summary"] for item in items}
    assert {"Incoming note one", "Incoming note two"} <= summaries             # B links to the record (incoming)
    assert {"Linked note one", "Global note", "Referenced note"} <= summaries  # outgoing link, Global link, typed reference
    # From the linked Task's side the record's events are present too: the link is bidirectional.
    back = everything(world, owner_ctx(world, world.first), task=world.linked)
    assert {"Own note one", "Own note two"} <= {item["summary"] for item in back["items"]}
    # The decoy is linked to nothing, and nothing a linked Task is itself linked to is pulled in.
    far = make_task(world, world.first, "Two hops away")["id"]
    link(world, world.linked, world.first, far, world.first)
    comment(world, world.first, far, "far1", stamp(19), "Two hops note")
    assert "Two hops note" not in {item["summary"] for item in everything(world, owner_ctx(world, world.first))["items"]}
    assert "Two hops note" in {item["summary"] for item in everything(world, owner_ctx(world, world.first), task=world.linked)["items"]}


def test_recorded_observations_and_actions_and_documents_carry_their_own_times(world):
    items = everything(world, owner_ctx(world, world.first))["items"]
    by_summary = {item["summary"]: item for item in items}
    email = by_summary["Synthetic email received"]
    assert email["origin"] == "recorded" and email["occurred_at"] == stamp(5) and email["recorded_at"] == stamp(20)
    assert email["source"]["label"] == "Synthetic mailbox"
    # The Action happened on 1 September and was recorded later: it is ordered by when it happened, cut off by when it was recorded.
    recorded_action = by_summary["Action recorded"]
    assert recorded_action["occurred_at"].startswith("2026-09-01") and recorded_action["recorded_at"] > stamp(20)
    assert recorded_action["source"]["id"] == world.action and recorded_action["change"]["kind"] == "action"
    document = by_summary["Document added"]
    assert document["source"]["id"] == world.document and document["change"]["kind"] == "document"


def test_signoff_developments_are_listed_with_their_lifecycle_and_actors(world):
    items = [item for item in everything(world, owner_ctx(world, world.first))["items"] if item["source"]["type"] == "signoff"]
    states = {item["change"]["state"]: item for item in items}
    assert {"proposed", "decided"} <= set(states)
    assert all(item["kind"] == "development" and item["origin"] == "linked" and item["source"]["id"] == world.signoff for item in items)
    assert states["proposed"]["actor"]["id"] == world.worker and states["decided"]["actor"]["id"] == world.instance.owner
    assert states["proposed"]["occurred_at"] <= states["decided"]["occurred_at"]
    assert states["decided"]["summary"] == "Signoff approved"


# ----- pages, cursor and cutoff -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 3, 7])
def test_bounded_pages_concatenate_to_the_unpaged_listing_without_gaps_or_repeats(world, limit):
    ctx = owner_ctx(world, world.first)
    whole = everything(world, ctx)
    collected = pages(world, ctx, limit)
    assert [identifier for page in collected for identifier in ids(page)] == ids(whole)
    assert all(len(page["items"]) == limit for page in collected[:-1]) and collected[-1]["next_cursor"] is None
    assert len({page["as_of"] for page in collected}) == 1                        # the cutoff is fixed by the first page


def test_a_page_is_bounded_and_an_oversized_or_invalid_request_is_refused(world):
    ctx = owner_ctx(world, world.first)
    assert len(ok(listing(world, ctx, limit=2))["items"]) == 2
    assert len(ok(listing(world, ctx))["items"]) <= 50                           # the default page
    for bad in ({"limit": 101}, {"limit": 0}, {"cursor": "garbage"}, {"cursor": "x" * 2001}):
        assert listing(world, ctx, **bad).error["code"] == "validation_error"


def test_a_cursor_belongs_to_one_record_one_caller_and_one_set_of_links(world):
    ctx = owner_ctx(world, world.first)
    cursor = ok(listing(world, ctx, limit=2))["next_cursor"]
    assert listing(world, ctx, task=world.linked, limit=2, cursor=cursor).error["code"] == "validation_error"
    assert listing(world, agent_ctx(world, world.worker, world.first), limit=2, cursor=cursor).error["code"] == "validation_error"
    assert listing(world, ctx, limit=2, cursor=cursor, include_linked=False).error["code"] == "validation_error"
    extra = make_task(world, world.first, "Linked mid-paging")["id"]
    comment(world, world.first, extra, "mid1", stamp(2), "Mid-paging note")
    link(world, world.record, world.first, extra, world.first)
    stale = listing(world, ctx, limit=2, cursor=cursor)
    assert stale.error["code"] == "revision_conflict"                             # the link set changed: start again
    assert "Mid-paging note" in {item["summary"] for item in everything(world, ctx)["items"]}


def test_nothing_recorded_after_the_cutoff_enters_later_pages_even_when_backdated(world, monkeypatch):
    ctx = owner_ctx(world, world.first)
    cutoff = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(world.runtime.crm_activity, "clock", lambda: cutoff)
    first_page = ok(listing(world, ctx, limit=4))
    before = ids(everything(world, ctx))
    later = (cutoff + timedelta(days=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    comment(world, world.first, world.linked, "post-1", later, "Written after the cutoff")
    recorded(world, world.first, world.record, "post-2", stamp(6), later, "Backdated but recorded after the cutoff", "key-post")
    rest, cursor = [], first_page["next_cursor"]
    while cursor:
        page = ok(listing(world, ctx, limit=4, cursor=cursor))
        rest += ids(page)
        cursor = page["next_cursor"]
    assert ids(first_page) + rest == before
    assert not {"Written after the cutoff", "Backdated but recorded after the cutoff"} & {
        item["summary"] for item in everything(world, ctx)["items"]}              # still excluded at the same cutoff
    # A new listing with a later cutoff includes both, the backdated one in its place by when it happened.
    monkeypatch.setattr(world.runtime.crm_activity, "clock", lambda: cutoff + timedelta(days=2))
    fresh = everything(world, ctx)["items"]
    by_summary = {item["summary"]: item for item in fresh}
    assert by_summary["Backdated but recorded after the cutoff"]["occurred_at"] == stamp(6)
    assert by_summary["Written after the cutoff"]["occurred_at"] == later


# ----- authorization ----------------------------------------------------------------------------------------------------


def test_a_caller_without_access_to_a_linked_source_sees_nothing_of_it(world):
    member = person_ctx(world, world.first)
    items = everything(world, member)["items"]
    summaries = {item["summary"] for item in items}
    assert {"Own note one", "Linked note one"} <= summaries                        # the member's own business is readable
    assert not ({"Incoming note one", "Incoming note two", "Global note"} & summaries)
    text = json.dumps(items)
    assert "Incoming" not in text and world.incoming not in text and "Global task" not in text
    # The same record read with the owner's reach includes them: the omission is authorization, not absence.
    assert "Incoming note one" in {item["summary"] for item in everything(world, owner_ctx(world, world.first))["items"]}


def test_a_revoked_membership_removes_a_sources_events_on_the_next_read(world):
    with world.database.transaction() as uow:
        uow.repository("business_memberships", business_id=world.second).insert({"principal_id": world.person, "is_owner": False})
    wide = InvocationContext(Principal(world.person, "human"), business_id=world.first,
                             allowed_business_ids=frozenset({world.first, world.second}), source="dashboard")
    assert "Incoming note one" in {item["summary"] for item in everything(world, wide)["items"]}
    with world.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?", (world.second, world.person))
    assert "Incoming note one" not in {item["summary"] for item in everything(world, wide)["items"]}


def test_a_registered_agent_reads_with_its_own_access_and_an_absent_link_is_silent(world):
    # An absent target (deleted, or never readable) is skipped without error or placeholder.
    with world.database.transaction() as uow:
        row = uow.connection.execute("SELECT fields_json FROM tasks WHERE business_id=? AND id=?", (world.first, world.decoy)).fetchone()
        fields = json.loads(row[0])
        fields["$task_links"] = [{"task_id": "task-that-is-gone", "business_id": world.first}]
        uow.connection.execute("UPDATE tasks SET fields_json=? WHERE business_id=? AND id=?", (json.dumps(fields), world.first, world.decoy))
    page = everything(world, owner_ctx(world, world.first), task=world.decoy)
    assert all(item["origin"] == "own" for item in page["items"]) and "task-that-is-gone" not in json.dumps(page)
    worker = agent_ctx(world, world.worker, world.first)
    seen = everything(world, worker)
    assert {"Own note one", "Linked note one", "Incoming note one"} <= {item["summary"] for item in seen["items"]}


# ----- purity -----------------------------------------------------------------------------------------------------------


def test_reading_activity_writes_nothing_acknowledges_nothing_and_never_reads_private_conversations(world):
    marker = "PRIVATE-OWNER-SENTENCE"
    owner_ctx_first = owner_ctx(world, world.first)
    source = {"type": "task", "id": world.record, "business_id": world.first}
    ok(call(world, owner_ctx_first, "owner_conversation_send", {"source": source, "text": marker}))
    ok(call(world, agent_ctx(world, world.coordinator, world.first), "owner_conversation_send", {"source": source, "text": marker + " reply"}))
    before = counts(world)
    with world.database.read() as uow:
        data_version = uow.connection.execute("PRAGMA data_version").fetchone()[0]
    for ctx in (owner_ctx_first, agent_ctx(world, world.worker, world.first), person_ctx(world, world.first)):
        page = everything(world, ctx)
        assert marker not in json.dumps(page)
        for items in pages(world, ctx, 3):
            assert marker not in json.dumps(items)
    assert counts(world) == before                                            # no row anywhere: no receipt, event, delivery or read mark
    assert before["owner_conversation_reads"] == counts(world)["owner_conversation_reads"]
    # Nothing was marked read: the owner's unread marker for Ordubashy's reply survives the listing.
    with world.database.read() as uow:
        from bazarlyk.inbox.private_conversations import private_state
        state = private_state(world.runtime.application.current_context(owner_ctx_first, uow), source, uow)
    assert state["private_unread"] is True and state["private_message_count"] == 2
    with world.database.read() as uow:
        assert uow.connection.execute("PRAGMA data_version").fetchone()[0] == data_version
