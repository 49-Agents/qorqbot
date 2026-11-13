"""Owner UI preferences, outside versioned releases and operational records."""
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from qorqut_operations.contracts import DomainError


def _owner_file(runtime_directory, owner_id, suffix):
    return Path(runtime_directory) / 'dashboard-preferences' / (hashlib.sha256(owner_id.encode()).hexdigest() + suffix)


def testing_mode(runtime_directory, owner_id, change=None):
    """Read or set the owner's explicit Testing mode preference.

    The boolean lives in its own file, so enabling or disabling Testing mode
    never rewrites the saved agent pins. A missing file is the normal default:
    Testing mode is off until the owner explicitly turns it on.
    """
    path = _owner_file(runtime_directory, owner_id, '.testing-mode.json')

    def read():
        if not path.exists():
            return False
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError, UnicodeError):
            raise DomainError('operation_failed', 'The saved Testing mode preference could not be read') from None
        if not isinstance(saved, dict) or set(saved) != {'enabled'} or type(saved['enabled']) is not bool:
            raise DomainError('operation_failed', 'The saved Testing mode preference is not valid')
        return saved['enabled']

    if change is not None:
        if not isinstance(change, dict) or set(change) != {'enabled'} or type(change['enabled']) is not bool:
            raise DomainError('validation_error', 'Supply exactly an enabled boolean')
        enabled = change['enabled']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.testing-mode-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'enabled': enabled}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {'enabled': enabled}
    return {'enabled': read()}


def pins(runtime_directory, owner_id, change=None):
    """Read, initialize once, or set one pin under a cross-process lock.

    Per-item writes avoid clobbering changes made in another browser. Retried
    writes set a desired value rather than toggling it. An empty saved list is
    authoritative, so stale browser caches never resurrect removed pins.
    """
    directory = Path(runtime_directory) / 'dashboard-preferences'
    directory.mkdir(parents=True, exist_ok=True)
    # Owner identity comes from the authenticated installation, never the URL.
    path = directory / (hashlib.sha256(owner_id.encode()).hexdigest() + '.json')
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(path.read_text()) if path.exists() else None
        ids = list(saved['pinned_agents']) if saved is not None else []
        if change is not None:
            if set(change) == {'initialize'}:
                incoming = change['initialize']
                if not isinstance(incoming, list) or len(incoming) > 200 or any(not isinstance(x, str) or not 1 <= len(x) <= 200 for x in incoming):
                    raise DomainError('validation_error', 'Invalid saved agent pins')
                if saved is None: ids = list(dict.fromkeys(incoming))
            elif set(change) == {'agent_id', 'pinned'}:
                identity = change['agent_id']
                if not isinstance(identity, str) or not 1 <= len(identity) <= 200 or type(change['pinned']) is not bool:
                    raise DomainError('validation_error', 'Supply an agent ID and pinned boolean')
                ids = [x for x in ids if x != identity]
                if change['pinned']: ids.append(identity)
                if len(ids) > 200: raise DomainError('validation_error', 'Too many agent pins')
            else:
                raise DomainError('validation_error', 'Invalid pin preference update')
            fd, temporary = tempfile.mkstemp(dir=directory, prefix='.pins-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'pinned_agents': ids}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
            saved = {'pinned_agents': ids}
        return {'initialized': saved is not None, 'pinned_agents': ids}


MAX_THREAD_PINS = 50
_SOURCE_TYPE = __import__('re').compile(r'[a-z][a-z_]{0,39}')


def _thread_source(value):
    """A thread's canonical source identity: type, id and owning Project (None for Global)."""
    if not isinstance(value, dict) or set(value) != {'type', 'id', 'business_id'}:
        return None
    kind, identity, business = value['type'], value['id'], value['business_id']
    if not isinstance(kind, str) or not _SOURCE_TYPE.fullmatch(kind):
        return None
    if not isinstance(identity, str) or not 1 <= len(identity) <= 200:
        return None
    if business is not None and (not isinstance(business, str) or not 1 <= len(business) <= 200):
        return None
    return {'type': kind, 'id': identity, 'business_id': business}


def thread_pins(runtime_directory, owner_id, change=None):
    """Read or set one pinned thread in the owner's Focus top row, under a cross-process lock.

    Pins are owner presentation only: the source is opened through its ordinary authorized
    reads, so a pin never grants access or reveals anything a read would not. Each write sets
    the desired state of one source (retries are harmless) and keeps the stored title, which
    the dashboard shows until the record's own title is read again.
    """
    path = _owner_file(runtime_directory, owner_id, '.thread-pins.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = []
        if path.exists():
            try:
                saved = json.loads(path.read_text())
            except (OSError, ValueError, UnicodeError):
                raise DomainError('operation_failed', 'The saved pinned threads could not be read') from None
            if not isinstance(saved, dict) or not isinstance(saved.get('threads'), list):
                raise DomainError('operation_failed', 'The saved pinned threads are not valid')
            items = [item for item in saved['threads'] if isinstance(item, dict) and _thread_source(item.get('source'))]
        if change is not None:
            if not isinstance(change, dict) or set(change) != {'source', 'title', 'pinned'} or type(change['pinned']) is not bool:
                raise DomainError('validation_error', 'Supply a source, a title and a pinned boolean')
            source, title = _thread_source(change['source']), change['title']
            if source is None:
                raise DomainError('validation_error', 'Supply a source with a type, an id and a business_id')
            if not isinstance(title, str) or len(title) > 200:
                raise DomainError('validation_error', 'A pinned thread title is at most 200 characters')
            items = [item for item in items if item['source'] != source]
            if change['pinned']:
                items.append({'source': source, 'title': title.strip()})
            if len(items) > MAX_THREAD_PINS:
                raise DomainError('validation_error', f'At most {MAX_THREAD_PINS} threads can be pinned')
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.thread-pins-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'threads': items}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {'threads': items}
