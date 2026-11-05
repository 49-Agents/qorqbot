import pytest

from bazarlyk.browser.service import _admitted_legacy_writer, serve
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.admission import InstallationRecoveryLock
from bazarlyk.sandbox.state import SandboxStateStore
from foundation_support import BUSINESS, OTHER, database_at


@pytest.mark.parametrize("state", ["running", "stopped"])
def test_selected_business_cannot_start_a_legacy_host_browser(tmp_path, monkeypatch, state):
    database = database_at(tmp_path)
    store = SandboxStateStore()
    with database.transaction() as uow:
        row = store.insert_intent(uow, BUSINESS)
        if state == "stopped":
            store.request_state(uow, BUSINESS, desired_state=state, expected_revision=row["revision"])
    def forbidden(*args, **kwargs):
        pytest.fail("Legacy host browser must refuse before proxy resolution or process launch")
    monkeypatch.setattr("bazarlyk.browser.service.upstream_url", forbidden)
    monkeypatch.setattr("bazarlyk.browser.service.subprocess.Popen", forbidden)
    with pytest.raises(DomainError, match="assigned to its sandbox"):
        serve(database, tmp_path, BUSINESS, tmp_path / "unused-runtime", "/unused-chrome")
    assert not (tmp_path / ".bazarlyk/browsers").exists()


def test_legacy_browser_rechecks_sandbox_intent_while_holding_selected_writer_lock(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    store = SandboxStateStore()

    def concurrent_adoption(*_args, **_kwargs):
        with database.transaction() as uow:
            store.insert_intent(uow, BUSINESS)
        return "http://proxy.invalid"

    def forbidden(*_args, **_kwargs):
        pytest.fail("Legacy host browser must not launch after concurrent sandbox adoption")

    monkeypatch.setattr("bazarlyk.browser.service.configured_target", lambda *_args: {
        "cdp_port": 19400, "user_data_dir": str(tmp_path / "profile"),
        "downloads_dir": str(tmp_path / "downloads"), "proxy_server": "http://proxy.invalid"})
    monkeypatch.setattr("bazarlyk.browser.service.upstream_url", concurrent_adoption)
    monkeypatch.setattr("bazarlyk.browser.service.subprocess.Popen", forbidden)
    with pytest.raises(DomainError, match="assigned to its sandbox"):
        serve(database, tmp_path, BUSINESS, tmp_path / "unused-runtime", "/unused-chrome")


def test_legacy_browser_cannot_start_between_adoption_inventory_and_durable_intent(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    store, upstream_calls = SandboxStateStore(), []
    monkeypatch.setattr("bazarlyk.browser.service.configured_target", lambda *_args: {
        "cdp_port": 19400, "user_data_dir": str(tmp_path / "profile"),
        "downloads_dir": str(tmp_path / "downloads"), "proxy_server": "http://proxy.invalid"})
    monkeypatch.setattr("bazarlyk.browser.service.upstream_url",
                        lambda *_args: upstream_calls.append(True) or "http://proxy.invalid")
    monkeypatch.setattr("bazarlyk.browser.service.subprocess.Popen",
                        lambda *_args, **_kwargs: pytest.fail("legacy Chrome must not launch during adoption EX"))
    # This is the exact adoption interval after selected legacy inventory has
    # observed no service.lock and before the first sandbox row is committed.
    with InstallationRecoveryLock(tmp_path).exclusive(timeout=5):
        with pytest.raises(DomainError, match="Writes are unavailable during recovery"):
            serve(database, tmp_path, BUSINESS, tmp_path / "unused-runtime", "/unused-chrome")
        with database.transaction() as uow:
            store.insert_intent(uow, BUSINESS)
    assert upstream_calls == []
    with database.read() as uow:
        assert store.get(uow, BUSINESS) is not None


def test_running_legacy_browser_keeps_only_its_profile_lock_not_installation_shared_admission(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    store = SandboxStateStore()
    monkeypatch.setattr("bazarlyk.browser.service.configured_target", lambda *_args: {
        "cdp_port": 19400, "user_data_dir": str(tmp_path / "profile"),
        "downloads_dir": str(tmp_path / "downloads"), "proxy_server": "http://proxy.invalid"})
    # This context represents a running legacy browser for BUSINESS: it retains
    # that exact service.lock, while a different business can acquire adoption
    # EX immediately rather than being blocked for Chrome's full lifetime.
    with _admitted_legacy_writer(database, tmp_path, BUSINESS):
        with InstallationRecoveryLock(tmp_path).exclusive(timeout=0):
            with database.transaction() as uow:
                store.insert_intent(uow, OTHER)
    assert (tmp_path / ".bazarlyk/browsers" / BUSINESS / "service.lock").exists()
    with database.read() as uow:
        assert store.get(uow, OTHER) is not None
