"""Inventory time slices and overlapping transport retries, with synthetic stores."""
from types import SimpleNamespace
import pytest
from qorqut_operations.contracts import DomainError
from test_overseer_evidence_flow import OperationalHost, Access, Coverage
from bazarlyk.overseer.evidence import EvidenceProvider
from bazarlyk.overseer.operations import OverseerOperations


def test_time_slice_resumes_without_skipping_sources_and_drains_source_continuation(tmp_path, monkeypatch):
    host = OperationalHost(tmp_path / 'host')
    root = tmp_path / 'archive'
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={'overseer'}, coverage=Coverage())
    keys = [(['a'], {}, 'task', 'a'), (['b'], {}, 'task', 'b')]
    captured = []
    selections = []

    def candidates(selection, uow, *, after, limit):
        return [key for key in keys if after is None or key[0] > after][:limit + 1]

    def capture(selection, namespace, kind, identity, uow):
        selections.append(selection)
        captured.append(identity)
        # One source's own continuation survives an entire inventory sweep.
        if identity == 'a':
            provider.journal.set_continuation(selection, 'notes', 'next-page' if captured.count('a') == 1 else None)
        return [], []

    monkeypatch.setattr(provider.scanner, 'keys', candidates)
    monkeypatch.setattr(provider.scanner, 'capture', capture)
    monkeypatch.setattr(provider.scanner, 'expired', lambda: True)
    selection = {'mode': 'all_authorized'}
    one = provider.list_changes(selection)
    assert captured == ['a'] and one['next_cursor']
    two = provider.list_changes(selection, cursor=one['next_cursor'])
    assert captured == ['a', 'b'] and two['next_cursor'] and not two['complete']
    three = provider.list_changes(selection, cursor=two['next_cursor'])
    four = provider.list_changes(selection, cursor=three['next_cursor'])
    assert captured == ['a', 'b', 'a', 'b']
    assert four['complete'] and four['next_cursor'] is None


@pytest.mark.parametrize('name', ['overseer_evidence_list', 'overseer_episode_get'])
def test_overlapping_scan_refuses_without_advancing_and_releases_after_failure(monkeypatch, name):
    import bazarlyk.overseer.operations as module
    ops = OverseerOperations(SimpleNamespace(agentic_layer=object()))
    monkeypatch.setattr(module, 'current_overseer', lambda *a, **k: ('ctx', None))
    calls = []
    def delivery(*args):
        calls.append(args[2])
        raise DomainError('evidence_unavailable', 'synthetic')
    monkeypatch.setattr(ops, '_evidence_delivery', delivery)
    request = {'cursor': 'same-opaque-cursor'}
    ops._evidence_scan_lock.acquire()
    try:
        with pytest.raises(DomainError) as busy:
            ops.read(name, None, request, None)
        assert busy.value.code == 'evidence_busy' and not calls
    finally:
        ops._evidence_scan_lock.release()
    with pytest.raises(DomainError) as failed:
        ops.read(name, None, request, None)
    assert failed.value.code == 'evidence_unavailable' and calls == [request]
    assert ops._evidence_scan_lock.acquire(blocking=False)
    ops._evidence_scan_lock.release()
    monkeypatch.setattr(ops, '_evidence_delivery', lambda *args: {'page': 'retained'})
    assert ops.read(name, None, {}, None) == {'page': 'retained'}
    assert ops._evidence_scan_lock.acquire(blocking=False)
    ops._evidence_scan_lock.release()
