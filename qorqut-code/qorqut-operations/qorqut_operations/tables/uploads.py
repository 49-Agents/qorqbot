"""Original business data files, separate from context filing and marketing assets."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import base64
import binascii
import hashlib
import mimetypes
from pathlib import PurePosixPath

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.assets.validation import filename
from qorqut_operations.application.plans import PAGE, TEXT, UUID, PlanBuilder, apply_records, page, schema, utc_clock
from qorqut_operations.records.associations import TASK_IDS, link_new_resource

MAX_BYTES = 20 * 1024 * 1024


class DataUploadOperations:
    def __init__(self, store, *, clock=utc_clock):
        self.store, self.clock = store, clock

    def register(self, registry):
        for name, fields, required in (("data_uploads_list", {**PAGE, "query": TEXT}, ()),
                                      ("data_uploads_get", {"upload_id": UUID}, ("upload_id",))):
            registry.register(OperationDescriptor(name, schema(fields, required), effect_kind="read", supports_signoff=False,
                description=instruction_text('uploads-find-original-shared-data-uploads-and-their-registered'),
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        registry.register(OperationDescriptor("data_uploads_upload", schema({"filename": TEXT,
            "content_b64": {"type": "string", "maxLength": ((MAX_BYTES + 2) // 3) * 4},
            "notes": {"type": "string", "maxLength": 10000}, "task_ids": TASK_IDS}, ("filename", "content_b64")),
            effect_kind="files", description=instruction_text('uploads-preserve-an-original-csv-spreadsheet-or-other-data'),
            prepare=self.prepare, apply=self.apply))

    def view(self, row, uow):
        result = self.store.view(row, uow, include_body=False)
        resource = self.store.file_ref(row["id"], row["path"], row["business_id"])
        result["local_path"] = str(self.store._file_target(resource, uow).resolve())
        result["tables"] = [dict(item) for item in uow.connection.execute(
            "SELECT DISTINCT t.id,t.title FROM data_tables t JOIN data_rows r ON r.business_id=t.business_id AND r.table_id=t.id "
            "WHERE r.business_id=? AND r.source_upload_id=? ORDER BY t.title,t.id", (row["business_id"], row["id"]))]
        return result

    def read(self, operation, ctx, args, uow):
        if operation == "data_uploads_get":
            return {"upload": self.view(self.store.get(ctx, args["upload_id"], uow, kinds={"data_upload"}), uow)}
        found = self.store.collection(ctx, uow, kinds={"data_upload"})
        query = args.get("query", "").casefold()
        found = [row for row in found if query in (row["metadata_json"].get("filename", "") + " " + row["metadata_json"].get("notes", "")).casefold()]
        selected, cursor = page(found, args, ctx.business_id, operation)
        return {"items": [self.view(row, uow) for row in selected], "next_cursor": cursor}

    def prepare(self, ctx, args, options, uow):
        try:
            data = base64.b64decode(args["content_b64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DomainError("validation_error", "Upload content must be valid base64") from exc
        if not data or len(data) > MAX_BYTES:
            raise DomainError("validation_error", "Data uploads must contain 1 byte to 20 MiB")
        builder = PlanBuilder(ctx, "data_uploads_upload", args, "Upload project data", clock=self.clock)
        name = filename(args["filename"])
        path = self.store.free_path(uow, ctx.business_id, "data-uploads", PurePosixPath(name).stem,
            suffix=PurePosixPath(name).suffix, builder=builder)
        metadata = {"filename": str(args["filename"]).replace("\\", "/").split("/")[-1],
            "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream", "size": len(data),
            "notes": args.get("notes", ""), "by": ctx.principal_id, "at": builder.at}
        identity = self.store.write(builder, uow, kind="data_upload", path=path, data=data, metadata=metadata)
        link_new_resource(builder, uow, "documents", identity, args.get("task_ids", ()))
        return builder.build()

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        identity = next(change.resource.resource_id for change in plan.changes if change.resource.resource_type == "documents")
        row = uow.repository("documents", business_id=ctx.business_id).get(identity, required=True)
        return {"upload": self.view({**row, "availability": "ready"}, uow)}

    def download(self, ctx, identity, uow):
        row = self.store.get(ctx, identity, uow, kinds={"data_upload"})
        data = self.store.read_bytes(row, uow)
        if hashlib.sha256(data).hexdigest() != row["content_hash"].removeprefix("sha256:"):
            raise DomainError("revision_conflict", "Original upload was changed outside the service")
        return row["metadata_json"]["filename"], data
