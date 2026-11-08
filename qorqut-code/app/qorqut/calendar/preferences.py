"""Owner-only personal status labels for calendar events, kept outside provider data and operational records.

A label (pending, completed, cancelled or rescheduled) is a dashboard tracking note. The provider stays authoritative for
the event, its guests and its schedule: nothing here reads or writes a provider, notifies a guest, answers an invitation or
moves an event. The installation owner's labels live in one private, bounded file under the runtime's dashboard-preferences
directory, outside versioned releases, the operational database and Git history.

A label is identified by connection, calendar, event and occurrence start; the last is empty for a nonrecurring event. Every
save names the revision it was based on, so an older browser can never overwrite a newer edit, and carries a request ID, so a
retry after a lost response replays the saved item instead of applying twice.
"""
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut.management.identity import installation_owner

logger = logging.getLogger(__name__)

STATUSES = ('pending', 'completed', 'cancelled', 'rescheduled')
MAX_ITEMS = 10000
ID_LIMIT = 2000
REQUEST_ID_LIMIT = 200
LOCK_SECONDS = 10.0
STORE_VERSION = 1
PUBLIC_FIELDS = ('connection_id', 'calendar_id', 'event_id', 'occurrence_start', 'status', 'revision')
REQUEST_FIELDS = ('connection_id', 'calendar_id', 'event_id', 'occurrence_start', 'status', 'expected_revision', 'request_id')
STORED_FIELDS = frozenset((*PUBLIC_FIELDS, 'request_id', 'request_hash'))

# ASCII digits only: Python's \d also matches other scripts' digits.
_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_INSTANT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,3})?)?(?:Z|[+-](?P<offset_hours>[0-9]{2}):(?P<offset_minutes>[0-9]{2}))')
_HASH = re.compile(r'[0-9a-f]{64}')


def canonical_occurrence(value):
    """The occurrence start as stored: empty, an exact ISO date, or a UTC ISO instant in milliseconds. None when invalid."""
    if value == '':
        return ''
    try:
        if _DATE.fullmatch(value):
            return date.fromisoformat(value).isoformat()
        instant = _INSTANT.fullmatch(value)
        # fromisoformat folds an overflowing offset (+05:99) into a different instant, so bound its parts first.
        if instant and (instant['offset_hours'] or '00') <= '23' and (instant['offset_minutes'] or '00') <= '59':
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is not None:
                return moment.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        pass
    return None


def _text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        return False
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def validated_save(change):
    """The exact, normalized save request, or a field-by-field validation error."""
    if not isinstance(change, dict):
        raise DomainError('validation_error', 'Supply a calendar status request object')
    errors = {name: 'unknown field' for name in sorted(set(change) - set(REQUEST_FIELDS))}
    errors.update({name: 'required' for name in REQUEST_FIELDS if name not in change})
    present = {name: change[name] for name in REQUEST_FIELDS if name in change}
    for name in ('connection_id', 'calendar_id', 'event_id'):
        if name in present and not _text(present[name], ID_LIMIT):
            errors[name] = f'must be a nonempty string of at most {ID_LIMIT} characters'
    if 'occurrence_start' in present and (not isinstance(present['occurrence_start'], str)
                                           or canonical_occurrence(present['occurrence_start']) is None):
        errors['occurrence_start'] = 'must be empty, YYYY-MM-DD, or an ISO date-time with a UTC offset'
    if 'status' in present and present['status'] not in STATUSES:
        errors['status'] = 'must be one of ' + ', '.join(STATUSES)
    if 'expected_revision' in present and (type(present['expected_revision']) is not int or present['expected_revision'] < 0):
        errors['expected_revision'] = 'must be an integer of at least 0'
    if 'request_id' in present and not _text(present['request_id'], REQUEST_ID_LIMIT):
        errors['request_id'] = f'must be a nonempty string of at most {REQUEST_ID_LIMIT} characters'
    if errors:
        raise DomainError('validation_error', 'Calendar status request fields are invalid', field_errors=errors)
    return {**change, 'occurrence_start': canonical_occurrence(change['occurrence_start'])}


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


def _key(item):
    return _digest([item['connection_id'], item['calendar_id'], item['event_id'], item['occurrence_start']])


def _public(item):
    return {name: item[name] for name in PUBLIC_FIELDS}


def _unreadable(reason):
    return DomainError('operation_failed', f'Saved calendar statuses {reason}; nothing was changed')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError('nonfinite number')


def _stored_item(key, item):
    return (isinstance(item, dict) and set(item) == STORED_FIELDS
            and all(_text(item[name], ID_LIMIT) for name in ('connection_id', 'calendar_id', 'event_id'))
            and isinstance(item['occurrence_start'], str) and canonical_occurrence(item['occurrence_start']) == item['occurrence_start']
            and item['status'] in STATUSES and type(item['revision']) is int and item['revision'] >= 1
            and _text(item['request_id'], REQUEST_ID_LIMIT)
            and isinstance(item['request_hash'], str) and _HASH.fullmatch(item['request_hash'])
            and key == _key(item))


