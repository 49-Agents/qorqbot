"""The ``create_directory_tree`` file effect: private staging, an exact identity proof, no-replace publication.

Real: the extracted engine (``FileApplier``, receipts, claims, ``PreparedFileExecutor``, ``Application``) over temporary
directories and a synthetic domain. Faults are raised from the applier's ``after_phase`` hook at each durable boundary; the
hook raises a ``BaseException`` so the executor's own failure handling does not run, as for a crashed process."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Event

import pytest

from qorqut_operations.application import Application, OperationDescriptor, OperationRegistry
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, InvocationOptions, PreparedChange,
                                         PreviewResource, ResourceRef, StructuredPreview, FieldChange, new_id)
from qorqut_operations.storage import (ApplyReceipts, DIRECTORY_ACTION, FileApplier, LockPaths, RegisteredDirectoryTarget,
                                       directory_values, layout_hash, observe_directory)
from qorqut_operations.storage.directories import proof_from
from qorqut_operations.storage.directory_layout import checked_values
from qorqut_operations.storage.executor import PreparedFileExecutor
from support import HUMAN, FixedPolicy, database_at

LAYOUT = ("code", "hq")
TYPE = "project_directories"


class Crash(BaseException):
    """A simulated process crash: nothing in the executor's failure handling runs for it."""


class Place:
    """One synthetic registered root, its private staging area and a prepared directory plan."""

    def __init__(self, tmp_path, slug="alpha", layout=LAYOUT, *, database=None, staging=None, receipt=True):
        self.tmp = Path(tmp_path)
        self.database = database or database_at(tmp_path)
        self.root = self.tmp / "operations"
        self.root.mkdir(exist_ok=True)
        self.staging = staging or self.root / ".runtime" / "staging"
        self.slug, self.layout = slug, layout
        self.relative = f"projects/{slug}"
        self.path = self.root / self.relative
        self.resource = ResourceRef(TYPE, slug, None, "global")
        self.locks = LockPaths.under(self.tmp / "locks")
        label = PreviewResource(TYPE, slug, slug)
        self.plan = ChangeSet("projects_make", 1, HUMAN, "global", {"slug": slug}, {"kind": "global"},
            (ExpectedTarget(self.resource, exists=False),),
            (PreparedChange("file", self.resource, DIRECTORY_ACTION, directory_values(layout)),),
            ChangePreview("Make project folder", "", {"kind": "global"}, (label,),
                          (StructuredPreview(label, (FieldChange("directories", None, list(layout)),)),)),
            policy_binding={"profile_revision": None, "policy_revision": None,
                            "operations": {"projects_make": {"mode": "allow", "provenance": "test", "binding": {}}}})
        if receipt:
            with self.database.transaction() as uow:
                self.receipt = ApplyReceipts(uow).create(self.plan)["id"]

    def target(self, resource=None):
        return RegisteredDirectoryTarget(self.root, self.relative, self.staging)

    def applier(self, **hooks):
        return FileApplier(self.database, self.target, locks=self.locks, **hooks)

    def crash_at(self, phase):
        def hook(resource, reached):
            if reached == phase:
                raise Crash(phase)
        return self.applier(after_phase=hook)

    def progress(self):
        with self.database.read() as uow:
            return ApplyReceipts(uow).get(self.receipt)["progress_json"].get(ApplyReceipts.progress_key(self.resource), {})

    def receipt_row(self):
        with self.database.read() as uow:
            return ApplyReceipts(uow).get(self.receipt)

    def claims(self):
        with self.database.read() as uow:
            return uow.repository("resource_claims").list()

    def staged_names(self):
        return sorted(os.listdir(self.staging)) if self.staging.is_dir() else []

    def tree(self):
        return sorted(str(item.relative_to(self.path)) for item in self.path.rglob("*")) if self.path.is_dir() else None

    def finish(self):
        with self.database.transaction() as uow:
            return ApplyReceipts(uow).finish(self.receipt, {"slug": self.slug})


