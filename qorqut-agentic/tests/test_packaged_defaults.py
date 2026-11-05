"""Distribution bundles are readable through the same catalogue as an authored tree."""
import hashlib
import json
import shutil
from importlib.resources import files
from pathlib import Path

import yaml

from tests.conftest import make_layer

DEFAULTS = files('qorqut_agentic').joinpath('resources', 'defaults')


def test_all_defaults_catalogue_and_resources(tmp_path):
    layer = make_layer(tmp_path)
    shutil.copytree(DEFAULTS, layer.root, dirs_exist_ok=True)
    report = layer.catalog.validate()
    assert report['valid'], report['issues']
    entries = layer.catalog.list(limit=1000)['entries']
    assert entries
    for entry in entries:
        assert entry['editable'] == (entry['ref'] == 'user-preferences/preferences')
        result = layer.catalog.read_entry(entry['ref'], include_dependency_bodies=True)
        assert result['body']
        for resource in result['resources']:
            data = layer.catalog.read_resource(entry['package_id'], resource['path'])
            assert data['sha256'] == resource['sha256']
        assert all(dep['body'] for dep in result['dependencies'])
    assert layer.catalog.read_entry('user-preferences/preferences')['entry']['editable']
    package = layer.root / 'roles/imported-roles/native/roles'
    assert {yaml.safe_load(p.read_text())['id'] for p in package.glob('*/role.yaml')} == {
        'global-worker', 'learning-applier', 'ordubashy'}
    for role in package.glob('*/role.yaml'):
        metadata = layer.catalog.read_resource('imported-roles', role.relative_to(package.parent.parent).as_posix())
        assert metadata['content'] == role.read_text()


def test_provenance_defaults_hashes_and_bounded_rewrites():
    mapping = json.loads((Path(__file__).parents[1] / 'docs/defaults-provenance.json').read_text())
    rewritten = {r['defaults_path'] for r in mapping['native_manifest_field_rewrites']}
    transformations = {
        'overseer/overseer/instructions/extract.md': 'Combine focused episode and continuation guidance with protected outside-observation routing.',
        'roles/imported-roles/native/roles/learning-applier/role.yaml': 'Retain relocated paths, version and outside-observation grants; add current change and evidence reads and remove retired learning work grants.',
        'roles/imported-roles/native/roles/ordubashy/role.yaml': 'Retain relocated paths and increment version; preserve current shared agentic change and Attention grants.',
        'roles/imported-roles/native/roles/learning-applier/BRIEF.md': 'Combine current direct-instruction Signoff and complete legacy inventory guidance with the relocated brief.',
        'roles/imported-roles/native/roles/ordubashy/BRIEF.md': 'Retain report-and-takeover, duplicate-execution reconciliation and direct-instruction Signoff guidance alongside current Attention guidance.',
        'operations/imported-role-skills/native/skills/operations-delegation/SKILL.md': 'Apply the report-and-takeover rule to unavailable initial dispatch while preserving records and preventing duplicate execution.',
        'operations/imported-role-skills/native/skills/owner-focus/SKILL.md': 'Retain corrected private record channels and source kinds, relay and acknowledgement rules alongside current Pinned and Attention separation.',
        'operations/agentic-management/bootstrap.md': 'Combine current Signoff guidance with editable targets, discovery authority and API boundary.',
        'operations/agentic-management/skills/manage/SKILL.md': 'Combine current direct-instruction Signoff workflow with editable targets, refresh, delivery and discovery semantics.',
        'overseer/overseer/instructions/apply.md': 'Route protected and host observations outside while retaining outcome safeguards.',
        'overseer/overseer-role-skills/native/skills/learning-application/SKILL.md': 'Replace automatic external task routing with outside observation handling.',
        'overseer/overseer-role-skills/native/skills/learning-application/references/workflow.md': 'Route outside observations without automatic tasks while retaining outcome safeguards.',
    }
    assert {row['defaults_path'] for row in mapping['files'] if 'transformation' in row} == set(transformations)
    for row in mapping['files']:
        path = DEFAULTS.joinpath(*row['defaults_path'].split('/'))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['defaults_sha256']
        if row['defaults_path'] in transformations:
            assert row['transformation'] == transformations[row['defaults_path']]
            assert row['source_sha256'] != row['defaults_sha256']
        elif row['defaults_path'] not in rewritten:
            assert row['source_sha256'] == row['defaults_sha256']


def test_combined_native_roles_preserve_current_grants_and_review_guidance():
    root = DEFAULTS.joinpath('roles', 'imported-roles', 'native', 'roles')
    roles = {name: yaml.safe_load(root.joinpath(name, 'role.yaml').read_text())
             for name in ('ordubashy', 'learning-applier')}
    assert roles['ordubashy']['version'] == '1.73.0'
    assert roles['learning-applier']['version'] == '1.17.0'
    for role in roles.values():
        grants = role['default_tool_policy']
        assert all(grants.get(name) == 'allow' for name in
                   ('agentic_change_propose', 'agentic_change_get', 'agentic_change_reconcile'))
    grants = roles['learning-applier']['default_tool_policy']
    assert all(grants.get(name) == 'allow' for name in (
        'actions_thread_get', 'agents_get', 'tasks_get', 'tasks_thread_get',
        'overseer_outside_delta_record', 'overseer_outside_delta_get', 'overseer_outside_deltas_list'))
    assert 'learnings_issue_action' not in grants
    assert not any(name.startswith('learnings_work_') for name in grants)
    learning = root.joinpath('learning-applier', 'BRIEF.md').read_text()
    assert 'complete\nhistorical inventory source' in learning
    assert 'filtered\nfactual agenda' in learning
    assert 'delta decision as Context authority' in learning
    brief = root.joinpath('ordubashy', 'BRIEF.md').read_text()
    assert 'Reconcile uncertain delivery and prevent' in brief
    assert 'agentic_change_reconcile' in brief


def test_attention_survives_bundled_role_integration():
    root = DEFAULTS.joinpath("roles/imported-roles/native/roles/ordubashy")
    grants = yaml.safe_load(root.joinpath("role.yaml").read_text())["default_tool_policy"]
    assert all(grants.get(name) == "allow" for name in ('workers_list', 'attention_get', 'attention_add', 'attention_remove', 'attention_focus_set', 'attention_focus_clear', 'attention_workflow_update', 'attention_workflow_context', 'attention_workflow_sync'))
    assert "Your commitments live on your own Attention board" in root.joinpath("BRIEF.md").read_text()
    focus = DEFAULTS.joinpath("operations/imported-role-skills/native/skills/owner-focus/SKILL.md").read_text()
    assert "This board is shown as Pinned." in focus
