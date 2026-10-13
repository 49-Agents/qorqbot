"""Bounded workspace publication, generated identity projection and shell-ready activation.

Only positively owned stages may be resumed. Published authored files are never
reseeded. The installation effect gate serializes cooperating effects; arbitrary
shell writers are not fenced. After the workspace is published, provisioning
prepares the agent's own recorded Bash shell (D62) and only then activates the
agent. No native CLI/provider is launched here.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from contextlib import contextmanager
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from ..contracts import CoreError, JobProgress, JobResolution, Model, canonical
from ..storage.database import assert_external_io_allowed
from ..storage.events import EventInput, append_event
from ..storage.files import directory
from ..storage.jobs import JobClaim, JobHandler, after
from .models import AgentJob
from .registry import advance, get_row, view

MARKER = ".qorqut-owner.json"
LAYOUT_VERSION = 1
MAX_PROJECTION_BYTES = 16384
INITIAL_STATUS = "Initializing."
SHELL_READY_STATUS = "Shell ready. Start a CLI in Terminal to connect Chat."
DIRECTORIES = (
    "workspace", "workspace/notes", "workspace/research", "workspace/data", "workspace/files",
    "workspace/downloads", "workspace/scratch", "runtime", "history", "history/sources",
    "history/chunks", "history/attachments", "history/context-packets",
)


class Ownership(Model):
    version: Literal[1] = 1
    installation_id: UUID
    agent_id: UUID
    operation_id: UUID
    job_id: UUID
    phase: Literal["preparing", "complete"]


def stage_name(row):
    return ".qorqut-agent-stage-" + str(UUID(row["provisioning_job_id"]))


def rename_noreplace(fd, source, target):
    """Linux atomic no-replace publication, loaded only at an explicit effect."""
    assert_external_io_allowed()
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError:
        raise CoreError("unsupported_capability", "Atomic no-replace publication is unavailable") from None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(fd, os.fsencode(source), fd, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise CoreError("unsupported_capability", "Filesystem lacks atomic no-replace publication")
        raise OSError(error, os.strerror(error))


@contextmanager
def child_directory(fd, name, *, create=False):
    assert_external_io_allowed()
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=fd)
            os.fsync(fd)
        except FileExistsError:
            pass
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        yield child
    finally:
        os.close(child)


def same_child(fd, name, child):
    current, opened = os.stat(name, dir_fd=fd, follow_symlinks=False), os.fstat(child)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise CoreError("target_changed", "Workspace directory changed during effect")


def read_regular(fd, name, limit):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    with os.fdopen(handle, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CoreError("uncertain_effect", "Workspace evidence is not a regular file")
        data = stream.read(limit + 1)
        end = os.fstat(stream.fileno())
        if len(data) > limit:
            raise CoreError("uncertain_effect", "Workspace evidence exceeds its bound")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (end.st_size, end.st_mtime_ns, end.st_ctime_ns):
            raise CoreError("target_changed", "Workspace evidence changed during inspection")
        return data


def regular_or_absent(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise CoreError("uncertain_effect", "Generated target is a link or special file")


def seed(fd, name, data):
    try:
        handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    except FileExistsError:
        # Positively owned, preparing stage: preserve any existing regular bytes.
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(handle).st_mode):
                raise CoreError("uncertain_effect", "Authored seed is not a regular file")
            os.fsync(handle)
        finally:
            os.close(handle)
    else:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    os.fsync(fd)


class Workspaces:
    def __init__(self, db, root):
        self.db, self.root = db, root
        self.jobs = None
        self.terminals = None  # Assigned by Application assembly.

    def handlers(self):
        return (
            JobHandler("agent_provision", AgentJob, True, self.execute, self.inspect),
            JobHandler("agent_project", AgentJob, True, self.execute, self.inspect),
        )

    def _coordinates(self, tx, job, claim=None, *, allow_hold=False):
        current = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        if claim is not None:
            self.jobs._current_claim(tx, claim)
        elif current is None or dict(current) != job:
            raise CoreError("target_changed", "Workspace job changed during inspection")
        installation = tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone()
        held = current["wait_reason"] == "recovery_review" or current["recovery_epoch"] != installation["recovery_epoch"]
        if held and not allow_hold:
            raise CoreError("target_changed", "Workspace effect is held for recovery review")
        args = AgentJob.model_validate_json(current["args_json"])
        row = get_row(tx, args.agent_id)
        if (current["kind"] not in ("agent_provision", "agent_project")
                or current["target_id"] != row["id"] or current["expected_generation"] != args.expected_lifecycle_revision
                or not self._lifecycle(tx, current, row, args)
                or (current["kind"] == "agent_provision" and
                    (current["job_id"] != row["provisioning_job_id"] or current["operation_id"] != row["creation_operation_id"]))):
            raise CoreError("target_changed", "Workspace lifecycle or registration coordinates changed")
        return dict(row), dict(installation), held

    @staticmethod
    def _activated(tx, row, job_id):
        return tx.connection.execute("""SELECT 1 FROM events WHERE kind='agent.lifecycle_changed' AND agent_id=?
            AND json_extract(payload_json,'$.provisioning_job_id')=? LIMIT 1""", (row["id"], job_id)).fetchone() is not None

    def _lifecycle(self, tx, job, row, args):
        expected = args.expected_lifecycle_revision
        if row["lifecycle"] == "provisioning":
            return row["lifecycle_revision"] == expected
        if row["lifecycle"] != "active":
            return False
        # A projection always writes the latest committed row, so one queued before
        # activation stays valid. Provisioning resumes only after its own activation.
        if job["kind"] == "agent_project":
            return row["lifecycle_revision"] >= expected
        return row["lifecycle_revision"] == expected + 1 and self._activated(tx, row, job["job_id"])

    def _snapshot(self, job, claim=None, *, allow_hold=False):
        with self.db.read() as tx:
            return self._coordinates(tx, job, claim, allow_hold=allow_hold)

    @staticmethod
    def _owner(row, installation, phase):
        return Ownership(installation_id=installation["installation_id"], agent_id=row["id"],
            operation_id=row["creation_operation_id"], job_id=row["provisioning_job_id"], phase=phase)

    def _marker(self, fd, row, installation, *, complete=False):
        try:
            marker = Ownership.model_validate_json(read_regular(fd, MARKER, 4096))
        except (FileNotFoundError, ValidationError) as exc:
            raise CoreError("uncertain_effect", "Directory has no valid ownership marker") from exc
        expected = self._owner(row, installation, marker.phase)
        if marker != expected or (complete and marker.phase != "complete"):
            raise CoreError("uncertain_effect", "Directory ownership or completion is not established")
        return marker

    @contextmanager
    def owned(self, row, installation):
        """Verify an already published workspace; never seed or repair it.

        The caller holds the installation effect/use guards and rechecks its
        domain binding outside SQL transactions. The yielded guard detects path
        substitution and reuses the provisioning marker's complete identity.
        """
        assert_external_io_allowed()
        if row["workspace_initialized_at"] is None:
            raise CoreError("not_ready", "Workspace has not been durably initialized")
        with directory(self.root) as root, child_directory(root, "agents") as parent:
            target = str(UUID(row["id"]))
            with child_directory(parent, target) as final:
                def guard():
                    with self.db.read() as tx:
                        current = get_row(tx, row["id"])
                        installed = tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone()
                        for key in ("id", "actor_id", "creation_operation_id", "provisioning_job_id",
                                    "workspace_initialized_at", "lifecycle", "lifecycle_revision"):
                            if current[key] != row[key]:
                                raise CoreError("target_changed", "Registered workspace binding changed")
                        if any(installed[key] != installation[key] for key in ("installation_id", "recovery_epoch")):
                            raise CoreError("target_changed", "Workspace installation binding changed")
                    same_child(root, "agents", parent)
                    same_child(parent, target, final)
                    self._marker(final, row, installation, complete=True)
                    with directory(self.root) as reopened:
                        if (os.fstat(reopened).st_dev, os.fstat(reopened).st_ino) != (os.fstat(root).st_dev, os.fstat(root).st_ino):
                            raise CoreError("target_changed", "Installation directory changed")
                guard()
                yield final, guard
                guard()

    def _atomic_file(self, fd, target, data, *, prefix, guard):
        regular_or_absent(fd, target)
        guard()
        temporary = prefix + str(self.db.id_factory())
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=fd)
        # A crash may retain this private generated orphan. Never scan/delete
        # unrelated entries or use its existence as proof of a completed effect.
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.db.fault("agent_projection_after_stage_fsync" if target == "agent.json" else "agent_marker_after_stage_fsync")
        guard()
        regular_or_absent(fd, target)
        os.replace(temporary, target, src_dir_fd=fd, dst_dir_fd=fd)
        self.db.fault("agent_projection_after_replace" if target == "agent.json" else "agent_marker_after_complete")
        os.fsync(fd)

    def _prepare(self, parent, row, installation, guard, *, resume_only):
        name = stage_name(row)
        guard()
        created = False
        if not resume_only:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                created = True
                self.db.fault("agent_after_stage_mkdir")
                os.fsync(parent)
        with child_directory(parent, name) as fd:
            if created:
                guard()
                seed(fd, MARKER, canonical(self._owner(row, installation, "preparing")).encode("utf-8"))
                self.db.fault("agent_after_preparing_marker")
            marker = self._marker(fd, row, installation)
            if marker.phase == "preparing":
                for path in DIRECTORIES:
                    guard()
                    parts = path.split("/")
                    with child_directory(fd, parts[0], create=True) as first:
                        if len(parts) == 2:
                            with child_directory(first, parts[1], create=True) as second:
                                os.fsync(second)
                        os.fsync(first)
                with child_directory(fd, "workspace") as workspace:
                    guard()
                    seed(workspace, "persona.md", row["persona_seed"].encode("utf-8"))
                    seed(workspace, "memory.md", b"")
                os.fsync(fd)
                self.db.fault("agent_after_seeds_fsync")
                self._atomic_file(fd, MARKER, canonical(self._owner(row, installation, "complete")).encode("utf-8"),
                    prefix=".qorqut-marker-", guard=guard)
            self._marker(fd, row, installation, complete=True)
            os.fsync(fd)
            same_child(parent, name, fd)
        return name

    def _projection(self, row, installation):
        identity = view(row).model_dump(mode="json", include={
            "id", "actor_id", "conversation_id", "display_name", "icon_key", "kind", "workspace_path",
            "cli", "resources", "revision", "lifecycle_revision", "start_requested", "created_by", "created_at"})
        return {"version": 1, "layout_version": LAYOUT_VERSION,
                "installation_id": installation["installation_id"], **identity}

    def _write_projection(self, fd, job, claim, guard):
        row, installation, _ = self._snapshot(job, claim)
        projection = self._projection(row, installation)
        data = (canonical(projection) + "\n").encode("utf-8")
        if len(data) > MAX_PROJECTION_BYTES:
            raise CoreError("storage_blocked", "Generated projection exceeds its bound")
        self._atomic_file(fd, "agent.json", data, prefix=".qorqut-projection-", guard=guard)
        if read_regular(fd, "agent.json", MAX_PROJECTION_BYTES) != data:
            raise CoreError("target_changed", "Generated projection changed before finalization")
        return row["revision"]

    def _finalize(self, job, claim, revision):
        with self.db.transaction() as tx:
            row, installation, _ = self._coordinates(tx, job, claim)
            if row["workspace_initialized_at"] is None:
                now = self.db.clock()
                tx.connection.execute("UPDATE agents SET workspace_initialized_at=? WHERE id=?", (now, row["id"]))
                append_event(tx, EventInput(kind="agent.workspace_initialized", actor_id=row["created_by"],
                    agent_id=row["id"], operation_id=row["creation_operation_id"],
                    payload={"agent_id": row["id"], "provisioning_job_id": row["provisioning_job_id"],
                             "layout_version": LAYOUT_VERSION, "initialized_at": now}))
            tx.connection.execute("UPDATE agents SET projection_revision=? WHERE id=?", (revision, row["id"]))
            if row["revision"] != revision:
                pending = tx.connection.execute("""SELECT 1 FROM jobs WHERE kind='agent_project' AND target_id=?
                    AND job_id<>? AND recovery_epoch=? AND expected_generation=? AND
                    (state IN ('queued','running') OR (state='waiting' AND wait_reason='retry_delay'))""",
                    (row["id"], job["job_id"], installation["recovery_epoch"], row["lifecycle_revision"])).fetchone()
                if pending is None:
                    self.jobs.enqueue(tx, "agent_project", AgentJob(agent_id=row["id"],
                        expected_lifecycle_revision=row["lifecycle_revision"]), operation_id=UUID(job["operation_id"]),
                        target_id=UUID(row["id"]), expected_generation=row["lifecycle_revision"])
            self.db.fault("agent_before_workspace_commit")
        self.db.fault("agent_after_workspace_commit")

    def _dependency(self):
        return JobResolution(state="waiting", reason="dependency_unavailable",
            next_attempt_at=after(self.db.clock(), 60),
            progress=JobProgress(phase="runtime_integration_required", completed=1, total=2),
            error=CoreError("unsupported_capability", "Workspace is initialized; resource/runtime integration is not implemented").error)

    def _shell_wait(self):
        # Explicit inspection time, not an automatic effect retry. The terminal
        # observer settles this job once it verifies the shell.
        return JobResolution(state="waiting", reason="dependency_unavailable",
            next_attempt_at=after(self.db.clock(), 60),
            progress=JobProgress(phase="terminal_not_ready", completed=1, total=2),
            error=CoreError("not_ready", "Workspace is initialized; the recorded shell is not verified").error)

    def _local(self, job, claim, *, inspecting):
        """Prepare (or only observe, when inspecting) this agent's recorded shell, then activate.

        Preparing a verified shell is the only extra effect. A recorded launch is
        never repeated, and nothing here starts a native CLI.
        """
        if self.terminals is None or not self.terminals.enabled:
            return self._dependency()
        agent_id = AgentJob.model_validate_json(job["args_json"]).agent_id
        shell = self.terminals.observe(agent_id, create=not inspecting)
        if shell.status == "disabled":
            return self._dependency()
        if shell.status != "available" or shell.reason != "verified":
            return self._shell_wait()
        self._activate(job, claim, shell)
        # Publish under the new lifecycle coordinates without waiting for the observer.
        self.terminals.observe(agent_id, create=False)
        return JobResolution(state="completed", progress=JobProgress(phase="shell_ready", completed=2, total=2))

    def _activate(self, job, claim, shell):
        with self.db.transaction() as tx:
            row, installation, _ = self._coordinates(tx, job, claim)
            if row["lifecycle"] == "active":
                return  # This job's activation already committed; settle idempotently.
            now, revision = self.db.clock(), advance(row["lifecycle_revision"])
            tx.connection.execute("UPDATE agents SET lifecycle='active',lifecycle_revision=? WHERE id=?",
                                  (revision, row["id"]))
            append_event(tx, EventInput(kind="agent.lifecycle_changed", actor_id=row["created_by"],
                agent_id=row["id"], operation_id=row["creation_operation_id"],
                payload={"agent_id": row["id"], "old_lifecycle": "provisioning", "lifecycle": "active",
                         "old_lifecycle_revision": row["lifecycle_revision"], "lifecycle_revision": revision,
                         "provisioning_job_id": row["provisioning_job_id"], "terminal_id": str(shell.terminal_id),
                         "reason": "recorded_shell_verified", "changed_at": now}))
            if row["status_revision"] == 1 and row["status_text"] == INITIAL_STATUS:
                # Only the untouched seed status; authored status is never replaced.
                status_revision = advance(row["status_revision"])
                tx.connection.execute("""UPDATE agents SET status_text=?,status_revision=?,status_updated_at=?,
                    status_reviewed_at=?,status_actor_id=? WHERE id=?""",
                    (SHELL_READY_STATUS, status_revision, now, now, row["created_by"], row["id"]))
                append_event(tx, EventInput(kind="agent.status_changed", actor_id=row["created_by"],
                    agent_id=row["id"], operation_id=row["creation_operation_id"],
                    payload={"old_text": INITIAL_STATUS, "new_text": SHELL_READY_STATUS,
                             "status_revision": status_revision}))
            self.jobs.enqueue(tx, "agent_project", AgentJob(agent_id=row["id"], expected_lifecycle_revision=revision),
                operation_id=UUID(row["creation_operation_id"]), target_id=UUID(row["id"]),
                expected_generation=revision)
            self.db.fault("agent_before_activation_commit")
        self.db.fault("agent_after_activation_commit")

    def settle(self, agent_id):
        """Complete a provisioning job that waits only on the shell, once the observer verifies it.

        Uses the fixed typed handler's inspection (never a second launch); an
        uncertain or recovery-held job is left for explicit reconciliation.
        """
        with self.db.read() as tx:
            row = get_row(tx, agent_id)
            job = tx.connection.execute("SELECT job_id,state,wait_reason FROM jobs WHERE job_id=?",
                                        (row["provisioning_job_id"],)).fetchone()
        if (row["lifecycle"] == "provisioning" and job is not None
                and (job["state"], job["wait_reason"]) == ("waiting", "dependency_unavailable")):
            try:
                self.jobs.reconcile(UUID(job["job_id"]))
            except CoreError:
                pass

    def _wait_workspace(self):
        return JobResolution(state="waiting", reason="retry_delay", next_attempt_at=after(self.db.clock(), 5),
            progress=JobProgress(phase="workspace_required"),
            error=CoreError("not_ready", "Projection awaits the owned workspace", retryable=True).error)

    def _perform(self, job, claim=None, *, inspecting=False):
        row, installation, held = self._snapshot(job, claim, allow_hold=inspecting)
        if held:
            return self._inspect_hold(job, row, installation)

        def guard():
            self._snapshot(job, claim)

        with directory(self.root) as root:
            guard()
            try:
                # Projection cannot create even the agents parent before provisioning.
                with child_directory(root, "agents", create=job["kind"] == "agent_provision" and not inspecting) as parent:
                    target = str(UUID(row["id"]))
                    try:
                        info = os.stat(target, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        info = None
                    if info is None:
                        if row["workspace_initialized_at"] is not None:
                            raise CoreError("uncertain_effect", "Published workspace is missing; explicit repair is required")
                        if job["kind"] == "agent_project":
                            return self._wait_workspace()
                        if inspecting:
                            # Never replay an ambiguous intent without a positively
                            # owned stage. Inspect absent paths without creating them.
                            with child_directory(parent, stage_name(row)) as staged:
                                self._marker(staged, row, installation)
                        staging = self._prepare(parent, row, installation, guard, resume_only=inspecting)
                        guard()
                        same_child(root, "agents", parent)
                        self.db.fault("agent_before_publish")
                        guard()
                        with child_directory(parent, staging) as staged:
                            self._marker(staged, row, installation, complete=True)
                            same_child(parent, staging, staged)
                            same_child(root, "agents", parent)
                            try:
                                rename_noreplace(parent, staging, target)
                            except FileExistsError:
                                # Verify the existing destination's complete
                                # ownership below, never replace or adopt it.
                                pass
                            else:
                                same_child(parent, target, staged)
                        self.db.fault("agent_after_publish")
                        os.fsync(parent)
                        self.db.fault("agent_after_publish_fsync")
                    with child_directory(parent, target) as final:
                        self._marker(final, row, installation, complete=True)

                        def projection_guard():
                            guard()
                            same_child(root, "agents", parent)
                            same_child(parent, target, final)
                            self._marker(final, row, installation, complete=True)

                        projection_guard()
                        revision = self._write_projection(final, job, claim, projection_guard)
                        os.fsync(parent)
                        projection_guard()
                        # Re-open the data-root chain to detect ancestor substitution
                        # before committing evidence for a detached descriptor.
                        with directory(self.root) as current_root:
                            if (os.fstat(current_root).st_dev, os.fstat(current_root).st_ino) != (os.fstat(root).st_dev, os.fstat(root).st_ino):
                                raise CoreError("target_changed", "Installation directory changed during effect")
                        self._finalize(job, claim, revision)
            except FileNotFoundError:
                if job["kind"] == "agent_project" and row["workspace_initialized_at"] is None:
                    return self._wait_workspace()
                raise CoreError("uncertain_effect", "Workspace ownership evidence is missing") from None
        if job["kind"] == "agent_provision":
            return self._local(job, claim, inspecting=inspecting)
        return JobResolution(state="completed", progress=JobProgress(phase="projected", completed=1, total=1))

    def _inspect_hold(self, job, row, installation):
        # Deliberately no mkdir/write/fsync/finalization. Even complete evidence
        # cannot release a restored resource/start intent automatically.
        phase = "recovery_inspection_required"
        error = CoreError("uncertain_effect", "Restored workspace intent requires explicit recovery review").error
        try:
            with directory(self.root) as root, child_directory(root, "agents") as parent:
                with child_directory(parent, str(UUID(row["id"]))) as final:
                    self._marker(final, row, installation, complete=True)
                    data = json.loads(read_regular(final, "agent.json", MAX_PROJECTION_BYTES))
                    if data == self._projection(row, installation):
                        phase = "workspace_observed_recovery_held"
        except (CoreError, OSError, ValueError):
            pass
        return JobResolution(state="waiting", reason="recovery_review", next_attempt_at=after(self.db.clock(), 60),
            progress=JobProgress(phase=phase), error=error)

    @staticmethod
    def _error(exc):
        if isinstance(exc, CoreError):
            return exc.error
        return CoreError("uncertain_effect", "Workspace filesystem effect requires inspection").error

    def execute(self, claim: JobClaim):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            with self.db.read() as tx:
                job = dict(self.jobs._current_claim(tx, claim))
            try:
                return self._perform(job, claim)
            except OSError as exc:
                raise CoreError("uncertain_effect", "Workspace filesystem effect requires inspection") from exc

    def inspect(self, job):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            try:
                return self._perform(job, inspecting=True)
            except (CoreError, OSError) as exc:
                held = job["wait_reason"] == "recovery_review"
                return JobResolution(state="waiting" if held else "uncertain",
                    reason="recovery_review" if held else "reconciliation_required", next_attempt_at=after(self.db.clock(), 60),
                    progress=JobProgress(phase="inspection_required"), error=self._error(exc))
