"""Agent Viewbox: placement authority, link/title validation, ordering, owner-only reads. No network or model."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from bazarlyk.domains.viewbox import canonical_link, normalize_title
from bazarlyk.storage import Database
from test_global_agents import global_agent
from test_service_transports import ORIGIN, ServiceFixture

TABLE = new_id()
DOC = new_id()


@pytest.fixture
def service(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    service.viewbox = service.runtime.domains.viewbox
    service.viewbox.configure_origins(["http://testserver", "http://127.0.0.1:1071", "http://qorqut.tail1234.ts.net:1071"])
    service.app = service.runtime.application
    service.business_agent = service.runtime.resolver.invocation_context(service.agent)
    service.ordubashy = service.runtime.resolver.invocation_context(service.manager)
    service.applier = global_agent(service, "global-operator")
    service.global_agent = service.runtime.resolver.invocation_context(service.applier)
    return service


def place(service, ctx, link, title="Research", key=None):
    return service.app.invoke(ctx, "viewbox_place", {"link": link, "title": title}, {"idempotency_key": key or new_id()})


def owner_read(service, agent_id):
    with service.runtime.database.read() as uow:
        owner = service.runtime.policy.context_for(service.fixture.owner.id, business_id=None, review_business_id=None, uow=uow)
    return service.viewbox.owner_board(owner, agent_id)


def test_migration_adds_tables_and_preserves_existing_rows(tmp_path):
    database = Database(tmp_path / "state.sqlite3").initialize(through=107)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": new_id(), "kind": "human", "display_name": "Owner"})
    assert not {row[0] for row in database._connect().execute("SELECT name FROM sqlite_master")} & {"agent_viewbox_boards", "agent_viewbox_items"}
    database.initialize()
    with database.read() as uow:
        assert uow.connection.execute("SELECT display_name FROM principals").fetchone()[0] == "Owner"
        tables = {row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"agent_viewbox_boards", "agent_viewbox_items"} <= tables
        assert uow.connection.execute("PRAGMA user_version").fetchone()[0] == len(Database.migrations()) >= 108


def test_business_and_global_agents_keep_separate_boards(service):
    for ctx, agent in ((service.business_agent, service.agent), (service.ordubashy, service.manager),
                       (service.global_agent, service.applier)):
        result = place(service, ctx, f"http://qorqut:3000/{agent.id}", "Landing page")
        assert result.status == "ok", result.to_dict()
        assert result.data["kind"] == "local" and result.data["board_revision"] == 1
    for agent in (service.agent, service.manager, service.applier):
        board = owner_read(service, agent.id)
        assert [item["link"] for item in board["items"]] == [f"http://qorqut:3000/{agent.id}"]
        assert board["revision"] == 1 and set(board["items"][0]) == {"id", "link", "title", "kind", "placed_at", "attention_id"}
    # Business agents selected on Global (an ecosystem agent working in a business) use the same board.
    selected = service.runtime.resolver.invocation_context(service.manager, business_id=service.fixture.business_id)
    assert place(service, selected, "https://example.com/selected").status == "ok"
    assert [i["link"] for i in owner_read(service, service.manager.id)["items"]][0] == "https://example.com/selected"


def test_latest_thirty_unique_newest_first_and_repin_keeps_identity(service):
    ctx = service.business_agent
    first = place(service, ctx, "https://example.com/0", "Zero")
    attention = owner_read(service, service.agent.id)["items"][0]["attention_id"]
    for index in range(1, 32):
        assert place(service, ctx, f"https://example.com/{index}").status == "ok"
    board = owner_read(service, service.agent.id)
    assert [i["link"] for i in board["items"]] == [f"https://example.com/{n}" for n in range(31, 1, -1)]
    assert board["revision"] == 32
    repin = place(service, ctx, "https://example.com/0", "Again")
    assert repin.data["id"] == first.data["id"] and repin.data["repinned"] and repin.data["board_revision"] == 33
    top = owner_read(service, service.agent.id)["items"]
    assert len(top) == 30 and top[0]["id"] == first.data["id"] and top[0]["title"] == "Again"
    assert top[0]["attention_id"] != attention
    with service.runtime.database.read() as uow:
        rows = uow.connection.execute("SELECT count(*) FROM agent_viewbox_items WHERE agent_id=?", (service.agent.id,)).fetchone()[0]
    assert rows == 32  # Every unique placement is kept; only thirty are exposed.


def test_transport_retry_does_not_bump_but_a_new_invocation_repins(service):
    ctx = service.business_agent
    key = new_id()
    first = place(service, ctx, "https://example.com/a", key=key)
    place(service, ctx, "https://example.com/b")
    retry = place(service, ctx, "https://example.com/a", key=key)
    assert retry.status == "ok" and retry.data == first.data
    assert [i["link"] for i in owner_read(service, service.agent.id)["items"]] == ["https://example.com/b", "https://example.com/a"]
    assert owner_read(service, service.agent.id)["revision"] == 2
    conflict = place(service, ctx, "https://example.com/other", key=key)
    assert conflict.status == "error" and conflict.error["code"] == "idempotency_conflict"
    place(service, ctx, "https://example.com/a")
    assert owner_read(service, service.agent.id)["items"][0]["link"] == "https://example.com/a"


@pytest.mark.parametrize("raw, expected", [
    ("  https://Example.COM:443/Path/A?b=2&a=1#Frag  ", "https://example.com/Path/A?b=2&a=1#Frag"),
    ("http://example.com:80", "http://example.com/"),
    ("http://example.com:8080/x", "http://example.com:8080/x"),
    ("https://example.com./x", "https://example.com/x"),
    ("http://[::1]:3000/", "http://[::1]:3000/"),
    (f"/tables/view?table_id={TABLE}", f"/tables/view?table_id={TABLE}"),
    (f"http://qorqut:1071/tables/view?table_id={TABLE}&business_id={DOC}", f"/tables/view?table_id={TABLE}&business_id={DOC}"),
    (f"https://qorqut.tail1234.ts.net:1071/files/view?document_id={DOC}#top", f"/files/view?document_id={DOC}#top"),
    ("/files/view?path=clients%2Facme%2Fnotes.md", "/files/view?path=clients%2Facme%2Fnotes.md"),
    # A non-app host with a viewer-looking path stays an ordinary external link.
    (f"http://qorqut.evil/files/view?path=a", "http://qorqut.evil/files/view?path=a"),
])
def test_link_canonicalization(raw, expected):
    authorities = frozenset({("qorqut", 1071), ("qorqut.tail1234.ts.net", 1071)})
    assert canonical_link(raw, authorities) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "javascript:alert(1)", "data:text/html,hi", "file:///etc/passwd", "blob:http://a/b", "//evil.com/x",
    "ftp://example.com/x", "/etc/passwd", "/tables/view", "/tables/view?table_id=nope", "/files/view?path=a&document_id=" + DOC,
    "/files/view?document_id=" + DOC + "&document_id=" + DOC, "/files/view?path=a&extra=1", "/\\evil.com/files/view?path=a",
    "https://user:pw@example.com/", "https://user@example.com/", "https://exa mple.com/", "https://example.com/a\nb",
    "https://example.com/a\x00b", "https://example.com\r\n/x", "https://example.com:99999/", "https://example.com:0/",
    "https://example.com:/", "http://", "http:example.com", "https://ex_ample!.com/", "https://[::1/", "https://-bad-.example/",
    "https://example.com/" + "a" * 4100,
])
def test_hostile_or_malformed_links_are_rejected(raw):
    with pytest.raises(DomainError) as error:
        canonical_link(raw)
    assert error.value.code == "validation_error"


def test_fragment_and_query_distinguish_links_but_host_case_and_default_port_do_not(service):
    ctx = service.business_agent
    for link in ("https://example.com/p", "https://EXAMPLE.com:443/p", "https://example.com/p#one", "https://example.com/p?a=1&b=2",
                 "https://example.com/p?b=2&a=1", "https://example.com/P"):
        assert place(service, ctx, link).status == "ok"
    with service.runtime.database.read() as uow:
        stored = [r[0] for r in uow.connection.execute(
            "SELECT canonical_link FROM agent_viewbox_items WHERE agent_id=? ORDER BY placement_sequence", (service.agent.id,))]
    assert stored == ["https://example.com/p#one", "https://example.com/p?a=1&b=2", "https://example.com/p?b=2&a=1",
                      "https://example.com/P"] or len(stored) == 5
    assert len(stored) == 5 and "https://example.com/p" in stored


def test_absolute_and_relative_canonical_viewer_links_share_one_record_and_kind(service):
    ctx = service.business_agent
    relative = place(service, ctx, f"/tables/view?table_id={TABLE}", "Sales table")
    absolute = place(service, ctx, f"http://qorqut:1071/tables/view?table_id={TABLE}", "Sales table")
    assert relative.data["id"] == absolute.data["id"] and absolute.data["link"] == f"/tables/view?table_id={TABLE}"
    assert absolute.data["kind"] == "table"
    assert place(service, ctx, f"/files/view?path=a.md").data["kind"] == "file"
    kinds = {link: place(service, ctx, link).data["kind"] for link in (
        "http://qorqut:3000/", "https://qorqut.tail1234.ts.net:8443/app", "http://qorqut.evil:3000/",
        "https://example.com/qorqut", "http://localhost:3000/", "http://127.0.0.1:3000/", "http://qorqut.example.com/")}
    assert kinds == {"http://qorqut:3000/": "local", "https://qorqut.tail1234.ts.net:8443/app": "local",
                     "http://qorqut.evil:3000/": "web", "https://example.com/qorqut": "web",
                     "http://localhost:3000/": "web", "http://127.0.0.1:3000/": "web", "http://qorqut.example.com/": "web"}


@pytest.mark.parametrize("raw, stored", [("  Launch   plan  ", "Launch plan"), ("Brief\nnotes\tfor", "Brief notes for"),
                                         ("Zażółć gęślą", "Zażółć gęślą"), ("x" * 80, "x" * 80)])
def test_title_normalization(raw, stored):
    assert normalize_title(raw) == stored


@pytest.mark.parametrize("raw", ["", "   ", "one two three four", "a" * 81, "bad\x00title", "zero​width", "esc\x1b[0m title"])
def test_bad_titles_are_rejected_not_truncated(raw):
    with pytest.raises(DomainError) as error:
        normalize_title(raw)
    assert error.value.code == "validation_error"


def test_invalid_arguments_fail_before_any_write(service):
    ctx = service.business_agent
    for args in ({"link": "javascript:alert(1)", "title": "Hi"}, {"link": "https://example.com", "title": "a b c d"},
                 {"link": "https://example.com", "title": "x", "agent_id": service.manager.id},
                 {"link": "https://example.com", "title": "x", "kind": "file"}, {"link": "https://example.com"}):
        result = service.app.invoke(ctx, "viewbox_place", args, {"idempotency_key": new_id()})
        assert result.status == "error" and result.error["code"] == "validation_error", (args, result.to_dict())
    assert owner_read(service, service.agent.id) == {"agent_id": service.agent.id, "revision": 0, "items": []}


def test_only_a_registered_agent_can_place_and_only_on_its_own_board(service):
    owner = service.fixture.ctx
    for ctx in (owner, replace(owner, source="mcp"), replace(owner, source="worker")):
        result = place(service, ctx, "https://example.com/x")
        assert result.status == "error" and result.error["code"] in {"forbidden", "invalid_principal", "policy_denied", "scope_required"}
    # The board is derived from the authenticated principal, never from an argument.
    result = service.app.invoke(service.business_agent, "viewbox_place",
                                {"link": "https://example.com/x", "title": "Mine", "agent_id": service.manager.id}, {"idempotency_key": new_id()})
    assert result.status == "error"
    assert place(service, service.business_agent, "https://example.com/mine").status == "ok"
    assert owner_read(service, service.manager.id)["items"] == []
    assert len(owner_read(service, service.agent.id)["items"]) == 1


def test_explicit_deny_and_revocation_hold_at_prepare_and_apply(service):
    ctx = service.business_agent
    assert place(service, ctx, "https://example.com/ok").status == "ok"
    descriptor = service.runtime.registry.get("viewbox_place")
    args = descriptor.validate({"link": "https://example.com/later", "title": "Later"})
    with service.runtime.database.read() as uow:
        plan = descriptor.prepare(ctx, args, InvocationOptions(idempotency_key=new_id()), uow)
    service.fixture.update(service.agent, {"tool_policy": {"overrides": {"viewbox_place": "deny"}}})
    denied = service.runtime.resolver.invocation_context(service.agent)
    assert place(service, denied, "https://example.com/blocked").error["code"] == "policy_denied"
    with service.runtime.database.transaction() as uow, pytest.raises(DomainError) as error:
        service.app.apply_prepared(denied, plan, uow)
    assert error.value.code in {"policy_denied", "context_refresh_required"}
    assert [i["link"] for i in owner_read(service, service.agent.id)["items"]] == ["https://example.com/ok"]
    # A deny on one agent does not affect another; "signoff" is not a valid mode for this operation.
    assert place(service, service.ordubashy, "https://example.com/fine").status == "ok"
    with pytest.raises(DomainError):
        service.fixture.update(service.manager, {"tool_policy": {"overrides": {"viewbox_place": "signoff"}}})


def test_concurrent_placements_serialize_without_loss(service):
    ctx = service.business_agent
    barrier = threading.Barrier(8)

    def run(index):
        barrier.wait()
        link = "https://example.com/same" if index % 2 else f"https://example.com/distinct/{index}"
        return place(service, ctx, link, "Race")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(run, range(8)))
    assert all(r.status == "ok" for r in results), [r.to_dict() for r in results if r.status != "ok"]
    board = owner_read(service, service.agent.id)
    links = [i["link"] for i in board["items"]]
    assert sorted(links) == sorted(["https://example.com/same", *[f"https://example.com/distinct/{i}" for i in (0, 2, 4, 6)]])
    assert board["revision"] == 8
    with service.runtime.database.read() as uow:
        sequences = [r[0] for r in uow.connection.execute(
            "SELECT placement_sequence FROM agent_viewbox_items WHERE agent_id=?", (service.agent.id,))]
    assert len(set(sequences)) == len(sequences) == 5


def test_owner_reads_are_owner_only_bounded_and_change_nothing(service):
    place(service, service.business_agent, "https://example.com/x")
    with service.runtime.database.read() as uow:
        before = [tuple(r) for r in uow.connection.execute("SELECT * FROM agent_viewbox_items")] + \
                 [tuple(r) for r in uow.connection.execute("SELECT * FROM agent_viewbox_boards")]
        unread = uow.connection.execute("SELECT count(*) FROM inbox_events").fetchone()[0]
    with TestClient(service.web) as client:
        ok = client.get(f"/api/root/agents/{service.agent.id}/viewbox", headers=ORIGIN)
        assert ok.status_code == 200, ok.text
        data = ok.json()["data"]
        assert data["agent_id"] == service.agent.id and [i["link"] for i in data["items"]] == ["https://example.com/x"]
        tag = ok.headers["etag"]
        again = client.get(f"/api/root/agents/{service.agent.id}/viewbox", headers={**ORIGIN, "If-None-Match": tag})
        assert again.status_code == 304
        assert client.get(f"/api/root/agents/{new_id()}/viewbox", headers=ORIGIN).status_code == 404
        assert client.get("/api/root/agents/not-an-id/viewbox", headers=ORIGIN).status_code == 404
        assert client.get(f"/api/root/agents/{service.fixture.owner.id}/viewbox", headers=ORIGIN).status_code == 404
        assert client.post(f"/api/root/agents/{service.agent.id}/viewbox", headers=ORIGIN, json={}).status_code >= 400
        assert ok.headers["cache-control"] == "private, no-store" and again.headers["cache-control"] == "private, no-store"
        # The origin check still applies.
        mcp = service.initialize(client, service.manager)
        assert client.get(f"/api/root/agents/{service.agent.id}/viewbox", headers={"Origin": "http://evil.example"}).status_code == 403
        listed = client.post("/mcp", headers=mcp, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).json()
        assert "viewbox_place" in {tool["name"] for tool in listed["result"]["tools"]}
    with service.runtime.database.read() as uow:
        assert before == [tuple(r) for r in uow.connection.execute("SELECT * FROM agent_viewbox_items")] + \
                         [tuple(r) for r in uow.connection.execute("SELECT * FROM agent_viewbox_boards")]
        assert unread == uow.connection.execute("SELECT count(*) FROM inbox_events").fetchone()[0]
    with pytest.raises(DomainError) as error:
        service.viewbox.owner_board(service.business_agent, service.agent.id)
    assert error.value.code == "forbidden"


def test_no_bearer_credential_can_read_the_owner_board(service):
    from test_file_viewer_http import stored_human_credentials
    service.business_id = service.fixture.business_id
    place(service, service.business_agent, "https://example.com/private", "Secret")
    url = f"/api/root/agents/{service.agent.id}/viewbox"
    tokens = stored_human_credentials(service)
    headers = [{"Authorization": "Bearer " + token} for token in tokens.values()]
    headers += [{"Authorization": "Bearer " + service.tokens[service.agent.id]}, {"Authorization": "Bearer " + service.tokens[service.manager.id]},
                {"Authorization": "Bearer " + "x" * 40}, {"Authorization": "Bearer "}, {"Authorization": ""}, {"Authorization": "Basic abc"}]
    with TestClient(service.web) as client:
        service.login(client)
        for header in headers:
            for extra in ({}, {"If-None-Match": "*"}):
                got = client.get(url, headers={**ORIGIN, **header, **extra})
                assert got.status_code == 403 and got.json()["error"]["code"] == "forbidden", (header, got.text)
                assert "example.com" not in got.text and "Secret" not in got.text and len(got.content) < 400
        # The tokenless owner browser request still reads, revalidates and gets a bounded error for an unknown agent.
        ok = client.get(url, headers=ORIGIN)
        assert ok.status_code == 200 and ok.json()["data"]["items"][0]["title"] == "Secret"
        assert ok.headers["cache-control"] == "private, no-store"
        assert client.get(url, headers={**ORIGIN, "If-None-Match": ok.headers["etag"]}).status_code == 304
        missing = client.get(f"/api/root/agents/{new_id()}/viewbox", headers=ORIGIN)
        assert missing.status_code == 404 and "items" not in missing.text and len(missing.content) < 400


def test_existing_roles_discover_it_and_receive_guidance(service):
    with TestClient(service.web) as client:
        for principal in (service.agent, service.manager):
            headers = service.initialize(client, principal)
            listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).json()
            tool = next(t for t in listed["result"]["tools"] if t["name"] == "viewbox_place")
            properties = tool["inputSchema"]["properties"]
            assert {"link", "title"} <= set(properties) and "agent_id" not in properties and "kind" not in properties
            assert tool["inputSchema"]["required"][:2] == ["link", "title"] or set(tool["inputSchema"]["required"]) >= {"link", "title"}
    for principal in (service.agent, service.manager, service.applier):
        rendered = service.runtime.resolver.resolve_agent_config(principal).to_dict()["mandatory_context"]["rendered"]
        assert "viewbox_place(link, title)" in rendered and "never file:// or localhost" in rendered
        profile, role = service.runtime.resolver.profile(principal)[0], None
        from bazarlyk.agents.resolver import effective_tool_policy
        with service.runtime.database.read() as uow:
            role = service.runtime.resolver.catalog.get("role", profile.data["role"], connection=uow.connection)
        row = next(r for r in effective_tool_policy(profile, role, service.runtime.registry) if r["canonical_operation"] == "viewbox_place")
        assert row["available"] and row["effective_mode"] == "allow" and row["source"] == {"kind": "core_default"}


def test_placement_never_touches_the_target(service, monkeypatch):
    import socket
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("placing a link must not resolve or fetch it")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert place(service, service.business_agent, "http://no-such-host.invalid/x").status == "ok"
    assert place(service, service.business_agent, "/files/view?path=does%2Fnot%2Fexist.md").status == "ok"
