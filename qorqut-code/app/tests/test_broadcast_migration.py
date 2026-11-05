import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.storage import Database
from foundation_support import AGENT, BUSINESS, HUMAN, database_at


def test_upgrade_preserves_message_threads_and_allows_self_reminder(tmp_path, monkeypatch):
    migrations = Database.migrations()
    old = [entry for entry in migrations if entry[0] < 57]
    with monkeypatch.context() as patch:
        patch.setattr(Database, 'migrations', staticmethod(lambda: old))
        db = database_at(tmp_path)
        first, reply = new_id(), new_id()
        with db.transaction() as uow:
            uow.repository('principals').insert({'id': AGENT, 'kind': 'agent', 'display_name': 'Agent'})
            for identity, sender, recipient, parent in [(first, HUMAN, AGENT, None), (reply, AGENT, HUMAN, first)]:
                event = uow.connection.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (BUSINESS, 'agent_messages', identity, sender, 'direct_message', 'Original', '2026-01-01T00:00:00Z')).lastrowid
                uow.connection.execute('INSERT INTO agent_messages VALUES(?,?,?,?,?,?,?,?,?)',
                    (identity, event, BUSINESS, sender, recipient, parent, first, 'Original content', '2026-01-01T00:00:00Z'))
        with db.read() as uow:
            before = [tuple(row) for row in uow.connection.execute('SELECT * FROM agent_messages ORDER BY event_id')]
    db.initialize()
    with db.read() as uow:
        assert [tuple(row) for row in uow.connection.execute('SELECT * FROM agent_messages ORDER BY event_id')] == before
        assert uow.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    with db.transaction() as uow:
        identity = new_id()
        event = uow.connection.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (BUSINESS, 'agent_messages', identity, AGENT, 'direct_message', 'Self reminder', '2026-01-02T00:00:00Z')).lastrowid
        uow.connection.execute('INSERT INTO agent_messages VALUES(?,?,?,?,?,?,?,?,?)',
            (identity, event, BUSINESS, AGENT, AGENT, None, identity, 'Self reminder', '2026-01-02T00:00:00Z'))
    with pytest.raises(DomainError):
        with db.transaction() as uow:
            uow.connection.execute('UPDATE agent_messages SET body=? WHERE id=?', ('Changed', first))
    db.initialize()
