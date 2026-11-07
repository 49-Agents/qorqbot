"""Registered media uploads, metadata, and archival without deleting bytes."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import base64
import binascii
import mimetypes
from pathlib import PurePosixPath

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.application.plans import PAGE, TEXT, UUID, PlanBuilder, apply_records, page, rows, schema, utc_clock
from qorqut_operations.assets.validation import filename
from qorqut_operations.documents.serialization import HASH, SLUG
from qorqut_operations.records.associations import TASK_IDS, link_new_resource

MAX_BYTES = 100 * 1024 * 1024
META = {"notes": {"type": "string", "maxLength": 300}, "batch": {"type": "string", "maxLength": 40}}


class AssetOperations:
    """``document_store`` is the host's source store and ``additional_references(row, ctx, uow)`` the host source of the
    ``used_by`` entries beyond Operations documents (a list of resource dictionaries for the already-authorized asset
    ``row``); both are required, and a standalone fixture with no other reference source passes ``lambda row, ctx, uow: []``."""

    def __init__(self, document_store, *, additional_references, clock=utc_clock):
        if not callable(additional_references):
            raise TypeError("Asset operations require a callable additional_references(row, ctx, uow)")
        self.store, self.additional_references, self.clock = document_store, additional_references, clock

    def register(self, registry, application=None):
        registry.register(OperationDescriptor("assets_list", schema({**PAGE, "group": TEXT, "topic": SLUG, "batch": META["batch"]}),
            effect_kind="read", supports_signoff=False, read=lambda ctx, args, options, uow: self.read("assets_list", ctx, args, uow)))
        registry.register(OperationDescriptor("assets_get", schema({"asset_id": UUID}, ("asset_id",)),
            effect_kind="read", supports_signoff=False, read=lambda ctx, args, options, uow: self.read("assets_get", ctx, args, uow)))
        mutations = {
            "assets_upload": ({"filename": TEXT, "content_b64": {"type": "string", "maxLength": ((MAX_BYTES + 2) // 3) * 4},
                "media_type": {"type": "string", "pattern": "^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$", "maxLength": 150},
                "group": {"enum": ["polished", "drafted"]}, "topic": SLUG, "task_ids": TASK_IDS, **META}, ("filename", "content_b64", "group")),
            "assets_update": ({"asset_id": UUID, "changes": {**schema(META), "minProperties": 1}, "expected_content_hash": HASH}, ("asset_id", "changes")),
            "assets_delete": ({"asset_id": UUID, "expected_content_hash": HASH}, ("asset_id",)),
        }
        for name, (properties, required) in mutations.items():
            registry.register(OperationDescriptor(name, schema({**properties, "rationale": TEXT}, required), effect_kind="files",
                description=instruction_text('operations-archive-an-asset-while-preserving-its-bytes-and') if name == "assets_delete" else "",
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply))

    def references(self, row, ctx, uow):
        found = []
        for document in rows(uow, "documents", ctx.business_id):
            meta = document["metadata_json"]
            if row["id"] in meta.get("asset_ids", []) or row["path"] in meta.get("assets", []):
                found.append({"resource_type": "documents", "resource_id": document["id"], "title": meta.get("title", document["path"])})
        found.extend(self.additional_references(row, ctx, uow))
        return found

    def view(self, row, ctx, uow):
        return {**self.store.view(row, uow, include_body=False), "used_by": self.references(row, ctx, uow)}

    def read(self, operation, ctx, args, uow):
        if operation == "assets_get":
            return {"asset": self.view(self.store.get(ctx, args["asset_id"], uow, kinds={"asset"}), ctx, uow)}
        found = self.store.collection(ctx, uow, kinds={"asset"})
        for key in ("group", "topic", "batch"):
            if key in args: found = [row for row in found if row["metadata_json"].get(key) == args[key]]
        selected, cursor = page(found, args, ctx.business_id, operation)
        return {"items": [self.view(row, ctx, uow) for row in selected], "next_cursor": cursor,
                "groups": sorted({row["metadata_json"].get("group", "") for row in found})}

    def prepare(self, operation, ctx, args, options, uow):
        builder = PlanBuilder(ctx, operation, args, operation.replace("_", " ").title(), clock=self.clock)
        if operation == "assets_upload":
            try: data = base64.b64decode(args["content_b64"], validate=True)
            except (binascii.Error, ValueError) as exc: raise DomainError("validation_error", "Asset content must be valid base64") from exc
            if not data or len(data) > MAX_BYTES: raise DomainError("validation_error", "Assets must contain 1 byte to 100 MiB")
            name = filename(args["filename"])
            prefix = "assets/" + args["group"] + ("/" + args["topic"] if args.get("topic") else "")
            path = self.store.free_path(uow, ctx.business_id, prefix, PurePosixPath(name).stem, suffix=PurePosixPath(name).suffix, builder=builder)
            metadata = {key: value for key, value in args.items() if key in {"group", "topic", "notes", "batch"}}
            metadata.update(filename=PurePosixPath(path).name, media_type=args.get("media_type") or mimetypes.guess_type(name)[0] or "application/octet-stream",
                            size=len(data), at=builder.at, by=ctx.principal_id)
            self.store.write(builder, uow, kind="asset", path=path, data=data, metadata=metadata)
            if args.get("task_ids"):
                created = next(change for change in builder.changes if change.resource.resource_type == "documents")
                link_new_resource(builder, uow, "documents", created.resource.resource_id, args["task_ids"])
        else:
            row = self.store.get(ctx, args["asset_id"], uow, kinds={"asset"})
            if operation == "assets_delete":
                self.store.archive(builder, uow, row, expected_revision=options.expected_revision, expected_content_hash=args.get("expected_content_hash"))
            else:
                # Asset bytes stay immutable under metadata edits and retain their stable registered path.
                self.store.write(builder, uow, kind="asset", before=row, data=self.store.read_bytes(row, uow),
                    metadata={**row["metadata_json"], **args["changes"]}, expected_revision=options.expected_revision,
                    expected_content_hash=args.get("expected_content_hash"))
        return builder.build()

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        change = next(change for change in plan.changes if change.resource.resource_type == "documents")
        row = uow.repository("documents", business_id=ctx.business_id).get(change.resource.resource_id, required=True)
        return {"asset": {"id": row["id"], "path": row["path"], "revision": row["revision"], "archived": bool(row["archived"]), "content_hash": row["content_hash"],
                          "metadata": row["metadata_json"], "file_resource": self.store.view({**row, "availability": "ready"}, uow, include_body=False)["file_resource"]}}
