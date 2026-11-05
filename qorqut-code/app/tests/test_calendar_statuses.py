"""Focused checks of the owner's personal calendar-event status labels (store and owner-only HTTP route).

Store checks use temporary directories only. Route and identity checks use the empty temporary v1.2 host of
``calendar_support`` with synthetic registrations; Google and Fastmail are never contacted.
"""

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from bazarlyk.calendar import preferences
from bazarlyk.calendar.preferences import MAX_ITEMS, read_statuses, save_status, statuses
from host_foundation_support import register_sources, tree
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id

OWNER = '11111111-1111-4111-8111-111111111111'
PUBLIC = {'connection_id', 'calendar_id', 'event_id', 'occurrence_start', 'status', 'revision'}
ORIGIN = {'Origin': 'http://testserver'}
BASE = {'connection_id': 'personal', 'calendar_id': 'primary', 'event_id': 'evt-1', 'occurrence_start': '',
        'status': 'completed', 'expected_revision': 0, 'request_id': 'request-1'}


def body(**changes):
    return {**BASE, 'request_id': new_id(), **changes}


def store_file(directory, owner=OWNER):
    return directory / 'dashboard-preferences' / (hashlib.sha256(owner.encode()).hexdigest() + '.calendar-statuses.json')


def code(call, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        call(*args, **kwargs)
    return caught.value


# --- identity, statuses, restoration -------------------------------------------------------------------------------

def test_missing_store_is_empty_without_creating_anything(tmp_path):
    assert read_statuses(tmp_path, OWNER) == []
    assert tree(tmp_path) == []


def test_all_four_statuses_restore_and_reopen_with_increasing_revisions(tmp_path):
    expected = 0
    for revision, status in enumerate(('completed', 'cancelled', 'rescheduled', 'pending', 'completed', 'pending'), 1):
        item = save_status(tmp_path, OWNER, body(status=status, expected_revision=expected))
        assert item == {**{k: BASE[k] for k in PUBLIC - {'status', 'revision'}}, 'status': status, 'revision': revision}
        expected = revision
        # A fresh read (a refreshed or reopened dashboard) sees exactly the saved label; Pending stays a saved revision.
        assert read_statuses(tmp_path, OWNER) == [item]
    assert read_statuses(tmp_path, OWNER)[0]['status'] == 'pending'


def test_target_identity_separates_connection_calendar_event_and_occurrence(tmp_path):
    targets = [
        {},                                                    # the nonrecurring event
        {'occurrence_start': '2026-10-15T14:00:00.000Z'},      # one occurrence of the same event ID
        {'occurrence_start': '2026-10-22T14:00:00.000Z'},      # another day's occurrence
        {'occurrence_start': '2026-10-15'},                    # an all-day occurrence
        {'calendar_id': 'other'}, {'connection_id': 'business'}, {'event_id': 'evt-2'},
    ]
    for number, target in enumerate(targets, 1):
        assert save_status(tmp_path, OWNER, body(status='cancelled', **target))['revision'] == 1
        assert len(read_statuses(tmp_path, OWNER)) == number
    # Changing one occurrence leaves the series, the other days and the other calendars alone.
    series = body(occurrence_start='2026-10-15T14:00:00.000Z', expected_revision=1, status='completed')
    assert save_status(tmp_path, OWNER, series)['status'] == 'completed'
    by_target = {(i['connection_id'], i['calendar_id'], i['event_id'], i['occurrence_start']): i for i in read_statuses(tmp_path, OWNER)}
    assert by_target[('personal', 'primary', 'evt-1', '2026-10-15T14:00:00.000Z')]['status'] == 'completed'
    assert all(i['status'] == 'cancelled' for key, i in by_target.items() if key != ('personal', 'primary', 'evt-1', '2026-10-15T14:00:00.000Z'))


@pytest.mark.parametrize('given,canonical', [
    ('', ''), ('2026-10-15', '2026-10-15'),
    ('2026-10-15T14:00:00.000Z', '2026-10-15T14:00:00.000Z'), ('2026-10-15T14:00:00Z', '2026-10-15T14:00:00.000Z'),
    ('2026-10-15T10:00:00-04:00', '2026-10-15T14:00:00.000Z'), ('2026-10-15T16:30:00+02:30', '2026-10-15T14:00:00.000Z'),
    ('2026-10-15T14:00:00.5+00:00', '2026-10-15T14:00:00.500Z'), ('2026-10-15T14:00Z', '2026-10-15T14:00:00.000Z'),
    ('2026-10-16T01:00:00+11:00', '2026-10-15T14:00:00.000Z'), ('2026-10-15T10:00:00+05:30', '2026-10-15T04:30:00.000Z'),
    ('2026-10-15T10:00:00-09:30', '2026-10-15T19:30:00.000Z'), ('2026-10-15T10:00:00+23:59', '2026-10-14T10:01:00.000Z'),
    ('2026-10-15T10:00:00-23:59', '2026-10-16T09:59:00.000Z'), ('2026-10-15T10:00:00+00:00', '2026-10-15T10:00:00.000Z')])
def test_occurrence_start_is_canonical_utc_milliseconds_or_exact_date(tmp_path, given, canonical):
    item = save_status(tmp_path, OWNER, body(occurrence_start=given))
    assert item['occurrence_start'] == canonical
    # Equivalent spellings are one target.
    again = save_status(tmp_path, OWNER, body(occurrence_start=canonical, expected_revision=1, status='cancelled'))
    assert again['revision'] == 2 and len(read_statuses(tmp_path, OWNER)) == 1


def test_identifiers_are_opaque_text_up_to_the_limit(tmp_path):
    odd = 'evént/ ☃ ?&=%00 \U0001f600'
    item = save_status(tmp_path, OWNER, body(connection_id='c' * 2000, calendar_id=odd, event_id=odd + 'x' * 1900))
    assert read_statuses(tmp_path, OWNER) == [item]
    assert (stat.S_IMODE(store_file(tmp_path).stat().st_mode)) == 0o600
    assert OWNER not in store_file(tmp_path).name


def test_store_functions_refuse_a_missing_owner_identity(tmp_path):
    for owner in ('', None, 7):
        assert code(read_statuses, tmp_path, owner).code == 'forbidden'
        assert code(save_status, tmp_path, owner, body()).code == 'forbidden'
    assert tree(tmp_path) == []


def test_each_owner_has_a_separate_store(tmp_path):
    save_status(tmp_path, OWNER, body())
    assert read_statuses(tmp_path, 'another-owner') == []
    assert save_status(tmp_path, 'another-owner', body())['revision'] == 1
    assert store_file(tmp_path, 'another-owner') != store_file(tmp_path)


# --- persistence, atomicity and durability ------------------------------------------------------------------------

def test_saved_store_survives_a_new_process_and_is_private(tmp_path):
    item = save_status(tmp_path, OWNER, body(status='rescheduled'))
    script = 'import sys, json; from bazarlyk.calendar.preferences import read_statuses; print(json.dumps(read_statuses(sys.argv[1], sys.argv[2])))'
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), OWNER], check=True, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONPATH': os.pathsep.join(p for p in sys.path if p)})
    assert json.loads(result.stdout) == [item]
    assert stat.S_IMODE(store_file(tmp_path).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / 'dashboard-preferences').stat().st_mode) & 0o077 == 0
    assert not [name for name in os.listdir(tmp_path / 'dashboard-preferences') if name.startswith('.')]


