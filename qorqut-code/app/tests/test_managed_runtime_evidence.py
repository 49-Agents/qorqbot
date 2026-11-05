import sqlite3

import pytest
from qorqut_agentic.store import DDL
from qorqut_operations.contracts import DomainError
from bazarlyk.agentic.managed_runtime_evidence import snapshot


def test_runtime_protected_projection_isolates_only_reviewed_effects():
    conn = sqlite3.connect(':memory:')
    try:
        conn.executescript(DDL)
        conn.execute('CREATE TABLE synthetic_extra (value BLOB)')
        conn.execute("INSERT INTO synthetic_extra VALUES (X'0102')")
        conn.execute("INSERT INTO meta VALUES ('identity','synthetic')")
        conn.execute("INSERT INTO inventory VALUES ('roles/example/file.md', ?, 3)", ('a' * 64,))
        conn.execute("INSERT INTO inventory VALUES ('plugins/other/file.md', ?, 4)", ('b' * 64,))
        conn.execute("INSERT INTO consumers VALUES ('synthetic', 1, 'initial')")
        old_id = 'chg_' + 'b' * 32
        conn.execute("INSERT INTO changes(change_id,created_at,kind,actor_id,actor_kind,reason,source_refs,trust,package_ids,entry_refs,details) VALUES (?, 'time','external_scan','actor','human','reason','[]','host_installer','[]','[]','{}')", (old_id,))
        conn.execute("INSERT INTO change_files VALUES (?,0,'plugins/other/file.md','edit',?,?,NULL)", (old_id, 'd' * 64, 'b' * 64))
        changed = ['roles/example/file.md']
        change_id = 'chg_' + 'a' * 32
        before = snapshot(conn, changed, change_id)
        assert before['changed_inventory_before'] == {changed[0]: {'sha256': 'a' * 64, 'bytes': 3}}
        empty = snapshot(conn, [], None)
        assert empty['tables'] == empty['protected']
        assert snapshot(conn, [changed[0], 'plugins/new/file.md'], change_id)['changed_inventory_before']['plugins/new/file.md'] is None
        conn.execute("UPDATE inventory SET sha256=?,size=5 WHERE path=?", ('c' * 64, changed[0]))
        conn.execute("INSERT INTO changes(change_id,created_at,kind,actor_id,actor_kind,reason,source_refs,trust,package_ids,entry_refs,details) VALUES (?, 'time','external_scan','actor','human','reason','[]','host_installer','[]','[]','{}')", (change_id,))
        conn.execute("INSERT INTO change_files VALUES (?,0,?,'edit',?,?,NULL)", (change_id, changed[0], 'a' * 64, 'c' * 64))
        with pytest.raises(DomainError, match='already exists'):
            snapshot(conn, changed, change_id)
        after = snapshot(conn, changed, change_id, require_unused=False)
        assert after['schema_sha256'] == before['schema_sha256']
        assert after['protected'] == before['protected']
        assert after['tables']['inventory'] != before['tables']['inventory']
        for statement in ("UPDATE meta SET value='changed' WHERE key='identity'",
                          "UPDATE consumers SET cursor=2 WHERE name='synthetic'",
                          "UPDATE inventory SET size=5 WHERE path='plugins/other/file.md'",
                          "UPDATE synthetic_extra SET value=X'0103'",
                          "UPDATE change_files SET commit_id='unexpected' WHERE path='plugins/other/file.md'",
                          "UPDATE sqlite_sequence SET seq=3 WHERE name='changes'"):
            conn.execute('SAVEPOINT probe')
            conn.execute(statement)
            result = snapshot(conn, changed, change_id, require_unused=False)
            # Changes' own sequence is intentionally isolated; its exact old value is separate evidence.
            if 'sqlite_sequence' in statement:
                assert result['protected'] == before['protected']
                assert result['changes_sequence_before'] == 3
            else:
                assert result['protected'] != before['protected']
            conn.execute('ROLLBACK TO probe')
            conn.execute('RELEASE probe')
        conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('other',1)")
        assert snapshot(conn, changed, change_id, require_unused=False)['protected'] != before['protected']
        with pytest.raises(DomainError):
            snapshot(conn, ['../unsafe'], change_id)
    finally:
        conn.close()
