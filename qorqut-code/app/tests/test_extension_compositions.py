"""The four extension compositions of one host (neither, Paper only, content only, both) on isolated data.

Each composition is a real synthetic host (``build_instance`` over a disposable instance) whose store, catalog, runtime and
HTTP surface are composed for exactly the selected extensions. A package that is not installed skips its compositions, so a
core-only checkout still proves the "neither" case. Nothing leaves the process: Paper's worker is never started and
no key, credential, mail account or network is touched; the content adapter is configured off."""

import copy
import dataclasses
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.specs import TableSpec
from starlette.routing import Route
from starlette.testclient import TestClient

from bazarlyk.composition.assembly import require_selected_storage
from bazarlyk.extensions import NO_EXTENSIONS, Extension, FrameRule, SourceHandler, WebContribution, select
from bazarlyk.service.runtime import build_runtime
from bazarlyk.service.web import create_app
from host_foundation_support import build_instance, owner_global_context

TOKEN = "t" * 40
ORIGIN = {"Origin": "http://testserver"}
OWNER = {"Authorization": "Bearer " + TOKEN, **ORIGIN}
COMPOSITIONS = {"neither": (), "paper": ("paper",), "content": ("content",), "both": ("paper", "content")}
# What each package adds, so every case can assert presence and absence from the same table.
PAPER_TABLES = {"outreach_events", "outreach_handoffs", "outreach_sends", "outreach_assertions", "outreach_agent_credentials",
                "outreach_business_registrations", "outreach_business_routes", "outreach_route_settings", "integration_principals",
                "integration_businesses"}
CONTENT_TABLES = {"content_session_invocations"}


def extension(name, tmp_path):
    """A ready extension object, Paper with a synthetic key (host configured) and content configured off."""
    if name == "paper":
        paper = pytest.importorskip("qorqut_paper.extension")
        key = tmp_path / "paper-key.pem"
        key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key.chmod(0o600)
        return paper.create({"host": {"base_url": "https://outreach.example.test", "audience": "outreach", "subject": "owner@example.test",
                                      "private_key_file": str(key), "dashboard_origin": "https://dashboard.example.test"},
                             "token_root": str(tmp_path / "paper-tokens")})
    content = pytest.importorskip("qorqut_content.extension")
    return content.create({})


