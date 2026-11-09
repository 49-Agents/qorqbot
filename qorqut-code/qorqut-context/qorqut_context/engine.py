"""Sequential source events with durable drafts and atomic Git publication."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import string
import tempfile

from .git_store import CONFIG_PATH, CURRENT_CONFIG, FORMAT_VERSION, Git, Snapshot
from .schema import (Conflict, ContextError, RECORD_KINDS, change_details, digest, encode,
                     identifier, metadata as normalize_metadata, note_record, object_record,
                     record_path, source_path, text, value_key)
from .search import SQLiteIndex, changes_between, project
from . import controlled, graph_edits, input_modes as modes, personal, questions, supervised_edits


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path):
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Conflict("Another writer/query is using this shared context") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check_ownership(path: Path, expected: dict) -> bool:
    try:
        actual = json.loads(path.read_bytes())
    except FileNotFoundError as error:
        if path.is_symlink():
            raise Conflict(f"Invalid context ownership marker: {path}") from error
        return False
    except (OSError, ValueError) as error:
        raise Conflict(f"Invalid context ownership marker: {path}") from error
    if actual != expected:
        raise Conflict(f"Context ownership does not match this repository/runtime: {path}")
    return True


@dataclass(frozen=True)
class Submission:
    event_id: str
    source_id: str
    base_revision: str
    published_commit: str | None = None


class Context:
    """One shared context. Model execution is outside this class."""

    def __init__(self, root, runtime=None, *, index_factory=SQLiteIndex):
        self.root = Path(root).expanduser().resolve()
        self.runtime = (Path(runtime).expanduser() if runtime else
                        self.root.with_name(self.root.name + ".runtime")).resolve()
        if (self.root == self.runtime or self.runtime.is_relative_to(self.root) or
                self.root.is_relative_to(self.runtime)):
            raise ContextError("Repository and runtime directories must be separate")
        self.git = Git(self.root)
        self.index = index_factory(self.runtime / "search.sqlite3")
        self.job_path = self.runtime / "pending.json"

    @classmethod
    def initialize(cls, root, runtime=None, *, basic_entity_types=None, index_factory=SQLiteIndex,
                   personal_context=True):
        context = cls(root, runtime, index_factory=index_factory)
        seeds = basic_entity_types if basic_entity_types is not None else []
        if not isinstance(seeds, list) or any(not isinstance(v, str) or not v.strip() for v in seeds):
            raise ContextError("Initial categories must be nonempty strings")
        if type(personal_context) is not bool:
            raise ContextError("personal_context must be a boolean")
        context.root.mkdir(parents=True, exist_ok=True)
        if any(context.root.iterdir()):
            raise Conflict("Initialization requires an empty directory")
        try:
            context.git.run("init", "-b", "main")
        except ContextError as error:
            raise Conflict("Could not prepare the empty Git repository for initialization") from error
        with context._lock(initializing=True):
            files = {CONFIG_PATH: encode(CURRENT_CONFIG),
                     "graph/catalogs/nodes.json": encode({"basic_entity_type": list(dict.fromkeys(seeds))}),
                     "graph/catalogs/edges.json": encode({})}
            commit = context.git.commit(files, context.runtime, None, "Initialize context vocabulary")
            context.git.publish(commit, None)
            context.git.checkout(None, commit)
            context._ensure_index(context.snapshot())
        if personal_context:
            personal.initialize(context)
        return context

    @contextmanager
    def _lock(self, *, initializing=False):
        coordination = self.git.common_directory() / "qorqut-context"
        coordination.mkdir(parents=True, exist_ok=True)
        with file_lock(coordination / "writer.lock"):
            if initializing and (self.git.run("show-ref", check=False).strip() or
                                 any(path.name != ".git" for path in self.root.iterdir()) or
                                 self.git.run("ls-files", "--stage").strip()):
                raise Conflict("Initialization found existing context or unrelated files; they were preserved")
            binding = coordination / "runtime.json"
            expected_runtime = {"runtime_root": str(self.runtime)}
            bound = check_ownership(binding, expected_runtime)
            self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Repository first, then runtime, including when two repositories
            # attempt to claim the same runtime before either has a binding.
            with file_lock(self.runtime / "writer.lock"):
                marker = self.runtime / "context.json"
                expected = {"context_root": str(self.root)}
                owned = check_ownership(marker, expected)
                # Validate both directions before creating either marker.
                if not owned:
                    atomic_write(marker, encode(expected))
                if not bound:
                    atomic_write(binding, encode(expected_runtime))
                yield

    def snapshot(self, revision=None) -> Snapshot:
        return Snapshot(self.git, self.git.resolve(revision))

    def _job(self):
        """The pending job, validated as current-format before any reader or writer sees it."""
        if not self.job_path.exists():
            return None
        job = json.loads(self.job_path.read_bytes())
        self._require_current_format(job)
        controlled.binding(job)
        return job

    def pending(self):
        with self._lock():
            return self._job()

    def _save(self, job):
        atomic_write(self.job_path, encode(job))

    def _event_directory(self, event_id):
        return self.runtime / "events" / identifier(event_id)

    def _random_id(self):
        return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))

    def _allocate(self, used):
        for _ in range(1024):
            candidate = identifier(self._random_id())
            if candidate not in used:
                return candidate
        raise Conflict("Could not allocate an unused five-character ID")

    @staticmethod
    def _submission_digest(operation, path, content, normalized, submitted_by, evidence):
        return digest({"input_mode": modes.GENERAL, "operation": operation, "path": path,
                       "content": hashlib.sha256(content).hexdigest() if content is not None else None,
                       "metadata": normalized, "submitted_by": submitted_by, **evidence})

    @staticmethod
    def _evidence(**values):
        """Optional controlled evidence in the order it joins identity; ``None`` means not supplied and is omitted."""
        return {name: value for name, value in values.items() if value is not None}

    def begin(self, operation, path, *, content=None, metadata=None, submitted_by,
              event_id=None, source_bytes=modes.SOURCE_BYTES, record_context=None,
              expected_revision=None) -> Submission:
        """Stage one GENERAL source change.

        ``record_context`` is optional trusted-caller evidence ``{scope, provenance}`` bound into the
        submission and recorded in its event; it creates no permission. ``expected_revision`` is an
        optional full published commit that a NEW event requires to be the published head when it is
        staged; it is bound into the submission but is not recorded as a second base.
        """
        if operation not in ("add", "edit", "remove"):
            raise ContextError("Source operation must be add, edit, or remove")
        if record_context is not None:
            self._validate_source(None, source_bytes)
            record_context = modes.record_context(record_context, source_bytes)
        if expected_revision is not None:
            modes.full_commit(expected_revision, "expected_revision")
        path = source_path(path)
        if path.startswith("foundation/actions/"):
            raise ContextError("foundation/actions/ is reserved for accepted action evidence")
        if path.startswith(graph_edits.PREFIX):
            raise ContextError("foundation/edits/ is reserved for controlled graph edits")
        if path.startswith(supervised_edits.PREFIX):
            raise ContextError("foundation/supervised-edits/ is reserved for supervised edits")
        submitted_by = text(submitted_by, "submitted_by")
        if ((operation == "remove" and content is not None) or
                (operation != "remove" and not isinstance(content, bytes))):
            raise ContextError("Add/edit requires bytes; removal must not supply content")
        if event_id is not None:
            identifier(event_id)
        return self._admit_general(operation, path, content, metadata, submitted_by, event_id,
                                   source_bytes, record_context, expected_revision)

    def begin_edit(self, plan, *, submitted_by, event_id, source_bytes=modes.SOURCE_BYTES,
                   record_context=None) -> Submission:
        """Stage one controlled deterministic graph edit; trusted callers only.

        ``plan`` is the ``qorqut-graph-edit-v1`` document (see CORE_CONTRACTS.md). It is validated in full
        against its ``expected_revision``, which must be the published head, before anything is written. The
        canonical plan becomes the add-only GENERAL source ``foundation/edits/{event_id}.txt`` and an
        execution marker is saved with that binding in the same atomic pending-job write. Nothing here
        runs a model; ``record_context`` is the same evidence as for ``begin``.
        """
        event_id = identifier(event_id)
        submitted_by = text(submitted_by, "submitted_by")
        self._validate_source(None, source_bytes)
        if record_context is not None:
            record_context = modes.record_context(record_context, source_bytes)
        plan = graph_edits.normalize(plan)
        return self._admit_general("add", graph_edits.source_path(event_id), graph_edits.canonical(plan),
                                   graph_edits.METADATA, submitted_by, event_id, source_bytes,
                                   record_context, None, edit=controlled.graph_edit(plan))

    def begin_supervised(self, plan, *, content, submitted_by, event_id, source_bytes=modes.SOURCE_BYTES,
                         record_context=None) -> Submission:
        """Stage one supervised edit: a supporting Markdown document plus explicit node/edge/note operations.

        Trusted callers only, like ``begin_edit``; nothing here runs a model. ``content`` is preserved exactly as the
        add-only source ``foundation/supervised-edits/{event_id}.md``; the normalized plan and the document
        digest are retained in the execution marker. The trusted caller submits exactly the plan and document it
        reviewed; the engine rechecks ``expected_revision`` and the event binding but cannot establish approval.
        """
        event_id, submitted_by, edit, record_context = self._supervised_proposal(
            plan, content, submitted_by, event_id, source_bytes, record_context)
        return self._admit_general("add", supervised_edits.source_path(event_id), content,
                                   supervised_edits.metadata(event_id), submitted_by, event_id, source_bytes,
                                   record_context, None, edit=edit)

    def _supervised_proposal(self, plan, content, submitted_by, event_id, source_bytes, record_context):
        event_id = identifier(event_id)
        submitted_by = text(submitted_by, "submitted_by")
        self._validate_source(None, source_bytes)
        content = supervised_edits.document(content, source_bytes)
        if record_context is not None:
            record_context = modes.record_context(record_context, source_bytes)
        plan = supervised_edits.normalize(plan, source_bytes)
        if len(supervised_edits.canonical(plan)) > source_bytes:
            raise ContextError("The supervised edit plan exceeds the configured byte limit")
        return event_id, submitted_by, controlled.supervised_edit(plan, content), record_context

    def preview_supervised(self, plan, *, content, submitted_by, event_id, source_bytes=modes.SOURCE_BYTES,
                           record_context=None) -> dict:
        """Validate a supervised proposal against the published head and describe it; mutates nothing.

        Nothing is staged, locked, created or published and no model is involved. A plan whose
        ``expected_revision`` is no longer the published head, a conflicting pending or published event, or any
        operation the ordinary validators refuse raises the same error application would. ``submission_digest``
        is informative: it identifies this proposal but is not an approval or an authorization.
        """
        event_id, submitted_by, edit, record_context = self._supervised_proposal(
            plan, content, submitted_by, event_id, source_bytes, record_context)
        snapshot, pending = self.snapshot(), self._job()
        path, metadata = supervised_edits.source_path(event_id), supervised_edits.metadata(event_id)
        evidence = self._evidence(record_context=record_context, execution=edit.marker)
        request_digest = self._submission_digest("add", path, content, normalize_metadata(metadata),
                                                 submitted_by, evidence)
        summary = {"revision": snapshot.revision, "plan": edit.plan, "document_digest": edit.marker["document_digest"],
                   "event_id": event_id, "base_revision": snapshot.revision, "source_path": path,
                   "plan_digest": edit.marker["plan_digest"], "document_bytes": len(content),
                   "submission_digest": request_digest}
        existing = snapshot.events.get(event_id)
        if existing:
            if existing["submission_digest"] != request_digest:
                raise Conflict("A published event ID cannot be reused with different input")
            return {**summary, "status": "published", "published_commit": snapshot.event_commits[event_id]}
        if pending:
            if pending["id"] != event_id or pending["submission_digest"] != request_digest:
                raise Conflict("Complete, retry, or discard the pending event before submitting another")
            return {**summary, "status": "pending"}
        if snapshot.revision != edit.plan["expected_revision"]:
            raise Conflict("Stale expected revision: the published head is not the expected commit")
        if event_id in snapshot.used_ids:
            raise Conflict("This ID has already been published")
        if path in snapshot.files:
            raise Conflict("This foundation path already exists")
        self._check_edit(snapshot, edit, event_id, None, source_bytes)
        job = {"input_mode": modes.GENERAL, "operation": "add", "source": {"id": None},
               "source_bytes": source_bytes, "draft": supervised_edits.draft(edit.plan)}
        changes = {(change["kind"], change["id"]): change
                   for change in self._graph_files(snapshot, job, controlled_edit=True)[1]}
        operations, counts = [], {kind: {"add": 0, "edit": 0, "remove": 0} for kind in RECORD_KINDS}
        for operation in edit.plan["operations"]:
            kind, target = supervised_edits.kind(operation["op"]), supervised_edits.object_id(operation)
            change = changes[(kind, target)]
            counts[kind][change["operation"]] += 1
            before = snapshot.records(kind).get(target)
            support = snapshot.support.get((kind, target), [])
            operations.append({
                "op": operation["op"], "kind": kind, "id": target, "operation": change["operation"],
                "before": json.loads(json.dumps(before)) if before is not None else None,
                "after": operation.get("record"), "explanation": operation["explanation"],
                "citations": list(operation["citations"]), "support_before": list(support),
                "support_after": list(operation.get("support_after", []))})
        cites = any("self" in operation["citations"] for operation in edit.plan["operations"])
        return {**summary, "status": "ready", "rationale": edit.plan["rationale"], "operations": operations,
                "result_counts": counts, "cites_supporting_document": cites}

    def _admit_general(self, operation, path, content, metadata, submitted_by, event_id,
                       source_bytes, record_context, expected_revision, *, edit=None):
        """Locked GENERAL admission shared by ordinary source changes and controlled edits."""
        with self._lock():
            snapshot, pending = self.snapshot(), self._job()
            previous = next((event for event in snapshot.sources.values()
                             if event["source"]["path"] == path and event["operation"] != "remove"), None)
            existing = snapshot.events.get(event_id)
            if any(item and item.get("input_mode") != modes.GENERAL
                   for item in (existing, previous, pending if pending and pending["id"] == event_id else None)):
                raise Conflict("A source input mode cannot change")
            default_source = (existing or (pending if pending and pending["id"] == event_id else None) or previous)
            if metadata is None and default_source:
                metadata = {k: v for k, v in default_source["source"].items() if k not in ("id", "path")}
            normalized = normalize_metadata(metadata)
            evidence = self._evidence(record_context=record_context, expected_revision=expected_revision,
                                      execution=edit.marker if edit else None)
            request_digest = self._submission_digest(operation, path, content, normalized,
                                                     submitted_by, evidence)
            if existing:
                if existing["submission_digest"] != request_digest:
                    raise Conflict("A published event ID cannot be reused with different input")
                return Submission(event_id, existing["source"]["id"], existing["base_revision"],
                                  snapshot.event_commits[event_id])
            if pending:
                if pending["id"] != event_id or pending["submission_digest"] != request_digest:
                    raise Conflict("Complete, retry, or discard the pending event before submitting another")
                return Submission(pending["id"], pending["source"]["id"], pending["base_revision"])
            required = edit.plan["expected_revision"] if edit else expected_revision
            if required is not None and snapshot.revision != required:
                raise Conflict("Stale expected revision: the published head is not the expected commit")
            self._validate_source(content, source_bytes)
            if operation in ("edit", "remove"):
                self._validate_source(snapshot.files.get(path), source_bytes)
            if event_id in snapshot.used_ids:
                raise Conflict("This ID has already been published")
            if operation == "add" and path in snapshot.files:
                raise Conflict("This foundation path already exists")
            if operation != "add" and previous is None:
                raise ContextError("Edit/removal requires an existing managed source")
            if operation == "edit" and snapshot.files[path] == content:
                raise ContextError("Metadata-only or identical-byte edits are deferred")
            self.git.require_clean()
            event_id = event_id or self._allocate(snapshot.used_ids)
            reserved = {event_id} | (edit.module.object_ids(edit.plan) if edit else set())
            source_id = (self._allocate(snapshot.used_ids | reserved) if operation == "add"
                         else previous["source"]["id"])
            if edit:
                self._check_edit(snapshot, edit, event_id, source_id, source_bytes)
            return self._stage_submission(snapshot, event_id, source_id, path, normalized,
                operation, content, submitted_by, request_digest, source_bytes, previous,
                input_mode=modes.GENERAL, evidence=evidence)

    @staticmethod
    def _require_current_format(job):
        version = job.get("format_version") if isinstance(job, dict) else None
        if version != FORMAT_VERSION:
            raise Conflict(f"Unsupported pending event format version {version!r}; "
                           f"this engine supports format {FORMAT_VERSION} only"
                           + ("; complete this unfinished format 2 job with the older engine before switching"
                              if version == 2 else ""))

    @staticmethod
    def _validate_source(content, limit):
        if type(limit) is not int or limit <= 0:
            raise ContextError("source_bytes must be a positive integer")
        if content is None:
            return
        if not isinstance(content, bytes) or len(content) > limit:
            raise ContextError("Source input must be bytes within the configured byte limit")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextError("Source input must be model-readable UTF-8") from error

    def _stage_submission(self, snapshot, event_id, source_id, path, metadata, operation,
                          content, submitted_by, request_digest, source_bytes, previous,
                          *, input_mode, action=None, evidence=None):
        directory = self._event_directory(event_id)
        if content is not None:
            atomic_write(directory / "input.bin", content)
        job = {"format_version": FORMAT_VERSION, "id": event_id, "base_revision": snapshot.revision,
               "input_mode": input_mode, "submission_digest": request_digest,
               "operation": operation, "submitted_by": submitted_by,
               "source": {"id": source_id, "path": path, **metadata},
               "previous_source_event": ({"event_id": previous["id"],
                   "commit": snapshot.event_commits[previous["id"]]} if previous else None),
               "input_digest": hashlib.sha256(content).hexdigest() if content is not None else None,
               "source_bytes": source_bytes,
               "input_path": str(directory / "input.bin") if content is not None else None,
               "draft_location": str(self.job_path), "phase": "draft", "error": None,
               "reserved_ids": [event_id, source_id], "draft": {}}
        if action is not None:
            job["action"] = action
        job.update(evidence or {})
        self._save(job)
        return Submission(event_id, source_id, snapshot.revision)

    def begin_action(self, operation, action_ref, *, text=None, revision_ref=None,
                     related_objects=None, tags=None, submitted_by, event_id=None,
                     expected_previous_commit=None, source_bytes=modes.SOURCE_BYTES,
                     record_context=None) -> Submission:
        """Stage one accepted Action revision.

        ``record_context`` is optional trusted-caller evidence ``{scope, provenance}``: it joins the
        canonical Action source document (add/edit), the submission identity and the event, and
        is retained by a removal's event. It is separate from tags, which stay ordinary labels.
        """
        if operation not in ("add", "edit", "remove"):
            raise ContextError("Action operation must be add, edit, or remove")
        action_ref = modes.opaque_ref(action_ref, "action_ref")
        if revision_ref is not None:
            modes.opaque_ref(revision_ref, "revision_ref")
        tags = modes.tag_snapshots([] if tags is None else tags)
        submitted_by = modes.text(submitted_by, "submitted_by")
        related_objects = modes.pointers([] if related_objects is None else related_objects)
        self._validate_source(None, source_bytes)
        record_context = modes.record_context(record_context, source_bytes)
        if event_id is not None:
            identifier(event_id)
        if expected_previous_commit is not None:
            from .schema import references
            if expected_previous_commit == "self":
                raise ContextError("Expected previous commit must be a full published commit")
            references([expected_previous_commit])
            if operation == "add":
                raise ContextError("Expected previous commit applies only to edit/remove")
        if operation == "remove":
            if text is not None or related_objects:
                raise ContextError("Action removal supplies no text or pointers")
            content = None
        else:
            content = modes.action_document(action_ref, revision_ref, text, related_objects, tags,
                                            source_bytes, record_context)
        action = {"action_ref": action_ref, "revision_ref": revision_ref,
                  "pointers": related_objects, "tags": tags,
                  "expected_previous_commit": expected_previous_commit}
        evidence = self._evidence(record_context=record_context)
        request_digest = digest({"input_mode": modes.ACTION, "operation": operation,
            "content": hashlib.sha256(content).hexdigest() if content is not None else None,
            "action": action, "submitted_by": submitted_by, **evidence})
        with self._lock():
            snapshot, pending = self.snapshot(), self._job()
            existing = snapshot.events.get(event_id)
            if existing:
                if existing["submission_digest"] != request_digest:
                    raise Conflict("A published event ID cannot be reused with different input")
                return Submission(event_id, existing["source"]["id"], existing["base_revision"],
                                  snapshot.event_commits[event_id])
            if pending:
                if pending["id"] != event_id or pending["submission_digest"] != request_digest:
                    raise Conflict("Complete, retry, or discard the pending event before submitting another")
                return Submission(pending["id"], pending["source"]["id"], pending["base_revision"])
            previous = snapshot.actions.get(action_ref)
            if operation == "add" and previous:
                raise Conflict("This action_ref already has a source identity, including after withdrawal")
            if operation != "add" and (not previous or previous["operation"] == "remove"):
                raise Conflict("Action edit/removal requires a current accepted action")
            if expected_previous_commit is not None and snapshot.event_commits[previous["id"]] != expected_previous_commit:
                raise Conflict("Stale previous action-ingestion commit")
            if event_id in snapshot.used_ids:
                raise Conflict("This ID has already been published")
            event_id = event_id or self._allocate(snapshot.used_ids)
            source_id = (previous["source"]["id"] if previous else
                         self._allocate(snapshot.used_ids | {event_id}))
            path = f"foundation/actions/{source_id}.txt"
            if previous:
                self._validate_source(snapshot.files[path], source_bytes)
                if operation == "edit" and snapshot.files[path] == content:
                    raise ContextError("Metadata-only or identical-byte edits are deferred")
            self.git.require_clean()
            return self._stage_submission(snapshot, event_id, source_id, path,
                {"kind": "accepted_action", "title": action_ref, "origin": "controlled_caller"},
                operation, content, submitted_by, request_digest, source_bytes, previous,
                input_mode=modes.ACTION, action=action, evidence=evidence)

    def stage_handoff_note(self, event_id, text):
        with self._lock():
            job = self._editable_job(event_id)
            job["handoff_note"] = modes.handoff_note(text, job["source_bytes"])
            job.update(phase="draft", error=None)
            self._save(job)
            return {"event_id": event_id, "disposition": job["handoff_note"]["disposition"],
                    "bytes": len(text.encode("utf-8")), "published": False}

    def stage_action_associations(self, event_id, date, summary, associations):
        with self._lock():
            job = self._editable_job(event_id)
            if job["input_mode"] != modes.ACTION:
                raise ContextError("Action associations require OPERATIONAL_ACTION input")
            output = modes.action_associations(date, summary, associations)
            self._validate_associations(self.snapshot(job["base_revision"]), job, output)
            job["action_associations"] = output
            job.update(phase="draft", error=None)
            self._save(job)
            return {"event_id": event_id, "associations": len(associations), "published": False}

    @staticmethod
    def _validate_associations(snapshot, job, output):
        if job["operation"] == "remove":
            previous = snapshot.actions[job["action"]["action_ref"]]["action_associations"]
            if output["associations"] or output["date"] != previous["date"]:
                raise ContextError("Withdrawal requires empty associations and the previous action date")
        for item in output["associations"]:
            key = (item["kind"], item["id"])
            draft = job["draft"].get(":".join(key))
            if key not in snapshot.known_objects and not (draft and draft["record"] is not None):
                raise ContextError("Association target must exist in published history or this draft")

    def required_reassessment(self, draft):
        snapshot = self.snapshot(draft["base_revision"])
        if draft["operation"] == "add":
            return []
        required = {(r["kind"], r["id"]) for r in snapshot.supported_by(draft["source"]["id"])}
        if draft["input_mode"] == modes.ACTION:
            for event in snapshot.action_events(draft["action"]["action_ref"]):
                required.update((item["kind"], item["id"])
                                for item in event["action_associations"]["associations"])
        return [{"kind": kind, "id": key} for kind, key in sorted(required)]

    def _editable_job(self, event_id):
        identifier(event_id)
        job = self._job()
        if job is None or job["id"] != event_id:
            raise Conflict("No matching pending event")
        snapshot = self.snapshot()
        if event_id in snapshot.events:
            raise Conflict("This event is already published; recover it instead of editing")
        if self.git.head() != job["base_revision"]:
            raise Conflict("Publication base changed; retain or explicitly discard the draft")
        return job

    def _put(self, event_id, kind, record, *, explanation, citations, support_after, developments=None):
        details = change_details(explanation, citations, support_after,
                                 [] if developments is None else developments)
        with self._lock():
            job = self._editable_job(event_id)
            if job["input_mode"] == modes.GENERAL and details["developments"]:
                raise ContextError("GENERAL input cannot record developments")
            if kind == "note" and details["developments"]:
                raise ContextError("A note cannot record developments")
            snapshot = self.snapshot(job["base_revision"])
            record = dict(record)
            object_id = (record["id"] if "id" in record else
                         self._allocate(snapshot.used_ids | set(job["reserved_ids"])))
            record["id"] = object_id
            record = (note_record(record, job["source_bytes"]) if kind == "note"
                      else object_record(kind, record))
            personal.validate_change(snapshot, kind, object_id, snapshot.records(kind).get(object_id), record,
                                     controlled_edit=controlled.binding(job))
            key = f"{kind}:{object_id}"
            if object_id not in snapshot.records(kind) and key not in job["draft"]:
                if object_id in snapshot.used_ids or object_id in job["reserved_ids"]:
                    raise Conflict("Object ID is already reserved or has been published before")
            if object_id not in job["reserved_ids"]:
                job["reserved_ids"].append(object_id)
            job["draft"][key] = {"kind": kind, "id": object_id, "record": record, **details}
            job.update(phase="draft", error=None)
            self._save(job)
            return object_id

    def put_node(self, event_id, record, **details):
        return self._put(event_id, "node", record, **details)

    def put_edge(self, event_id, record, **details):
        return self._put(event_id, "edge", record, **details)

    def put_note(self, event_id, record, *, explanation, citations, support_after):
        """Stage a complete note ``{id?, text, objects}``; links are validated against the final draft at publication."""
        return self._put(event_id, "note", record, explanation=explanation, citations=citations,
                         support_after=support_after)

    def _remove(self, event_id, kind, object_id, *, explanation, citations, developments=None):
        identifier(object_id)
        details = change_details(explanation, citations, [], [] if developments is None else developments)
        with self._lock():
            job = self._editable_job(event_id)
            if job["input_mode"] == modes.GENERAL and details["developments"]:
                raise ContextError("GENERAL input cannot record developments")
            if kind == "note" and details["developments"]:
                raise ContextError("A note cannot record developments")
            snapshot = self.snapshot(job["base_revision"])
            key = f"{kind}:{object_id}"
            personal.validate_change(snapshot, kind, object_id, snapshot.records(kind).get(object_id), None,
                                     controlled_edit=controlled.binding(job))
            if object_id not in snapshot.records(kind) and key not in job["draft"]:
                raise ContextError(f"No draft/current {kind} with this ID")
            if object_id not in snapshot.records(kind):
                del job["draft"][key]  # A create followed by a remove has no published object.
            else:
                job["draft"][key] = {"kind": kind, "id": object_id, "record": None, **details}
            job.update(phase="draft", error=None)
            self._save(job)

    def remove_node(self, event_id, object_id, **details):
        return self._remove(event_id, "node", object_id, **details)

    def remove_edge(self, event_id, object_id, **details):
        return self._remove(event_id, "edge", object_id, **details)

    def remove_note(self, event_id, object_id, *, explanation, citations):
        """Stage explicit removal of a note; its history and evidence stay published."""
        return self._remove(event_id, "note", object_id, explanation=explanation, citations=citations)

    def _prepare_publication(self, snapshot, job):
        plan = self._bound_plan(job)
        files, changes, objects = self._graph_files(snapshot, job, controlled_edit=plan is not None)
        if plan is not None:
            self._require_exact_edit(job, plan)
            self._require_effective(job, changes, controlled.module_for(plan))
        if job["operation"] == "remove":
            content = None
        else:
            content = (self._event_directory(job["id"]) / "input.bin").read_bytes()
            if hashlib.sha256(content).hexdigest() != job["input_digest"]:
                raise ContextError("The durable source input changed after submission")
        self._validate_source(content, job["source_bytes"])
        mode_outputs = self._validate_mode_output(snapshot, job, objects, content)
        self._validate_questions(snapshot, job, {kind: set(found) for kind, found in objects.items()})
        files[job["source"]["path"]] = content
        if snapshot.format_version != FORMAT_VERSION:
            config = dict(CURRENT_CONFIG)
            if snapshot.personal_context_node:
                config["personal_context_node"] = snapshot.personal_context_node
            files[CONFIG_PATH] = encode(config)
        event = {k: job[k] for k in ("format_version", "id", "base_revision", "submission_digest",
                                    "operation", "submitted_by", "source", "input_mode",
                                    "input_digest", "previous_source_event")}
        event.update({key: job[key] for key in ("record_context", "execution") if key in job})
        event.update(mode_outputs)
        event["changes"] = sorted(changes, key=lambda c: (c["kind"], c["id"]))
        files[f"graph/events/{job['id']}.json"] = encode(event)
        return files

    def _bound_plan(self, job):
        """The plan of a controlled edit after matching marker, source binding and durable bytes; ``None`` if ordinary."""
        if not controlled.binding(job):
            return None
        try:
            content = (self._event_directory(job["id"]) / "input.bin").read_bytes()
        except OSError as error:
            raise ContextError("The durable graph-edit/supervised-edit source input is unavailable") from error
        if hashlib.sha256(content).hexdigest() != job["input_digest"]:
            raise ContextError("The durable source input changed after submission")
        return controlled.bound_plan(job, content)

    @staticmethod
    def _require_exact_edit(job, plan):
        """A controlled edit publishes its normalized plan exactly: no missing, extra or altered draft content."""
        module = controlled.module_for(plan)
        if (encode(job["draft"]) != encode(module.draft(plan)) or
                encode(job.get("handoff_note")) != encode(module.HANDOFF) or job.get("questions")):
            raise Conflict(f"The draft is not exactly the bound {module.NOUN}; retry the event to stage it")

    @staticmethod
    def _require_effective(job, changes, module):
        if len(changes) != len(job["draft"]):
            raise ContextError(f"Every {module.NOUN} operation must change the context")

    def _check_edit(self, snapshot, edit, event_id, source_id, source_bytes):
        """Validate a plan against its fixed base before any pending state is written."""
        module, plan = edit.module, edit.plan
        for operation in plan["operations"]:
            kind, target = module.kind(operation["op"]), module.object_id(operation)
            current = target in snapshot.records(kind)
            if operation["op"] in module.REMOVES:
                if not current:
                    raise ContextError(f"{operation['op']} requires a current {kind}: {target}")
            elif not current and (target in snapshot.used_ids or target in (event_id, source_id)):
                raise Conflict("Object ID is already reserved or has been published before")
        job = {"input_mode": modes.GENERAL, "operation": "add", "source": {"id": source_id},
               "source_bytes": source_bytes, "draft": module.draft(plan)}
        self._require_effective(job, self._graph_files(snapshot, job, controlled_edit=True)[1], module)

    def pending_edit(self, event_id):
        """The validated plan of the pending controlled edit ``event_id`` (graph or supervised); ``None`` for ordinary input."""
        identifier(event_id)
        with self._lock():
            return self._bound_plan(self._editable_job(event_id))

    def _graph_files(self, snapshot, job, *, controlled_edit=False):
        objects = {"node": dict(snapshot.nodes), "edge": dict(snapshot.edges), "note": dict(snapshot.notes)}
        support = dict(snapshot.support)
        catalogs = json.loads(json.dumps(snapshot.catalogs))
        files, changes = {}, []
        for draft in job["draft"].values():
            if draft["developments"] and (job["input_mode"] == modes.GENERAL or draft["kind"] == "note"):
                raise ContextError("GENERAL input cannot record developments" if draft["kind"] != "note"
                                   else "A note cannot record developments")
            kind, object_id, record = draft["kind"], draft["id"], draft["record"]
            old = objects[kind].get(object_id)
            # Python equality conflates boolean and numeric attribute members.
            record_changed = encode(old) != encode(record)
            if (not record_changed and support.get((kind, object_id), []) == draft["support_after"]
                    and not draft["developments"]):
                continue
            personal.validate_change(snapshot, kind, object_id, old, record, controlled_edit=controlled_edit)
            operation = "remove" if record is None else ("add" if old is None else "edit")
            details = {k: v for k, v in draft.items() if k != "record"}
            changes.append({**details, "operation": operation})
            path = record_path(kind, object_id)
            if record is None:
                objects[kind].pop(object_id)
                files[path] = None
                support.pop((kind, object_id), None)
            else:
                record = (note_record(record, job.get("source_bytes")) if kind == "note"
                          else object_record(kind, record))
                objects[kind][object_id] = record
                if record_changed:
                    files[path] = encode(record)
                support[(kind, object_id)] = draft["support_after"]
                for name, values in (record["attributes"].items() if kind != "note" else ()):
                    known = catalogs[kind].setdefault(name, [])
                    keys = {value_key(v) for v in known}
                    for value in values:
                        if type(value) in (str, bool) and value_key(value) not in keys:
                            known.append(value)
                            keys.add(value_key(value))
        for record in objects["node"].values():
            object_record("node", record)
        for edge in objects["edge"].values():
            object_record("edge", edge)
            if edge["from_id"] not in objects["node"] or edge["to_id"] not in objects["node"]:
                raise ContextError("Every edge endpoint must exist; explicitly handle incident edges")
        for note in objects["note"].values():
            note_record(note, job.get("source_bytes"))
            for link in note["objects"]:
                if (link["id"] not in objects[link["kind"]] and
                        (link["kind"], link["id"]) not in snapshot.known_objects):
                    raise ContextError("A note may link only to final-draft or previously published nodes/edges")
        sources = {key for key, event in snapshot.sources.items() if event["operation"] != "remove"}
        if job["operation"] == "remove":
            sources.discard(job["source"]["id"])
        else:
            sources.add(job["source"]["id"])

        def referenced_source(ref):
            if ref == "self":
                return job["source"]["id"]
            if ref not in snapshot.by_commit:
                raise ContextError("Evidence must cite published source events in this shared context")
            return snapshot.by_commit[ref]["source"]["id"]

        for change in changes:
            for ref in change["citations"]:
                referenced_source(ref)
            for development in change["developments"]:
                for ref in development["citations"]:
                    referenced_source(ref)
        for kind, current in objects.items():
            for object_id in current:
                refs = support.get((kind, object_id), [])
                if not refs:
                    raise ContextError("Current notes require supporting source events" if kind == "note"
                                       else "Current graph objects require supporting source events")
                for ref in refs:
                    if referenced_source(ref) not in sources:
                        raise ContextError("Reassess graph support before deleting its source")
        for kind in ("node", "edge"):
            if catalogs[kind] != snapshot.catalogs[kind]:
                files[f"graph/catalogs/{kind}s.json"] = encode(catalogs[kind])
        return files, changes, objects

    def _validate_mode_output(self, snapshot, job, objects, content):
        mode = job["input_mode"]
        request = {"input_mode": mode, "operation": job["operation"],
                   "content": job["input_digest"], "submitted_by": job["submitted_by"],
                   **self._evidence(**{key: job.get(key) for key in ("record_context", "expected_revision", "execution")})}
        if mode == modes.GENERAL:
            if "action" in job or "action_associations" in job:
                raise ContextError("GENERAL input rejects action output")
            if job["source"]["path"].startswith("foundation/actions/"):
                raise ContextError("GENERAL input cannot write managed action evidence")
            request.update(path=job["source"]["path"], metadata={
                k: v for k, v in job["source"].items() if k not in ("id", "path")})
            outputs = {}
        elif mode == modes.ACTION:
            action = job["action"]
            request["action"] = action
            modes.tag_snapshots(action["tags"])
            if job["source"]["path"] != f"foundation/actions/{job['source']['id']}.txt":
                raise ContextError("Invalid managed action source path")
            if content is not None:
                document = json.loads(content)
                expected = modes.action_document(action["action_ref"], action["revision_ref"],
                    document["text"], action["pointers"], action["tags"], job["source_bytes"],
                    job.get("record_context"))
                if content != expected:
                    raise Conflict("Action evidence does not match the bound input")
            output = job.get("action_associations")
            if not isinstance(output, dict) or set(output) != {"date", "summary", "associations"}:
                raise ContextError("Action publication requires an explicit association draft")
            normalized = modes.action_associations(output["date"], output["summary"], [
                {k: v for k, v in item.items() if k != "citations"} for item in output["associations"]])
            if normalized != output:
                raise ContextError("Invalid action association draft")
            self._validate_associations(snapshot, job, output)
            previous = snapshot.actions.get(action["action_ref"])
            old = {(item["kind"], item["id"]) for item in
                   previous["action_associations"]["associations"]} if previous else set()
            new = {(item["kind"], item["id"]) for item in output["associations"]}
            outputs = {"action": action, "action_associations": output,
                       "association_transition": {name: [{"kind": kind, "id": key} for kind, key in sorted(values)]
                           for name, values in (("before", old), ("after", new), ("added", new - old), ("removed", old - new))}}
        else:
            raise ContextError("Unknown input mode")
        note = job.get("handoff_note")
        if not isinstance(note, dict) or set(note) != {"disposition", "text"}:
            raise ContextError("Publication requires an explicit handoff note disposition")
        if note != modes.handoff_note(note["text"], job["source_bytes"]):
            raise ContextError("Invalid handoff note disposition")
        outputs["handoff_note"] = note
        if digest(request) != job["submission_digest"]:
            raise Conflict("The bound submission changed after ingestion")
        return outputs

    def _outbox_path(self, event_id):
        return self.runtime / "questions" / f"{identifier(event_id)}.json"

    @staticmethod
    def _question_target(snapshot, event_id, present, reference):
        """Whether a question may reference this record: the event itself or a published event, a final-draft record
        or a known historical one; never an invented ID or a record created and withdrawn before publication."""
        kind, target = reference["kind"], reference["id"]
        if kind == "event":
            return target == event_id or target in snapshot.events
        return target in present[kind] or (kind, target) in snapshot.known_objects

    def _validate_questions(self, snapshot, job, present):
        if "questions" not in job:
            return
        if controlled.binding(job) or job["input_mode"] not in (modes.GENERAL, modes.ACTION):
            raise ContextError("A deterministic edit cannot carry question output")
        staged = questions.entries(job["questions"], job["source_bytes"])
        if encode(staged) != encode(job["questions"]):
            raise ContextError("Invalid staged question output")
        for entry in staged:
            for reference in entry["related"]:
                if not self._question_target(snapshot, job["id"], present, reference):
                    raise ContextError("A question may reference only this event, a published event, "
                                       "or a final-draft or previously published record")

    def emit_question(self, event_id, text, related=None):
        """Add one question ``{text, related}`` to this pending event's staged output; never removes or edits one.

        References are validated now against the published base and this draft, and again, against the final
        draft, when the draft is validated for publication. The output stays in runtime material and is not part
        of the draft's records, notes, source evidence or published event.
        """
        entry = questions.question(text, related)
        with self._lock():
            job = self._editable_job(event_id)
            if controlled.binding(job):
                raise ContextError("A deterministic edit cannot emit questions")
            snapshot = self.snapshot(job["base_revision"])
            present = {kind: set(snapshot.records(kind)) for kind in RECORD_KINDS}
            for staged in job["draft"].values():
                (present[staged["kind"]].discard if staged["record"] is None
                 else present[staged["kind"]].add)(staged["id"])
            for reference in entry["related"]:
                if not self._question_target(snapshot, event_id, present, reference):
                    raise ContextError("A question may reference only this event, a published event, "
                                       "or a draft or previously published record")
            current = list(job.get("questions", []))
            if any(encode(item) == encode(entry) for item in current):
                return {"event_id": event_id, "staged": True, "duplicate": True, "count": len(current)}
            current.append(entry)
            questions.entries(current, job["source_bytes"])
            job["questions"] = current
            job.update(phase="draft", error=None)
            self._save(job)
            return {"event_id": event_id, "staged": True, "duplicate": False, "count": len(current)}

    def reset_questions(self, event_id):
        """Drop the staged question output of an unpublished event (a new model attempt starts without stale questions)."""
        with self._lock():
            job = self._editable_job(event_id)
            if job.get("questions"):
                job["questions"] = []
                job.update(phase="draft", error=None)
                self._save(job)
            return {"event_id": event_id, "count": 0}

    def _publish_questions(self, job, commit):
        """Promote staged questions to the deliverable outbox once their event is in Git; idempotent for recovery."""
        staged = questions.entries(job.get("questions", []), job.get("source_bytes"))
        if staged:
            atomic_write(self._outbox_path(job["id"]),
                         encode({"format_version": FORMAT_VERSION, "event_id": job["id"],
                                 "published_commit": commit, "questions": staged}))

    def _finished_commit(self, event_id):
        """The published commit of an event whose publication has fully completed; anything else is a conflict."""
        snapshot = self.snapshot()
        commit = snapshot.event_commits.get(event_id)
        if commit is None:
            raise Conflict("This event is not published; it has no deliverable question output")
        job = self._job()
        if job is not None and job["id"] == event_id:
            raise Conflict("The published event is still completing; recover it before reading its question output")
        return commit

    def _outbox(self, event_id, commit):
        path = self._outbox_path(event_id)
        if path.is_symlink():
            raise ContextError("The question outbox entry is not a regular file")
        try:
            value = json.loads(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise ContextError("The question outbox entry is unreadable; it was retained") from error
        if (not isinstance(value, dict) or set(value) != {"format_version", "event_id", "published_commit", "questions"}
                or value["format_version"] != FORMAT_VERSION or value["event_id"] != event_id
                or value["published_commit"] != commit):
            raise ContextError("The question outbox entry does not belong to this published event; it was retained")
        return questions.entries(value["questions"])

    def question_output(self, event_id):
        """``{event_id, published_commit, questions}`` of a published event; an empty list when nothing is deliverable.

        Read-only. An unpublished, discarded or still-completing event has no deliverable output and is refused.
        """
        identifier(event_id)
        with self._lock():
            commit = self._finished_commit(event_id)
            return {"event_id": event_id, "published_commit": commit,
                    "questions": self._outbox(event_id, commit) or []}

    def acknowledge_question_output(self, event_id, published_commit):
        """Remove the delivered runtime payload of a published event; idempotent, and a different commit is refused."""
        identifier(event_id)
        modes.full_commit(published_commit, "published_commit")
        with self._lock():
            commit = self._finished_commit(event_id)
            if commit != published_commit:
                raise Conflict("The acknowledged commit is not this event's published commit")
            removed = self._outbox(event_id, commit) is not None
            if removed:
                self._outbox_path(event_id).unlink()
                directory = os.open(self.runtime / "questions", os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return {"event_id": event_id, "published_commit": commit, "acknowledged": True, "removed": removed}

    def _ensure_index(self, snapshot, before=None):
        revision = self.index.revision()
        if revision == snapshot.revision:
            return
        if before is not None and revision == before.revision:
            self.index.apply(changes_between(before, snapshot))
        else:
            self.index.rebuild(snapshot.revision, project(snapshot))

    def _checkpoint(self, phase):
        """No-op fault-injection seam for focused crash/recovery tests."""

    def _cleanup(self, job):
        directory = self._event_directory(job["id"])
        if directory.exists():
            shutil.rmtree(directory)
        self.job_path.unlink(missing_ok=True)
        descriptor = os.open(self.runtime, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _finish(self, job, commit):
        if self.git.head() != commit:
            raise Conflict("Published head moved during completion; preserve the pending record for reconciliation")
        job.update(phase="published", published_commit=commit, error=None)
        self._save(job)
        self._publish_questions(job, commit)
        self._checkpoint("after_questions")
        self.git.checkout(job["base_revision"], commit)
        self._checkpoint("after_checkout")
        before = self.snapshot(job["base_revision"])
        self._ensure_index(self.snapshot(commit), before)
        self._checkpoint("after_index")
        self._cleanup(job)
        return commit

    @staticmethod
    def _draft_digest(job):
        # Operational phase/error bookkeeping does not change the sealed input.
        return digest({key: job[key] for key in (
            "format_version", "id", "base_revision", "submission_digest", "operation", "submitted_by",
            "source", "input_digest", "source_bytes", "input_mode", "previous_source_event", "draft")}
            | {key: job[key] for key in ("action", "handoff_note", "action_associations", "record_context", "execution", "questions") if key in job})

    def inspect_draft(self, event_id, *, validate=False):
        """Inspect a pending draft; optionally run publication validation, without publishing.

        The digest binds the entire input/base/draft, including support-only changes.
        No runtime paths, source bytes, or disposable index state are returned.
        """
        with self._lock():
            job = self._editable_job(event_id)
            if validate:
                self._prepare_publication(self.snapshot(job["base_revision"]), job)
            return {"event_id": job["id"], "base_revision": job["base_revision"],
                    "operation": job["operation"], "source": job["source"],
                    "submission_digest": job["submission_digest"],
                    "input_digest": job["input_digest"], "draft": job["draft"],
                    "input_mode": job["input_mode"], "previous_source_event": job["previous_source_event"],
                    **{key: job[key] for key in ("action", "handoff_note", "action_associations", "record_context", "execution", "questions") if key in job},
                    "digest": self._draft_digest(job), "validated": validate}

    def publish(self, event_id, *, expected_draft_digest=None):
        identifier(event_id)
        with self._lock():
            job, snapshot = self._job(), self.snapshot()
            existing = snapshot.event_commits.get(event_id)
            if existing and (job is None or job["id"] != event_id):
                self._ensure_index(snapshot)
                return existing
            if job is None or job["id"] != event_id:
                raise Conflict("No matching pending event")
            try:
                if existing:
                    return self._finish(job, existing)
                if snapshot.revision != job["base_revision"]:
                    raise Conflict("Publication base changed; the draft is retained")
                self.git.require_clean()
                files = self._prepare_publication(snapshot, job)
                if (expected_draft_digest is not None and
                        expected_draft_digest != self._draft_digest(job)):
                    raise Conflict("The draft changed after propagation completed; publication refused")
                job.update(phase="ready", error=None)
                self._save(job)
                commit = self.git.commit(files, self.runtime, snapshot.revision,
                                         f"Context event {event_id}: {job['operation']}")
                self._verify_commit(job, commit)
                self._checkpoint("before_publication")
                self.git.publish(commit, snapshot.revision)
                self._checkpoint("after_publication")
                return self._finish(job, commit)
            except Exception as error:
                job["error"] = str(error)
                self._save(job)
                raise

    def _verify_commit(self, job, commit):
        """Refuse to promote a controlled supervised commit whose records differ from its bound plan."""
        plan = self._bound_plan(job)
        if plan is not None and controlled.module_for(plan) is supervised_edits:
            supervised_edits.check_commit(plan, lambda paths: self.git.read(commit, paths))

    def recover(self):
        with self._lock():
            job = self._job()
            if job is None:
                snapshot = self.snapshot()
                self._ensure_index(snapshot)
                return {"status": "idle", "revision": snapshot.revision}
            snapshot = self.snapshot()
            commit = snapshot.event_commits.get(job["id"])
            if commit:
                try:
                    return {"status": "completed", "commit": self._finish(job, commit), "event_id": job["id"]}
                except Exception as error:
                    job["error"] = str(error)
                    self._save(job)
                    raise
            if job["phase"] == "discarding":
                self._cleanup(job)
                return {"status": "discarded", "event_id": job["id"]}
            job.update(phase="interrupted", error=job["error"] or "Unpublished event retained for explicit retry/discard")
            self._save(job)
            return {"status": "needs_retry", "event_id": job["id"], "error": job["error"]}

    def discard(self, event_id):
        identifier(event_id)
        with self._lock():
            job = self._job()
            if job is None or job["id"] != event_id:
                raise Conflict("No matching pending event")
            snapshot = self.snapshot()
            if event_id in snapshot.events:
                raise Conflict("A published event must be recovered, not discarded")
            job.update(phase="discarding", error=None)
            self._save(job)
            self._cleanup(job)

    def actions_history(self, *, tag_id=None, global_only=False, audit=False,
                        offset=0, limit=20, revision=None):
        return self.snapshot(revision).actions_history(tag_id=tag_id, global_only=global_only,
                                                      audit=audit, offset=offset, limit=limit)

    def search(self, query="", *, revision=None, kind=None, attributes=None, limit=20):
        with self._lock():
            snapshot = self.snapshot(revision)
            self._ensure_index(snapshot)
            hits = self.index.search(snapshot.revision, query, kind=kind, attributes=attributes, limit=limit)
            return [replace(hit, input_mode=snapshot.input_mode(snapshot.sources[hit.id]))
                    if hit.kind == "source" else hit for hit in hits]
