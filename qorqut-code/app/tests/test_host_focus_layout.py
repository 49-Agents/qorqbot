"""Acceptance cases: the owner's personal Focus text blocks (``/api/root/agents/{id}/focus-layout``).

A real synthetic host and HTTP application over temporary paths. A layout is a durable owner preference under
``runtime/dashboard-preferences``: exact plain text per agent, revision-checked, read by no agent, and never a Focus card, task,
message or notification. No model, provider, network or operational data."""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain
from starlette.testclient import TestClient

from bazarlyk import focus_layout_preferences as preferences
from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources

ORIGIN = {"Origin": "http://testserver"}
TEXT = "  Today\n\n\tindented  \r\n\n"      # leading, inner and trailing whitespace, a tab, CRLF and trailing newlines


def serve(runtime, dashboard):
    """A fresh authentication and web application over the same runtime, as after a restart or in another browser."""
    authentication = Authentication(runtime.database, runtime.paths)
    app = create_app(runtime, dashboard_root=dashboard, origins=["http://testserver"], authentication=authentication,
                     manage_lifecycle=False)
    client = TestClient(app, base_url="http://testserver")
    client.cookies.set("bazarlyk_session", authentication.issue_browser())
    return client


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path)
    register_sources(instance, ids["first"], "first")
    ordubashy, colleague = create_registered_global(instance, "ordubashy"), create_worker(instance, "colleague")
    runtime = instance.runtime
    attach_background(runtime, runtime.programs)
    runtime.freeze_registry()
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    client = serve(runtime, dashboard)
    with client:
        yield SimpleNamespace(
            instance=instance, runtime=runtime, client=client, ordubashy=ordubashy, colleague=colleague, dashboard=dashboard,
            owner=InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard"),
            context={name: runtime.resolver.invocation_context(Principal(identity, "agent"))
                     for name, identity in (("ordubashy", ordubashy), ("colleague", colleague))})


def block(text="", before=None, after=None, identity=None):
    return {"id": identity or new_id(), "text": text, "before_id": before, "after_id": after}


def fetch(host, agent, query="", client=None, **headers):
    return (client or host.client).get(f"/api/root/agents/{agent}/focus-layout{query}", headers={**ORIGIN, **headers})


def save(host, agent, body, query="", client=None, **headers):
    return (client or host.client).post(f"/api/root/agents/{agent}/focus-layout{query}", headers={**ORIGIN, **headers}, json=body)


def raw(host, agent, body):
    return host.client.post(f"/api/root/agents/{agent}/focus-layout", headers={**ORIGIN, "Content-Type": "application/json"},
                            content=json.dumps(body).encode())


def store(host):
    return host.runtime.paths.runtime / "dashboard-preferences" / (
        hashlib.sha256(host.instance.owner.encode()).hexdigest() + ".focus-layouts.json")


def preference_files(host):
    directory = host.runtime.paths.runtime / "dashboard-preferences"
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def invoke(host, ctx, operation, arguments):
    result = host.runtime.application.invoke(ctx, operation, {"scope": "global", **arguments}, InvocationOptions(idempotency_key=new_id()))
    assert result.status == "ok", result.error
    return plain(result.data)


def present(host, key):
    return invoke(host, host.owner, "focus_present", {"reference": {
        "key": key, "title": key, "target": {"type": "url", "url": f"https://example.com/{key}"}}})["item"]


def sql(host, statement):
    with host.runtime.database.read() as uow:
        return [tuple(row) for row in uow.connection.execute(statement)]


