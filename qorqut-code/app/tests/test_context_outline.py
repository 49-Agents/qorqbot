import pytest

from bazarlyk.background.context import parse_outline
from qorqut_operations.contracts import DomainError


def test_outline_supports_both_consistent_first_branch_indents():
    expected = parse_outline('Root\n- Branch\n  - Detail\n- Second')
    assert parse_outline('Root\n  - Branch\n    - Detail\n  - Second') == expected


@pytest.mark.parametrize('text', [
    'Root\n    - Skipped levels', 'Root\n- Branch\n    - Skipped level',
    'Root\n  - Branch\n- Unexpected dedent', 'Root\n - Odd indentation',
    'Root\n- One\n  - Two\n    - Three\n      - Four\n        - Five',
])
def test_outline_still_rejects_invalid_nesting(text):
    with pytest.raises(DomainError, match='nesting'):
        parse_outline(text)
