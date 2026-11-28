"""Owner-only personal notes on the Focus tab, kept outside tasks and every agent's context.

The dashboard shows one text per agent pane in the Journal's lines editor and saves it as a single block. Older layouts may
still hold several blocks (they were once placed among Focus cards); the dashboard reads those as one text and saves them back
as one block. A line that starts with "-" is a task line; ``statuses`` keeps each one's status by its trimmed text, exactly as
a Journal page does, and a task line without an entry is open. The
notes are a dashboard preference: they create no card, task, Signoff, message or notification and are read by no agent or
model. Each owner's layouts live in one private, bounded file under the runtime's dashboard-preferences directory, outside
versioned releases, the operational database and Git history; each agent has its own independent layout and revision inside it.
A layout that was never saved reads as revision 0 with no blocks, which the dashboard shows as a first-start sample text.

Every save names the revision it was based on. A save whose blocks and statuses equal the stored ones succeeds unchanged
whatever revision it names, so a retry after a lost response is harmless; any other stale save is a ``revision_conflict`` and writes nothing. A
block's ``before_id`` and ``after_id`` are kept for older layouts and never checked; ``card_ids`` (computed on every read,
never stored) still lists the stored Focus card IDs for compatibility.
"""
import hashlib
import json
import logging
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from qorqut_operations.application.plans import timestamp
from qorqut_operations.contracts import DomainError
from qorqut_system import locks, private_files
from qorqut_system.errors import PrivateFileError
from qorqut.management.identity import is_active_installation_owner

logger = logging.getLogger(__name__)

MAX_BLOCKS = 200
MAX_TEXT = 100000
MAX_REQUEST_BYTES = 1024 * 1024
MAX_AGENTS = 100
MAX_STORE_BYTES = 32 * 1024 * 1024
LOCK_SECONDS = 10.0
STORE_VERSION = 1
MAX_STATUSES = 2000
SAVE_FIELDS = ('expected_revision', 'blocks')
OPTIONAL_SAVE_FIELDS = ('statuses',)
STATUSES = ('todo', 'white', 'in_progress', 'done', 'blocked', 'cancelled')
BLOCK_FIELDS = ('id', 'text', 'before_id', 'after_id')
FLAGS = ('include_archived', 'include_deferred')

# ASCII only: Python's \d and [a-fA-F] classes would admit lookalikes, and a canonical form keeps one identity per block.
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_AGENT = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


def _uuid(value):
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _text(value):
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        return False
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def _block_errors(blocks):
    """Field-by-field problems of a list of blocks; empty when it is acceptable. The text is never echoed."""
    if not isinstance(blocks, list):
        return {'blocks': 'must be a list'}
    if len(blocks) > MAX_BLOCKS:
        return {'blocks': f'must have at most {MAX_BLOCKS} blocks'}
    errors, ids = {}, set()
    for index, block in enumerate(blocks):
        where = f'blocks[{index}]'
        if not isinstance(block, dict):
            errors[where] = 'must be an object'
            continue
        errors.update({f'{where}.{name}': 'unknown field' for name in sorted(set(block) - set(BLOCK_FIELDS))})
        errors.update({f'{where}.{name}': 'required' for name in BLOCK_FIELDS if name not in block})
        if 'id' in block:
            if not _uuid(block['id']):
                errors[f'{where}.id'] = 'must be a lowercase UUID'
            elif block['id'] in ids:
                errors[f'{where}.id'] = 'must be unique'
            else:
                ids.add(block['id'])
        if 'text' in block and not _text(block['text']):
            errors[f'{where}.text'] = f'must be a text string of at most {MAX_TEXT} characters'
        for name in ('before_id', 'after_id'):
            if name in block and block[name] is not None and not _uuid(block[name]):
                errors[f'{where}.{name}'] = 'must be a lowercase UUID or null'
    if not errors:
        for index, block in enumerate(blocks):
            for name in ('before_id', 'after_id'):
                if block[name] in ids:
                    errors[f'blocks[{index}].{name}'] = 'must be a root Focus card, not a text block'
    return errors


def _status_errors(statuses):
    """Problems of a task-status map; empty when it is acceptable. The task text is never echoed."""
    if not isinstance(statuses, dict):
        return {'statuses': 'must be an object'}
    if len(statuses) > MAX_STATUSES:
        return {'statuses': f'must have at most {MAX_STATUSES} entries'}
    if not all(_text(text) for text in statuses):
        return {'statuses': f'must be keyed by task text of at most {MAX_TEXT} characters'}
    if not all(isinstance(status, str) and status in STATUSES for status in statuses.values()):
        return {'statuses': 'must hold only ' + ', '.join(STATUSES)}
    return {}


