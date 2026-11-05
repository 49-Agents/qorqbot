"""Synthetic source lineage, never real conversations."""
from bazarlyk.overseer.provenance import excluded, source_ref


def ref(identity, *, producer=None, origin=None, originals=(), overseer=False):
    return source_ref(provider="operations", kind="note", record_id=identity, revision=1,
        content={"body": identity}, episode_id="task", actor_id=None, actor_kind=None, occurred_at=None,
        locator={}, scope={}, namespace={"kind": "global", "business_id": None}, producer_id=producer, original_refs=originals, original_event_id=origin,
        overseer_origin=overseer)


def test_explicit_mirrors_follow_original_and_exclude_overseer():
    original = ref("original", producer="overseer", origin="event-1")
    mirror = ref("mirror", producer="other", origin="event-1", originals=["original"])
    lookup = {"original": original}.get
    assert excluded(mirror, overseer_ids={"overseer"}, lookup=lookup)
    assert mirror["origin_event_id"] == original["origin_event_id"]
    assert original["actor_kind"] is None


def test_unknown_origin_is_not_invented_and_cycles_terminate():
    first = ref("first", originals=["second"])
    second = ref("second", originals=["first"])
    assert first["provenance"]["independence"] == "unknown"
    assert first["origin_event_id"] is None
    assert not excluded(first, overseer_ids={"overseer"}, lookup={"first": first, "second": second}.get)


def test_text_hash_matches_source_bytes():
    from bazarlyk.overseer.provenance import content_hash
    assert content_hash("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_same_original_native_event_remains_one_event_across_relays():
    from bazarlyk.overseer.provenance import native_event_id
    original = native_event_id("claude", "session", "message")
    first = ref("relay-a", origin=original)
    second = ref("relay-b", origin=native_event_id("claude", "session", "message"))
    assert first["origin_event_id"] == second["origin_event_id"]
    assert native_event_id("claude", "session", "different") != original
