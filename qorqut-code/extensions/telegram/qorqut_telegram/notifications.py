"""Durable reminder delivery and pre-send validation, owned by the optional Telegram connector."""
from qorqut_operations.contracts import DomainError
from .outbox import enqueue_text

UNAVAILABLE = frozenset({'operation_unavailable', 'binding_invalid', 'installation_binding_mismatch',
                         'owner_binding_mismatch', 'binding_changed'})


class OwnerNotifications:
    def __init__(self, extension):
        self.extension = extension

    def publish(self, uow, payload, service):
        bridge = self.extension.bridge
        if bridge is None:
            return None
        try:
            _, row, _ = enqueue_text(bridge, uow, bridge.config.chat('urgent'), text=payload['text'].strip(), purpose='notice',
                source_kind='reminder_occurrence', source_key=payload['occurrence_id'])
        except DomainError as error:
            if error.code in UNAVAILABLE:
                return None
            raise
        return {'kind': 'telegram', 'outbox_id': row['id']}

    def admit(self, uow, row):
        try:
            payload = self.extension.runtime.reminders.current_occurrence(uow, row['source_key'])
        except DomainError:
            return 'cancelled', 'reminder_unavailable', 'The reminder occurrence could not be verified'
        if payload is None:
            return 'cancelled', 'reminder_invalidated', 'The reminder occurrence is no longer current'
        installation = uow.repository('installation').get('service')
        if not installation or payload.get('recipient_id') != installation['owner_principal_id']:
            return 'cancelled', 'recipient_changed', 'The reminder is no longer addressed to the installation owner'
        return None
