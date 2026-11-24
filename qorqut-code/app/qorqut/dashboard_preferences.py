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


# What the owner's dashboards show, as set from outside a browser: theme, text size and the four desktop alerts.
DISPLAY_FIELDS = {
    'theme': ('dark', 'light', 'system'),
    'text_size': ('small', 'default', 'large'),
    'notify_replies': (True, False),
    'notify_signoffs': (True, False),
    'notify_reminders': (True, False),
    'notify_routines': (True, False),
}


def display(runtime_directory, owner_id, change=None):
    """Read or set the display choices made for the owner outside a browser (the coordinator's ``settings_set``).

    Theme, text size and desktop alerts live in each browser. A choice saved here is a request to every one of the owner's
    browsers: each applies a field once, when it first sees that field's revision, and keeps it as its own choice from then on.
    So a later change made in a browser stays until a newer revision of the same field arrives. ``change`` is a mapping of
    fields to values; each changed field gets the next revision. Returns ``{"fields": {name: {"value", "revision"}},
    "revision"}``, where ``revision`` is the highest one given so far (0 when nothing was ever set).
    """
    path = _owner_file(runtime_directory, owner_id, '.display.json')

    def valid(name, value):
        return name in DISPLAY_FIELDS and type(value) is type(DISPLAY_FIELDS[name][0]) and value in DISPLAY_FIELDS[name]

    def read():
        if not path.exists():
            return {}
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError, UnicodeError):
            raise DomainError('operation_failed', 'The saved display choices could not be read') from None
        fields = saved.get('fields') if isinstance(saved, dict) else None
        if not isinstance(fields, dict):
            raise DomainError('operation_failed', 'The saved display choices are not valid')
        return {name: {'value': item['value'], 'revision': item['revision']} for name, item in fields.items()
                if isinstance(item, dict) and valid(name, item.get('value')) and type(item.get('revision')) is int and item['revision'] > 0}

    def answer(fields):
        return {'fields': fields, 'revision': max((item['revision'] for item in fields.values()), default=0)}

    if change is None:
        return answer(read())
    if not isinstance(change, dict) or not change or any(not valid(name, value) for name, value in change.items()):
        raise DomainError('validation_error', 'Supply display fields with allowed values: ' + ', '.join(sorted(DISPLAY_FIELDS)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fields = read()
        revision = answer(fields)['revision']
        for name in sorted(change):
            revision += 1
            fields[name] = {'value': change[name], 'revision': revision}
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.display-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'fields': fields}, stream)
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
    return answer(fields)
