"""Phase A item 2: expected-hash batches, journal recovery, entry/package operations."""

from __future__ import annotations

import threading

import pytest

from qorqut_agentic import AgenticError
from tests.conftest import ATTR, SimulatedCrash, crash_at, file_sha, git, reopen, sha

STYLE = "plugins/email-composition/preferences/style.md"
COMPOSE = "plugins/email-composition/skills/compose/SKILL.md"


def _append_rule(layer, path, text, attribution=ATTR):
    current = (layer.root / path).read_text()
    return layer.authoring.apply(attribution, [
        {"op": "edit", "path": path, "expected_sha256": sha(current), "content": current + text}])


def test_direct_instruction_is_saved_discoverable_and_evented_before_any_commit(email_layer):
    direct = {"actor_id": "ordubashy", "actor_kind": "agent", "reason": "Owner said: never use exclamation marks",
              "source_refs": ["conversation:abc#42"]}
    result = _append_rule(email_layer, STYLE, "- Never use exclamation marks.\n", direct)
    assert result["changed"] and result["entries"][0]["ref"] == "email-composition/style"
    # the next catalog read already sees the saved revision
    entry = email_layer.catalog.read_entry("email-composition/style")
    assert "exclamation marks" in entry["body"]
    assert entry["entry"]["revision"] == result["entries"][0]["revision"]
    # a durable event exists, with attribution and hashes, while Git has no commit yet
    event = email_layer.changes.list()["changes"][-1]
    assert event["change_id"] == result["change"]["change_id"] and event["kind"] == "apply"
    assert event["attribution"]["actor_id"] == "ordubashy" and event["attribution"]["source_refs"] == ["conversation:abc#42"]
    assert event["attribution"]["trust"] == "caller_supplied"
    assert event["files"][0]["after_sha256"] == file_sha(email_layer, STYLE) and event["commits"] == []
    assert email_layer.history.log()["commits"] == [] and not email_layer.history.status()["clean"]
    # committing later links the commit to the existing event instead of inventing a second one
    head = email_layer.changes.list()["head"]
    commit = email_layer.history.commit("Add exclamation-mark rule", paths=[STYLE])
    assert email_layer.changes.list()["head"] == head
    assert email_layer.changes.get(event["change_id"])["change"]["commits"] == [commit["commit"]]


def test_replacement_content_may_be_base64_and_empty_edits_are_no_ops(email_layer):
    current = (email_layer.root / STYLE).read_bytes()
    result = email_layer.authoring.apply(ATTR, [{"op": "edit", "path": STYLE, "expected_sha256": sha(current),
                                                 "content": current.decode()}])
    assert not result["changed"] and result["unchanged"] == [STYLE] and result["change"] is None
    email_layer.authoring.apply(ATTR, [{"op": "create", "path": "plugins/email-composition/bin.dat",
                                        "content_base64": "AP8Q"}])
    assert (email_layer.root / "plugins/email-composition/bin.dat").read_bytes() == b"\x00\xff\x10"


def test_concurrent_edit_is_a_conflict_and_never_overwrites(email_layer):
    other = reopen(email_layer)
    stale = sha((email_layer.root / STYLE).read_bytes())
    email_layer.authoring.apply(ATTR, [{"op": "edit", "path": STYLE, "expected_sha256": stale, "content": "first\n"}])
    with pytest.raises(AgenticError) as caught:
        other.authoring.apply(ATTR, [{"op": "edit", "path": STYLE, "expected_sha256": stale, "content": "second\n"}])
    conflict = caught.value.details["conflicts"][0]
    assert caught.value.code == "conflict" and conflict["reason"] == "hash_mismatch"
    assert conflict["current_sha256"] == sha("first\n") and (email_layer.root / STYLE).read_text() == "first\n"
    for bad in ({"op": "create", "path": STYLE, "content": "x"},
                {"op": "edit", "path": "plugins/email-composition/nothing.md", "expected_sha256": stale, "content": "x"},
                {"op": "delete", "path": "plugins/email-composition/nothing.md", "expected_sha256": stale},
                {"op": "create", "path": STYLE + "/child", "content": "x"}):
        with pytest.raises(AgenticError) as caught:
            email_layer.authoring.apply(ATTR, [bad])
        assert caught.value.code == "conflict"


def test_racing_writers_with_the_same_expected_hash_produce_exactly_one_winner(email_layer):
    expected = sha((email_layer.root / STYLE).read_bytes())
    outcomes, barrier = [], threading.Barrier(4)

    def attempt(index):
        handle = reopen(email_layer)
        barrier.wait()
        try:
            handle.authoring.apply(ATTR, [{"op": "edit", "path": STYLE, "expected_sha256": expected,
                                           "content": f"writer {index}\n"}])
            outcomes.append("ok")
        except AgenticError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["conflict", "conflict", "conflict", "ok"]
    assert email_layer.changes.list()["head"] == 2  # import + exactly one edit


