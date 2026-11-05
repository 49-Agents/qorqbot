from types import SimpleNamespace

from bazarlyk.native_chat.nudges import HostVisibility


def test_focused_gate_only_filters_automatic_visible_events(monkeypatch):
    from bazarlyk.management import identity

    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'coordinator')
    attention = SimpleNamespace(state=lambda uow, agent: {'focus_json': {'type': 'task'}},
                                related_event=lambda uow, agent, event: event['id'] == 2)
    inbox = SimpleNamespace(app=SimpleNamespace(ordubashy_attention=attention),
                            event_visible=lambda ctx, row, uow: row['id'] != 3)
    visibility = HostVisibility(inbox)
    visibility._context = lambda uow, agent: object()
    events = [dict(id=i, business_id=None, source_type='other', source_id=str(i),
                   event_type='changed', visibility_json='{}') for i in (1, 2, 3)]
    assert visibility.authorized(object(), 'coordinator', events) == {2}
    assert visibility.authorized(object(), 'another-agent', events) == {1, 2}
    attention.state = lambda uow, agent: {'focus_json': None}
    assert visibility.authorized(object(), 'coordinator', events) == {1, 2}
