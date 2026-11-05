"""Isolated provisioning: idempotent, private, credential-free projection, no direct database writes."""
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import threading

import pytest

from bazarlyk.browser import config, provision as legacy
from bazarlyk.browser.isolated import deployment as deploy, provision, setup
from qorqut_operations.contracts import DomainError
from test_environment_discovery import service  # noqa: F401  (fixture)

PROXY = "http://188.220.114.215:12323"
EXIT = "188.220.114.215"
URL = "http://tenant-user:tenant-secret@188.220.114.215:12323"
NOW = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)


def snapshot(tmp_path, *, ip=EXIT, classification="no-known-non-HN-account", profile="clean", persona_assigned=False):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema": 1, "generated_at": "2026-09-29T22:00:00Z", "source": "sanitized proxy audit", "ips": {
        ip: {"classification": classification, "profile_check": profile, "non_hn_platform_count": 0,
             "persona_assigned": persona_assigned,
             "exit_verified": True, "checked_at": "2026-09-29T22:00:00Z"}}}))
    path.chmod(0o600)
    return path


def saved(service):
    with service.runtime.database.read() as uow:
        return uow.repository("businesses").get(service.fixture.business_id)


def run(service, tmp_path, **options):
    if "inventory_path" not in options:
        options["inventory_path"] = snapshot(tmp_path)
    options.setdefault("now", NOW)
    return provision.provision_isolated(service.runtime, "widget", URL, expected_exit_ip=EXIT, **options)


def deployment_file(service, tmp_path, **changes):
    value = {"schema_version": 1, "business_id": service.fixture.business_id, "root": str(service.runtime.root),
             "display": 23, "geometry": {"width": 1440, "height": 900}, "vnc": {"interface": "127.0.0.1", "port": 5923},
             "runtime_dir": str(tmp_path / "run"), "tools_dir": str(tmp_path / "tools"), **changes}
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_dry_run_and_ineligible_inputs_write_nothing(service, tmp_path):
    before = saved(service)
    report = run(service, tmp_path, dry_run=True)
    assert report["will_write"] is False and report["eligibility"] == {EXIT: "available"} and "would" in report
    assert "tenant-secret" not in json.dumps(report)
    # Unknown, occupied or missing evidence is never free; nothing is written on refusal.
    for evidence in (None, dict(classification="occupied/reserved"), dict(profile="unavailable"), dict(ip="104.16.0.9"),
                     dict(persona_assigned=True)):
        with pytest.raises(DomainError, match="not confirmed available"):
            run(service, tmp_path, inventory_path=snapshot(tmp_path, **evidence) if evidence else None)
    assert run(service, tmp_path, dry_run=True, inventory_path=None)["blocked"]
    assert saved(service) == before
    assert not (service.runtime.root / ".bazarlyk/secrets").exists()


def test_old_audit_cannot_allocate_without_persona_evidence(service, tmp_path):
    path = snapshot(tmp_path)
    value = json.loads(path.read_text())
    del value["ips"][EXIT]["persona_assigned"]
    path.write_text(json.dumps(value))
    with pytest.raises(DomainError, match="persona_assigned=false"):
        run(service, tmp_path, inventory_path=path)
    assert "browser" not in saved(service)["config_json"]
    assert not (service.runtime.root / ".bazarlyk/secrets").exists()


@pytest.mark.parametrize("evidence", [None, {"persona_assigned": True}])
def test_existing_assignment_is_preserved_without_a_new_free_ip_check(service, tmp_path, evidence):
    run(service, tmp_path)
    before = saved(service)
    path = snapshot(tmp_path, **evidence) if evidence else None
    result = run(service, tmp_path, inventory_path=path)
    assert result["already_assigned"] is True
    assert saved(service) == before