@pytest.fixture(scope="module", params=sorted(COMPOSITIONS))
def composition(request, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp(request.param)
    selection = select(instances=[extension(name, tmp_path) for name in COMPOSITIONS[request.param]])
    instance, _ = build_instance(tmp_path, extensions=selection)
    instance.runtime.freeze_registry()
    return request.param, instance, selection


def selected(name, case):
    return name in COMPOSITIONS[case]


def tables(instance):
    with instance.database.read() as uow:
        return {row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_the_store_carries_exactly_the_selected_extensions_tables(composition):
    case, instance, selection = composition
    present = tables(instance)
    assert selection.ids == tuple(sorted(COMPOSITIONS[case]))
    assert PAPER_TABLES <= present if selected("paper", case) else not (PAPER_TABLES & present)
    assert CONTENT_TABLES <= present if selected("content", case) else not (CONTENT_TABLES & present)
    assert {"businesses", "tasks", "inbox_events"} <= present                           # the core store is whole either way


def test_owner_can_discover_workspace_types_without_selecting_a_project(composition):
    case, instance, selection = composition
    context = owner_global_context(instance)
    result = instance.runtime.application.invoke(context, "workspace_types", {})
    assert result.status == "ok", result.error
    names = {item["key"] for item in result.data["types"]}
    assert "focus" in names
    assert "threads" not in names
    assert ("campaign_review" in names) is selected("paper", case)
    # Catalog discovery does not authorize reading any project's presentation.
    denied = instance.runtime.application.invoke(context, "workspace_get", {"workspace": "focus"})
    assert denied.status == "error" and denied.error["code"] == "forbidden"


def test_declarations_schema_and_handlers_are_present_only_when_selected(composition):
    case, instance, selection = composition
    runtime, registry = instance.runtime, instance.runtime.registry
    for operation, owner in (("outreach_handoff_repair", "paper"), ("content_session_start", "content"),
                             ("content_session_message_add", "content")):
        assert registry.known(operation) is selected(owner, case), operation
    assert registry.known("tasks_get")
    assert ("campaign_review" in {value.key for value in selection.workspace_types()}) is selected("paper", case)
    assert ("outreach" in {kind for contribution in selection.item_kinds() for kind in contribution.kinds}) is selected("paper", case)
    assert (selection.sources.get("outreach_events") is not None) is selected("paper", case)
    assert (selection.sources.get("content_sessions") is not None) is selected("content", case)
    assert not hasattr(selection, "mailbox_purposes")                                   # the mail-domain hook is retired from core
    assert ("outreach" in runtime.resolver.native_connections) is selected("paper", case)
    assert ("content-creation" in runtime.resolver.plugin_handlers) is selected("content", case)
    assert ("content_conflict" in selection.error_statuses) is selected("content", case)
    assert (selection.integration_adapter is not None) is selected("paper", case)
    assert bool(selection.assets) is selected("paper", case)                            # only Paper ships pane assets


def test_the_lifecycle_and_recovery_inventory_follow_the_selection(composition):
    case, instance, selection = composition
    runtime = instance.runtime
    lifetime = runtime.lifetime()
    names = [step.name for step in lifetime.steps]
    # Paper's worker joins only with its host configured (this fixture does) and starts ahead of the change feed; content's
    # adapter (configured off) is still a verified-inactive step ahead of Telegram.
    assert ("outreach_registrations" in names) is selected("paper", case)
    assert ("content_sessions" in names) is selected("content", case)
    if selected("paper", case):
        assert names.index("outreach_registrations") == names.index("changes") - 1
        assert lifetime.stop_order.index("outreach_registrations") == lifetime.stop_order.index("previews") - 1
    if selected("content", case):
        assert names.index("content_sessions") == names.index("telegram") - 1
        assert lifetime.stop_order.index("content_sessions") == lifetime.stop_order.index("context") - 1
    expected = tuple(name for name, owner in (("content_sessions", "content"), ("outreach_registrations", "paper"))
                     if selected(owner, case))
    assert set(runtime.extension_writers()) == set(expected)
    assert set(expected) <= set(runtime.recovery_quiescer.required)
    writers = runtime.sync_recovery_writers().writers
    for name in expected:
        # A selected service has a real controller whose drain is judged by typed evidence; nothing is running here.
        assert writers[name].drain(timeout=2).state == "drained"
    assert not {"outreach_registrations", "content_sessions"} - set(expected) & set(writers)


def test_http_routes_exist_only_for_selected_extensions_and_keep_their_authentication(composition, tmp_path):
    case, instance, selection = composition
    dashboard = tmp_path / "dashboard"
    for folder in ("js", "css"):
        (dashboard / folder).mkdir(parents=True)
    (dashboard / "index.html").write_text("<html></html>")
    app = create_app(instance.runtime, origins=["http://testserver"], bootstrap_token=TOKEN, dashboard_root=dashboard,
                     manage_lifecycle=False)
    with TestClient(app) as client:
        listed = client.get("/api/root/extensions", headers=OWNER)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["data"]["extensions"]] == (["paper"] if selected("paper", case) else [])
        # Integration intake: ordinary not-found when absent, the integration bearer requirement when present.
        intake = client.post("/api/integrations/outreach/events", json={})
        assert intake.status_code == (401 if selected("paper", case) else 404)
        if selected("paper", case):
            assert intake.json()["error"]["code"] == "unauthenticated"
            forged = client.post("/api/integrations/outreach/events", json={}, headers={"Authorization": "Bearer " + "x" * 40})
            assert forged.status_code == 401
        owner = client.post("/api/root/integrations/outreach", json={"action": "route_coverage"}, headers=OWNER)
        if selected("paper", case):
            assert owner.status_code == 200 and owner.json()["status"] == "ok"
        else:
            assert (owner.status_code, owner.json()["error"]["code"]) == (404, "not_found")
        # Owner mutations still need the same-origin proof, with or without the extension.
        assert client.post("/api/root/integrations/outreach", json={"action": "revoke"},
                           headers={"Authorization": "Bearer " + TOKEN}).status_code == 403
        # The recorded-content canvas route exists only with content (configured off here: unavailable, never an open door).
        canvas = client.get("/content/sessions/example/index.html")
        assert canvas.status_code == (503 if selected("content", case) else 404)
        if selected("content", case):
            assert canvas.json()["code"] == "content_sessions_unavailable"
        # Declared assets are served beneath their extension's path (and listed in the manifest); nothing else is.
        script = client.get("/extensions/paper/assets/campaign-review.js")
        if selected("paper", case):
            from qorqut_paper.extension import ASSETS
            assert script.status_code == (200 if (ASSETS / "campaign-review.js").is_file() else 404)
            assert listed.json()["data"]["extensions"][0]["assets"]["scripts"] == ["/extensions/paper/assets/campaign-review.js"]
            assert client.get("/extensions/paper/assets/../extension.py").status_code == 404
            assert client.get("/extensions/paper/assets/undeclared.js").status_code == 404
        else:
            assert script.status_code == 404
        assert client.get("/extensions/missing/assets/x.js").status_code == 404


def test_a_store_prepared_for_one_selection_refuses_another(composition, tmp_path):
    """The store is fingerprinted with its selected storage: serving it with a different set is refused before any service."""
    case, instance, selection = composition
    other = NO_EXTENSIONS if COMPOSITIONS[case] else select(instances=[extension("content", tmp_path)])
    with pytest.raises(DomainError):
        build_runtime(instance.workspace, paths=instance.paths, database=instance.database, extensions=other)


ALTERED_STORAGE = {
    "ddl": lambda item: dataclasses.replace(item, schema=(*item.schema, "CREATE INDEX content_altered ON content_session_invocations(state)")),
    "tables": lambda item: dataclasses.replace(
        item, tables={"content_session_invocations": TableSpec(("principal_id",), revision_column=None)},
        immutable_fields={"content_session_invocations": frozenset({"session_id"})}),
    "required": lambda item: dataclasses.replace(item, required_tables=frozenset({"principals"})),
}


@pytest.mark.parametrize("change", sorted(ALTERED_STORAGE))
def test_the_same_owner_with_different_storage_is_not_the_selected_storage(tmp_path, change):
    """Owner names alone do not make a store compatible: the selected package's ordered DDL, table specifications, immutable
    columns and required-table metadata must be the ones the opened store carries."""
    real = extension("content", tmp_path)
    altered = copy.copy(real)
    altered.storage = lambda: tuple(ALTERED_STORAGE[change](item) for item in real.storage())
    prepared = select(instances=[altered])
    instance, _ = build_instance(tmp_path, extensions=prepared)
    require_selected_storage(instance.database.assembly, prepared)                        # the store matches what it was made for
    chosen = select(instances=[real])
    assert chosen.ids == prepared.ids == ("content",)                                     # the same owner name
    with pytest.raises(DomainError) as refused:
        build_runtime(instance.workspace, paths=instance.paths, database=instance.database, extensions=chosen)
    assert refused.value.code == "invalid_configuration" and "selected extension set" in refused.value.message


class Registers(Extension):
    """A synthetic extension whose only contribution is the registration under test."""

    def __init__(self, id="fake", *, validators=None, sources=(), routes=(), framing=()):
        self.id, self.declared = id, SimpleNamespace(validators=validators or {}, sources=sources, web=WebContribution(routes, framing))

    def package_validators(self):
        return self.declared.validators

    def sources(self):
        return self.declared.sources

    def web(self, api, *, agent_only):
        return self.declared.web


def served(path, *, prefix=False):
    return SimpleNamespace(path=path, prefix=prefix)


ORIGIN_RULE = "https://frame.example.test"
REFUSED_REGISTRATIONS = {
    "core package validator": ({"validators": {"email-management": print}}, "core package validator"),
    "core terminal validator": ({"validators": {"agent-terminal-control": print}}, "core package validator"),
    "core table as a source": ({"sources": (SourceHandler("signoffs"),)}, "core source or item kind"),
    "core inbox source": ({"sources": (SourceHandler("tasks"),)}, "core source or item kind"),
    "core item kind as a reference": ({"sources": (SourceHandler("fake_events", item_type="signoff"),)}, "core source or item kind"),
}
REFUSED_ROUTES = {
    "core API namespace": ({"routes": (Route("/api/fake", print),)}, "overlaps core /api"),
    "core MCP namespace": ({"routes": (Route("/mcp/fake", print),)}, "overlaps core /mcp"),
    "core health namespace": ({"routes": (Route("/health/fake", print),)}, "overlaps core /health"),
    "core static namespace": ({"routes": (Route("/js/fake.js", print),)}, "overlaps core /js"),
    "core asset namespace": ({"routes": (served("/extensions/fake/other"),)}, "overlaps core /extensions"),
    "core page": ({"routes": (Route("/files/view", print),)}, "overlaps core /files/view"),
    "core index": ({"routes": (Route("/", print),)}, "overlaps core /"),
    "everything": ({"routes": (Route("/{anything:path}", print),)}, "whole site"),
    "parameterized root": ({"routes": (Route("/{section}/page", print),)}, "whole site"),
    "opaque route": ({"routes": (served(None),)}, "inspectable absolute path"),
    "relative route": ({"routes": (served("fake/page"),)}, "inspectable absolute path"),
    "proxy prefix over a core page": ({"routes": (served("/tables", prefix=True),)}, "overlaps core /tables/view"),
    "frame rule outside its routes": ({"routes": (Route("/fake/{item}", print),), "framing": (FrameRule("/elsewhere", ORIGIN_RULE),)},
                                      "not covered"),
    "frame rule over a core viewer": ({"routes": (Route("/fake/{item}", print),), "framing": (FrameRule("/tables/view", ORIGIN_RULE),)},
                                      "not covered"),
    "frame rule wider than its route": ({"routes": (served("/fake/page"),), "framing": (FrameRule("/fake/page", ORIGIN_RULE, prefix=True),)},
                                        "not covered"),
}


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    instance, _ = build_instance(tmp_path_factory.mktemp("core"))
    return instance


@pytest.mark.parametrize("case", sorted(REFUSED_REGISTRATIONS))
def test_an_extension_cannot_replace_core_validators_sources_or_item_kinds(core, case):
    declared, reason = REFUSED_REGISTRATIONS[case]
    with pytest.raises(DomainError) as refused:
        build_runtime(core.workspace, paths=core.paths, database=core.database, extensions=select(instances=[Registers(**declared)]))
    assert refused.value.code == "invalid_installation" and reason in refused.value.message


def build_web(core, monkeypatch, tmp_path, *extensions):
    for folder in ("js", "css"):
        (tmp_path / folder).mkdir(exist_ok=True)
    monkeypatch.setattr(core.runtime, "selection", select(instances=list(extensions)))
    return create_app(core.runtime, origins=["http://testserver"], bootstrap_token=TOKEN, dashboard_root=tmp_path, manage_lifecycle=False)


@pytest.mark.parametrize("case", sorted(REFUSED_ROUTES))
def test_extension_routes_and_framing_must_stay_clear_of_core_and_of_each_other(core, monkeypatch, tmp_path, case):
    declared, reason = REFUSED_ROUTES[case]
    with pytest.raises(DomainError) as refused:
        build_web(core, monkeypatch, tmp_path, Registers(**declared))
    assert refused.value.code == "invalid_installation" and reason in refused.value.message


def test_two_extensions_cannot_serve_overlapping_paths_but_may_serve_separate_ones(core, monkeypatch, tmp_path):
    first = Registers("first", routes=(Route("/shared/{item}", print),))
    for routes in ((served("/shared", prefix=True),), (Route("/shared/page", print),)):
        with pytest.raises(DomainError) as refused:
            build_web(core, monkeypatch, tmp_path, first, Registers("second", routes=routes))
        assert "overlaps Extension first route /shared" in refused.value.message
    app = build_web(core, monkeypatch, tmp_path, first, Registers("second", routes=(Route("/shared-other/page", print),),
                                                                 framing=(FrameRule("/shared-other/page", ORIGIN_RULE),)))
    assert app is not None


def test_the_startup_selection_imports_the_installed_factories_by_name(tmp_path):
    """The trusted installation names ``module:factory`` entries; core imports exactly those, validates their own configuration
    and orders them by id. A name that is not installed is a startup error, never a silent skip."""
    pytest.importorskip("qorqut_paper.extension")
    pytest.importorskip("qorqut_content.extension")
    both = select([{"factory": "qorqut_content.extension:create", "config": {"schema_version": 1, "enabled": False}},
                   {"factory": "qorqut_paper.extension:create"}])
    assert both.ids == ("content", "paper")
    assert select([]).ids == () and select(None).ids == ()
    with pytest.raises(DomainError):
        select([{"factory": "qorqut_missing.extension:create"}])
    with pytest.raises(DomainError):
        select([{"factory": "qorqut_paper.extension:create", "config": {"unknown": 1}}])