def test_save_flushes_the_file_and_the_directory_before_returning(tmp_path, monkeypatch):
    real, kinds = os.fsync, []
    def recording(descriptor):
        kinds.append('directory' if stat.S_ISDIR(os.fstat(descriptor).st_mode) else 'file')
        return real(descriptor)
    monkeypatch.setattr(os, 'fsync', recording)
    save_status(tmp_path, OWNER, body())
    assert kinds == ['file', 'directory']


def test_directory_flush_failure_after_the_swap_is_reported_as_uncertain_and_replays_without_duplicating(tmp_path, monkeypatch):
    real = os.fsync
    def failing(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(5, 'Input/output error')
        return real(descriptor)
    request = body(status='completed')
    monkeypatch.setattr(os, 'fsync', failing)
    error = code(save_status, tmp_path, OWNER, request)
    monkeypatch.undo()
    # The swap had already happened: the error must not claim the save was undone, and must tell the client how to settle it.
    assert error.code == 'operation_failed' and error.retryable
    assert 'nothing was changed' not in error.message and 'may have been saved' in error.message and 'same request ID' in error.message
    saved = {k: BASE[k] for k in PUBLIC - {'status', 'revision'}} | {'status': 'completed', 'revision': 1}
    assert read_statuses(tmp_path, OWNER) == [saved]
    assert not [name for name in os.listdir(tmp_path / 'dashboard-preferences') if name.startswith('.')]
    # The identical retry replays the stored item instead of applying again, and the bookkeeping survived.
    stored = store_file(tmp_path).read_bytes()
    assert save_status(tmp_path, OWNER, request) == saved
    assert store_file(tmp_path).read_bytes() == stored and read_statuses(tmp_path, OWNER) == [saved]
    assert code(save_status, tmp_path, OWNER, {**request, 'status': 'cancelled'}).code == 'idempotency_conflict'
    assert save_status(tmp_path, OWNER, body(status='cancelled', expected_revision=1))['revision'] == 2


def test_failed_replacement_leaves_the_previous_store_and_no_temporary_file(tmp_path, monkeypatch):
    first = save_status(tmp_path, OWNER, body())
    before = store_file(tmp_path).read_bytes()
    def broken(*args, **kwargs):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(os, 'replace', broken)
    error = code(save_status, tmp_path, OWNER, body(status='cancelled', expected_revision=1))
    assert error.code == 'operation_failed' and 'nothing was changed' in error.message
    monkeypatch.undo()
    assert store_file(tmp_path).read_bytes() == before
    assert read_statuses(tmp_path, OWNER) == [first]
    assert not [name for name in os.listdir(tmp_path / 'dashboard-preferences') if name.startswith('.')]


def test_leftover_temporary_file_of_a_crashed_writer_is_swept_on_the_next_save(tmp_path):
    save_status(tmp_path, OWNER, body())
    leftover = tmp_path / 'dashboard-preferences' / ('.' + hashlib.sha256(OWNER.encode()).hexdigest() + '.calendar-statuses-dead')
    leftover.write_text('partial')
    save_status(tmp_path, OWNER, body(event_id='evt-2'))
    assert not leftover.exists()


def test_independent_concurrent_saves_in_threads_all_persist(tmp_path):
    with ThreadPoolExecutor(16) as pool:
        saved = list(pool.map(lambda n: save_status(tmp_path, OWNER, body(event_id=f'evt-{n}')), range(48)))
    assert all(item['revision'] == 1 for item in saved)
    assert {i['event_id'] for i in read_statuses(tmp_path, OWNER)} == {f'evt-{n}' for n in range(48)}


def test_independent_concurrent_saves_from_separate_processes_all_persist(tmp_path):
    script = ('import sys\nfrom bazarlyk.calendar.preferences import save_status\n'
              'for n in range(8):\n'
              "    save_status(sys.argv[1], sys.argv[2], {'connection_id': 'c', 'calendar_id': 'k', 'event_id': f'{sys.argv[3]}-{n}',"
              " 'occurrence_start': '', 'status': 'completed', 'expected_revision': 0, 'request_id': f'{sys.argv[3]}-{n}'})\n")
    environment = {**os.environ, 'PYTHONPATH': os.pathsep.join(p for p in sys.path if p)}
    workers = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path), OWNER, f'p{n}'], env=environment,
                                stderr=subprocess.PIPE, text=True) for n in range(4)]
    for worker in workers:
        _, error = worker.communicate(timeout=120)
        assert worker.returncode == 0, error
    assert len(read_statuses(tmp_path, OWNER)) == 32


