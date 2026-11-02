"""Authority metadata for the retained v1 catalog; handlers install separately.

Every operation name, alias, exposure, owner restriction and scope is the deployed v1 one. This is host data: the
engine ships no operation names. ``builtin_registry`` returns the one open ``OperationRegistry`` that trusted assembly
then registers every domain, transport and program handler on; the assembly freezes it before it serves traffic."""

from qorqut_context.integration.registration import CONTEXT_ENTRIES
from qorqut_operations.actions import ACTION_ENTRIES
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.crm import CATALOG_ENTRIES as CRM_ENTRIES
from qorqut_operations.application.registry import CatalogEntry, OperationRegistry
from qorqut_operations.inbox.catalog import BROADCAST_READ_ENTRIES, MUTATION_ENTRIES, READ_ENTRIES
from qorqut_operations.relationships.operations import RELATIONSHIP_ENTRIES
from qorqut_operations.routines.operations import ROUTINE_ENTRIES
from qorqut_operations.signoffs.service import SIGNOFF_ENTRIES
from qorqut_operations.tags import TAG_ENTRIES
from qorqut_operations.tasks import TASK_ENTRIES

from bazarlyk.extensions import NO_EXTENSIONS

# The Operations engine owns the authority metadata (and handlers) of its Signoff, routine and neutral Context
# relationship/review operations (deployed v1 catalog lines, unchanged) and of the operational records: the shared tag
# catalog, Tasks and Actions. Context owns that of its queue, reads, filing, source, edit and question operations
# (``qorqut_context.integration.registration``). They are declared here once so every host registration can name them;
# their handlers are installed with ``install_contribution`` when each service is constructed.
# ``BUILTIN_CATALOG`` below holds only the host's own operations.
ENGINE_ENTRIES = (*SIGNOFF_ENTRIES, *ROUTINE_ENTRIES, *RELATIONSHIP_ENTRIES, *TAG_ENTRIES, *TASK_ENTRIES, *ACTION_ENTRIES,
                  *CRM_ENTRIES, *CONTEXT_ENTRIES)

_READS = """
business_progress_get
business_briefing
files_link
dashboard_summary_get
global_progress_get
working_memory_get agent_context_get agent_runtime_get operations_describe
agents_activity_list
tables_list tables_get tables_rows_list tables_history
data_uploads_list data_uploads_get
agent_config_get agents_list agents_get permissions_get
plans_list plans_get notes_list notes_get
assets_list assets_get content_list content_get learnings_list learnings_get
learnings_review_list learnings_review_get learnings_legacy_pending
context_list context_get context_search
jobs_get business_config_get
roles_list roles_get plugins_list agents_icons_list
delegated_assignment_get delegated_assignments_list
""".split()
_MUTATIONS = """
owner_action_propose business_archive
business_create
tables_create tables_update tables_rows_write tables_history_retry tables_archive
data_uploads_upload
plans_set notes_create notes_update notes_delete
context_document_update context_topics_create context_topics_move context_notes_create
context_notes_move context_people_create context_people_update context_people_affiliation_set context_people_merge
assets_upload assets_update
assets_delete learnings_extract
agents_create
agents_update agents_deactivate permissions_set plugins_enable plugins_disable
business_config_update business_config_patch roles_publish plugins_publish shared_instructions_update
delegated_assignments_create delegated_assignments_control delegated_assignments_revise delegated_assignment_checkpoint delegated_assignment_events_claim delegated_assignment_event_settle
""".split()
_CONTEXT_READS = """
context_people_list context_people_get context_maps_get
""".split()
_OWNER = """
learnings_review learnings_edit
terminals_open terminals_close terminals_write terminals_resize
""".split()
_WORKERS = """
learnings_record_batch
""".split()