def test_text_persists_exactly_per_agent_survives_reopen_and_a_retry_changes_nothing(host):
    first = fetch(host, host.ordubashy)
    assert first.status_code == 200 and first.headers["cache-control"] == "private, no-store" and first.json()["status"] == "ok"
    assert first.json()["data"] == {"agent_id": host.ordubashy, "revision": 0, "blocks": [], "card_ids": []}
    assert preference_files(host) == []                                    # reading creates nothing

    anchor = new_id()
    blocks = [block(TEXT, before=anchor), block(""), block("\n\n\n", after=anchor), block("Heading")]
    saved = save(host, host.ordubashy, {"expected_revision": 0, "blocks": blocks})
    assert saved.status_code == 200 and saved.headers["cache-control"] == "private, no-store"
    assert saved.json()["data"] == {"agent_id": host.ordubashy, "revision": 1, "blocks": blocks, "card_ids": []}
    assert [item["text"] for item in saved.json()["data"]["blocks"]] == [TEXT, "", "\n\n\n", "Heading"]    # no trimming, no markup
    assert (os.stat(store(host)).st_mode & 0o777) == 0o600 and (os.stat(store(host).parent).st_mode & 0o077) == 0
    neighbor = store(host).parent / "other.viewbox-dismissed.json"
    neighbor.write_text('{"x": ["y"]}')

    # A lost response is retried with the old revision and the same blocks: success, unchanged, nothing rewritten.
    content = store(host).read_bytes()
    replay = save(host, host.ordubashy, {"expected_revision": 0, "blocks": blocks})
    assert replay.status_code == 200 and replay.json()["data"]["revision"] == 1 and store(host).read_bytes() == content

    # Each agent has its own layout and revision; another browser and a restarted service see the same durable text.
    other = save(host, host.colleague, {"expected_revision": 0, "blocks": [block("Colleague note")]})
    assert other.json()["data"]["revision"] == 1 and other.json()["data"]["blocks"][0]["text"] == "Colleague note"
    reopened = serve(host.runtime, host.dashboard)
    with reopened:
        assert fetch(host, host.ordubashy, client=reopened).json()["data"]["blocks"] == blocks
        assert [b["text"] for b in fetch(host, host.colleague.upper(), client=reopened).json()["data"]["blocks"]] == ["Colleague note"]
    assert preferences.read_layout(host.runtime.paths.runtime, host.instance.owner, host.ordubashy)["blocks"] == blocks
    assert preferences.read_layout(host.runtime.paths.runtime, new_id(), host.ordubashy) == {"revision": 0, "blocks": []}

    # Emptying a layout is a change that keeps its revision line; saving one agent never touches the other.
    emptied = save(host, host.ordubashy, {"expected_revision": 1, "blocks": []})
    assert emptied.json()["data"]["revision"] == 2 and emptied.json()["data"]["blocks"] == []
    assert save(host, host.ordubashy, {"expected_revision": 1, "blocks": [block("late")]}).status_code == 409
    assert fetch(host, host.colleague).json()["data"]["revision"] == 1
    assert json.loads(store(host).read_text())["agents"][host.ordubashy] == {"blocks": [], "revision": 2}
    assert neighbor.read_text() == '{"x": ["y"]}' and not [name for name in preference_files(host) if name.startswith(".")]