def test_provisioning_is_private_idempotent_and_credential_free(service, tmp_path):
    first = run(service, tmp_path)
    business_id = service.fixture.business_id
    row = saved(service)
    browser = row["config_json"]["browser"]
    assert browser["mode"] == config.ISOLATED and browser["expected_exit_ip"] == EXIT and first["started"] is False
    secret = service.runtime.root / ".bazarlyk/secrets/browser-proxy" / (business_id + "-isolated.json")
    assert secret.stat().st_mode & 0o777 == 0o600
    assert json.loads(secret.read_text()) == {"username": "tenant-user", "password": "tenant-secret"}
    assert not (secret.parent / (business_id + ".json")).exists()  # the legacy file name is never used here
    projection = service.fixture.hq / "browser/setup.json"
    text = projection.read_text()
    assert json.loads(text)["proxy"] == {"server": PROXY, "expected_exit_ip": EXIT,
                                          "credentials_reference": "browser-proxy/" + business_id + "-isolated"}
    assert "tenant" not in text and "secret" not in text and "password" not in text
    assert (service.fixture.hq / "browser/.gitignore").read_text() == "*\n"
    assert not (service.runtime.root / ".bazarlyk/browsers").exists()  # nothing started, no lock or lease created
    # Repeating it changes nothing: same revision, same generation, same file.
    second = run(service, tmp_path)
    assert second["already_assigned"] is True and second["projection_written"] is False
    assert saved(service)["revision"] == row["revision"] and saved(service)["config_json"]["browser"] == browser
    # Different credentials for the same assignment are refused, never rotated.
    with pytest.raises(DomainError, match="differ"):
        provision.provision_isolated(service.runtime, "widget", "http://tenant-user:other@188.220.114.215:12323",
                                     expected_exit_ip=EXIT, inventory_path=snapshot(tmp_path), now=NOW)
    assert json.loads(secret.read_text())["password"] == "tenant-secret"


@pytest.mark.parametrize("url", ["http://188.220.114.215:12323", "https://u:p@188.220.114.215:12323",
                                 "http://u:p@proxy.example:12323", "socks5://u:p@188.220.114.215:1080", "http://u:p@10.1.2.3:8080"])
def test_only_authenticated_numeric_http_proxies(service, tmp_path, url):
    with pytest.raises(DomainError):
        provision.provision_isolated(service.runtime, "widget", url, expected_exit_ip=EXIT,
                                     inventory_path=snapshot(tmp_path), now=NOW)
    assert "browser" not in saved(service)["config_json"]
    assert not (service.runtime.root / ".bazarlyk/secrets").exists()


def test_a_legacy_assignment_moves_only_by_the_explicit_mode_and_keeps_its_files(service, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "port_available", lambda port: True)
    business_id = service.fixture.business_id
    legacy.provision(service.runtime, "widget", "http://legacy-user:legacy-secret@proxy.example:8080")
    old = saved(service)["config_json"]["browser"]
    legacy_file = service.runtime.root / ".bazarlyk/secrets/browser-proxy" / (business_id + ".json")
    profile = service.runtime.root / ".bazarlyk/browsers" / business_id / "profile"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("old persona")
    prior = tmp_path / "prior.json"
    run(service, tmp_path, prior_path=prior)
    new = saved(service)["config_json"]["browser"]
    assert new["cdp_port"] == old["cdp_port"] and new["generation"] != old["generation"] and new["user_data_dir"] != old["user_data_dir"]
    assert json.loads(legacy_file.read_text()) == {"username": "legacy-user", "password": "legacy-secret"}
    assert (profile / "Cookies").read_text() == "old persona"
    # The exact prior block is kept privately for canonical-API recovery, and the first record wins.
    kept = json.loads(prior.read_text())
    assert kept["browser"] == old and prior.stat().st_mode & 0o777 == 0o600 and "legacy-secret" not in prior.read_text()
    # The legacy provisioning command refuses to touch an explicit-mode business.
    with pytest.raises(DomainError, match="explicit browser mode"):
        legacy.provision(service.runtime, "widget", "http://legacy-user:legacy-secret@proxy.example:8080")


def test_a_running_legacy_browser_blocks_the_mode_change(service, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "port_available", lambda port: True)
    legacy.provision(service.runtime, "widget", "http://legacy-user:legacy-secret@proxy.example:8080")
    directory = service.runtime.root / ".bazarlyk/browsers" / service.fixture.business_id
    directory.mkdir(parents=True)
    (directory / "ready.json").write_text("{}")
    before = saved(service)["config_json"]["browser"]
    with pytest.raises(DomainError):
        run(service, tmp_path)
    assert saved(service)["config_json"]["browser"] == before


