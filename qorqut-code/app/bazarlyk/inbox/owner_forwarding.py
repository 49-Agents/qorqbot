"""A private notice to Ordubashy of each accepted owner message to a worker.

The owner may message a worker directly. Ordubashy manages the team, so the complete accepted message also reaches his
private inbox, with the worker, the original input and its time. The notice is a plain `agent_messages` row with its
inbox event and recipient, written in the same transaction that accepted the original. Its identity is derived from the
source kind, the original durable message or input ID and the manager, so the existing primary key is the only guard
against a second copy: there is no ledger. The inbox summary is fixed. The body never reaches an event summary, a Task
thread, shared status, Context or operational history.

Only an accepted owner message to an active worker is forwarded. Agent-authored messages, broadcasts, command pings, raw
terminal input and messages to Ordubashy or Learning Applier never are, and the notice is written here rather
than through a send operation, so it cannot be forwarded again.
"""

import uuid

from bazarlyk.management.identity import configured_ordubashy, installation_owner

NOTICE_SUMMARY = 'Owner message to a worker'
NAMESPACE = uuid.UUID('4f0d6d5e-8f55-4c4e-9c5a-6a1e0b3f2d17')
# The original acceptance point, named for the reader of the notice.
KINDS = {'chat': 'Chat input', 'agent_message': 'inbox message', 'private_conversation': 'private Task conversation message'}
# The wrapper (everything but the owner's text) stays well inside WRAPPER_LIMIT: fixed text, two UUIDs, a timestamp and
# a display name cut at NAME_LIMIT. The original body is never cut.
WRAPPER_LIMIT = 1000
NAME_LIMIT = 100


def forwarded_id(kind, source_id, manager_id):
    return str(uuid.uuid5(NAMESPACE, ':'.join((kind, source_id, manager_id))))


def notice_body(kind, source_id, worker_id, worker_name, accepted_at, body):
    header = (f'Forwarded notice: the owner sent a direct message to the worker {worker_name[:NAME_LIMIT]} (agent {worker_id}). '
              'This only keeps you informed; the worker received the original and this is not a second assignment for you to execute.\n'
              f'Original: {KINDS[kind]} {source_id}, accepted {accepted_at}.\n\nOwner message:\n')
    return header + body


def write_notice(connection, *, kind, source_id, sender_id, manager_id, worker_id, body, accepted_at):
    """Insert the notice for one accepted message, once; return its ID."""
    identity = forwarded_id(kind, source_id, manager_id)
    if connection.execute('SELECT 1 FROM agent_messages WHERE id=?', (identity,)).fetchone():
        return identity
    worker = connection.execute('SELECT display_name FROM principals WHERE id=?', (worker_id,)).fetchone()
    event_id = connection.execute(
        'INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?)',
        (None, 'agent_messages', identity, sender_id, 'direct_message', NOTICE_SUMMARY, accepted_at)).lastrowid
    connection.execute(
        'INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,summary,'
        'current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (identity, event_id, None, sender_id, manager_id, None, identity,
         notice_body(kind, source_id, worker_id, worker['display_name'] if worker else worker_id, accepted_at, body),
         accepted_at, None, sender_id, manager_id))
    connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (manager_id, event_id))
    return identity


def forward_owner_message(uow, *, kind, source_id, sender_id, worker_id, body, accepted_at):
    """Note one accepted message to Ordubashy unless it is not an owner message to an active worker; return the notice ID or None."""
    manager_id = configured_ordubashy(uow, require_codex=False)
    if manager_id is None or sender_id != installation_owner(uow):
        return None
    from bazarlyk.agents.worker_access import is_worker
    if not is_worker(uow.connection, worker_id):
        return None
    return write_notice(uow.connection, kind=kind, source_id=source_id, sender_id=sender_id, manager_id=manager_id,
                        worker_id=worker_id, body=body, accepted_at=accepted_at)
