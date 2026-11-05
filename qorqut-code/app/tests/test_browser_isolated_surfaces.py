"""Owner viewer staging for a business's isolated browser: business-owned, additive, idempotent and recoverable.

Synthetic only: a temporary instance whose businesses have no registered agent at all; the stager and its CLI run
against these explicit temporary paths. Guacamole, units, browsers and real instance paths are never touched.
"""
import json
from pathlib import Path

import pytest
from qorqut_operations.contracts import DomainError, utc_now

from bazarlyk.browser.isolated import surfaces
from bazarlyk.service import browser_surfaces
from host_foundation_support import build_instance, snapshot

VIEWER = "https://guacamole.example/#/client/NDkAYw"
ORDU = "7f3a1c52-9d4e-4b68-8a2f-5c1e0d9b3a47"
MARKETING = "c2e6b8d4-1a53-4f97-b0e8-6d3a9f2c7e15"
ORDUBASHY_OWN = {"label": "Ordubashy", "kind": "browser", "viewer_url": "https://guacamole.example/#/client/own", "width": 1440, "height": 900}
DESKTOP = {"label": "Desktop", "kind": "desktop", "viewer_url": "https://guacamole.example/#/client/desktop"}
MARKETING_OWN = {"label": "Marketing", "kind": "browser", "viewer_url": "https://guacamole.example/#/client/marketing"}
OTHER = {"label": "Other", "kind": "browser", "viewer_url": "https://guacamole.example/#/client/other"}
WIDGET = {"label": "Widget", "kind": "browser", "viewer_url": VIEWER, "width": 1440, "height": 900}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("staging"), businesses=("widget", "other", "retired"))
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), ids["retired"]))
    return instance, ids


@pytest.fixture
def env(built):
    instance, ids = built
    path = browser_surfaces.surfaces_path(instance.paths)
    for item in path.parent.glob("browser-surfaces*"):
        item.unlink()
    with instance.database.read() as uow:
        assert not uow.connection.execute("SELECT 1 FROM agent_profiles WHERE home_business_id IS NOT NULL").fetchone()
    return type("Env", (), dict(instance=instance, db=instance.database, paths=instance.paths, path=path,
                                widget=ids["widget"], other=ids["other"]))


def stage(e, **options):
    return surfaces.stage(e.db, e.paths, options.pop("business", "widget"), surface_id=options.pop("surface_id", "widget-browser"),
                          **({"label": "Widget", "viewer_url": VIEWER, "width": 1440, "height": 900} | options))


def write(e, document):
    e.path.parent.mkdir(parents=True, exist_ok=True)
    e.path.write_text(json.dumps(document))
    e.path.chmod(0o600)


def read(e):
    return json.loads(e.path.read_text())


def existing(e, **businesses):
    document = {"schema_version": 2,
                "surfaces": {"second-browser": MARKETING_OWN, "ordubashy-browser": ORDUBASHY_OWN, "ordubashy-desktop": DESKTOP,
                             "other-browser": OTHER},
                "agents": {MARKETING: {"browsers": ["second-browser"]},
                           ORDU: {"browsers": ["ordubashy-browser"], "desktop": "ordubashy-desktop"}},
                "businesses": {e.other: {"browser": "other-browser"}, **businesses}}
    write(e, document)
    return document


def database(e):
    with e.db.read() as uow:
        return snapshot(uow.connection, ("agent_profiles", "business_memberships", "businesses", "principals"))


def test_dry_run_reports_the_business_and_writes_nothing(env):
    e = env
    result = stage(e, dry_run=True)
    assert result == {"business_id": e.widget, "slug": "widget", "surface_id": "widget-browser", "removed": False,
                      "changed": True, "dry_run": True}
    assert not e.path.exists() and not list(e.path.parent.glob("browser-surfaces*"))
    existing(e)
    before = e.path.read_bytes()
    assert stage(e, business=e.widget, dry_run=True)["changed"] and e.path.read_bytes() == before
    assert not (e.path.parent / "browser-surfaces.lock").exists()
    assert VIEWER not in json.dumps(result)


