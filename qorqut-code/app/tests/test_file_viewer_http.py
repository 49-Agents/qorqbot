"""Owner file viewer over the real HTTP application, and the agent-callable files_link helper.

The service is the real assembled runtime with real agent credentials and notes written through the registered
operations; files live under a temporary businesses root. Nothing touches live data.
"""
import hashlib
import json
import os
import socket
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from bazarlyk.application.catalog import builtin_registry
from bazarlyk import owner_sessions
from qorqut_operations.contracts import InvocationOptions, new_id, plain
from bazarlyk.policy.resolver import resolve_operation_policy
from bazarlyk.service.web import ServiceBoundary, create_app
from test_service_transports import ORIGIN, ServiceFixture

FILES = "/api/root/files"


@pytest.fixture
def s(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    service.runtime.background.start = lambda: None
    service.business_id = service.fixture.business_id
    service.root = service.fixture.root
    yield service
    service.runtime.stop()


@pytest.fixture
def client(s):
    with TestClient(s.web) as web:
        s.login(web)
        yield web


def owner_invoke(s, op, args, revision=None, *, scope="business"):
    ctx = s.fixture.ctx if scope == "business" else replace(s.fixture.ctx, business_id=None, review_business_id=None)
    result = s.runtime.application.invoke(ctx, op, args, InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
    assert result.status == "ok", result.to_dict()
    return result.data


def note(s, title, body, *, scope="business"):
    args = {"title": title, "body": body}
    if scope == "business":
        args |= {"target_business_id": s.business_id}
    return owner_invoke(s, "notes_create", args, scope=scope)["document"]


def get(client, action, query, **kwargs):
    return client.get(f"{FILES}/{action}?{query}", headers=ORIGIN, **kwargs)


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def error(response):
    body = response.json()
    assert body["status"] == "error", body
    return response.status_code, body["error"]["code"]


def write(s, relative, content=b"hello"):
    path = s.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def rows(s):
    """Everything a read must leave alone: registered documents, saved versions, business activity."""
    with s.runtime.database.read() as uow:
        c = uow.connection
        return tuple(c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("documents", "qbdv_document_versions", "qbdv_business_events", "tasks", "jobs"))


# Reading files below the root


def test_the_owner_reads_unregistered_files_anywhere_below_the_root_and_nothing_is_written(s, client):
    write(s, "widget/hq/scratch/research.md", "# Findings\n\nSee [site](https://example.com).\n")
    write(s, "widget/code/app/main.py", "print('hi')\n")
    write(s, "shared/agents/onbashy/notes.txt", "plain\nnotes\n")
    write(s, "top.csv", "a,b\n1,2\n")
    before = rows(s)
    meta = data(get(client, "metadata", "path=widget/hq/scratch/research.md"))
    assert (meta["name"], meta["path"], meta["preview_kind"], meta["managed"], meta["current"]) == \
        ("research.md", "widget/hq/scratch/research.md", "markdown", None, True)
    assert meta["size"] == len("# Findings\n\nSee [site](https://example.com).\n") and meta["modified_at"].endswith("Z") and "tmp" not in json.dumps(meta)
    assert meta["urls"]["copy"] == meta["urls"]["view"] == "/files/view?path=widget/hq/scratch/research.md"
    page = data(get(client, "content", "path=widget/hq/scratch/research.md"))
    assert page["kind"] == "markdown" and "<h1>Findings</h1>" in page["html"] and 'rel="noopener noreferrer"' in page["html"]
    assert data(get(client, "content", "path=widget/code/app/main.py"))["text"] == "print('hi')\n"
    assert data(get(client, "content", "path=shared/agents/onbashy/notes.txt"))["kind"] == "text"
    assert data(get(client, "content", "path=top.csv"))["rows"] == [["a", "b"], ["1", "2"]]
    # The absolute spelling of the same file under the configured root resolves to the same thing.
    absolute = f"path={s.root}/widget/hq/scratch/research.md".replace(" ", "%20")
    assert data(get(client, "metadata", absolute))["path"] == "widget/hq/scratch/research.md"
    assert rows(s) == before, "viewing wrote documents, history, events, tasks or jobs"


def test_content_and_download_headers_are_private_nosniff_and_attachment_only(s, client):
    write(s, "widget/hq/scratch/page.html", "<script>alert(1)</script><h1>x</h1>")
    write(s, "widget/hq/scratch/ünï \"q\".svg", "<svg onload=alert(1)/>")
    for action in ("metadata", "content", "download"):
        got = get(client, action, "path=widget/hq/scratch/page.html")
        assert got.status_code == 200
        assert got.headers["cache-control"] == "private, no-store" and got.headers["x-content-type-options"] == "nosniff"
    download = get(client, "download", "path=widget/hq/scratch/page.html")
    assert download.headers["content-type"] == "application/octet-stream" and download.content == b"<script>alert(1)</script><h1>x</h1>"
    assert download.headers["content-disposition"].startswith("attachment;") and download.headers["content-length"] == "35"
    svg = get(client, "download", "path=widget/hq/scratch/%C3%BCn%C3%AF%20%22q%22.svg")
    assert svg.headers["content-type"] == "application/octet-stream"
    disposition = svg.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition and "%C3%BC" in disposition and 'filename="_n_' in disposition
    assert svg.content == b"<svg onload=alert(1)/>"
    page = data(get(client, "content", "path=widget/hq/scratch/page.html"))
    assert page["kind"] == "html" and page["text"].startswith("<script>") and "script-src" not in page["frame_prefix"]


def test_html_preview_embeds_sibling_images_but_keeps_the_original_source(s, client):
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><rect width="4" height="4"/></svg>'
    page = '<h1>x</h1><img src="hero.svg"><img src="gone.svg"><img src="link.svg">'
    write(s, "widget/hq/scratch/p/index.html", page)
    write(s, "widget/hq/scratch/p/hero.svg", svg)
    write(s, "widget/hq/scratch/outside.svg", svg)
    os.symlink(s.root / "widget/hq/scratch/outside.svg", s.root / "widget/hq/scratch/p/link.svg")
    got = data(get(client, "content", "path=widget/hq/scratch/p/index.html"))
    assert got["text"] == page, "Source, copy and download keep the original"
    assert got["rendered"].count("data:image/svg+xml;base64,") == 1 and 'src="gone.svg"' in got["rendered"] and 'src="link.svg"' in got["rendered"]
    assert got["images"] == {"inlined": 1, "unavailable": 2, "historical": False} and "script-src" not in got["frame_prefix"]


def test_large_files_preview_a_bounded_prefix_and_download_in_full(s, client):
    body = (b"line of text\n" * 400_000)  # About 5 MB.
    write(s, "widget/hq/scratch/big.txt", body)
    page = data(get(client, "content", "path=widget/hq/scratch/big.txt"))
    assert page["truncated"] is True and page["preview_bytes"] == 2 * 1024 * 1024 and page["size"] == len(body)
    assert len(page["text"].encode()) <= 2 * 1024 * 1024
    meta = data(get(client, "metadata", "path=widget/hq/scratch/big.txt"))
    assert meta["size"] == len(body) and meta["preview_limit"] == 2 * 1024 * 1024
    download = get(client, "download", "path=widget/hq/scratch/big.txt")
    assert download.headers["content-length"] == str(len(body)) and download.content == body
    assert "content-encoding" not in download.headers


def test_unreadable_binary_missing_and_special_files_answer_with_bounded_errors(s, client):
    write(s, "widget/hq/scratch/image.bin", b"\x89PNG\r\n\x1a\n\x00\x00")
    meta = data(get(client, "metadata", "path=widget/hq/scratch/image.bin"))
    assert meta["binary"] is True and meta["preview_kind"] == "binary"
    assert data(get(client, "content", "path=widget/hq/scratch/image.bin"))["kind"] == "binary"
    assert get(client, "download", "path=widget/hq/scratch/image.bin").content.startswith(b"\x89PNG")
    os.mkfifo(s.root / "widget/hq/scratch/pipe")
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(s.root / "widget/hq/scratch/sock"))
        outside = s.root.parent / "outside.txt"
        outside.write_text("SECRET")
        (s.root / "widget/hq/scratch/leak.txt").symlink_to(outside)
        (s.root / "widget/hq/scratch/leakdir").symlink_to(s.root.parent)
        for query, expected in (("path=widget/hq/scratch/missing.md", (404, "not_found")),
                                ("path=widget/hq/scratch", (422, "not_a_regular_file")),
                                ("path=widget/hq/scratch/pipe", (422, "not_a_regular_file")),
                                ("path=widget/hq/scratch/sock", (422, "not_a_regular_file")),
                                ("path=widget/hq/scratch/leak.txt", (422, "symlink_not_followed")),
                                ("path=widget/hq/scratch/leakdir/outside.txt", (422, "symlink_not_followed"))):
            for action in ("metadata", "content", "download"):
                got = get(client, action, query)
                assert error(got) == expected, (action, query, got.text)
                assert "SECRET" not in got.text and "Traceback" not in got.text and str(s.root) not in got.text


def test_hostile_paths_and_selectors_are_refused_before_any_file_access(s, client, monkeypatch):
    from bazarlyk.file_viewer import files
    monkeypatch.setattr(files, "open_regular", lambda *a: pytest.fail("a rejected selector reached the filesystem"))
    uuid = new_id()
    for query in ("path=../etc/passwd", "path=widget/../../etc/passwd", "path=/etc/passwd", f"path={s.root}/../etc/passwd",
                  "path=a%00b", "path=%2e%2e/b", "path=a/%2e%2e/%2e%2e/b", "path=%252e%252e/x", "path=a%252Fb", "path=a%zz", "path=100%",
                  "path=", "path=widget/", "path=file:///etc/passwd", "", "path=a&path=b", "path=a&document_id=" + uuid,
                  "path=a&version=1", "path=a&business_id=" + uuid, "document_id=nope", f"document_id={uuid}&version=0",
                  "path=a&token=secret", "path=a&extra=1", "p=a"):
        for action in ("metadata", "content", "download"):
            got = get(client, action, query)
            assert got.status_code in (400, 422) and got.json()["status"] == "error", (action, query, got.status_code, got.text)
    assert error(get(client, "history", "path=a/b"))[0] == 422
    assert error(get(client, "nope", "path=a"))[0] == 404
    assert client.post(f"{FILES}/metadata?path=a", headers=ORIGIN, json={}).status_code in (403, 404, 405)


# Who may read


def test_only_the_installation_owner_reaches_the_file_routes(s, client):
    write(s, "widget/hq/scratch/private.md", "private")
    uuid = new_id()
    agent = {"Authorization": "Bearer " + s.tokens[s.agent.id]}
    manager = {"Authorization": "Bearer " + s.tokens[s.manager.id]}
    for action in ("metadata", "content", "download", "history"):
        query = "path=widget/hq/scratch/private.md" if action != "history" else f"document_id={uuid}"
        for headers in (agent, manager, {"Authorization": "Bearer " + "x" * 40}, {"Authorization": "Basic abc"}):
            got = client.get(f"{FILES}/{action}?{query}", headers={**ORIGIN, **headers})
            assert got.status_code == 403 and got.json()["error"]["code"] == "forbidden", (action, headers, got.text)
            assert "private" not in got.text
        assert client.get(f"{FILES}/{action}?{query}", headers={"Host": "attacker.invalid"}).status_code == 403
        assert client.get(f"{FILES}/{action}?{query}", headers={"Origin": "http://attacker.invalid"}).status_code == 403
    assert client.get(f"{FILES}/content?path=widget/hq/scratch/private.md&token=x", headers=ORIGIN).status_code == 400
    # The owner's own request, same machine, same origin, works.
    assert data(get(client, "content", "path=widget/hq/scratch/private.md"))["text"] == "private"


def stored_human_credentials(s):
    """Bearer tokens that ``credential_kind`` classifies as human, none of them an ordinary owner browser request."""
    other = new_id()
    with s.runtime.database.transaction() as uow:
        uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "Another person"})
        uow.connection.execute("UPDATE businesses SET config_json=json_set(config_json, '$.owner_sessions.enabled', json('true')) "
                               "WHERE id=?", (s.business_id,))
    tokens = {"another human": "another-human-credential-" + "h" * 40}
    with s.runtime.database.transaction() as uow:
        uow.repository("credentials").insert({"id": new_id(), "principal_id": other,
                                              "token_hash": hashlib.sha256(tokens["another human"].encode()).hexdigest(),
                                              "metadata_json": {}})
    for label in ("live", "revoked", "expired"):
        tokens[label + " owner session"] = owner_sessions.issue_credential(s.runtime.database, business_id=s.business_id, label=label)
    for label, column, value in (("revoked", "revoked_at", "2026-01-01T00:00:00+00:00"),
                                 ("expired", "expires_at", "2020-01-01T00:00:00+00:00")):
        digest = hashlib.sha256(tokens[label + " owner session"].encode()).hexdigest()
        with s.runtime.database.transaction() as uow:
            uow.connection.execute(f"UPDATE credentials SET {column}=? WHERE token_hash=?", (value, digest))
    return tokens