def test_failed_validation_writes_nothing_and_leaves_no_journal(email_layer):
    before = {p.name: p.read_bytes() for p in (email_layer.root / "plugins/email-composition").rglob("*") if p.is_file()}
    head = email_layer.changes.list()["head"]
    style = (email_layer.root / STYLE).read_text()
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.apply(ATTR, [
            {"op": "edit", "path": STYLE, "expected_sha256": sha(style), "content": style + "extra\n"},
            {"op": "delete", "path": "plugins/email-composition/preferences/style.md.x", "expected_sha256": "0" * 64},
        ])
    assert caught.value.code == "conflict"
    with pytest.raises(AgenticError) as caught:  # deleting a required file breaks the catalog
        email_layer.authoring.apply(ATTR, [
            {"op": "edit", "path": COMPOSE, "expected_sha256": file_sha(email_layer, COMPOSE), "content": "x"},
            {"op": "delete", "path": STYLE, "expected_sha256": sha(style)}])
    assert caught.value.code == "validation_failed" and caught.value.details["changed"] is False
    assert {p.name: p.read_bytes() for p in (email_layer.root / "plugins/email-composition").rglob("*") if p.is_file()} == before
    assert email_layer.changes.list()["head"] == head and not email_layer.recovery_required


def test_a_multi_package_change_is_one_event(full_layer):
    ids = {p: file_sha(full_layer, p) for p in (STYLE, "plugins/influencer-outreach/preferences/pricing-rule.md")}
    result = full_layer.authoring.apply(ATTR, [
        {"op": "edit", "path": path, "expected_sha256": digest, "content": f"theme change {i}\n"}
        for i, (path, digest) in enumerate(ids.items())])
    event = result["change"]
    assert event["package_ids"] == ["email-composition", "influencer-outreach"] and len(event["files"]) == 2


@pytest.mark.parametrize("hook, after", [("after_journal", 1), ("after_file_write", 1), ("after_file_write", 2),
                                         ("before_finalize", 1)])
def test_interrupted_edits_recover_with_their_event(email_layer, hook, after):
    paths = [STYLE, COMPOSE]
    originals = {p: (email_layer.root / p).read_text() for p in paths}
    crash_at(email_layer, hook, after=after)
    with pytest.raises(SimulatedCrash):
        email_layer.authoring.apply(ATTR, [
            {"op": "edit", "path": p, "expected_sha256": sha(originals[p]), "content": originals[p] + "NEW\n"} for p in paths])
    head = email_layer.changes.list()["head"]
    reopened = reopen(email_layer)
    assert reopened.recovery_required and head == 1  # consumers never see the unfinalized change
    status = reopened.recovery_status()
    assert status["required"] and status["resolvable"]
    with pytest.raises(AgenticError) as caught:
        _append_rule(reopened, STYLE, "x")
    assert caught.value.code == "recovery_required"
    report = reopened.recover()
    assert report["recovered"] and len(report["changes"]) == 1
    assert all((reopened.root / p).read_text() == originals[p] + "NEW\n" for p in paths)
    assert reopened.changes.list()["head"] == 2 and not reopened.recovery_required
    assert reopened.changes.scan()["change"] is None  # the observed inventory already matches the files
    assert not list(reopened.root.rglob(".agentic-tmp-*"))
    assert reopened.recover()["recovered"] is False


def test_recovery_never_overwrites_an_unexpected_external_edit(email_layer):
    originals = {p: (email_layer.root / p).read_text() for p in (STYLE, COMPOSE)}
    crash_at(email_layer, "after_file_write", after=1)
    with pytest.raises(SimulatedCrash):
        email_layer.authoring.apply(ATTR, [
            {"op": "edit", "path": p, "expected_sha256": sha(originals[p]), "content": originals[p] + "NEW\n"}
            for p in (STYLE, COMPOSE)])
    (email_layer.root / COMPOSE).write_text("someone else edited this\n")  # third content: neither before nor after
    reopened = reopen(email_layer)
    assert not reopened.recovery_status()["resolvable"]
    with pytest.raises(AgenticError) as caught:
        reopened.recover()
    assert caught.value.code == "recovery_conflict"
    assert caught.value.details["conflicts"][0]["path"] == COMPOSE
    assert (reopened.root / COMPOSE).read_text() == "someone else edited this\n"
    assert reopened.changes.list()["head"] == 1 and reopened.recovery_required
    # the owner resolves it by putting the file back to a recorded state, then recovery completes
    (reopened.root / COMPOSE).write_text(originals[COMPOSE])
    assert reopened.recover()["recovered"]
    assert (reopened.root / COMPOSE).read_text() == originals[COMPOSE] + "NEW\n"


