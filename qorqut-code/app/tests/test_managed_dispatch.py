from bazarlyk.agentic import upgrade
from bazarlyk.agentic.installation import prepare_installation
from bazarlyk.agentic.layout_upgrade import prepare_layout
from test_agentic_managed_plan import installed, snapshot_instance

ATTR = {'actor_id': 'synthetic-admin', 'actor_kind': 'human', 'reason': 'Offline managed update'}


def test_schema_two_dispatch_and_historical_layout_noop(installed):
    paths, export = installed
    binding, sha = upgrade.read_binding(paths)
    historical = prepare_layout(paths, binding, sha, prepare_installation(export, []))
    assert historical['kind'] == 'agentic_layout_v2' and not historical['layout_change']
    assert historical['complete'], historical['conflicts']
    fresh = upgrade.prepare_upgrade(paths, export)
    assert fresh['kind'] == 'agentic_managed_update_v2' and fresh['no_change']
    before = snapshot_instance(paths)
    result = upgrade.apply_upgrade(paths, historical, attribution=ATTR)
    assert result['kind'] == 'agentic_layout_v2' and result['no_change']
    assert snapshot_instance(paths) == before


def test_assume_unchanged_index_is_refused(installed):
    import subprocess
    paths, export = installed
    path = 'overseer/overseer/instructions/review.md'
    subprocess.run(['git', '-C', str(paths.agentic_root), 'update-index', '--assume-unchanged', '--', path], check=True)
    before = snapshot_instance(paths)
    plan = upgrade.prepare_upgrade(paths, export)
    assert not plan['complete']
    assert any(c['code'] == 'git_assume_unchanged_unsupported' for c in plan['conflicts'])
    assert snapshot_instance(paths) == before
