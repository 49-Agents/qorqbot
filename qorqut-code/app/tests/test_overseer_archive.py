"""All presentation data below is synthetic."""
import os
import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.overseer.archive import PrivateArchive


def test_presentation_is_immutable_private_and_revalidates_access(tmp_path):
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    denied = set()
    def authorize(ref):
        if ref["id"] in denied:
            raise DomainError("forbidden", "Revoked")
    archive = PrivateArchive(root, authorize)
    run = {"type": "run", "id": "run1"}
    source = {"type": "source", "id": "task1"}
    records = [{"speaker": "owner", "text": "synthetic feedback"}, {"speaker": "agent", "text": "synthetic answer"}]
    ref = archive.capture(run, records, [source], "operation-1")
    assert archive.capture(run, records, [source], "operation-1") == ref
    assert archive.get(ref)["records"] == records
    assert all(os.stat(p).st_mode & 0o077 == 0 for p in root.iterdir())
    with pytest.raises(DomainError) as conflict:
        archive.capture(run, [], [source], "operation-1")
    assert conflict.value.code == "operation_conflict"
    denied.add("task1")
    with pytest.raises(DomainError) as refused:
        archive.get(ref)
    assert refused.value.code == "forbidden"
    denied.clear()
    denied.add("run1")
    with pytest.raises(DomainError):
        archive.get(ref)


def test_corruption_and_unsafe_root_are_refused(tmp_path):
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    archive = PrivateArchive(root, lambda ref: None)
    ref = archive.capture({"id": "run"}, [], [], "op")
    (root / (ref["id"] + ".json")).write_text("{}")
    with pytest.raises(DomainError) as error:
        archive.get(ref)
    assert error.value.code == "evidence_unavailable"
    root.chmod(0o755)
    with pytest.raises(DomainError):
        PrivateArchive(root, lambda ref: None)


def test_source_free_retention_uses_saved_run_authority_before_body_read(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    run = {"provider": "qorqut_overseer", "agent_id": "overseer", "conversation_id": "saved-conversation",
           "run_id": None, "namespace": {"kind": "global", "business_id": None}}
    records = [{"speaker": "owner", "text": "synthetic reason"}, {"speaker": "agent", "text": "synthetic reply"}]
    no_gate = PrivateArchive(root, lambda ref: None)
    with pytest.raises(DomainError) as missing:
        no_gate.capture_retention(run, records, [], "op")
    assert missing.value.code == "forbidden"
    enabled, seen = {"capture": False, "read": True}, []
    def retention(saved, *, action):
        seen.append((saved, action))
        if not enabled[action]:
            raise DomainError("forbidden", "revoked")
    archive = PrivateArchive(root, lambda ref: None, retention)
    with pytest.raises(DomainError):
        archive.capture_retention(run, records, [], "op")
    assert not list(root.glob("*.body"))
    enabled["capture"] = True
    ref = archive.capture_retention(run, records, [], "op")
    assert archive.get_retention(ref)["records"] == records
    assert archive.get_retention(ref)["run_ref"]["run_id"] is None
    with pytest.raises(DomainError):
        archive.get(ref)
    forged = {**ref, "run_ref": {**run, "conversation_id": "different"}}
    with pytest.raises(DomainError):
        archive.get_retention(forged)
    enabled["read"] = False
    touched = []
    original = archive._read
    def read(path):
        touched.append(path.suffix)
        return original(path)
    monkeypatch.setattr(archive, "_read", read)
    with pytest.raises(DomainError) as revoked:
        archive.get_retention(ref)
    assert revoked.value.code == "forbidden" and touched == [".json"]
    assert seen[-1] == (run, "read")


def test_retention_rechecks_underlying_sources(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    run = {"provider": "qorqut_overseer", "agent_id": "overseer", "conversation_id": "saved",
           "run_id": "run", "namespace": {"kind": "global", "business_id": None}}
    denied = set()
    def source(ref):
        if ref["id"] in denied:
            raise DomainError("forbidden", "source revoked")
    archive = PrivateArchive(root, source, lambda ref, *, action: None)
    ref = archive.capture_retention(run, [{"text": "synthetic"}], [{"id": "source"}], "op")
    denied.add("source")
    with pytest.raises(DomainError) as revoked:
        archive.get_retention(ref)
    assert revoked.value.code == "forbidden"
