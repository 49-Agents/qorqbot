from bazarlyk.storage import Database
from foundation_support import BUSINESS, HUMAN, database_at


def test_upgrade_preserves_existing_records_without_historical_notifications(tmp_path, monkeypatch):
    migrations = Database.migrations()
    inbox_index = next(i for i, migration in enumerate(migrations) if 'CREATE TABLE inbox_events' in migration[2])
    with monkeypatch.context() as patch:
        patch.setattr(Database, 'migrations', staticmethod(lambda: migrations[:inbox_index]))
        database = database_at(tmp_path)
        with database.transaction() as u:
            u.repository('tasks', business_id=BUSINESS).insert({
                'id': 'existing-task', 'title': 'Existing task', 'status': 'todo', 'created_by': HUMAN})
    database.initialize()
    with database.read() as u:
        assert u.repository('tasks', business_id=BUSINESS).get('existing-task')['title'] == 'Existing task'
        assert u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0] == 0
        assert u.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0] == 0
        assert u.connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert u.connection.execute('PRAGMA user_version').fetchone()[0] == len(migrations)
        assert u.connection.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'inbox_capture_%'").fetchone()[0] == 59  # +outreach_events (076)
    # Re-running initialization validates checksums without replaying migrations.
    database.initialize()
