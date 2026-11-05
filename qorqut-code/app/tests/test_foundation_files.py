from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from threading import Event

import pytest

from bazarlyk.application import Application, OperationDescriptor, OperationRegistry
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget,
    PreparedChange, PreviewResource, ResourceRef, TextPreview, new_id)
from bazarlyk.storage import ApplyReceipts, FileApplier, RegisteredFileTarget, file_values
from bazarlyk.storage.files import file_hash
from foundation_support import BUSINESS, HUMAN, FixedPolicy, context, database_at


def fixture(tmp_path, *, before=b"Before\n", after=b"After\n", resource_type="documents"):
    database = database_at(tmp_path)
    root = tmp_path / "authored"
    root.mkdir()
    path = root / "PLAN.md"
    if before is not None:
        path.write_bytes(before)
    identity = new_id()
    if resource_type == "documents":
        with database.transaction(business_id=BUSINESS) as uow:
            uow.repository("documents").insert({"id": identity, "namespace": BUSINESS, "kind": "plan",
                "path": "PLAN.md", "content_hash": hashlib.sha256(before).hexdigest()})
    resource = ResourceRef(resource_type, identity, BUSINESS)
    label = PreviewResource(resource_type, identity, "Plan")
    plan = ChangeSet("plans_set", 1, HUMAN, "business:" + BUSINESS, {}, {"kind": "business", "business_id": BUSINESS},
        (ExpectedTarget(resource, revision=1 if resource_type == "documents" else None,
                        content_hash=file_hash(path), exists=before is not None),),
        (PreparedChange("file", resource, "write" if after is not None else "delete", file_values(after, before_hash=file_hash(path))),),
        ChangePreview("Update plan", "", {"kind": "business", "business_id": BUSINESS}, (label,),
            (TextPreview(label, (before or b"").decode(), (after or b"").decode(), "exact test diff"),)),
        policy_binding={"profile_revision": None, "policy_revision": None,
                        "operations": {"plans_set": {"mode": "allow", "provenance": "test", "binding": {}}}},
        review_business_id=BUSINESS)
    with database.transaction() as uow:
        receipt = ApplyReceipts(uow).create(plan)
    resolver = lambda _: RegisteredFileTarget(root, "PLAN.md")
    return database, plan, receipt, path, resolver


