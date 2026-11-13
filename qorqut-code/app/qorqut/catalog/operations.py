"""Publication plans snapshot authored bytes before any approval or application."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import difflib
import json
from pathlib import Path

from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, PreparedChange,
                                         PreviewResource, ResourceRef, TextPreview, canonical_bytes)
from qorqut_operations.storage.files import RegisteredFileTarget, file_hash, file_values
from .packages import Catalog, load_package, relative_path


class CatalogOperations:
    def __init__(self, catalog: Catalog, *, sources: dict[str, tuple[str, Path, str]]):
        self.catalog = catalog
        self.sources = dict(sources)

    def register(self, registry, application=None):
        for operation, kind in (("roles_publish", "role"), ("plugins_publish", "plugin")):
            registry.register(OperationDescriptor(operation,
                {"type": "object", "required": ["source_id"], "properties": {"source_id": {"type": "string"}, "rationale": {"type": "string", "maxLength": 2000}}},
                effect_kind="compound", scope_kinds=frozenset({"global"}),
                global_capability="publish_roles" if kind == "role" else "publish_plugins",
                description=instruction_text('operations-publish-an-exact-immutable-authored-revision-without-updating'),
                prepare=lambda ctx, args, options, uow, k=kind: self.prepare(ctx, args, options, uow, k),
                apply=self.apply, validate_targets=self.validate_targets))
        if application:
            application.register_target_resolver("artifacts", self.resolve_record)
            application.register_target_resolver("artifact_files", self.resolve_file_snapshot)

    def resolve_record(self, ctx, resource, uow):
        try:
            kind, identity, version = json.loads(resource.resource_id)
        except (ValueError, TypeError) as exc:
            raise DomainError("invalid_plan", "Invalid artifact record reference") from exc
        row = uow.connection.execute("SELECT * FROM artifacts WHERE kind=? AND artifact_id=? AND version=?", (kind, identity, version)).fetchone()
        return {**dict(row), "content_hash": row["digest"]} if row else None

    def resolve_file_target(self, resource) -> RegisteredFileTarget:
        if resource.resource_type != "artifact_files" or resource.namespace != "global" or resource.business_id is not None:
            raise DomainError("invalid_plan", "Not an artifact file resource")
        try:
            kind, identity, version, digest, path = json.loads(resource.resource_id)
            if kind not in {"role", "plugin"}:
                raise ValueError
            from .packages import ID, VERSION, validate_schema
            validate_schema(identity, ID, "artifact ID")
            validate_schema(version, VERSION, "artifact version")
            if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:") or any(char not in "0123456789abcdef" for char in digest[7:]):
                raise ValueError
            relative = f"artifacts/{kind}s/{identity}/{version}/{digest[7:]}/{relative_path(path)}"
        except (ValueError, TypeError) as exc:
            raise DomainError("invalid_plan", "Invalid artifact file reference") from exc
        return RegisteredFileTarget(self.catalog.root, relative)

    def resolve_file_snapshot(self, ctx, resource, uow):
        digest = file_hash(self.resolve_file_target(resource).resolve())
        return {"content_hash": digest} if digest is not None else None

    def prepare(self, ctx, args, options, uow, kind: str):
        capability = "publish_roles" if kind == "role" else "publish_plugins"
        if capability not in ctx.global_capabilities:
            raise DomainError("forbidden", f"Publishing requires {capability} global capability")
        source = self.sources.get(args["source_id"])
        if source is None or source[0] != kind:
            raise DomainError("not_found", "Authored source is not registered for this artifact kind")
        package = load_package(source[1], source[2], kind, self.catalog.registry)
        if kind == "role" and "business" in package.manifest["scope_kinds"]:
            # Businesses own resources only; existing immutable business-role artifacts stay readable.
            raise DomainError("retired_role", "Business-scoped roles are retired; only global roles are published")
        record_id = canonical_bytes([kind, package.id, package.version]).decode()
        record = ResourceRef("artifacts", record_id, namespace="global")
        current = self.resolve_record(ctx, record, uow)
        if current and current["digest"] != package.digest:
            raise DomainError("immutable_artifact", "Changed bytes cannot be published under an existing artifact version")
        expectations = [ExpectedTarget(record, exists=current is not None, content_hash=package.digest if current else None)]
        changes, previews, resources = [], [], []
        for path, content in sorted(package.files.items()):
            ref = ResourceRef("artifact_files", canonical_bytes([kind, package.id, package.version, package.digest, path]).decode(), namespace="global")
            target = self.resolve_file_target(ref).resolve()
            before = file_hash(target)
            after = file_values(content, before_hash=before)
            if before is not None and before != after["after_hash"]:
                raise DomainError("artifact_conflict", f"Unregistered artifact bytes conflict with publication: {path}")
            expectations.append(ExpectedTarget(ref, content_hash=before, exists=before is not None))
            changes.append(PreparedChange("file", ref, "write", after))
            preview_resource = PreviewResource("artifact_files", ref.resource_id, f"{kind} {package.id}@{package.version}/{path}")
            resources.append(preview_resource)
            try:
                after_text = content.decode("utf-8")
                before_text = target.read_text() if before else ""
                previews.append(TextPreview(preview_resource, before_text, after_text,
                    "".join(difflib.unified_diff(before_text.splitlines(keepends=True), after_text.splitlines(keepends=True), fromfile=path, tofile=path))))
            except UnicodeDecodeError:
                from qorqut_operations.contracts import FieldChange, StructuredPreview
                previews.append(StructuredPreview(preview_resource, (FieldChange("content_hash", before, after["after_hash"]), FieldChange("bytes", 0 if before is None else target.stat().st_size, len(content)))))
        values = {"kind": kind, "artifact_id": package.id, "version": package.version, "digest": package.digest,
                  "manifest_schema_version": 1, "stored_path": str(self.catalog.artifact_path(package)),
                  "metadata_json": {"manifest_path": package.manifest_path, "source_id": args["source_id"]}}
        changes.append(PreparedChange("record", record, "publish", values))
        operation = "roles_publish" if kind == "role" else "plugins_publish"
        return ChangeSet(operation, 1, ctx.principal_id, "global", args, {"kind": "global", "artifact": package.ref},
            tuple(expectations), tuple(changes), ChangePreview(f"Publish {kind} {package.id}@{package.version}", args.get("rationale", ""),
                {"kind": "global", "artifact": package.ref}, tuple(resources), tuple(previews),
                ("Publishes the exact reviewed files. Existing profile revision pins do not change.",)),
            review_business_id=options.review_business_id or ctx.review_business_id,
            proposer_agent_id=ctx.agent_id)

    def apply(self, ctx, plan, uow):
        for change in plan.changes:
            if change.kind == "file":
                if file_hash(self.resolve_file_target(change.resource).resolve()) != change.values["after_hash"]:
                    raise DomainError("artifact_conflict", "Published file effects are not fully verified")
        record = next(change for change in plan.changes if change.kind == "record")
        values = dict(record.values)
        current = self.resolve_record(ctx, record.resource, uow)
        if current:
            if current["digest"] != values["digest"]:
                raise DomainError("immutable_artifact", "Existing artifact version cannot be replaced")
        else:
            uow.repository("artifacts").insert(values)
        return {"artifact": {"id": values["artifact_id"], "version": values["version"], "digest": values["digest"]}, "published": True}

    def validate_targets(self, ctx, plan, uow):
        record = next((change for change in plan.changes if change.kind == "record"), None)
