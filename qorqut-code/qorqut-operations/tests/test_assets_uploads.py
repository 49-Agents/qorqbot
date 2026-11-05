"""Assets and data uploads at their boundary with the host's source store, and the table rows that cite an upload.

The store is the host's source-document store, supplied explicitly; here a recording stand-in answers the store protocol
so the assertions are about what the asset and upload operations ask of it, validate and return. No file is written."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from operational_support import BUSINESS
from qorqut_operations.assets import AssetOperations
from qorqut_operations.assets.operations import MAX_BYTES as ASSET_MAX_BYTES
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain
from qorqut_operations.tables import DataUploadOperations
from qorqut_operations.tables.uploads import MAX_BYTES as UPLOAD_MAX_BYTES
from tables_support import COLUMNS, TablesHost

NOW = "2026-10-12T12:00:00.000000Z"


def encoded(data):
    return base64.b64encode(data).decode()


class StubStore:
    """Answers the source-store protocol for the asset and upload operations and records every request made of it."""

    def __init__(self, root):
        self.root, self.calls, self.documents, self.unreadable = root, [], {}, False

    def add(self, kind, path, *, metadata=None, identity=None, data=b"bytes", business_id=BUSINESS, availability="ready", revision=1):
        identity = identity or new_id()
        self.documents[identity] = {"id": identity, "business_id": business_id, "kind": kind, "path": path, "revision": revision,
                                    "availability": availability, "content_hash": hashlib.sha256(data).hexdigest(),
                                    "metadata_json": metadata or {}, "archived": False, "created_at": NOW, "data": data}
        return self.documents[identity]

    def file_ref(self, identity, path, business_id, *, namespace=None):
        return {"document_id": identity, "path": path, "business_id": business_id}

    def _file_target(self, resource, uow):
        return self.root / resource["path"]

    def view(self, row, uow, *, include_body=True):
        self.calls.append(("view", row["id"], include_body))
        return {"id": row["id"], "path": row["path"], "kind": row["kind"], "revision": row["revision"], "content_hash": row["content_hash"],
                "metadata": plain(row["metadata_json"]), "file_resource": self.file_ref(row["id"], row["path"], row["business_id"]),
                "availability": row["availability"], "created_at": row.get("created_at", NOW)}

    def get(self, ctx, identity, uow, *, kinds=None, write=False):
        row = self.documents.get(identity)
        if row is None or (kinds is not None and row["kind"] not in kinds):
            raise DomainError("not_found", "Document not found")
        return row

    def collection(self, ctx, uow, *, kinds=None, agent_id=None, include_archived=False):
        return [row for row in self.documents.values() if row["kind"] in kinds]

    def read_bytes(self, row, uow):
        if self.unreadable:
            raise DomainError("document_unavailable", "Registered document file is missing")
        return row["data"]

    def free_path(self, uow, business_id, prefix, name, *, suffix=".md", builder=None):
        self.calls.append(("free_path", business_id, prefix, name, suffix))
        return f"{prefix}/{name}{suffix}"

    def write(self, builder, uow, *, kind, data=None, metadata=None, path=None, before=None, **kwargs):
        identity = before["id"] if before else new_id()
        self.calls.append(("write", kind, path or before["path"], data, plain(metadata), {**kwargs, "before": before and before["id"]}))
        builder.record("documents", identity, None if before is None else {"revision": before["revision"]},
                       {"id": identity, "business_id": builder.ctx.business_id, "kind": kind, "path": path or before["path"],
                        "content_hash": hashlib.sha256(data).hexdigest(), "availability": "ready", "created_at": NOW, "updated_at": NOW})
        return identity

    def archive(self, builder, uow, row, *, expected_revision, expected_content_hash=None):
        self.calls.append(("archive", row["id"], expected_revision, expected_content_hash))


@pytest.fixture
def host(tmp_path):
    return TablesHost(tmp_path)


@pytest.fixture
def store(tmp_path):
    return StubStore(tmp_path)


def options(**kwargs):
    return InvocationOptions(idempotency_key=new_id(), **kwargs)


def prepare(host, operation, descriptor_owner, arguments, **kwargs):
    with host.database.read() as uow:
        return descriptor_owner.prepare(*((operation,) if operation else ()), host.owner(), arguments, options(**kwargs), uow)


def failure(call, *args):
    with pytest.raises(DomainError) as raised:
        call(*args)
    return raised.value.code, str(raised.value)


# -- construction -------------------------------------------------------------------------------------------------

def test_the_host_reference_source_is_required_and_must_be_callable(store):
    with pytest.raises(TypeError):
        AssetOperations(store)
    with pytest.raises(TypeError):
        AssetOperations(store, additional_references=None)
    with pytest.raises(TypeError):
        AssetOperations(store, additional_references=["not callable"])
    assert AssetOperations(store, additional_references=lambda row, ctx, uow: []).store is store


# -- asset references ---------------------------------------------------------------------------------------------

def test_references_keep_document_entries_first_then_the_host_entries_in_order(host, store):
    asset = store.add("asset", "assets/polished/logo.png")
    seen = []
    extras = [{"resource_type": "signoffs", "resource_id": "b", "state": "pending"}, {"resource_type": "signoffs", "resource_id": "a", "state": "applied"},
              {"resource_type": "signoffs", "resource_id": "b", "state": "pending"}]

    def additional(row, ctx, uow):
        seen.append((row["id"], ctx.business_id, uow))
        return extras

    assets = AssetOperations(store, additional_references=additional)
    with host.database.read() as uow:
        # The synthetic documents table carries no metadata column; supply the stored documents the loop reads.
        stored = [{"id": "11111111-1111-4111-8111-111111111111", "path": "notes/11.md", "metadata_json": {"title": "By ID", "asset_ids": [asset["id"]]}},
                  {"id": "22222222-2222-4222-8222-222222222222", "path": "notes/22.md", "metadata_json": {"assets": ["assets/polished/logo.png"]}},
                  {"id": "33333333-3333-4333-8333-333333333333", "path": "notes/33.md", "metadata_json": {"asset_ids": ["someone-else"]}}]
        with patch("qorqut_operations.assets.operations.rows", return_value=stored):
            found = assets.references(asset, host.owner(), uow)
        assert seen == [(asset["id"], BUSINESS, uow)]
    assert found == [{"resource_type": "documents", "resource_id": "11111111-1111-4111-8111-111111111111", "title": "By ID"},
                     {"resource_type": "documents", "resource_id": "22222222-2222-4222-8222-222222222222", "title": "notes/22.md"}, *extras]


def test_a_host_reference_failure_is_not_swallowed(host, store):
    asset = store.add("asset", "assets/drafted/a.png")

    def broken(row, ctx, uow):
        raise DomainError("invalid_plan", "Platform proposal has an invalid dispatch binding")

    assets = AssetOperations(store, additional_references=broken)
    with host.database.read() as uow:
        assert failure(assets.view, asset, host.owner(), uow) == ("invalid_plan", "Platform proposal has an invalid dispatch binding")


def test_asset_reads_filter_group_and_page_with_references(host, store):
    first = store.add("asset", "assets/polished/a.png", metadata={"group": "polished", "topic": "logo", "batch": "b1"})
    second = store.add("asset", "assets/drafted/b.png", metadata={"group": "drafted", "batch": "b2"})
    store.add("note", "notes/x.md", metadata={"group": "polished"})
    assets = AssetOperations(store, additional_references=lambda row, ctx, uow: [{"resource_type": "signoffs", "resource_id": row["id"]}])
    with host.database.read() as uow:
        listed = assets.read("assets_list", host.owner(), {}, uow)
        assert {item["id"] for item in listed["items"]} == {first["id"], second["id"]} and listed["groups"] == ["drafted", "polished"]
        assert all(item["used_by"] == [{"resource_type": "signoffs", "resource_id": item["id"]}] for item in listed["items"])
        assert [item["id"] for item in assets.read("assets_list", host.owner(), {"group": "polished", "topic": "logo"}, uow)["items"]] == [first["id"]]
        assert assets.read("assets_list", host.owner(), {"batch": "b9"}, uow)["items"] == []
        paged = assets.read("assets_list", host.owner(), {"limit": 1}, uow)
        assert len(paged["items"]) == 1 and paged["next_cursor"]
        assert assets.read("assets_get", host.owner(), {"asset_id": second["id"]}, uow)["asset"]["id"] == second["id"]
        assert failure(assets.read, "assets_get", host.owner(), {"asset_id": new_id()}, uow)[0] == "not_found"


# -- asset mutations ---------------------------------------------------------------------------------------------------

def test_asset_upload_preparation_validates_and_names_the_stored_resource(host, store):
    assets = AssetOperations(store, additional_references=lambda row, ctx, uow: [])
    data = b"\x89PNG synthetic"
    plan = prepare(host, "assets_upload", assets, {"filename": "My Logo (1).PNG", "content_b64": encoded(data), "group": "polished", "topic": "brand",
                                                    "notes": "primary", "batch": "b1"})
    _, kind, path, written, metadata, extra = next(call for call in store.calls if call[0] == "write")
    assert (kind, path, written) == ("asset", "assets/polished/brand/my-logo-1.png", data)
    assert metadata["group"] == "polished" and metadata["topic"] == "brand" and metadata["notes"] == "primary" and metadata["batch"] == "b1"
    assert (metadata["filename"], metadata["media_type"], metadata["size"], metadata["by"]) == ("my-logo-1.png", "image/png", len(data), host.owner().principal_id)
    assert ("free_path", BUSINESS, "assets/polished/brand", "my-logo-1", ".png") in store.calls
    assert plan.operation_name == "assets_upload" and [change.resource.resource_type for change in plan.changes] == ["documents"]
    store.calls.clear()
    prepare(host, "assets_upload", assets, {"filename": "x.bin", "content_b64": encoded(b"1"), "group": "drafted", "media_type": "application/x-custom"})
    assert next(call for call in store.calls if call[0] == "write")[4]["media_type"] == "application/x-custom"
    assert ("free_path", BUSINESS, "assets/drafted", "x", ".bin") in store.calls


@pytest.mark.parametrize("arguments, expected", [
    ({"filename": "a.png", "content_b64": "###", "group": "polished"}, ("validation_error", "Asset content must be valid base64")),
    ({"filename": "a.png", "content_b64": "", "group": "polished"}, ("validation_error", "Assets must contain 1 byte to 100 MiB")),
    ({"filename": ".hidden", "content_b64": encoded(b"1"), "group": "polished"}, ("validation_error", "Invalid asset filename")),
])
def test_asset_upload_refusals(host, store, arguments, expected):
    assets = AssetOperations(store, additional_references=lambda row, ctx, uow: [])
    assert failure(prepare, host, "assets_upload", assets, arguments) == expected
    assert not store.calls


def test_asset_size_bound_and_task_links(host, store, monkeypatch):
    assets = AssetOperations(store, additional_references=lambda row, ctx, uow: [])
    monkeypatch.setattr("qorqut_operations.assets.operations.MAX_BYTES", 4)
    assert failure(prepare, host, "assets_upload", assets, {"filename": "a.png", "content_b64": encoded(b"12345"), "group": "polished"})[0] == "validation_error"
    prepare(host, "assets_upload", assets, {"filename": "a.png", "content_b64": encoded(b"1234"), "group": "polished"})
    assert ASSET_MAX_BYTES == 100 * 1024 * 1024
    task = host.create_task("Brand kit")
    plan = prepare(host, "assets_upload", assets, {"filename": "k.png", "content_b64": encoded(b"1"), "group": "polished", "task_ids": [task["id"]]})
    assert sorted(change.resource.resource_type for change in plan.changes) == ["documents", "relationships"]
    assert failure(prepare, host, "assets_upload", assets, {"filename": "k.png", "content_b64": encoded(b"1"), "group": "polished", "task_ids": [new_id()]})[0] == "not_found"


def test_asset_update_keeps_bytes_and_delete_archives_with_the_caller_pins(host, store):
    assets = AssetOperations(store, additional_references=lambda row, ctx, uow: [])
    row = store.add("asset", "assets/polished/a.png", metadata={"group": "polished", "notes": "old"}, data=b"original", revision=3)
    prepare(host, "assets_update", assets, {"asset_id": row["id"], "changes": {"notes": "new"}, "expected_content_hash": "a" * 64}, expected_revision=3)
    _, kind, path, written, metadata, extra = next(call for call in store.calls if call[0] == "write")
    assert (kind, path, written, metadata["notes"], metadata["group"]) == ("asset", row["path"], b"original", "new", "polished")
    assert extra["before"] == row["id"]
    assert (extra["expected_revision"], extra["expected_content_hash"]) == (3, "a" * 64)
    prepare(host, "assets_delete", assets, {"asset_id": row["id"]}, expected_revision=3)
    assert ("archive", row["id"], 3, None) in store.calls
    assert failure(prepare, host, "assets_delete", assets, {"asset_id": new_id()})[0] == "not_found"
    store.unreadable = True
    assert failure(prepare, host, "assets_update", assets, {"asset_id": row["id"], "changes": {"notes": "x"}})[0] == "document_unavailable"


# -- data uploads and the table rows that cite them --------------------------------------------------------------------------

def test_data_upload_preparation_apply_and_download(host, store):
    uploads = DataUploadOperations(store)
    data = b"name,amount\nA,1\n"
    plan = prepare(host, None, uploads, {"filename": "Q3 Sales.CSV", "content_b64": encoded(data), "notes": "from finance"})
    _, kind, path, written, metadata, _ = next(call for call in store.calls if call[0] == "write")
    assert (kind, path, written) == ("data_upload", "data-uploads/q3-sales.csv", data)
    assert {key: value for key, value in metadata.items() if key != "at"} == {
        "filename": "Q3 Sales.CSV", "media_type": "text/csv", "size": len(data), "notes": "from finance", "by": host.owner().principal_id}
    assert isinstance(metadata["at"], str) and [change.resource.resource_type for change in plan.changes] == ["documents"]
    assert ("free_path", BUSINESS, "data-uploads", "q3-sales", ".csv") in store.calls
    assert failure(prepare, host, None, uploads, {"filename": "a.csv", "content_b64": "***"}) == ("validation_error", "Upload content must be valid base64")
    assert failure(prepare, host, None, uploads, {"filename": "a.csv", "content_b64": ""}) == ("validation_error", "Data uploads must contain 1 byte to 20 MiB")
    assert UPLOAD_MAX_BYTES == 20 * 1024 * 1024
    row = store.add("data_upload", "data-uploads/q3-sales.csv", metadata={"filename": "Q3 Sales.CSV"}, data=data)
    with host.database.read() as uow:
        assert uploads.download(host.owner(), row["id"], uow) == ("Q3 Sales.CSV", data)
        row["data"] = b"tampered"
        assert failure(uploads.download, host.owner(), row["id"], uow) == ("revision_conflict", "Original upload was changed outside the service")


def test_rows_cite_ready_uploads_and_uploads_list_their_tables(host, store):
    uploads = DataUploadOperations(store)
    upload = store.add("data_upload", "data-uploads/s.csv", metadata={"filename": "s.csv", "notes": "q3"})
    unready = store.add("data_upload", "data-uploads/u.csv", availability="applying")
    note = store.add("note", "notes/n.md")
    with host.database.transaction() as uow:
        for row in store.documents.values():
            uow.connection.execute("INSERT INTO documents(business_id, id, kind, path, content_hash, availability, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                   (BUSINESS, row["id"], row["kind"], row["path"], row["content_hash"], row["availability"], NOW, NOW))
    table = host.make("Sales")["table"]
    host.write(table, [{"cells": {"name": "A"}, "source_upload_id": upload["id"]}], revision=1)
    for source in (unready["id"], note["id"]):
        assert host.error("tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "B"}, "source_upload_id": source}]},
                          expected_revision=2) == "validation_error"
    assert host.error("tables_rows_write", {"table_id": table["id"], "rows": [{"cells": {"name": "B"}, "source_upload_id": new_id()}]},
                      expected_revision=2) == "not_found"
    with host.database.read() as uow:
        listed = uploads.read("data_uploads_list", host.owner(), {"query": "Q3"}, uow)
        assert [item["id"] for item in listed["items"]] == [upload["id"]]
        assert listed["items"][0]["tables"] == [{"id": table["id"], "title": "Sales"}]
        assert listed["items"][0]["local_path"] == str(Path(store.root / upload["path"]).resolve())
        assert uploads.read("data_uploads_get", host.owner(), {"upload_id": upload["id"]}, uow)["upload"]["tables"][0]["title"] == "Sales"
        assert uploads.read("data_uploads_list", host.owner(), {"query": "nothing"}, uow)["items"] == []