def test_a_business_with_no_agent_gets_its_one_browser_and_nothing_else_moves(env):
    e = env
    before, rows = existing(e), database(e)
    result = stage(e)
    assert result["changed"] and (e.path.stat().st_mode & 0o777) == 0o600
    document = read(e)
    assert document["schema_version"] == 2 and document["surfaces"]["widget-browser"] == WIDGET
    assert document["businesses"] == {e.other: {"browser": "other-browser"}, e.widget: {"browser": "widget-browser"}}
    # Every Global agent's own browsers and Desktop, and the other business, are exactly as before.
    assert document["agents"] == before["agents"]
    for key in ("second-browser", "ordubashy-browser", "ordubashy-desktop", "other-browser"):
        assert document["surfaces"][key] == before["surfaces"][key]
    registry = browser_surfaces.read(e.paths)   # the same strict reader the service uses
    assert browser_surfaces.business_surfaces(registry, e.widget)["browsers"] == [{"id": "widget-browser", **WIDGET}]
    assert browser_surfaces.agent_surfaces(registry, ORDU, desktop_allowed=True)["browsers"] == [{"id": "ordubashy-browser", **ORDUBASHY_OWN}]
    # The first prior file is kept byte for byte, and no database row moves.
    [kept] = list(e.path.parent.glob("browser-surfaces.json.prior-*"))
    assert json.loads(kept.read_text()) == before and (kept.stat().st_mode & 0o777) == 0o600
    assert database(e) == rows


def test_a_first_registry_is_created_as_schema_two(env):
    e = env
    stage(e)
    assert read(e) == {"schema_version": 2, "surfaces": {"widget-browser": WIDGET}, "agents": {},
                       "businesses": {e.widget: {"browser": "widget-browser"}}}
    assert not list(e.path.parent.glob("browser-surfaces.json.prior-*"))


def test_a_second_run_is_a_no_op_and_never_rewrites_a_correct_file(env):
    e = env
    existing(e)
    stage(e)
    first = e.path.read_bytes()
    assert stage(e)["changed"] is False and e.path.read_bytes() == first
    # A compact but equivalent file is left alone too.
    e.path.write_text(json.dumps(json.loads(first), separators=(",", ":")))
    e.path.chmod(0o600)
    compact = e.path.read_bytes()
    assert stage(e)["changed"] is False and e.path.read_bytes() == compact


def test_refusals_leave_the_file_untouched(env):
    e = env
    existing(e)
    before = e.path.read_bytes()
    for business in ("no-such-business", "retired"):
        with pytest.raises(DomainError, match="No active project"):
            stage(e, business=business)
    with pytest.raises(DomainError, match="different viewer"):
        stage(e, surface_id="second-browser")
    with pytest.raises(DomainError, match="own surface"):
        stage(e, surface_id="second-browser", label="Marketing", viewer_url=MARKETING_OWN["viewer_url"], width=None, height=None)
    with pytest.raises(DomainError, match="another project"):
        stage(e, surface_id="other-browser", label="Other", viewer_url=OTHER["viewer_url"], width=None, height=None)
    for bad in ("http://user:secret@host/x", "javascript:alert(1)", "not a url"):
        with pytest.raises(DomainError):
            stage(e, viewer_url=bad)
    with pytest.raises(DomainError, match="lowercase letters"):
        stage(e, surface_id="9project")
    assert e.path.read_bytes() == before
    # Each business has exactly one browser: a second surface is refused until the first is removed.
    stage(e)
    bound = e.path.read_bytes()
    with pytest.raises(DomainError, match="already has a browser surface"):
        stage(e, surface_id="widget-second", viewer_url=VIEWER + "2")
    assert e.path.read_bytes() == bound
    e.path.chmod(0o644)
    with pytest.raises(DomainError, match="private"):
        stage(e)
    e.path.chmod(0o600)
    e.path.write_text("{not json")
    with pytest.raises(DomainError, match="not valid JSON"):
        stage(e)
    assert e.path.read_text() == "{not json"
    # An older registry is refused untouched and never converted here.
    old = {"schema_version": 1, "surfaces": {"widget-browser": WIDGET},
           "agents": {"5e89f3d1-1abe-4e48-bd46-a86da432bc7c": {"browsers": ["widget-browser"]}}}
    write(e, old)
    with pytest.raises(DomainError, match="explicit v1.2 migration"):
        stage(e)
    with pytest.raises(DomainError, match="explicit v1.2 migration"):
        stage(e, remove=True)
    assert read(e) == old
    # Any invalid schema-2 document fails closed and is never replaced by an empty one.
    write(e, {"schema_version": 2, "surfaces": {}, "agents": {}})
    with pytest.raises(DomainError):
        stage(e)
    assert read(e) == {"schema_version": 2, "surfaces": {}, "agents": {}}