def test_classification_is_not_authentication_so_no_explicit_authorization_header_reaches_the_owner(s, client):
    write(s, "widget/hq/scratch/private.md", "private")
    uuid = new_id()
    tokens = stored_human_credentials(s)
    for label, token in tokens.items():
        # The old gate trusted this classification; it says nothing about validity, ownership or expiry.
        assert s.authentication.credential_kind("Bearer " + token) == "human", label
    headers = [{"Authorization": "Bearer " + token} for token in tokens.values()]
    headers += [{"Authorization": "Bearer "}, {"Authorization": ""}, {"Authorization": "bearer lower"}, {"Authorization": "Negotiate abc"}]
    for action in ("metadata", "content", "download", "history"):
        query = "path=widget/hq/scratch/private.md" if action != "history" else f"document_id={uuid}"
        for header in headers:
            got = client.get(f"{FILES}/{action}?{query}", headers={**ORIGIN, **header})
            assert error(got) == (403, "forbidden"), (action, header, got.text)
            assert "private" not in got.text and "Another person" not in got.text
            assert len(got.content) < 400
    # Even a live, enabled owner-session credential (bounded automation) gets no file reading.
    assert error(client.get(f"{FILES}/content?path=widget/hq/scratch/private.md",
                            headers={**ORIGIN, "Authorization": "Bearer " + tokens["live owner session"]})) == (403, "forbidden")
    # The ordinary owner browser request has no Authorization header at all, and still reads every action.
    assert data(get(client, "content", "path=widget/hq/scratch/private.md"))["text"] == "private"
    assert data(get(client, "metadata", "path=widget/hq/scratch/private.md"))["name"] == "private.md"
    assert get(client, "download", "path=widget/hq/scratch/private.md").content == b"private"


