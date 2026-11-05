"""Isolated real backend for the file viewer's browser acceptance: synthetic files and records only.

Serves the real shared application over HTTP on a loopback port (never the live one) with a temporary
businesses root, registered notes that have real saved versions, and a task and a story whose prose
carries ordinary links. Nothing here reads the live content or records.
"""

import argparse
import csv
import io
import json
import os
from pathlib import Path
import socket
import tempfile

import uvicorn

from qorqut_operations.contracts import InvocationOptions, new_id
from bazarlyk.service.web import create_app
from bazarlyk.transport.compatibility import DashboardCompatibility
from test_agent_configuration import REPO, PATHS
from test_service_transports import OWNER_TOKEN, ServiceFixture

NOTE_VERSIONS = 24
PROSE = ("Read [the brief](/files/view?path=widget%2Ffiles%2Freport.md) or https://example.com/page, but not "
         "[this](javascript:alert(1)) or <b>markup</b> <img src=x onerror=\"window.__prose=1\">.\nSecond line.")

REPORT = """---
title: Synthetic report
owner: fixture
---
# Synthetic report

Plain paragraph with a [sibling file](notes.txt), a [web link](https://example.com/a) and an
[unsafe link](javascript:window.__md=1).

<script>window.__md = 1</script>
<b id="raw-html">raw html stays text</b>

![tracker](http://127.0.0.1:9/pixel.png)

| Name | Value |
|------|-------|
| alpha | 1 |
| beta | 2 |

```python
print("code")
```
"""

PAGE = """<!doctype html><title>Synthetic page</title><style>body{font-family:sans-serif}</style>
<h1 id="heading">Synthetic HTML</h1>
<script>
document.getElementById('heading').textContent = 'SCRIPT RAN';
try { parent.document.title = 'pwned'; } catch (error) {}
try { parent.postMessage('leak', '*'); } catch (error) {}
fetch('/api/root/files/metadata?path=widget%2Ffiles%2Freport.md').then(r => r.text()).then(t => { window.leaked = t; });
</script>
<img src="/ping?image=1" alt="tracker">
<p><a id="api-link" href="/api/root/files/content?path=widget%2Ffiles%2Freport.md">owner api</a>
<a id="sibling-link" href="report.md">relative</a></p>
<form action="/ping?form=1"><button id="submit">Submit</button></form>
"""

# Hostile page for the framed reader: scripts, explicit self/top/parent targets, a meta refresh, a form and subframes
# that would each reach the parent, the owner API or the network if the inert preview let them.
OWNER_API = "/api/root/files/content?path=widget%2Ffiles%2Freport.md"
EVIL = f"""<!doctype html><title>Hostile page</title>
<meta http-equiv="refresh" content="1;url={OWNER_API}">
<h1 id="heading">Hostile HTML</h1>
<script>
document.getElementById('heading').textContent = 'SCRIPT RAN';
try {{ parent.window.__evil = 'parent'; top.window.__evil = 'top'; }} catch (error) {{}}
try {{ top.location = '{OWNER_API}'; }} catch (error) {{}}
try {{ parent.postMessage('leak', '*'); }} catch (error) {{}}
fetch('{OWNER_API}').catch(() => {{}});
</script>
<img src="/ping?image=1" alt="tracker"><link rel="stylesheet" href="/ping?css=1">
<iframe src="{OWNER_API}" title="nested api"></iframe>
<p><a id="self-link" target="_self" href="{OWNER_API}">self</a>
<a id="top-link" target="_top" href="{OWNER_API}">top</a>
<a id="parent-link" target="_parent" href="{OWNER_API}">parent</a>
<a id="blank-link" target="_blank" href="{OWNER_API}">blank</a>
<a id="named-link" target="viewbox" href="{OWNER_API}">named</a>
<a id="plain-link" href="{OWNER_API}">plain</a></p>
<form id="self-form" method="get" target="_self" action="/ping?form=1"><button id="self-submit">Submit</button></form>
"""

SMALL_CSV = b'\xef\xbb\xbfname,note\r\n"Smith, J","line1\nline2"\r\n=SUM(A1:A2),"say ""hi"""\r\n'


