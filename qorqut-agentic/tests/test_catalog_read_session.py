from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.conftest import ATTR, sha


def test_sessions_are_nested_thread_local_and_never_survive_the_call(email_layer):
    catalog = email_layer.catalog
    path = "plugins/email-composition/skills/compose/SKILL.md"
    before = catalog.list()["catalog_revision"]
    with catalog.read_session():
        assert catalog.list()["catalog_revision"] == before
        with catalog.read_session():
            old = (email_layer.root / path).read_bytes()
            email_layer.authoring.apply(ATTR, [{"op": "edit", "path": path,
                "expected_sha256": sha(old), "content": old.decode() + "\nUpdated instruction.\n"}])
            # A separate caller must observe the update, even while this caller has a snapshot.
            with ThreadPoolExecutor(max_workers=1) as pool:
                current = pool.submit(lambda: catalog.list()["catalog_revision"]).result(timeout=5)
            assert current != before
            assert catalog.list()["catalog_revision"] == before
    assert catalog.list()["catalog_revision"] == current
    with pytest.raises(RuntimeError):
        with catalog.read_session():
            raise RuntimeError("failed projection")
    assert catalog.read_entry("email-composition/compose")["body"].endswith("Updated instruction.\n")


def test_session_and_snapshot_nesting_isolation_and_exception_cleanup(email_layer, monkeypatch):
    from qorqut_agentic import catalog_api

    catalog = email_layer.catalog
    original = catalog_api.load_catalog
    calls = []
    def load(source):
        calls.append(1)
        return original(source)
    monkeypatch.setattr(catalog_api, 'load_catalog', load)
    with catalog.read_snapshot():
        with catalog.read_session():
            catalog.list()
        assert len(calls) == 1
    with catalog.read_session():
        outer = catalog._load()
        with pytest.raises(RuntimeError):
            with catalog.read_snapshot():
                assert catalog._load() is not outer
                with catalog.read_session():
                    assert catalog._load() is not outer
                raise RuntimeError('failed snapshot')
        assert catalog._load() is outer
    assert catalog._load() is not outer
    assert len(calls) == 4
    with pytest.raises(RuntimeError):
        with catalog.read_snapshot():
            with catalog.read_session():
                raise RuntimeError('failed session')
    assert getattr(catalog._reads, 'catalog', None) is None
    assert catalog._active_catalog() is None
