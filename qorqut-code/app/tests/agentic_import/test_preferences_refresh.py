"""Component-only tests: future catalogue shape is synthetic, not W01 integration."""
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.agents.context import compose_context, MAX_CONTEXT_CHARACTERS
from bazarlyk.agents.preferences import load_preferences
from text_support import inputs, seeded_view


class Catalog:
    def __init__(self):
        self.body = '---\nname: preferences\ndescription: Synthetic preferences\n---\n\nFull preferences.\n\n'
        self.entry = dict(ref='user-preferences/preferences', kind='skill', path='SKILL.md',
                          root_path='user-preferences/SKILL.md', revision='abc')

    def read_entry(self, ref):
        assert ref == 'user-preferences/preferences'
        return dict(entry=self.entry, body=self.body)


class View:
    def __init__(self):
        self.layer = SimpleNamespace(catalog=Catalog())

    def consistent(self, read):
        return read()


@pytest.mark.parametrize('role_id', ['ordubashy', 'global-worker', 'learning-applier'])
def test_full_universal_preferences_for_each_role(role_id):
    catalog_view = View()
    profile, role, sources, kwargs = inputs('plain')
    role.id = role_id
    sources['user-preferences/preferences'] = load_preferences(catalog_view)
    result = compose_context(profile, role, sources, **kwargs, agentic_view=seeded_view())
    assert catalog_view.layer.catalog.body in result['rendered']
    entry = next(e for e in result['entries'] if e['source_id'] == 'user-preferences/preferences')
    assert entry['character_count'] == len(catalog_view.layer.catalog.body)
    assert entry['revision'] == 'abc' and entry['source_path'] == 'user-preferences/SKILL.md'
    assert 'Discover and read relevant shared catalogue entries' in result['rendered']


@pytest.mark.parametrize('field,value', [('kind','tool'), ('root_path','packages/user-preferences/SKILL.md'),
                                         ('path','other/SKILL.md'), ('revision',None)])
def test_invalid_preference_entry_is_a_setup_error(field, value):
    view = View()
    view.layer.catalog.entry[field] = value
    with pytest.raises(DomainError, match='Canonical user preferences') as error:
        load_preferences(view)
    assert error.value.code == 'invalid_user_preferences'


def test_no_abridgement_when_budget_exceeded():
    view = View()
    view.layer.catalog.body = 'p' * MAX_CONTEXT_CHARACTERS
    profile, role, sources, kwargs = inputs('plain')
    sources['user-preferences/preferences'] = load_preferences(view)
    with pytest.raises(DomainError) as error:
        compose_context(profile, role, sources, **kwargs, agentic_view=seeded_view())
    assert error.value.code == 'context_budget_exceeded'


def test_missing_or_nontext_preferences_fail_clearly():
    from qorqut_agentic.errors import NotFoundError
    view = View()
    def missing(ref):
        raise NotFoundError('missing')
    view.layer.catalog.read_entry = missing
    with pytest.raises(DomainError, match='Canonical user preferences') as error:
        load_preferences(view)
    assert error.value.code == 'invalid_user_preferences'
    view = View()
    view.layer.catalog.body = None
    with pytest.raises(DomainError, match='Canonical user preferences'):
        load_preferences(view)
