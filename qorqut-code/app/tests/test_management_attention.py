from test_signoff_inputs import create, scenario
from qorqut_operations.contracts import new_id


def route(s, signoff_id, destination, *, management_allowed=True):
    with s.db.transaction() as uow:
        now = '2026-09-09T02:00:00.000000Z'
        uow.connection.execute(
            '''INSERT INTO management_routing
               (id,source_type,source_id,business_id,coordinator_id,attention_destination,
                management_allowed,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (new_id(), 'signoffs', signoff_id, s.business, s.agent.id, destination,
             int(management_allowed), s.agent.id, now, now))


def test_managed_pending_input_is_hidden_from_default_owner_attention(scenario, monkeypatch):
    s = scenario
    s.service.clock = lambda: '2026-09-09T02:00:00.000000Z'
    managed = create(s, request_key='managed', attention={'notify': True, 'urgent': True, 'reason': 'Routine business choice'})
    escalated = create(s, request_key='escalated', attention={'notify': True, 'urgent': True, 'reason': 'Owner authentication required'})
    from qorqut_operations.signoffs import inputs
    current_validation = inputs.validate_form
    def legacy_form(form):
        validated = current_validation(form)
        validated.pop('response_policy', None)
        return validated
    # Simulate the old creator; immutable proposal bytes cannot be edited later.
    with monkeypatch.context() as patch:
        patch.setattr(inputs, 'validate_form', legacy_form)
        legacy = create(s, request_key='legacy', attention={'notify': True, 'urgent': True, 'reason': 'Existing pending question'})
    route(s, managed['id'], 'management')
    route(s, escalated['id'], 'owner', management_allowed=False)
    # Even an old routing row cannot delegate legacy owner-only input.
    route(s, legacy['id'], 'management')

    notifications = s.service.notifications
    notifications.tick()
    default = notifications.overview(s.owner_context)
    assert {row['id'] for row in default['urgent']} == {escalated['id'], legacy['id']}
    assert managed['id'] not in {row['id'] for row in default['urgent']}
    delivered = notifications.deliveries(s.owner_context)['items']
    delivered_ids = {item['id'] for delivery in delivered for item in delivery['items']}
    assert delivered_ids == {escalated['id'], legacy['id']}

    all_items = notifications.overview(s.owner_context, all_items=True)
    assert {row['id'] for row in all_items['urgent']} == {managed['id'], escalated['id'], legacy['id']}
    # The all-items view preserves owner inspection without manufacturing a
    # delivery/toast for an item that was intentionally filtered at tick time.
    assert {item['id'] for delivery in notifications.deliveries(s.owner_context, all_items=True)['items']
            for item in delivery['items']} == {escalated['id'], legacy['id']}
