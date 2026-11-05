"""The host forbids the Overseer as a Worker without applying assignment rules."""
from bazarlyk.agents.roles import LEARNING_APPLIER_ROLE
from bazarlyk.domains.tasks.ports import HostAssignees


class Repo:
    def __init__(self, rows):
        self.rows = rows

    def get(self, identity):
        return self.rows.get(identity)


class Uow:
    def __init__(self, rows):
        self.rows = rows

    def repository(self, name, **kwargs):
        assert name == 'agent_profiles'
        return Repo(self.rows)


def test_overseer_role_is_excluded_by_identity_not_display_name():
    rules = HostAssignees()
    uow = Uow({'overseer': {'profile_json': {'role': {'id': LEARNING_APPLIER_ROLE}}},
               'ordinary': {'profile_json': {'role': {'id': 'ordubashy'}, 'display_name': 'Overseer'}}})
    assert rules.worker_excluded(None, 'overseer', uow)
    assert not rules.worker_excluded(None, 'ordinary', uow)
    assert not rules.worker_excluded(None, 'human', uow)


def test_attention_routes_are_unioned_with_existing_mandatory_routes(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace
    from bazarlyk.domains.tasks import ports

    calls = []
    module = ModuleType('bazarlyk.agents.attention')
    module.task_recipients = lambda uow, business, task, actor: calls.append((business, task, actor)) or {'board-agent'}
    monkeypatch.setitem(sys.modules, 'bazarlyk.agents.attention', module)
    monkeypatch.setattr(ports, 'ordubashies', lambda uow: {'owner-agent'})
    monkeypatch.setattr(ports, 'owner_task_rows', lambda *args, **kwargs: SimpleNamespace(fetchone=lambda: True))
    uow = SimpleNamespace(repository=lambda name: Repo({'service': {'management_coordinator_id': None,
                                                                   'owner_principal_id': 'owner'}}))
    task = {'id': 'task', 'created_at': 'created'}
    assert ports.HostRouting.mandatory(uow, 'project', task, 'actor') == {'board-agent', 'owner-agent'}
    assert calls == [('project', task, 'actor')]
