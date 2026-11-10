"""Bounded native-hook wait, with HTTP and telemetry owned by the host supervisor.

One response slot per native session, replaced on the next request. Saved context
is input to the trusted hook, never authority for an operation or another owner.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import uuid

from qorqut_operations.errors import DomainError
from qorqut.installation import installation_config
from .client import path_has_symlink, write_json_private
from .recovery import locked

HOOK_WAIT = 4.0
IDENTITY = ('registration_id', 'session_id', 'owner', 'runtime')


def same(left, right):
    return all(left.get(key) == right.get(key) for key in IDENTITY)


def read(path):
    if path_has_symlink(path):
        raise OSError('Bootstrap handoff cannot follow symlinks')
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def running(root):
    path = root / 'state' / f'supervisor-{socket.gethostname()}.lock'
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(descriptor)


def result_path(root, item):
    session = hashlib.sha256(item['session_id'].encode()).hexdigest()
    return root / 'state/agent-context' / f"{item['registration_id']}-{session}.json"


def pending_path(root, item):
    # Matches the existing supervisor pending-repair location.
    return root / 'state/agent-recovery' / f"{item['registration_id']}-{item['session_id']}.json"


def offer(root, registration_path, registration, native, body, revision=None):
    """Reuse only a result or in-flight request for this identity and revision."""
    result = result_path(root, native)
    pending = pending_path(root, native)
    with locked(registration, native['session_id']):
        saved = read(result)
        item = read(pending)
        saved_revision = ((saved.get('response') or {}).get('agentic_catalog_revision')
                          if not saved.get('error') else saved.get('catalog_revision'))
        usable = body.get('event') == 'prompt' or bool((saved.get('response') or {}).get('mandatory_context')) or bool(saved.get('error'))
        matching_result = (same(saved, native) and usable and (revision is None or saved_revision == revision)
                           and (not item.get('request_id') or item.get('request_id') == saved.get('request_id')))
        recent = (revision is not None and matching_result and saved.get('consumed') and not saved.get('error')
                  and body.get('event') == 'prompt' and body.get('last_effective_config_id')
                  == (saved.get('response') or {}).get('effective_config_id')
                  and 0 <= time.time() - saved.get('published_at', 0) < 30)
        if recent or (matching_result and not saved.get('consumed')):
            return result
        if not (same(item, native) and item.get('request_id')
                and item.get('request_id') != saved.get('request_id')
                and (revision is None or item.get('catalog_revision') == revision)):
            item = {**native, 'registration_path': str(registration_path),
                    'request_id': str(uuid.uuid4()), 'bootstrap_body': dict(body), 'catalog_revision': revision}
            write_json_private(pending, item)
    return result


def request(registration_path, registration, native, body):
    config = installation_config()
    if not native or not config or not running(Path(config['root'])):
        return None
    from .catalog_revision import open_observer
    root = Path(config['root'])
    deadline = time.monotonic() + HOOK_WAIT
    with open_observer(root, deadline) as observer:
        return _request_with_observer(root, registration_path, registration, native, body, deadline, observer)


def _request_with_observer(root, registration_path, registration, native, body, deadline, observer):
    # A hint can schedule an authenticated refresh but never authorize delivery.
    # Both successful paths below require the full observation before returning.
    revision = observer.hint()
    result = offer(root, registration_path, registration, native, body, revision)
    while True:
        saved = read(result)
        response = saved.get('response') or {}
        usable = (body.get('event') == 'prompt' or bool(response.get('mandatory_context')) or saved.get('error'))
        pending = read(pending_path(root, native))
        matching = (same(saved, native) and usable and response.get('agentic_catalog_revision') == revision
                    and (not pending.get('request_id') or pending.get('request_id') == saved.get('request_id')))
        if matching and not saved.get('consumed'):
            current = observer.observe()
            if current != revision:
                revision = current
                offer(root, registration_path, registration, native, body, revision)
                continue
            return response, saved
        if matching and saved.get('consumed') and body.get('event') == 'prompt' and not saved.get('error'):
            if (body.get('last_effective_config_id') == response.get('effective_config_id')
                    and 0 <= time.time() - saved.get('published_at', 0) < 30):
                current = observer.observe()
                if current != revision:
                    revision = current
                    offer(root, registration_path, registration, native, body, revision)
                    continue
                return {**response, 'refresh_required': False}, {**native, 'registration_path': str(registration_path), 'deferred': True}
        # A permanent error belongs to the current offered request, not a stale result.
        if (same(saved, native) and saved.get('error') and not saved.get('consumed')
                and saved.get('catalog_revision') == revision
                and (not pending.get('request_id') or pending.get('request_id') == saved.get('request_id'))):
            consume(root, registration, saved)
            error = saved['error']
            raise DomainError(error['code'], error['message'], retryable=error.get('retryable', False))
        if time.monotonic() >= deadline:
            raise DomainError('bootstrap_pending', 'The helper is refreshing this session in the background', retryable=True)
        time.sleep(min(.025, max(0, deadline - time.monotonic())))


def publish(root, registration, item, *, response=None, error=None):
    if response:
        from qorqut.agents.context import MAX_CONTEXT_CHARACTERS
        context = response.get('mandatory_context')
        if isinstance(context,dict):context = context.get('rendered')
        if (not isinstance(response.get('effective_config_id'),str)
                or (response.get('refresh_required') is not False and
                    (not isinstance(context,str) or not context.strip() or len(context)>MAX_CONTEXT_CHARACTERS))):
            response,error = None,{'code':'invalid_bootstrap','message':'Mandatory context is unavailable','retryable':False}
    with locked(registration, item['session_id']):
        if read(pending_path(root, item)).get('request_id') != item['request_id']:
            return False
        write_json_private(result_path(root, item), {**{k: item[k] for k in IDENTITY},
            'registration_path': item['registration_path'], 'request_id': item['request_id'],
            'catalog_revision': item.get('catalog_revision'), 'response': response, 'error': error, 'consumed': False, 'published_at': time.time()})
        return True


def discard_pending(path, item):
    """Do not delete a newer hook's request while reaping an older future."""
    from .client import load_registration
    registration = load_registration(Path(item['registration_path']))
    with locked(registration,item['session_id']):
        current = read(path)
        if same(current,item) and current.get('request_id') == item.get('request_id'):
            path.unlink(missing_ok=True)
            return True
    return False