def test_remove_unbinds_only_this_business_surface_and_is_idempotent(env):
    e = env
    before = existing(e)
    stage(e)
    result = stage(e, remove=True)
    assert result["changed"] and result["removed"]
    document = read(e)
    assert document == before
    browser_surfaces.read(e.paths)
    assert stage(e, remove=True)["changed"] is False
    # Removing another surface ID never unbinds the business's actual browser.
    stage(e)
    assert stage(e, surface_id="other-browser", remove=True)["changed"] is False
    assert read(e)["businesses"][e.widget] == {"browser": "widget-browser"}


def test_staged_example_is_a_valid_business_owned_binding():
    example = Path(__file__).resolve().parents[1] / "tools/browser/isolated/browser-surfaces.project.json.example"
    text = example.read_text()
    assert "CONFIGURED-ORDUBASHY-UUID" in text and "PROJECT-BUSINESS-UUID" in text
    business = "0b9d2c4e-7a51-4f0e-9d3c-2f1e8a6b5c4d"
    value = json.loads(text.replace("CONFIGURED-ORDUBASHY-UUID", ORDU).replace("PROJECT-BUSINESS-UUID", business)
                       .replace("GUACAMOLE-HOST", "guac.example").replace("CONNECTION-IDENTIFIER", "abc"))
    parsed = browser_surfaces.parse(value)
    assert parsed["businesses"] == {business: {"browser": "project-browser"}}
    assert parsed["agents"] == {ORDU: {"browsers": ["ordubashy-browser"], "desktop": "ordubashy-desktop"}}
    desktops = Path(__file__).resolve().parents[1] / "tools/browser/desktops/browser-surfaces.json.example"
    parsed = browser_surfaces.parse(json.loads(desktops.read_text().replace("GUACAMOLE-HOST", "guac.example")))
    assert parsed["businesses"] == {} and set(parsed["agents"]) == {ORDU, MARKETING}


def test_cli_runs_only_against_explicit_paths_and_prints_no_viewer_url(env, capsys):
    e = env
    base = ["--instance-root", str(e.paths.instance_root), "--business", "widget", "--surface-id", "widget-browser", "--label", "Widget"]
    assert surfaces.main([*base, "--viewer-url", VIEWER, "--dry-run"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["changed"] and printed["dry_run"] and not e.path.exists()
    assert surfaces.main([*base, "--viewer-url", VIEWER]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["changed"] and e.path.exists() and VIEWER not in printed
    assert read(e)["businesses"] == {e.widget: {"browser": "widget-browser"}}
    assert surfaces.main([*base]) == 2 and "viewer-url" in capsys.readouterr().err
    assert surfaces.main(base[2:] + ["--viewer-url", VIEWER]) == 2 and "--instance-root" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exited:
        surfaces.main([*base, "--viewer-url", VIEWER, "--with-ordubashy"])
    assert exited.value.code == 2
    capsys.readouterr()
    assert surfaces.main([*base, "--remove"]) == 0
    assert "widget-browser" not in read(e)["surfaces"] and read(e)["businesses"] == {}