def test_another_business_holding_the_ip_blocks_provisioning(service, tmp_path):
    from qorqut_operations.contracts import new_id
    with service.runtime.database.transaction() as uow:
        uow.repository("businesses").insert({"id": new_id(), "slug": "other", "name": "Other", "workspace": str(tmp_path / "o"),
                                             "hq_path": str(tmp_path / "o/hq"), "config_json": {"browser": {
                                                 "enabled": False, "proxy_server": PROXY, "cdp_port": 19401,
                                                 "user_data_dir": "x", "downloads_dir": "y", "credentials_ref": "z", "generation": "g"}}})
    with pytest.raises(DomainError, match="not confirmed available"):
        run(service, tmp_path)
    assert "browser" not in saved(service)["config_json"]


@pytest.mark.skipif(not os.access("/usr/bin/vncpasswd", os.X_OK), reason="TigerVNC tools are not installed")
def test_deployment_adds_private_vnc_secrets_and_a_desktop_projection(service, tmp_path):
    path = deployment_file(service, tmp_path)
    run(service, tmp_path, deployment_path=path)
    item = deploy.load(path)
    for name in ("vnc.password", "vnc.passwd"):
        assert (item.secrets_dir / name).stat().st_mode & 0o777 == 0o600
    password = (item.secrets_dir / "vnc.password").read_text().strip()
    projection = json.loads((service.fixture.hq / "browser/setup.json").read_text())
    assert projection["desktop"] == {"display": 23, "geometry": {"width": 1440, "height": 900},
                                     "vnc": {"interface": "127.0.0.1", "port": 5923}}
    assert password not in json.dumps(projection) and password not in Path(item.vnc_password_file).read_text(errors="ignore")
    derived = item.vnc_password_file.read_bytes()
    run(service, tmp_path, deployment_path=path)  # rerun keeps both files exactly as they were
    assert item.vnc_password_file.read_bytes() == derived and (item.secrets_dir / "vnc.password").read_text().strip() == password
    with pytest.raises(DomainError):
        run(service, tmp_path, deployment_path=deployment_file(service, tmp_path, business_id="7a49b000-0000-4000-8000-000000000049"))


def test_vnc_password_repair_states_are_explicit(service, tmp_path):
    item = deploy.load(deployment_file(service, tmp_path))
    fake = tmp_path / "vncpasswd"
    fake.write_text("#!/bin/sh\ncat >/dev/null\nprintf 'OBFUSCATED'\n")
    fake.chmod(0o700)
    assert setup.ensure_vnc_password(item, vncpasswd=str(fake), generate=lambda: "fixed-pass") is True
    assert (item.secrets_dir / "vnc.password").read_text() == "fixed-pass\n" and item.vnc_password_file.read_bytes() == b"OBFUSCATED"
    assert setup.ensure_vnc_password(item, vncpasswd=str(fake)) is False
    (item.secrets_dir / "vnc.password").unlink()
    with pytest.raises(DomainError, match="explicit repair"):
        setup.ensure_vnc_password(item, vncpasswd=str(fake))


def test_check_is_read_only_and_reports_what_is_missing(service, tmp_path):
    path = deployment_file(service, tmp_path)
    report = provision.check_isolated(service.runtime, "widget", path)
    failed = {item["check"] for item in report["findings"] if not item["ok"]}
    assert report["ok"] is False and {"assignment", "isolated proxy credentials", "setup projection", "VNC vnc.password"} <= failed
    assert not (service.runtime.root / ".bazarlyk/secrets").exists() and not (service.fixture.hq / "browser").exists()
    run(service, tmp_path, deployment_path=path) if os.access("/usr/bin/vncpasswd", os.X_OK) else None
    with pytest.raises(DomainError):
        provision.check_isolated(service.runtime, "widget", deployment_file(service, tmp_path, root="/somewhere/else"))