BUILTIN_CATALOG = (
    CatalogEntry("dev_tickets_list", False, "read", frozenset({"dashboard"}), True),
    *(CatalogEntry(name, False, "read", frozenset({"dashboard"}), True)
      for name in ("document_history_get", "business_history_list")),
    CatalogEntry("dev_tickets_status_set", False, "records", frozenset({"dashboard"}), True),
    # Retained internal reader for surviving Task, Signoff and Project references. The agent MCP caller
    # refuses it in discovery and dispatch; phase 2 removes it with the remaining views and data.
    CatalogEntry("dev_tickets_get", False, 'read', frozenset({'mcp'})),
    *(CatalogEntry(name, False, 'read', frozenset({'dashboard'}), True) for name in ('system_incidents_list', 'system_incidents_get')),
    CatalogEntry("learnings_decide", False, "records", frozenset({"mcp"})),
    CatalogEntry("viewbox_place", False, "records", frozenset({"mcp"})),
    CatalogEntry("focus_get", False, "read"),
    *(CatalogEntry(name, False, "records") for name in ("focus_present", "focus_edit", "focus_move", "focus_set_archived", "focus_reply", "focus_show", "focus_clear", "focus_browser_input")),
    CatalogEntry("working_memory_update", False, "compound"),
    CatalogEntry("agent_work_update", False, "records"),
    CatalogEntry("environment_list", False, "read", frozenset({"mcp", "dashboard"})),
    CatalogEntry("environment_get", False, "read", frozenset({"mcp", "dashboard"})),
    CatalogEntry("environment_set", True, "compound", frozenset({"mcp", "dashboard"})),
    CatalogEntry("browser_turn", False, "records", frozenset({"mcp"})),  # Descriptor installed by the MCP bridge.
    CatalogEntry("agent_browser_manage", False, "records", frozenset({"mcp"})),  # Descriptor installed by the MCP bridge.
    # Ordubashy's presentation of existing records in Chat; a read that returns at once and changes nothing.
    CatalogEntry("request_user_input", False, "read", frozenset({"mcp"})),
    *(CatalogEntry(name, False, "records") for name in ("management_route", "management_message_send", "management_resolve")),
    *MUTATION_ENTRIES,
    *READ_ENTRIES,
    *(CatalogEntry(name, False, "read") for name in ("management_list", "management_get")),
    CatalogEntry("management_coordinator_set", False, "records", frozenset({"dashboard", "worker"}), True),
    *(CatalogEntry(name, False, kind) for name, kind in (("owner_conversation_get", "read"), ("owner_conversation_send", "records"))),
    # Owner-directed code sessions only; registered agents keep their managed bootstrap.
    CatalogEntry("owner_session_bootstrap", False, "read", frozenset({"mcp"})),
    CatalogEntry("owner_conversations_list", False, "read", frozenset({"dashboard", "worker"}), True),
    CatalogEntry("owner_conversation_related", False, "read", frozenset({"dashboard", "worker"}), True),
    CatalogEntry("owner_conversation_peers", False, "read", frozenset({"dashboard", "worker"}), True),
    CatalogEntry("owner_conversation_mark_read", False, "records", frozenset({"dashboard", "worker"}), True),
    # Project records: one existing Project's inventory, neutral references and explicit cross-scope links. The
    # handler adds the owner/configured-Ordubashy identity gate and the Project's current reach to every call.
    *(CatalogEntry(name, False, kind) for name, kind in (
        ("project_record_get", "read"), ("project_items_list", "read"), ("project_items_candidates", "read"),
        ("project_item_get", "read"), ("project_items_link", "records"), ("project_items_unlink", "records"))),
    # Move to project: destination listing, the read-only preview and the one atomic relocation of Tasks with their descendants.
    # The handler adds the owner/configured-Ordubashy identity gate and the source and destination Task authority to every call.
    *(CatalogEntry(name, False, kind) for name, kind in (
        ("tasks_move_targets", "read"), ("tasks_move_to_project_preview", "read"), ("tasks_move_to_project", "records"))),
    # One record's authorized activity: the Domain's local activity merged with its directly linked sources (host-owned).
    CatalogEntry("crm_activity_list", False, "read"),
    # Narrow owner/Ordubashy read of the owner's stored Project reminders; the shared reminders_* operations own writes and delivery.
    CatalogEntry("owner_reminders_list", False, "read", frozenset({"dashboard", "worker"})),
    *BROADCAST_READ_ENTRIES,
    CatalogEntry("learnings_intake_status", False, "read", frozenset({"dashboard", "worker"}), True),
    *(CatalogEntry(name, False, "read", frozenset({"mcp"})) for name in ("journal_entries_list", "journal_entry_get")),
    CatalogEntry("business_restore", False, "records", frozenset({"dashboard"}), True),
    CatalogEntry("business_archived_list", False, "read", frozenset({"dashboard"}), True),
    *(CatalogEntry(name, False, "read") for name in _READS),
    *(CatalogEntry(name, True, "records") for name in _MUTATIONS),
    *(CatalogEntry(name, False, "read", frozenset({"dashboard", "worker"})) for name in _CONTEXT_READS),
    *(CatalogEntry(name, False, "records", frozenset({"dashboard", "worker"}), True) for name in _OWNER),
    CatalogEntry("terminals_list", False, "read", frozenset({"dashboard", "worker"}), True),
    # Private provider calendars, exposed only with the explicit calendar grant.
    *(CatalogEntry(name, False, "read", frozenset({"mcp", "dashboard"})) for name in
      ("calendar_calendars_list", "calendar_events_list", "calendar_event_get")),
    *(CatalogEntry(name, False, "compound", frozenset({"mcp", "dashboard"})) for name in
      ("calendar_event_create", "calendar_event_update", "calendar_event_delete", "calendar_event_respond")),
    # Legacy reminder alias remains available independently of an owner delivery connector.
    CatalogEntry("owner_reminder", False, "records", frozenset({"mcp", "dashboard"})),
    # Shared reminders for the owner, Ordubashy and other registered agents; owner_reminder and owner_reminders_list are thin wrappers.
    *(CatalogEntry(name, False, kind, frozenset({"mcp", "dashboard", "worker"})) for name, kind in (
        ("reminders_create", "records"), ("reminders_get", "read"), ("reminders_list", "read"), ("reminders_update", "records"),
        ("reminders_snooze", "records"), ("reminders_dismiss", "records"), ("reminders_cancel", "records"))),
    *(CatalogEntry(name, True, "records", frozenset({"worker"})) for name in _WORKERS),
)



