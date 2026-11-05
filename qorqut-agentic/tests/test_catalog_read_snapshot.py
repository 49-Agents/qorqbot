"""Request snapshots preserve authored consistency and remain isolated."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from qorqut_agentic import AgenticError, AgenticLayer
from qorqut_agentic import catalog_api
from tests.conftest import ATTR


@pytest.mark.parametrize('kind', ['file', 'symlink', 'reserved'])
def test_snapshot_detects_legacy_content_added_during_read(email_layer, kind):
    legacy = email_layer.root / 'packages' / 'empty'
    legacy.mkdir(parents=True)
    reader = email_layer.catalog
    with reader.read_snapshot():
        assert all(item['package_id'] != 'empty' for item in reader.list()['entries'])
    with pytest.raises(AgenticError):
        with reader.read_snapshot():
            reader.list()
            if kind == 'file':
                (legacy / 'agentic.yaml').write_text('id: hidden\n')
            elif kind == 'symlink':
                (legacy / 'link').symlink_to('missing')
            else:
                (legacy / '.git').mkdir()
    if kind == 'reserved':
        with pytest.raises(AgenticError):
            reader.list()
    else:
        listing = reader.list()
        assert any(issue['code'] == 'layout_migration_required' for issue in listing['issues'])
        assert all(item['package_id'] != 'empty' for item in listing['entries'])


def test_snapshot_matches_ordinary_reads_and_parses_once(email_layer, monkeypatch):
    reader = email_layer.catalog
    expected = (reader.list(), reader.read_entry('email-composition/compose'),
                reader.read_resource('email-composition', 'skills/compose/assets/checklist.txt'), reader.validate())
    assert reader.revision()['catalog_revision'] == expected[0]['catalog_revision']
    calls = []
    original = catalog_api.load_catalog
    def load(source):
        calls.append(1)
        return original(source)
    monkeypatch.setattr(catalog_api, 'load_catalog', load)
    assert reader.revision()['catalog_revision'] == expected[0]['catalog_revision']
    assert not calls
    with reader.read_snapshot() as revision:
        assert revision == expected[0]['catalog_revision'] == reader.revision()['catalog_revision']
        with reader.read_snapshot():
            actual = (reader.list(), reader.read_entry('email-composition/compose'),
                      reader.read_resource('email-composition', 'skills/compose/assets/checklist.txt'), reader.validate())
        assert actual == expected
        assert len(calls) == 1
    reader.list()
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['edit', 'remove', 'symlink'])
def test_direct_change_or_unsafe_replacement_rejects_snapshot_and_next_read_is_current(email_layer, change):
    reader = email_layer.catalog
    path = email_layer.root / 'plugins/email-composition/preferences/style.md'
    original = path.read_bytes()
    with pytest.raises(AgenticError) as failure:
        with reader.read_snapshot():
            reader.list()
            if change == 'edit': path.write_bytes(original + b'\nNew guidance.\n')
            elif change == 'remove': path.unlink()
            else:
                path.unlink()
                path.symlink_to('missing-target')
    assert failure.value.code == 'revision_conflict'
    if change == 'symlink': path.unlink()
    path.write_bytes(original + b'\nCurrent guidance.\n')
    assert 'Current guidance.' in reader.read_entry('email-composition/style')['body']


def test_snapshot_exception_cleanup_and_shared_lock_refuses_write(email_layer):
    reader = email_layer.catalog
    with pytest.raises(RuntimeError):
        with reader.read_snapshot():
            with pytest.raises(AgenticError) as error:
                email_layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'plugins/email-composition/new.md', 'content': 'new'}])
            assert error.value.code == 'state_conflict'
            raise RuntimeError('fixture')
    email_layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'plugins/email-composition/new.md', 'content': 'new'}])
    assert reader.read_resource('email-composition', 'new.md')['content'] == 'new'


@pytest.mark.parametrize("same_layer", [True, False])
def test_snapshot_isolated_across_threads_and_layer_objects(email_layer, monkeypatch, same_layer):
    other = AgenticLayer.open(email_layer.root, email_layer.state)
    barrier = Barrier(2)
    calls = []
    original = catalog_api.load_catalog
    def load(source):
        calls.append(1)
        return original(source)
    monkeypatch.setattr(catalog_api, 'load_catalog', load)
    def read(reader):
        with reader.read_snapshot():
            barrier.wait(timeout=10)
            return reader.list()['catalog_revision']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read, (email_layer.catalog, email_layer.catalog if same_layer else other.catalog)))
    assert results[0] == results[1]
    assert len(calls) == 2


def test_snapshot_enforces_resource_limit_and_detects_growth(email_layer):
    reader = email_layer.catalog
    name = 'skills/compose/assets/checklist.txt'
    path = email_layer.root / 'plugins/email-composition' / name
    with reader.read_snapshot():
        with pytest.raises(AgenticError) as error:
            reader.read_resource('email-composition', name, max_bytes=1)
        assert error.value.code == 'limit_exceeded'
    limit = len(path.read_bytes()) + 1
    with pytest.raises(AgenticError) as error:
        with reader.read_snapshot():
            path.write_bytes(b'x' * (limit + 1))
            reader.read_resource('email-composition', name, max_bytes=limit)
    assert error.value.code == 'revision_conflict'


def test_returned_nested_values_cannot_mutate_snapshot(full_layer):
    reader = full_layer.catalog
    ref = 'example-project-workflow/landing-page'
    with reader.read_snapshot():
        expected = reader.read_entry(ref)
        result = reader.read_entry(ref)
        result['definition']['steps'].clear()
        result['entry']['source']['note'] = 'mutated'
        listing = reader.list()
        listing['entries'][0]['source']['note'] = 'mutated'
        validation = reader.validate()
        package = next(item for item in validation['packages'] if item['id'] == 'example-project-workflow')
        package['source']['note'] = 'mutated'
        package['entry_ids'].clear()
        assert reader.read_entry(ref) == expected
        fresh = next(item for item in reader.validate()['packages'] if item['id'] == 'example-project-workflow')
        assert fresh['source']['note'] != 'mutated' and fresh['entry_ids']