def test_concurrent_saves_of_one_target_let_exactly_one_win(tmp_path):
    def attempt(_):
        try:
            return save_status(tmp_path, OWNER, body(status='completed'))
        except DomainError as error:
            return error
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(attempt, range(12)))
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, DomainError)]
    assert len(winners) == 1 and winners[0]['revision'] == 1
    assert len(losers) == 11 and {e.code for e in losers} == {'revision_conflict'} and {e.current_revision for e in losers} == {1}
    assert read_statuses(tmp_path, OWNER) == winners


def test_a_stuck_lock_holder_becomes_a_retryable_busy_error(tmp_path, monkeypatch):
    save_status(tmp_path, OWNER, body())
    monkeypatch.setattr(preferences, 'LOCK_SECONDS', 0.05)
    lock = tmp_path / 'dashboard-preferences' / (hashlib.sha256(OWNER.encode()).hexdigest() + '.calendar-statuses.lock')
    with open(lock, 'a') as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        error = code(save_status, tmp_path, OWNER, body(event_id='evt-2'))
        assert error.code == 'busy' and error.retryable
        assert len(read_statuses(tmp_path, OWNER)) == 1       # reads never wait for the writer lock


# --- revisions, idempotency ----------------------------------------------------------------------------------------

def test_stale_revision_is_rejected_and_never_overwrites_a_newer_edit(tmp_path):
    first = save_status(tmp_path, OWNER, body(status='completed'))
    newer = save_status(tmp_path, OWNER, body(status='cancelled', expected_revision=1))
    for stale in (0, 1):
        error = code(save_status, tmp_path, OWNER, body(status='pending', expected_revision=stale))
        assert error.code == 'revision_conflict' and error.current_revision == 2
    assert code(save_status, tmp_path, OWNER, body(expected_revision=3)).current_revision == 2     # a future revision is no better
    assert (first['revision'], newer['revision']) == (1, 2)
    assert read_statuses(tmp_path, OWNER) == [newer]
    # A first save for an unseen target must declare revision 0.
    assert code(save_status, tmp_path, OWNER, body(event_id='new', expected_revision=1)).current_revision == 0