def builtin_registry(extensions=NO_EXTENSIONS) -> OperationRegistry:
    """The open registry: the host's and the engine's metadata plus that of the selected extensions. An extension's
    metadata may not replace a core entry, and its handlers are installed later by the extension itself."""
    from qorqut_operations.application.batches import descriptor
    contributed = tuple(entry for contribution in extensions.catalog() for entry in contribution.entries)
    host = {entry.name for entry in (*BUILTIN_CATALOG, *contributed)}
    shared = sorted(host & {entry.name for entry in ENGINE_ENTRIES})
    if shared:
        raise ValueError("Operation metadata is declared by both the host and the Operations engine: " + ", ".join(shared))
    taken = sorted({entry.name for entry in contributed} & {entry.name for entry in (*BUILTIN_CATALOG, *ENGINE_ENTRIES)})
    if taken or len({entry.name for entry in contributed}) != len(contributed):
        raise ValueError("An extension may not redeclare operation metadata: " + ", ".join(taken))
    registry = OperationRegistry((*BUILTIN_CATALOG, *ENGINE_ENTRIES, *contributed))
    registry.extension_policy_rules = extensions.policy_rules
    registry.extension_operation_guidance = extensions.operation_guidance
    registry.extension_global_discovery = extensions.global_discovery_operations
    registry.register(descriptor())
    return registry


def install_contribution(registry: OperationRegistry, contribution: CatalogContribution) -> CatalogContribution:
    """Install one engine catalog contribution on the open registry: its metadata must equal what was declared and
    each handler is registered once. Registration ends when the runtime freezes the registry."""
    if type(contribution) is not CatalogContribution:
        raise ValueError("Only typed catalog contributions made by trusted assembly code can be installed")
    for entry in contribution.entries:
        registry.declare(entry)
    for descriptor in contribution.descriptors:
        registry.register(descriptor)
    return contribution