def inode(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino


# -- the effect, its values and its proof ----------------------------------------------------------------------------------

def test_the_directory_effect_creates_exactly_the_empty_layout_and_records_its_identity(tmp_path):
    place = Place(tmp_path)
    assert place.plan.changes[0].values["before_hash"] is None
    assert place.plan.changes[0].values["after_hash"] == layout_hash(LAYOUT)
    phases = []
    place.applier(after_phase=lambda resource, phase: phases.append(phase)).apply(place.receipt)
    assert phases == ["intent", "built", "staged", "renamed", "published"]
    assert place.tree() == ["code", "hq"] and all(not any(p.iterdir()) for p in (place.path / "code", place.path / "hq"))
    assert place.staged_names() == []                                    # the staging area holds nothing afterwards
    progress = place.progress()
    assert progress["phase"] == "published" and progress["verified"] is True and progress["after_hash"] == layout_hash(LAYOUT)
    assert [item["path"] for item in progress["identity"]] == [".", "code", "hq"]
    assert (progress["identity"][0]["dev"], progress["identity"][0]["ino"]) == inode(place.path)
    assert observe_directory(place.target()) == layout_hash(LAYOUT)       # the hash describes the tree, not who made it
    assert oct(place.path.stat().st_mode & 0o777) == "0o700"
    # Applying again re-proves the recorded identity and changes nothing.
    place.applier().apply(place.receipt)
    assert inode(place.path) == (progress["identity"][0]["dev"], progress["identity"][0]["ino"])
    place.finish()
    assert place.claims() == [] and place.receipt_row()["state"] == "applied"


def test_a_receipt_cannot_finish_a_directory_without_its_recorded_identity(tmp_path):
    place = Place(tmp_path)
    key = ApplyReceipts.progress_key(place.resource)
    with place.database.transaction() as uow:
        ApplyReceipts(uow).record_progress(place.receipt, key, {"verified": True, "after_hash": layout_hash(LAYOUT)})
        with pytest.raises(DomainError, match="identity proof"):
            ApplyReceipts(uow).finish(place.receipt, {})


@pytest.mark.parametrize("values,message", [
    ({"directories": ["../escape"]}, "safe relative"),
    ({"directories": ["/absolute"]}, "safe relative"),
    ({"directories": ["a/b"]}, "every parent"),
    ({"directories": ["a", "a"]}, "distinct"),
    ({"directories": ["bad name"]}, "safe relative"),
    ({"directories": [f"d{i}" for i in range(65)]}, "bounded"),
    ({"directories": ["a/b/c/d/e/f/g/h/i"] + ["a", "a/b", "a/b/c", "a/b/c/d", "a/b/c/d/e", "a/b/c/d/e/f", "a/b/c/d/e/f/g",
                                              "a/b/c/d/e/f/g/h"]}, "safe relative"),
])
def test_layouts_are_validated_when_prepared_and_when_applied(values, message):
    with pytest.raises(DomainError, match=message):
        directory_values(values["directories"])
    forged = {**directory_values(LAYOUT), "directories": list(values["directories"])}
    with pytest.raises(DomainError):
        checked_values(forged)


def test_forged_directory_values_are_refused_at_application(tmp_path):
    place = Place(tmp_path)
    for forged in ({**directory_values(LAYOUT), "after_hash": "0" * 64}, {**directory_values(LAYOUT), "before_hash": "1" * 64},
                   {**directory_values(LAYOUT), "extra": 1}, {"content_b64": None, "before_hash": None, "after_hash": None}):
        with pytest.raises(DomainError):
            checked_values(forged)
    assert place.tree() is None


def test_a_file_target_for_a_directory_effect_is_refused(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(TypeError, match="directory"):
        FileApplier(place.database, lambda resource: object(), locks=place.locks).apply(place.receipt)


# -- ownership: nothing preexisting is ever adopted -----------------------------------------------------------------------------

def test_an_identical_preexisting_empty_tree_is_not_adopted(tmp_path):
    place = Place(tmp_path)
    (place.path / "code").mkdir(parents=True)
    (place.path / "hq").mkdir()
    before = inode(place.path)
    assert observe_directory(place.target()) == layout_hash(LAYOUT)       # identical, and still not ours
    with pytest.raises(DomainError) as refused:
        place.applier().apply(place.receipt)
    assert refused.value.code == "revision_conflict" and refused.value.details["disposition"] == "destination_occupied"
    assert inode(place.path) == before and place.tree() == ["code", "hq"]
    assert place.staged_names() == []
    # Nothing of this receipt exists, so its claims can be released; the foreign tree is untouched.
    place.applier().release_unchanged(place.receipt, refused.value)
    assert place.claims() == [] and inode(place.path) == before


def test_a_preexisting_directory_with_files_or_links_is_never_touched(tmp_path):
    place = Place(tmp_path)
    place.path.mkdir(parents=True)
    (place.path / "owner.txt").write_text("keep")
    with pytest.raises(DomainError, match="never adopted"):
        place.applier().apply(place.receipt)
    assert (place.path / "owner.txt").read_text() == "keep" and place.staged_names() == []
    assert observe_directory(place.target()) != layout_hash(LAYOUT)


def test_symlinked_parents_and_final_paths_are_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    place = Place(tmp_path)
    (place.root / "projects").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError, match="symbolic link"):
        place.target().resolve()
    with pytest.raises(DomainError):
        place.applier().apply(place.receipt)
    assert list(outside.iterdir()) == []
    (place.root / "projects").unlink()
    (place.root / "projects").mkdir()
    (place.root / "projects" / "alpha").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError, match="symbolic link"):
        place.target().resolve()
    with pytest.raises(DomainError):
        observe_directory(place.target())
    assert list(outside.iterdir()) == []