def test_retry_with_the_same_request_replays_the_saved_item_without_a_second_change(tmp_path):
    request = body(status='completed')
    first = save_status(tmp_path, OWNER, request)
    assert save_status(tmp_path, OWNER, request) == first == save_status(tmp_path, OWNER, dict(request))
    assert first['revision'] == 1 and read_statuses(tmp_path, OWNER) == [first]
    # The same instant spelled differently is the same original request.
    spelled = body(occurrence_start='2026-10-15T10:00:00-04:00', request_id='spelled')
    saved = save_status(tmp_path, OWNER, spelled)
    assert save_status(tmp_path, OWNER, {**spelled, 'occurrence_start': '2026-10-15T14:00:00.000Z'}) == saved


def test_reused_request_id_with_changed_content_conflicts_and_changes_nothing(tmp_path):
    request = body(status='completed')
    first = save_status(tmp_path, OWNER, request)
    before = store_file(tmp_path).read_bytes()
    for changed in ({'status': 'cancelled'}, {'event_id': 'evt-2'}, {'expected_revision': 1}, {'occurrence_start': '2026-10-15'},
                    {'connection_id': 'business'}, {'calendar_id': 'other'}):
        assert code(save_status, tmp_path, OWNER, {**request, **changed}).code == 'idempotency_conflict'
    assert store_file(tmp_path).read_bytes() == before and read_statuses(tmp_path, OWNER) == [first]


def test_replay_after_a_superseding_change_conflicts_instead_of_reapplying(tmp_path):
    request = body(status='completed')
    save_status(tmp_path, OWNER, request)
    newer = save_status(tmp_path, OWNER, body(status='cancelled', expected_revision=1))
    assert code(save_status, tmp_path, OWNER, request).code == 'revision_conflict'
    assert read_statuses(tmp_path, OWNER) == [newer]