def consume(root, registration, item):
    with locked(registration, item['session_id']):
        path = result_path(root, item)
        saved = read(path)
        if same(saved, item) and saved.get('request_id') == item['request_id']:
            write_json_private(path, {**saved, 'consumed': True})


def queue(registration, item, route, body):
    """Latest receipt/observation per session; removed after delivery, no history copies."""
    config = installation_config()
    if not config:
        return False
    slot = result_path(Path(config['root']), item).name.removesuffix('.json')
    path = Path(config['root']) / 'state/agent-followups' / (slot + '-' + route.rsplit('/', 1)[1] + '.json')
    with locked(registration, item['session_id']):
        write_json_private(path, {**{k: item[k] for k in IDENTITY},
            'registration_path': item['registration_path'], 'request_id': str(uuid.uuid4()),
            'route': route, 'body': body})
    return True


def observation(registration):
    """Read a hook observation; callers still verify its native process and backend ownership."""
    base = Path(registration['workspace']) / '.qorqut'
    native = read(base / 'native-binding.json')
    if native:
        return native
    legacy = read(base / 'terminal-chat.json')
    if (legacy.get('agent_id') != registration['agent_id']
            or legacy.get('workspace') != registration['workspace'] or legacy.get('event') == 'SessionEnd'):
        return {}
    return {**legacy, 'registration_id': registration['registration_id'],
            'owner': f"{legacy['pid']}:{legacy['started']}", 'legacy_observation': True}
