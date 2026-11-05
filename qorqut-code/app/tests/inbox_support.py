"""Helpers for inbox tests. The poller (``watch``/``release``) is gone; a test now reads the pending view or asks the
current-owner question directly; a fixture that needs a session to own the inbox claims it with ``claim``."""

import json


def pending(service, ctx):
    """Every currently authorized pending event of the context's agent, oldest first (the inbox_list view, paged to the end)."""
    items, after = [], 0
    with service.app.database.read() as uow:
        ctx = service.context(ctx, uow)
        while True:
            batch = service.pending(ctx, uow, after=after, limit=100)
            items += batch['items']
            if batch['next_cursor'] is None:
                return items
            after = batch['next_cursor']


def pending_count(service, ctx, _consumer=None):
    return len(pending(service, ctx))


def pending_ids(service, ctx, _consumer=None):
    return [item['id'] for item in pending(service, ctx)]


def owner(service, ctx, consumer):
    """The check a session-bound call makes: this exact consumer and generation own the agent's native session."""
    with service.app.database.read() as uow:
        return service.session_owner(service.context(ctx, uow), consumer, uow)


def claim(service, ctx, consumer, generation=1):
    """Make ``consumer`` the native session owner of the context's agent inbox at ``generation`` with a synthetic binding
    (the table refuses an empty binding or a zero generation, as the verified native binding always supplies both)."""
    binding = json.dumps({"session_id": consumer, "agent_id": ctx.principal_id, "owner": "1:1", "execution_target": "local"})
    with service.app.database.transaction() as uow:
        uow.connection.execute('INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation) VALUES(?,?,?,?) '
            'ON CONFLICT(agent_id) DO UPDATE SET consumer_id=excluded.consumer_id,binding_json=excluded.binding_json,'
            'generation=excluded.generation', (ctx.principal_id, consumer, binding, generation))