def validated_save(change):
    """The exact save request as ``(expected_revision, canonical blocks, statuses)``, or a field-by-field validation error.

    ``statuses`` may be left out, which means every task line is open."""
    if not isinstance(change, dict):
        raise DomainError('validation_error', 'Supply a Focus layout request object')
    errors = {name: 'unknown field' for name in sorted(set(change) - set(SAVE_FIELDS) - set(OPTIONAL_SAVE_FIELDS))}
    errors.update({name: 'required' for name in SAVE_FIELDS if name not in change})
    if 'expected_revision' in change and (type(change['expected_revision']) is not int or change['expected_revision'] < 0):
        errors['expected_revision'] = 'must be an integer of at least 0'
    if 'blocks' in change:
        errors.update(_block_errors(change['blocks']))
    if 'statuses' in change:
        errors.update(_status_errors(change['statuses']))
    if errors:
        raise DomainError('validation_error', 'Focus layout request fields are invalid', field_errors=errors)
    return (change['expected_revision'], [{name: block[name] for name in BLOCK_FIELDS} for block in change['blocks']],
            dict(change.get('statuses', {})))


def query_flags(pairs):
    """The archive and postpone filters of a request's query, each given once as 0 or 1 and defaulting to 0."""
    flags, seen = dict.fromkeys(FLAGS, False), set()
    for name, value in pairs:
        if name not in flags or name in seen or value not in ('0', '1'):
            raise DomainError('validation_error', 'The query accepts include_archived and include_deferred, each once, as 0 or 1',
                              field_errors={name: 'must be given once as 0 or 1' if name in flags else 'unknown parameter'})
        seen.add(name)
        flags[name] = value == '1'
    return flags


def _unreadable(reason):
    return DomainError('operation_failed', f'Saved Focus layouts {reason}; nothing was changed')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError('nonfinite number')


def _parse(data):
    try:
        document = json.loads(data, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _unreadable('are not valid JSON') from None
    if (not isinstance(document, dict) or set(document) != {'version', 'agents'} or type(document['version']) is not int
            or document['version'] != STORE_VERSION or not isinstance(document['agents'], dict)
            or len(document['agents']) > MAX_AGENTS):
        raise _unreadable('have an unrecognized layout')
    for agent_id, entry in document['agents'].items():
        if (not _uuid(agent_id) or not isinstance(entry, dict) or set(entry) - {'statuses'} != {'revision', 'blocks'}
                or type(entry['revision']) is not int or entry['revision'] < 1 or _block_errors(entry['blocks'])
                or _status_errors(entry.get('statuses', {}))):
            raise _unreadable('contain an invalid entry')
    return document['agents']


def _owner_name(owner_id):
    if not isinstance(owner_id, str) or not owner_id:
        raise DomainError('forbidden', 'Focus layouts are owner-only')
    return hashlib.sha256(owner_id.encode()).hexdigest()


def _open_directory(runtime_directory, *, create):
    """The preferences directory as a folder handle (every later access is relative to it), or None when absent and not created."""
    directory = Path(runtime_directory) / 'dashboard-preferences'
    try:
        folder = private_files.open_folder(directory, create=create)
    except FileNotFoundError:
        if create:
            raise _unreadable('cannot be stored') from None
        return None
    except OSError:
        logger.exception('Focus layout directory is not usable')
        raise _unreadable('cannot be reached') from None
    if not private_files.is_mine(private_files.stat_folder(folder)):
        folder.close()
        raise _unreadable('are in a directory owned by another user')
    return folder


def _load(directory, name):
    """The stored per-agent entries; empty only when the file does not exist. Anything unreadable is an error, never an empty set."""
    try:
        descriptor = private_files.open_read(directory, name)
    except FileNotFoundError:
        return {}
    except OSError:
        logger.exception('Focus layouts could not be opened')
        raise _unreadable('could not be opened') from None
    try:
        with os.fdopen(descriptor, 'rb') as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode) or not private_files.is_private_and_mine(status):
                raise _unreadable('are not a private regular file')
            data = stream.read(MAX_STORE_BYTES + 1)
    except OSError:
        logger.exception('Focus layouts could not be read')
        raise _unreadable('could not be read') from None
    if len(data) > MAX_STORE_BYTES:
        raise _unreadable('are larger than their limit')
    return _parse(data)


def _remove(directory, name):
    private_files.remove_entry(directory, name, missing_ok=True)


def _replace(directory, name, agents, prefix):
    """Write a private temporary file, flush it, swap it in atomically, then flush the directory entry.

    A failure before the swap changes nothing. Once the swap has happened the new store is visible, so a failure to
    flush the directory leaves the outcome uncertain rather than undone."""
    payload = json.dumps({'version': STORE_VERSION, 'agents': agents}, ensure_ascii=False, separators=(',', ':'),
                         sort_keys=True).encode('utf-8')
    if len(payload) > MAX_STORE_BYTES:
        raise DomainError('validation_error', 'The limit of saved Focus layout text is reached')
    try:
        private_files.replace_entry(directory, name, payload, mode=0o600, prefix=prefix)
    except OSError:
        logger.exception('Focus layouts could not be saved')
        raise _unreadable('could not be saved') from None
    try:
        private_files.sync_folder(directory)
    except OSError:
        logger.exception('Focus layout directory could not be flushed after the swap')
        raise DomainError('operation_failed', 'The Focus layout may have been saved but could not be confirmed durable; '
                          'reload it and retry', retryable=True) from None


