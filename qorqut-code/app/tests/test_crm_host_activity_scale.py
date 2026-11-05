"""CRM activity beyond discovery/version batch sizes, with a stable local resume position.

Runs against the real composed Domain and Host over a synthetic SQLite store. Source counts, discovery rows and document
history exceed the former fixed ceilings. Tests that simulate an event disappearing wrap local_activity explicitly;
the Domain resumes at the last emitted timestamp and event id, never a count of previously visible rows."""

from __future__ import annotations

import base64
import json

import pytest

from qorqut_operations.contracts import InvocationContext, Principal, new_id

from bazarlyk.domains.crm import activity as activity_module
from bazarlyk.domains.crm.activity import CRMActivityOperations
from bazarlyk.domains.crm.sources import instant
from test_crm_host_activity import (call, everything, ids, link, listing, make_task, ok, owner_ctx, pages, world)  # noqa: F401

BULK = "2026-09-{day:02d}T{hour:02d}:{minute:02d}:00.000000Z"


def when(index):
    """A distinct, increasing synthetic instant for the ``index``-th bulk event."""
    return BULK.format(day=1 + index // 1440, hour=(index // 60) % 24, minute=index % 60)


def bulk(world, sql, rows):
    with world.database.transaction() as uow:
        uow.connection.executemany(sql, rows)


def bulk_comments(world, business, comments):
    """``comments``: ``(task, id, created_at, text, refs)``."""
    bulk(world, "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,refs_json,author_id,recorder_id,created_at) "
                "VALUES (?,?,'task',?,'comment',?,?,?,?,?)",
         [(business, identifier, task, text, json.dumps(refs), world.instance.owner, world.instance.owner, at)
          for task, identifier, at, text, refs in comments])


def link_from(world, tasks, business, record, record_business):
    """Make each of ``tasks`` (of ``business``) a direct Task link to ``record``, in the store's own link form."""
    bulk(world, "UPDATE tasks SET fields_json=json_set(fields_json,'$.\"$task_links\"',json(?)) WHERE business_id IS ? AND id=?",
         [(json.dumps([{"business_id": record_business, "task_id": record}]), business, task) for task in tasks])


def member_of(world, business):
    """A human who is a member of exactly ``business``."""
    person = new_id()
    with world.database.transaction() as uow:
        uow.repository("principals").insert({"id": person, "kind": "human", "display_name": "Synthetic Reader"})
        uow.repository("business_memberships", business_id=business).insert({"principal_id": person, "is_owner": False})
    return InvocationContext(Principal(person, "human"), business_id=business, allowed_business_ids=frozenset({business}), source="dashboard")


def walk(world, ctx, record, limit, **arguments):
    """Every item of every page, in page order. No page may exceed ``limit`` or be empty before the last."""
    collected = pages(world, ctx, limit, task=record, **arguments)
    assert all(page["items"] for page in collected[:-1])
    return [item for page in collected for item in page["items"]]


def in_canonical_order(items):
    keys = [(instant(item["occurred_at"]), item["id"]) for item in items]
    assert keys == sorted(keys, reverse=True) and len(set(keys)) == len(keys)


# ----- no source ceiling ------------------------------------------------------------------------------------------------


def test_more_than_one_hundred_incoming_sources_are_all_reachable_when_denied_sources_sort_first(world):
    denied, readable = sorted((world.first, world.second))             # the sources of the denied business sort first
    reader = member_of(world, readable)
    record = make_task(world, readable, "Scale record")["id"]
    mine = [make_task(world, readable, f"Readable source {index}")["id"] for index in range(120)]
    theirs = [make_task(world, denied, f"Denied source {index}")["id"] for index in range(150)]
    link_from(world, mine, readable, record, readable)
    link_from(world, theirs, denied, record, readable)
    bulk_comments(world, readable, [(task, f"scale-m-{index}", when(index), f"Readable note {index}", []) for index, task in enumerate(mine)])
    bulk_comments(world, denied, [(task, f"scale-d-{index}", when(400 + index), f"Denied note {index}", []) for index, task in enumerate(theirs)])

    seen = walk(world, reader, record, 7)
    in_canonical_order(seen)
    notes = [item["summary"] for item in seen if item["kind"] == "comment"]
    assert sorted(notes) == sorted(f"Readable note {index}" for index in range(120))           # all of them, once each
    assert "Denied" not in json.dumps(seen)                                                    # and not one denied source
    assert [item["id"] for item in seen][:100] == ids(everything(world, reader, record))       # page size does not change the order

    everyone = walk(world, owner_ctx(world, readable), record, 100)                            # 270 sources, pages of 100
    in_canonical_order(everyone)
    assert len([item for item in everyone if item["kind"] == "comment"]) == 270

    # The cursor covers the complete link set, not its first hundred: a source that sorts after the denied ones changes it.
    first = ok(listing(world, reader, record, limit=5))
    assert first["next_cursor"]
    added = make_task(world, readable, "Late readable source")["id"]
    link_from(world, [added], readable, record, readable)
    stale = listing(world, reader, record, limit=5, cursor=first["next_cursor"])
    assert stale.status != "ok" and stale.error["code"] == "revision_conflict"


def test_a_source_reached_by_every_kind_of_link_is_listed_once_among_more_than_one_hundred_sources(world):
    reader = member_of(world, world.first)
    record = make_task(world, world.first, "Deduplication record")["id"]
    many = [make_task(world, world.first, f"Dedup source {index}")["id"] for index in range(130)]
    link_from(world, many, world.first, record, world.first)                       # incoming link from each
    twice = many[0]
    link(world, record, world.first, twice, world.first)                           # ... and an outgoing link to the first
    with world.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,created_at,updated_at,created_by) "
            "VALUES (?,?,'task',?,'task',?,'concerns',?,?,?)",
            (world.first, new_id(), record, twice, when(1), when(1), world.instance.owner))     # ... and a relationship
    bulk_comments(world, world.first, [(record, "dd-own", when(300), "Own note", [{"type": "task", "id": twice}])])   # ... and a reference
    bulk_comments(world, world.first, [(task, f"dd-{index}", when(index), f"Dedup note {index}", []) for index, task in enumerate(many)])
    seen = walk(world, reader, record, 9)
    in_canonical_order(seen)
    assert sorted(item["summary"] for item in seen if item["summary"].startswith("Dedup note")) == sorted(f"Dedup note {index}" for index in range(130))