def test_a_stale_save_is_rejected_without_writing_and_the_request_is_strictly_validated(host):
    one, two = block("one"), block("two")
    assert save(host, host.ordubashy, {"expected_revision": 0, "blocks": [one]}).status_code == 200
    content = store(host).read_bytes()

    stale = save(host, host.ordubashy, {"expected_revision": 0, "blocks": [one, two]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "revision_conflict"
    assert stale.json()["error"]["current_revision"] == 1 and "one" not in stale.text
    assert save(host, host.ordubashy, {"expected_revision": 5, "blocks": [one, two]}).status_code == 409   # a future revision is no better
    assert store(host).read_bytes() == content

    clone = new_id()
    invalid = {
        "no body fields": {},
        "extra field": {"expected_revision": 1, "blocks": [], "card_ids": []},
        "no revision": {"blocks": []},
        "negative revision": {"expected_revision": -1, "blocks": []},
        "boolean revision": {"expected_revision": True, "blocks": []},
        "float revision": {"expected_revision": 1.0, "blocks": []},
        "blocks not a list": {"expected_revision": 1, "blocks": {"a": 1}},
        "block not an object": {"expected_revision": 1, "blocks": ["text"]},
        "extra block field": {"expected_revision": 1, "blocks": [{**two, "kind": "heading"}]},
        "missing block field": {"expected_revision": 1, "blocks": [{"id": new_id(), "text": ""}]},
        "text not a string": {"expected_revision": 1, "blocks": [{**two, "text": None}]},
        "text too long": {"expected_revision": 1, "blocks": [block("x" * 8001)]},
        "lone surrogate": {"expected_revision": 1, "blocks": [block("\ud800")]},
        "uppercase id": {"expected_revision": 1, "blocks": [{**two, "id": two["id"].upper()}]},
        "not a uuid": {"expected_revision": 1, "blocks": [{**two, "id": "nope"}]},
        "bad anchor": {"expected_revision": 1, "blocks": [{**two, "before_id": "nope"}]},
        "duplicate ids": {"expected_revision": 1, "blocks": [block("a", identity=clone), block("b", identity=clone)]},
        "anchored to a text block": {"expected_revision": 1, "blocks": [one, {**two, "after_id": one["id"]}]},
        "too many blocks": {"expected_revision": 1, "blocks": [block() for _ in range(201)]},
    }
    for name, body in invalid.items():
        refused = raw(host, host.ordubashy, body)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "validation_error", name
    # The limits are exact: 200 blocks of 8000 characters pass the validation (only the encoded request size can stop them).
    assert save(host, host.ordubashy, {"expected_revision": 1, "blocks": [block("é" * 8000)] + [block() for _ in range(199)]}).status_code == 200
    large = save(host, host.ordubashy, {"expected_revision": 2, "blocks": [block("y" * 8000) for _ in range(140)]})
    assert large.status_code == 422 and large.json()["error"]["code"] == "validation_error" and "1 MiB" in large.text
    path = f"/api/root/agents/{host.ordubashy}/focus-layout"
    assert host.client.post(path, headers=ORIGIN, json=[one]).status_code == 400
    assert host.client.post(path, headers=ORIGIN, content=b"{").status_code == 400
    assert host.client.post(path, headers={**ORIGIN, "Content-Type": "text/plain"}, content=b"{}").status_code == 400
    assert fetch(host, host.ordubashy).json()["data"]["revision"] == 2
    for query in ("?include_archived=2", "?include_deferred=true", "?include_archived=1&include_archived=0", "?other=1"):
        assert fetch(host, host.ordubashy, query).status_code == 422, query
    assert json.loads(store(host).read_text())["agents"].keys() == {host.ordubashy}


def test_only_the_origin_checked_tokenless_owner_dashboard_reaches_an_active_registered_agent(host):
    body = {"expected_revision": 0, "blocks": [block("private")]}
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer "}, {"Authorization": "Bearer " + "x" * 40}, {"Authorization": "Basic abc"}):
        for response in (fetch(host, host.ordubashy, **headers), save(host, host.ordubashy, body, **headers)):
            assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden", headers
            assert "private" not in response.text and len(response.content) < 400
    assert host.client.post(f"/api/root/agents/{host.ordubashy}/focus-layout", headers={"Origin": "http://evil.example"},
                            json=body).status_code == 403
    assert save(host, host.ordubashy, body, **{"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"}).status_code == 403
    assert host.client.put(f"/api/root/agents/{host.ordubashy}/focus-layout", headers=ORIGIN, json=body).status_code in {404, 405}
    assert preference_files(host) == []
    # The domain layer refuses every principal but the installation owner, an agent included.
    for ctx in (host.context["ordubashy"], host.context["colleague"], InvocationContext(Principal(new_id(), "human"), source="dashboard")):
        with pytest.raises(DomainError) as error:
            preferences.layout(host.runtime, ctx, host.ordubashy, body)
        assert error.value.code in {"forbidden", "invalid_principal"}
    assert preference_files(host) == []

    # Only an active registered agent has a layout: unknown, malformed, the owner's own ID and a deactivated agent are all unavailable.
    for target in (new_id(), "not-an-id", host.instance.owner, host.ordubashy + "0"):
        assert fetch(host, target).status_code == 404 and save(host, target, body).status_code == 404, target
    assert save(host, host.ordubashy, body).status_code == 200
    with host.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.colleague,))
    assert fetch(host, host.colleague).status_code == 404 and save(host, host.colleague, body).status_code == 404
    assert json.loads(store(host).read_text())["agents"].keys() == {host.ordubashy}