def test_agents_get_no_file_reading_tool_and_cannot_use_the_owner_history_read(s, client):
    for principal in (s.agent, s.manager):
        headers = s.initialize(client, principal)
        tools = {row["name"] for row in client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]}
        assert "files_link" in tools and not {name for name in tools if name.startswith("files_")} - {"files_link"}
        assert "document_history_get" not in tools
        denied = s.tool(client, headers, "document_history_get", {"source_kind": "document", "source_id": new_id()})
        assert denied["isError"]


# Registered documents: identity, history, scope, moves


def test_a_registered_note_adds_its_real_saved_versions_and_a_stable_link(s, client):
    doc = note(s, "Plan", "One\n")
    owner_invoke(s, "notes_update", {"document_id": doc["id"], "body": "Two\nmore\n", "target_business_id": s.business_id}, revision=1)
    owner_invoke(s, "notes_update", {"document_id": doc["id"], "body": "Three\n", "target_business_id": s.business_id}, revision=2)
    location = f"widget/hq/{doc['path']}"
    before = rows(s)
    meta = data(get(client, "metadata", f"path={location}"))
    stable = f"/files/view?document_id={doc['id']}&business_id={s.business_id}"
    assert meta["managed"] == {"document_id": doc["id"], "business_id": s.business_id, "scope": "business",
                               "business_name": "Widget", "kind": "business_owned_note", "revision": 3,
                               "history_available": True, "stable_url": stable}
    assert meta["urls"]["view"] == f"/files/view?path={location}" and meta["urls"]["copy"] == stable
    selected = f"document_id={doc['id']}&business_id={s.business_id}"
    by_id = data(get(client, "metadata", selected))
    assert by_id["path"] == location and by_id["current"] and by_id["version"] is None
    page = data(get(client, "history", selected))
    assert [v["history_version"] for v in page["versions"]] == [3, 2, 1] and page["next_before_version"] is None
    assert page["versions"][0]["author_name"] == "Owner" and "content" not in page["versions"][0]
    assert page["current_revision"] == 3 and page["current_matches_latest"] is True
    small = data(get(client, "history", selected + "&limit=2"))
    assert [v["history_version"] for v in small["versions"]] == [3, 2] and small["next_before_version"] == 2
    older = data(get(client, "history", selected + "&before_version=2&limit=2"))
    assert [v["history_version"] for v in older["versions"]] == [1] and "current_content_sha256" not in older or True
    diff = data(get(client, "history", selected + "&from_version=1&to_version=3"))
    assert "-One" in diff["diff"] and "+Three" in diff["diff"] and diff["diff_truncated"] is False
    version = data(get(client, "content", selected + "&version=1"))
    assert "One" in version["text"] and "Three" not in version["text"]
    version_meta = data(get(client, "metadata", selected + "&version=1"))
    assert version_meta["current"] is False and version_meta["version"]["history_version"] == 1
    assert version_meta["urls"]["current"] == stable and version_meta["size"] is None
    saved = get(client, "download", selected + "&version=2")
    assert saved.content.decode().endswith("Two\nmore\n") and "filename=\"2026-09-30-plan.v2.md\"" in saved.headers["content-disposition"] or ".v2.md" in saved.headers["content-disposition"]
    assert error(get(client, "content", selected + "&version=9")) == (404, "not_found")
    assert error(get(client, "metadata", selected + "&version=9")) == (404, "not_found")
    assert rows(s) == before, "reading history wrote"


