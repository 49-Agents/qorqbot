from qorqut_operations.history_exports import HistoryContribution, PRIVATE_EXCLUDED, TableClassification

CONTRIBUTION = HistoryContribution("telegram", classifications=(
            TableClassification('telegram_cleanup_jobs', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
            TableClassification('telegram_events', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
            TableClassification('telegram_file_payloads', PRIVATE_EXCLUDED, 'Outbound file snapshot metadata names owner-bound files and private source identity; the bytes live only in the private bridge state and are never exported.'),
            TableClassification('telegram_messages', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
            TableClassification('telegram_outbox', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
            TableClassification('telegram_owner_inputs', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
            TableClassification('telegram_signoff_posts', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
            TableClassification('telegram_state', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
            TableClassification('telegram_updates', PRIVATE_EXCLUDED, 'Owner communication transport and raw requests may contain private message contents.'),
))
