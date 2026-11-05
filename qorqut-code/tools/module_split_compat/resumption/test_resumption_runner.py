"""Focused checks of the runner's own machinery: closed checkpoints, import-origin control, evidence comparison.

No host, Operations or Context code is imported here; the pending-work cases themselves are exercised by running the runner
against the baseline (see README.md)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resumption  # noqa: E402
import resumption_cases as cases  # noqa: E402
import resumption_support as support  # noqa: E402


def make_tree(root: Path) -> None:
    (root / "instance" / "deep").mkdir(parents=True)
    (root / "instance" / "deep" / "one.txt").write_text("one")
    (root / "instance" / "script.sh").write_text("#!/bin/sh\n")
    os.chmod(root / "instance" / "script.sh", 0o775)           # group-writable: the "tar" extraction filter would clear it
    (root / "instance" / "empty.lock").write_bytes(b"")
    os.symlink("deep/one.txt", root / "instance" / "link")


def test_a_closed_checkpoint_restores_byte_for_byte_at_the_same_root_and_is_never_overwritten(tmp_path):
    fixtures = tmp_path / "fixtures"
    root = fixtures / "S0"
    make_tree(root)
    snapshot = support.snapshot_tree(root, tmp_path / "checkpoints" / "S0" / "closed.tar")
    with pytest.raises(FileExistsError):
        support.snapshot_tree(root, tmp_path / "checkpoints" / "S0" / "closed.tar")
    (root / "instance" / "deep" / "one.txt").write_text("changed by a continuation")
    (root / "instance" / "extra").write_text("left over")
    result = support.restore_tree(Path(snapshot["archive"]), root, within=fixtures, expect=snapshot)
    assert result["manifest_sha256"] == snapshot["manifest_sha256"]
    assert (root / "instance" / "deep" / "one.txt").read_text() == "one"
    assert not (root / "instance" / "extra").exists()
    assert oct((root / "instance" / "script.sh").stat().st_mode & 0o7777) == "0o775"
    assert os.readlink(root / "instance" / "link") == "deep/one.txt"


def test_restore_refuses_a_tampered_archive_and_a_root_outside_the_fixtures(tmp_path):
    fixtures = tmp_path / "fixtures"
    root = fixtures / "S0"
    make_tree(root)
    snapshot = support.snapshot_tree(root, tmp_path / "closed.tar")
    with pytest.raises(ValueError, match="not below"):
        support.restore_tree(Path(snapshot["archive"]), tmp_path / "elsewhere", within=fixtures, expect=snapshot)
    with open(snapshot["archive"], "ab") as handle:
        handle.write(b"x")
    with pytest.raises(ValueError, match="checksum"):
        support.restore_tree(Path(snapshot["archive"]), root, within=fixtures, expect=snapshot)
    assert (root / "instance" / "deep" / "one.txt").read_text() == "one"      # nothing was deleted before the check


def test_reading_a_closed_database_leaves_the_checkpoint_untouched_and_refuses_an_unclean_close(tmp_path):
    fixture = tmp_path / "S0"
    database = fixture / "instance" / support.DATABASE
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t(id TEXT, body_json TEXT)")
    connection.execute("INSERT INTO t VALUES ('a', '{\"k\": 1}')")
    connection.commit()
    connection.close()
    before = support.tree_manifest(fixture)
    with support.open_database(fixture) as db:
        assert db.rows("SELECT * FROM t") == [{"id": "a", "body_json": {"k": 1}}]
    assert support.tree_manifest(fixture) == before                              # no -shm / -wal appeared
    Path(str(database) + "-wal").write_bytes(b"unmerged")
    with pytest.raises(RuntimeError, match="not closed cleanly"):
        support.open_database(fixture)


def test_import_origins_are_checked_against_expected_and_forbidden_roots(tmp_path):
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    origins = {"bazarlyk": str(candidate / "app" / "bazarlyk"), "qorqut_operations": str(baseline / "ops" / "qorqut_operations"),
               "qorqut_context": str(candidate / "ctx"), "qorqut_business_docs_versioning": None}
    problems = support.origin_problems(origins, expect_under=[candidate], forbid_under=[baseline])
    assert len(problems) == 2 and any("forbidden root" in item for item in problems) and any("expected roots" in item for item in problems)
    assert support.origin_problems({**origins, "qorqut_operations": str(candidate / "ops")}, [candidate], [baseline]) == []
    assert support.origin_problems({**origins, "bazarlyk": None}, [], []) == ["bazarlyk is not importable"]


def test_scrub_removes_run_specific_values_and_keeps_saved_identities_and_hashes():
    known = {"11111111-1111-4111-8111-111111111111"}
    sha = "ab" * 32
    value = {"saved": "11111111-1111-4111-8111-111111111111", "generated": "22222222-2222-4222-8222-222222222222", "hash": sha,
             "commit": "c" * 40, "at": "2026-10-12T12:00:00Z", "note": "done 2026-10-12T12:00:15.5+00:00", "claimed_by": "x"}
    assert support.scrub(value, known) == {"saved": "11111111-1111-4111-8111-111111111111", "generated": "<new-id>", "hash": sha,
                                           "commit": "<git-oid>", "note": "done <time>"}
    assert support.scrub(value, known, oids=False)["commit"] == "c" * 40


def evidence(directory: Path, name: str, *, digest, origins, forbid, manifest="m1", result="pass", comparable=None):
    support.write_json(directory / name, {
        "result": result, "failed": [], "comparable_digest": digest, "comparable": comparable or {"state": digest},
        "checkpoint": {"manifest_sha256": manifest}, "imports": {"origins": origins, "problems": [], "forbid_under": forbid}})


def check_arguments(root: Path, **options):
    return argparse.Namespace(artifacts=str(root), baseline_label="baseline-control", candidate_label="candidate", case=None,
                              self_test=False, **options)


def test_check_accepts_an_equivalent_candidate_and_rejects_every_kind_of_difference(tmp_path, capsys):
    base, other = tmp_path / "evidence" / "baseline-control", tmp_path / "evidence" / "candidate"
    old, new = {"bazarlyk": "/baseline/app"}, {"bazarlyk": "/candidate/app"}
    evidence(base, "S1-none.json", digest="d1", origins=old, forbid=[])
    evidence(other, "S1-none.json", digest="d1", origins=new, forbid=["/baseline"])
    assert resumption.command_check(check_arguments(tmp_path)) == 0

    evidence(other, "S1-none.json", digest="d2", origins=new, forbid=["/baseline"], comparable={"state": "d2"})
    assert resumption.command_check(check_arguments(tmp_path)) == 1
    assert "outcome differs" in capsys.readouterr().out

    evidence(other, "S1-none.json", digest="d1", origins=new, forbid=[], manifest="m2")                 # no forbid, other start
    assert resumption.command_check(check_arguments(tmp_path)) == 1
    output = capsys.readouterr().out
    assert "same closed checkpoint" in output and "no forbidden" in output

    evidence(other, "S1-none.json", digest="d1", origins=old, forbid=["/baseline"])                        # baseline code imported
    assert resumption.command_check(check_arguments(tmp_path)) == 1
    assert "exactly what the baseline imported" in capsys.readouterr().out
    assert resumption.command_check(argparse.Namespace(**{**vars(check_arguments(tmp_path)), "self_test": True})) == 0

    (other / "S1-none.json").unlink()
    assert resumption.command_check(check_arguments(tmp_path)) == 1


def test_the_cases_are_the_four_required_scenarios_with_their_variants():
    assert tuple(cases.CASES) == resumption.CASE_IDS == ("S1", "S2", "S3a", "S3b", "S4a", "S4b")
    assert all("none" in case.variants and case.title for case in cases.CASES.values())
    assert set(cases.CASES["S2"].variants) == {"none", "revoked", "conflict"}
    assert {name for case in cases.CASES.values() for name in case.variants} <= {"none", "revoked", "conflict"}


def test_an_unknown_component_or_missing_roots_are_refused_before_anything_is_imported():
    args = argparse.Namespace(command="resume", baseline_checkout=None, code_path=["/x"], component=["engine=/y"])
    with pytest.raises(resumption.Refused, match="NAME=DIR"):
        resumption.selected_imports(args)
    args = argparse.Namespace(command="resume", baseline_checkout=None, code_path=["/x"], component=["app=/y"])
    with pytest.raises(resumption.Refused, match="component locations are required"):
        resumption.selected_imports(args)
    args = argparse.Namespace(command="resume", baseline_checkout=None, code_path=None,
                              component=[f"{name}=/y/{name}" for name in resumption.COMPONENT_NAMES])
    with pytest.raises(resumption.Refused, match="no import roots"):
        resumption.selected_imports(args)


def test_the_comparable_state_keeps_saved_rows_by_id_and_new_rows_by_projection_without_volatile_fields():
    before = {"state": {"documents": {"d1": {"revision": 1, "content_hash": "old"}}}}
    after = {"state": {"documents": {"d1": {"revision": 2, "content_hash": "new"}, "random-id": {"revision": 1, "content_hash": "x"}}}}
    assert resumption.comparable_state(before, after) == {"documents": {
        "existing": {"d1": {"revision": 2, "content_hash": "new"}}, "new": [{"revision": 1, "content_hash": "x"}]}}
    assert resumption.comparable_state(before, after, {"documents": ("content_hash",)}) == {"documents": {
        "existing": {"d1": {"revision": 2}}, "new": [{"revision": 1}]}}


# -- negative checks: the added publication-content and event-binding assertions cannot silently accept a mismatch -------------

def facts(**parts):
    base = {"identities": {}, "state": {}, "counts": {}, "files": {}, "ids": [], "table_snapshots": {},
            "git": {"operations": {"refs": {}, "table_history": [], "table_content": []},
                    "context": {"refs": {}, "commits": 0, "log": [], "head": None, "tree": None}},
            "context": {"inputs": {}, "attempts": [], "filings": {}, "completions": []}}
    for key, value in parts.items():
        base[key] = value
    return base


def file_entry(sha, digest):
    return {"mode": "100644", "bytes": 10, "sha256": sha, "json_digest": digest}


def table_pair():
    """S3a: two versions, snapshots saved before; both published after, in order, with matching content."""
    snapshots = {"v1": {"path": "t/a.json", "sequence": 1, "digest": "D1"}, "v2": {"path": "t/a.json", "sequence": 2, "digest": "D2"}}
    jobs = {"j1": {"state": "queued", "kind": "tables.version"}, "j2": {"state": "queued", "kind": "tables.version"}}
    before = facts(table_snapshots=snapshots, state={
        "table_versions": {"v1": {"sequence": 1, "git_commit": None, "snapshot_retained": True},
                           "v2": {"sequence": 2, "git_commit": None, "snapshot_retained": True}}, "jobs": jobs})
    after = facts(state={
        "table_versions": {"v1": {"sequence": 1, "git_commit": "c1", "snapshot_retained": False},
                           "v2": {"sequence": 2, "git_commit": "c2", "snapshot_retained": False}},
        "jobs": {key: {**item, "state": "succeeded"} for key, item in jobs.items()}})
    after["git"]["operations"]["table_history"] = [["c2", "Qorqut table version v2"], ["c1", "Qorqut table version v1"]]
    after["git"]["operations"]["table_content"] = [
        {"commit": "c2", "subject": "Qorqut table version v2", "parents": ["c1"], "files": {"t/a.json": file_entry("S2", "D2")}},
        {"commit": "c1", "subject": "Qorqut table version v1", "parents": [], "files": {"t/a.json": file_entry("S1", "D1")}}]
    return before, after


def context_pair():
    """S4b: the event ev1 is published as HEAD H1 at the checkpoint; recovery records its completion."""
    log = [["H1", "Context event ev1: add"], ["H0", "Initialize context vocabulary"]]
    filings = {"f1": {"input_id": "i1", "job_id": "j1", "source_version_id": "sv1"}}
    saved = {"inputs": {"i1": {"event_id": "ev1"}}, "attempts": [{"input_id": "i1", "attempt": 1, "context_event_id": "ev1"}],
             "filings": filings, "completions": []}
    completion = {"input_id": "i1", "filing_id": "f1", "job_id": "j1", "source_version_id": "sv1", "context_event_id": "ev1",
                  "published_commit": "H1", "outcome": "published", "state": "applied"}
    git = {"operations": {"refs": {}, "table_history": [], "table_content": []},
           "context": {"refs": {"refs/heads/main": "H1"}, "commits": 2, "log": log, "head": "H1", "tree": "T1"}}
    before = facts(context=saved, git=git, state={"context_inputs": {"i1": {"state": "processing"}}})
    after = facts(context={**saved, "completions": [completion]}, git=json.loads(json.dumps(git)), state={
        "context_inputs": {"i1": {"state": "published"}}, "context_completions": {"c1": {"state": "applied"}},
        "jobs": {"j1": {"state": "succeeded", "kind": "context.file"}}})
    return before, after


EFFECTS = {"model_calls": [], "model_propagations": 0}
RECOVERY = [{"step": "context-recover", "result": {"recovered": True, "context_event_id": "ev1", "published_commit": "H1"}}]


def clone(value):
    return json.loads(json.dumps(value))


def comparable(case, before, after, steps=()):
    return resumption.comparable_outcome(case, before, after, {**EFFECTS}, list(steps), [], None, cases.Checks())


def test_s3_accepts_matching_publication_and_rejects_altered_content():
    case = cases.CASES["S3a"]
    before, after = table_pair()
    assert case.expectations("none", before, after, EFFECTS, []).failed == []
    different = clone(after)
    different["git"]["operations"]["table_content"][0]["files"]["t/a.json"]["json_digest"] = "OTHER"
    assert "every saved snapshot is the content its commit publishes at the version path" in case.expectations("none", before, different, EFFECTS, []).failed
    # the same subjects, count and order but other bytes are a different outcome in the comparison itself
    reformatted = clone(after)
    reformatted["git"]["operations"]["table_content"][0]["files"]["t/a.json"]["sha256"] = "OTHER-BYTES"
    assert comparable(case, before, after)["exact"] != comparable(case, before, reformatted)["exact"]
    assert comparable(case, before, after) == comparable(case, before, clone(after))
    assert resumption.support.digest(comparable(case, before, after)) != resumption.support.digest(comparable(case, before, reformatted))


def test_s3b_rejects_a_changed_existing_commit_and_a_second_commit():
    case = cases.CASES["S3b"]
    before, after = table_pair()
    before["state"]["table_versions"]["v1"] = {"sequence": 1, "git_commit": "c1", "snapshot_retained": False}
    before["state"]["table_versions"]["v2"]["git_commit"] = None
    before["table_snapshots"] = {"v2": before["table_snapshots"]["v2"]}
    before["git"] = clone(after["git"])
    before["state"]["jobs"] = {"j1": {"state": "succeeded", "kind": "tables.version"}, "j2": {"state": "running", "kind": "tables.version"}}
    assert case.expectations("none", before, after, EFFECTS, []).failed == []
    rewritten = clone(after)
    rewritten["git"]["operations"]["table_content"][1]["files"]["t/a.json"]["sha256"] = "REWRITTEN"
    assert "commits already on the branch keep their exact tree content and parents" in case.expectations("none", before, rewritten, EFFECTS, []).failed
    republished = clone(after)
    republished["git"]["operations"]["table_history"].insert(0, ["c3", "Qorqut table version v2"])
    republished["state"]["table_versions"]["v2"]["git_commit"] = "c3"
    assert case.expectations("none", before, republished, EFFECTS, []).failed


def test_s4b_binds_the_completion_to_the_original_event_and_publication_commit():
    case = cases.CASES["S4b"]
    for variant in ("none", "revoked"):
        before, after = context_pair()
        assert case.expectations(variant, before, after, EFFECTS, RECOVERY).failed == [], variant
    before, after = context_pair()
    names = {
        "event": "the completion binds the original input, filing, job and source version to the original event and its actual publication commit",
        "head": "Context HEAD, refs, tree and history are unchanged (no second event)",
        "saved": "the input keeps its saved event id and no attempt was added or rebound",
        "step": "the recovery step reports that same event and publication commit as recovered"}

    other_event = clone(after)
    other_event["context"]["completions"][0]["context_event_id"] = "ev2"
    assert names["event"] in case.expectations("revoked", before, other_event, EFFECTS, RECOVERY).failed
    other_commit = clone(after)
    other_commit["context"]["completions"][0]["published_commit"] = "H2"
    assert names["event"] in case.expectations("none", before, other_commit, EFFECTS, RECOVERY).failed
    other_job = clone(after)
    other_job["context"]["completions"][0]["job_id"] = "j2"
    assert names["event"] in case.expectations("none", before, other_job, EFFECTS, RECOVERY).failed
    second_event = clone(after)
    second_event["git"]["context"].update(head="H2", commits=3, log=[["H2", "Context event ev2: add"], *after["git"]["context"]["log"]])
    assert names["head"] in case.expectations("none", before, second_event, EFFECTS, RECOVERY).failed
    new_event_id = clone(after)
    new_event_id["context"]["inputs"]["i1"]["event_id"] = "ev2"
    assert names["saved"] in case.expectations("none", before, new_event_id, EFFECTS, RECOVERY).failed
    wrong_step = [{"step": "context-recover", "result": {"recovered": True, "context_event_id": "ev2", "published_commit": "H1"}}]
    assert names["step"] in case.expectations("none", before, after, EFFECTS, wrong_step).failed

    # the comparison keeps the saved ids and commits exactly (S4a-style scrubbing does not apply to already-published work)
    exact = comparable(case, before, after)["exact"]["context"]
    assert exact["head"] == "H1" and exact["completions"][0]["context_event_id"] == "ev1" and exact["inputs"]["i1"]["event_id"] == "ev1"
    assert comparable(case, before, after)["exact"] != comparable(case, before, other_event)["exact"]
    assert comparable(case, before, after)["exact"] != comparable(case, before, other_commit)["exact"]
    assert comparable(case, before, after)["exact"] != comparable(case, before, second_event)["exact"]
