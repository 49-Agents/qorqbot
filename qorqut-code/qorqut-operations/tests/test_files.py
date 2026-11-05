from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Event

import pytest

from qorqut_operations.application import Application, OperationDescriptor, OperationRegistry
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, PreparedChange,
    PreviewResource, ResourceRef, TextPreview, new_id)
from qorqut_operations.storage import ApplyReceipts, FileApplier, LockPaths, RegisteredFileTarget, file_values
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from qorqut_operations.storage.executor import PreparedFileExecutor, application_claim
from qorqut_operations.storage.files import file_hash
from support import BUSINESS, HUMAN, FixedPolicy, context, database_at


def locks_for(tmp_path):
    return LockPaths.under(tmp_path / "locks")


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
            uow.repository("documents").insert({"id": identity, "kind": "plan", "path": "PLAN.md",
                                                "content_hash": hashlib.sha256(before).hexdigest()})
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


def applier(database, resolver, tmp_path, **kwargs):
    return FileApplier(database, resolver, locks=locks_for(tmp_path), **kwargs)


def test_file_effects_and_metadata_commit_are_separate_and_recoverable(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    file_applier = applier(database, resolver, tmp_path)
    file_applier.apply(receipt["id"])
    assert path.read_bytes() == b"After\n"
    with database.read(business_id=BUSINESS) as uow:
        document = uow.repository("documents").get(plan.changes[0].resource.resource_id)
        assert document["revision"] == 1 and document["availability"] == "applying"
        assert document["content_hash"] == hashlib.sha256(b"Before\n").hexdigest()
    with database.transaction(business_id=BUSINESS) as uow:
        uow.repository("documents").update(document["id"], {"content_hash": file_hash(path)}, expected_revision=1)
        ApplyReceipts(uow).finish(receipt["id"], {"document_id": document["id"], "revision": 2})
    assert file_applier.apply(receipt["id"])
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("documents").get(document["id"])["availability"] == "ready"
        assert uow.repository("resource_claims").list() == []
        assert ApplyReceipts(uow).get(receipt["id"])["state"] == "applied"
    # The domain's receipt history ran in the finalising transaction, with the receipt it belongs to.
    assert database.demo.history.captured == [receipt["id"]]
    assert [event[0] for event in database.demo.lifecycle.events] == ["applying", "ready"]


class Interrupted(BaseException):
    pass


def test_crash_after_replace_recovers_exact_after_without_replaying_write(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    def crash(_):
        raise Interrupted()
    with pytest.raises(Interrupted):
        applier(database, resolver, tmp_path, after_replace=crash).apply(receipt["id"])
    inode = path.stat().st_ino
    with database.read() as uow:
        row = ApplyReceipts(uow).get(receipt["id"])
        assert row["state"] == "applying" and row["progress_json"] == {}
    applier(database, resolver, tmp_path, after_replace=crash).apply(receipt["id"])
    assert path.stat().st_ino == inode
    with database.read() as uow:
        assert ApplyReceipts(uow).get(receipt["id"])["progress_json"]


def test_unexpected_file_bytes_are_preserved_and_claims_remain(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    path.write_bytes(b"Owner changed this outside the operation\n")
    with pytest.raises(DomainError, match="changed outside"):
        applier(database, resolver, tmp_path).apply(receipt["id"])
    assert path.read_bytes() == b"Owner changed this outside the operation\n"
    with database.read(business_id=BUSINESS) as uow:
        assert ApplyReceipts(uow).get(receipt["id"])["state"] == "conflicted"
        assert len(uow.repository("resource_claims").list()) == 1
        # The conflict reaches the domain's own availability bookkeeping.
        assert uow.repository("documents").get(plan.changes[0].resource.resource_id)["availability"] == "conflicted"


def test_second_receipt_cannot_enter_claimed_resource(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    with database.transaction() as uow:
        ApplyReceipts(uow).claim_resources(receipt["id"])
        second = ApplyReceipts(uow).create(plan)
    with pytest.raises(DomainError) as blocked:
        applier(database, resolver, tmp_path).apply(second["id"])
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
    file_applier = applier(database, resolver, tmp_path, after_replace=after_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(file_applier.apply, receipt["id"])
        assert entered.wait(5)
        second = pool.submit(file_applier.apply, receipt["id"])
        release.set()
        assert first.result(timeout=10) == second.result(timeout=10)
    assert len(replacements) == 1


@pytest.mark.parametrize("before,after", [(None, b"New artifact bytes"), (b"Remove me", None), (b"", b"")])
def test_generic_registered_files_support_create_delete_and_empty_content(tmp_path, before, after):
    database, plan, receipt, path, resolver = fixture(tmp_path, before=before, after=after, resource_type="artifact_files")
    applier(database, resolver, tmp_path).apply(receipt["id"])
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
    file_applier = applier(database, resolver, tmp_path)
    with pytest.raises(DomainError, match="file effects"):
        file_applier.release_unchanged(receipt["id"], DomainError("policy_denied", "Revoked"))
    path.write_bytes(b"Before\n")
    file_applier.release_unchanged(receipt["id"], DomainError("policy_denied", "Revoked"))
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("resource_claims").list() == []
        assert ApplyReceipts(uow).get(receipt["id"])["state"] == "failed"
        assert uow.repository("documents").get(plan.changes[0].resource.resource_id)["availability"] == "ready"


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
    assert database.demo.history.captured == []  # nothing is recorded for an unverified receipt


def test_a_failing_domain_history_keeps_the_receipt_unfinished(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    applier(database, resolver, tmp_path).apply(receipt["id"])
    def failing(uow, receipt_row, plan_value):
        raise RuntimeError("history unavailable")
    database.demo.history.capture_receipt = failing
    with pytest.raises(RuntimeError, match="history"):
        with database.transaction() as uow:
            ApplyReceipts(uow).finish(receipt["id"], {})
    with database.read(business_id=BUSINESS) as uow:
        assert ApplyReceipts(uow).get(receipt["id"])["state"] == "applying"
        assert len(uow.repository("resource_claims").list()) == 1


def application_with_documents(database, plan, path, **options):
    registry = OperationRegistry()
    def apply(ctx, prepared, uow):
        document = uow.repository("documents", business_id=BUSINESS).update(prepared.changes[0].resource.resource_id,
            {"content_hash": prepared.changes[0].values["after_hash"]}, expected_revision=1)
        return {"document": document}
    registry.register(OperationDescriptor("plans_set", {"type": "object"}, effect_kind="files", prepare=lambda *_: plan,
                                          apply=apply))
    return Application(database, registry, policy=FixedPolicy(), **options)


def test_application_file_preflight_and_finalize_use_same_revision_contract(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    app = application_with_documents(database, plan, path)
    with database.read() as uow:
        app.validate_prepared(context(), plan, uow, require_approval=False)
    with pytest.raises(DomainError, match="verified"):
        with database.transaction() as uow:
            app.apply_prepared(context(), plan, uow, receipt_id=receipt["id"])
    applier(database, resolver, tmp_path).apply(receipt["id"])
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
    with pytest.raises(DomainError, match="durable application receipt"):
        with database.read() as uow:
            app.check_targets(context(), plan, uow, allow_unverified_files=True)


def test_prepared_file_receipt_end_to_end_through_the_application_and_executor(tmp_path):
    """The applicable prepared-file case: invocation -> exact receipt -> claims -> file bytes -> one finalising
    transaction (domain row, idempotency receipt, receipt finish, domain history)."""
    database, plan, _, path, resolver = fixture(tmp_path)
    file_applier = applier(database, resolver, tmp_path)
    executor = PreparedFileExecutor(database, file_applier)
    registry = OperationRegistry()
    def prepare(ctx, args, options, uow):
        document = uow.repository("documents", business_id=ctx.business_id).get(args["document_id"], required=True)
        content = args["content"].encode()
        resource = ResourceRef("documents", document["id"], ctx.business_id)
        label = PreviewResource("documents", document["id"], "Plan")
        return ChangeSet("docs_set", 1, ctx.principal_id, "business:" + ctx.business_id, args,
            {"kind": "business", "business_id": ctx.business_id},
            (ExpectedTarget(resource, revision=document["revision"], content_hash=file_hash(path), exists=True),),
            (PreparedChange("file", resource, "write", file_values(content, before_hash=file_hash(path))),),
            ChangePreview("Update", "", {"kind": "business", "business_id": ctx.business_id}, (label,),
                (TextPreview(label, "", args["content"], "exact"),)))
    def apply(ctx, prepared, uow):
        change = prepared.changes[0]
        document = uow.repository("documents", business_id=BUSINESS).update(change.resource.resource_id,
            {"content_hash": change.values["after_hash"]}, expected_revision=1)
        return {"document": document}
    schema = {"type": "object", "required": ["document_id", "content"],
              "properties": {"document_id": {"type": "string"}, "content": {"type": "string"}}}
    registry.register(OperationDescriptor("docs_set", schema, effect_kind="files", prepare=prepare, apply=apply))
    app = Application(database, registry, policy=FixedPolicy(), file_executor=executor)
    document_id = plan.changes[0].resource.resource_id
    arguments, options = {"document_id": document_id, "content": "Executed\n"}, {"idempotency_key": "run-1"}
    result = app.invoke(context(), "docs_set", arguments, options)
    assert result.status == "ok" and result.data["document"]["revision"] == 2
    assert path.read_bytes() == b"Executed\n"
    with database.read(business_id=BUSINESS) as uow:
        receipts = uow.connection.execute("SELECT state, payload_hash FROM apply_receipts WHERE signoff_id IS NULL "
                                          "AND operation_name='docs_set'").fetchall()
        assert [row["state"] for row in receipts] == ["applied"]
        assert uow.repository("resource_claims").list() == []
        assert uow.repository("documents").get(document_id)["availability"] == "ready"
        assert len(uow.repository("idempotency_receipts").list()) == 1
    assert len(database.demo.history.captured) == 1
    # The same key replays the saved result without touching the file again.
    path.write_bytes(b"Owner edit\n")
    again = app.invoke(context(), "docs_set", arguments, options)
    assert again.status == "ok" and again.data == result.data and path.read_bytes() == b"Owner edit\n"
    path.write_bytes(b"Executed\n")
    # Another input under the same key is a conflict, and a protected (non-allow) mode never reaches the executor.
    assert app.invoke(context(), "docs_set", {**arguments, "content": "Other\n"}, options).error["code"] == "idempotency_conflict"
    protected = Application(database, registry, policy=FixedPolicy("signoff"), file_executor=executor)
    assert protected.invoke(context(), "docs_set", arguments, {"idempotency_key": "run-2"}).error["code"] == "operation_unavailable"
    unavailable = Application(database, registry, policy=FixedPolicy(), file_executor=None)
    assert unavailable.invoke(context(), "docs_set", arguments, {"idempotency_key": "run-3"}).error["code"] == "operation_unavailable"


def test_file_work_needs_explicit_lock_paths_and_claims_are_exclusive(tmp_path):
    database, plan, receipt, path, resolver = fixture(tmp_path)
    with pytest.raises(TypeError, match="locks"):
        FileApplier(database, resolver)
    with pytest.raises(TypeError, match="LockPaths"):
        FileApplier(database, resolver, locks=tmp_path)
    with pytest.raises(TypeError, match="LockPaths"):
        with application_claim(tmp_path, receipt["id"]):
            pass
    with pytest.raises(DomainError, match="absolute"):
        LockPaths.under("relative/locks")
    locks = locks_for(tmp_path)
    with application_claim(locks, receipt["id"]):
        with pytest.raises(DomainError) as busy:
            with application_claim(locks, receipt["id"]):
                pass
        assert busy.value.code == "busy"
    with application_claim(locks, receipt["id"]):
        pass
    with pytest.raises(DomainError, match="stable UUID"):
        with application_claim(locks, "../escape"):
            pass


def test_recovery_admission_gates_writers_with_the_explicit_installation_lock(tmp_path):
    lock = InstallationRecoveryLock(tmp_path / "locks" / "recovery.lock")
    with pytest.raises(DomainError, match="absolute"):
        InstallationRecoveryLock("recovery.lock")
    admission = RecoveryAdmission(installation_lock=lock)
    with admission.writer():
        with admission.writer():  # re-entrant for the same thread
            pass
        with pytest.raises(TimeoutError):
            with lock.exclusive(timeout=0):
                pass
    with lock.exclusive(timeout=0):
        with pytest.raises(DomainError) as unavailable:
            with admission.writer():
                pass
        assert unavailable.value.code == "maintenance"
    admission.close_and_wait(timeout=1)
    with pytest.raises(DomainError, match="recovery"):
        with admission.writer():
            pass
    admission.open()
    with admission.writer():
        pass
