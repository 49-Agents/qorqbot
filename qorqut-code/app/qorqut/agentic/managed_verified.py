"""Remember which retained managed updates already passed full re-derivation.

Re-deriving one retained managed update (its plan, witnessed inventory, supplier decisions and resulting binding)
reads only its own retained bytes and the hash-linked history they name, so the answer for the same bytes never
changes. ``managed_ancestry`` re-derived every update on every check; with a dozen updates that took over twenty
seconds, longer than the start-up hook may wait, so no refreshed context was ever delivered.

A remembered result is keyed by the SHA-256 of the exact bytes read in this check (installed record, prior record,
intent and journal), the instance paths and the source of the verifying code. Any changed byte, path or code is a
miss and the update is re-derived in full. A hit skips only the pure re-derivation and the parse of the prior binding (its few fields the walk reads are kept
with the entry): ``managed_ancestry`` still reads every retained file, checks the prior binding's digest, and runs
every check against current state (directory and Git identities, the copy receipt, the feed event and Git ancestry).

The file is a cache, not evidence. It lives with other host runtime state, never in the retained history, and an
unreadable, oversized or malformed file is ignored. Offline and read-only callers neither read nor write it.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from qorqut.private_files import read_private_file, require_private_directory

FORMAT = 1
_DIRECTORY = 'agentic-verified'
_NAME = 'managed-history.json'
_MAX_BYTES = 1 << 20
_MAX_ENTRIES = 512
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_STALE_TEMPORARY = 3600


@functools.lru_cache(maxsize=1)
def code_identity():
    """Digest of every source file that can take part in verification, computed once per process."""
    import qorqut.agentic
    import qorqut_agentic
    import qorqut.file_identity
    import qorqut.private_files
    files = []
    for package in (qorqut.agentic, qorqut_agentic):
        for directory in package.__path__:
            base = Path(directory)
            files.extend((package.__name__, path.relative_to(base).as_posix(), path) for path in base.rglob('*.py')
                         if '__pycache__' not in path.parts)
    for module in (qorqut.file_identity, qorqut.private_files):
        files.append((module.__name__, '', Path(module.__file__)))
    hasher = hashlib.sha256()
    for package, name, path in sorted(files):
        hasher.update(f'{package}\0{name}\0'.encode())
        hasher.update(hashlib.sha256(path.read_bytes()).digest())
    return hasher.hexdigest()


def location(paths):
    """The cache file, or None when these paths have no host runtime directory."""
    try:
        runtime = getattr(paths, 'runtime', None)
        return None if runtime is None else Path(runtime) / _DIRECTORY / _NAME
    except Exception:
        return None


def node_key(paths, record_sha256, prior_sha256, intent_raw, journal_raw):
    """``record_sha256`` and ``prior_sha256`` are digests the caller computed or checked from the bytes it read."""
    from .upgrade import history_directory
    value = {'format': FORMAT, 'code': code_identity(), 'instance_root': str(paths.instance_root),
             'root': str(paths.agentic_root), 'state': str(paths.agentic_runtime),
             'history': str(history_directory(paths)), 'record': record_sha256, 'prior': prior_sha256,
             # A cache key, not evidence: BLAKE2b reads the large intent faster than SHA-256 on hosts without SHA
             # instructions, and is collision resistant too.
             'intent': hashlib.blake2b(intent_raw, digest_size=32).hexdigest(),
             'journal': hashlib.blake2b(journal_raw, digest_size=32).hexdigest()}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# The prior binding fields the chain walk reads before it needs a whole record (see managed_ancestry._verify_chain).
PRIOR_KEYS = ('managed_upgrade', 'instance_id', 'root', 'state', 'initial_commit')
_PRIOR_REQUIRED = {'instance_id', 'root', 'state', 'initial_commit'}


def facts(intent, prior):
    """The few intent fields the current-state checks need on a hit, and the walk's view of the prior binding."""
    plan = intent['plan']
    return {'plan_digest': plan['plan_digest'], 'receipt_sha256': intent['receipt_sha256'],
            'identity': {'root': plan['identity']['root'], 'state': plan['identity']['state']},
            'changes': bool(plan['changes']), 'prior': {k: prior[k] for k in PRIOR_KEYS if k in prior}}


def _valid_facts(value):
    return (isinstance(value, dict) and set(value) == {'plan_digest', 'receipt_sha256', 'identity', 'changes', 'prior'}
            and isinstance(value['plan_digest'], str) and _HEX.match(value['plan_digest']) is not None
            and isinstance(value['receipt_sha256'], str) and _HEX.match(value['receipt_sha256']) is not None
            and type(value['changes']) is bool and isinstance(value['identity'], dict)
            and set(value['identity']) == {'root', 'state'}
            and all(isinstance(item, dict) for item in value['identity'].values())
            and isinstance(value['prior'], dict) and _PRIOR_REQUIRED <= set(value['prior']) <= set(PRIOR_KEYS))


def load(paths):
    """Remembered entries by key, or None when these paths cannot use the cache. A bad file reads as empty."""
    path = location(paths)
    if path is None:
        return None
    try:
        code_identity()
    except Exception:
        return None
    try:
        if not os.path.lexists(path):
            return {}
        value = json.loads(read_private_file(path, max_bytes=_MAX_BYTES))
    except Exception:
        return {}
    if not isinstance(value, dict) or value.get('format') != FORMAT or not isinstance(value.get('entries'), dict):
        return {}
    return {key: item for key, item in value['entries'].items()
            if isinstance(key, str) and _HEX.match(key) and _valid_facts(item)}


def remember(paths, key, value):
    """Add one entry. A failure to write only costs a later full re-derivation, so it is never raised."""
    path = location(paths)
    if path is None or not _HEX.match(key) or not _valid_facts(value):
        return False
    try:
        directory = require_private_directory(path.parent, create=True)
        entries = load(paths) or {}
        entries.pop(key, None)
        entries[key] = value
        while len(entries) > _MAX_ENTRIES:
            entries.pop(next(iter(entries)))
        data = json.dumps({'format': FORMAT, 'entries': entries}, sort_keys=False, separators=(',', ':')).encode() + b'\n'
        if len(data) > _MAX_BYTES:
            return False
        _sweep(directory)
        fd, temporary = tempfile.mkstemp(prefix='.' + _NAME + '.', suffix='.tmp', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def _sweep(directory):
    """Remove temporary files left by a check that was stopped while writing."""
    now = time.time()
    with os.scandir(directory) as stream:
        for entry in stream:
            if entry.name.startswith('.' + _NAME + '.') and entry.name.endswith('.tmp'):
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime > _STALE_TEMPORARY:
                        os.unlink(entry.path)
                except OSError:
                    pass
