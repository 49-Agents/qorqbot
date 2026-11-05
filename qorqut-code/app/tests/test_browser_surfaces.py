"""Owner viewer surfaces: Global agents' own bindings and each business's one browser, exact and fail-closed.

Synthetic only: a temporary instance with no resident business agents for the viewed businesses, Global agents
registered through the real catalog, one historical business-home record used only for rejection, and a stand-in for
the sandbox owner viewer service. No browser, service, provider or network is used.
"""
import json
import os
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, utc_now
from starlette.testclient import TestClient

from bazarlyk.service import browser_surfaces
from bazarlyk.service.local import LocalFiles
from host_foundation_support import build_instance, create_business_agent, register_sources, snapshot, tree

GUAC = "https://guac.test/#/client/"
ORIGIN = {"Origin": "http://testserver"}
GLOBALS = ("ordubashy", "founder", "global-marketing", "learning-applier")
ORDU, MARKETING = "7f3a1c52-9d4e-4b68-8a2f-5c1e0d9b3a47", "c2e6b8d4-1a53-4f97-b0e8-6d3a9f2c7e15"
FIRST, SECOND = "5e89f3d1-1abe-4e48-bd46-a86da432bc7c", "c2b61a92-8f12-4a7f-85ca-c75db35a1714"
STATE_TABLES = ("browser_turns", "browser_handles", "business_sandboxes", "agent_profiles", "business_memberships")


def document(ordubashy=ORDU, marketing=MARKETING, first=FIRST, **businesses):
    return {"schema_version": 2, "surfaces": {
        "ordubashy-browser": {"label": "Ordubashy", "kind": "browser", "viewer_url": GUAC + "b3JkdQ", "width": 1440, "height": 900},
        "second-browser": {"label": "Marketing", "kind": "browser", "viewer_url": GUAC + "bWFya2V0aW5n", "width": 1440, "height": 900},
        "outlook-browser": {"label": "Outlook", "kind": "browser", "viewer_url": GUAC + "b3V0bG9vaw"},
        "ordubashy-desktop": {"label": "Desktop", "kind": "desktop", "viewer_url": GUAC + "ZGVza3RvcA"},
        "first-browser": {"label": "First", "kind": "browser", "viewer_url": GUAC + "Zmlyc3Q", "width": 1280, "height": 800},
        "second-browser": {"label": "Second", "kind": "browser", "viewer_url": GUAC + "c2Vjb25k"},
    }, "agents": {
        ordubashy: {"browsers": ["ordubashy-browser"], "desktop": "ordubashy-desktop"},
        marketing: {"browsers": ["second-browser", "outlook-browser"]},
    }, "businesses": {first: {"browser": "first-browser"}, **{key: {"browser": value} for key, value in businesses.items()}}}


# -- the strict schema-2 reader and the side-effect-free projections ------------------------------------------------------

def test_schema_two_keeps_global_own_bindings_and_one_browser_per_business():
    parsed = browser_surfaces.parse(document(**{SECOND: "second-browser"}))
    assert parsed["businesses"] == {FIRST: {"browser": "first-browser"}, SECOND: {"browser": "second-browser"}}
    assert parsed["agents"][MARKETING] == {"browsers": ["second-browser", "outlook-browser"], "desktop": None}
    first = browser_surfaces.business_surfaces(parsed, FIRST)
    assert first == {"business_id": FIRST, "configured": True, "desktop": None, "browsers": [
        {"id": "first-browser", "label": "First", "kind": "browser", "viewer_url": GUAC + "Zmlyc3Q", "width": 1280, "height": 800}]}
    other = "0b9d2c4e-7a51-4f0e-9d3c-2f1e8a6b5c4d"
    assert browser_surfaces.business_surfaces(parsed, other) == {"business_id": other, "configured": False, "browsers": [], "desktop": None}
    assert browser_surfaces.business_surfaces(None, FIRST)["configured"] is False
    # A Global agent's own view never contains a business browser.
    own = browser_surfaces.agent_surfaces(parsed, ORDU, desktop_allowed=True)
    assert [item["id"] for item in own["browsers"]] == ["ordubashy-browser"] and own["desktop"]["id"] == "ordubashy-desktop"