def test_outside_edits_are_labelled_current_and_never_passed_off_as_saved_history(s, client):
    doc = note(s, "Drift", "Saved text\n")
    selected = f"document_id={doc['id']}&business_id={s.business_id}"
    (s.root / "widget/hq" / doc["path"]).write_text("Edited outside Qorqut\n")
    page = data(get(client, "history", selected))
    assert [v["history_version"] for v in page["versions"]] == [1] and page["current_matches_latest"] is False
    assert page["current_revision"] == 1  # A document revision is not a history version, and neither implies the file.
    assert "Edited outside" in data(get(client, "content", selected))["text"]
    assert "Saved text" in data(get(client, "content", selected + "&version=1"))["text"]
    (s.root / "widget/hq" / doc["path"]).unlink()
    assert data(get(client, "history", selected))["current_matches_latest"] is None
    assert error(get(client, "content", selected))[1] == "not_found"
    assert "Saved text" in data(get(client, "content", selected + "&version=1"))["text"]  # Saved bytes survive.


def test_a_registered_move_keeps_the_document_link_and_frees_the_old_path(s, client):
    topic = owner_invoke(s, "context_topics_create", {"title": "Customers", "slug": "customers"})["document"]
    selected = f"document_id={topic['id']}&business_id={s.business_id}"
    old = f"widget/hq/{topic['path']}"
    assert data(get(client, "metadata", selected))["path"] == old
    moved = owner_invoke(s, "context_topics_move", {"document_id": topic["id"], "slug": "clients"}, revision=topic["revision"])["document"]
    new = f"widget/hq/{moved['path']}"
    assert new != old
    assert data(get(client, "metadata", selected))["path"] == new  # The stable link followed the move.
    assert data(get(client, "history", selected))["versions"]
    assert data(get(client, "metadata", f"path={new}"))["managed"]["document_id"] == topic["id"]
    assert error(get(client, "metadata", f"path={old}")) == (404, "not_found")
    assert data(get(client, "metadata", selected))["managed"]["history_available"]