# --- invalid input -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('changes,field', [
    ({'status': 'done'}, 'status'), ({'status': 'Completed'}, 'status'), ({'status': None}, 'status'), ({'status': ['completed']}, 'status'),
    ({'occurrence_start': '2026-10-15T14:00:00'}, 'occurrence_start'),        # naive
    ({'occurrence_start': '2026-10-15T14:00:00.123456Z'}, 'occurrence_start'),
    ({'occurrence_start': '2026-13-01'}, 'occurrence_start'), ({'occurrence_start': '2026-02-30'}, 'occurrence_start'),
    ({'occurrence_start': '20261015'}, 'occurrence_start'), ({'occurrence_start': 'tomorrow'}, 'occurrence_start'),
    ({'occurrence_start': '2026-10-15t14:00:00z'}, 'occurrence_start'), ({'occurrence_start': '2026-10-15 14:00:00Z'}, 'occurrence_start'),
    ({'occurrence_start': '2026-10-15T25:00:00Z'}, 'occurrence_start'), ({'occurrence_start': '2026-10-15T14:00:00+24:00'}, 'occurrence_start'),
    ({'occurrence_start': '2026-10-15T10:00:00+05:99'}, 'occurrence_start'), ({'occurrence_start': '2026-10-15T10:00:00-05:60'}, 'occurrence_start'),
    ({'occurrence_start': '2026-10-15T10:00:00+00:60'}, 'occurrence_start'), ({'occurrence_start': '2026-10-15T10:00:00+99:00'}, 'occurrence_start'),
    ({'occurrence_start': '2026-10-15T10:00:00-24:00'}, 'occurrence_start'), ({'occurrence_start': '2026-10-15T10:00:00+0530'}, 'occurrence_start'),
    ({'occurrence_start': '٢٠٢٦-10-15'}, 'occurrence_start'), ({'occurrence_start': None}, 'occurrence_start'),
    ({'occurrence_start': 20261015}, 'occurrence_start'), ({'occurrence_start': '0001-01-01T00:00:00+01:00'}, 'occurrence_start'),
    ({'expected_revision': -1}, 'expected_revision'), ({'expected_revision': True}, 'expected_revision'),
    ({'expected_revision': 1.0}, 'expected_revision'), ({'expected_revision': '0'}, 'expected_revision'),
    ({'expected_revision': None}, 'expected_revision'),
    ({'connection_id': ''}, 'connection_id'), ({'calendar_id': ''}, 'calendar_id'), ({'event_id': ''}, 'event_id'),
    ({'connection_id': 'c' * 2001}, 'connection_id'), ({'calendar_id': 'c' * 2001}, 'calendar_id'), ({'event_id': 'e' * 2001}, 'event_id'),
    ({'event_id': 7}, 'event_id'), ({'event_id': None}, 'event_id'), ({'event_id': '\ud800'}, 'event_id'),
    ({'request_id': ''}, 'request_id'), ({'request_id': 'r' * 201}, 'request_id'), ({'request_id': 7}, 'request_id'),
    ({'owner_id': OWNER}, 'owner_id'), ({'extra': 1}, 'extra'), ({'revision': 1}, 'revision')])
def test_invalid_requests_are_rejected_before_anything_is_written(tmp_path, changes, field):
    error = code(save_status, tmp_path, OWNER, body(**changes))
    assert error.code == 'validation_error' and field in error.field_errors
    assert tree(tmp_path) == []


@pytest.mark.parametrize('missing', sorted(preferences.REQUEST_FIELDS))
def test_every_field_is_required(tmp_path, missing):
    request = body()
    del request[missing]
    error = code(save_status, tmp_path, OWNER, request)
    assert error.code == 'validation_error' and error.field_errors[missing] == 'required'
    assert tree(tmp_path) == []


@pytest.mark.parametrize('given', [None, [], 'completed', 7])
def test_request_must_be_an_object(tmp_path, given):
    assert code(save_status, tmp_path, OWNER, given).code == 'validation_error'
    assert tree(tmp_path) == []


# --- bounded store -------------------------------------------------------------------------------------------------

def write_store(directory, count, owner=OWNER):
    items = {}
    for number in range(count):
        item = {'connection_id': 'c', 'calendar_id': 'k', 'event_id': f'e{number}', 'occurrence_start': '', 'status': 'completed',
                'revision': 1, 'request_id': f'r{number}', 'request_hash': '0' * 64}
        items[preferences._key(item)] = item
    path = store_file(directory, owner)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({'version': 1, 'items': items}))
    path.chmod(0o600)
    return items


def test_full_store_rejects_new_entries_but_still_allows_updates_without_eviction(tmp_path):
    assert MAX_ITEMS == 10000
    write_store(tmp_path, MAX_ITEMS)
    error = code(save_status, tmp_path, OWNER, body(event_id='one-too-many'))
    assert error.code == 'validation_error' and '10000' in error.message
    updated = save_status(tmp_path, OWNER, body(event_id='e17', connection_id='c', calendar_id='k', status='cancelled', expected_revision=1))
    assert updated['revision'] == 2
    items = read_statuses(tmp_path, OWNER)
    assert len(items) == MAX_ITEMS and {i['event_id'] for i in items} >= {'e0', 'e9999', 'e17'}
    assert not [i for i in items if i['event_id'] == 'one-too-many']
    assert code(save_status, tmp_path, OWNER, body(event_id='one-too-many')).code == 'validation_error'


def test_store_with_more_than_the_limit_is_unreadable_not_truncated(tmp_path):
    write_store(tmp_path, MAX_ITEMS + 1)
    before = store_file(tmp_path).read_bytes()
    assert code(read_statuses, tmp_path, OWNER).code == 'operation_failed'
    assert code(save_status, tmp_path, OWNER, body()).code == 'operation_failed'
    assert store_file(tmp_path).read_bytes() == before


