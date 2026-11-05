import subprocess

from bazarlyk.agentic import managed_git, managed_intent
from bazarlyk.agentic.managed_plan import prepare
from test_agentic_managed_plan import installed, refresh_export

ATTR = {'actor_id': 'synthetic-admin', 'actor_kind': 'human', 'reason': 'Offline managed update'}


def test_reviewed_commit_projection(installed):
    paths, source = installed
    guidance = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    guidance.write_bytes(guidance.read_bytes() + b'\nSynthetic supplier change.\n')
    refresh_export(source)
    plan = prepare(paths, source)
    assert plan['complete'], plan['conflicts']
    intent = managed_intent.capture(paths, plan, attribution=ATTR)
    head = subprocess.check_output(['git', '-C', str(paths.agentic_root), 'rev-parse', 'HEAD'])
    index = (paths.agentic_root / '.git/index').read_bytes()
    proof = managed_git.prepare(paths, intent, paths.instance_root / 'config')
    assert managed_git.verify(paths, intent, proof, paths.instance_root / 'config')
    assert subprocess.check_output(['git', '-C', str(paths.agentic_root), 'rev-parse', 'HEAD']) == head
    assert (paths.agentic_root / '.git/index').read_bytes() == index
    actual = subprocess.check_output(['git', '-C', str(paths.agentic_root), 'diff-tree', '--no-commit-id', '--name-only', '-r', proof['commit']]).decode().splitlines()
    assert set(actual) == {c['path'] for c in plan['changes']}
