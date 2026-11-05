"""Explicit public-resource fake and entirely synthetic composition inputs."""
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from bazarlyk.agents.context import ContextSource
from bazarlyk.agents.models import AgentProfile
from qorqut_operations.contracts import DomainError

HOST = Path(__file__).resolve().parents[3]
GROUP = HOST.parent
ALL_OPERATIONS = frozenset('''telegram_send business_briefing inbox_list inbox_acknowledge dev_tickets_create viewbox_place
 environment_set environment_get operations_batch tasks_overview environment_list files_link plans_get plans_set
 notes_get notes_create agents_list tasks_list tasks_update owner_conversation_get owner_conversation_send tasks_assign
 tasks_create signoffs_list signoffs_get learnings_review_list learnings_review_get learnings_work_list learnings_work_get
 context_home context_find context_object context_expand'''.split())


class FakeView:
    """No selection/binding implementation: just the documented public resource interface."""
    def __init__(self, resources=None):
        self.resources = dict(resources or {})
        self.calls = []

    def role_brief(self, role):
        body = self.resources[("roles", "roles/ordubashy/BRIEF.md", None)]
        if isinstance(body, bytes):
            body = body.decode()
        return dict(body=body, path="roles/imported-roles/native/roles/ordubashy/BRIEF.md",
                    revision="synthetic", sha256="sha256:" + sha256(body.encode()).hexdigest())

    def resource(self, source_id, source_path, *, symbol=None):
        self.calls.append((source_id, source_path, symbol))
        try:
            body = self.resources[(source_id, source_path, symbol)]
        except KeyError:
            raise DomainError('missing_resource', f'Missing managed resource: {source_id}:{source_path}')
        if isinstance(body, str):
            body = body.encode()
        return dict(content=body.decode(), encoding='utf-8', sha256=sha256(body).hexdigest(),
                    root_path=f'packages/imported-{source_id}/native/{source_path}')


def seeded_view():
    resources = {('host', path.relative_to(HOST).as_posix(), None): path.read_bytes()
                 for directory in ('app/bazarlyk/agentic/assets', 'app/bazarlyk/agents/skills')
                 for path in (HOST / directory).rglob('*.md')}
    resources[('roles', 'roles/ordubashy/BRIEF.md', None)] = b'Synthetic role brief.'
    for path in (GROUP / 'qorqut-paper/qorqut_paper/agentic-assets').glob('*.md'):
        resources[('paper', 'qorqut_paper/agentic-assets/' + path.name, None)] = path.read_bytes()
    return FakeView(resources)


def inputs(case):
    profile = AgentProfile(dict(agent_id='00000000-0000-4000-8000-000000000001', profile_revision=7,
        name='test-agent', display_name='Test Agent', assignment_brief='Perform the synthetic task.', plugins=[],
        scope=dict(kind='ecosystem', allowed_business_ids=[], global_capabilities=[])))
    role = SimpleNamespace(id='ordubashy', version='1.0.0', digest='sha256:'+'a'*64,
        manifest=dict(scope_brief='scope', brief='roles/ordubashy/BRIEF.md'),
        files={'roles/ordubashy/BRIEF.md': b'Synthetic role brief.'})
    sources = {key: ContextSource(key, f'Synthetic {key}.', 2, 'sha256:'+'b'*64, key+'.md')
               for key in ('foundation','scope','ecosystem')}
    # Synthetic future canonical entry; the frozen engine does not yet scan its layout.
    sources['user-preferences/preferences'] = ContextSource('user-preferences/preferences', 'Synthetic preferences.',
        'synthetic', 'sha256:'+'d'*64, 'user-preferences/SKILL.md', 'agentic', preserve_text=True)
    kwargs = dict(effective_config_id='sha256:'+'c'*64, workspace='/synthetic/work space', business=None)
    if case != 'plain':
        business = dict(id='00000000-0000-4000-8000-000000000002',display_name='Project Alpha', slug='alpha',stage='active',
            revision=3,workspace='/synthetic/project',config_json=dict(aliases=['Alias A'], one_liner='Synthetic project.',
            engineering=dict(repositories=['example/repository']),code=['example/repository']))
        kwargs.update(business=business, allowed_businesses=(business,), context_home='Synthetic current Context graph.')
        profile.data['plugins'] = [dict(id='email-management',version='3.0.0',config={'mode':'synthetic'})]
        profile.data['scope']['global_capabilities'] = ['read_global_context']
        kwargs['available_operations'] = ALL_OPERATIONS if case == 'all' else frozenset({'tasks_list','files_link','inbox_list'})
    return profile, role, sources, kwargs


def normalized(rendered):
    import re
    rendered = re.sub(r'## User Preferences\n.*?(?=\n\n## |\Z)', '', rendered, flags=re.S)
    rendered = rendered.replace('\nDiscover and read relevant shared catalogue entries for the current project and work type, including applicable plugins, without requiring explicit plugin assignment. Catalogue discovery does not grant tools, credentials, permissions, or native plugin access.\n', '\n')
    rendered = re.sub(r'## Agentic Toolkit\n.*?(?=\n\n## |\Z)', '', rendered, flags=re.S)
    rendered = re.sub(r'^\[source=.*\]\n', '', rendered, flags=re.M)
    return re.sub(r'\n{3,}', '\n\n', rendered).strip()
