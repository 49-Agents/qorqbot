import base64
import csv
import io
import shutil
from datetime import datetime
from pathlib import Path

from starlette.testclient import TestClient
from openpyxl import Workbook, load_workbook
from bazarlyk.storage import Database
from bazarlyk.storage.recovery import BackupStore
from qorqut_operations.contracts import new_id

from test_tables import service, call, create, ok, run
from test_service_transports import ORIGIN


def upload(service, content, filename="analytics.csv", **extra):
    return ok(call(service, "data_uploads_upload", {"filename": filename, "content_b64": base64.b64encode(content).decode(), **extra}))["upload"]


def test_original_csv_can_be_parsed_in_scratchpad_and_imported_with_provenance(service, tmp_path):
    original = b'name,amount\r\n"A, Inc",12.50\r\nB,24\r\n'
    source = upload(service, original)
    assert Path(source["local_path"]).read_bytes() == original
    assert source["path"].startswith("data-uploads/")
    assert not ok(call(service, "assets_list"))["items"]
    made = create(service)
    # The agent's normal scratchpad parser produces the structured MCP rows.
    rows = [{"cells": {"name": row["name"], "amount": float(row["amount"])}, "source_upload_id": source["id"]}
        for row in csv.DictReader(io.StringIO(Path(source["local_path"]).read_text()))]
    saved = ok(call(service, "tables_rows_write", {"table_id": made["table"]["id"], "rows": rows}, 1))
    assert saved["rows"][0]["cells"] == {"name": "A, Inc", "amount": 12.5}
    assert ok(call(service, "data_uploads_get", {"upload_id": source["id"]}))["upload"]["tables"] == [{"id": made["table"]["id"], "title": "CRM"}]
    run(service, saved["job_id"])
    assert Path(source["local_path"]).read_bytes() == original
    store, identity = BackupStore(service.fixture.db, service.fixture.root), new_id()
    store.create(identity)
    restored = tmp_path / "restored"
    result = store.restore(identity, restored)
    with Database(result["database"]).open_existing().read() as uow:
        row = uow.repository("data_rows", business_id=service.fixture.business_id).get(saved["rows"][0]["id"])
        assert row["source_upload_id"] == source["id"] and row["cells_json"]["amount"] == 12.5
    relative = Path(source["local_path"]).relative_to(service.fixture.root)
    assert (restored / relative).read_bytes() == original


def test_binary_spreadsheet_is_preserved_and_same_filenames_do_not_overwrite(service):
    data = b'PK\x03\x04\x00\xff\x00' + b'\x00' * 2_100_000
    first = upload(service, data, "Workbook.xlsx")
    second = upload(service, b'other file', "Workbook.xlsx")
    assert first["path"] != second["path"]
    assert Path(first["local_path"]).read_bytes() == data
    assert len(ok(call(service, "data_uploads_list", {"query": "workbook"}))["items"]) == 2


def test_real_excel_copy_can_be_parsed_and_imported_without_altering_the_original(service, tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.append(["name", "amount", "due", "formula"])
    sheet.append(["Campaign A", 125.5, datetime(2026, 10, 1), "=B2*2"])
    buffer = io.BytesIO()
    book.save(buffer)
    original = buffer.getvalue()
    source = upload(service, original, "costs.xlsx")
    scratch = tmp_path / "scratchpad"
    scratch.mkdir()
    copy = scratch / "costs.xlsx"
    shutil.copyfile(source["local_path"], copy)
    parsed = load_workbook(copy, read_only=True, data_only=False, keep_links=False)
    values = list(parsed.active.iter_rows(min_row=2, values_only=True))
    parsed.close()
    table = create(service, columns=[{"id": key, "name": key.title(), "type": kind}
        for key, kind in [("name", "text"), ("amount", "number"), ("due", "date"), ("formula", "text")]])["table"]
    rows = [{"cells": {"name": name, "amount": amount, "due": due.date().isoformat(), "formula": formula},
        "source_upload_id": source["id"]} for name, amount, due, formula in values]
    saved = ok(call(service, "tables_rows_write", {"table_id": table["id"], "rows": rows}, 1))
    assert saved["rows"][0]["cells"] == {"name": "Campaign A", "amount": 125.5, "due": "2026-10-01", "formula": "=B2*2"}
    assert Path(source["local_path"]).read_bytes() == original


def test_story_links_download_and_modified_original_detection(service):
    story = ok(call(service, "stories_create", {"title": "Analytics", "purpose": "Review export"}))["story"]
    source = upload(service, b'a,b\n1,2\n', story_ids=[story["id"]])
    links = ok(call(service, "stories_resource_get", {"resource_type": "uploads", "resource_id": source["id"]}))["links"]
    assert links[0]["story_id"] == story["id"]
    with TestClient(service.web) as client:
        url = "/api/biz/widget/data-uploads/" + source["id"] + "/download"
        response = client.get(url)
        assert response.status_code == 200 and response.content == b'a,b\n1,2\n'
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"].startswith("attachment;")
        Path(source["local_path"]).write_bytes(b"outside change")
        assert client.get(url).status_code == 409


def test_upload_requires_valid_bytes_and_does_not_create_a_background_job(service):
    bad = call(service, "data_uploads_upload", {"filename": "x.csv", "content_b64": "??"})
    assert bad["error"]["code"] == "validation_error"
    upload(service, b"x\n1\n")
    with service.fixture.db.read() as uow:
        assert not uow.repository("jobs", business_id=service.fixture.business_id).list()


def test_owner_upload_endpoint_supports_more_than_default_json_limit(service):
    with TestClient(service.web) as client:
        data = b'x,y\n' * 450000
        response = client.post("/api/biz/widget/operations/data_uploads_upload", headers=ORIGIN,
            json={"filename": "export.csv", "content_b64": base64.b64encode(data).decode(), "idempotency_key": "large-upload"})
        result = response.json()
        assert result["status"] == "ok", result
        assert Path(result["data"]["upload"]["local_path"]).read_bytes() == data
