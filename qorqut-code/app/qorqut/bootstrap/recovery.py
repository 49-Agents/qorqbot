"""Session-local preparation failures, advisory only; never grant authority."""
import hashlib
import json
import fcntl
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from .client import path_has_symlink, write_json_private

DENIALS = frozenset({'forbidden', 'invalid_principal', 'inactive_agent', 'invalid_credential',
    'missing_credential', 'missing_registration', 'invalid_registration', 'registration_conflict'})


@contextmanager
def locked(registration, session):
    """Serialize short local state updates, never HTTP or native delivery."""
    path = state_path(registration, session)
    if path is None:
        yield
        return
    path = path.with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def state_path(registration, session):
    if not isinstance(session, str) or not session or 'workspace' not in registration:
        return None
    path = Path(registration['workspace']) / '.qorqut/preparation-recovery' / (
        hashlib.sha256(session.encode()).hexdigest() + '.json')
    if path_has_symlink(path):
        raise OSError('Preparation state cannot follow symlinks')
    return path


def read(registration, session):
    path = state_path(registration, session)
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('runtime') not in {'codex', 'claude-code'}:
            raise ValueError('Invalid preparation state')
    except ValueError as exc:
        raise OSError('Invalid preparation state') from exc
    if value.get('registration_id') != registration['registration_id']:
        return None
    return value


def record(registration, session, runtime, code, *, owner=None, details=None):
    path = state_path(registration, session)
    if path:
        with locked(registration, session):
            prior = read(registration, session)
            same = prior and prior.get('runtime') == runtime and prior.get('owner') == owner
            value = {**(prior if same else {}), 'registration_id': registration['registration_id'], 'runtime': runtime,
                'code': code, 'owner': owner,
                'failure_id': prior['failure_id'] if same else str(uuid.uuid4())}
            if isinstance(details,dict) and isinstance(details.get('reason'),str) and re.fullmatch('[a-z_]{1,80}',details['reason']):
                value['last_request'] = {'code':code,'reason':details['reason']}
                if type(details.get('elapsed_ms')) in (int,float):
                    value['last_request']['elapsed_ms'] = details['elapsed_ms']
            write_json_private(path, value)
            return value


def clear(registration, session, *, failure_id=None):
    path = state_path(registration, session)
    if path:
        with locked(registration, session):
            current = read(registration, session)
            if failure_id is None or current and current.get('failure_id') == failure_id:
                path.unlink(missing_ok=True)
