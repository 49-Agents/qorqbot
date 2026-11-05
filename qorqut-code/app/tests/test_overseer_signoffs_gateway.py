"""Synthetic canonical Signoffs exercise the host response adapter."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.overseer import inputs
from operational_support import AGENT2, BUSINESS, OperationalHost


def test_canonical_owner_response_and_stale_binding(tmp_path, monkeypatch):
    host = OperationalHost(tmp_path)
    runtime = SimpleNamespace(database=host.database, application=host.app,
                              registry=host.registry, signoffs=host.service)
    ctx = host.agent(AGENT2)
    # This isolates gateway reconciliation; registered identity is exercised separately.
    monkeypatch.setattr(inputs, 'current_overseer', lambda runtime, context, uow: (context, None))
    with host.database.transaction() as uow:
        uow.connection.execute('CREATE TABLE overseer_signoffs (binding_id TEXT,request_key TEXT,signoff_id TEXT,'
                               'proposal_revision INTEGER,payload_hash TEXT,review_business_id TEXT)')
    form = {'title': 'Review change', 'explanation': 'Review this exact change.', 'kind': 'question',
            'selection_mode': 'single', 'response_policy': 'owner_only',
            'choices': [{'id': 'accept', 'label': 'Accept', 'explanation': 'Accept this change.'},
                        {'id': 'later', 'label': 'Later', 'explanation': 'Decide later.'}]}
    result = host.app.invoke(ctx, 'signoffs_input_create', {'request_key': 'synthetic-key', 'form': form},
                             {'idempotency_key': 'synthetic-create'})
    assert result.status == 'ok', result.error
    row = result.to_dict()['data']['signoff']
    binding = {'id': str(uuid4()), 'request_key': 'synthetic-key',
               'owner_scope': {'kind': 'business', 'business_id': BUSINESS},
               'choice_actions': {'accept': 'accept', 'later': 'later'}}
    with host.database.transaction() as uow:
        uow.connection.execute('INSERT INTO overseer_signoffs VALUES (?, ?, ?, ?, ?, ?)',
                               (binding['id'], binding['request_key'], row['id'], row['proposal_revision'],
                                row['payload_hash'], BUSINESS))
    answered = host.service.respond(host.owner(), row['id'], proposal_revision=row['proposal_revision'],
                                    payload_hash=row['payload_hash'], expected_status_revision=row['status_revision'],
                                    idempotency_key='answer', notes='Only for this scope', selected_choices=['accept'])
    assert answered.status == 'ok', answered.error
    saved = answered.to_dict()['data']['signoff']
    reference = {'provider': 'qorqut_signoff', 'signoff_id': row['id'], 'proposal_revision': row['proposal_revision'],
                 'payload_hash': row['payload_hash'], 'status_revision': saved['status_revision']}
    gateway = inputs.OwnerInputGateway(runtime, ctx)
    response = gateway.resolve_response(binding, reference)
    assert response['notes'] == 'Only for this scope' and response['selected_choice_ids'] == ['accept']
    assert response['owner_principal_id'] == host.owner().principal_id
    assert gateway.resolve_response(binding, reference) == response
    with pytest.raises(DomainError):
        gateway.resolve_response(binding, {**reference, 'status_revision': reference['status_revision'] + 1})
    with pytest.raises(DomainError):
        gateway.resolve_response({**binding, 'choice_actions': {'later': 'later'}}, reference)
