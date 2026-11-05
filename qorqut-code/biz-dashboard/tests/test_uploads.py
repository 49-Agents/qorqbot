"""Scoped asset HTTP delivery through the shared service."""

from test_service_transports import ORIGIN


def test_scoped_asset_upload_metadata_deduplication_and_safe_delivery(connected):
    _, client = connected
    upload = lambda name, content, media="image/png", **fields: client.post("/api/biz/widget/assets/upload",
        data={"group": "polished", **fields}, files={"file": (name, content, media)}, headers=ORIGIN)
    first = upload("Screen Shot.PNG", b"fixture image", topic="launch", notes="Actual metadata", batch="one")
    assert first.status_code == 200, first.text
    again = upload("Screen Shot.PNG", b"another image", topic="launch")
    assert again.status_code == 200, again.text
    assert again.json()["path"] != first.json()["path"]
    rows = client.get("/api/biz/widget/assets").json()["polished"]
    assert any(row["path"] == first.json()["path"] and row["notes"] == "Actual metadata" for row in rows)
    image = client.get("/api/biz/widget/asset/" + first.json()["path"])
    assert image.content == b"fixture image"
    assert image.headers["content-disposition"].startswith("inline;")
    active = upload("script.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")
    assert active.status_code == 200, active.text
    download = client.get("/api/biz/widget/asset/" + active.json()["path"])
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-type"] == "application/octet-stream"
    for name, fields in [(".hidden", {}), ("okay.png", {"group": "secret"}), ("okay.png", {"topic": "../outside"})]:
        invalid = upload(name, b"content", **fields)
        assert invalid.status_code == 422, invalid.text
    missing = client.post("/api/biz/widget/assets/upload", data={"group": "polished"}, headers=ORIGIN)
    assert missing.status_code in {400, 422}
    over = client.post("/api/biz/widget/assets/upload", content=b"", headers={**ORIGIN,
        "Content-Type": "multipart/form-data; boundary=bounded", "Content-Length": str(101 * 1024 * 1024)})
    assert over.status_code == 413
