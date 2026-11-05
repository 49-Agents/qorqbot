"""Real updater evidence must not exempt its surrounding files or database from preservation."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.agentic import upgrade
from bazarlyk.bootstrap import learning_removal_evidence as evidence
from bazarlyk.bootstrap import learning_removal_preparation as coordinator
from test_agentic_managed_plan import installed, refresh_export, scan_and_commit, snapshot_instance
from test_layout_intent import legacy


def make_case(tmp_path, mode):
    paths, source = installed.__wrapped__(tmp_path)
    preference = paths.agentic_root / "user-preferences/SKILL.md"
    preference.write_text("Synthetic owner's private preference marker.\n")
    scan_and_commit(paths)
    if mode == "content":
        guidance = source / "qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md"
        guidance.write_bytes(guidance.read_bytes() + b"\nSynthetic supplier update marker.\n")
        refresh_export(source)
    elif mode == "binding":
        manifest = source / "SOURCE-MANIFEST.json"
        value = json.loads(manifest.read_text())
        value["components"][0]["commit"] = "c" * 40
        manifest.write_text(json.dumps(value))
        first = upgrade.prepare_upgrade(paths, source)
        for change in first["changes"]:
            (paths.agentic_root / change["path"]).write_bytes(base64.b64decode(change["content_base64"]))
        scan_and_commit(paths)
    intent = coordinator._managed_intent(paths, "synthetic-owner", {"export": str(source)})
    db_path = paths.agentic_runtime / "state.sqlite3"
    original_db = db_path.read_bytes()
    sha = hashlib.sha256(original_db).hexdigest()
    directory = tmp_path / "attempt"
    blobs = directory / "restore/blobs"
    blobs.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    blobs.parent.chmod(0o700)
    blob = blobs / sha
    blob.write_bytes(original_db)
    blob.chmod(0o600)
    attempt = SimpleNamespace(paths=paths, attempt_directory=directory,
                              baseline_a={"files": {str(db_path): {"exists": True, "sha256": sha}}})
    before = snapshot_instance(paths)
    result = {"result": coordinator._managed(attempt, intent), "rows": [], "files": []}
    after = snapshot_instance(paths)
    result["files"] = [{"path": p, "before": before.get(p), "after": after.get(p)}
                       for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
    evidence._verify_managed(intent, result, None, paths, attempt)
    assert snapshot_instance(paths) == after
    return paths, source, intent, result, attempt


@pytest.fixture(scope="module")
def applied(tmp_path_factory):
    return make_case(tmp_path_factory.mktemp("managed-evidence"), "content")


@pytest.mark.parametrize("mode", ["noop", "binding"])
def test_empty_content_effects_are_verified(tmp_path, mode):
    paths, _, intent, result, attempt = make_case(tmp_path, mode)
    receipt = result["result"]["data"]
    assert not intent["changes"] and receipt["change_id"] is None and receipt["sequence"] is None
    assert receipt["no_change"] == (mode == "noop")
    assert ("proof" in receipt) == (mode == "binding")
    if mode == "noop":
        assert result["files"] == []
    altered = copy.deepcopy(result)
    altered["result"]["data"]["upgrade_commit"] = "f" * 40
    with pytest.raises(DomainError):
        evidence._verify_managed(intent, altered, None, paths, attempt)


@pytest.mark.parametrize("fault", ["cursor", "old_link", "event_actor", "event_path", "event_hash",
                                   "stray", "proof", "preferences", "operation_row", "git_config"])
def test_foreign_effects_cannot_ride_a_real_completion(applied, fault):
    paths, _, original_intent, original_result, attempt = applied
    intent, result = copy.deepcopy(original_intent), copy.deepcopy(original_result)
    receipt = result["result"]["data"]
    restore = None
    if fault in ("cursor", "old_link", "event_actor", "event_path", "event_hash"):
        path = paths.agentic_runtime / "state.sqlite3"
        restore = (path, path.read_bytes())
        db = sqlite3.connect(path)
        try:
            if fault == "cursor":
                db.execute("INSERT INTO consumers VALUES ('foreign',0,'synthetic')")
            elif fault == "old_link":
                db.execute("UPDATE change_files SET commit_id=? WHERE change_id != ?", (receipt["upgrade_commit"], receipt["change_id"]))
            elif fault == "event_actor":
                db.execute("UPDATE changes SET actor_id='foreign' WHERE change_id=?", (receipt["change_id"],))
            elif fault == "event_path":
                db.execute("UPDATE change_files SET path='plugins/foreign' WHERE change_id=?", (receipt["change_id"],))
            else:
                db.execute("UPDATE change_files SET after_sha256=? WHERE change_id=?", ("f" * 64, receipt["change_id"]))
            db.commit()
        finally:
            db.close()
    elif fault in ("stray", "git_config"):
        path = Path(intent["history"]["directory"]) / "foreign.txt" if fault == "stray" else paths.agentic_root / ".git/config"
        restore = (path, path.read_bytes() if path.exists() else None)
        path.write_bytes((restore[1] or b"") + b"\n# synthetic unrelated change\n")
        result["files"].append({"path": str(path), "before": hashlib.sha256(restore[1]).hexdigest() if restore[1] else None,
                                "after": hashlib.sha256(path.read_bytes()).hexdigest()})
    elif fault == "proof":
        receipt["proof"]["intent"]["sha256"] = "f" * 64
    elif fault == "preferences":
        intent["binding"]["preferences_seed_sha256"] = "f" * 64
    else:
        result["rows"] = [{"table": "principals", "keys": ["foreign"]}]
    try:
        with pytest.raises(DomainError):
            evidence._verify_managed(intent, result, None, paths, attempt)
    finally:
        if restore:
            if restore[1] is None:
                restore[0].unlink()
            else:
                restore[0].write_bytes(restore[1])


def test_compact_reference_preserves_identity_without_contents(applied):
    paths, source, intent, result, _ = applied
    raw = json.dumps(intent).encode()
    assert b"content_base64" not in raw and b"private preference marker" not in raw
    assert b"supplier update marker" not in raw
    assert b"private preference marker" not in json.dumps(result).encode()
    assert len(raw) < 128 * 1024
    proof = json.loads(Path(result["result"]["data"]["proof"]["intent"]["path"]).read_bytes())
    plan = proof["plan"]
    coordinator.verify_managed_projection(paths, plan, intent)
    plan["grants"] = {"changed_informational_report": True}
    coordinator.verify_managed_projection(paths, plan, intent)
    altered = copy.deepcopy(intent)
    altered["arguments"]["plan_ref"]["plan_digest"] = "f" * 64
    with pytest.raises(DomainError):
        coordinator.verify_managed_projection(paths, plan, altered)


def test_layout_is_a_separate_prerequisite(legacy):
    paths, source = legacy
    before = snapshot_instance(paths)
    with pytest.raises(DomainError, match="layout relocation is a separate prerequisite"):
        coordinator._managed_intent(paths, "synthetic-owner", {"export": str(source)})
    assert snapshot_instance(paths) == before
