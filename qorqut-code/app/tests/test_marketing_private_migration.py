"""Migration 109 only adds the exact-peer binding; every existing Ordubashy conversation and every other schema
object and history row of the 108 database stays as it was."""
import hashlib
import sqlite3

import pytest

from bazarlyk.storage import Database
from foundation_support import BUSINESS, HUMAN

LEGACY_KEY = 'source:%s:task:existing-task' % BUSINESS
# Main's migrations 1..107 plus the Agent Viewbox 108, exactly as merged on main; none of them may ever change.
MAIN_CHAIN_SHA256 = 'a73cbf1ec85a12f945e385d8725caebcd5099b6fed4e9115b6d70cf2e304795f'
VIEWBOX_108_SHA256 = '6e034f1ee4bbe8fb61e5aa41849e262874c4e1fb6fa27f75330f130e0c554858'
MIGRATION_109_SHA256 = '3a9c8513dc20ccc5a3fc68c340ce97ad7096064f5aa2bfcfbae73a4ea9abee0c'
AGENT = 'agent-with-a-board'


def test_109_is_contiguous_additive_and_adds_only_the_nullable_peer_binding():
    migrations = Database.migrations()
    assert [version for version, _, _ in migrations] == list(range(1, len(migrations) + 1))
    # The first migration after main's 1..108 is the next contiguous number, not the provisional 104.
    chain = '\n'.join(f'{version}:{checksum}' for version, checksum, _ in migrations[:108])
    assert hashlib.sha256(chain.encode()).hexdigest() == MAIN_CHAIN_SHA256
    assert migrations[107][:2] == (108, VIEWBOX_108_SHA256)
    version, digest, sql = migrations[108]
    assert (version, digest) == (109, MIGRATION_109_SHA256)
    code = ' '.join(line.strip() for line in sql.splitlines() if line.strip() and not line.strip().startswith('--'))
    statements = [statement.strip() for statement in code.split(';') if statement.strip()]
    assert statements == [
        'ALTER TABLE owner_conversations ADD COLUMN peer_agent_id TEXT REFERENCES principals(id)',
        'CREATE INDEX owner_conversation_peer ON owner_conversations(peer_agent_id,source_type,source_id)']


def seed_108(database):
    """A private synthetic schema-108 database: legacy private rows and cursors plus Viewbox data from 108."""
    with database.transaction() as u:
        u.repository('principals').insert({'id': HUMAN, 'kind': 'human', 'display_name': 'Owner'})
        u.repository('principals').insert({'id': AGENT, 'kind': 'agent', 'display_name': 'Agent'})
        u.repository('businesses').insert({'id': BUSINESS, 'slug': 'first', 'name': 'first',
                                           'workspace': '/private/first', 'hq_path': '/private/first/hq'})
        u.repository('business_memberships', business_id=BUSINESS).insert({'principal_id': HUMAN, 'is_owner': True})
        u.repository('tasks', business_id=BUSINESS).insert({
            'id': 'existing-task', 'title': 'Existing task', 'status': 'todo', 'created_by': HUMAN})
        u.connection.execute("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,"
            "audience,created_at,updated_at,latest_sequence) VALUES('legacy',?,?,'task','existing-task',"
            "'owner_ordubashy','t','t',2)", (LEGACY_KEY, BUSINESS))
        for identity, sequence, author, body in (('m1', 1, HUMAN, 'Private text'), ('m2', 2, HUMAN, 'Later private text')):
            u.connection.execute("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,"
                "body,created_at) VALUES(?,'legacy',?,'owner',?,'t')", (identity, author, body))
        u.connection.execute("INSERT INTO owner_conversation_reads(conversation_id,principal_id,last_read_sequence,"
            "updated_at) VALUES('legacy',?,1,'t')", (HUMAN,))
        u.connection.execute("INSERT INTO agent_viewbox_boards(agent_id,sequence,updated_at) VALUES(?,1,'t')", (AGENT,))
        u.connection.execute("INSERT INTO agent_viewbox_items(id,agent_id,canonical_link,link,title,created_at,placed_at,"
            "placement_sequence) VALUES('v1',?,'https://example.com/a','https://example.com/A','Page','t','t',1)", (AGENT,))


def snapshot(connection):
    """Every table's rows (by name) except the two this migration may touch, plus the recorded schema history."""
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY 1, 2')]
            for name in names if name != 'owner_conversations'}