# --- corruption is explicit and never overwritten -----------------------------------------------------------------

def valid_item(**changes):
    item = {'connection_id': 'c', 'calendar_id': 'k', 'event_id': 'e', 'occurrence_start': '', 'status': 'completed',
            'revision': 1, 'request_id': 'r', 'request_hash': '0' * 64, **changes}
    return preferences._key(item), item


def corrupt_cases():
    key, item = valid_item()
    other_key, _ = valid_item(event_id='different')
    document = lambda **changes: json.dumps({'version': 1, 'items': {key: item}, **changes})
    yield 'empty', b''
    yield 'garbage', b'not json at all'
    yield 'truncated', document().encode()[:-9]
    yield 'wrong version', json.dumps({'version': 2, 'items': {}}).encode()
    yield 'boolean version', json.dumps({'version': True, 'items': {}}).encode()
    yield 'items list', json.dumps({'version': 1, 'items': []}).encode()
    yield 'extra top-level field', document(extra=1).encode()
    yield 'duplicate keys', b'{"version":1,"version":1,"items":{}}'
    yield 'nonfinite number', b'{"version":1,"items":{},"n":NaN}'
    yield 'key mismatch', json.dumps({'version': 1, 'items': {other_key: item}}).encode()
    yield 'unknown status', json.dumps({'version': 1, 'items': {valid_item(status='done')[0]: valid_item(status='done')[1]}}).encode()
    yield 'zero revision', json.dumps({'version': 1, 'items': {valid_item(revision=0)[0]: valid_item(revision=0)[1]}}).encode()
    yield 'extra item field', json.dumps({'version': 1, 'items': {key: {**item, 'note': 'x'}}}).encode()
    yield 'noncanonical occurrence', json.dumps({'version': 1, 'items': {valid_item(occurrence_start='2026-10-15T10:00:00-04:00')[0]:
        valid_item(occurrence_start='2026-10-15T10:00:00-04:00')[1]}}).encode()
    yield 'bad request hash', json.dumps({'version': 1, 'items': {valid_item(request_hash='x')[0]: valid_item(request_hash='x')[1]}}).encode()


@pytest.mark.parametrize('name,content', list(corrupt_cases()), ids=[name for name, _ in corrupt_cases()])
def test_corrupt_store_is_an_explicit_error_and_is_never_replaced(tmp_path, name, content):
    path = store_file(tmp_path)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes(content)
    path.chmod(0o600)
    for call in (lambda: read_statuses(tmp_path, OWNER), lambda: save_status(tmp_path, OWNER, body())):
        error = code(call)
        assert error.code == 'operation_failed' and 'nothing was changed' in error.message
    assert path.read_bytes() == content
    assert not [n for n in os.listdir(path.parent) if n.startswith('.')]


def test_unsafe_or_unusual_store_files_are_refused_not_treated_as_empty(tmp_path):
    save_status(tmp_path, OWNER, body())
    path = store_file(tmp_path)
    content = path.read_bytes()
    path.chmod(0o644)                                           # readable by others: not a private file
    assert code(read_statuses, tmp_path, OWNER).code == 'operation_failed'
    assert code(save_status, tmp_path, OWNER, body(event_id='evt-2')).code == 'operation_failed'
    assert path.read_bytes() == content
    path.chmod(0o600)
    moved = path.with_name('moved')
    path.rename(moved)
    path.symlink_to(moved)                                      # a link is never followed
    assert code(read_statuses, tmp_path, OWNER).code == 'operation_failed'
    path.unlink()
    path.mkdir()                                                # not a regular file
    assert code(read_statuses, tmp_path, OWNER).code == 'operation_failed'
    assert code(save_status, tmp_path, OWNER, body(event_id='evt-2')).code == 'operation_failed'
    path.rmdir()
    moved.rename(path)
    assert len(read_statuses(tmp_path, OWNER)) == 1             # repaired by the operator: the old data is intact


