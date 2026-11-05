"""The standalone owner table page: strict selectors, same-origin framing only for the viewers, owner-only bounded reads."""
from dataclasses import replace
import subprocess

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import InvocationOptions, new_id
from bazarlyk.service.web import create_app
from test_service_transports import ORIGIN, ServiceFixture

COLUMNS = [{"id": "name", "name": "Name", "type": "text"}, {"id": "amount", "name": "Amount", "type": "number"}]


@pytest.fixture
def s(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    subprocess.run(["git", "init", "-q", str(s.fixture.root)], check=True)
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    (dashboard / "index.html").write_text("<!doctype html><title>app</title>")
    (dashboard / "tables-view.html").write_text("<!doctype html><title>table</title>")
    from bazarlyk.transport.compatibility import DashboardCompatibility
    s.web = create_app(s.runtime, origins=["http://testserver"], authentication=s.authentication, dashboard_root=dashboard,
                       compatibility=DashboardCompatibility(s.runtime, background=s.runtime.background))
    s.owner = replace(s.fixture.ctx, business_id=None, review_business_id=None)
    return s


def create(s, ctx, scope):
    args = {"title": "Sales", "columns": COLUMNS, **({"scope": "global"} if scope else {})}
    result = s.runtime.application.invoke(ctx, "tables_create", args, InvocationOptions(idempotency_key=new_id()))
    assert result.status == "ok", result.to_dict()
    table_id = result.data["table"]["id"]
    rows = [{"cells": {"name": f"row {n}", "amount": n}} for n in range(60)]
    written = s.runtime.application.invoke(ctx, "tables_rows_write", {"table_id": table_id, "rows": rows, **({"scope": "global"} if scope else {})},
        InvocationOptions(expected_revision=result.data["table"]["revision"], idempotency_key=new_id()))
    assert written.status == "ok", written.to_dict()
    return table_id


def test_shell_is_served_only_for_strict_selectors_and_frames_same_origin(s):
    table = new_id()
    with TestClient(s.web) as client:
        ok = client.get(f"/tables/view?table_id={table}", headers=ORIGIN)
        assert ok.status_code == 200 and "table" in ok.text
        assert ok.headers["x-frame-options"] == "SAMEORIGIN" and ok.headers["content-security-policy"] == "frame-ancestors 'self'"
        assert ok.headers["cache-control"] == "no-store"
        assert client.get(f"/tables/view?table_id={table}&business_id={new_id()}", headers=ORIGIN).status_code == 200
        for query in ("", "table_id=null", f"table_id={table}&business_id=null", f"table_id={table}&business_id=", f"table_id={table}&x=1",
                      f"table_id={table}&table_id={table}", f"business_id={table}", "table_id=../etc"):
            assert client.get("/tables/view?" + query, headers=ORIGIN).status_code == 400, query
        assert client.get(f"/tables/view?table_id={table}", headers={"Origin": "http://evil.example"}).status_code == 403
        # Only the viewers may be framed; the application itself never is.
        assert client.get("/", headers=ORIGIN).headers["x-frame-options"] == "DENY"


def test_owner_reads_global_and_business_tables_with_bounded_pages(s):
    business = create(s, s.fixture.ctx, False)
    global_table = create(s, s.owner, True)
    with TestClient(s.web) as client:
        # The shell maps its business_id selector to the scoped route through the owner's business list.
        listed = client.get("/api/biz", headers=ORIGIN)
        assert listed.status_code == 200, listed.text
        slug = next(b["slug"] for b in listed.json() if b["id"] == s.fixture.business_id)
        page = client.post(f"/api/biz/{slug}/operations/tables_rows_list", headers=ORIGIN,
                           json={"table_id": business, "limit": 50})
        assert page.status_code == 200, page.text
        data = page.json()["data"]
        assert len(data["items"]) == 50 and data["next_cursor"]
        rest = client.post(f"/api/biz/{slug}/operations/tables_rows_list", headers=ORIGIN,
                           json={"table_id": business, "limit": 50, "cursor": data["next_cursor"]}).json()["data"]
        assert len(rest["items"]) == 10 and not rest["next_cursor"]
        assert client.post(f"/api/biz/{slug}/operations/tables_get", headers=ORIGIN, json={"table_id": business}).json()["data"]["table"]["title"] == "Sales"
        got = client.post("/api/root/operations/tables_get", headers=ORIGIN, json={"scope": "global", "table_id": global_table})
        assert got.status_code == 200 and got.json()["data"]["table"]["id"] == global_table
        # Scopes stay separate, and a missing table is a bounded not-found rather than an error page.
        cross = client.post(f"/api/biz/{slug}/operations/tables_get", headers=ORIGIN, json={"table_id": global_table})
        assert cross.status_code == 404 and cross.json()["status"] == "error"
        assert client.post("/api/root/operations/tables_get", headers=ORIGIN, json={"scope": "global", "table_id": new_id()}).status_code == 404
        assert client.post("/api/biz/not-a-business/operations/tables_get", headers=ORIGIN, json={"table_id": business}).status_code in {403, 404}
        # An agent credential is never the owner identity for these routes.
        headers = {**s.initialize(client, s.agent)}
        assert "Mcp-Session-Id" in headers