def test_a_failed_write_midway_is_reported_as_recovery_required(email_layer):
    original = (email_layer.root / STYLE).read_text()

    def failing(**_context):
        raise OSError("disk full")

    email_layer._ctx.hooks["after_file_write"] = failing
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.apply(ATTR, [{"op": "edit", "path": STYLE, "expected_sha256": sha(original),
                                            "content": original + "x\n"}])
    assert caught.value.code == "recovery_required" and "disk full" in caught.value.details["cause"]
    email_layer._ctx.hooks.clear()
    assert email_layer.recover()["recovered"]


def test_edits_made_outside_the_toolkit_get_their_own_event_before_a_toolkit_change(email_layer):
    (email_layer.root / STYLE).write_text("edited by hand\n")
    result = email_layer.authoring.apply(ATTR, [{"op": "edit", "path": STYLE, "expected_sha256": sha("edited by hand\n"),
                                                 "content": "edited by hand\nand by the toolkit\n"}])
    kinds = [e["kind"] for e in email_layer.changes.list()["changes"]]
    assert kinds[-2:] == ["external_scan", "apply"]
    external = email_layer.changes.list()["changes"][-2]
    assert external["attribution"]["actor_id"] == "external/unknown"
    assert external["files"][0]["after_sha256"] == sha("edited by hand\n")
    assert result["change"]["files"][0]["before_sha256"] == sha("edited by hand\n")


def test_update_entry_and_remove_entry_and_remove_package(email_layer):
    manifest = "plugins/email-composition/agentic.yaml"
    result = email_layer.authoring.update_entry(
        ATTR, "email-composition",
        {"id": "signoff", "kind": "instruction", "description": "How to sign off", "when_to_use": "When closing an email",
         "path": "instructions/signoff.md", "requires": ["email-composition/style"]},
        file_sha(email_layer, manifest), content="Sign off with the owner's first name.\n")
    assert result["entries"] == [{"ref": "email-composition/signoff", "state": "added",
                                  "revision": sha("Sign off with the owner's first name.\n"),
                                  "bundle_revision": result["entries"][0]["bundle_revision"]}]
    assert "first name" in email_layer.catalog.read_entry("email-composition/signoff")["body"]
    # updating metadata only
    email_layer.authoring.update_entry(ATTR, "email-composition", {"id": "signoff", "when_to_use": "Closing lines"},
                                       file_sha(email_layer, manifest))
    assert email_layer.catalog.read_entry("email-composition/signoff")["entry"]["when_to_use"] == "Closing lines"
    # a required entry cannot be removed
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.remove_entry(ATTR, "email-composition", "style", file_sha(email_layer, manifest))
    assert caught.value.code == "validation_failed"
    bundle = email_layer.catalog.read_entry("email-composition/signoff")["entry"]["bundle_revision"]
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.remove_entry(ATTR, "email-composition", "signoff", file_sha(email_layer, manifest),
                                           delete_files=True, expected_bundle_revision="0" * 64)
    assert caught.value.code == "conflict"
    email_layer.authoring.remove_entry(ATTR, "email-composition", "signoff", file_sha(email_layer, manifest),
                                       delete_files=True, expected_bundle_revision=bundle)
    assert not (email_layer.root / "plugins/email-composition/instructions").exists()
    revision = email_layer.catalog.validate()
    assert revision["valid"]
    from qorqut_agentic.authoring import package_revision
    from qorqut_agentic.files import DiskSource

    current = package_revision(DiskSource(email_layer._ctx.tree).files, "plugins/email-composition")
    email_layer.authoring.remove_package(ATTR, "email-composition", current)
    assert not (email_layer.root / "plugins/email-composition").exists() and email_layer.catalog.list()["total"] == 0
    assert [e["kind"] for e in email_layer.changes.list()["changes"]][-1] == "remove_package"


def test_changes_outside_packages_are_allowed_but_git_and_reserved_names_are_not(email_layer):
    email_layer.authoring.apply(ATTR, [{"op": "create", "path": "plugins/email-composition/docs/NOTE.md", "content": "n"}])
    for bad in (".git/hooks/pre-commit", "plugins/x/.agentic-tmp-1"):
        with pytest.raises(AgenticError) as caught:
            email_layer.authoring.apply(ATTR, [{"op": "create", "path": bad, "content": "x"}])
        assert caught.value.code == "unsafe_path"
    assert git(email_layer, "status", "--short").stdout.count("plugins/") == 1