def test_file_effects_and_metadata_commit_are_separate_and_recoverable(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    applier = FileApplier(database, resolver)
    applier.apply(receipt["id"])
    assert path.read_bytes() == b"After\n"
    with database.read(business_id=BUSINESS) as uow:
        document = uow.repository("documents").get(plan.changes[0].resource.resource_id)
        assert document["revision"] == 1 and document["availability"] == "applying"
        assert document["content_hash"] == hashlib.sha256(b"Before\n").hexdigest()
    with database.transaction(business_id=BUSINESS) as uow:
        uow.repository("documents").update(document["id"], {"content_hash": file_hash(path)}, expected_revision=1)
        ApplyReceipts(uow).finish(receipt["id"], {"document_id": document["id"], "revision": 2})
    assert applier.apply(receipt["id"])
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("documents").get(document["id"])["availability"] == "ready"
        assert uow.repository("resource_claims").list() == []
        assert ApplyReceipts(uow).get(receipt["id"])["state"] == "applied"


class Interrupted(BaseException):
    pass


def test_crash_after_replace_recovers_exact_after_without_replaying_write(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    def crash(_):
        raise Interrupted()
    with pytest.raises(Interrupted):
        FileApplier(database, resolver, after_replace=crash).apply(receipt["id"])
    inode = path.stat().st_ino
    with database.read() as uow:
        row = ApplyReceipts(uow).get(receipt["id"])
        assert row["state"] == "applying" and row["progress_json"] == {}
    FileApplier(database, resolver, after_replace=crash).apply(receipt["id"])
    assert path.stat().st_ino == inode
    with database.read() as uow:
        assert ApplyReceipts(uow).get(receipt["id"])["progress_json"]


def test_unexpected_file_bytes_are_preserved_and_claims_remain(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    path.write_bytes(b"Owner changed this outside the operation\n")
    with pytest.raises(DomainError, match="changed outside"):
        FileApplier(database, resolver).apply(receipt["id"])
    assert path.read_bytes() == b"Owner changed this outside the operation\n"
    with database.read() as uow:
        assert ApplyReceipts(uow).get(receipt["id"])["state"] == "conflicted"
        assert len(uow.repository("resource_claims").list()) == 1


def test_second_receipt_cannot_enter_claimed_resource(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    with database.transaction() as uow:
        ApplyReceipts(uow).claim_resources(receipt["id"])
        second = ApplyReceipts(uow).create(plan)
    with pytest.raises(DomainError) as blocked:
        FileApplier(database, resolver).apply(second["id"])
    assert blocked.value.code == "busy"
    assert path.read_bytes() == b"Before\n"


def test_same_receipt_serializes_concurrent_file_replacement(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    entered, release = Event(), Event()
    replacements = []
    def after_replace(resource):
        replacements.append(resource)
        entered.set()
        assert release.wait(5)
    applier = FileApplier(database, resolver, after_replace=after_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(applier.apply, receipt["id"])
        assert entered.wait(5)
        second = pool.submit(applier.apply, receipt["id"])
        release.set()
        assert first.result(timeout=10) == second.result(timeout=10)
    assert len(replacements) == 1


@pytest.mark.parametrize("before,after", [(None, b"New artifact bytes"), (b"Remove me", None), (b"", b"")])
def test_generic_registered_files_support_create_delete_and_empty_content(tmp_path, before, after):
    database, plan, receipt, path, resolver = fixture(tmp_path, before=before, after=after, resource_type="artifact_files")
    FileApplier(database, resolver).apply(receipt["id"])
    assert (path.read_bytes() if path.exists() else None) == after


@pytest.mark.parametrize("relative", ["../outside", "/absolute", "folder/../../outside", "bad\\path", "bad\0path", "."])
def test_registered_target_refuses_path_escape(tmp_path, relative):
    with pytest.raises(DomainError):
        RegisteredFileTarget(tmp_path, relative).resolve()


def test_registered_target_refuses_symlink_parents_and_final_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "registered"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError, match="symbolic link"):
        RegisteredFileTarget(root, "linked/document.md").resolve()
    (root / "leaf").symlink_to(outside / "absent")
    with pytest.raises(DomainError, match="symbolic link"):
        RegisteredFileTarget(root, "leaf").resolve()


def test_no_progress_is_not_proof_that_claims_can_be_released(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    with database.transaction() as uow:
        ApplyReceipts(uow).claim_resources(receipt["id"])
    path.write_bytes(b"After\n")
    with pytest.raises(DomainError, match="file effects"):
        FileApplier(database, resolver).release_unchanged(receipt["id"], DomainError("policy_denied", "Revoked"))
    path.write_bytes(b"Before\n")
    FileApplier(database, resolver).release_unchanged(receipt["id"], DomainError("policy_denied", "Revoked"))
    with database.read() as uow:
        assert uow.repository("resource_claims").list() == []
        assert ApplyReceipts(uow).get(receipt["id"])["state"] == "failed"


def test_receipt_binding_is_immutable_and_cannot_finish_unverified(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="unverified"):
            ApplyReceipts(uow).finish(receipt["id"], {})
        with pytest.raises(DomainError, match="Immutable"):
            uow.repository("apply_receipts").update(receipt["id"], {"payload_hash": "0" * 64}, expected_revision=1)
    with pytest.raises(DomainError):
        with database.transaction() as uow:
            uow.connection.execute("UPDATE apply_receipts SET change_set=? WHERE id=?", (b"{}", receipt["id"]))


def test_application_file_preflight_and_finalize_use_same_revision_contract(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    registry = OperationRegistry()
    def apply(ctx, prepared, uow):
        document = uow.repository("documents", business_id=BUSINESS).update(prepared.changes[0].resource.resource_id,
            {"content_hash": prepared.changes[0].values["after_hash"]}, expected_revision=1)
        return {"document": document}
    registry.register(OperationDescriptor("plans_set", {"type": "object"}, effect_kind="files", prepare=lambda *_: plan, apply=apply))
    app = Application(database, registry, policy=FixedPolicy())
    with database.read() as uow:
        app.validate_prepared(context(), plan, uow, require_approval=False)
    with pytest.raises(DomainError, match="verified"):
        with database.transaction() as uow:
            app.apply_prepared(context(), plan, uow, receipt_id=receipt["id"])
    FileApplier(database, resolver).apply(receipt["id"])
    with database.transaction() as uow:
        result = app.apply_prepared(context(), plan, uow, receipt_id=receipt["id"])
        ApplyReceipts(uow).finish(receipt["id"], result.to_dict())
    assert result.data["document"]["revision"] == 2


def test_recovery_preflight_accepts_only_exact_before_or_after(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path, before=None, resource_type="artifact_files")
    registry = OperationRegistry()
    registry.register(OperationDescriptor("plans_set", {"type": "object"}, effect_kind="files", prepare=lambda *_: plan, apply=lambda *_: {}))
    app = Application(database, registry, policy=FixedPolicy())
    app.register_target_resolver("artifact_files", lambda ctx, ref, uow: {"content_hash": file_hash(path)} if path.exists() else None)
    path.write_bytes(b"After\n")
    with database.read() as uow:
        app.validate_prepared(context(), plan, uow, receipt_id=receipt["id"], allow_unverified_files=True)
    path.write_bytes(b"Unexpected\n")
    with pytest.raises(DomainError, match="unexpected"):
        with database.read() as uow:
            app.validate_prepared(context(), plan, uow, receipt_id=receipt["id"], allow_unverified_files=True)