def test_a_sandboxed_business_projects_only_its_sandbox_viewer():
    parsed = browser_surfaces.parse(document(**{SECOND: "second-browser"}))
    ready = browser_surfaces.business_surfaces(parsed, SECOND, sandbox={
        "configured": True, "label": "Second", "viewer_url": "http://10.0.0.5:7300/" + SECOND + "/"})
    assert ready["browsers"] == [{"id": browser_surfaces.SANDBOX_SURFACE_ID, "label": "Second", "kind": "browser",
                                  "viewer_url": "http://10.0.0.5:7300/" + SECOND + "/"}]
    for sandbox in ({"configured": True, "label": "Second", "viewer_url": None},
                    {"configured": False, "label": "Second", "viewer_url": None}):
        # Never the registered host surface instead.
        assert browser_surfaces.business_surfaces(parsed, SECOND, sandbox=sandbox) == {
            "business_id": SECOND, "configured": sandbox["configured"], "browsers": [], "desktop": None}


@pytest.mark.parametrize("profile, expected", [
    ({"role": {"id": role}, "scope": {"kind": "ecosystem"}}, True) for role in GLOBALS] + [
    ({"role": {"id": "business-agent"}, "scope": {"kind": "business"}}, False),
    ({"role": {"id": "dev"}, "scope": {"kind": "business"}}, False),
    ({"role": {"id": "business-agent"}, "scope": {"kind": "ecosystem"}}, False),
    ({"role": {"id": "ordubashy"}, "scope": {"kind": "business"}}, False),
    ({"role": {"id": "marketing"}, "scope": {"kind": "ecosystem"}}, False),
    ({}, False),
])
def test_only_the_four_global_roles_with_ecosystem_scope_have_own_viewers(profile, expected):
    assert browser_surfaces.global_profile(json.dumps(profile)) is expected


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(schema_version=1),
    lambda r: r.pop("businesses"),
    lambda r: r.update(extra=True),
    lambda r: r["businesses"].update({FIRST.upper(): {"browser": "second-browser"}}),
    lambda r: r["businesses"].update({"not-a-uuid": {"browser": "second-browser"}}),
    lambda r: r["businesses"][FIRST].update(desktop="ordubashy-desktop"),
    lambda r: r["businesses"].update({FIRST: {"browsers": ["first-browser"]}}),
    lambda r: r["businesses"].update({FIRST: {"browser": ["first-browser"]}}),
    lambda r: r["businesses"].update({FIRST: {"browser": "ordubashy-desktop"}}),
    lambda r: r["businesses"].update({FIRST: {"browser": "missing-browser"}}),
    lambda r: r["businesses"].update({SECOND: {"browser": "first-browser"}}),
    lambda r: r["agents"][MARKETING]["browsers"].append("first-browser"),
    lambda r: r["businesses"].update({SECOND: {"browser": "ordubashy-browser"}}),
    lambda r: r.update(businesses=[]),
    lambda r: r["surfaces"]["second-browser"].update(viewer_url="javascript:alert(1)"),
    lambda r: r["surfaces"]["first-browser"].update(viewer_url="https://user:pw@guac.test/"),
    lambda r: r["surfaces"]["first-browser"].update(viewer_url="/relative"),
    lambda r: r["surfaces"]["first-browser"].update(kind="vnc"),
    lambda r: r["surfaces"]["first-browser"].update(cdp="http://127.0.0.1:9230"),
    lambda r: r["surfaces"]["first-browser"].pop("height"),
    lambda r: r["surfaces"]["first-browser"].update(width=0),
    lambda r: r["surfaces"]["first-browser"].update(label="x\ny"),
    lambda r: r["surfaces"].update({"Bad_ID": {"label": "x", "kind": "browser", "viewer_url": GUAC}}),
    lambda r: r["agents"].update({"not-a-uuid": {"browsers": []}}),
    lambda r: r["agents"][MARKETING].update(browsers=["missing-browser"]),
    lambda r: r["agents"][MARKETING].update(browsers=["ordubashy-desktop"]),
    lambda r: r["agents"][MARKETING].update(browsers=["outlook-browser", "outlook-browser"]),
    lambda r: r["agents"][MARKETING].update(desktop="second-browser"),
    lambda r: r["agents"][MARKETING].update(password="secret"),
])
def test_invalid_mismatched_duplicate_or_older_registries_are_rejected_whole(mutate):
    value = document()
    mutate(value)
    with pytest.raises(DomainError) as error:
        browser_surfaces.parse(value)
    assert error.value.code == "invalid_configuration" and "guac.test" not in error.value.message


