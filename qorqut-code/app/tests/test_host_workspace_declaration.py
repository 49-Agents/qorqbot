"""Core pane declarations keep Focus and retire only the standalone Threads screen."""
import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.workspaces import WORKSPACE_TYPES, WorkspaceRegistry, WorkspaceService


def test_core_declares_focus_without_the_retired_threads_screen():
    registry = WorkspaceRegistry()
    assert [item.key for item in registry.all()] == ['focus']
    focus = registry.get('focus')
    assert focus.supported_roles == frozenset({'ordubashy', 'global-worker'})
    assert focus.renderer == 'focus_board'
    assert 'focus_get' not in focus.presentation_actions and 'focus_show' in focus.presentation_actions
    for role in ('ordubashy', 'global-worker'):
        assert [item.key for item in registry.for_role(role)] == ['focus']
    assert registry.for_role('learning-applier') == ()
    assert len(WORKSPACE_TYPES) == 1
    with pytest.raises(DomainError, match='Unknown workspace type'):
        registry.get('threads')


@pytest.mark.parametrize('role, expected', [(None, ['focus']), ('ordubashy', ['focus']),
    ('global-worker', ['focus']), ('learning-applier', []), ('business-agent', [])])
def test_workspace_types_read_never_offers_threads(role, expected):
    result = WorkspaceService(None).read_types(None, {'role_id': role} if role else {}, None)
    assert [item['key'] for item in result['types']] == expected
    assert result['role_id'] == role
