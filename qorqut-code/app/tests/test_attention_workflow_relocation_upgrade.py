"""Synthetic checks for Attention task references and completion backfill."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import crm_baseline_store
import pytest
from bazarlyk.domains.tasks.relocation import HostRelocation
from bazarlyk.migration.crm_upgrade import backfill_latest_completion, recognized_baseline, plan, build
from qorqut_operations.storage.assembly import SchemaTarget
from qorqut_operations.storage.inventory import read_objects, compile_target
from qorqut_operations.contracts import DomainError
from bazarlyk.migration.crm_upgrade import _digest
from bazarlyk.migration import crm_upgrade
from qorqut_operations.tasks.relocation import Relocation


def test_attention_references_follow_move_and_fingerprint_changes():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE attention_memberships(id TEXT, agent_id TEXT, task_business_id TEXT, task_id TEXT,
            task_created_at TEXT, revision INTEGER);
        CREATE TABLE agent_attention(agent_id TEXT, revision INTEGER, focus_json TEXT, work_task_json TEXT, updated_at TEXT);
        CREATE TABLE attention_workflows(agent_id TEXT, run_id TEXT, revision INTEGER, work_task_json TEXT,
            related_refs_json TEXT, updated_at TEXT);
        CREATE TABLE attention_membership_events(id TEXT, task_business_id TEXT, task_id TEXT, task_created_at TEXT);
    """)
    task = {"type": "task", "id": "t", "business_id": None}
    other = {"type": "task", "id": "t", "business_id": "other"}
    db.execute("INSERT INTO attention_memberships VALUES('m','a',NULL,'t','birth',1)")
    db.execute("INSERT INTO agent_attention VALUES('a',1,?,?, 'old')", (json.dumps(task), json.dumps(task)))
    db.execute("INSERT INTO attention_workflows VALUES('a','r',1,?,?, 'old')", (json.dumps(task), json.dumps([task, other])))
    db.execute("INSERT INTO attention_membership_events VALUES('audit',NULL,'t','birth')")
    uow = SimpleNamespace(connection=db)
    move = Relocation(None, "dest", ("t",), ("t",))
    adapter = HostRelocation()
    before = adapter.inspect(uow, move)
    assert before.counts["attention_references"] == 3 and not before.blockers
    db.execute("UPDATE attention_workflows SET revision=2 WHERE run_id='r'")
    assert adapter.inspect(uow, move).material["attention"] != before.material["attention"]
    assert adapter.apply(uow, move, "now")["attention_references"] == 3
    assert tuple(db.execute("SELECT task_business_id,task_created_at,revision FROM attention_memberships").fetchone()) == ("dest", "birth", 2)
    focus = db.execute("SELECT * FROM agent_attention").fetchone()
    assert json.loads(focus["focus_json"])["business_id"] == "dest" and focus["revision"] == 2
    run = db.execute("SELECT * FROM attention_workflows").fetchone()
    assert json.loads(run["work_task_json"])["business_id"] == "dest"
    assert [ref["business_id"] for ref in json.loads(run["related_refs_json"])] == ["dest", "other"]
    assert run["revision"] == 3
    assert tuple(db.execute("SELECT task_business_id,task_created_at FROM attention_membership_events").fetchone()) == (None, "birth")


def test_destination_attention_membership_collision_blocks_move():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE attention_memberships(id TEXT, agent_id TEXT, task_business_id TEXT, task_id TEXT, "
               "task_created_at TEXT, revision INTEGER)")
    db.execute("INSERT INTO attention_memberships VALUES('source','a',NULL,'t','birth',1)")
    db.execute("INSERT INTO attention_memberships VALUES('destination','a','dest','t','other-birth',1)")
    result = HostRelocation().inspect(SimpleNamespace(connection=db), Relocation(None, "dest", ("t",), ("t",)))
    assert [entry["code"] for entry in result.blockers] == ["attention_membership_exists"]


