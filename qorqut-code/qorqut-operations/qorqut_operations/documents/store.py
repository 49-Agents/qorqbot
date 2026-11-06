"""Registered authored files and exact revision/hash-bound document plans."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import PurePosixPath

from qorqut_operations.application.plans import UNSET, ref, require_revision, rows
from qorqut_operations.contracts import (DomainError, ExpectedTarget, PreparedChange, PreviewResource, ResourceRef,
                                         TextPreview, canonical_bytes, new_id, plain)
from qorqut_operations.documents.serialization import BINARY_KINDS, MAX_TEXT_BYTES, document_bytes, split_document
from qorqut_operations.relationships import related_rows
from qorqut_operations.storage.files import RegisteredFileTarget, file_hash, file_values
from qorqut_operations.storage.safe_io import read_bytes as read_file_bytes
from . import business_notes
from .journal_access import JOURNAL_KINDS, is_owner, require_writer
from .materials import classification
from .people import affiliation_effect, details as person_details
from .relationships import LEGACY_FIELDS, projection


class DocumentStore:
    """Registered authored files. ``document_root(business_row_or_none, relative_path)`` is the one host-bound selection of
    the root a registered document path is relative to; ``operations_root`` is the Global authored root."""

    def __init__(self, database, *, operations_root, document_root):
        if not callable(document_root):
            raise TypeError("The document store needs a bound document_root(business, relative_path)")
        self.database = database
        self.root = operations_root
        self._document_root = document_root

    def file_ref(self, identity, path, business_id, *, namespace=None):
        return ResourceRef("managed_document_files", canonical_bytes({"document_id": identity, "path": path}).decode(),
                           business_id, namespace)

    def _file_target(self, resource, uow):
        if resource.resource_type != "managed_document_files":
            raise DomainError("invalid_plan", "Not a managed document file")
        try:
            value = json.loads(resource.resource_id)
            if set(value) != {"document_id", "path"} or not all(isinstance(item, str) and item for item in value.values()):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise DomainError("invalid_plan", "Invalid registered document file reference") from exc
        business = None if resource.business_id is None else uow.repository("businesses").get(resource.business_id, required=True)
        return RegisteredFileTarget(self._document_root(business, value["path"]), value["path"])

    def resolve_file_target(self, resource):
        with self.database.read() as uow:
            return self._file_target(resource, uow)

    def resolve_snapshot(self, ctx, resource, uow):
        digest = file_hash(self._file_target(resource, uow).resolve())
        return {"content_hash": digest} if digest is not None else None

    def get(self, ctx, identity, uow, *, kinds=None, write=False):
        row = uow.repository("documents", business_id=ctx.business_id).get(identity)
        if row is None and ctx.business_id is None and is_owner(ctx, uow):
            saved = uow.connection.execute("SELECT business_id,kind FROM documents WHERE id=?", (identity,)).fetchone()
            if saved and saved["kind"] in JOURNAL_KINDS:
                row = uow.repository("documents", business_id=saved["business_id"]).get(identity)
        if row is None and ctx.business_id is not None:
            row = uow.repository("documents", business_id=None).get(identity)
        if row is None:
            raise DomainError("not_found", "Document not found")
        owner_journal = row["kind"] in JOURNAL_KINDS
        if owner_journal and not is_owner(ctx, uow):
            # Ordubashy has separate, explicitly selected journal review tools.
            # Generic document readers must not leak hidden entries.
            raise DomainError("not_found", "Document not found")
        if row['kind'] == 'working_memory':
            # Dedicated memory operations validate provenance, drift and history.
            # Generic document APIs must not become a second reader/writer.
            raise DomainError('not_found', 'Document not found')
        if row["business_id"] is not None and not (owner_journal and ctx.business_id is None):
            if row["business_id"] != ctx.business_id or row["business_id"] not in ctx.allowed_business_ids:
                raise DomainError("not_found", "Document not found")
        elif row["owning_agent_id"] != ctx.agent_id:
            if "read_global_context" not in ctx.global_capabilities:
                raise DomainError("not_found", "Document not found")
        if kinds is not None and row["kind"] not in kinds:
            raise DomainError("validation_error", "Document has a different registered kind")
        if write and ctx.principal_kind == "agent" and row["kind"] in {"plan", "agent_note"}:
            if row["owning_agent_id"] != ctx.agent_id:
                raise DomainError("forbidden", "Agents can edit only their own plans and notes")
        if row["availability"] != "ready":
            raise DomainError("busy", "Document has an unfinished application", retryable=True)
        return row

    def collection(self, ctx, uow, *, kinds=None, agent_id=UNSET, include_archived=False):
        result = rows(uow, "documents", ctx.business_id)
        if not is_owner(ctx, uow):
            result = [row for row in result if row["kind"] not in JOURNAL_KINDS]
        result = [row for row in result if row['kind'] != 'working_memory']
        if not include_archived: result = [row for row in result if not row.get("archived")]
        if kinds is not None:
            result = [row for row in result if row["kind"] in kinds]
        if agent_id is not UNSET:
            result = [row for row in result if row["owning_agent_id"] == agent_id]
        return [{**row, "metadata_json": projection(row, uow)} if row["kind"] in {"context_topic", "context_note", "context_person", "content"} else row for row in result]

    def read_bytes(self, row, uow):
        resource = self.file_ref(row["id"], row["path"], row["business_id"])
        path = self._file_target(resource, uow).resolve()
        if not path.is_file():
            raise DomainError("document_unavailable", "Registered document file is missing")
        if row["kind"] not in BINARY_KINDS and path.stat().st_size > MAX_TEXT_BYTES:
            raise DomainError("invalid_document", "Registered text exceeds size limit")
        try:
            return read_file_bytes(path, limit=None if row["kind"] in BINARY_KINDS else MAX_TEXT_BYTES)
        except DomainError as exc:
            if exc.code == "invalid_configuration":
                raise DomainError("invalid_document", "Registered text exceeds size limit") from None
            raise

    @staticmethod
    def related_business(row, uow):
        related_id = row.get("metadata_json", {}).get("related_business_id")
        if not related_id:
            return None
        related = uow.repository("businesses").get(related_id)
        if related is None:
            return None
        return {"business_id": related["id"], "business_slug": related["slug"],
                "business_name": related["name"]}

    def view(self, row, uow, *, include_body=True):
        if row["availability"] != "ready":
            raise DomainError("busy", "Document has an unfinished application", retryable=True)
        data = self.read_bytes(row, uow) if include_body else None
        result = {key: value for key, value in row.items() if key not in {"metadata_json", "source_refs_json", "owning_agent_id"}}
        result.update(metadata=plain(row["metadata_json"]), source_refs=plain(row["source_refs_json"]), agent_id=row["owning_agent_id"])
        result["file_resource"] = plain(self.file_ref(row["id"], row["path"], row["business_id"]))
        if data is not None:
            digest = hashlib.sha256(data).hexdigest()
            result["indexed_content_hash"] = row["content_hash"]
            result["content_hash"] = digest
            result["metadata_stale"] = row["content_hash"].removeprefix("sha256:") != digest
            if row["kind"] not in BINARY_KINDS:
                frontmatter, result["body"] = split_document(data)
                if frontmatter:
                    result["metadata"] = {**result["metadata"], **frontmatter}
        if row["kind"] == "agent_note":
            related_business = self.related_business(row, uow)
            if related_business is not None:
                result["related_business"] = related_business
        if row["kind"] in {"context_topic", "context_note", "context_person", "content"}:
            result["metadata"] = projection({**row, "metadata_json": result["metadata"]}, uow)
        # Classification is registry metadata, not an instruction embedded in source text.
        material = classification(row)
        if material:
            result.update(material)
            result["metadata"].update(material)
        if row["kind"] == "context_person":
            result["metadata"].update(person_details(row, uow))
        if row["business_id"] is not None and row["kind"] in {"content", "context_note", "email_message"}:
            sources = [edge["to_id"] for edge in related_rows(uow, row["business_id"],
                source={"type": "material", "id": row["id"]}, role="extracted_from") if edge["to_type"] == "signoff"]
            result["signoff_id"] = sources[0] if len(sources) == 1 else None
        return result

    def _file(self, builder, uow, identity, path, business_id, before, after, *, namespace=None, label=None, binary=False):
        resource = self.file_ref(identity, path, business_id, namespace=namespace)
        actual = file_hash(self._file_target(resource, uow).resolve())
        digest = hashlib.sha256(before).hexdigest() if before is not None else None
        if actual != digest:
            raise DomainError("revision_conflict", "Managed document file changed during preparation")
        builder.targets[resource] = ExpectedTarget(resource, content_hash=digest, exists=before is not None)
        builder.changes.append(PreparedChange("file", resource, "delete" if after is None else "write",
                                             file_values(after, before_hash=digest)))
        preview = PreviewResource("documents", identity, label or path)
        builder.resources.append(preview)
        if binary:
            builder.effects.append(f"{'Deletes' if after is None else 'Writes'} asset {path}: {len(after or b'')} bytes.")
            return resource
        try:
            old, new = (before or b"").decode(), (after or b"").decode()
        except UnicodeDecodeError:
            builder.effects.append(f"{'Deletes' if after is None else 'Writes'} asset {path}: {len(after or b'')} bytes.")
        else:
            diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=path, tofile=path))
            builder.previews.append(TextPreview(preview, old, new, diff))
        return resource

    def write(self, builder, uow, *, kind, body=None, metadata=None, data=None, before=None, identity=None,
              path=None, expected_revision=None, expected_content_hash=None, agent_id=None,
              business_id=UNSET, namespace=None, source_refs=None, operation_name=None):
        if kind in JOURNAL_KINDS or before and before["kind"] in JOURNAL_KINDS:
            require_writer(builder.ctx, uow, builder.operation)
        if before and before['kind'] == 'plan' and before['metadata_json'].get('working_memory_id'):
            raise DomainError('plan_migrated', 'Legacy plan is frozen; use working_memory_update')
        business_id = builder.ctx.business_id if business_id is UNSET else business_id
        business_notes.require_project_access(builder.ctx, before, kind, business_id)
        identity = identity or (before["id"] if before else new_id())
        require_revision(before, expected_revision)
        if before and before["availability"] != "ready":
            raise DomainError("busy", "Document has an unfinished application", retryable=True)
        path = path or (before["path"] if before else None)
        if path is None:
            raise DomainError("invalid_plan", "New documents need a trusted generated path")
        old = self.read_bytes(before, uow) if before else None
        observed = hashlib.sha256(old).hexdigest() if old is not None else None
        if before:
            indexed = before["content_hash"].removeprefix("sha256:")
            if expected_content_hash is not None and expected_content_hash.removeprefix("sha256:") != observed:
                raise DomainError("revision_conflict", "Document changed since it was read")
            if indexed != observed and expected_content_hash is None:
                raise DomainError("revision_conflict", "Document was edited outside the service; read it and supply its observed content hash")
        metadata = plain(metadata if metadata is not None else (before or {}).get("metadata_json", {}))
        if kind == "context_person":
            old_affiliation = (before or {}).get("metadata_json", {}).get("affiliation", "external")
            explicit = (builder.arguments.get("changes", builder.arguments))
            if metadata.get("affiliation", "external") != old_affiliation or "affiliation" in explicit:
                affiliation_effect(builder, ref("documents", identity, business_id))

        metadata.update(classification(before) if before else classification({"kind": kind, "business_id": business_id, "owning_agent_id": agent_id,
            "namespace": business_id or "global", "metadata_json": metadata, "source_refs_json": source_refs or []}))
        metadata = {key: value for key, value in metadata.items() if key not in LEGACY_FIELDS | {"topic_ids", "parent_ids"}}
        if data is None:
            if body is None:
                body = split_document(old)[1] if old is not None else ""
            data = document_bytes(metadata, body, frontmatter=kind != "plan")
        if before and before["path"] != path:
            self._file(builder, uow, identity, before["path"], business_id, old, None, namespace=namespace, binary=kind in BINARY_KINDS)
            self._file(builder, uow, identity, path, business_id, None, data, namespace=namespace, binary=kind in BINARY_KINDS)
        else:
            self._file(builder, uow, identity, path, business_id, old, data, namespace=namespace, binary=kind in BINARY_KINDS)
        values = {"kind": kind, "path": path, "content_hash": hashlib.sha256(data).hexdigest(),
                  "metadata_json": metadata, "source_refs_json": source_refs if source_refs is not None else (before or {}).get("source_refs_json", []),
                  "updated_at": builder.at}
        if before is None:
            values.update(id=identity, business_id=business_id, namespace=business_id or "global",
                          owning_agent_id=agent_id, availability="ready", created_at=builder.at)
        builder.record("documents", identity, before, values, business_id=business_id, namespace=namespace, operation_name=operation_name)
        return identity

    def archive(self, builder, uow, row, *, expected_revision, expected_content_hash=None, namespace=None, operation_name=None, archived=True):
        if row["kind"] in JOURNAL_KINDS:
            require_writer(builder.ctx, uow, builder.operation)
        business_notes.require_project_access(builder.ctx, row)
        require_revision(row, expected_revision)
        if expected_content_hash is not None:
            snapshot = self.resolve_snapshot(builder.ctx, self.file_ref(row["id"], row["path"], row["business_id"]), uow)
            if snapshot is None or snapshot["content_hash"] != expected_content_hash.removeprefix("sha256:"):
                raise DomainError("revision_conflict", "Document changed since it was read")
        builder.record("documents", row["id"], row, {"archived": archived, "updated_at": builder.at},
            business_id=row["business_id"], namespace=namespace, operation_name=operation_name)

    def free_path(self, uow, business_id, prefix, name, *, suffix=".md", builder=None):
        existing = {row["path"] for row in rows(uow, "documents", business_id)}
        if builder:
            existing.update(change.values["path"] for change in builder.changes
                            if change.resource.resource_type == "documents" and "path" in change.values)
        for number in range(1, 10001):
            path = str(PurePosixPath(prefix) / (name + (f"-{number}" if number > 1 else "") + suffix))
            target = self._file_target(self.file_ref("new", path, business_id), uow).resolve()
            if path not in existing and not target.exists():
                return path
        raise DomainError("conflict", "Cannot allocate an unused document path")
