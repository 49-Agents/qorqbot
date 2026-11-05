"""Focused authored-text behavior; no host runtime, provider or native session."""
import json
from hashlib import sha256
from pathlib import Path

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.agentic import text
from bazarlyk.agents import cleanup_workflow, role_workflow
from bazarlyk.agents.context import compose_context
from text_support import FakeView, inputs, normalized, seeded_view


def test_composition_matches_recorded_preconversion_bodies():
    baseline = json.loads(Path(__file__).with_name('context_baseline.json').read_text())
    for case, expected in baseline['normalized_body_sha256'].items():
        profile, role, sources, kwargs = inputs(case)
        result = compose_context(profile, role, sources, **kwargs, agentic_view=seeded_view())
        assert sha256(normalized(result['rendered']).encode()).hexdigest() == expected, case
        assert "'/synthetic/work space/.bazarlyk/agentic'" in result['rendered']
        assert result['bundle_hash'] == 'sha256:' + sha256(result['rendered'].encode()).hexdigest()


def test_managed_edits_are_read_at_next_composition_and_context_stays_authoritative():
    view = seeded_view()
    profile, role, sources, kwargs = inputs('all')
    first = compose_context(profile, role, sources, **kwargs, agentic_view=view)
    view.resources['host','app/bazarlyk/agentic/assets/startup.md',None] = 'Edited managed startup — 現在.'
    view.resources['roles','roles/ordubashy/BRIEF.md',None] = 'Edited managed role.'
    second = compose_context(profile, role, sources, **kwargs, agentic_view=view)
    assert 'Edited managed startup — 現在.' in second['rendered']
    assert 'Edited managed role.' in second['rendered']
    assert 'Synthetic foundation.' in second['rendered'] and 'Synthetic current Context graph.' in second['rendered']
    assert second['bundle_hash'] != first['bundle_hash']
    assert next(e for e in second['entries'] if e['source_id'].startswith('role:'))['hash'] != next(
        e for e in first['entries'] if e['source_id'].startswith('role:'))['hash']
    del view.resources['host','app/bazarlyk/agentic/assets/startup.md',None]
    with pytest.raises(DomainError, match='Missing managed resource'):
        compose_context(profile, role, sources, **kwargs, agentic_view=view)
    with pytest.raises(DomainError, match='requires an agentic view'):
        compose_context(profile, role, sources, **kwargs)


def test_one_pass_strict_substitution_unicode_and_real_newlines():
    view = seeded_view()
    path = 'app/bazarlyk/agentic/assets/scope.md'
    view.resources['host',path,None] = '第一行\n{{PROJECT_COUNT}}\n第二行'
    assert text.render(view,'host',path,PROJECT_COUNT='{{NOT_RECURSIVE}}') == '第一行\n{{NOT_RECURSIVE}}\n第二行'
    for values in ({}, {'PROJECT_COUNT':'2','EXTRA':'x'}, {'PROJECT_COUNT':2}):
        with pytest.raises(DomainError) as error:
            text.render(view,'host',path,**values)
        assert error.value.code == 'invalid_instruction_parameters'
    for body in ('{{PROJECT_COUNT + 1}}', '{{__import__("os")}}', '{{OTHER}}', '{{PROJECT_COUNT}', 'plain'):
        view.resources['host',path,None] = body
        with pytest.raises(DomainError) as error:
            text.render(view,'host',path,PROJECT_COUNT='2')
        assert error.value.code == 'invalid_instruction_template'


@pytest.mark.parametrize('path',['../elsewhere.md','/etc/passwd','app/../secret.md','x\\secret.md'])
def test_traversal_fails_before_resource_read(path):
    view = FakeView()
    with pytest.raises(ValueError):
        text.render(view,'host',path)
    assert not view.calls


def test_missing_declaration_is_not_a_file_fallback():
    view = seeded_view()
    with pytest.raises(DomainError) as error:
        text.render(view,'host','app/bazarlyk/agentic/assets/undeclared.md')
    assert error.value.code == 'undeclared_instruction'
    assert not view.calls


@pytest.mark.parametrize('module,subtree', [(role_workflow,'role'),(cleanup_workflow,'cleanup')])
def test_workflow_skill_files_and_metadata_use_the_view(module, subtree):
    view = seeded_view()
    assert module.skill_files(agentic_view=view) == module.skill_files()
    before = module.skill_metadata(agentic_view=view)
    path = f'app/bazarlyk/agents/skills/{subtree}/SKILL.md'
    view.resources['host',path,None] += b'\nCurrent authored addition.\n'
    assert 'Current authored addition.' in module.skill_files(agentic_view=view)['SKILL.md'].decode()
    assert module.skill_metadata(agentic_view=view)['materialization_hash'] != before['materialization_hash']
    del view.resources['host',path,None]
    with pytest.raises(DomainError):
        module.skill_files(agentic_view=view)


def test_paper_orientation_and_guidance_preserve_conditions_and_read_managed_edits():
    from qorqut_paper.native import PaperNative
    view = seeded_view()
    assert PaperNative.orientation(True,True,agentic_view=view) == '\nThis profile declares the allowlisted `outreach-paper` MCP connection. Retrieve Outreach\ninstructions from the Outreach application; do not maintain a copied Qorqut playbook.\n'
    start = '\nThe `outreach-paper` connection is not prepared yet (no Outreach credential for this agent).\n'
    assert PaperNative.orientation(False,True,agentic_view=view) == start+'Report this to the owner; ordinary work is unaffected.\n'
    assert PaperNative.orientation(False,False,agentic_view=view) == start+'Ask Ordubashy about cold-email campaigns; ordinary work is unaffected.\n'
    path='qorqut_paper/agentic-assets/guidance.md'
    assert PaperNative.guidance(agentic_view=view) == text.bundled('paper',path)
    view.resources['paper',path,None] = 'Managed Paper procedure.'
    assert PaperNative.guidance(agentic_view=view) == 'Managed Paper procedure.'
    del view.resources['paper',path,None]
    with pytest.raises(DomainError):
        PaperNative.guidance(agentic_view=view)