def test_the_staging_area_must_be_separate_from_the_authored_target(tmp_path):
    place = Place(tmp_path, staging=None)
    for staging in (place.path, place.path / "hq", place.root / "projects"):
        with pytest.raises(DomainError, match="overlaps"):
            RegisteredDirectoryTarget(place.root, place.relative, staging).resolve()


def test_staging_on_another_filesystem_is_refused_before_anything_is_published(tmp_path):
    if not Path("/dev/shm").is_dir() or os.stat("/dev/shm").st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("no second filesystem is available")
    other = Path("/dev/shm") / f"qorqut-staging-{new_id()}"
    try:
        place = Place(tmp_path, staging=other)
        with pytest.raises(DomainError, match="not on the destination filesystem"):
            place.applier().apply(place.receipt)
        assert place.tree() is None and place.staged_names() == [] and not list(other.iterdir())
        place.applier().release_unchanged(place.receipt, DomainError("invalid_configuration", "x"))
        assert place.claims() == []
    finally:
        if other.exists():
            for item in sorted(other.rglob("*"), reverse=True):
                item.rmdir() if item.is_dir() else item.unlink()
            other.rmdir()


# -- every durable boundary -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phase", ["intent", "staged", "renamed", "published"])
def test_a_crash_at_a_provable_boundary_resumes_to_the_same_single_directory(tmp_path, phase):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at(phase).apply(place.receipt)
    published = place.path.is_dir()
    identity = inode(place.path) if published else None
    place.applier().apply(place.receipt)
    assert place.tree() == ["code", "hq"] and place.staged_names() == []
    if published:
        assert inode(place.path) == identity                              # the very directory the crashed run published
    progress = place.progress()
    assert progress["phase"] == "published" and (progress["identity"][0]["dev"], progress["identity"][0]["ino"]) == inode(place.path)
    place.finish()
    assert place.claims() == []