@contextmanager
def _locked(directory, name):
    """Exclusive cross-process lock for one owner's store; a stuck holder becomes a retryable error, not a hung request."""
    try:
        handle = locks.open_lock(directory, name)
    except (OSError, PrivateFileError):
        logger.exception('Focus layout lock could not be opened')
        raise _unreadable('could not be locked') from None
    try:
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                handle.acquire(wait=False)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DomainError('busy', 'Focus layouts are being saved; retry shortly', retryable=True) from None
                time.sleep(0.002)
        try:
            yield
        finally:
            handle.release()
    finally:
        handle.close()


def _entry(agents, agent_id):
    entry = agents.get(agent_id)
    return (entry['revision'], entry['blocks'], entry.get('statuses', {})) if entry else (0, [], {})


def _public(revision, blocks, statuses):
    return {'revision': revision, 'blocks': [{name: block[name] for name in BLOCK_FIELDS} for block in blocks],
            'statuses': dict(statuses)}


def read_layout(runtime_directory, owner_id, agent_id):
    """The owner's saved layout of one agent. A missing store is the legitimate empty layout at revision 0; nothing is created."""
    prefix = _owner_name(owner_id)
    directory = _open_directory(runtime_directory, create=False)
    if directory is None:
        return _public(0, [], {})
    try:
        return _public(*_entry(_load(directory, prefix + '.focus-layouts.json'), agent_id))
    finally:
        directory.close()


def save_layout(runtime_directory, owner_id, agent_id, change):
    """Replace one agent's layout under the owner's lock, keeping every other agent's, and return the saved layout."""
    expected, blocks, statuses = validated_save(change)
    prefix = _owner_name(owner_id)
    directory = _open_directory(runtime_directory, create=True)
    try:
        with _locked(directory, prefix + '.focus-layouts.lock'):
            # Every writer of this owner's store holds the lock, so a leftover temporary file belongs to a crashed writer.
            temporary = '.' + prefix + '.focus-layouts-'
            for leftover in (name for name in private_files.list_entries(directory) if name.startswith(temporary)):
                _remove(directory, leftover)
            name = prefix + '.focus-layouts.json'
            agents = _load(directory, name)
            revision, stored, stored_statuses = _entry(agents, agent_id)
            if blocks == stored and statuses == stored_statuses:
                return _public(revision, stored, stored_statuses)
            if expected != revision:
                raise DomainError('revision_conflict', 'This layout changed elsewhere; reload it and try again',
                                  current_revision=revision)
            if agent_id not in agents and len(agents) >= MAX_AGENTS:
                raise DomainError('validation_error', f'The limit of {MAX_AGENTS} agents with saved Focus layouts is reached')
            # An emptied layout keeps its entry, so its revision never restarts and a stale save cannot reuse an old one.
            # The statuses key is written only when a status is set, so a store without any keeps its earlier shape.
            entry = {'revision': revision + 1, 'blocks': blocks, **({'statuses': statuses} if statuses else {})}
            _replace(directory, name, {**agents, agent_id: entry}, temporary)
            return _public(revision + 1, blocks, statuses)
    finally:
        directory.close()


def _owned_agent(runtime, ctx, agent_id):
    """The canonical target agent ID once the caller is the active installation owner and the target an active registered agent."""
    with runtime.database.read() as uow:
        ctx = runtime.application.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        if not is_active_installation_owner(uow, ctx.principal_id, ctx.principal_kind):
            raise DomainError('forbidden', 'Focus layouts are owner-only')
        if not isinstance(agent_id, str) or not _AGENT.fullmatch(agent_id):
            raise DomainError('not_found', 'Agent is unavailable')
        agent_id = agent_id.lower()
        if uow.connection.execute(
                "SELECT 1 FROM principals p JOIN agent_profiles a ON a.agent_id=p.id "
                "WHERE p.id=? AND p.kind='agent' AND p.active=1 AND a.status='active'", (agent_id,)).fetchone() is None:
            raise DomainError('not_found', 'Agent is unavailable')
    return agent_id


def card_ids(runtime, *, include_archived=False, include_deferred=False):
    """Every Focus card ID in board order that the archive and postpone filters keep, across all pages; reads nothing else."""
    clauses, params = [], []
    if not include_archived:
        clauses.append('archived=0')
    if not include_deferred:
        clauses.append('(deferred_until IS NULL OR deferred_until<=?)')
        params.append(timestamp(runtime.focus.inbox.clock))
    with runtime.database.read() as uow:
        return [row[0] for row in uow.connection.execute(
            'SELECT id FROM focus_cards' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY position,id', params)]


def layout(runtime, ctx, agent_id, change=None, *, include_archived=False, include_deferred=False):
    """The owner's layout of one agent with the matching Focus card IDs, or with `change` the layout after one save.

    The installation owner and the active registered target are verified before any file access."""
    agent_id = _owned_agent(runtime, ctx, agent_id)
    saved = (read_layout(runtime.paths.runtime, ctx.principal_id, agent_id) if change is None
             else save_layout(runtime.paths.runtime, ctx.principal_id, agent_id, change))
    return {'agent_id': agent_id, **saved,
            'card_ids': card_ids(runtime, include_archived=include_archived, include_deferred=include_deferred)}