def test_the_version_one_shape_names_the_explicit_migration():
    with pytest.raises(DomainError, match="explicit migration"):
        browser_surfaces.parse({"schema_version": 1, "surfaces": {}, "agents": {}})


# -- the installation owner's HTTP reads --------------------------------------------------------------------------------

class SandboxViewers:
    """Stand-in for the sandbox owner viewer service: configuration and ``link`` only, so a start, reconcile or lease
    attempt during a read fails loudly."""

    def __init__(self, port=7300, url=None):
        self.port, self.url, self.linked = port, url, []

    def config_loader(self, paths):
        return SimpleNamespace(viewer_port=self.port)

    def link(self, business_id):
        self.linked.append(business_id)
        return self.url


def register_global(instance, owner, role, businesses):
    """Publish one Global role through the real catalog and register its ecosystem agent."""
    from bazarlyk.bootstrap.seed import apply_initial_plan
    runtime = instance.runtime
    with instance.database.read() as uow:
        plan = runtime.publisher.prepare(owner, {"source_id": role}, InvocationOptions(idempotency_key="seed-" + role), uow, "role")
    pin = apply_initial_plan(instance.database, owner, plan, runtime.publisher, runtime.publisher.resolve_file_target,
                             runtime.locks)["artifact"]
    created = instance.invoke(owner, "agents_create", {
        "role": pin, "plugins": [], "name": role + "-agent", "display_name": role.title(), "assignment_brief": "Synthetic fixture agent.",
        "scope": {"kind": "ecosystem", "allowed_business_ids": sorted(businesses) if role == "ordubashy" else [],
                  "global_capabilities": ["read_global_context"]}})
    assert created.status == "ok", created.error
    return created.data["agent_id"]


@pytest.fixture
def host(tmp_path):
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES
    from bazarlyk.service.web import create_app
    from bazarlyk.transport.auth import Authentication
    from bazarlyk.transport.compatibility import DashboardCompatibility

    instance, ids = build_instance(tmp_path, businesses=("first", "second", "third"))
    for slug, identity in ids.items():
        register_sources(instance, identity, slug)
    owner = InvocationContext(Principal(instance.owner, "human"), allowed_business_ids=frozenset(ids.values()),
                              global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
    agents = {role: register_global(instance, owner, role, ids.values()) for role in GLOBALS}
    # A historical business-home registration, present only to prove it is never served.
    retired = create_business_agent(instance, ids["first"])
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (agents["ordubashy"],))
    runtime = instance.runtime
    runtime.previews = None
    authentication = Authentication(instance.database, runtime.paths)
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    app = create_app(runtime, dashboard_root=dashboard, origins=["http://testserver"], authentication=authentication,
                     compatibility=DashboardCompatibility(runtime), manage_lifecycle=False)
    with TestClient(app, base_url="http://testserver") as client:
        client.cookies.set("bazarlyk_session", authentication.issue_browser())
        yield SimpleNamespace(instance=instance, runtime=runtime, client=client, ids=ids, agents=agents,
                              retired=retired, local=LocalFiles(runtime))


