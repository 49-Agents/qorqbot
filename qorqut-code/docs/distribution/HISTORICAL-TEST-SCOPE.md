# Historical test scope

The native release checks fresh assembled stores and the supported same-schema
update path. It does not promise automatic conversion of every former monolithic
database version. The tests below predate the Operations/host separation and
still import the retired `bazarlyk.storage.Database`, `TABLES` or database module.
They cannot run unchanged against the current composed engine. Their presence is
source history and migration intent, not a passing release gate.

The inventory is based on direct imports; dependent old fixtures may affect
additional tests. A complete host-suite pass is **not claimed**. Porting a fixture
requires the corresponding current API or a deliberately frozen historical
schema, not a compatibility shim that silently upgrades production data.

## Preserved intent

`test_sandbox_migration_upgrade.py` combines obsolete sandbox provisioning and
disk-budget schema expectations with valuable principal/document/working-memory
and incident-history preservation checks. Its assertions are retained pending an
explicit old-schema migration project; sandbox provisioning is not a supported
release feature. Likewise, `test_global_tables_migration.py` includes old
accounting/ledger guards as well as history preservation. Those historical
assertions do not reintroduce accounting into core.

The remaining tests cover retained features or historical transitions, including
session identities, shared document history, action dates, routine/inbox records,
retired role attribution and research retirement. They were not deleted or marked
passing merely because their old API no longer exists.

Current migration tests using frozen schema fingerprints and the composed engine
remain separate, including `test_crm_host_upgrade.py`,
`test_crm_release_upgrade.py`, `test_task_move_upgrade.py`,
`test_attention_workflow_relocation_upgrade.py` and
`test_task_needs_input_upgrade.py`. Their applicability must be established for
each proposed source/target pair; this inventory does not certify their results.
Fresh initialization is covered by `test_host_foundation_schema.py`, while
`test_extension_install.py` covers the supported additive connector transition.

## Direct retired-API import inventory

Paths are relative to `qorqut-code/app/tests/`. This list deliberately includes
support fixtures so the source of transitive failures is visible.

- `schema_seed.py` (imports at lines 22, 23).
- `single_agent_support.py` (imports at lines 17).
- `test_agent_configuration.py` (imports at lines 33).
- `test_agent_inbox.py` (imports at lines 348).
- `test_agent_viewbox.py` (imports at lines 11).
- `test_broadcast_migration.py` (imports at lines 4).
- `test_browser_short_handles.py` (imports at lines 15).
- `test_business_progress.py` (imports at lines 9).
- `test_business_role_slots_migration.py` (imports at lines 11).
- `test_context_relationships.py` (imports at lines 140, 184).
- `test_dashboard_read_indexes_migration.py` (imports at lines 3).
- `test_data_uploads.py` (imports at lines 10).
- `test_foundation_storage.py` (imports at lines 11).
- `test_global_context_migration.py` (imports at lines 4).
- `test_global_scope.py` (imports at lines 10).
- `test_global_tables_migration.py` (imports at lines 9, 10).
- `test_global_tasks.py` (imports at lines 9).
- `test_inbox_migration.py` (imports at lines 1).
- `test_incidents.py` (imports at lines 11).
- `test_installed_application_contract.py` (imports at lines 21).
- `test_learning_sources.py` (imports at lines 217).
- `test_learning_storage.py` (imports at lines 6).
- `test_marketing_private_migration.py` (imports at lines 8).
- `test_material_classification.py` (imports at lines 8).
- `test_message_participants.py` (imports at lines 10).
- `test_message_summaries.py` (imports at lines 106).
- `test_operational_delegation_store.py` (imports at lines 13).
- `test_owner_attention.py` (imports at lines 372).
- `test_research_retirement.py` (imports at lines 4).
- `test_routine_migration.py` (imports at lines 4).
- `test_sandbox_migration_upgrade.py` (imports at lines 10).
- `test_schema_seed.py` (imports at lines 6, 7).
- `test_service_background.py` (imports at lines 13).
- `test_service_entrypoint.py` (imports at lines 11).
- `test_shared_history_migration.py` (imports at lines 12).
- `test_signoff_inputs.py` (imports at lines 147).
- `test_signoffs.py` (imports at lines 23).
- `test_storage_maintenance.py` (imports at lines 14).
- `test_task_snooze.py` (imports at lines 12).
- `test_unified_actions.py` (imports at lines 12, 522).
- `test_unified_learning_routine.py` (imports at lines 87, 129).
- `test_working_memory.py` (imports at lines 241).