def test_discovery_reaches_a_link_beyond_two_hundred_rows_whatever_their_kind_and_order(world):
    reader = member_of(world, world.first)
    record = make_task(world, world.first, "Discovery record")["id"]
    through_edge, early_ref, late_ref = (make_task(world, world.first, name)["id"] for name in ("Edge target", "Early reference", "Late reference"))
    bulk_comments(world, world.first, [(through_edge, "dr-e", when(10), "Reached through a relationship", []),
                                       (early_ref, "dr-r1", when(11), "Reached through an early reference", []),
                                       (late_ref, "dr-r2", when(12), "Reached through a late reference", [])])
    # 230 relationships to sources that no longer exist come first; the readable edge is the 231st row.
    edges = [(world.first, new_id(), record, new_id(), "concerns", when(1), when(1), world.instance.owner) for _ in range(230)]
    edges.append((world.first, new_id(), record, through_edge, "concerns", when(1), when(1), world.instance.owner))
    bulk(world, "INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,created_at,updated_at,created_by) "
                "VALUES (?,?,'task',?,'task',?,?,?,?,?)", edges)
    # The record's own comments: the oldest one is inserted first and references a readable Task, then 230 newer comments
    # reference absent ones, and the last one inserted is again the *oldest* by time and references the other readable Task.
    crowd = [(record, f"dr-c{index}", when(100 + index), f"Own crowd note {index}", [{"type": "task", "id": new_id()}]) for index in range(230)]
    bulk_comments(world, world.first, [(record, "dr-first", when(2), "Own first reference", [{"type": "task", "id": early_ref}])])
    bulk_comments(world, world.first, crowd)
    bulk_comments(world, world.first, [(record, "dr-last", when(3), "Own last reference", [{"type": "task", "id": late_ref}])])

    seen = walk(world, reader, record, 100)
    in_canonical_order(seen)
    assert {item["summary"] for item in seen if item["origin"] == "linked" and item["kind"] == "comment"} == {
        "Reached through a relationship", "Reached through an early reference", "Reached through a late reference"}
    assert len([item for item in seen if item["origin"] == "own" and item["kind"] == "comment"]) == 230 + 2   # nothing own is lost either
    assert [item["id"] for item in seen] == [item["id"] for item in walk(world, reader, record, 13)]