def _parse(data):
    try:
        document = json.loads(data, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _unreadable('are not valid JSON') from None
    if (not isinstance(document, dict) or set(document) != {'version', 'items'} or type(document['version']) is not int
            or document['version'] != STORE_VERSION or not isinstance(document['items'], dict)
            or len(document['items']) > MAX_ITEMS):
        raise _unreadable('have an unrecognized layout')
    if not all(_stored_item(key, item) for key, item in document['items'].items()):
        raise _unreadable('contain an invalid entry')
    return document['items']


def _owner_name(owner_id):
    if not isinstance(owner_id, str) or not owner_id:
        raise DomainError('forbidden', 'Calendar statuses are owner-only')
    return hashlib.sha256(owner_id.encode()).hexdigest()


def _open_directory(runtime_directory, *, create):
    """The preferences directory as a descriptor (every later access is relative to it), or None when absent and not created."""
    directory = Path(runtime_directory) / 'dashboard-preferences'
    try:
        if create:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if create:
            raise _unreadable('cannot be stored') from None
        return None
    except OSError:
        logger.exception('Calendar status directory is not usable')
        raise _unreadable('cannot be reached') from None
    if os.fstat(descriptor).st_uid != os.geteuid():
        os.close(descriptor)
        raise _unreadable('are in a directory owned by another user')
    return descriptor


def _load(directory, name):
    """The stored items; empty only when the file does not exist. Anything unreadable is an error, never an empty set."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return {}
    except OSError:
        logger.exception('Calendar statuses could not be opened')
        raise _unreadable('could not be opened') from None
    try:
        with os.fdopen(descriptor, 'rb') as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077 or status.st_uid != os.geteuid():
                raise _unreadable('are not a private regular file')
            data = stream.read()
    except OSError:
        logger.exception('Calendar statuses could not be read')
        raise _unreadable('could not be read') from None
    return _parse(data)


def _remove(directory, name):
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _replace(directory, name, items, prefix):
    """Write a private temporary file, flush it, swap it in atomically, then flush the directory entry.

    A failure before the swap changes nothing. Once the swap has happened the new store is visible, so a failure to
    flush the directory leaves the outcome uncertain rather than undone."""
    payload = json.dumps({'version': STORE_VERSION, 'items': items}, separators=(',', ':'), sort_keys=True).encode()
    temporary = prefix + secrets.token_hex(8)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor is not None:
                os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except OSError:
        logger.exception('Calendar statuses could not be saved')
        raise _unreadable('could not be saved') from None
    finally:
        _remove(directory, temporary)
    try:
        os.fsync(directory)
    except OSError:
        logger.exception('Calendar status directory could not be flushed after the swap')
        raise DomainError('operation_failed', 'The calendar status may have been saved but could not be confirmed durable; '
                          'reload it and retry with the same request ID', retryable=True) from None


@contextmanager
def _locked(directory, name):
    """Exclusive cross-process lock for one owner's store; a stuck holder becomes a retryable error, not a hung request."""
    try:
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    except OSError:
        logger.exception('Calendar status lock could not be opened')
        raise _unreadable('could not be locked') from None
    try:
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DomainError('busy', 'Calendar statuses are being saved; retry shortly', retryable=True) from None
                time.sleep(0.002)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def read_statuses(runtime_directory, owner_id):
    """Every saved item of the owner, ordered by target. A missing store is the legitimate empty set."""
    prefix = _owner_name(owner_id)
    directory = _open_directory(runtime_directory, create=False)
    if directory is None:
        return []
    try:
        items = _load(directory, prefix + '.calendar-statuses.json')
    finally:
        os.close(directory)
    return [_public(item) for item in sorted(items.values(), key=lambda item: (
        item['connection_id'], item['calendar_id'], item['event_id'], item['occurrence_start']))]


def save_status(runtime_directory, owner_id, change):
    """Apply one revision-checked, idempotent save and return the saved public item."""
    request = validated_save(change)
    prefix = _owner_name(owner_id)
    content = {name: request[name] for name in REQUEST_FIELDS if name != 'request_id'}
    request_hash = _digest(content)
    directory = _open_directory(runtime_directory, create=True)
    try:
        with _locked(directory, prefix + '.calendar-statuses.lock'):
            # Every writer of this owner's store holds the lock, so a leftover temporary file belongs to a crashed writer.
            temporary = '.' + prefix + '.calendar-statuses-'
            for leftover in (name for name in os.listdir(directory) if name.startswith(temporary)):
                _remove(directory, leftover)
            name = prefix + '.calendar-statuses.json'
            items = _load(directory, name)
            for item in items.values():
                if item['request_id'] == request['request_id']:
                    if item['request_hash'] != request_hash:
                        raise DomainError('idempotency_conflict', 'This request ID was already used for different content')
                    return _public(item)
            target = {field: request[field] for field in ('connection_id', 'calendar_id', 'event_id', 'occurrence_start')}
            key = _key(target)
            current = items.get(key)
            revision = current['revision'] if current else 0
            if request['expected_revision'] != revision:
                raise DomainError('revision_conflict', 'This status changed elsewhere; refresh and try again',
                                  current_revision=revision)
            if current is None and len(items) >= MAX_ITEMS:
                raise DomainError('validation_error', f'The limit of {MAX_ITEMS} saved calendar statuses is reached')
            saved = {**target, 'status': request['status'], 'revision': revision + 1,
                     'request_id': request['request_id'], 'request_hash': request_hash}
            _replace(directory, name, {**items, key: saved}, temporary)
            return _public(saved)
    finally:
        os.close(directory)


def statuses(runtime, ctx, change=None):
    """The owner's saved statuses, or with `change` one saved item. The installation owner is verified before any file access."""
    with runtime.database.read() as uow:
        owner = installation_owner(uow)
    if ctx.principal_kind != 'human' or not owner or ctx.principal_id != owner:
        raise DomainError('forbidden', 'Calendar statuses are owner-only')
    if change is None:
        return {'items': read_statuses(runtime.paths.runtime, ctx.principal_id)}
    return {'item': save_status(runtime.paths.runtime, ctx.principal_id, change)}
