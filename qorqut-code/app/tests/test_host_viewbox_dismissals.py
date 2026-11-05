"""Acceptance cases: the owner's exact-placement dismissals of Viewbox queue items.

A real synthetic host and HTTP application over temporary paths. Dismissal is a durable owner preference under
``runtime/dashboard-preferences``: it names one placement, never edits the board, its source or the viewed
acknowledgements, and a link placed again is a new placement that reappears. No model, provider, network or operational data."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain
from starlette.testclient import TestClient

from bazarlyk import viewbox_preferences
from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.attention_queue import queue, viewed
from bazarlyk.background.integration import attach_background
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from bazarlyk.viewbox_preferences import dismiss, dismissed
from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources

ORIGIN = {"Origin": "http://testserver"}


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


def place(host, who, name, title="Draft"):
    result = host.runtime.application.invoke(host.context[who], "viewbox_place", {"link": f"https://example.com/{name}", "title": title},
                                             InvocationOptions(idempotency_key=new_id()))
    assert result.status == "ok", result.error
    return plain(result.data)


def board(host, agent, client=None, **headers):
    return (client or host.client).get(f"/api/root/agents/{agent}/viewbox", headers={**ORIGIN, **headers})


def attention(host, agent):
    return {item["title"]: item["attention_id"] for item in host.runtime.domains.viewbox.owner_board(host.owner, agent)["items"]}


def post(host, agent, body, client=None, **headers):
    return (client or host.client).post(f"/api/root/agents/{agent}/viewbox/dismiss", headers={**ORIGIN, **headers}, json=body)


def sql(host, statement):
    with host.runtime.database.read() as uow:
        return [tuple(row) for row in uow.connection.execute(statement)]


def preference_files(host):
    directory = host.runtime.paths.runtime / "dashboard-preferences"
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def test_dismissal_is_exact_durable_and_changes_the_conditional_read(host):
    place(host, "ordubashy", "one", "One")
    place(host, "ordubashy", "two", "Two")
    ids = attention(host, host.ordubashy)
    first = board(host, host.ordubashy)
    assert first.status_code == 200 and first.json()["data"]["dismissed_attention_ids"] == []
    assert preference_files(host) == []                                    # reading creates no preference content
    assert board(host, host.ordubashy, **{"If-None-Match": first.headers["etag"]}).status_code == 304
    before = sql(host, "SELECT * FROM agent_viewbox_items") + sql(host, "SELECT * FROM agent_viewbox_boards")

    saved = post(host, host.ordubashy, {"attention_id": ids["One"]})
    assert saved.status_code == 200 and saved.headers["cache-control"] == "private, no-store"
    assert saved.json()["status"] == "ok"
    assert saved.json()["data"] == {"agent_id": host.ordubashy, "dismissed_attention_ids": [ids["One"]]}

    # The conditional read changes with the preference, while the full board stays truthful.
    after = board(host, host.ordubashy, **{"If-None-Match": first.headers["etag"]})
    assert after.status_code == 200 and after.headers["etag"] != first.headers["etag"]
    data = after.json()["data"]
    assert data["dismissed_attention_ids"] == [ids["One"]] and [item["title"] for item in data["items"]] == ["Two", "One"]
    assert board(host, host.ordubashy, **{"If-None-Match": after.headers["etag"]}).status_code == 304

    # Another browser and a restarted service see the same durable preference; another agent is unaffected.
    other = serve(host.runtime, host.dashboard)
    with other:
        assert board(host, host.ordubashy, other).json()["data"]["dismissed_attention_ids"] == [ids["One"]]
        assert board(host, host.colleague, other).json()["data"]["dismissed_attention_ids"] == []
    assert dismissed(host.runtime.paths.runtime, host.instance.owner, host.ordubashy) == [ids["One"]]
    assert dismissed(host.runtime.paths.runtime, new_id(), host.ordubashy) == []

    # Source rows are untouched, and viewing and dismissing are independent preferences.
    assert before == sql(host, "SELECT * FROM agent_viewbox_items") + sql(host, "SELECT * FROM agent_viewbox_boards")
    assert [item["title"] for item in queue(host.runtime, host.owner)["items"] if item["kind"] == "viewbox"] == ["Two", "One"]
    queue(host.runtime, host.owner, {"viewed": [ids["Two"]]})
    assert viewed(host.runtime.paths.runtime, host.instance.owner) == {ids["Two"]}
    assert board(host, host.ordubashy).json()["data"]["dismissed_attention_ids"] == [ids["One"]]


def test_replay_is_idempotent_and_a_replaced_placement_reappears(host):
    place(host, "ordubashy", "one", "One")
    old = attention(host, host.ordubashy)["One"]
    assert post(host, host.ordubashy, {"attention_id": old}).status_code == 200
    path = viewbox_preferences._path(host.runtime.paths.runtime, host.instance.owner)
    content = path.read_bytes()
    replay = post(host, host.ordubashy, {"attention_id": old})
    assert replay.status_code == 200 and replay.json()["data"]["dismissed_attention_ids"] == [old] and path.read_bytes() == content

    place(host, "ordubashy", "one", "One again")                            # the same link: same item, new placement
    new = attention(host, host.ordubashy)["One again"]
    assert new != old and new.split(":")[1] == old.split(":")[1]
    listed = board(host, host.ordubashy).json()["data"]
    assert listed["dismissed_attention_ids"] == [old] and listed["items"][0]["attention_id"] == new
    # A late retry of the saved old placement succeeds without hiding the newer one; the newer is dismissed separately.
    assert post(host, host.ordubashy, {"attention_id": old}).json()["data"]["dismissed_attention_ids"] == [old]
    assert post(host, host.ordubashy, {"attention_id": new}).json()["data"]["dismissed_attention_ids"] == sorted([old, new])

    # A stale placement that was never dismissed cannot be saved afterwards, and does not hide its successor.
    place(host, "ordubashy", "two", "Two")
    stale = attention(host, host.ordubashy)["Two"]
    place(host, "ordubashy", "two", "Two again")
    assert post(host, host.ordubashy, {"attention_id": stale}).status_code == 409
    assert stale not in dismissed(host.runtime.paths.runtime, host.instance.owner, host.ordubashy)
    assert attention(host, host.ordubashy)["Two again"] not in dismissed(host.runtime.paths.runtime, host.instance.owner, host.ordubashy)


def test_unknown_foreign_and_malformed_requests_are_refused_without_saving(host):
    place(host, "ordubashy", "mine", "Mine")
    place(host, "colleague", "theirs", "Theirs")
    mine, theirs = attention(host, host.ordubashy)["Mine"], attention(host, host.colleague)["Theirs"]
    path = f"/api/root/agents/{host.ordubashy}/viewbox/dismiss"
    for body in ({"attention_id": theirs}, {"attention_id": f"viewbox:{new_id()}:1"}, {"attention_id": "viewbox:nope:9"}):
        assert post(host, host.ordubashy, body).status_code == 409
    for body in ({}, {"attention_id": mine, "extra": 1}, {"attention_id": ""}, {"attention_id": "x" * 201}, {"attention_id": 7},
                 {"attention_id": None}, {"attention_id": [mine]}, {"id": mine}):
        refused = post(host, host.ordubashy, body)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "validation_error", body
    assert host.client.post(path, headers=ORIGIN, json=[mine]).status_code == 400
    assert host.client.post(path, headers=ORIGIN, content=b"{", ).status_code == 400
    assert host.client.post(path, headers={**ORIGIN, "Content-Type": "text/plain"}, content=mine).status_code == 400
    assert post(host, new_id(), {"attention_id": mine}).status_code == 404
    assert post(host, host.instance.owner, {"attention_id": mine}).status_code == 404
    assert post(host, "not-an-id", {"attention_id": mine}).status_code == 404
    assert host.client.get(path, headers=ORIGIN).status_code == 404
    assert preference_files(host) == []


def test_only_the_origin_checked_tokenless_owner_dashboard_can_dismiss(host):
    place(host, "ordubashy", "mine", "Mine")
    mine = attention(host, host.ordubashy)["Mine"]
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer "}, {"Authorization": "Bearer " + "x" * 40}, {"Authorization": "Basic abc"}):
        refused = post(host, host.ordubashy, {"attention_id": mine}, **headers)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "forbidden", headers
        assert "example.com" not in refused.text and len(refused.content) < 400
    assert host.client.post(f"/api/root/agents/{host.ordubashy}/viewbox/dismiss", headers={"Origin": "http://evil.example"},
                            json={"attention_id": mine}).status_code == 403
    assert post(host, host.ordubashy, {"attention_id": mine}, **{"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"}).status_code == 403
    assert preference_files(host) == []
    # The domain layer refuses every principal but the installation owner, an agent included.
    for ctx in (host.context["ordubashy"], host.context["colleague"], InvocationContext(Principal(new_id(), "human"), source="dashboard")):
        with pytest.raises(DomainError) as error:
            dismiss(host.runtime.domains.viewbox, host.runtime.paths.runtime, ctx, host.ordubashy, {"attention_id": mine})
        assert error.value.code == "forbidden"
    assert preference_files(host) == []
    assert post(host, host.ordubashy, {"attention_id": mine}).status_code == 200


def test_concurrent_dismissals_across_agents_do_not_clobber_each_other(host):
    for name in range(6):
        place(host, "ordubashy", f"a{name}", f"A{name}")
        place(host, "colleague", f"m{name}", f"M{name}")
    work = [(agent, item["attention_id"]) for agent in (host.ordubashy, host.colleague)
            for item in host.runtime.domains.viewbox.owner_board(host.owner, agent)["items"]]
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda item: dismiss(host.runtime.domains.viewbox, host.runtime.paths.runtime, host.owner, item[0],
                                                     {"attention_id": item[1]}), work + work[:3]))
    assert len(results) == 15
    for agent in (host.ordubashy, host.colleague):
        assert dismissed(host.runtime.paths.runtime, host.instance.owner, agent) == sorted(i for a, i in work if a == agent)
    assert json.loads(viewbox_preferences._path(host.runtime.paths.runtime, host.instance.owner).read_text()).keys() == {host.ordubashy, host.colleague}
    assert not [name for name in preference_files(host) if name.startswith(".")]


def test_oldest_placements_off_the_board_are_forgotten_beyond_the_per_agent_bound(host, monkeypatch):
    monkeypatch.setattr(viewbox_preferences, "MAX_PER_AGENT", 2)
    place(host, "ordubashy", "one", "One")
    old = attention(host, host.ordubashy)["One"]
    assert post(host, host.ordubashy, {"attention_id": old}).status_code == 200
    place(host, "ordubashy", "one", "One again")
    place(host, "ordubashy", "two", "Two")
    ids = attention(host, host.ordubashy)
    assert post(host, host.ordubashy, {"attention_id": ids["One again"]}).status_code == 200
    assert post(host, host.ordubashy, {"attention_id": ids["Two"]}).json()["data"]["dismissed_attention_ids"] == sorted([ids["One again"], ids["Two"]])
    assert post(host, host.ordubashy, {"attention_id": old}).status_code == 409   # forgotten; its placement was already replaced
