"""Owner-session profile, credentials, and the explicit MCP operation catalog.

Owner-directed code sessions are interactive CLI conversations acting with the
installation owner's own identity inside one configured business
(docs/plans/OWNER-DIRECTED-CODE-SESSIONS.md). This module owns the durable
pieces: the optional ``owner_sessions`` business-configuration section, the
per-machine owner credentials bound to that business through trusted
``credentials.metadata_json``, and the fixed initial operation catalog.
Newly registered operations never enter the catalog automatically.
"""

from copy import deepcopy
import hashlib
import json
import secrets

from qorqut_operations.contracts import DomainError, new_id, utc_now

PURPOSE = "owner_session"
METADATA_VERSION = 1
CONFIG_KEY = "owner_sessions"

READ_OPERATIONS = frozenset({
    "operations_describe", "owner_session_bootstrap",
    "context_list", "context_search", "context_get",
    "context_home", "context_object", "context_expand", "context_find",
    "context_people_list", "context_people_get", "context_maps_get",
    "context_items_list", "context_items_get", "context_navigation_get", "context_graph_get",
    "context_reviews_list", "context_reviews_history", "relationship_roles_list",
    "relationships_list", "relationships_get", "tables_list", "tables_get", "tables_rows_list",
    "tables_history", "crm_entries_list", "tables_thread_get", "crm_activity_list", "data_uploads_list", "data_uploads_get",
    "content_list", "content_get", "notes_list", "notes_get",
    "assets_list", "assets_get", "actions_list", "actions_get", "actions_config_get", "tags_list",
    "business_progress_get",
    "environment_list", "environment_get", "business_config_get",
    "tasks_list", "tasks_get", "tasks_notes_list", "tasks_thread_get",
    "tasks_config_get", "assignees_list",
    "signoffs_list", "signoffs_get", "signoffs_notes_list",
    "owner_conversation_get", "agents_messages_list", "agents_message_recipients",
})

# Owner-requested saves: a specific owner instruction authorizes the write, so
# these use the ordinary human-owner policy without Signoff.
# Shared-discussion notes stay excluded: the Task note fields already refuse the
# installation owner with channel_required, and so do tables_comment and crm_activity_record.
WRITE_OPERATIONS = frozenset({
    "owner_conversation_send",
    "tasks_create", "tasks_update", "tasks_assign", "tasks_complete", "tasks_config_update",
    "context_edit", "context_instruct", "context_upload",
    "context_items_archive", "materials_update", "context_review",
    "context_review_reopen",
    "relationships_create", "relationships_update", "relationships_remove",
    "tables_create", "tables_update", "tables_rows_write", "tables_history_retry",
    "crm_entries_add", "crm_entries_update", "crm_entries_remove",
    "data_uploads_upload", "assets_upload", "assets_update", "assets_delete",
    "notes_create", "notes_update", "notes_delete", "notes_upload",
    "actions_record", "business_config_update", "business_config_patch", "environment_set",
    "agents_message_send",
})

# These are owner-only in the normal registry. They are deliberately listed
# here so the owner route can expose only this small, audited exception set.
OWNER_ROUTE_OPERATIONS = frozenset({"context_instruct"})
CATALOG = READ_OPERATIONS | WRITE_OPERATIONS | OWNER_ROUTE_OPERATIONS | frozenset({"operations_batch", "operations_describe"})


def validate_section(section):
    if (not isinstance(section, dict) or set(section) - {"enabled", "revision"}
            or not isinstance(section.get("enabled"), bool)
            or ("revision" in section and (type(section["revision"]) is not int or section["revision"] < 1))):
        raise DomainError("validation_error",
                          "owner_sessions accepts enabled (boolean) and revision (positive integer) only")


def prepare_owner_sessions(config, previous, *, principal_kind):
    """Business-configuration validator: the section is owner-managed."""
    if CONFIG_KEY not in config:
        if CONFIG_KEY in previous:
            config[CONFIG_KEY] = deepcopy(previous[CONFIG_KEY])
        return
    if principal_kind != "human" and config[CONFIG_KEY] != previous.get(CONFIG_KEY):
        raise DomainError("forbidden", "Owner-session enablement is owner-managed")
    validate_section(config[CONFIG_KEY])