def build(root, origin, dashboard_root):
    fixture = ServiceFixture(root, full=True)
    fixture.runtime.background.start = lambda: None
    runtime, owner = fixture.runtime, fixture.fixture.ctx
    business_id, base = fixture.fixture.business_id, fixture.fixture.root

    def call(context, operation, arguments, revision=None):
        result = runtime.application.invoke(context, operation, arguments,
                                            InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
        if result.status != "ok":
            raise RuntimeError(f"Fixture operation {operation} failed: {json.dumps(result.to_dict())}")
        return result.data

    files = base / "widget" / "files"
    files.mkdir(parents=True)
    (base / "shared").mkdir()
    (files / "report.md").write_text(REPORT)
    (files / "notes.txt").write_text("Plain text with <script>alert(1)</script> and a very long line " + "x" * 400 + "\nsecond line\n")
    def cell(r, c):
        return '=HYPERLINK("http://evil.example","x")' if (r, c) == (1, 0) else "@SUM(1+1)" if (r, c) == (1, 1) else f"r{r}c{c}"

    table = io.StringIO()
    writer = csv.writer(table)
    writer.writerow([f"h{c}" for c in range(55)])
    writer.writerows([cell(r, c) for c in range(55)] for r in range(1, 250))
    (files / "table.csv").write_text(table.getvalue())
    (files / "small.csv").write_bytes(SMALL_CSV)
    # Just over the 2 MiB read limit: Source must show the original text cut at that limit, not rebuilt cells.
    (files / "long.csv").write_text('name,note,n\n' + ''.join(f'"row {n}, quoted","line1\nline2",{n}\n' for n in range(54_000)))
    (files / "page.html").write_text(PAGE)
    (files / "evil.html").write_text(EVIL)
    (files / "big.log").write_bytes(b"".join(f"line {n}\n".encode() for n in range(400_000)))
    (files / "image.bin").write_bytes(b"\x00\x01\x02binary\x00" * 64)
    (files / "weird & name (1).md").write_text("# Weird name\n")
    (base / "shared" / "brief.md").write_text("# Shared brief\n\nVisible below the businesses root.\n")
    outside = Path(tempfile.mkdtemp(prefix="fv-outside-")) / "secret.txt"
    outside.write_text("outside the root")
    os.symlink(outside, files / "escape.txt")
    os.mkdir(files / "folder")

    def note(title, revisions):
        document = call(owner, "notes_create", {"title": title, "body": revisions(1), "target_business_id": business_id})["document"]
        for number in range(2, NOTE_VERSIONS + 2 if title == "History note" else 3):
            document = call(owner, "notes_update", {"document_id": document["id"], "body": revisions(number),
                                                    "target_business_id": business_id}, document["revision"])["document"]
        return document

    history = note("History note", lambda n: f"# Plan\n\nRevision {n}\n\n" + "- item\n" * n)
    drifted = note("Drifted note", lambda n: f"# Drift\n\nSaved revision {n}\n")
    drifted_path = base / "widget" / "hq" / drifted["path"]
    drifted_path.write_text(drifted_path.read_text() + "\nEdited outside Qorqut.\n")

    task = call(owner, "tasks_create", {"title": "Review the brief", "assignee_ids": [fixture.agent.id]})["task"]
    # Owner comments use the private channel, so the assigned agent writes the shared progress note.
    agent = runtime.resolver.invocation_context(fixture.agent, business_id=business_id)
    call(agent, "tasks_comment", {"task_id": task["id"], "text": PROSE}, task["revision"])
    story = call(owner, "stories_create", {"title": "Launch story", "purpose": PROSE})["story"]
    manager = runtime.resolver.invocation_context(fixture.manager, business_id=business_id)
    call(manager, "stories_note_add", {"story_id": story["id"], "body": PROSE})

    app = create_app(runtime, dashboard_root=dashboard_root, origins=[origin], authentication=fixture.authentication,
                     compatibility=DashboardCompatibility(runtime, background=runtime.background))
    return app, {"url": origin + "/?token=" + OWNER_TOKEN, "origin": origin, "business_slug": "widget", "business_id": business_id,
                 "root": str(base), "outside": str(outside), "history_id": history["id"], "history_path": f"widget/hq/{history['path']}",
                 "drifted_id": drifted["id"], "drifted_path": f"widget/hq/{drifted['path']}", "task_id": task["id"],
                 "story_id": story["id"], "versions": NOTE_VERSIONS + 1, "prose": PROSE}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--dashboard-root", type=Path, default=PATHS.component("dashboard"))
    args = parser.parse_args()
    if args.port == 1049:
        parser.error("The live service port is forbidden for the isolated fixture")
    with tempfile.TemporaryDirectory(prefix="bazarlyk-file-viewer-") as temporary, socket.socket() as listener:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        app, details = build(Path(temporary), origin, args.dashboard_root)
        print(json.dumps(details), flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False,
                                      lifespan="on")).run(sockets=[listener])


if __name__ == "__main__":
    main()