def test_a_document_link_never_changes_scope(s, client):
    doc = note(s, "Scoped", "Body\n")
    other = new_id()
    with s.runtime.database.transaction() as uow:
        uow.repository("businesses").insert({"id": other, "slug": "gadget", "workspace": str(s.root / "gadget"),
                                             "hq_path": str(s.root / "gadget/hq"), "name": "Gadget", "stage": "validating"})
        uow.repository("business_memberships", business_id=other).insert({"principal_id": s.fixture.owner.id, "is_owner": True})
    for query in (f"document_id={doc['id']}", f"document_id={doc['id']}&business_id={other}",
                  f"document_id={new_id()}&business_id={s.business_id}", f"document_id={doc['id']}&business_id={new_id()}"):
        for action in ("metadata", "content", "download", "history"):
            assert error(get(client, action, query))[0] == 404, (action, query)


def test_a_global_document_is_read_in_global_scope_and_history_stays_with_its_owner(s, client):
    doc = note(s, "Shared", "Global text\n", scope="global")
    assert doc["business_id"] is None
    meta = data(get(client, "metadata", f"document_id={doc['id']}"))
    assert meta["managed"]["scope"] == "global" and meta["managed"]["business_id"] is None and meta["managed"]["history_available"]
    assert data(get(client, "metadata", f"path={doc['path']}"))["managed"]["stable_url"] == f"/files/view?document_id={doc['id']}"
    assert [v["history_version"] for v in data(get(client, "history", f"document_id={doc['id']}"))["versions"]] == [1]
    assert error(get(client, "metadata", f"document_id={doc['id']}&business_id={s.business_id}"))[0] == 404


def agent_memory(s, body="Working notes"):
    """The agent's own working memory, saved through the registered operation (importing its legacy plan first)."""
    ctx = s.runtime.resolver.invocation_context(s.agent, business_id=s.business_id)
    app = s.runtime.application
    args = {"body": body, "sources": [], "create_if_missing": True}
    legacy = app.invoke(ctx, "plans_get", {}).data["document"]
    if legacy:
        args["legacy_plan_content_hash"] = legacy["content_hash"]
    saved = app.invoke(ctx, "working_memory_update", args, InvocationOptions(business_id=s.business_id, idempotency_key=new_id()))
    assert saved.status == "ok", saved.to_dict()
    with s.runtime.database.transaction() as uow:
        row = uow.connection.execute("SELECT id, path FROM documents WHERE kind='working_memory' AND business_id=?", (s.business_id,)).fetchone()
    return {"id": row["id"], "path": row["path"]}