def test_unreadable_store_is_not_replaced(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root ignores file permissions')
    save_status(tmp_path, OWNER, body())
    path = store_file(tmp_path)
    content = path.read_bytes()
    path.chmod(0)
    try:
        assert code(read_statuses, tmp_path, OWNER).code == 'operation_failed'
        assert code(save_status, tmp_path, OWNER, body(event_id='evt-2')).code == 'operation_failed'
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == content


# --- owner-only identity and HTTP ----------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    from calendar_support import calendar_instance
    with calendar_instance(tmp_path) as instance:
        yield instance


@pytest.fixture
def http(host, tmp_path):
    """The assembled v1.2 app with a synthetic dashboard directory, and a recording transport nothing may ever call."""
    from starlette.testclient import TestClient
    from bazarlyk.service.web import create_app
    from test_calendar_tools import GOOGLE_CONFIG

    for name in ('js', 'css'):
        (tmp_path / 'dashboard' / name).mkdir(parents=True)
    requests = []
    host.runtime.calendar.config_override = {'personal': GOOGLE_CONFIG}
    host.runtime.calendar.transport = httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(500))
    web = create_app(host.runtime, origins=['http://testserver'], manage_lifecycle=False, dashboard_root=tmp_path / 'dashboard')
    with TestClient(web) as client:
        client.provider_requests = requests
        yield client


def owner_context(host):
    return InvocationContext(Principal(host.owner, 'human'), source='dashboard')


def preferences_directory(host):
    return host.paths.runtime / 'dashboard-preferences'


def test_owner_context_saves_under_the_runtime_directory_and_others_are_refused_before_any_store_access(host):
    from calendar_support import create_calendar_agent
    runtime = host.runtime
    assert statuses(runtime, owner_context(host)) == {'items': []}
    assert not preferences_directory(host).exists()                      # reading creates nothing
    saved = statuses(runtime, owner_context(host), body())['item']
    assert statuses(runtime, owner_context(host)) == {'items': [saved]}
    assert store_file(host.paths.runtime, host.owner).is_file()

    register_sources(host, host.business, 'first')
    agent = create_calendar_agent(host, 'calendar-coordinator')
    with runtime.database.transaction() as uow:                          # even the configured coordinator is not the owner
        installation = uow.repository('installation').get('service', required=True)
        uow.repository('installation').update('service', {'management_coordinator_id': agent.id}, expected_revision=installation['revision'])
    before = tree(preferences_directory(host))
    outsiders = [runtime.resolver.invocation_context(agent),
                 InvocationContext(Principal(new_id(), 'human'), source='dashboard'),     # another human, not the registered owner
                 InvocationContext(Principal(host.owner, 'agent'), source='mcp')]         # the owner's ID presented as an agent
    for outsider in outsiders:
        for change in (None, body(event_id='intruder')):
            assert code(statuses, runtime, outsider, change).code == 'forbidden'
    assert tree(preferences_directory(host)) == before
    assert [i['event_id'] for i in read_statuses(host.paths.runtime, host.owner)] == ['evt-1']


def test_owner_http_contract_envelope_headers_and_private_fields(http, host):
    route = '/api/root/calendar-statuses'
    empty = http.get(route, headers=ORIGIN)
    assert empty.status_code == 200, empty.text
    assert empty.headers['cache-control'] == 'private, no-store'
    assert empty.json()['status'] == 'ok' and empty.json()['data'] == {'items': []}
    assert not preferences_directory(host).exists()

    request = {**BASE, 'request_id': new_id(), 'occurrence_start': '2026-10-15T10:00:00-04:00'}
    saved = http.post(route, headers=ORIGIN, json=request)
    assert saved.status_code == 200, saved.text
    assert saved.headers['cache-control'] == 'private, no-store'
    item = saved.json()['data']['item']
    assert set(saved.json()['data']) == {'item'} and set(item) == PUBLIC
    assert item == {'connection_id': 'personal', 'calendar_id': 'primary', 'event_id': 'evt-1',
                    'occurrence_start': '2026-10-15T14:00:00.000Z', 'status': 'completed', 'revision': 1}
    listing = http.get(route, headers=ORIGIN)
    assert listing.headers['cache-control'] == 'private, no-store' and listing.json()['data'] == {'items': [item]}
    assert set(listing.json()['data']['items'][0]) == PUBLIC

    # Persisted bookkeeping stays private: the request ID is stored, never returned, and the file name hides the owner.
    stored = store_file(host.paths.runtime, host.owner)
    assert request['request_id'] in stored.read_text() and request['request_id'] not in saved.text + listing.text
    assert host.owner not in stored.name and stat.S_IMODE(stored.stat().st_mode) == 0o600

    replay = http.post(route, headers=ORIGIN, json=request)
    assert replay.status_code == 200 and replay.json()['data'] == saved.json()['data']
    conflict = http.post(route, headers=ORIGIN, json={**request, 'status': 'cancelled'})
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'idempotency_conflict'
    stale = http.post(route, headers=ORIGIN, json={**request, 'request_id': new_id(), 'status': 'cancelled'})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'revision_conflict'
    assert stale.json()['error']['current_revision'] == 1
    assert http.post(route, headers=ORIGIN, json={**request, 'request_id': new_id(), 'status': 'pending', 'expected_revision': 1}
                     ).json()['data']['item']['revision'] == 2

    invalid = http.post(route, headers=ORIGIN, json={**request, 'request_id': new_id(), 'status': 'later', 'owner_id': 'someone'})
    assert invalid.status_code == 422 and invalid.json()['error']['code'] == 'validation_error'
    assert set(invalid.json()['error']['field_errors']) == {'status', 'owner_id'}
    assert http.get(route, headers=ORIGIN).json()['data']['items'][0]['status'] == 'pending'


