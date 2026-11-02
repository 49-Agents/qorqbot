"""Exact managed feed event derivation and transaction reconciliation."""
from __future__ import annotations

from qorqut_agentic.changes import Changes, entries_touching, event_dict, package_ids_of
from qorqut_agentic.core import LayerContext
from qorqut_agentic.store import Store, _verify_layout
from . import managed_files, managed_runtime_evidence
from .layout_inspection import load_memory_catalog
from .layout_intent import _conflict
from .support import digest


def expected(intent, commit):
    changes = intent['plan']['changes']
    fixed = intent['fixed']
    prior = intent['runtime_before']
    seq = max(prior['changes_sequence_before'] or 0, prior['feed_head']) + 1
    paths = [c['path'] for c in changes]
    final = managed_files.target(intent)
    catalogue = load_memory_catalog({p: v[0] for p, v in final.items()})
    files = [{'path': c['path'], 'action': c['op'], 'before_sha256': c['expected_sha256'],
              'after_sha256': None if c['op'] == 'delete' else c['desired_sha256'], 'commit': commit} for c in changes]
    return {'sequence': seq, 'change_id': fixed['change_id'], 'timestamp': fixed['created_at'],
            'kind': 'external_scan', 'attribution': intent['attribution'], 'files': files, 'paths': paths,
            'package_ids': package_ids_of(paths), 'entry_refs': entries_touching(catalogue, set(paths)),
            'commits': [commit], 'details': {'observed': 'net change between the recorded inventory and the files on disk',
                                             'managed_plan_digest': intent['plan']['plan_digest']}}


def verify_event(conn, intent, commit):
    wanted = expected(intent, commit)
    rows = conn.execute('SELECT * FROM changes WHERE change_id=?', (wanted['change_id'],)).fetchall()
    if len(rows) != 1 or event_dict(conn, rows[0]) != wanted:
        raise _conflict('Managed feed event differs from fixed evidence')
    return wanted


def verify_effects(conn, intent, commit):
    wanted = verify_event(conn, intent, commit)
    changes = intent['plan']['changes']
    after = managed_runtime_evidence.snapshot(conn, [c['path'] for c in changes], wanted['change_id'], require_unused=False)
    before = intent['runtime_before']
    if after['schema_sha256'] != before['schema_sha256'] or after['protected'] != before['protected']:
        raise _conflict('Unrelated managed runtime evidence changed')
    if after['feed_head'] != wanted['sequence'] or after['changes_sequence_before'] != wanted['sequence']:
        raise _conflict('Managed feed sequence differs')
    final = managed_files.target(intent)
    expected_inventory = {c['path']: None if c['op'] == 'delete' else
                          {'sha256': digest(final[c['path']][0]), 'bytes': len(final[c['path']][0])} for c in changes}
    if after['changed_inventory_before'] != dict(sorted(expected_inventory.items())):
        raise _conflict('Managed inventory effects differ')
    return wanted


def apply(paths, intent, commit):
    changes = intent['plan']['changes']
    if not changes:
        return None
    store = Store(paths.agentic_runtime)
    ctx = LayerContext(root=paths.agentic_root, store=store, git_executable=None, runtimes={}, clock=None,
                       attribution_source='host_installer')
    editor = Changes(ctx)
    fixed = intent['fixed']
    with store.transaction() as conn:
        _verify_layout(conn, '3')
        prior = intent['runtime_before']
        existing = conn.execute('SELECT 1 FROM changes WHERE change_id=?', (fixed['change_id'],)).fetchone()
        if existing:
            return verify_effects(conn, intent, commit)
        before = managed_runtime_evidence.snapshot(conn, [c['path'] for c in changes], fixed['change_id'])
        if before != prior:
            raise _conflict('Managed runtime changed before feed publication')
        scan = editor._scan_connection(conn, intent['attribution'], paths={c['path'] for c in changes},
                                       change_id=fixed['change_id'], created_at=fixed['created_at'],
                                       details={'managed_plan_digest': intent['plan']['plan_digest']})
        if scan['skipped'] or not scan['change']:
            raise _conflict('Managed feed scan did not capture reviewed changes')
        committed = {c['path']: None if c['op'] == 'delete' else c['desired_sha256'] for c in changes}
        if editor._link_commit_connection(conn, commit, committed, change_id=fixed['change_id']) != len(changes):
            raise _conflict('Managed feed commit linkage is incomplete')
        return verify_effects(conn, intent, commit)