def test_backfill_uses_done_transition_or_stable_fallback():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE tasks(business_id TEXT,id TEXT,status TEXT,created_at TEXT,latest_completed_at TEXT);
        CREATE TABLE thread_messages(business_id TEXT,item_type TEXT,item_id TEXT,kind TEXT,change_json TEXT,created_at TEXT);
        INSERT INTO tasks VALUES(NULL,'a','done','2020-01-01',NULL),('p','b','done','2020-01-02',NULL),
                                (NULL,'c','open','2020-01-03',NULL);
        INSERT INTO thread_messages VALUES(NULL,'task','a','system','{"from":"open","to":"done"}','2020-02-01'),
            (NULL,'task','a','system','{"from":"working","to":"done"}','2020-04-01'),
            (NULL,'task','a','comment','{"from":"open","to":"done"}','2020-05-01'),
            ('other','task','b','system','{"from":"open","to":"done"}','2020-06-01');
    """)
    assert backfill_latest_completion(db) == {"history": 1, "fallback": 1}
    assert db.execute("SELECT id,latest_completed_at FROM tasks ORDER BY id").fetchall() == [
        ("a", "2020-04-01"), ("b", "2020-01-02"), ("c", None)]


@pytest.mark.parametrize(('fixture_name', 'extensions'), [
    ('attention_baseline_schema.json', []),
    ('attention_extension_baseline_schema.json', ['content', 'fastmail', 'paper']),
])
def test_frozen_baseline_is_recognized_and_schema_tampering_is_refused(tmp_path, monkeypatch, fixture_name, extensions):
    fixture = json.loads((Path(__file__).parent / 'fixtures' / fixture_name).read_text())
    monkeypatch.setattr(crm_baseline_store, "definition", lambda: fixture)
    db = crm_baseline_store.create(tmp_path / "baseline.sqlite3")
    target = SimpleNamespace(name="future", version=2, digest="future")
    recognized = recognized_baseline(db, target, extensions)
    assert recognized["target"] == fixture["target"]
    assert recognized["objects"] == {key: (owner, _digest(sql)) for key, (owner, sql) in read_objects(db).items()}
    db.execute("CREATE TABLE unexpected(value TEXT)")
    with pytest.raises(DomainError) as error:
        recognized_baseline(db, target, extensions)
    assert error.value.code == "upgrade_source_unrecognized"
    db.close()


def test_offline_build_preserves_baseline_and_backfills_synthetic_target(tmp_path, monkeypatch):
    fixture = json.loads((Path(__file__).parent / 'fixtures/attention_baseline_schema.json').read_text())
    monkeypatch.setattr(crm_baseline_store, 'definition', lambda: fixture)
    source = tmp_path / 'source.sqlite3'
    db = crm_baseline_store.create(source)
    crm_baseline_store.populate(db)
    db.execute("UPDATE tasks SET status='done' WHERE id='task-g'")
    db.execute("UPDATE tasks SET status='done' WHERE id='task-a'")
    db.execute("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,change_json,created_at) "
               "VALUES(?, 'done-history', 'task', 'task-a', 'system', 'State changed', ?, ?, ?)",
               (crm_baseline_store.BUSINESS, crm_baseline_store.OWNER,
                json.dumps({'from': 'working', 'to': 'done'}), '2026-05-01T00:00:00Z'))
    frozen = recognized_baseline(db, SimpleNamespace(name='future', version=2, digest='future'), [])
    schema_rows = []
    for owner, statements in fixture['schema']:
        changed = []
        for statement in statements:
            if statement.startswith('CREATE TABLE tasks ('):
                statement = statement.replace("status IN ('open','working','waiting','done','cancelled')",
                                              "status IN ('open','working','waiting','done','cancelled','blocked')")
                statement = statement.replace('    parent_id TEXT,',
                    "    worker_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(worker_ids_json)),\n"
                    "    latest_completed_at TEXT,\n    parent_id TEXT,")
            changed.append(statement)
        schema_rows.append((owner, tuple(changed)))
    # These placeholders exercise the generic upgrade only; the real target is composed by companion commits.
    names = ('agent_attention', 'attention_memberships', 'attention_workflows', 'attention_reminders', 'attention_membership_events')
    schema_rows.append(('synthetic_attention', tuple(f'CREATE TABLE {name}(id TEXT PRIMARY KEY)' for name in names)))
    assembly = SimpleNamespace(schema=tuple(schema_rows), tables={}, immutable_fields={}, required_tables={},
                               target=SchemaTarget('synthetic-attention', 1, 'synthetic'))
    compiled = compile_target(assembly)
    steps = plan(db, frozen, compiled, assembly)
    db.close()
    rows, backfill = build(source, tmp_path / 'upgraded.sqlite3', assembly, compiled, steps)
    assert backfill == {'history': 1, 'fallback': 1} and rows['tasks'] == 3
    assert set(steps['new_tables']) == set(names)
    with sqlite3.connect(tmp_path / 'upgraded.sqlite3') as upgraded:
        assert upgraded.execute("SELECT latest_completed_at FROM tasks WHERE id='task-a'").fetchone()[0] == '2026-05-01T00:00:00Z'
        assert upgraded.execute("SELECT latest_completed_at FROM tasks WHERE id='task-g'").fetchone()[0] == crm_baseline_store.STAMP
        assert upgraded.execute("SELECT DISTINCT worker_ids_json FROM tasks").fetchone()[0] == '[]'


@pytest.mark.parametrize(('fixture_name', 'extensions'), [
    ('attention_baseline_schema.json', []),
    ('attention_extension_baseline_schema.json', ['content', 'fastmail', 'paper']),
])
def test_real_composed_target_upgrades_recognized_source(tmp_path, monkeypatch, fixture_name, extensions):
    fixture = json.loads((Path(__file__).parent / 'fixtures' / fixture_name).read_text())
    monkeypatch.setattr(crm_baseline_store, 'definition', lambda: fixture)
    source = tmp_path / 'source.sqlite3'
    db = crm_baseline_store.create(source)
    crm_baseline_store.populate(db)
    db.execute("INSERT INTO global_chat_bindings(agent_id,runtime,native_session_id,native_runtime,native_state,created_at,updated_at) "
               "VALUES(?, 'codex', 'synthetic-session', 'codex', 'confirmed', ?, ?)",
               (crm_baseline_store.WORKER, crm_baseline_store.STAMP, crm_baseline_store.STAMP))
    db.execute("UPDATE tasks SET status='done' WHERE id IN ('task-g','task-a')")
    db.execute("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,change_json,created_at) "
               "VALUES(?, 'done-history', 'task', 'task-a', 'system', 'State changed', ?, ?, ?)",
               (crm_baseline_store.BUSINESS, crm_baseline_store.OWNER,
                json.dumps({'from': 'working', 'to': 'done'}), '2026-05-01T00:00:00Z'))
    before = crm_baseline_store.tables(db)
    db.close()
    source_hash = crm_upgrade.file_sha256(source)
    config = {'extensions': [{'factory': f'qorqut_{name}.extension:create', **({'config': {
        'engine_url': 'http://127.0.0.1:1', 'admin_token_file': str(tmp_path / 'absent-token')}}
        if name == 'fastmail' else {})} for name in extensions]}
    target = crm_upgrade.current_assembly(config)
    destination = tmp_path / 'target.sqlite3'
    report = crm_upgrade.upgrade(source, destination, assembly=target, configuration=config)
    assert report['checks'] == {'source_recognized': True, 'schema': 'equal', 'rows': 'identical',
                                'foreign_keys': 'ok', 'integrity': 'ok'}
    assert report['latest_completion_backfill'] == {'history': 1, 'fallback': 1}
    assert report['target']['digest'] == target.target.digest
    assert crm_upgrade.file_sha256(source) == source_hash
    assert set(report['new_tables']) == {'agent_attention', 'attention_memberships', 'attention_workflows',
                                         'attention_reminders', 'attention_membership_events'}
    with sqlite3.connect(destination) as upgraded:
        assert read_objects(upgraded) == compile_target(target).objects
        after = crm_baseline_store.tables(upgraded)
        for name, rows in before.items():
            if name not in ('tasks', 'target_identity'):
                assert after[name] == rows, name
        assert upgraded.execute("SELECT latest_completed_at,worker_ids_json FROM tasks WHERE id='task-a'").fetchone() == ('2026-05-01T00:00:00Z', '[]')
        assert upgraded.execute("SELECT latest_completed_at,worker_ids_json FROM tasks WHERE id='task-g'").fetchone() == (crm_baseline_store.STAMP, '[]')