def test_a_crash_after_the_empty_tree_is_built_but_before_its_proof_fails_closed(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at("built").apply(place.receipt)
    name = place.progress()["staging"]
    assert place.progress()["phase"] == "intent" and place.staged_names() == [name] and place.tree() is None
    kept = inode(place.staging / name)
    with pytest.raises(DomainError) as stuck:
        place.applier().apply(place.receipt)
    assert stuck.value.code == "apply_incomplete" and stuck.value.details["disposition"] == "unproven_staging"
    assert stuck.value.details["staging"] == name and stuck.value.details["recovery"]
    # Fail closed: the unproven tree is neither adopted, published nor deleted, and the claims stay.
    assert place.tree() is None and inode(place.staging / name) == kept and place.staged_names() == [name]
    with pytest.raises(DomainError, match="claims must remain"):
        place.applier().release_unchanged(place.receipt, stuck.value)
    assert len(place.claims()) == 1 and place.receipt_row()["state"] == "failed"
    assert place.receipt_row()["error_json"]["details"]["disposition"] == "unproven_staging"


def test_a_crash_before_any_effect_is_released_cleanly(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at("intent").apply(place.receipt)
    place.applier().release_unchanged(place.receipt, DomainError("policy_denied", "Revoked"))
    assert place.claims() == [] and place.tree() is None and place.staged_names() == []


def test_a_proven_unpublished_tree_is_removed_when_the_application_is_released(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at("staged").apply(place.receipt)
    name = place.progress()["staging"]
    assert place.staged_names() == [name]
    place.applier().release_unchanged(place.receipt, DomainError("policy_denied", "Authority revoked"))
    assert place.staged_names() == [] and place.tree() is None and place.claims() == []
    assert place.progress() == {"phase": "abandoned"}


def test_losing_the_publication_race_removes_only_the_proven_tree(tmp_path):
    place = Place(tmp_path)

    def occupy(resource, phase):
        if phase == "staged":                                              # another actor wins the name after our proof
            (place.path / "theirs").mkdir(parents=True)
            (place.path / "theirs" / "keep.txt").write_text("someone else")
    with pytest.raises(DomainError) as lost:
        place.applier(after_phase=occupy).apply(place.receipt)
    assert lost.value.code == "revision_conflict" and lost.value.details["disposition"] == "destination_occupied"
    assert (place.path / "theirs" / "keep.txt").read_text() == "someone else"
    assert place.staged_names() == [] and place.progress() == {"phase": "abandoned"}
    place.applier().release_unchanged(place.receipt, lost.value)
    assert place.claims() == []


def test_a_changed_staging_tree_is_preserved_and_blocks_publication(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at("staged").apply(place.receipt)
    name = place.progress()["staging"]
    (place.staging / name / "hq" / "stray.txt").write_text("not ours")
    with pytest.raises(DomainError) as stuck:
        place.applier().apply(place.receipt)
    assert stuck.value.details["disposition"] == "staging_changed"
    assert (place.staging / name / "hq" / "stray.txt").read_text() == "not ours" and place.tree() is None
    with pytest.raises(DomainError, match="claims must remain"):
        place.applier().release_unchanged(place.receipt, stuck.value)
    assert len(place.claims()) == 1


def test_a_replaced_staging_inode_is_never_published(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at("staged").apply(place.receipt)
    name = place.progress()["staging"]
    for child in ("code", "hq"):
        (place.staging / name / child).rmdir()
    (place.staging / name).rmdir()
    (place.staging / name / "code").mkdir(parents=True)
    (place.staging / name / "hq").mkdir()                                   # the same layout, a different inode
    with pytest.raises(DomainError) as stuck:
        place.applier().apply(place.receipt)
    assert stuck.value.details["disposition"] == "staging_changed" and place.tree() is None


def test_a_vanished_staging_tree_is_not_guessed_about(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at("staged").apply(place.receipt)
    name = place.progress()["staging"]
    for child in ("code", "hq"):
        (place.staging / name / child).rmdir()
    (place.staging / name).rmdir()
    with pytest.raises(DomainError) as stuck:
        place.applier().apply(place.receipt)
    assert stuck.value.details["disposition"] == "staging_missing"
    with pytest.raises(DomainError, match="claims must remain"):
        place.applier().release_unchanged(place.receipt, stuck.value)


def test_a_published_directory_that_was_replaced_or_filled_is_detected(tmp_path):
    place = Place(tmp_path)
    place.applier().apply(place.receipt)
    original = inode(place.path)
    (place.path / "code" / "late.txt").write_text("written after publication")
    with pytest.raises(DomainError) as filled:
        place.applier().apply(place.receipt)
    assert filled.value.code == "revision_conflict" and filled.value.details["disposition"] == "unexpected_contents"
    (place.path / "code" / "late.txt").unlink()
    place.applier().apply(place.receipt)                                     # exact again, so it is recognized again
    os.rename(place.path, place.tmp / "moved-away")
    (place.path / "code").mkdir(parents=True)
    (place.path / "hq").mkdir()                                              # an identical tree with another identity
    assert observe_directory(place.target()) == layout_hash(LAYOUT) and inode(place.path) != original
    with pytest.raises(DomainError) as replaced:
        place.applier().apply(place.receipt)
    assert replaced.value.details["disposition"] == "target_replaced"
    with pytest.raises(DomainError, match="claims must remain"):
        place.applier().release_unchanged(place.receipt, replaced.value)


def test_a_crash_after_publication_with_a_swapped_target_does_not_resume_into_it(tmp_path):
    place = Place(tmp_path)
    with pytest.raises(Crash):
        place.crash_at("renamed").apply(place.receipt)
    os.rename(place.path, place.tmp / "moved-away")
    place.path.mkdir()
    (place.path / "code").mkdir()
    (place.path / "hq").mkdir()
    with pytest.raises(DomainError) as stuck:
        place.applier().apply(place.receipt)
    assert stuck.value.details["disposition"] == "target_replaced"


# -- concurrency ----------------------------------------------------------------------------------------------------------------

def test_the_same_receipt_publishes_once_when_two_workers_race(tmp_path):
    place = Place(tmp_path)
    entered, release = Event(), Event()

    def hold(resource, phase):
        if phase == "staged":
            entered.set()
            assert release.wait(5)
    applier = place.applier(after_phase=hold)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(applier.apply, place.receipt)
        assert entered.wait(5)
        second = pool.submit(applier.apply, place.receipt)
        release.set()
        assert first.result(timeout=10) == second.result(timeout=10)
    assert place.tree() == ["code", "hq"] and place.staged_names() == []


def test_a_second_application_for_the_same_target_is_blocked_by_the_claim(tmp_path):
    first = Place(tmp_path)
    with first.database.transaction() as uow:
        ApplyReceipts(uow).claim_resources(first.receipt)
    other_plan = ChangeSet("projects_make", 1, HUMAN, "global", {"slug": "alpha", "again": True}, {"kind": "global"},
        first.plan.expected_targets, first.plan.changes, first.plan.preview, policy_binding=first.plan.policy_binding)
    with first.database.transaction() as uow:
        second = ApplyReceipts(uow).create(other_plan)["id"]
    with pytest.raises(DomainError) as blocked:
        first.applier().apply(second)
    assert blocked.value.code == "busy" and first.tree() is None and first.staged_names() == []


# -- through the Application and the prepared-file executor ------------------------------------------------------------------------

def application(place, *, fail_after=None):
    registry = OperationRegistry()
    resource, slug = place.resource, place.slug

    def prepare(ctx, args, options, uow):
        label = PreviewResource(TYPE, args["slug"], args["slug"])
        return ChangeSet("projects_make", 1, ctx.principal_id, "global", args, {"kind": "global"},
            (ExpectedTarget(resource, exists=False),),
            (PreparedChange("file", resource, DIRECTORY_ACTION, directory_values(LAYOUT)),),
            ChangePreview("Make project folder", "", {"kind": "global"}, (label,),
                          (StructuredPreview(label, (FieldChange("directories", None, list(LAYOUT)),)),)))

    def apply(ctx, plan, uow):
        uow.connection.execute("INSERT INTO demo_history(kind, detail) VALUES ('project', ?)", (plan.arguments["slug"],))
        return {"slug": plan.arguments["slug"], "path": place.relative}
    registry.register(OperationDescriptor("projects_make", {"type": "object", "required": ["slug"],
                                                             "properties": {"slug": {"type": "string"}}},
                                          effect_kind="files", prepare=prepare, apply=apply, scope_kinds=frozenset({"global"}),
                                          global_capability="coordinate_businesses"))
    from qorqut_operations.contracts import InvocationContext, Principal
    caller = InvocationContext(Principal(HUMAN, "human"), source="dashboard", global_capabilities=frozenset({"coordinate_businesses"}))
    applier = place.applier(after_phase=fail_after)
    app = Application(place.database, registry, policy=FixedPolicy(),
                      file_executor=PreparedFileExecutor(place.database, applier, context_loader=lambda plan, uow: caller))
    app.register_target_resolver(TYPE, lambda ctx, ref, uow: {"content_hash": observe_directory(place.target())}
                                 if observe_directory(place.target()) else None)
    return app, applier


def test_the_executor_creates_the_directory_and_its_record_in_one_finalizing_transaction(tmp_path):
    from qorqut_operations.contracts import InvocationContext, Principal
    place = Place(tmp_path, receipt=False)
    app, applier = application(place)
    ctx = InvocationContext(Principal(HUMAN, "human"), source="dashboard", global_capabilities=frozenset({"coordinate_businesses"}))
    options = {"idempotency_key": "key-1"}
    result = app.invoke(ctx, "projects_make", {"slug": "alpha"}, options)
    assert result.status == "ok", result.error
    assert place.tree() == ["code", "hq"] and place.staged_names() == [] and place.claims() == []
    with place.database.read() as uow:
        states = [row[0] for row in uow.connection.execute("SELECT state FROM apply_receipts WHERE operation_name='projects_make' "
                                                          "AND signoff_id IS NULL")]
    assert states[-1] == "applied" and states.count("applied") == 1
    identity = inode(place.path)
    # The same key replays the saved result and a changed input under it conflicts; neither touches the directory.
    assert app.invoke(ctx, "projects_make", {"slug": "alpha"}, options).data == result.data
    assert app.invoke(ctx, "projects_make", {"slug": "other"}, options).error["code"] == "idempotency_conflict"
    assert inode(place.path) == identity
    # Another key for the same, now occupied, slug is a conflict that adopts nothing.
    again = app.invoke(ctx, "projects_make", {"slug": "alpha"}, {"idempotency_key": "key-2"})
    assert again.status == "error" and again.error["code"] == "revision_conflict" and inode(place.path) == identity


@pytest.mark.parametrize("phase", ["staged", "renamed", "published"])
def test_a_crash_before_the_final_transaction_resumes_exactly_once_through_recovery(tmp_path, phase):
    from qorqut_operations.contracts import InvocationContext, Principal
    place = Place(tmp_path, receipt=False)

    def crash(resource, reached):
        if reached == phase:
            raise Crash(reached)
    app, applier = application(place, fail_after=crash)
    ctx = InvocationContext(Principal(HUMAN, "human"), source="dashboard", global_capabilities=frozenset({"coordinate_businesses"}))
    with pytest.raises(Crash):
        app.invoke(ctx, "projects_make", {"slug": "alpha"}, {"idempotency_key": "key-1"})
    with place.database.read() as uow:
        row = uow.connection.execute("SELECT id, state FROM apply_receipts WHERE operation_name='projects_make'").fetchone()
    assert row["state"] == "applying" and len(place.claims()) == 1
    applier.after_phase = None
    results = app.file_executor.recover_pending(app)
    assert [item.status for item in results] == ["ok"], [item.error for item in results]
    assert place.tree() == ["code", "hq"] and place.claims() == [] and place.staged_names() == []
    with place.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM demo_history WHERE kind='project'").fetchone()[0] == 1
    # The original request key now replays the one saved result.
    assert app.invoke(ctx, "projects_make", {"slug": "alpha"}, {"idempotency_key": "key-1"}).status == "ok"
    with place.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM demo_history WHERE kind='project'").fetchone()[0] == 1


def test_recovery_of_an_unprovable_staging_fails_closed_and_keeps_the_claims(tmp_path):
    from qorqut_operations.contracts import InvocationContext, Principal
    place = Place(tmp_path, receipt=False)

    def crash(resource, reached):
        if reached == "built":
            raise Crash(reached)
    app, applier = application(place, fail_after=crash)
    ctx = InvocationContext(Principal(HUMAN, "human"), source="dashboard", global_capabilities=frozenset({"coordinate_businesses"}))
    with pytest.raises(Crash):
        app.invoke(ctx, "projects_make", {"slug": "alpha"}, {"idempotency_key": "key-1"})
    applier.after_phase = None
    results = app.file_executor.recover_pending(app)
    assert [item.status for item in results] == ["error"] and results[0].error["details"]["disposition"] == "unproven_staging"
    assert place.tree() is None and len(place.claims()) == 1 and len(place.staged_names()) == 1
    with place.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM demo_history WHERE kind='project'").fetchone()[0] == 0


def test_the_domain_record_is_not_committed_when_the_published_directory_changed(tmp_path):
    from qorqut_operations.contracts import InvocationContext, Principal
    place = Place(tmp_path, receipt=False)

    def tamper(resource, reached):
        if reached == "published":                                             # after the proof, before the final transaction
            (place.path / "hq" / "intruder.txt").write_text("x")
    app, applier = application(place, fail_after=tamper)
    ctx = InvocationContext(Principal(HUMAN, "human"), source="dashboard", global_capabilities=frozenset({"coordinate_businesses"}))
    result = app.invoke(ctx, "projects_make", {"slug": "alpha"}, {"idempotency_key": "key-1"})
    assert result.status == "error" and result.error["code"] == "revision_conflict"
    with place.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM demo_history WHERE kind='project'").fetchone()[0] == 0
    assert (place.path / "hq" / "intruder.txt").exists() and len(place.claims()) == 1


def test_proof_helper_refuses_unexpected_entries(tmp_path):
    root = tmp_path / "tree"
    (root / "a").mkdir(parents=True)
    (root / "a" / "file").write_text("x")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(DomainError, match="beyond its prepared layout"):
            proof_from(descriptor)
    finally:
        os.close(descriptor)
