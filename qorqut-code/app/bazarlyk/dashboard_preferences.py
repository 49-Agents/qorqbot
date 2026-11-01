"""Owner UI preferences, outside versioned releases and operational records."""
import fcntl
import json
import os
import tempfile
from pathlib import Path

from bazarlyk.contracts import DomainError

def pins(root, owner_id, change=None):
    """Read, initialize once, or set one pin under a cross-process lock.

    Per-item writes avoid clobbering changes made in another browser. Retried
    writes set a desired value rather than toggling it. An empty saved list is
    authoritative, so stale browser caches never resurrect removed pins.
    """
    directory = Path(root) / '.bazarlyk' / 'dashboard-preferences'
    directory.mkdir(parents=True, exist_ok=True)
    # Owner identity comes from the authenticated installation, never the URL.
    import hashlib
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
