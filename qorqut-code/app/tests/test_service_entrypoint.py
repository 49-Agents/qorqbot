import subprocess
import sqlite3
from threading import Thread
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from bazarlyk.service.main import assemble, main
from bazarlyk.service.instance_arguments import instance_arguments
from bazarlyk.storage import Database
from test_agent_configuration import REPO, PATHS
from test_service_transports import OWNER_TOKEN, ServiceFixture


@pytest.mark.parametrize("maintenance", ["default", "disabled", "injected"])
def test_assembly_installs_real_handlers_without_mutating_or_starting_work(tmp_path, monkeypatch, maintenance):
    service = ServiceFixture(tmp_path)
    with service.fixture.db.read() as uow:
        before = list(uow.connection.iterdump())

    def refused(*args, **kwargs):
        raise AssertionError("Assembly must not start a process, thread, model or external request")

    monkeypatch.setattr(subprocess, "Popen", refused)
    monkeypatch.setattr(Thread, "start", refused)
    injected = SimpleNamespace(run=refused)
    options = {} if maintenance == "default" else {"nightly_maintenance": None if maintenance == "disabled" else injected}
    app = assemble(service.fixture.root, REPO, paths=PATHS, origins=["http://testserver"], endpoint="http://testserver/mcp",
        bootstrap_token=OWNER_TOKEN, database=service.fixture.db, signing_key=b"fixed-key" * 8, model_client_factory=refused,
        **options)
    assert not hasattr(app.state.runtime, "provider_tokens")
    assert app.state.runtime.registry.get("signoffs_platform_submit").prepare
    assert app.state.runtime.registry.get("signoffs_get").read
    assert callable(app.state.runtime.signoffs.review)
    # Owner sessions read the same shared published Context text as managed agents, and no orientation provider is bound.
    runtime = app.state.runtime
    assert runtime.owner_bootstrap.context_home == runtime.resolver.context_home
    assert runtime.resolver.context_home.__self__.reader is runtime.domains.shared_context
    assert runtime.owner_bootstrap.context_status.__self__ is runtime.resolver.context_home.__self__
    assert not hasattr(runtime.resolver, "context_orientation")
    assert app.state.runtime.registry.get("routines_create").prepare
    assert not app.state.runtime.registry.known("agent_compact")
    assert app.state.runtime.background._thread is None
    assert app.state.runtime.sandbox.worker._thread is None
    programs = app.state.runtime.background.programs
    assert programs.get("agent.compact") is None
    assert bool(programs.get("maintenance.nightly")) is (maintenance != "disabled")
    if maintenance != "disabled":
        program = programs.require("maintenance.nightly")
        assert program.scope_kinds == ("global",)
        if maintenance == "default":
            assert program.run.__self__.git_enabled is True
        else:
            assert program.run is refused
    assert not (service.fixture.root / ".bazarlyk/backups").exists()
    with service.fixture.db.read() as uow:
        assert list(uow.connection.iterdump()) == before


def test_dashboard_needs_no_token_and_strips_obsolete_query_tokens(tmp_path):
    service = ServiceFixture(tmp_path)
    with TestClient(service.web) as client:
        first = client.get("/?token=" + OWNER_TOKEN, follow_redirects=False)
        assert first.status_code == 303
        assert first.headers["location"] == "/"
        assert "set-cookie" not in first.headers
        assert not client.cookies
        assert client.get("/api/session").status_code == 200
        assert client.get("/api/session?token=" + OWNER_TOKEN).status_code == 400
        assert client.get("/?token=wrong", follow_redirects=False).status_code == 303
        assert client.get("/").status_code == 200


def test_entrypoint_requires_explicit_registered_authority(tmp_path):
    assert main(["--root", str(tmp_path), "--workspace", str(REPO), *instance_arguments(PATHS)]) == 1
    assert not (tmp_path / ".bazarlyk").exists()


def test_main_opens_existing_database_before_business_selection_without_migration(tmp_path, monkeypatch):
    import uvicorn
    service = ServiceFixture(tmp_path)
    before = service.fixture.db.path.read_bytes()
    served = []

    def refused(*args, **kwargs):
        raise AssertionError("Service startup must not initialize storage or launch a subprocess")

    def serve(app, **options):
        app.state.runtime.require_authority()
        assert app.state.http_api.terminals.workspace == tmp_path / "qorqut-terminal"
        with app.state.runtime.database.read() as uow:
            assert uow.repository("installation").get("service")["owner_principal_id"] == service.fixture.owner.id
        assert options["port"] == 13377
        assert not hasattr(app.state.runtime, "provider_tokens")
        served.append(app)

    monkeypatch.setattr(Database, "initialize", refused)
    monkeypatch.setattr(subprocess, "Popen", refused)
    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setenv("QORQUT_TERMINAL_WORKSPACE", str(tmp_path / "qorqut-terminal"))
    assert main(["--root", str(service.fixture.root), "--workspace", str(REPO), *instance_arguments(PATHS),
                 "--biz", str(service.fixture.hq.parent), "--port", "13377"]) == 0
    assert len(served) == 1
    assert service.fixture.db.path.read_bytes() == before
    cold = assemble(service.fixture.root, REPO, paths=PATHS, origins=["http://testserver"], endpoint="http://testserver/mcp")
    cold.state.runtime.require_authority()
    assert service.fixture.db.path.read_bytes() == before


@pytest.mark.parametrize("damage", ["older_schema", "foreign_application"])
def test_main_rejects_unreviewed_database_without_upgrading_it(tmp_path, monkeypatch, damage):
    import uvicorn
    service = ServiceFixture(tmp_path)
    with sqlite3.connect(service.fixture.db.path) as connection:
        if damage == "older_schema":
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.execute("DELETE FROM schema_migrations WHERE version=?", (version,))
            connection.execute(f"PRAGMA user_version={version - 1}")
        else:
            connection.execute("PRAGMA application_id=123")
    before = service.fixture.db.path.read_bytes()

    def refused(*args, **kwargs):
        raise AssertionError("Rejected authority must not migrate or serve")

    monkeypatch.setattr(Database, "initialize", refused)
    monkeypatch.setattr(uvicorn, "run", refused)
    assert main(["--root", str(service.fixture.root), "--workspace", str(REPO), *instance_arguments(PATHS)]) == 1
    assert service.fixture.db.path.read_bytes() == before