def test_upgrade_from_108_keeps_legacy_conversations_unbound_and_enforces_peer_identity(tmp_path):
    database = Database(tmp_path / 'state.sqlite3').initialize(through=108)
    seed_108(database)
    with database.read() as u:
        assert u.connection.execute('PRAGMA user_version').fetchone()[0] == 108
        assert not [row for row in u.connection.execute('PRAGMA table_info(owner_conversations)')
                    if row['name'] == 'peer_agent_id']
        before = dict(u.connection.execute('SELECT * FROM owner_conversations').fetchone())
        others_before = snapshot(u.connection)
        history_before = [tuple(row) for row in u.connection.execute(
            'SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version')]
        schema_before = {row['name']: row['sql'] for row in u.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT IN "
            "('owner_conversations')")}
    assert len(history_before) == 108 and history_before[-1][:2] == (108, VIEWBOX_108_SHA256)

    database.initialize()
    with database.read() as u:
        after = dict(u.connection.execute('SELECT * FROM owner_conversations').fetchone())
        column = next(row for row in u.connection.execute('PRAGMA table_info(owner_conversations)')
                      if row['name'] == 'peer_agent_id')
        assert (column['notnull'], column['dflt_value']) == (0, None)
        assert after.pop('peer_agent_id') is None and after == before
        # Legacy messages, read cursors and the Viewbox board/items written at 108 are untouched.
        assert [row[0] for row in u.connection.execute('SELECT body FROM owner_conversation_messages ORDER BY id')] == [
            'Private text', 'Later private text']
        assert u.connection.execute('SELECT last_read_sequence FROM owner_conversation_reads').fetchone()[0] == 1
        unchanged = lambda rows: {name: value for name, value in rows.items() if name != 'schema_migrations'}
        assert unchanged(snapshot(u.connection)) == unchanged(others_before)
        assert u.connection.execute('SELECT link FROM agent_viewbox_items').fetchone()[0] == 'https://example.com/A'
        assert u.connection.execute('SELECT sequence FROM agent_viewbox_boards').fetchone()[0] == 1
        # Schema history: 1..108 rows (checksums and applied_at) are identical and exactly one row, 109, is new.
        history = [tuple(row) for row in u.connection.execute(
            'SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version')]
        assert history[:108] == history_before and [row[:2] for row in history[108:]] == [(109, MIGRATION_109_SHA256)]
        # Every other schema object's SQL is unchanged; only the new index exists besides the altered table.
        schema_after = {row['name']: row['sql'] for row in u.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT IN "
            "('owner_conversations')")}
        assert {name: sql for name, sql in schema_after.items() if name != 'owner_conversation_peer'} == schema_before
        assert u.connection.execute("SELECT count(*) FROM sqlite_master WHERE type='index' "
                                    "AND name='owner_conversation_peer'").fetchone()[0] == 1
        assert u.connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert u.connection.execute('PRAGMA user_version').fetchone()[0] == len(Database.migrations())
    # A bound row must name a real principal; the legacy source key stays unique, so a peer row needs its own key.
    with database.transaction() as u:
        with pytest.raises(sqlite3.IntegrityError):
            u.connection.execute("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,"
                "audience,created_at,updated_at,peer_agent_id) VALUES('p1','peer:missing:' || ?,?,'task','existing-task',"
                "'owner_ordubashy','t','t','missing')", (LEGACY_KEY, BUSINESS))
    with database.transaction() as u:
        with pytest.raises(sqlite3.IntegrityError):
            u.connection.execute("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,"
                "audience,created_at,updated_at,peer_agent_id) VALUES('p2',?,?,'task','existing-task',"
                "'owner_ordubashy','t','t',?)", (LEGACY_KEY, BUSINESS, HUMAN))
    # Re-running initialization validates checksums without replaying the migration: history and rows are unchanged.
    with database.read() as u:
        history_109 = [tuple(row) for row in u.connection.execute(
            'SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version')]
    database.initialize()
    with database.read() as u:
        assert u.connection.execute('SELECT count(*) FROM owner_conversations').fetchone()[0] == 1
        assert [tuple(row) for row in u.connection.execute(
            'SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version')] == history_109
        assert u.connection.execute('PRAGMA user_version').fetchone()[0] == len(history_109)