PROTECTED_CONFIGURATION_KEYS = frozenset({
    CONFIG_KEY, "delegation", "identity", "id", "slug", "workspace", "hq_path",
    "owner_principal_id", "registered_paths", "permissions", "grants", "role",
    "plugins", "tool_policy", "agent_policy", "registered_agents",
})


def protect_owner_session_configuration(config, previous, *, owner_session=False, explicit_deletions=()):
    """Preserve authority and identity controls during owner-session settings writes.

    A full replacement must retain protected sections, while a patch/replacement
    that attempts to alter or delete one is rejected. Engineering settings
    intentionally remain editable by this route.
    """
    if not owner_session:
        return
    previous = previous if isinstance(previous, dict) else {}
    explicit_deletions = set(explicit_deletions)
    for key in PROTECTED_CONFIGURATION_KEYS:
        if key in explicit_deletions:
            raise DomainError("forbidden", f"Owner sessions cannot delete protected project setting: {key}")
        if key not in previous:
            if key in config:
                raise DomainError("forbidden", f"Owner sessions cannot introduce protected project setting: {key}")
            continue
        if key in config and config[key] != previous[key]:
            raise DomainError("forbidden", f"Owner sessions cannot change protected project setting: {key}")
        config[key] = deepcopy(previous[key])


def enabled(config_json):
    section = config_json.get(CONFIG_KEY) if isinstance(config_json, dict) else None
    return isinstance(section, dict) and section.get("enabled") is True


def credential_binding(metadata_json):
    """Return the trusted business binding, or None for non-owner-session rows."""
    metadata = metadata_json if isinstance(metadata_json, dict) else None
    if metadata is None:
        try:
            metadata = json.loads(metadata_json or "{}")
        except (ValueError, TypeError):
            return None
    if (not isinstance(metadata, dict) or metadata.get("purpose") != PURPOSE
            or metadata.get("version") != METADATA_VERSION
            or not isinstance(metadata.get("business_id"), str)):
        return None
    return metadata


def issue_credential(database, *, business_id, label, endpoint=None, roots=None):
    """Issue one revocable per-machine owner-session credential for a business.

    Re-issuing for the same machine label revokes that label's previous
    credential; other machines' credentials stay valid. ``endpoint`` and
    ``roots`` record that machine's prepared service URL and local code-root
    mapping as installation metadata; they are diagnostics, never authority.
    """
    from qorqut_operations.application.engine import Application

    if not isinstance(label, str) or not label.strip() or len(label) > 200 or "\n" in label:
        raise DomainError("validation_error", "A short machine label is required")
    label = label.strip()
    token = secrets.token_urlsafe(48)
    with database.transaction() as uow:
        Application.ensure_writes_open(uow)
        installation = uow.repository("installation").get("service")
        owner_id = installation["owner_principal_id"] if installation else None
        owner = uow.repository("principals").get(owner_id) if owner_id else None
        if owner is None or owner["kind"] != "human" or not owner["active"]:
            raise DomainError("unauthenticated", "An active installation owner must be registered")
        business = uow.repository("businesses").get(business_id, required=True)
        if business["archived_at"]:
            raise DomainError("business_archived", "Owner sessions cannot be issued for an archived project")
        if not enabled(business["config_json"]):
            raise DomainError("forbidden", "Owner sessions are not enabled for this project")
        member = uow.repository("business_memberships", business_id=business_id).get(owner_id)
        if not member or not member["active"] or not member["is_owner"]:
            raise DomainError("forbidden", "The installation owner must own this project")
        for row in uow.connection.execute(
                "SELECT id, metadata_json, revision FROM credentials WHERE principal_id=? AND revoked_at IS NULL",
                (owner_id,)).fetchall():
            binding = credential_binding(row["metadata_json"])
            if binding and binding["business_id"] == business_id and binding.get("label") == label:
                uow.repository("credentials").update(row["id"], {"revoked_at": utc_now()},
                                                     expected_revision=row["revision"])
        metadata = {"purpose": PURPOSE, "version": METADATA_VERSION,
                    "business_id": business_id, "label": label}
        if endpoint is not None:
            metadata["endpoint"] = str(endpoint)
        if roots:
            metadata["roots"] = [str(root) for root in roots]
        uow.repository("credentials").insert({
            "id": new_id(), "principal_id": owner_id,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "metadata_json": metadata})
    return token