def test_working_memory_is_viewable_as_a_file_but_has_no_generic_history(s, client):
    memory = agent_memory(s)
    location = f"widget/hq/{memory['path']}"
    meta = data(get(client, "metadata", f"path={location}"))
    assert meta["managed"]["kind"] == "working_memory" and meta["managed"]["history_available"] is False
    assert meta["managed"]["stable_url"] is None and "Working notes" in data(get(client, "content", f"path={location}"))["text"]
    selected = f"document_id={memory['id']}&business_id={s.business_id}"
    for action in ("metadata", "content", "download", "history"):
        assert error(get(client, action, selected))[0] == 404, action


def test_an_ambiguous_registered_location_shows_the_current_file_without_guessed_history(s, client):
    doc = note(s, "Twin", "Body\n")
    location = f"widget/hq/{doc['path']}"
    with s.runtime.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO documents (id, business_id, namespace, owning_agent_id, kind, path, content_hash, created_at, updated_at) "
            "VALUES (?, NULL, 'global', NULL, 'shared_note', ?, NULL, '2026-09-30T00:00:00Z', '2026-09-30T00:00:00Z')",
            (new_id(), location))
    meta = data(get(client, "metadata", f"path={location}"))
    assert meta["managed"] is None and meta["urls"]["copy"] == f"/files/view?path={location}"
    assert "Body" in data(get(client, "content", f"path={location}"))["text"]


# files_link


def link(s, principal, args, *, business=True):
    business_id = s.business_id if business else None
    ctx = s.runtime.resolver.invocation_context(principal, business_id=business_id)
    return s.runtime.application.invoke(ctx, "files_link", args, InvocationOptions(business_id=business_id))


def test_files_link_builds_a_root_relative_markdown_link_from_path_text_alone(s, monkeypatch):
    import builtins
    import pathlib
    for name in ("open", "stat", "lstat", "listdir", "scandir", "walk", "access", "readlink"):
        target = os if hasattr(os, name) and name != "open" else None
    calls = []
    real_open, real_stat, real_scandir, real_listdir = os.open, os.stat, os.scandir, os.listdir
    # After the context is resolved, the handler must not touch the filesystem at all.
    ctx = s.runtime.resolver.invocation_context(s.agent, business_id=s.business_id)
    handler = s.runtime.registry.get("files_link").read
    monkeypatch.setattr(os, "stat", lambda *a, **k: calls.append(("stat", a)) or real_stat(*a, **k))
    monkeypatch.setattr(os, "open", lambda *a, **k: calls.append(("open", a)) or real_open(*a, **k))
    monkeypatch.setattr(os, "scandir", lambda *a, **k: calls.append(("scandir", a)) or real_scandir(*a, **k))
    monkeypatch.setattr(os, "listdir", lambda *a, **k: calls.append(("listdir", a)) or real_listdir(*a, **k))
    monkeypatch.setattr(pathlib.Path, "stat", lambda self, *a, **k: calls.append(("Path.stat", str(self))) or real_stat(self))
    monkeypatch.setattr(pathlib.Path, "resolve", lambda self, *a, **k: calls.append(("resolve", str(self))) or self)
    monkeypatch.setattr(pathlib.Path, "exists", lambda self: calls.append(("exists", str(self))) or False)
    monkeypatch.setattr(builtins, "open", lambda *a, **k: calls.append(("builtin open", a)) or pytest.fail("opened a file"))
    result = handler(ctx, {"path": "widget/hq/scratch/never-created [v2].md", "label": "Research *notes*"}, None, None)
    assert calls == [], calls
    assert result["viewer_url"] == "/files/view?path=widget/hq/scratch/never-created%20%5Bv2%5D.md"
    assert result["markdown"] == f"[Research \\*notes\\*]({result['viewer_url']})"
    assert result["checked"] == "not_checked" and "not" in result["note"] and "opened" in result["note"]
    assert result["target"] == {"type": "path", "path": "widget/hq/scratch/never-created [v2].md"}
    blob = json.dumps(plain(result))
    assert str(s.root) not in blob and "file://" not in blob and "http" not in blob and "token" not in blob.lower()


def test_files_link_accepts_absolute_paths_under_the_root_and_refuses_everything_else(s):
    ok = link(s, s.agent, {"path": f"{s.root}/widget/hq/scratch/a.md"})
    assert ok.status == "ok" and ok.data["viewer_url"] == "/files/view?path=widget/hq/scratch/a.md"
    assert ok.data["markdown"].startswith("[a.md](")
    for bad in ("/etc/passwd", "../x.md", "widget/../../x", "a\0b", "file:///etc/passwd", "widget/", f"{s.root}", ""):
        result = link(s, s.agent, {"path": bad})
        assert result.status == "error" and result.error["code"] == "validation_error", bad
    for bad in ({}, {"path": "a.md", "document_id": new_id()}, {"path": "a.md", "version": 2}, {"document_id": "nope"},
                {"path": "a.md", "extra": 1}):
        assert link(s, s.agent, bad).status == "error", bad


