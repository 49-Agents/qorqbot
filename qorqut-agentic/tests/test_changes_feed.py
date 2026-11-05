"""Phase A item 2/5: durable change feed, consumer cursors, external-change scan."""

from __future__ import annotations

import pytest

from qorqut_agentic import AgenticError
from tests.conftest import ATTR, reopen, sha

STYLE = "plugins/email-composition/preferences/style.md"


def _edit(layer, text):
    current = (layer.root / STYLE).read_bytes()
    return layer.authoring.apply(ATTR, [{"op": "edit", "path": STYLE, "expected_sha256": sha(current), "content": text}])


def test_cursors_survive_reopen_and_failed_delivery_replays(email_layer):
    _edit(email_layer, "one\n")
    _edit(email_layer, "two\n")
    assert email_layer.changes.cursor("overseer") == {**email_layer.changes.cursor("overseer"), "cursor": 0, "exists": False}

    pending = email_layer.changes.list(consumer="overseer")
    assert [e["sequence"] for e in pending["changes"]] == [1, 2, 3] and pending["head"] == 3
    # delivery fails: the cursor is not advanced, so the same events are offered again
    assert [e["change_id"] for e in email_layer.changes.list(consumer="overseer")["changes"]] == \
        [e["change_id"] for e in pending["changes"]]
    # the receiver acknowledged the first two (change IDs are the idempotency keys)
    email_layer.changes.advance_cursor("overseer", 2)
    reopened = reopen(email_layer)
    assert reopened.changes.cursor("overseer")["cursor"] == 2 and reopened.changes.cursor("overseer")["exists"]
    replay = reopened.changes.list(consumer="overseer")
    assert [e["sequence"] for e in replay["changes"]] == [3]
    assert replay["changes"][0]["change_id"] == pending["changes"][2]["change_id"]  # stable IDs across reopen
    assert reopened.changes.list(consumer="somebody-else")["changes"][0]["sequence"] == 1  # cursors are per consumer


def test_cursor_rules(email_layer):
    changes = email_layer.changes
    changes.advance_cursor("c", 1)
    assert changes.advance_cursor("c", 1)["previous"] == 1  # idempotent
    for kwargs, reason in (({"sequence": 0}, "cursor_regress"), ({"sequence": 99}, "beyond_head"),
                           ({"sequence": 1, "expected_cursor": 0}, "cursor_changed")):
        with pytest.raises(AgenticError) as caught:
            changes.advance_cursor("c", **kwargs)
        assert caught.value.code == "state_conflict" and caught.value.details["reason"] == reason
    with pytest.raises(AgenticError) as caught:
        changes.cursor("bad name!")
    assert caught.value.code == "invalid_request"


def test_feed_pages_in_sequence_order(email_layer):
    for i in range(5):
        _edit(email_layer, f"v{i}\n")
    first = email_layer.changes.list(limit=2)
    assert [e["sequence"] for e in first["changes"]] == [1, 2] and first["has_more"]
    rest = email_layer.changes.list(after=first["next_after"], limit=100)
    assert [e["sequence"] for e in rest["changes"]] == [3, 4, 5, 6] and not rest["has_more"]


def test_scan_reports_the_net_change_with_unknown_attribution(email_layer):
    root = email_layer.root / "plugins/email-composition"
    original = (root / "preferences/style.md").read_bytes()
    (root / "preferences/style.md").write_text("first hand edit\n")
    (root / "preferences/style.md").write_text("second hand edit\n")  # intermediate states are not recoverable
    (root / "preferences/new.md").write_text("brand new\n")
    (root / "skills/compose/assets/checklist.txt").unlink()
    (root / "preferences/transient.md").write_text("here and gone\n")
    (root / "preferences/transient.md").unlink()

    report = email_layer.changes.scan()
    assert (report["created"], report["modified"], report["deleted"]) == (1, 1, 1)
    event = report["change"]
    assert event["kind"] == "external_scan" and event["attribution"]["actor_id"] == "external/unknown"
    assert event["attribution"]["trust"] == "external_unknown"
    by_path = {f["path"]: f for f in event["files"]}
    style = by_path[STYLE]
    assert style["before_sha256"] == sha(original) and style["after_sha256"] == sha("second hand edit\n")
    assert by_path["plugins/email-composition/preferences/new.md"]["action"] == "create"
    assert by_path["plugins/email-composition/skills/compose/assets/checklist.txt"]["action"] == "delete"
    assert "plugins/email-composition/preferences/transient.md" not in by_path
    assert "email-composition/style" in event["entry_refs"]
    assert email_layer.changes.scan()["change"] is None  # a second scan has nothing to add


def test_scan_accepts_an_adapter_supplied_attribution_and_reports_skipped_links(email_layer, tmp_path):
    (email_layer.root / "plugins/email-composition/preferences/style.md").write_text("by adapter\n")
    (email_layer.root / "link").symlink_to(tmp_path)
    adapter = type(email_layer).open(email_layer.root, email_layer.state, attribution_source="host_derived")
    report = adapter.changes.scan({"actor_id": "user:42", "actor_kind": "owner", "reason": "inline edit in the UI"})
    assert report["change"]["attribution"] == {"actor_id": "user:42", "actor_kind": "owner",
                                               "reason": "inline edit in the UI", "source_refs": [], "trust": "host_derived"}
    assert report["skipped"] == [{"path": "link", "kind": "symlink"}]


def test_scan_is_refused_while_recovery_is_pending(email_layer):
    from tests.conftest import SimulatedCrash, crash_at

    crash_at(email_layer, "after_journal")
    with pytest.raises(SimulatedCrash):
        _edit(email_layer, "interrupted\n")
    with pytest.raises(AgenticError) as caught:
        reopen(email_layer).changes.scan()
    assert caught.value.code == "recovery_required"


@pytest.mark.parametrize('timeout', [True, -1, float('nan'), float('inf'), '0'])
def test_scan_rejects_invalid_lock_timeout(layer, timeout):
    with pytest.raises(AgenticError) as error:
        layer.changes.scan(lock_timeout=timeout)
    assert error.value.code == 'invalid_request'


def test_scan_defers_when_authoring_lock_is_busy(email_layer):
    from concurrent.futures import ThreadPoolExecutor

    path = email_layer.root / STYLE
    path.write_text(path.read_text() + '\nExternal change.\n')
    with email_layer._ctx.store.lock(exclusive=True):
        def scan():
            with pytest.raises(AgenticError) as error:
                email_layer.changes.scan(lock_timeout=0)
            return error.value.code
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(scan).result(timeout=5) == 'lock_timeout'
    assert email_layer.changes.scan(lock_timeout=0)['change'] is not None