def test_check_requires_a_bundled_node_and_a_relay_that_uses_it_for_a_bundled_runtime(service, tmp_path):
    tools = tmp_path / "tools"
    (tools / "node/bin").mkdir(parents=True)

    def findings(**changes):
        report = provision.check_isolated(service.runtime, "widget", deployment_file(service, tmp_path, **changes))
        return {item["check"]: item["ok"] for item in report["findings"]}
    assert "tools bundled node" not in findings()  # an older runtime without the wrapper is judged as before
    (tools / "run-agent-browser").write_text("#!/bin/sh\n")
    (tools / "run-agent-browser").chmod(0o755)
    missing = findings()
    assert missing["tools bundled node"] is False and missing["relay uses the bundled node"] is False
    bundled, other = tools / "node/bin/node", tmp_path / "other-node"
    for node in (bundled, other):
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
    assert findings(node=str(bundled))["tools bundled node"] and findings(node=str(bundled))["relay uses the bundled node"]
    assert findings(node=str(other))["tools bundled node"] is True and findings(node=str(other))["relay uses the bundled node"] is False
    bundled.chmod(0o644)
    assert findings(node=str(bundled))["tools bundled node"] is False


def test_cli_dry_run_reads_stdin_only_and_reports_without_secrets(service, tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(URL))
    monkeypatch.setattr("bazarlyk.service.runtime.build_runtime", lambda root, workspace: service.runtime)
    inventory_path = snapshot(tmp_path)
    code = legacy.main(["--root", str(service.runtime.root), "--workspace", str(tmp_path), "--business", "widget",
                        "--mode", "isolated-desktop-v1", "--expected-exit-ip", EXIT, "--inventory", str(inventory_path), "--dry-run"])
    output = capsys.readouterr()
    assert code == 0 and "tenant-secret" not in output.out + output.err
    assert json.loads(output.out)["will_write"] is False
    assert not (service.runtime.root / ".bazarlyk/secrets").exists()


# --- concurrent and interleaved provisioning -------------------------------------------------------------------

OTHER_EXIT = "188.220.114.216"
OTHER_PROXY = "http://188.220.114.216:12323"


def snapshot_many(tmp_path, *ips):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema": 1, "generated_at": "2026-09-29T22:00:00Z", "source": "sanitized proxy audit", "ips": {
        ip: {"classification": "no-known-non-HN-account", "profile_check": "clean", "non_hn_platform_count": 0,
             "persona_assigned": False,
             "exit_verified": True, "checked_at": "2026-09-29T22:00:00Z"} for ip in ips}}))
    path.chmod(0o600)
    return path


