"""Real isolated service for the Viewbox and table-viewer browser checks: real placements, owner board reads and table reads."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile

import uvicorn

from qorqut_operations.contracts import InvocationOptions, new_id
from bazarlyk.service.web import create_app
from bazarlyk.transport.compatibility import DashboardCompatibility
from file_viewer_browser_fixture import EVIL, PAGE, REPORT, SMALL_CSV
from test_service_transports import ServiceFixture

# Mirrors a real agent preview: self-contained styling plus images that sit beside it as relative files.
MOTION = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Motion options</title>
<style>body{margin:0;padding:24px 16px;background:#f6f8fa}main{max-width:1280px;margin:0 auto;display:grid;gap:24px}
img{width:100%;height:auto;display:block}.sq{max-width:640px}</style></head>
<body><main><p id="intro">01 Race: hero and square.</p>
<img id="hero" src="animated.svg" alt="Hero"><img id="square" class="sq" src="animated-square.svg" alt="Square">
<img id="gone" src="missing.svg" alt="Missing"></main></body></html>
"""
MOTION_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640"><rect width="1280" height="640" fill="#2da44e"/></svg>'

COLUMNS = [{"id": "name", "name": "Name", "type": "text"}, {"id": "amount", "name": "Amount", "type": "number"}]


def make_table(application, ctx, scope, title, count):
    key = lambda: InvocationOptions(idempotency_key=new_id())
    created = application.invoke(ctx, "tables_create", {"title": title, "columns": COLUMNS, **({"scope": "global"} if scope else {})}, key())
    assert created.status == "ok", created.to_dict()
    table = created.data["table"]
    rows = [{"cells": {"name": f"<b>row {n}</b>", "amount": n}} for n in range(count)]
    written = application.invoke(ctx, "tables_rows_write", {"table_id": table["id"], "rows": rows, **({"scope": "global"} if scope else {})},
                                 InvocationOptions(expected_revision=table["revision"], idempotency_key=new_id()))
    assert written.status == "ok", written.to_dict()
    return table["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-root", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="viewbox-browser-") as temporary, socket.socket() as listener:
        fixture = ServiceFixture(Path(temporary), full=True)
        subprocess.run(["git", "init", "-q", str(fixture.fixture.root)], check=True)
        listener.bind(("127.0.0.1", 0)); listener.listen(128)
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        app = create_app(fixture.runtime, dashboard_root=args.dashboard_root, origins=[origin], authentication=fixture.authentication,
                         compatibility=DashboardCompatibility(fixture.runtime, background=fixture.runtime.background))
        application = fixture.runtime.application
        from dataclasses import replace
        owner = replace(fixture.fixture.ctx, business_id=None, review_business_id=None)
        business_table = make_table(application, fixture.fixture.ctx, False, "Sales", 60)
        global_table = make_table(application, owner, True, "Global <i>pipeline</i>", 3)
        # Real files for the real /files/view reader, including a hostile HTML page (same synthetic files as the file viewer's own fixture).
        files = fixture.fixture.root / "widget" / "files"
        files.mkdir(parents=True)
        (files / "report.md").write_text(REPORT)
        (files / "notes.txt").write_text("notes\n")
        (files / "small.csv").write_bytes(SMALL_CSV)
        (files / "page.html").write_text(PAGE)
        (files / "evil.html").write_text(EVIL)
        (files / "motion.html").write_text(MOTION)
        (files / "animated.svg").write_text(MOTION_SVG)
        (files / "animated-square.svg").write_text(MOTION_SVG)
        manager = fixture.runtime.resolver.invocation_context(fixture.manager)
        business_agent = fixture.runtime.resolver.invocation_context(fixture.agent)
        for ctx, link, title in (
                (manager, f"/tables/view?table_id={global_table}", "Global table"),
                (manager, "https://example.test/report", "Report"),
                (manager, "/files/view?path=widget%2Ffiles%2Freport.md", "Report markdown"),
                (manager, "/files/view?path=widget%2Ffiles%2Fsmall.csv", "Small CSV"),
                (manager, "/files/view?path=widget%2Ffiles%2Fpage.html", "Page HTML"),
                (manager, "/files/view?path=widget%2Ffiles%2Fevil.html", "Hostile HTML"),
                (manager, "/files/view?path=widget%2Ffiles%2Fmotion.html", "Motion options"),
                (business_agent, f"/tables/view?table_id={business_table}&business_id={fixture.fixture.business_id}", "Sales table"),
                (business_agent, f"/tables/view?table_id={business_table}&business_id={fixture.fixture.business_id}", "Sales again")):
            placed = application.invoke(ctx, "viewbox_place", {"link": link, "title": title}, InvocationOptions(idempotency_key=new_id()))
            assert placed.status == "ok", placed.to_dict()
        print(json.dumps({"url": origin, "business_id": fixture.fixture.business_id, "business_table": business_table,
                          "global_table": global_table, "manager_id": fixture.manager.id, "agent_id": fixture.agent.id}), flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(sockets=[listener])


if __name__ == "__main__":
    main()
