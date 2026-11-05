"""Selecting the Fastmail provider changes the operation set and nothing in the store (synthetic hosts, no engine contact)."""

from bazarlyk.extensions import NO_EXTENSIONS
from bazarlyk.service.runtime import build_runtime
from fake_engine import FakeEngine
from fastmail_host import selection
from host_foundation_support import build_instance


def schema(instance):
    with instance.database.read() as uow:
        return sorted((row["type"], row["name"], row["sql"]) for row in
                      uow.connection.execute("SELECT type,name,sql FROM sqlite_master"))


def test_a_store_prepared_with_the_provider_is_the_core_only_store(tmp_path):
    core, _ = build_instance(tmp_path / "core")
    _, chosen = selection(tmp_path, FakeEngine())
    selected, _ = build_instance(tmp_path / "selected", extensions=chosen)
    assert schema(selected) == schema(core)
    assert not any("mail" in name.lower() for _, name, _ in schema(selected))


def test_a_core_only_store_is_served_with_the_provider_and_back(tmp_path):
    core, _ = build_instance(tmp_path / "core", agentic_sources=("fastmail",))
    _, chosen = selection(tmp_path, FakeEngine())
    record = core.paths.instance_root / "config/agentic-layer.json"
    original = record.read_bytes()
    with_provider = build_runtime(core.workspace, paths=core.paths, database=core.database, extensions=chosen)
    assert with_provider.registry.known("fastmail_send") and set(with_provider.extensions) == {"fastmail"}
    assert "imported-fastmail-metadata" in with_provider.agentic_view.selection({})["package_ids"]
    before = schema(core)
    again = build_runtime(core.workspace, paths=core.paths, database=core.database, extensions=NO_EXTENSIONS)
    assert not again.registry.known("fastmail_send") and dict(again.extensions) == {}
    assert schema(core) == before                                   # serving never prepares or migrates a store
    assert "imported-fastmail-metadata" not in again.agentic_view.selection({})["package_ids"]
    assert record.read_bytes() == original                          # activation never rewrites the imported binding