def test_card_ids_list_every_matching_card_across_pages_and_a_save_changes_no_operational_record(host):
    cards = [present(host, f"card-{number}") for number in range(25)]
    archived, postponed = cards[3], cards[7]
    invoke(host, host.owner, "focus_set_archived", {"item_id": archived["id"], "expected_card_revision": archived["revision"], "archived": True})
    later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
    invoke(host, host.owner, "focus_edit", {"item_id": postponed["id"], "expected_card_revision": postponed["revision"],
                                            "changes": {"deferred_until": later}})

    def paged(**flags):
        """What the board lists page by page for these filters, in board order."""
        ids, offset = [], 0
        while offset is not None:
            page = invoke(host, host.owner, "focus_get", {"limit": 10, "offset": offset, **flags})
            ids += [item["id"] for item in page["items"]]
            offset = page["next_offset"]
        return ids

    def listed(query, **flags):
        response = fetch(host, host.ordubashy, query)
        assert response.status_code == 200
        assert response.json()["data"]["card_ids"] == paged(**flags)
        return response.json()["data"]["card_ids"]

    everything = [card["id"] for card in cards]
    assert len(paged()) == 23 and len(paged()) > 10                                  # more than one page of the board
    assert listed("") == listed("?include_archived=0&include_deferred=0") == [i for i in everything if i not in (archived["id"], postponed["id"])]
    assert archived["id"] in listed("?include_archived=1", include_archived=True) and postponed["id"] not in paged(include_archived=True)
    assert postponed["id"] in listed("?include_deferred=1", include_deferred=True) and archived["id"] not in paged(include_deferred=True)
    assert listed("?include_archived=1&include_deferred=1", include_archived=True, include_deferred=True) == everything

    # The configured coordinator reads the shared board through its own tool; owner layouts never reach it.
    with host.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (host.ordubashy,))
    tables = ("focus_cards", "focus_notes", "agent_focus", "tasks", "agent_messages", "inbox_changes", "change_sequences")
    task = invoke(host, host.owner, "tasks_create", {"title": "Plain task"})["task"]
    seen = invoke(host, host.context["ordubashy"], "focus_get", {})
    before = {table: sql(host, f"SELECT * FROM {table}") for table in tables}
    anchored = [block("Today", before=cards[0]["id"]), block("after one", after=cards[1]["id"]), block("orphan", before=new_id())]
    saved = save(host, host.ordubashy, {"expected_revision": 0, "blocks": anchored}, "?include_archived=1&include_deferred=1")
    assert saved.status_code == 200 and saved.json()["data"]["card_ids"] == everything and saved.json()["data"]["blocks"] == anchored
    assert save(host, host.colleague, {"expected_revision": 0, "blocks": [block("Mine")]}).status_code == 200
    assert {table: sql(host, f"SELECT * FROM {table}") for table in tables} == before
    assert task["id"] in json.dumps(before["tasks"])
    # No agent can read or learn it: the Focus board an agent sees is unchanged, and no layout operation exists to register or call.
    assert invoke(host, host.context["ordubashy"], "focus_get", {}) == seen
    assert not [entry.name for entry in host.runtime.application.registry.entries() if "layout" in entry.name]
    assert "Today" not in json.dumps(seen) and "Mine" not in json.dumps(seen)


def test_a_corrupt_or_unsafe_store_is_refused_never_reset_and_concurrent_saves_keep_every_agent(tmp_path):
    runtime, owner, agent = tmp_path, new_id(), new_id()
    path = runtime / "dashboard-preferences" / (hashlib.sha256(owner.encode()).hexdigest() + ".focus-layouts.json")
    saved = preferences.save_layout(runtime, owner, agent, {"expected_revision": 0, "blocks": [block("keep me")]})
    good = path.read_bytes()

    for damaged in (b"", b"{", b'{"version":1,"agents":{}', b'{"version":2,"agents":{}}', b'{"version":1,"agents":[]}',
                    b'{"version":1,"agents":{"%s":{"revision":0,"blocks":[]}}}' % agent.encode(),
                    b'{"version":1,"agents":{"%s":{"revision":1,"blocks":[{"id":"x"}]}}}' % agent.encode(),
                    b'{"version":1,"agents":{},"version":1}', b'{"version":NaN,"agents":{}}'):
        path.write_bytes(damaged)
        path.chmod(0o600)
        for action in (lambda: preferences.read_layout(runtime, owner, agent),
                       lambda: preferences.save_layout(runtime, owner, agent, {"expected_revision": 0, "blocks": []})):
            with pytest.raises(DomainError) as error:
                action()
            assert error.value.code == "operation_failed", damaged
        assert path.read_bytes() == damaged                                              # never reset to an empty layout
    path.write_bytes(good)
    path.chmod(0o644)                                                                    # readable by others: not a private store
    with pytest.raises(DomainError):
        preferences.read_layout(runtime, owner, agent)
    path.unlink()
    target = runtime / "elsewhere.json"
    target.write_bytes(good)
    target.chmod(0o600)
    path.symlink_to(target)
    for action in (lambda: preferences.read_layout(runtime, owner, agent),
                   lambda: preferences.save_layout(runtime, owner, agent, {"expected_revision": 1, "blocks": []})):
        with pytest.raises(DomainError) as error:
            action()
        assert error.value.code == "operation_failed"
    assert target.read_bytes() == good and path.is_symlink()
    path.unlink()
    path.write_bytes(good)
    path.chmod(0o600)
    assert preferences.read_layout(runtime, owner, agent) == saved

    agents = [new_id() for _ in range(8)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda identity: preferences.save_layout(runtime, owner, identity, {"expected_revision": 0, "blocks": [block(identity)]}), agents))
    assert all(preferences.read_layout(runtime, owner, identity)["blocks"][0]["text"] == identity for identity in agents)
    assert preferences.read_layout(runtime, owner, agent) == saved
    assert not [name for name in os.listdir(path.parent) if name.startswith(".")]
