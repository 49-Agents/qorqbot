"""Isolated desktop browser mode: exact identity, mode changes and IP uniqueness."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from bazarlyk.browser import config, service
from qorqut_operations.contracts import DomainError
from foundation_support import BUSINESS, OTHER, database_at

PROXY = "http://188.220.114.215:12323"
EXIT = "188.220.114.215"


@pytest.fixture(autouse=True)
def local_ports(monkeypatch):
    monkeypatch.setattr(config, "port_available", lambda port: True)


def propose(db, root, business_id, browser, *, kind="human"):
    """Prepare and apply one settings change the way the owner path does."""
    with db.transaction() as uow:
        repo = uow.repository("businesses")
        row = repo.get(business_id, required=True)
        value = {**row["config_json"], "browser": browser}
        config.prepare_config(value, row["config_json"], root=root, business_id=business_id,
                              principal_kind=kind, uow=uow)
        config.validate_applied(value["browser"], row["config_json"].get("browser"), root=root,
                                business_id=business_id, uow=uow)
        repo.update(business_id, {"config_json": value}, expected_revision=row["revision"])
        return value["browser"]


def isolated(**changes):
    return {"mode": config.ISOLATED, "enabled": True, "proxy_server": PROXY, "expected_exit_ip": EXIT, **changes}


def stored(db, business_id):
    with db.read() as uow:
        return uow.repository("businesses").get(business_id)["config_json"].get("browser")


def test_isolated_identity_derives_paths_from_registered_hq(tmp_path):
    db = database_at(tmp_path)
    browser = propose(db, tmp_path, BUSINESS, isolated())
    hq = tmp_path / "first" / "hq"
    assert browser["mode"] == config.ISOLATED
    assert browser["user_data_dir"] == str(hq / "browser/profile")
    assert browser["downloads_dir"] == str(hq / "browser/downloads")
    assert browser["credentials_ref"] == "browser-proxy/" + BUSINESS + "-isolated"
    assert browser["cdp_port"] == config.PORTS.start
    assert set(browser) == config.ISOLATED_KEYS
    with db.read() as uow:
        row = uow.repository("businesses").get(BUSINESS)
    assert config.configured_target(row, tmp_path) == browser
    # Repeating the exact request is idempotent; nothing about the identity rotates.
    assert propose(db, tmp_path, BUSINESS, isolated()) == browser


def test_legacy_block_and_validation_are_unchanged(tmp_path):
    db = database_at(tmp_path)
    legacy = propose(db, tmp_path, BUSINESS, {"enabled": True, "proxy_server": "http://proxy.example:8080"})
    assert "mode" not in legacy and set(legacy) == config.LEGACY_KEYS
    assert legacy["credentials_ref"] == "browser-proxy/" + BUSINESS
    with db.read() as uow:
        row = uow.repository("businesses").get(BUSINESS)
    assert config.legacy_target(row, tmp_path) == legacy
    with pytest.raises(DomainError):
        propose(db, tmp_path, BUSINESS, {"enabled": True, "proxy_server": "http://proxy.example:8080",
                                         "expected_exit_ip": EXIT})


@pytest.mark.parametrize("change", [
    {"proxy_server": "https://188.220.114.215:12323"}, {"proxy_server": "socks5://188.220.114.215:1080"},
    {"proxy_server": "http://proxy.example:12323"}, {"proxy_server": "http://127.0.0.1:12323"},
    {"proxy_server": "http://10.0.0.5:12323"}, {"proxy_server": "http://100.64.0.10:12323"},
    {"proxy_server": "http://[2001:db8::1]:12323"}, {"proxy_server": "http://188.220.114.215"},
    {"proxy_server": "http://188.220.114.215:12323/x"}, {"proxy_server": "http://u:p@188.220.114.215:12323"},
    {"proxy_server": "http://0188.220.114.215:12323"}, {"proxy_server": "http://3162730067:12323"},
    {"expected_exit_ip": "proxy.example"}, {"expected_exit_ip": "::1"}, {"expected_exit_ip": "188.220.114.0215"},
    {"expected_exit_ip": "192.168.1.4"}, {"expected_exit_ip": 5},
    {"enabled": "yes"},
    {"cdp_port": 9225}, {"user_data_dir": "/tmp/persona"}, {"downloads_dir": "/tmp/d"},
    {"credentials_ref": "browser-proxy/" + BUSINESS}, {"generation": "invented"}, {"password": "no"},
    {"mode": "isolated-desktop-v2"},
])
def test_isolated_rejects_unsafe_or_generated_values(tmp_path, change):
    db = database_at(tmp_path)
    with pytest.raises(DomainError):
        propose(db, tmp_path, BUSINESS, isolated(**change))
    assert stored(db, BUSINESS) is None


def test_missing_exit_ip_or_enabled_is_never_inherited(tmp_path):
    db = database_at(tmp_path)
    for missing in ("expected_exit_ip", "proxy_server", "enabled"):
        block = isolated()
        del block[missing]
        with pytest.raises(DomainError):
            propose(db, tmp_path, BUSINESS, block)
    browser = propose(db, tmp_path, BUSINESS, isolated())
    # A later proxy change must restate the exit IP; enabling toggles keep both.
    with pytest.raises(DomainError):
        propose(db, tmp_path, BUSINESS, {"proxy_server": "http://188.220.114.216:12323"})
    disabled = propose(db, tmp_path, BUSINESS, {"enabled": False})
    assert disabled["proxy_server"] == PROXY and disabled["expected_exit_ip"] == EXIT
    assert disabled["generation"] != browser["generation"]


def test_config_edit_without_mode_keeps_the_isolated_identity(tmp_path):
    db = database_at(tmp_path)
    browser = propose(db, tmp_path, BUSINESS, isolated())
    with db.transaction() as uow:
        repo = uow.repository("businesses")
        row = repo.get(BUSINESS)
        value = {**row["config_json"], "stage": "validating"}
        config.prepare_config(value, row["config_json"], root=tmp_path, business_id=BUSINESS,
                              principal_kind="agent", uow=uow)
        assert value["browser"] == browser


def test_agents_cannot_write_or_move_the_browser_block(tmp_path):
    db = database_at(tmp_path)
    with pytest.raises(DomainError, match="require the owner"):
        propose(db, tmp_path, BUSINESS, isolated(), kind="agent")
    assert stored(db, BUSINESS) is None


def test_hq_must_stay_inside_the_businesses_root_without_symlinks(tmp_path):
    db = database_at(tmp_path)
    with db.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET hq_path=? WHERE id=?", (str(tmp_path.parent / "elsewhere"), BUSINESS))
    with pytest.raises(DomainError):
        propose(db, tmp_path, BUSINESS, isolated())
    real = tmp_path / "real-hq"
    real.mkdir()
    (tmp_path / "first").mkdir()
    (tmp_path / "first" / "hq").symlink_to(real)
    with db.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET hq_path=? WHERE id=?", (str(tmp_path / "first" / "hq"), BUSINESS))
    with pytest.raises(DomainError, match="symlink"):
        propose(db, tmp_path, BUSINESS, isolated())
    (tmp_path / "first" / "hq").unlink()
    (tmp_path / "first" / "hq").mkdir()
    (tmp_path / "first" / "hq" / "browser").symlink_to(real)
    with pytest.raises(DomainError, match="symlink"):
        propose(db, tmp_path, BUSINESS, isolated())


def test_saved_identity_is_validated_exactly_even_when_disabled(tmp_path):
    db = database_at(tmp_path)
    browser = propose(db, tmp_path, BUSINESS, isolated())
    for bad in ({**browser, "user_data_dir": "/tmp/x"}, {**browser, "credentials_ref": "browser-proxy/x"},
                {**browser, "cdp_port": 80}, {**browser, "generation": ""}, {**browser, "extra": 1},
                {**browser, "proxy_server": "http://proxy.example:80"}, {**browser, "expected_exit_ip": "10.1.1.1"},
                {key: value for key, value in browser.items() if key != "expected_exit_ip"}):
        row = {"id": BUSINESS, "hq_path": str(tmp_path / "first/hq"), "config_json": {"browser": bad}}
        with pytest.raises(DomainError):
            config.configured_target(row, tmp_path)
        with pytest.raises(DomainError):
            config.check_identity(row, tmp_path, bad)
    mixed = {**browser, "mode": None}
    with pytest.raises(DomainError):
        config.check_identity({"id": BUSINESS, "hq_path": str(tmp_path / "first/hq")}, tmp_path, mixed)


def test_legacy_only_consumers_reject_the_isolated_mode(tmp_path):
    db = database_at(tmp_path)
    propose(db, tmp_path, BUSINESS, isolated())
    with db.read() as uow:
        row = uow.repository("businesses").get(BUSINESS)
    with pytest.raises(DomainError, match="isolated desktop"):
        config.legacy_target(row, tmp_path)
    with pytest.raises(DomainError, match="different browser mode"):
        with service._admitted_legacy_writer(db, tmp_path, BUSINESS):
            pytest.fail("the headless host service must never admit an isolated block")
    # The isolated session admits it under the same durable lock.
    with service._admitted_legacy_writer(db, tmp_path, BUSINESS, isolated=True) as (browser, directory):
        assert browser["mode"] == config.ISOLATED
        assert directory == tmp_path / ".bazarlyk/browsers" / BUSINESS
        with pytest.raises(DomainError, match="already running"):
            with service._admitted_legacy_writer(db, tmp_path, BUSINESS, isolated=True):
                pass


def test_legacy_service_still_admits_legacy_and_isolated_session_refuses_it(tmp_path):
    db = database_at(tmp_path)
    propose(db, tmp_path, BUSINESS, {"enabled": True, "proxy_server": "http://proxy.example:8080"})
    with service._admitted_legacy_writer(db, tmp_path, BUSINESS) as (browser, _):
        assert "mode" not in browser
    with pytest.raises(DomainError, match="different browser mode"):
        with service._admitted_legacy_writer(db, tmp_path, BUSINESS, isolated=True):
            pass


def test_mode_change_needs_explicit_mode_stopped_service_and_fresh_generation(tmp_path):
    db = database_at(tmp_path)
    legacy = propose(db, tmp_path, BUSINESS, {"enabled": True, "proxy_server": "http://166.88.210.144:12323"})
    # No mode, no transition: the block stays legacy and rejects isolated-only fields.
    with pytest.raises(DomainError):
        propose(db, tmp_path, BUSINESS, {"expected_exit_ip": EXIT})
    directory = tmp_path / ".bazarlyk/browsers" / BUSINESS
    directory.mkdir(parents=True)
    (directory / "ready.json").write_text("{}")
    with pytest.raises(DomainError, match="Stop this project browser"):
        propose(db, tmp_path, BUSINESS, isolated())
    (directory / "ready.json").unlink()
    (directory / "lease.json").write_text('{"holder": "a"}')
    with pytest.raises(DomainError, match="leased"):
        propose(db, tmp_path, BUSINESS, isolated())
    (directory / "lease.json").write_text("{}")
    (directory / "profile").mkdir()
    (directory / "profile" / "Cookies").write_text("old persona")
    moved = propose(db, tmp_path, BUSINESS, isolated())
    assert moved["cdp_port"] == legacy["cdp_port"]
    assert moved["generation"] != legacy["generation"]
    assert moved["user_data_dir"] != legacy["user_data_dir"]
    # Nothing about the old profile or its credential file is touched by the transition.
    assert (directory / "profile" / "Cookies").read_text() == "old persona"
    # Rollback is explicit, restates the proxy, and stores no mode key.
    with pytest.raises(DomainError):
        propose(db, tmp_path, BUSINESS, {"mode": "legacy"})
    back = propose(db, tmp_path, BUSINESS, {"mode": "legacy", "enabled": False, "proxy_server": "http://166.88.210.144:12323"})
    assert "mode" not in back and back["user_data_dir"] == legacy["user_data_dir"]
    assert back["generation"] not in {legacy["generation"], moved["generation"]}


def test_mode_change_is_refused_while_the_service_lock_is_held(tmp_path):
    import fcntl
    db = database_at(tmp_path)
    propose(db, tmp_path, BUSINESS, {"enabled": True, "proxy_server": "http://166.88.210.144:12323"})
    directory = tmp_path / ".bazarlyk/browsers" / BUSINESS
    directory.mkdir(parents=True)
    with (directory / "service.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(DomainError, match="running"):
            propose(db, tmp_path, BUSINESS, isolated())
    assert "mode" not in stored(db, BUSINESS)


def test_sandbox_assigned_business_cannot_select_the_isolated_browser(tmp_path):
    db = database_at(tmp_path)
    with db.transaction() as uow:
        uow.repository("business_sandboxes").insert({"business_id": BUSINESS, "desired_generation": 1, "state": "pending"})
    with pytest.raises(DomainError, match="sandbox"):
        propose(db, tmp_path, BUSINESS, isolated())


# --- proxy IP uniqueness ---------------------------------------------------------------------


def test_second_business_cannot_claim_the_same_proxy_or_exit_ip(tmp_path):
    db = database_at(tmp_path)
    propose(db, tmp_path, BUSINESS, isolated())
    with pytest.raises(DomainError, match="already assigned"):
        propose(db, tmp_path, OTHER, isolated(proxy_server="http://188.220.114.215:9999"))
    with pytest.raises(DomainError, match="already assigned"):
        propose(db, tmp_path, OTHER, isolated(proxy_server="http://104.16.0.1:1", expected_exit_ip=EXIT))
    with pytest.raises(DomainError, match="already assigned"):
        propose(db, tmp_path, OTHER, isolated(proxy_server=PROXY, expected_exit_ip="104.16.0.2"))
    assert stored(db, OTHER) is None


def test_disabled_and_legacy_incumbents_still_reserve_their_ips(tmp_path):
    db = database_at(tmp_path)
    propose(db, tmp_path, BUSINESS, {"enabled": False, "proxy_server": "http://166.88.210.144:12323"})
    with pytest.raises(DomainError, match="already assigned"):
        propose(db, tmp_path, OTHER, isolated(proxy_server="http://166.88.210.144:9", expected_exit_ip="166.88.210.144"))
    with pytest.raises(DomainError, match="already assigned"):
        propose(db, tmp_path, OTHER, {"enabled": True, "proxy_server": "http://166.88.210.144:1"})
    # A hostname proxy claims no IP; the legacy path stays open for such configurations.
    propose(db, tmp_path, OTHER, {"enabled": True, "proxy_server": "http://proxy.example:80"})


def test_concurrent_prepared_assignments_cannot_both_apply(tmp_path):
    db = database_at(tmp_path)
    prepared = {}
    for name, identity in (("a", BUSINESS), ("b", OTHER)):
        with db.read() as uow:
            row = uow.repository("businesses").get(identity)
            value = {**row["config_json"], "browser": isolated(proxy_server="http://188.220.114.215:%d" % (2000 if name == "a" else 3000))}
            config.prepare_config(value, row["config_json"], root=tmp_path, business_id=identity, principal_kind="human", uow=uow)
            prepared[name] = (identity, row, value)
    # Each passed its own preparation; only the writing transaction can arbitrate.
    outcomes = []
    for name in ("a", "b"):
        identity, row, value = prepared[name]
        try:
            with db.transaction() as uow:
                config.validate_applied(value["browser"], row["config_json"].get("browser"), root=tmp_path,
                                        business_id=identity, uow=uow)
                uow.repository("businesses").update(identity, {"config_json": value}, expected_revision=row["revision"])
            outcomes.append("applied")
        except DomainError as error:
            outcomes.append(error.code)
    assert outcomes == ["applied", "configuration_conflict"]
    assert stored(db, OTHER) is None


def test_unchanged_claims_are_not_relitigated_and_release_frees_the_ip(tmp_path):
    db = database_at(tmp_path)
    propose(db, tmp_path, BUSINESS, isolated())
    # Moving the incumbent to another exit frees the old address for a different business.
    propose(db, tmp_path, BUSINESS, isolated(proxy_server="http://188.220.114.216:12323", expected_exit_ip="188.220.114.216"))
    assert propose(db, tmp_path, OTHER, isolated())["expected_exit_ip"] == EXIT


def test_applied_validation_rejects_forged_or_stale_blocks(tmp_path):
    db = database_at(tmp_path)
    browser = propose(db, tmp_path, BUSINESS, isolated())
    with db.transaction() as uow:
        forged = {**browser, "user_data_dir": str(tmp_path / "elsewhere/profile")}
        with pytest.raises(DomainError):
            config.validate_applied(forged, browser, root=tmp_path, business_id=BUSINESS, uow=uow)
        clash = {**browser, "generation": "g2"}
        # An unchanged claim by the same business is fine.
        config.validate_applied(clash, browser, root=tmp_path, business_id=BUSINESS, uow=uow)
        # Another business's block must carry that business's own derived identity.
        with pytest.raises(DomainError, match="identity is invalid"):
            config.validate_applied(browser, None, root=tmp_path, business_id=OTHER, uow=uow)
        theirs = {**browser, "user_data_dir": str(tmp_path / "second/hq/browser/profile"),
                  "downloads_dir": str(tmp_path / "second/hq/browser/downloads"),
                  "credentials_ref": "browser-proxy/" + OTHER + "-isolated", "cdp_port": browser["cdp_port"] + 1}
        with pytest.raises(DomainError, match="already assigned"):
            config.validate_applied(theirs, None, root=tmp_path, business_id=OTHER, uow=uow)
        config.validate_applied(None, browser, root=tmp_path, business_id=BUSINESS, uow=uow)