def test_a_documents_history_is_reachable_beyond_two_thousand_versions_with_equal_timestamps(world):
    reader = owner_ctx(world, world.first)
    record = make_task(world, world.first, "Document history record")["id"]
    bulk_comments(world, world.first, [(record, "dv-ref", when(1), "Names the document", [{"type": "material", "id": world.document}])])
    with world.database.read() as uow:
        namespace = uow.connection.execute("SELECT namespace FROM documents WHERE id=?", (world.document,)).fetchone()[0]
    shared = "2026-09-15T10:00:00.000000+00:00"
    rows = [(namespace, world.document, 10_000 + index, "synthetic text", f"{index:064x}", world.instance.owner,
             shared if index < 2100 else f"2026-09-15T10:00:{(index - 2100) % 50 + 1:02d}.000000+00:00", "synthetic", None) for index in range(2150)]
    bulk(world, "INSERT INTO qbdv_document_versions(namespace,document_id,version,content,content_sha256,author_id,created_at,reason,"
                "restored_from_version) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    with world.database.read() as uow:
        stored = uow.connection.execute("SELECT version,created_at FROM qbdv_document_versions WHERE namespace=? AND document_id=?",
                                        (namespace, world.document)).fetchall()
    assert len(stored) > 2000
    prefix = f"material:{world.first}:{world.document}:version:"
    expected = sorted(((instant(row["created_at"]), prefix + str(row["version"])) for row in stored), reverse=True)

    seen = walk(world, reader, record, 100)
    in_canonical_order(seen)
    versions = [(instant(item["occurred_at"]), item["id"]) for item in seen if item["source"]["type"] == "document"]
    assert versions == expected                                                                      # every version, once, in order
    assert [item["id"] for item in seen] == [item["id"] for item in walk(world, reader, record, 37)]


# ----- the Domain stream resumes by position ----------------------------------------------------------------------------------


@pytest.fixture
def stream(world, monkeypatch):
    """The real Domain behind a test-only filter that can hide events, and small windows so a page spans several of them."""
    crm = world.runtime.application.crm_activity.crm
    real = crm.local_activity
    hidden = set()

    def local_activity(ctx, task_id, args, uow):
        result = real(ctx, task_id, args, uow)
        return {**result, "items": [item for item in result["items"] if item["id"] not in hidden]}

    monkeypatch.setattr(crm, "local_activity", local_activity)
    monkeypatch.setattr(activity_module, "WINDOW", 3)
    return hidden


def local_record(world, count, *, linked_at=(), tie=None):
    """A record with ``count`` own comments (a shared instant for ``tie`` of them) and one linked Task commenting at ``linked_at``."""
    record = make_task(world, world.first, "Stream record")["id"]
    other = make_task(world, world.first, "Stream linked")["id"]
    link(world, record, world.first, other, world.first)
    bulk_comments(world, world.first, [(record, f"st-{record[:6]}-{index}", when(500 + (0 if tie and index < tie else index)), f"Own-{index:02d}-text", [])
                                       for index in range(count)])
    bulk_comments(world, world.first, [(other, f"sl-{record[:6]}-{index}", when(500 + moment), f"Linked-{index:02d}-text", []) for index, moment in enumerate(linked_at)])
    return record


def snapshot(world, reader, record):
    """Every item of the record's complete listing, the ids in order, and the ids of the Domain's own (local) events."""
    items = walk(world, reader, record, 100)
    return items, [item["id"] for item in items], [item["id"] for item in items if item["origin"] == "own"]


def test_hiding_events_between_pages_neither_skips_nor_repeats_the_surviving_ones(world, stream):
    reader = member_of(world, world.first)
    record = local_record(world, 14, linked_at=(2, 5, 9, 11))
    before, order, own = snapshot(world, reader, record)
    assert len(order) >= 18 and len(own) >= 14
    first = ok(listing(world, reader, record, limit=4))
    assert first["next_cursor"] and ids(first) == order[:4]
    # One own event already emitted and three not yet emitted (the next one among them) stop being visible between pages.
    emitted = [identifier for identifier in order[:4] if identifier in own][0]
    upcoming = [identifier for identifier in order[4:] if identifier in own]
    stream.update({emitted, upcoming[0], upcoming[len(upcoming) // 2], upcoming[-1]})
    rest, cursor = [], first["next_cursor"]
    while cursor:
        page = ok(listing(world, reader, record, limit=4, cursor=cursor))
        assert len(page["items"]) == 4 or not page["next_cursor"]
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    shown = [item["id"] for item in rest]
    assert shown == [identifier for identifier in order[4:] if identifier not in stream]
    assert len(set(shown)) == len(shown) and not set(shown) & set(order[:4])
    # Hidden events leave no text behind, and no empty page between two others (checked by walking from the start too).
    hidden_texts = [item["summary"] for item in before if item["id"] in stream]
    assert len(hidden_texts) == 4 and not any(text in json.dumps(rest) for text in hidden_texts)
    assert [item["id"] for item in walk(world, reader, record, 5)] == [identifier for identifier in order if identifier not in stream]
    # A restored event simply appears where it belongs when the listing is started again.
    stream.clear()
    assert [item["id"] for item in walk(world, reader, record, 5)] == order


def test_the_local_stream_resumes_at_the_last_emitted_position_not_at_a_count_of_visible_rows(world, stream):
    reader = member_of(world, world.first)
    record = local_record(world, 11)
    own_only = {"include_linked": False}                                  # the listing is then exactly the Domain's local stream
    order = [item["id"] for item in walk(world, reader, record, 100, **own_only)]
    assert len(order) >= 11
    # The page ends in the middle of a window (WINDOW is 3, the limit 4), and the last event it emitted - which is the first
    # of the next window - disappears, as does an earlier emitted one. Resuming at a count of rows would skip the next event.
    first = ok(listing(world, reader, record, limit=4, **own_only))
    assert ids(first) == order[:4]
    stream.update({order[3], order[0]})
    rest, cursor = [], first["next_cursor"]
    while cursor:
        page = ok(listing(world, reader, record, limit=4, cursor=cursor, **own_only))
        rest.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert rest == order[4:]


def test_an_event_that_becomes_visible_behind_the_emitted_position_is_neither_repeated_nor_inserted(world, stream):
    reader = member_of(world, world.first)
    record = local_record(world, 11)
    own_only = {"include_linked": False}
    order = [item["id"] for item in walk(world, reader, record, 100, **own_only)]
    stream.add(order[3])                                                  # not visible when the listing starts
    first = ok(listing(world, reader, record, limit=4, **own_only))
    assert ids(first) == [order[0], order[1], order[2], order[4]]
    stream.clear()                                                        # now visible again, but newer than the position
    rest, cursor = [], first["next_cursor"]
    while cursor:
        page = ok(listing(world, reader, record, limit=4, cursor=cursor, **own_only))
        rest.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert rest == order[5:] and len(set(rest)) == len(rest)              # the emitted order[4] is not repeated


def test_a_window_of_hidden_events_does_not_end_the_local_stream_early(world, stream):
    reader = member_of(world, world.first)
    record = local_record(world, 12)
    _, order, own = snapshot(world, reader, record)
    # Hide six consecutive local events: at least one whole window (WINDOW is 3) returns nothing, and the stream must go on.
    stream.update(own[3:9])
    paged = [item["id"] for item in walk(world, reader, record, 2)]
    assert paged == [identifier for identifier in order if identifier not in stream] and len(paged) == len(order) - 6
    # ... also when the very first window is entirely hidden.
    stream.clear()
    stream.update(own[:3])
    assert [item["id"] for item in walk(world, reader, record, 2)] == [identifier for identifier in order if identifier not in stream]


def test_events_sharing_an_instant_keep_one_canonical_order_across_pages_and_windows(world, stream):
    reader = member_of(world, world.first)
    record = local_record(world, 9, linked_at=(0, 0, 3), tie=6)           # six own comments and two linked ones share an instant
    items, order, own = snapshot(world, reader, record)
    keys = [(instant(item["occurred_at"]), item["id"]) for item in items]
    assert keys == sorted(keys, reverse=True) and len(set(keys)) == len(keys)
    assert max(keys.count(key) for key in keys) == 1 and len({key[0] for key in keys}) <= len(keys) - 7
    for limit in (1, 2, 3, 4, 7):
        assert [item["id"] for item in walk(world, reader, record, limit)] == order, limit
    # A tied event disappearing between two pages leaves the others exactly once, in the same order.
    first = ok(listing(world, reader, record, limit=3))
    gone = [identifier for identifier in order[3:] if identifier in own][1]
    stream.add(gone)
    rest, cursor = [], first["next_cursor"]
    while cursor:
        page = ok(listing(world, reader, record, limit=3, cursor=cursor))
        rest.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert rest == [identifier for identifier in order[3:] if identifier != gone]


def test_a_page_mixing_partial_windows_with_linked_events_resumes_at_the_last_emitted_position(world, stream):
    reader = member_of(world, world.first)
    record = local_record(world, 10, linked_at=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10))     # local and linked events alternate in time
    _, order, own = snapshot(world, reader, record)
    stream.update({own[2], own[5], own[6]})                                          # partial windows from the first read on
    expected = [identifier for identifier in order if identifier not in stream]
    for limit in (1, 2, 3, 5):
        assert [item["id"] for item in walk(world, reader, record, limit)] == expected, limit


def reencoded(cursor, **changes):
    payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    payload.update(changes)
    return CRMActivityOperations.encode({key: value for key, value in payload.items() if value is not ...})


def test_a_cursor_with_the_wrong_structure_or_types_is_a_validation_error_and_a_changed_link_set_a_conflict(world):
    reader = member_of(world, world.first)
    record = local_record(world, 8, linked_at=(1, 2))
    cursor = ok(listing(world, reader, record, limit=3))["next_cursor"]
    assert ok(listing(world, reader, record, limit=3, cursor=cursor))["items"]
    bad = {"position is an instant only": {"k": ["2026-09-01T00:00:00.000000Z"]}, "position holds numbers": {"k": [1, 2]},
           "position is a text": {"k": "2026-09-01T00:00:00.000000Z"}, "position is empty": {"k": []},
           "state is a text": {"l": "t"}, "state token is a number": {"l": {"t": 5}}, "state is not exhausted-or-token": {"l": {"done": False}},
           "state has an extra key": {"l": {"t": None, "extra": 1}}, "cutoff is not a time": {"a": "tomorrow"}, "cutoff is a number": {"a": 3},
           "fingerprint is a number": {"f": 7}, "version differs": {"v": 2}, "binding differs": {"b": "0" * 64},
           "unknown field": {"unexpected": 1}}
    for label, changes in bad.items():
        refused = listing(world, reader, record, limit=3, cursor=reencoded(cursor, **changes))
        assert refused.status != "ok" and refused.error["code"] == "validation_error", label
    for garbage in ("not a cursor", "e30=", base64.urlsafe_b64encode(b"[1,2]").decode(), base64.urlsafe_b64encode(b"\xff\xfe").decode()):
        refused = listing(world, reader, record, limit=3, cursor=garbage)
        assert refused.status != "ok" and refused.error["code"] == "validation_error", garbage
    changed = listing(world, reader, record, limit=3, cursor=reencoded(cursor, f="0" * 32))
    assert changed.status != "ok" and changed.error["code"] == "revision_conflict"
    # A position far outside the data is not clamped to anything: it simply leaves nothing older.
    old = ok(listing(world, reader, record, limit=3, cursor=reencoded(cursor, k=["0001-01-01T00:00:00.000000Z", ""], l=...)))
    assert old["items"] == [] and old["next_cursor"] is None


def test_a_domain_stream_that_is_not_ordered_across_windows_is_refused_rather_than_reordered(world, monkeypatch):
    reader = member_of(world, world.first)
    record = local_record(world, 8)
    crm = world.runtime.application.crm_activity.crm
    real = crm.local_activity
    first_window = {}

    def disordered(ctx, task_id, args, uow):
        result = real(ctx, task_id, args, uow)
        if not args.get("cursor"):
            first_window["items"] = result["items"]
            return result
        return {**result, "items": first_window["items"]}                           # the second window repeats newer events

    monkeypatch.setattr(crm, "local_activity", disordered)
    monkeypatch.setattr(activity_module, "WINDOW", 3)
    refused = listing(world, reader, record, limit=20)
    assert refused.status != "ok" and refused.error["code"] == "internal_error"