def write(host, value, mode=0o600):
    path = browser_surfaces.surfaces_path(host.runtime.paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    os.chmod(path, mode)
    return path


def registry(host, **extra):
    a, ids = host.agents, host.ids
    value = document(ordubashy=a["ordubashy"], marketing=a["global-marketing"], first=ids["second"], **extra)
    value["agents"][host.retired] = {"browsers": ["outlook-browser"]}
    return value


def business(host, slug):
    return host.client.get("/api/biz/" + slug + "/browser-surfaces")


def agent(host, identity):
    return host.client.get("/api/root/agents/" + identity + "/browser-surfaces")


def state(host):
    with host.instance.database.read() as uow:
        counts = snapshot(uow.connection, STATE_TABLES)
    return counts, tree(host.runtime.paths.runtime)


def test_a_business_without_any_resident_agent_shows_its_one_configured_viewer(host):
    ids = host.ids
    assert business(host, "second").json() == {"business_id": ids["second"], "configured": False, "browsers": [], "desktop": None}
    path = write(host, registry(host))
    before, content = state(host), path.read_bytes()
    with host.instance.database.read() as uow:
        assert not uow.connection.execute("SELECT 1 FROM agent_profiles WHERE home_business_id=?", (ids["second"],)).fetchone()
    result = business(host, "second")
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {"business_id": ids["second"], "configured": True, "desktop": None, "browsers": [
        {"id": "first-browser", "label": "First", "kind": "browser", "viewer_url": GUAC + "Zmlyc3Q", "width": 1280, "height": 800}]}
    # Exactly that business's record: an unbound business and the one with a historical agent show nothing.
    for slug in ("first", "third"):
        assert business(host, slug).json() == {"business_id": ids[slug], "configured": False, "browsers": [], "desktop": None}
    # Reads start, lease, provision and write nothing.
    assert state(host) == before and path.read_bytes() == content


def test_each_global_keeps_its_own_viewers_and_retired_business_agents_are_not_found(host):
    a = host.agents
    write(host, registry(host))
    ordu = agent(host, a["ordubashy"])
    assert ordu.status_code == 200 and ordu.headers["cache-control"] == "no-store"
    assert [b["id"] for b in ordu.json()["browsers"]] == ["ordubashy-browser"] and ordu.json()["desktop"]["id"] == "ordubashy-desktop"
    marketing = agent(host, a["global-marketing"]).json()
    assert [b["id"] for b in marketing["browsers"]] == ["second-browser", "outlook-browser"] and marketing["desktop"] is None
    for role in ("founder", "learning-applier"):
        assert agent(host, a[role]).json() == {"agent_id": a[role], "configured": True, "browsers": [], "desktop": None}
    assert "Zmlyc3Q" not in json.dumps([ordu.json(), marketing]), "a business browser is never an agent's own"
    # The historical business agent stays unreachable even with a stored binding, as do unknown and inactive agents.
    for identity in (host.retired, "00000000-0000-4000-8000-000000000000"):
        assert agent(host, identity).status_code == 404
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (a["founder"],))
    assert agent(host, a["founder"]).status_code == 404
    # The explicit Ordubashy Desktop fallback remains, and only for the configured coordinator.
    value = registry(host)
    value["agents"][a["ordubashy"]].pop("desktop")
    write(host, value)
    assert host.client.post("/api/root/env", headers=ORIGIN, json={"pairs": [
        {"key": "DESKTOP_CONTROL_URL", "value": "https://guac.test/#/client/eGZjZQ"}]}).status_code == 200
    assert agent(host, a["ordubashy"]).json()["desktop"]["id"] == "desktop-control-url"
    assert agent(host, a["global-marketing"]).json()["desktop"] is None