def race(*calls):
    """Each call on its own thread, released together; returns (value, error) pairs and never hides a crash."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def worker(index, call):
        barrier.wait()
        try:
            results[index] = (call(), None)
        except Exception as error:  # noqa: BLE001 - asserted to be a DomainError below
            results[index] = (None, error)
    threads = [threading.Thread(target=worker, args=(index, call)) for index, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert all(result is not None for result in results), "a competing call did not finish"
    for _value, error in results:
        assert error is None or isinstance(error, DomainError), repr(error)
    return results


def isolated_secret(service):
    return service.runtime.root / ".bazarlyk/secrets/browser-proxy" / (service.fixture.business_id + "-isolated.json")


def tree(root):
    return sorted(str(path.relative_to(root)) for path in Path(root).rglob("*"))


def test_direct_credential_writers_race_without_any_overwrite(tmp_path):
    root, business_id = tmp_path, "6c7c72b7-78c8-5791-a0af-95cbb7980ece"
    block = {"mode": config.ISOLATED}
    path = root / ".bazarlyk/secrets/browser-proxy" / (business_id + "-isolated.json")
    passwords = [f"password-{index}" for index in range(8)]
    results = race(*[lambda p=p: setup.store_credentials(root, business_id, block, {"username": "user", "password": p})
                     for p in passwords])
    winners = [index for index, (value, error) in enumerate(results) if value is True]
    assert len(winners) == 1  # exactly one creator; the others never reported creating it
    assert json.loads(path.read_text()) == {"password": passwords[winners[0]], "username": "user"}
    for index, (value, error) in enumerate(results):
        if index != winners[0]:
            assert value is None and error.code == "configuration_conflict"
            assert not any(password in str(error) or password in error.message for password in passwords)
    assert path.stat().st_mode & 0o777 == 0o600 and not list(path.parent.glob("*.tmp"))
    # Identical competitors: one creates, the rest are no-ops, and nothing is rewritten.
    path.unlink()
    identical = race(*[lambda: setup.store_credentials(root, business_id, block, {"username": "user", "password": "same"})
                       for _ in range(8)])
    assert sorted(value for value, error in identical if error is None) == [False] * 7 + [True]
    assert all(error is None for _value, error in identical)
    assert json.loads(path.read_text())["password"] == "same"


def test_a_secret_is_published_whole_or_not_at_all_and_never_replaced(tmp_path):
    (tmp_path / "keep").write_bytes(b"first")
    assert legacy.publish_private(tmp_path / "keep", b"second") is False
    assert (tmp_path / "keep").read_bytes() == b"first"
    assert legacy.publish_private(tmp_path / "new", b"value") is True
    assert (tmp_path / "new").stat().st_mode & 0o777 == 0o600 and not list(tmp_path.glob("*.tmp"))
    (tmp_path / "target").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "target")
    with pytest.raises(DomainError, match="symlink"):
        legacy.publish_private(tmp_path / "link", b"value")
    assert (tmp_path / "target").read_text() == "x"
    with pytest.raises(DomainError, match="authenticated"):
        setup.store_credentials(tmp_path, "b", {"mode": config.ISOLATED}, {"username": "", "password": ""})


def test_competing_vnc_password_creators_converge_on_one_matching_pair(service, tmp_path):
    item = deploy.load(deployment_file(service, tmp_path))
    fake = tmp_path / "vncpasswd"
    fake.write_text("#!/bin/sh\ntr 'A-Za-z' 'N-ZA-Mn-za-m'\n")  # a deterministic stand-in for the obfuscation
    fake.chmod(0o700)
    passwords = [f"generated-{index}" for index in range(6)]
    results = race(*[lambda p=p: setup.ensure_vnc_password(item, vncpasswd=str(fake), generate=lambda: p) for p in passwords])
    assert all(error is None for _value, error in results)
    plain = (item.secrets_dir / "vnc.password").read_text()
    assert plain.strip() in passwords
    assert item.vnc_password_file.read_bytes() == plain.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm")).encode()
    assert not list(item.secrets_dir.glob("*.tmp"))


def test_identical_competing_provisions_leave_one_result_and_retry_is_a_no_op(service, tmp_path):
    results = race(*[lambda: run(service, tmp_path, inventory_path=snapshot(tmp_path)) for _ in range(4)])
    completed = [value for value, error in results if error is None]
    assert completed and all(error.code == "busy" for _value, error in results if error is not None)
    row = saved(service)
    assert row["config_json"]["browser"]["mode"] == config.ISOLATED
    assert json.loads(isolated_secret(service).read_text()) == {"password": "tenant-secret", "username": "tenant-user"}
    retry = run(service, tmp_path)
    assert retry["already_assigned"] is True and retry["projection_written"] is False
    assert saved(service)["revision"] == row["revision"]


def test_different_competing_credentials_leave_exactly_one_winner_and_the_first_prior(service, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "port_available", lambda port: True)
    business_id = service.fixture.business_id
    legacy.provision(service.runtime, "widget", "http://legacy-user:legacy-secret@proxy.example:8080")
    old = saved(service)["config_json"]["browser"]
    legacy_file = service.runtime.root / ".bazarlyk/secrets/browser-proxy" / (business_id + ".json")
    legacy_bytes = legacy_file.read_bytes()
    prior = tmp_path / "prior.json"
    inventory_path = snapshot(tmp_path)
    urls = ("http://user-a:secret-a@188.220.114.215:12323", "http://user-b:secret-b@188.220.114.215:12323")
    results = race(*[lambda url=url: provision.provision_isolated(
        service.runtime, "widget", url, expected_exit_ip=EXIT, inventory_path=inventory_path, prior_path=prior, now=NOW)
        for url in urls])
    winners = [index for index, (value, error) in enumerate(results) if error is None]
    assert len(winners) == 1
    loser = results[1 - winners[0]][1]
    assert loser.code in {"busy", "configuration_conflict"}
    winner_user, winner_secret = ("user-a", "secret-a") if winners[0] == 0 else ("user-b", "secret-b")
    assert json.loads(isolated_secret(service).read_text()) == {"password": winner_secret, "username": winner_user}
    output = str(loser) + loser.message + json.dumps(getattr(loser, "details", None))
    assert not any(value in output for value in ("secret-a", "secret-b", "user-a", "user-b", "legacy-secret"))
    assert saved(service)["config_json"]["browser"]["mode"] == config.ISOLATED
    # Both old credentials and the first prior record survive untouched.
    assert legacy_file.read_bytes() == legacy_bytes
    assert json.loads(prior.read_text())["browser"] == old and "legacy-secret" not in prior.read_text()
    # A later differing call still changes nothing.
    before = (saved(service)["revision"], isolated_secret(service).read_bytes(), prior.read_bytes())
    with pytest.raises(DomainError, match="differ"):
        provision.provision_isolated(service.runtime, "widget", urls[1 - winners[0]], expected_exit_ip=EXIT,
                                     inventory_path=inventory_path, prior_path=prior, now=NOW)
    assert (saved(service)["revision"], isolated_secret(service).read_bytes(), prior.read_bytes()) == before


def test_competing_different_proxies_with_the_same_credentials_cannot_both_apply(service, tmp_path):
    inventory_path = snapshot_many(tmp_path, EXIT, OTHER_EXIT)
    calls = [lambda: provision.provision_isolated(service.runtime, "widget", URL, expected_exit_ip=EXIT,
                                                  inventory_path=inventory_path, now=NOW),
             lambda: provision.provision_isolated(service.runtime, "widget", "http://tenant-user:tenant-secret@" + OTHER_PROXY[7:],
                                                  expected_exit_ip=OTHER_EXIT, inventory_path=inventory_path, now=NOW)]
    results = race(*calls)
    winners = [index for index, (value, error) in enumerate(results) if error is None]
    assert len(winners) == 1
    assert results[1 - winners[0]][1].code in {"busy", "configuration_conflict"}
    browser = saved(service)["config_json"]["browser"]
    assert (browser["proxy_server"], browser["expected_exit_ip"]) == ((PROXY, EXIT), (OTHER_PROXY, OTHER_EXIT))[winners[0]]
    # Provisioning never re-points an already isolated business, even with identical credentials.
    other = (OTHER_PROXY, OTHER_EXIT, EXIT)[0:2] if winners[0] == 0 else (PROXY, EXIT)
    revision = saved(service)["revision"]
    with pytest.raises(DomainError, match="different isolated browser assignment"):
        provision.provision_isolated(service.runtime, "widget", "http://tenant-user:tenant-secret@" + other[0][7:],
                                     expected_exit_ip=other[1], inventory_path=inventory_path, now=NOW)
    assert saved(service)["revision"] == revision


def test_a_held_provisioning_lock_refuses_legacy_and_isolated_writes_and_creates_nothing(service, tmp_path):
    root, business_id = service.runtime.root, service.fixture.business_id
    before = saved(service)
    with legacy.provisioning_lock(root, business_id):
        for attempt in (lambda: run(service, tmp_path),
                        lambda: legacy.provision(service.runtime, "widget", "http://legacy-user:legacy-secret@proxy.example:8080")):
            with pytest.raises(DomainError, match="Another provisioning") as raised:
                attempt()
            assert raised.value.code == "busy"
            assert "tenant-secret" not in raised.value.message and "legacy-secret" not in raised.value.message
        # Reads never wait on it: a dry run and the read-only check still answer.
        assert run(service, tmp_path, dry_run=True)["will_write"] is False
        assert not isolated_secret(service).exists() and not (root / ".bazarlyk/secrets/browser-proxy" / (business_id + ".json")).exists()
        assert not (service.fixture.hq / "browser").exists()
    assert saved(service) == before
    assert run(service, tmp_path)["browser"]["mode"] == config.ISOLATED  # the identical retry is safe once it is free


def test_the_lock_is_held_from_the_state_read_through_apply_and_projection(service, tmp_path, monkeypatch):
    root = service.runtime.root
    busy = {}

    def probe(stage):
        try:
            legacy.provision(service.runtime, "widget", "http://legacy-user:legacy-secret@proxy.example:8080")
        except DomainError as error:
            busy[stage] = error.code
    real_store, real_invoke, real_write = setup.store_credentials, service.runtime.application.invoke, setup.write_projection

    def store(*args, **kwargs):
        probe("credentials")
        return real_store(*args, **kwargs)

    def invoke(*args, **kwargs):
        probe("apply")
        return real_invoke(*args, **kwargs)

    def write(*args, **kwargs):
        probe("projection")
        return real_write(*args, **kwargs)
    monkeypatch.setattr(setup, "store_credentials", store)
    monkeypatch.setattr(service.runtime.application, "invoke", invoke)
    monkeypatch.setattr(setup, "write_projection", write)
    run(service, tmp_path)
    assert busy == {"credentials": "busy", "apply": "busy", "projection": "busy"}
    # The lock is free again afterwards.
    with legacy.provisioning_lock(root, service.fixture.business_id):
        pass


def test_state_is_read_again_under_the_lock_not_trusted_from_before_it(service, tmp_path, monkeypatch):
    inventory_path = snapshot_many(tmp_path, EXIT, OTHER_EXIT)
    real_lock = provision.provisioning_lock

    @contextlib.contextmanager
    def raced(root, business_id):
        # Another operator finishes a full, different provisioning after our early reads but before we hold the lock.
        monkeypatch.setattr(provision, "provisioning_lock", real_lock)
        provision.provision_isolated(service.runtime, "widget", "http://tenant-user:tenant-secret@" + OTHER_PROXY[7:],
                                     expected_exit_ip=OTHER_EXIT, inventory_path=inventory_path, now=NOW)
        with real_lock(root, business_id):
            yield
    monkeypatch.setattr(provision, "provisioning_lock", raced)
    with pytest.raises(DomainError, match="different isolated browser assignment"):
        provision.provision_isolated(service.runtime, "widget", URL, expected_exit_ip=EXIT, inventory_path=inventory_path, now=NOW)
    browser = saved(service)["config_json"]["browser"]
    assert (browser["proxy_server"], browser["expected_exit_ip"]) == (OTHER_PROXY, OTHER_EXIT)


def test_dry_run_and_check_create_no_lock_directory_or_file(service, tmp_path):
    root = service.runtime.root
    path = deployment_file(service, tmp_path)
    before = tree(root)
    run(service, tmp_path, dry_run=True)
    run(service, tmp_path, dry_run=True, deployment_path=path)
    provision.check_isolated(service.runtime, "widget", path)
    assert tree(root) == before and not (root / ".bazarlyk/secrets").exists() and not (service.fixture.hq / "browser").exists()
    # Even a refused (ineligible) real run stops before creating the lock.
    with pytest.raises(DomainError, match="not confirmed available"):
        run(service, tmp_path, inventory_path=snapshot(tmp_path, profile="unavailable"))
    assert tree(root) == before


# --- recovery: legacy -> isolated -> legacy through the canonical application path ---------------------------------

def recovery_request(service, prior_block):
    """The documented owner-API recovery request: the CURRENT full config and revision, with only `browser` replaced by
    the explicit operator-settable legacy values (no generation, path, reference or port inputs)."""
    row = saved(service)
    browser = {"mode": "legacy", "enabled": prior_block["enabled"], "proxy_server": prior_block["proxy_server"]}
    return {**row["config_json"], "browser": browser}, row["revision"]


def send_config(service, value, revision, key):
    from qorqut_operations.contracts import InvocationOptions
    _business_id, ctx = legacy.owner_context(service.runtime, "widget")
    return service.runtime.application.invoke(ctx, "business_config_update", {"config": value},
                                              InvocationOptions(expected_revision=revision, idempotency_key=key))


def test_legacy_to_isolated_and_back_restores_the_legacy_identity_with_a_new_generation(service, tmp_path, monkeypatch):
    from qorqut_operations.contracts import InvocationOptions, new_id
    monkeypatch.setattr(config, "port_available", lambda port: True)
    business_id = service.fixture.business_id
    accounts = {"reddit": {"main": {"username": "example_user"}}}
    with service.runtime.database.transaction() as uow:
        row = uow.repository("businesses").get(business_id)
        uow.repository("businesses").update(business_id, {"config_json": {**row["config_json"], "accounts": accounts, "stage": "live"}},
                                            expected_revision=row["revision"])
    legacy.provision(service.runtime, "widget", "http://legacy-user:legacy-secret@proxy.example:8080")
    old = saved(service)["config_json"]["browser"]
    assert "mode" not in old
    secrets_dir = service.runtime.root / ".bazarlyk/secrets/browser-proxy"
    legacy_file = secrets_dir / (business_id + ".json")
    legacy_bytes = legacy_file.read_bytes()
    profile = service.runtime.root / ".bazarlyk/browsers" / business_id / "profile"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("old persona")
    prior = tmp_path / "prior.json"
    run(service, tmp_path, prior_path=prior)
    isolated_block = saved(service)["config_json"]["browser"]
    assert isolated_block["mode"] == config.ISOLATED and isolated_block["expected_exit_ip"] == EXIT
    isolated_bytes = isolated_secret(service).read_bytes()
    kept = json.loads(prior.read_text())
    assert kept["browser"] == old  # exact evidence of the old block

    _bid, ctx = legacy.owner_context(service.runtime, "widget")
    current = saved(service)

    def attempt(browser, *, key=None, revision=None):
        return service.runtime.application.invoke(ctx, "business_config_update",
            {"config": {**saved(service)["config_json"], "browser": browser}},
            InvocationOptions(expected_revision=revision or saved(service)["revision"], idempotency_key=key or new_id()))
    # The saved old block is evidence, not a request: sent back unchanged or merged onto the isolated block it is refused
    # (its generated fields and generation are forbidden and a missing mode inherits the isolated one).
    for rejected in (kept["browser"], {**isolated_block, **kept["browser"]}, {**isolated_block, "mode": "legacy"}):
        refused = attempt(rejected)
        assert refused.status == "error" and refused.error["code"] == "validation_error"
    assert saved(service)["revision"] == current["revision"] and saved(service)["config_json"]["browser"] == isolated_block

    # One explicit attempt: current config and revision, only `browser` replaced by the operator-settable legacy values.
    key = new_id()
    value, revision = recovery_request(service, kept["browser"])
    result = send_config(service, value, revision, key)
    assert result.status == "ok"
    row = saved(service)
    restored = row["config_json"]["browser"]
    assert "mode" not in restored and "expected_exit_ip" not in restored and set(restored) == config.LEGACY_KEYS
    assert (restored["enabled"], restored["proxy_server"]) == (old["enabled"], old["proxy_server"])
    for field in ("cdp_port", "user_data_dir", "credentials_ref"):
        assert restored[field] == old[field]  # derived back to the original legacy profile, port and credential reference
    assert restored["generation"] not in {old["generation"], isolated_block["generation"]}  # generated afresh, not restored
    assert row["config_json"]["accounts"] == accounts and row["config_json"]["stage"] == "live"
    # An uncertain retry of the exact same request under the same key replays instead of applying twice ...
    applied = row["revision"]
    replay = send_config(service, value, revision, key)
    assert replay.status == "ok" and saved(service)["revision"] == applied and saved(service)["config_json"]["browser"] == restored
    # ... but a request rebuilt from the changed current state is a different attempt and must not reuse that key.
    rebuilt, current_revision = recovery_request(service, kept["browser"])
    reused = send_config(service, rebuilt, current_revision, key)
    assert reused.status == "error" and reused.error["code"] == "idempotency_conflict"
    assert saved(service)["revision"] == applied
    # Both profiles and both credentials survive; nothing was started or restored by copy.
    assert legacy_file.read_bytes() == legacy_bytes and isolated_secret(service).read_bytes() == isolated_bytes
    assert (profile / "Cookies").read_text() == "old persona"
    assert not (service.runtime.root / ".bazarlyk/browsers" / business_id / "ready.json").exists()

    # The round trip repeats: the same canonical provisioning path re-selects the isolated mode with another generation.
    again = run(service, tmp_path)
    final = saved(service)["config_json"]["browser"]
    assert final["mode"] == config.ISOLATED and final["expected_exit_ip"] == EXIT and again["already_assigned"] is False
    assert final["user_data_dir"] == isolated_block["user_data_dir"] and final["generation"] != isolated_block["generation"]
    assert isolated_secret(service).read_bytes() == isolated_bytes and legacy_file.read_bytes() == legacy_bytes
    assert saved(service)["config_json"]["accounts"] == accounts
    assert json.loads(prior.read_text()) == kept  # the first prior record is the only one that was ever kept