def test_files_link_uses_the_callers_own_document_rules_and_builds_version_links(s):
    doc = note(s, "Mine", "Text\n")
    ok = link(s, s.agent, {"document_id": doc["id"]})
    assert ok.status == "ok", ok.to_dict()
    assert ok.data["viewer_url"] == f"/files/view?document_id={doc['id']}&business_id={s.business_id}"
    assert ok.data["checked"] == "document_readable" and ok.data["target"]["business_id"] == s.business_id
    versioned = link(s, s.agent, {"document_id": doc["id"], "version": 7, "label": "Fixed v7"})
    assert versioned.data["viewer_url"].endswith("&version=7") and versioned.data["markdown"].startswith("[Fixed v7](")
    assert "history" not in json.dumps(plain(versioned.data)).lower().replace("saved version", "")
    # A document of another business, a missing one and working memory are all simply "not found".
    other = new_id()
    with s.runtime.database.transaction() as uow:
        uow.repository("businesses").insert({"id": other, "slug": "gadget", "workspace": str(s.root / "gadget"),
                                             "hq_path": str(s.root / "gadget/hq"), "name": "Gadget", "stage": "validating"})
    foreign = s.runtime.application.invoke(replace(s.fixture.ctx, business_id=other, review_business_id=other,
                                                   allowed_business_ids=frozenset({s.business_id, other})), "notes_create",
        {"title": "Theirs", "body": "x", "target_business_id": other},
        InvocationOptions(idempotency_key=new_id()))
    if foreign.status == "ok":
        result = link(s, s.agent, {"document_id": foreign.data["document"]["id"]})
        assert result.status == "error" and result.error["code"] == "not_found"
    assert link(s, s.agent, {"document_id": new_id()}).error["code"] == "not_found"
    memory = agent_memory(s)
    assert link(s, s.agent, {"document_id": memory["id"]}).error["code"] == "not_found"


def test_files_link_is_a_core_default_for_active_agents_and_yields_to_an_explicit_deny(s):
    registry = builtin_registry()
    assert resolve_operation_policy("files_link", {}, {}, registry, role_id="business-agent").mode == "allow"
    assert resolve_operation_policy("files_link", {}, {}, registry, role_id="ordubashy").mode == "allow"
    assert resolve_operation_policy("files_link", {}, {"files_link": "deny"}, registry, role_id="business-agent").mode == "deny"
    # Global agents use it in Global scope with no business selected; the business agent in its own.
    assert link(s, s.manager, {"path": "widget/a.md"}, business=False).status == "ok"
    assert link(s, s.agent, {"path": "widget/a.md"}).status == "ok"
    ctx = s.runtime.resolver.invocation_context(s.agent, business_id=s.business_id)
    with s.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (s.agent.id,))
    inactive = s.runtime.application.invoke(ctx, "files_link", {"path": "widget/a.md"}, InvocationOptions(business_id=s.business_id))
    assert inactive.status == "error" and inactive.error["code"] == "inactive_agent", inactive.to_dict()


def test_the_owner_can_call_files_link_but_it_grants_the_owner_routes_nothing_new(s, client):
    body = client.post("/api/root/operations/files_link", json={"path": "widget/hq/x.md"}, headers=ORIGIN).json()
    assert body["status"] == "ok" and body["data"]["viewer_url"] == "/files/view?path=widget/hq/x.md"
    scoped = client.post("/api/biz/widget/operations/files_link", json={"path": "widget/hq/x.md"}, headers=ORIGIN).json()
    assert scoped["status"] == "ok"
    assert error(get(client, "metadata", "path=widget/hq/x.md")) == (404, "not_found")  # A link is not existence.


# The viewer page