def test_owner_http_route_bounds_and_shapes_requests(http, host):
    route = '/api/root/calendar-statuses'
    assert http.post(route, headers=ORIGIN, content=json.dumps(BASE)).status_code == 400            # not application/json
    assert http.post(route, headers={**ORIGIN, 'Content-Type': 'application/json'}, content=b'{').status_code == 400
    wrong = http.post(route, headers={**ORIGIN, 'Content-Type': 'application/json'}, content=b'{"a":1,"a":2}')
    assert wrong.status_code == 400 and wrong.json()['error']['code'] == 'invalid_json'
    assert http.post(route, headers=ORIGIN, json=[BASE]).status_code == 400                         # not an object
    huge = http.post(route, headers=ORIGIN, json={**BASE, 'event_id': 'x' * 200000})
    assert huge.status_code == 413
    assert http.put(route, headers=ORIGIN, json=BASE).status_code == 405
    assert http.post(route, json={**BASE, 'request_id': new_id()}).status_code == 403              # mutation needs a same-origin request
    assert not preferences_directory(host).exists()


def test_http_rejects_every_authorization_header_before_touching_the_store(http, host):
    route = '/api/root/calendar-statuses'
    for header in ('Bearer invalid', 'Bearer ' + 'a' * 64, 'Basic dXNlcjpwYXNz'):
        for method in ('get', 'post'):
            kwargs = {'json': body()} if method == 'post' else {}
            response = getattr(http, method)(route, headers={**ORIGIN, 'Authorization': header}, **kwargs)
            assert response.status_code == 403, (header, method, response.text)
            assert response.json()['status'] == 'error' and response.json()['error']['code'] == 'forbidden'
    assert not preferences_directory(host).exists()
    saved = http.post(route, headers=ORIGIN, json=body())
    assert saved.status_code == 200, saved.text


def test_http_corruption_is_an_explicit_server_error_and_keeps_the_file(http, host):
    route = '/api/root/calendar-statuses'
    http.post(route, headers=ORIGIN, json=body())
    path = store_file(host.paths.runtime, host.owner)
    path.write_text('{"version": 1, "items": ')
    for response in (http.get(route, headers=ORIGIN), http.post(route, headers=ORIGIN, json=body(event_id='evt-2'))):
        assert response.status_code == 500 and response.json()['error']['code'] == 'operation_failed'
        assert str(host.paths.runtime) not in response.text
    assert path.read_text() == '{"version": 1, "items": '


def test_status_changes_never_reach_a_calendar_provider(http, host):
    route = '/api/root/calendar-statuses'
    expected = 0
    for status in ('completed', 'cancelled', 'rescheduled', 'pending'):
        saved = http.post(route, headers=ORIGIN, json={**BASE, 'request_id': new_id(), 'status': status, 'expected_revision': expected,
                                                       'occurrence_start': '2026-10-15T14:00:00.000Z'})
        assert saved.status_code == 200, saved.text
        expected = saved.json()['data']['item']['revision']
    http.get(route, headers=ORIGIN)
    assert http.provider_requests == []                                  # no listing, no read, no write, no notification
    with host.runtime.database.read() as uow:                            # and no operational record of the label exists
        assert uow.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%calendar_status%'").fetchone()[0] == 0