@pytest.mark.parametrize("mutate", [
    lambda h, r: r.update(schema_version=1),
    lambda h, r: r.pop("businesses"),
    lambda h, r: r["businesses"].update({h.ids["third"]: {"browser": "first-browser"}}),
    lambda h, r: r["agents"][h.agents["global-marketing"]]["browsers"].append("first-browser"),
])
def test_an_invalid_registry_fails_closed_for_business_and_agent_reads(host, mutate):
    value = registry(host)
    mutate(host, value)
    write(host, value)
    for result in (business(host, "second"), agent(host, host.agents["global-marketing"])):
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == "invalid_configuration" and "guac.test" not in result.text
    write(host, registry(host), mode=0o644)
    assert business(host, "second").status_code == 422


def test_a_sandboxed_business_never_falls_back_to_a_host_surface(host):
    ids, runtime = host.ids, host.runtime
    write(host, registry(host, **{host.ids["third"]: "second-browser"}))
    now = utc_now()
    with host.instance.database.transaction() as uow:
        uow.connection.execute("INSERT INTO business_sandboxes(business_id, desired_generation, desired_state, state, created_at, "
                               "updated_at) VALUES (?, 1, 'running', 'pending', ?, ?)", (ids["third"], now, now))
    empty = {"business_id": ids["third"], "configured": False, "browsers": [], "desktop": None}
    # No viewer service, or one without a viewer port: not configured, and never the registered host browser.
    assert business(host, "third").json() == empty
    viewers = SandboxViewers(port=None)
    runtime.previews = SimpleNamespace(browser_viewers=viewers)
    assert business(host, "third").json() == empty and viewers.linked == []
    # Configured but not ready: no browser, and the viewer service is not even asked.
    viewers = SandboxViewers()
    runtime.previews = SimpleNamespace(browser_viewers=viewers)
    assert business(host, "third").json() == {**empty, "configured": True} and viewers.linked == []
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE business_sandboxes SET state='ready', observed_generation=1, environment_id='env-1' "
                               "WHERE business_id=?", (ids["third"],))
    # Ready but its viewer route is unavailable: still no fallback.
    assert business(host, "third").json() == {**empty, "configured": True} and viewers.linked == [ids["third"]]
    viewers.url = "http://10.20.0.1:7300/" + ids["third"] + "/"
    before = state(host)
    result = business(host, "third")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json() == {"business_id": ids["third"], "configured": True, "desktop": None, "browsers": [
        {"id": "business-sandbox-browser", "label": "Third", "kind": "browser", "viewer_url": viewers.url}]}
    assert state(host) == before
    # The sandbox viewer does not depend on the registry; a host business still reads its own record.
    assert business(host, "second").json()["browsers"][0]["id"] == "first-browser"


def test_unknown_archived_unauthorized_and_non_owner_reads_are_refused(host):
    ids, local = host.ids, host.local
    write(host, registry(host))
    assert business(host, "no-such-business").status_code == 404
    assert host.client.post("/api/biz/second/browser-surfaces", headers=ORIGIN, json={}).status_code in {404, 405}
    # The owner identity is the existing dashboard boundary: another host is refused.
    assert host.client.get("/api/biz/second/browser-surfaces", headers={"Host": "other.invalid"}).status_code == 403
    # Other principals, including a registered agent, are refused before anything is read.
    other = new_id()
    with host.instance.database.transaction() as uow:
        uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "Other"})
        uow.repository("business_memberships", business_id=ids["second"]).insert({"principal_id": other, "is_owner": True})
    for context in (InvocationContext(Principal(other, "human"), business_id=ids["second"],
                                      allowed_business_ids=frozenset({ids["second"]}), source="dashboard"),
                    host.runtime.resolver.invocation_context(Principal(host.agents["ordubashy"], "agent"), business_id=None)):
        with pytest.raises(DomainError) as error:
            local.business_browser_surfaces(context)
        assert error.value.code == "forbidden"
    # The owner without current membership, and an archived business, are unavailable.
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (ids["third"], host.instance.owner))
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), ids["second"]))
    for slug in ("second", "third"):
        assert business(host, slug).status_code == 404
    owner = host.instance.human(ids["second"])
    with pytest.raises(DomainError) as error:
        local.business_browser_surfaces(owner)
    assert error.value.code in {"business_archived", "not_found"}
