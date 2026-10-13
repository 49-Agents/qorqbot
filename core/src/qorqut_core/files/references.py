"""Observe a regular file outside SQL, then save its immutable past metadata."""

import errno
import os
import posixpath
import stat
from contextlib import ExitStack
from pathlib import Path

from ..agents.registry import get_row
from ..agents.workspace import child_directory, same_child
from ..app import Change, Operation
from ..contracts import CoreError
from ..messaging.models import EntryReference
from ..storage.database import assert_external_io_allowed
from ..storage.events import EventInput, append_event
from ..storage.files import directory
from .models import FileReference, FileReferenced, FilesReference


def reference_view(row):
    return FileReference(**dict(row))


def reference_record(tx, reference_id):
    row = tx.connection.execute("SELECT * FROM file_references WHERE reference_id=?", (str(reference_id),)).fetchone()
    if row is None:
        raise CoreError("not_found", "File reference not found")
    return reference_view(row)


def _normalized(requested):
    # POSIX lexical normalization only. In particular, never resolve(), expand
    # '~' or variables, or reinterpret a native guest absolute path on the host.
    path = posixpath.normpath("/" + requested.lstrip("/") if requested.startswith("/") else requested)
    if path in (".", "/") or requested.endswith("/") or requested.split("/")[-1] == ".":
        raise CoreError("invalid_argument", "File reference requires a file path")
    return path


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _filesystem_error(exc):
    if exc.errno == errno.ENOENT:
        return CoreError("not_found", "File reference source or workspace is missing")
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENAMETOOLONG):
        return CoreError("invalid_argument", "File reference path contains a link or invalid component")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return CoreError("unavailable", "File reference source or workspace is unreadable")
    return CoreError("storage_blocked", "File reference metadata observation failed")


class FileReferences:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def operations(self):
        return (Operation("files_reference", FilesReference, True, self._unprepared, FileReferenced,
            prepare=self.prepare,
            description="Reference a file when you want to identify an artifact in the conversation. "
                "Saves its path and observed metadata, with an optional label and note; file contents are not saved."),)

    @staticmethod
    def _unprepared(tx, args, context, operation_id):
        raise CoreError("invalid_transition", "File reference requires prepared metadata")

    def _binding(self, tx, args, context):
        actor = tx.connection.execute("SELECT * FROM actors WHERE actor_id=?", (str(context.actor_id),)).fetchone()
        if actor is None:
            raise CoreError("not_found", "Calling actor is not registered")
        target = args.agent_id if args.agent_id is not None else actor["agent_id"]
        if target is None:
            raise CoreError("invalid_argument", "Owner file references require a target agent")
        agent = get_row(tx, target)
        if agent["lifecycle"] in ("archiving", "archived"):
            raise CoreError("invalid_transition", "Archived agents cannot receive new file references")
        if agent["workspace_initialized_at"] is None:
            raise CoreError("not_ready", "Workspace has not been durably initialized")
        installation = tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone()
        if args.path.startswith("/") and agent["kind"] != "host":
            raise CoreError("unsupported_capability", "Absolute file paths require a supported target environment")
        return dict(actor), dict(agent), dict(installation)

    def _observe(self, agent, installation, location, path):
        assert_external_io_allowed()
        with self.app.workspaces.owned(agent, installation) as (owned, guard), ExitStack() as stack:
            if location == "workspace":
                root = owned
                components = ["workspace", *path.split("/")]
            else:
                root = stack.enter_context(directory(Path("/")))
                components = path.lstrip("/").split("/")
            parent, parents = root, []
            for part in components[:-1]:
                child = stack.enter_context(child_directory(parent, part))
                parents.append((parent, part, child))
                parent = child
            name = components[-1]
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise CoreError("invalid_argument", "File reference source must be a regular file without symlinks")
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            stack.callback(os.close, fd)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or _identity(before) != _identity(opened):
                raise CoreError("target_changed", "File reference source changed during observation")
            if not 0 <= opened.st_size <= 2**63 - 1:
                raise CoreError("unsupported_capability", "File reference size exceeds the supported range")
            self.db.fault("file_reference_observing")
            guard()
            for coordinates in parents:
                same_child(*coordinates)
            if (_identity(os.fstat(fd)) != _identity(opened)
                    or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(opened)):
                raise CoreError("target_changed", "File reference source changed during observation")
            observed_at = self.db.clock()
        # All source/directory descriptors have closed before returning to SQL.
        return opened.st_size, str(opened.st_mtime_ns), observed_at

    def prepare(self, args, context):
        assert_external_io_allowed()
        with self.db.read() as tx:
            actor, agent, installation = self._binding(tx, args, context)
        path, location = _normalized(args.path), "workspace"
        if path.startswith("/"):
            workspace = str(self.app.settings.data_root / "agents" / agent["id"] / "workspace")
            if path == workspace:
                raise CoreError("invalid_argument", "File reference requires a file path")
            if path.startswith(workspace + "/"):
                path = path[len(workspace) + 1:]
            else:
                location = "host"
        label = args.label
        if label is None:
            basename = posixpath.basename(path)
            label = "File" if not basename.strip() else basename[:199] + "…" if len(basename) > 200 else basename
        try:
            size, mtime, observed_at = self._observe(agent, installation, location, path)
        except OSError as exc:
            raise _filesystem_error(exc) from exc
        except CoreError as exc:
            # The reused directory context sanitizes OSError even when it was
            # raised by a nested source access. Retain this domain's fixed codes.
            if exc.code in ("storage_blocked", "invalid_argument") and isinstance(exc.__cause__, OSError):
                raise _filesystem_error(exc.__cause__) from exc
            raise
        self.db.fault("file_reference_after_observation")

        def record(tx, validated, invocation, operation_id):
            current_actor, current_agent, current_installation = self._binding(tx, validated, invocation)
            if (current_actor != actor or any(current_agent[key] != agent[key] for key in (
                    "id", "actor_id", "conversation_id", "kind", "creation_operation_id", "provisioning_job_id",
                    "lifecycle", "lifecycle_revision", "workspace_initialized_at"))
                    or any(current_installation[key] != installation[key] for key in (
                        "installation_id", "recovery_epoch", "onbash_agent_id"))):
                raise CoreError("target_changed", "File reference binding changed after observation")
            reference = FileReference(reference_id=self.db.id_factory(), agent_id=agent["id"],
                conversation_id=agent["conversation_id"], actor_id=invocation.actor_id,
                installation_id=installation["installation_id"], recovery_epoch=installation["recovery_epoch"],
                location=location, path=path, requested_path=validated.path,
                label=label, note=validated.note,
                size_bytes=size, mtime_ns=mtime, observed_at=observed_at, recorded_at=self.db.clock())
            fields = reference.model_dump(mode="json")
            tx.connection.execute(f"INSERT INTO file_references ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(fields.values()))
            self.db.fault("file_reference_after_record")
            seq = append_event(tx, EventInput(kind="file.referenced", actor_id=invocation.actor_id,
                agent_id=reference.agent_id, operation_id=operation_id, occurred_at=reference.observed_at,
                payload={key: fields[key] for key in ("reference_id", "agent_id", "conversation_id", "location", "path")}))
            entry = self.app.conversations.append_file_reference(tx, reference, operation_id=operation_id)
            return Change(FileReferenced(reference=reference,
                entry=EntryReference(**entry.model_dump(include=set(EntryReference.model_fields)))), seq)

        return record
