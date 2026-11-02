"""Owner's dismissals of exact Viewbox placements from the Focus queue. A preference only: it never edits a board or its source.

A dismissal names one placement (``viewbox:{item id}:{placement sequence}``), so placing the same link again is a new
placement and reappears. It is kept apart from the owner's viewed acknowledgements, pins and every operational record.
"""
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from qorqut_operations.contracts import DomainError

MAX_ATTENTION_ID = 200
MAX_PER_AGENT = 200


def _path(runtime_directory, owner_id):
    # Owner identity comes from the authenticated installation, never the URL.
    return Path(runtime_directory) / 'dashboard-preferences' / (hashlib.sha256(owner_id.encode()).hexdigest() + '.viewbox-dismissed.json')


def _load(path):
    return json.loads(path.read_text()) if path.exists() else {}


def dismissed(runtime_directory, owner_id, agent_id):
    """The agent's saved dismissals. Writes replace the file atomically, so a read needs no lock and creates nothing."""
    return sorted(_load(_path(runtime_directory, owner_id)).get(agent_id, ()))


def revision(ids):
    """A short digest of the saved dismissals, for the board's entity tag."""
    return hashlib.sha256('\n'.join(sorted(ids)).encode()).hexdigest()[:16]


def board_with_dismissals(viewbox, runtime_directory, ctx, agent_id):
    """The owner's full board plus this owner's dismissed attention IDs for that agent."""
    board = viewbox.owner_board(ctx, agent_id)
    return {**board, 'dismissed_attention_ids': dismissed(runtime_directory, ctx.principal_id, board['agent_id'])}


def dismiss(viewbox, runtime_directory, ctx, agent_id, body):
    """Add one exact placement to the owner's dismissals for an agent. Idempotent; a retry of a saved ID succeeds."""
    if not isinstance(body, dict) or set(body) != {'attention_id'}:
        raise DomainError('validation_error', 'Supply exactly an attention_id')
    attention_id = body['attention_id']
    if not isinstance(attention_id, str) or not 1 <= len(attention_id) <= MAX_ATTENTION_ID:
        raise DomainError('validation_error', 'Supply the placement attention_id')
    board = viewbox.owner_board(ctx, agent_id)
    agent_id = board['agent_id']
    current = {item['attention_id'] for item in board['items']}
    path = _path(runtime_directory, ctx.principal_id)
    saved = _load(path).get(agent_id, ())
    if attention_id in saved:
        return {'agent_id': agent_id, 'dismissed_attention_ids': sorted(saved)}
    if attention_id not in current:
        raise DomainError('revision_conflict', 'That Viewbox placement is no longer current')
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        everything = _load(path)
        ids = list(everything.get(agent_id, ()))
        if attention_id not in ids:
            ids.append(attention_id)
            if len(ids) > MAX_PER_AGENT:
                # A replaced placement never returns, so the oldest ones off the board are the safe ones to forget.
                stale = [x for x in ids if x not in current][:len(ids) - MAX_PER_AGENT]
                ids = [x for x in ids if x not in stale]
            fd, temporary = tempfile.mkstemp(dir=directory, prefix='.viewbox-dismissed-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({**everything, agent_id: ids}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
    return {'agent_id': agent_id, 'dismissed_attention_ids': sorted(ids)}