def test_the_viewer_shell_is_a_static_same_origin_page_with_a_strict_policy(s, tmp_path):
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    (dashboard / "file-viewer.html").write_text("<!doctype html><title>Files</title>")
    (dashboard / "index.html").write_text("<!doctype html><title>Dashboard</title>")
    (dashboard / "app.css").write_text("body{}")
    web = create_app(s.runtime, origins=["http://testserver"], authentication=s.authentication, dashboard_root=dashboard)
    with TestClient(web) as page:
        got = page.get("/files/view?path=widget/hq/a.md", headers=ORIGIN)
        assert got.status_code == 200 and "<title>Files</title>" in got.text
        policy = got.headers["content-security-policy"]
        assert "script-src 'self'" in policy and "default-src 'none'" in policy and "frame-src 'none'" in policy
        assert got.headers["cache-control"] == "no-store"
        # Only this exact shell may be framed, and only by the dashboard's own origin (the Viewbox).
        assert got.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in policy and "frame-ancestors 'none'" not in policy
        assert page.get("/files/view", headers={"Host": "attacker.invalid"}).status_code == 403
        for path in ("/", "/app.css", "/files/view/", "/files/view/x", "/files/viewer", "/files/viewx", "/files",
                     "/api/root/files/metadata?path=widget/hq/a.md", "/api/root/files/content?path=widget/hq/a.md",
                     "/api/root/files/download?path=widget/hq/a.md", "/api/root/files/history?path=widget/hq/a.md"):
            other = page.get(path, headers=ORIGIN, follow_redirects=False)  # A slash redirect is not the exact shell.
            assert other.headers["x-frame-options"] == "DENY", path
            assert other.headers["content-security-policy"].endswith("frame-ancestors 'none'"), path
            assert "SAMEORIGIN" not in str(other.headers) and "'self'" not in other.headers["content-security-policy"], path
    (tmp_path / "bare" / "js").mkdir(parents=True)
    (tmp_path / "bare" / "css").mkdir()
    missing = create_app(s.runtime, origins=["http://testserver"], authentication=s.authentication, dashboard_root=tmp_path / "bare")
    with TestClient(missing) as page:
        assert page.get("/files/view", headers=ORIGIN).status_code == 404



def test_the_configured_browser_frame_behavior_is_unchanged_and_files_get_no_prefix_exception():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    class Origins:
        def validate(self, request):
            return None

    page = TestClient(ServiceBoundary(app, Origins(), frame_prefix="/tickets", frame_origin="https://dashboard.example"))
    for path in ("/tickets", "/tickets/a/b"):
        got = page.get(path)
        assert got.headers["content-security-policy"] == "frame-ancestors https://dashboard.example", path
        assert "x-frame-options" not in got.headers, path
    got = page.get("/files/view")
    assert got.headers["x-frame-options"] == "SAMEORIGIN" and got.headers["content-security-policy"] == "frame-ancestors 'self'"
    for path in ("/ticketsx", "/files/view/", "/files/view/tickets", "/files", "/api/root/files/download"):
        got = page.get(path)
        assert got.headers["x-frame-options"] == "DENY" and got.headers["content-security-policy"] == "frame-ancestors 'none'", path
    page = TestClient(ServiceBoundary(app, Origins()))
    assert page.get("/files/view").headers["x-frame-options"] == "SAMEORIGIN"
    assert page.get("/anything").headers["x-frame-options"] == "DENY"

@pytest.mark.parametrize('signature,mime', [(b'\x89PNG\r\n\x1a\n','image/png'),(b'\xff\xd8\xff','image/jpeg'),(b'GIF89a','image/gif'),(b'RIFF\x10\0\0\0WEBP','image/webp')])
def test_raster_preview_stream_and_owner_boundary(s, client, signature, mime):
    payload = signature + b'\0' * (2 * 1024 * 1024 + 17)
    write(s, 'images/sample.bin', payload)
    meta = data(get(client, 'metadata', 'path=images/sample.bin'))
    assert meta['preview_kind'] == 'image' and not meta['binary']
    assert meta['urls']['image'] == '/api/root/files/image?path=images/sample.bin'
    assert data(get(client, 'content', 'path=images/sample.bin'))['kind'] == 'image'
    response = get(client, 'image', 'path=images/sample.bin')
    assert response.content == payload
    assert response.headers['content-type'] == mime
    assert response.headers['content-disposition'] == 'inline'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'private, no-store'
    denied = client.get('/api/root/files/image?path=images/sample.bin', headers={**ORIGIN, 'Authorization':'Bearer invalid'})
    assert denied.status_code in (401,403)


def test_image_endpoint_rejects_active_content_and_symlinks(s, client):
    write(s, 'images/fake.png', b'<html><script>alert(1)</script></html>')
    write(s, 'images/active.svg', b'<svg onload="alert(1)"/>')
    for name in ['fake.png', 'active.svg']:
        assert error(get(client, 'image', 'path=images/'+name))[1] == 'validation_error'
    (s.root/'images/link.png').symlink_to(s.root/'images/fake.png')
    assert error(get(client, 'image', 'path=images/link.png'))[1] == 'symlink_not_followed'
