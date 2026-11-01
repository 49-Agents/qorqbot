"""MCP-only indexes and receipts; canonical domain results remain authoritative.

Projection happens after execution, including idempotent replay. Detail selection
must never enter a prepared mutation or replace an evidence read.
"""

from collections import Counter
import hashlib
import json
import re

import yaml

from bazarlyk.contracts import DomainError


TASK_WRITES = frozenset({"tasks_create", "tasks_update", "tasks_assign", "tasks_complete", "tasks_comment"})
STORY_WRITES = frozenset({"stories_create", "stories_update", "stories_note_add", "stories_draft_commit",
    "stories_link_add", "stories_link_remove", "stories_campaign_responsible_set",
    "stories_context_review", "stories_context_reopen"})
IDEA_WRITES = frozenset({"ideas_create", "ideas_update"})
# Card writes return the touched card's identity/revision; focus_reply keeps its
# note-thread payload because the new note is the receipt. The legacy
# focus_show/focus_clear/focus_browser_input trio always answers upgrade_required
# and is deliberately not projected.
FOCUS_CARD_WRITES = frozenset({"focus_present", "focus_edit", "focus_move", "focus_set_archived"})
SIGNOFF_COMPLETION_WRITES = frozenset({"signoffs_reddit_approve", "signoffs_email_approve",
    "signoffs_reject", "signoffs_input_cancel", "signoffs_input_answer_delegated"})
RECEIPT_WRITES = frozenset({"agents_message_send", "signoffs_input_create"}) | FOCUS_CARD_WRITES
INDEX_READS = frozenset({"signoffs_list", "signoffs_get", "inbox_list", "agents_activity_list",
    "agents_messages_list", "agents_message_recipients", "broadcast_recipients", "tables_rows_list",
    "stories_list", "tasks_get", "tasks_notes_list", "operations_describe"})
# Already-lean reads gain default elision and the full-detail escape hatch only.
ELIDE_ONLY_READS = frozenset({"tasks_overview", "focus_get", "assignees_list", "tables_list"})
COMPACT_OPERATIONS = (TASK_WRITES | STORY_WRITES | IDEA_WRITES | RECEIPT_WRITES | INDEX_READS | SIGNOFF_COMPLETION_WRITES
    | ELIDE_ONLY_READS | {"tasks_list", "tables_rows_write", "agent_config_get"})
SUMMARY_OPERATIONS = frozenset({"focus_get", "agents_activity_list"})
DETAIL_SCHEMA = {"enum": ["compact", "full", "text"], "default": "compact",
    "description": "Compact receipt/index by default; platform defaults may be omitted, while authored sections remain exact. Use full for the canonical payload; text renders the compact result as YAML (fewest tokens; not inside batches). On write retries reuse the same idempotency_key."}
SUMMARY_DETAIL_SCHEMA = {"enum": ["compact", "summary", "full", "text"], "default": "compact",
    "description": "Compact by default. Summary is a bounded operational view (Focus boards or selected cards, without conversations or decision evidence); full is canonical. Text renders compact as YAML, not inside batches."}
# false means "default/absent" for these flags; other false values (available,
# actionable, ready, all_ok, sending_configured, ...) are meaningful answers.
ELIDE_FALSE_KEYS = frozenset({"closed", "paused", "archived", "snoozed", "has_more",
    "metadata_stale", "blocked", "stale",
    "inbox_owner", "work_stale", "is_owner", "title_truncated"})
# Table cell values are data, and attention flags are read-before-write state
# that agents send back complete; neither is a presentation default. A null
# business_id inside source means "Global object" and is not a default either.
ELIDE_PRESERVED_KEYS = frozenset({
    # These are authored or arbitrary user-data sections. Their empty, false,
    # zero and null values are meaningful and must not be treated as platform
    # defaults by recursive presentation elision.
    "attention", "body", "cells", "content", "custom_fields", "data_json", "delegation_attribution",
    "deferred_until",
    "metadata", "notes", "payload", "reason", "references", "requested_response", "source",
    "summary", "values",
})
CONFIG_DETAIL_SCHEMA = {"enum": ["compact", "summary", "context", "full"], "default": "compact",
    "description": "Identity/scope by default; summary is readiness metadata, not instructions. Context without sections returns complete mandatory rules without inventories or work briefing. Full returns diagnostic detail."}
TOOL_NAMES_SCHEMA = {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
    "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$", "maxLength": 100},
    "description": "Optional exact operation names/aliases whose current availability and permission to include. Unknown names are explicit. Use with compact or context."}
SECTIONS_SCHEMA = {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
    "items": {"type": "string", "minLength": 1, "maxLength": 120},
    "description": "context only: return exactly these '## ' sections of the mandatory rules, with a hash manifest of every omitted section. Omit for the full rendered bundle."}
TASK_FIELDS = ("id", "revision", "title", "status", "due", "follow_up", "snooze", "snoozed", "assignee_ids", "blocked_by",
    "unresolved_blockers", "blocks", "closed", "paused", "archived", "tags", "created_by", "updated_at")
STORY_FIELDS = ("id", "revision", "sequence", "title", "state", "state_reason", "next_step", "due",
    "follow_up", "closed", "archived", "outcome", "tags", "responsible_agent_id",
    "pending_context_count", "updated_at")
EVENT_FIELDS = ("id", "story_id", "sequence", "kind", "parent_event_id", "actor_id", "created_at", "created_by", "prepared_at")
SIGNOFF_FIELDS = ("id", "review_business_id", "review_scope", "proposal_revision", "payload_hash", "status_revision",
    "state", "decision_state", "actionable", "execution_state", "category", "operation_name", "operation_version",
    "title", "proposer_principal_id", "proposer_agent_id", "proposer_display_name", "proposer_department",
    "initiated_by_principal_id", "initiated_by_display_name", "attention", "job_id",
    "created_at", "updated_at", "updated_sequence", "supersedes_id", "superseded_by_id", "legacy_review_routing")
IDEA_FIELDS = ("id", "revision", "title", "summary", "status", "tags", "document_id", "content_hash",
    "metadata_stale", "business_id", "business_slug", "parent_idea_id", "created_at", "updated_at")
MESSAGE_RECEIPT_FIELDS = ("id", "event_id", "thread_id", "created_at", "recipient_id", "recipient_name", "terminal_delivery")
FOCUS_ITEM_FIELDS = ("id", "revision", "archived", "unavailable")
# Index rows keep identities/revisions the agent passes back and the human-readable
# columns; duplicated identity spellings, per-row attribution and echoed parent IDs
# are read through the full-detail escape hatch or the record's own read tool.
SIGNOFF_INDEX_FIELDS = ("id", "review_scope", "proposal_revision", "status_revision", "state",
    "decision_state", "actionable", "execution_state", "category", "operation_name", "title",
    "proposer_agent_id", "proposer_display_name", "proposer_department", "initiated_by_display_name",
    "attention", "job_id", "created_at", "updated_at", "supersedes_id", "superseded_by_id")
INBOX_EVENT_FIELDS = ("id", "source_type", "source_id", "event_type", "summary", "created_at",
    "business_id", "business_slug", "actor_name", "message", "source")
MESSAGE_ITEM_FIELDS = ("id", "event_id", "thread_id", "reply_to", "sender_id", "sender_name",
    "recipient_id", "recipient_name", "current_sender_id", "current_sender_name", "current_recipient_id",
    "current_recipient_name", "counterpart_id", "body", "summary", "created_at", "acknowledged_at", "outcome",
    "delegation_attribution", "terminal_delivery")
ACTIVITY_AGENT_FIELDS = ("agent_id", "name", "role_id", "business_id", "business_slug",
    "state", "last_seen_at", "sessions", "inbox")
ACTIVITY_SESSION_FIELDS = ("session_id", "runtime", "state", "last_seen_at", "work_revision",
    "items", "item_keys", "items_complete", "items_replacement_safe", "checkpoint", "inbox_owner", "work_stale")
RECIPIENT_FIELDS = ("agent_id", "name", "role_id", "business_id", "business_slug", "group")
TABLE_ROW_FIELDS = ("id", "position", "revision", "cells")
TASK_NOTE_FIELDS = ("id", "body", "created_by_name", "created_at", "refs")


def select(value, keys):
    return {key: value[key] for key in keys if key in value}


def message_item(value):
    """Compact message; current participants appear only when they differ from the originals."""
    item = select(value, MESSAGE_ITEM_FIELDS)
    for side in ("sender", "recipient"):
        if item.get(f"current_{side}_id") == item.get(f"{side}_id"):
            item.pop(f"current_{side}_id", None)
            item.pop(f"current_{side}_name", None)
    return item


class _TextDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, value):
    # Literal blocks keep multi-line bodies unescaped; the emitter falls back
    # to quoting when the text cannot be a block scalar.
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_TextDumper.add_representer(str, _str_representer)


def serialize(value):
    """One text block per result: compact JSON, or YAML when text detail was chosen.

    Errors and non-text details keep the JSON contract; the YAML rendering is
    the same slimmed envelope without the JSON quoting/escaping cost.
    """
    if isinstance(value, dict) and value.get("response_detail") == "text" and value.get("status") != "error":
        rendered = {key: item for key, item in value.items() if key != "response_detail"}
        return "response_detail: text\n" + yaml.dump(rendered, Dumper=_TextDumper,
            sort_keys=False, allow_unicode=True, width=100000)
    return json.dumps(value, separators=(",", ":"))


def finalize(value):
    """Strip correlation/refresh scaffolding from successful wire envelopes.

    Errors keep their correlation ID and full envelope; a true refresh signal
    keeps its configuration revision. Applies to the MCP wire only, after
    incident reporting has consumed the correlation ID.
    """
    if not isinstance(value, dict) or value.get("status") == "error":
        return value
    slimmed = dict(value)
    slimmed.pop("request_id", None)
    if not slimmed.get("context_refresh_required"):
        slimmed.pop("configuration_revision", None)
        slimmed.pop("context_refresh_required", None)
    if not slimmed.get("warnings"):
        slimmed.pop("warnings", None)
    data = slimmed.get("data")
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        rows = [{**row, "result": finalize(row["result"])}
                if isinstance(row.get("result"), dict) and "status" in row["result"] else row
                for row in data["results"] if isinstance(row, dict)]
        if len(rows) == len(data["results"]):
            slimmed["data"] = {**data, "results": rows}
    return slimmed


def split_detail(name, arguments):
    if not isinstance(arguments, dict):
        raise DomainError("validation_error", "Operation arguments must be an object")
    arguments = dict(arguments)
    if "response_detail" not in arguments:
        return arguments, "compact"
    if name not in COMPACT_OPERATIONS:
        raise DomainError("validation_error", "response_detail is not supported by this operation")
    detail = arguments.pop("response_detail")
    choices = (CONFIG_DETAIL_SCHEMA["enum"] if name == "agent_config_get"
               else SUMMARY_DETAIL_SCHEMA["enum"] if name in SUMMARY_OPERATIONS
               else DETAIL_SCHEMA["enum"])
    if detail not in choices:
        raise DomainError("validation_error", "response_detail must be " + ", ".join(choices))
    if name == "focus_get" and detail == "summary" and arguments.get("conversation"):
        raise DomainError("validation_error", "response_detail=summary does not include conversations; omit conversation or use compact/full")
    return arguments, detail


def split_tool_names(name, arguments, detail):
    """Transport selection never enters domain arguments or grants a capability."""
    arguments = dict(arguments)
    if "tool_names" not in arguments:
        return arguments, None
    names = arguments.pop("tool_names")
    if (name != "agent_config_get" or detail not in {"compact", "context"} or not isinstance(names, list)
            or not 1 <= len(names) <= 20
            or any(not isinstance(item, str) or len(item) > 100 or not re.fullmatch(r"[a-z][a-z0-9_]*", item) for item in names)
            or len(set(names)) != len(names)):
        raise DomainError("validation_error", "tool_names requires 1–20 unique operation names on compact/context agent_config_get")
    return arguments, names


def bounded_preview_value(value):
    if isinstance(value, (list, tuple)) and len(value) > 20:
        return {"omitted_entries": len(value), "read": "response_detail=full"}
    if isinstance(value, dict) and len(value) > 20:
        return {"omitted_keys": len(value), "read": "response_detail=full"}
    if isinstance(value, str) and len(value) > 2000:
        return {"omitted_chars": len(value), "head": value[:200], "read": "response_detail=full"}
    return value


def preview_change(change):
    """One encoding per change: diffs for text, changed fields for structures."""
    if not isinstance(change, dict):
        return change
    if change.get("kind") == "text" and change.get("diff") is not None:
        if change.get("before") == change.get("after"):
            return {**{key: change[key] for key in ("resource", "kind") if key in change}, "unchanged": True}
        return {key: item for key, item in change.items() if key not in ("before", "after")}
    if change.get("kind") == "structured" and isinstance(change.get("fields"), list):
        fields = [{**field, "before": bounded_preview_value(field.get("before")),
                   "after": bounded_preview_value(field.get("after"))}
                  for field in change["fields"]
                  if not (isinstance(field, dict) and field.get("before") == field.get("after"))]
        return {**change, "fields": fields}
    return change


def elide(value, *, preserved_keys=ELIDE_PRESERVED_KEYS):
    """Drop empty/default fields from a compact projection; absence means default."""
    if isinstance(value, dict):
        slimmed = {}
        for key, child in value.items():
            if key in preserved_keys:
                slimmed[key] = child
                continue
            child = elide(child, preserved_keys=preserved_keys)
            if child is None or child == "" or (isinstance(child, (list, dict, tuple)) and not child):
                continue
            if child is False and key in ELIDE_FALSE_KEYS:
                continue
            slimmed[key] = child
        return slimmed
    if isinstance(value, (list, tuple)):
        return [elide(item, preserved_keys=preserved_keys) for item in value]
    return value


def split_sections(name, arguments, detail):
    """Transport selection of mandatory-rule sections; never a domain argument."""
    arguments = dict(arguments)
    if "sections" not in arguments:
        return arguments, None
    sections = arguments.pop("sections")
    if (name != "agent_config_get" or detail != "context" or not isinstance(sections, list)
            or not 1 <= len(sections) <= 20
            or any(not isinstance(item, str) or not 1 <= len(item) <= 120 for item in sections)
            or len(set(sections)) != len(sections)):
        raise DomainError("validation_error", "sections requires 1-20 unique section titles on context agent_config_get")
    return arguments, sections


def sectioned_context(mandatory, requested):
    """Named sections verbatim plus a hash manifest of everything omitted.

    Nothing is silently hidden: bundle_hash still covers the whole rendered
    bundle, and every omitted section is named with its own hash and size.
    """
    rendered = mandatory.get("rendered") or ""
    parts = re.split(r"(?m)^(?=## )", rendered)
    preamble, sections = parts[0], parts[1:]
    matched, included, omitted = set(), [], []
    for part in sections:
        title = part.splitlines()[0][3:].strip()
        if title in requested:
            matched.add(title)
            included.append(part)
        else:
            omitted.append({"section": title, "sha256": hashlib.sha256(part.encode()).hexdigest(),
                            "characters": len(part)})
    result = select(mandatory, ("bundle_hash", "character_count"))
    result["rendered"] = preamble + "".join(included)
    result["omitted_sections"] = omitted
    unknown = [title for title in requested if title not in matched]
    if unknown:
        result["unknown_sections"] = unknown
    return result


def description(name):
    if name in TASK_WRITES or name == "tasks_list":
        return " Returns a compact task index/receipt; tasks_get reads fields and recent notes; tasks_notes_list pages older notes."
    if name in STORY_WRITES:
        return " Returns a compact Story/event receipt; stories_get and stories_event_get read details, or request response_detail=full."
    if name == "tables_rows_write":
        return " Returns row IDs/revisions, not echoed cells; tables_rows_list reads values, or request response_detail=full."
    if name == "agent_config_get":
        return " Compact identity/scope by default. response_detail=summary is readiness metadata only, not instructions. Before entering another business or when context_refresh_required, read response_detail=context without sections for complete mandatory rules. sections is a partial lookup, never a substitute for that read; tool_names checks named capabilities; full returns diagnostic detail."
    if name == "signoffs_list":
        return " Compact status/attention index. signoffs_get reads full proposal/answer evidence before acting; response_detail=full retains decision notes for bulk feedback review without per-item reads."
    if name == "signoffs_get":
        return " Decision bindings and approval evidence are complete; change previews keep diffs and changed fields only, and mark bounded values. response_detail=full returns raw before/after."
    if name in SIGNOFF_COMPLETION_WRITES:
        return " Returns a short decision/execution receipt; approval may only queue execution. signoffs_get reads evidence; response_detail=full with the same idempotency key reads the canonical receipt without repeating the decision."
    if name in IDEA_WRITES:
        return " Returns identity, revision and content hash without echoing the draft/history. Read ideas_get for evidence, or replay with response_detail=full and the same write key."
    if name == "agents_message_send":
        return " Returns a delivery receipt (message/thread/event IDs), not the sent text; agents_messages_list reads history, or request response_detail=full."
    if name == "signoffs_input_create":
        return " Returns the created signoff's status index; signoffs_get reads the full question and choices, or request response_detail=full."
    if name in FOCUS_CARD_WRITES:
        return " Returns a compact Focus receipt with revisions; focus_get reads the full pane, or request response_detail=full."
    if name in SUMMARY_OPERATIONS:
        return " Compact index by default. response_detail=summary is an explicit bounded operational view; response_detail=full returns the canonical payload."
    if name == "tasks_get":
        return " Note text is complete; omitted per-note attribution and empty fields are defaults. response_detail=full returns the canonical payload."
    if name == "operations_describe":
        return " Multiple names return availability/mode/description; name a single tool (or use response_detail=full) for its exact input schema."
    if name in INDEX_READS or name in ELIDE_ONLY_READS:
        return " Compact index: omitted fields are empty or default. response_detail=full returns the canonical payload."
    return ""


def _focus_summary_item(item):
    """Project one board card without conflating a source and a Reference."""
    if not isinstance(item, dict):
        return item
    # Domain redaction deliberately withholds the type and scope. Do not turn
    # an inaccessible card into an apparently Global Reference.
    if item.get("unavailable"):
        return select(item, ("id", "revision", "archived", "unavailable"))
    source = item.get("source")
    context = item.get("context")
    binding = source or context or {}
    business_id = binding.get("business_id") if isinstance(binding, dict) else None
    result = select(item, ("id", "revision", "position", "archived", "unavailable",
        "display_name", "label", "reason", "requested_response", "blocker", "urgent",
        "deferred_until", "private_unread", "private_message_count"))
    result["title"] = item.get("display_name") or item.get("label") or (
        item.get("reference", {}).get("title") if isinstance(item.get("reference"), dict) else "")
    result.pop("display_name", None)
    result.pop("label", None)
    result["card_kind"] = "source" if isinstance(source, dict) else "reference"
    result["scope"] = {"kind": "global" if business_id is None else "business",
                        "business_id": business_id}
    if item.get("context_business_slug"):
        result["scope"]["business_slug"] = item["context_business_slug"]
    if isinstance(source, dict):
        result["source"] = source
        result["source_state"] = item.get("state")
        result["source_revision"] = item.get("source_revision")
    else:
        # Keep the opening target and conversation context as separate typed
        # objects. The target is deliberately not replaced with context.
        result["reference"] = select(item.get("reference", {}), ("key", "title", "target"))
        if isinstance(context, dict):
            result["context"] = context
            result["context_revision"] = item.get("source_revision")
    return result


def _signoff_completion(signoff):
    """A decision receipt, not another delivery of the reviewed proposal.

    Keep execution separate from decision state. Execution errors/results and
    decision message references are retained even when that exceeds the normal
    small success receipt. Read tools and canonical/HTTP results stay complete.
    """
    result = select(signoff, ("id", "proposal_revision", "status_revision",
        "state", "decision_state", "execution_state"))
    for key in ("error", "apply_result"):
        if signoff.get(key) is not None:
            result[key] = signoff[key]
    decision = signoff.get("decision")
    if isinstance(decision, dict) and decision.get("message_id"):
        result["decision_message_id"] = decision["message_id"]
    return result


def _checkpoint_identity(issue):
    """Return only an explicitly authoritative finding identity."""
    if not isinstance(issue, dict):
        return None
    for key in ("finding_id", "check_key", "identity"):
        value = issue.get(key)
        if isinstance(value, (str, int)) and value != "":
            return (key, value)
    return None


def _activity_work_item(item):
    if not isinstance(item, dict):
        return item
    # Keep attribution, task identity and the actionable state. Expanded
    # blocker records remain available from the existing detailed read.
    return select(item, ("business_id", "business_slug", "task_id", "title", "task_status",
        "summary", "blocked", "blocked_reason"))


def _activity_summary(data):
    agents = []
    for agent in data.get("agents", []):
        if not isinstance(agent, dict):
            agents.append(agent)
            continue
        row = select(agent, ("agent_id", "name", "role_id", "business_id",
            "business_slug", "state", "last_seen_at", "inbox"))
        sessions = []
        findings = {}
        identities_available = True
        for session in agent.get("sessions", []):
            checkpoint = session.get("checkpoint") if isinstance(session, dict) else None
            issues = checkpoint.get("issues") if isinstance(checkpoint, dict) else None
            for issue in issues if isinstance(issues, list) else []:
                identity = _checkpoint_identity(issue)
                if identity is None or (identity in findings and findings[identity] != issue):
                    identities_available = False
                else:
                    findings[identity] = issue
        for session in agent.get("sessions", []):
            if not isinstance(session, dict):
                sessions.append(session)
                continue
            checkpoint = session.get("checkpoint")
            session_view = select(session, ("session_id", "runtime", "state", "last_seen_at",
                "work_revision", "inbox_owner", "work_stale"))
            # The list is bounded and may already be authorization-filtered by
            # the domain. It is useful current-work information, never a safe
            # replacement payload for agent_work_update.
            session_view["items"] = [_activity_work_item(item) for item in session.get("items", [])]
            if isinstance(checkpoint, dict):
                checkpoint_view = select(checkpoint, ("state", "last_checked_at", "outstanding_count"))
                issues = checkpoint.get("issues")
                if isinstance(issues, list):
                    # Keep the session's checkpoint state, and keep findings
                    # here when identity is unavailable rather than hiding an
                    # unresolved observation.
                    if not identities_available:
                        checkpoint_view["issues"] = issues
                session_view["checkpoint"] = checkpoint_view
            sessions.append(session_view)
        row["sessions"] = sessions
        row["work_items_complete"] = False
        row["work_items_replacement_safe"] = False
        row["not_included"] = ["expanded blocker records", "complete checkpoint inventory"]
        if identities_available:
            row["checkpoint_findings"] = {
                "items": list(findings.values()), "complete": False,
                "note": "bounded observations; not a complete inventory",
            }
        else:
            row["checkpoint_findings"] = {
                "not_included": "not deduplicated; finding identities were unavailable or observations differed; session findings retained"
            }
        agents.append(row)
    return {**data, "agents": agents}


def _config_summary(config):
    result = select(config, ("schema_version", "agent_id", "profile_revision", "name", "display_name",
        "status", "runtime", "business_id", "business_revision", "configuration_revision",
        "context_refresh_required", "scope"))
    if isinstance(config.get("role"), dict):
        result["role"] = select(config["role"], ("id", "version"))
    elif config.get("role") is not None:
        result["role"] = config["role"]
    preparation = config.get("preparation")
    if isinstance(preparation, dict):
        drift = preparation.get("projection_drift")
        missing = preparation.get("missing_artifacts")
        result["readiness"] = {
            "ready": preparation.get("ready"),
            "status": preparation.get("status"),
            "refresh_required": bool(config.get("context_refresh_required")),
            "problem": bool(preparation.get("ready") is False
                or config.get("context_refresh_required")
                or (isinstance(drift, list) and drift) or (isinstance(missing, list) and missing)),
        }
    else:
        result["readiness"] = {
            "ready": None, "status": "unknown",
            "refresh_required": bool(config.get("context_refresh_required")),
            "problem": bool(config.get("context_refresh_required")),
        }
    if result["readiness"]["problem"]:
        result["readiness"]["next_read"] = "agent_config_get response_detail=full for preparation diagnostics; response_detail=context without sections for mandatory rules"
    result["not_included"] = ["mandatory instructions", "tool inventory", "skills and paths", "package provenance",
                               "diagnostic preparation report", "work briefing"]
    return result


def _preserved_keys(name, detail):
    preserved = set(ELIDE_PRESERVED_KEYS)
    # Task custom fields are authored data, but structured signoff previews use
    # a controlled `fields` list whose unchanged entries are intentionally
    # compacted by preview_change(). Keep those rules operation-specific.
    if name in {"tasks_get", "tasks_list", "tasks_overview"}:
        preserved.update({"fields", "custom_fields"})
    if name in SIGNOFF_COMPLETION_WRITES:
        preserved.update({"error", "apply_result"})
    if name == "agents_activity_list":
        preserved.update({"items", "item_keys"})
    if detail == "summary" and name in {"focus_get", "agents_activity_list"}:
        preserved.update({"items", "checkpoint_findings", "context", "reference", "scope"})
        preserved.update({"next_cursor", "next_offset", "has_more"})
        preserved.add("business_id")
    if detail == "summary" and name == "agent_config_get":
        preserved.update({"scope", "readiness"})
    return frozenset(preserved)


def present(name, value, detail="compact", *, tool_names=None, sections=None, arguments=None):
    if detail == "full" or name not in COMPACT_OPERATIONS or value.get("status") != "ok":
        return value
    if detail == "summary" and name == "focus_get":
        if arguments and arguments.get("conversation"):
            raise DomainError("validation_error", "response_detail=summary does not include conversations; omit conversation or use compact/full")
    original = value.get("data")
    if not isinstance(original, dict):
        return value
    def rows(key, fields, transform=None):
        if isinstance(data.get(key), list):
            data[key] = [{**select(item, fields), **(transform(item) if transform else {})}
                         if isinstance(item, dict) else item for item in data[key]]

    def message_view(item):
        return {"message": message_item(item["message"])} if isinstance(item.get("message"), dict) else {}

    def session_views(item):
        if not isinstance(item.get("sessions"), list):
            return {}
        return {"sessions": [select(row, ACTIVITY_SESSION_FIELDS) if isinstance(row, dict) else row
                             for row in item["sessions"]]}

    def note_views(item):
        if not isinstance(item.get("notes"), list):
            return {}
        return {"notes": [select(row, TASK_NOTE_FIELDS) if isinstance(row, dict) else row
                          for row in item["notes"]]}

    data = dict(original)
    if detail == "summary":
        if name == "focus_get":
            if isinstance(data.get("item"), dict):
                data["item"] = _focus_summary_item(data["item"])
            if isinstance(data.get("items"), list):
                data["items"] = [_focus_summary_item(item) for item in data["items"]]
            data.pop("browser", None)
            data.pop("desktop_control_url", None)
            data["not_included"] = ["expanded source representations", "supporting references",
                                     "browser configuration", "desktop-control URLs"]
        elif name == "agents_activity_list":
            data = _activity_summary(data)
        elif name == "agent_config_get" and isinstance(data.get("effective_config"), dict):
            data["effective_config"] = _config_summary(data["effective_config"])
            data.pop("work_briefing", None)
        return {**value, "data": elide(data, preserved_keys=_preserved_keys(name, detail)), "response_detail": detail}
    if name in SIGNOFF_COMPLETION_WRITES and isinstance(data.get("signoff"), dict):
        data["signoff"] = _signoff_completion(data["signoff"])
    elif name == "signoffs_list":
        rows("items", SIGNOFF_INDEX_FIELDS)
    elif name == "signoffs_get" and isinstance(data.get("signoff"), dict):
        # Decision bindings (payload_hash, revisions) and approval evidence
        # (input, platform, decision) stay whole; the preview keeps one
        # encoding per change and marks bounded values explicitly.
        signoff = dict(data["signoff"])
        preview = signoff.get("preview")
        if isinstance(preview, dict) and isinstance(preview.get("changes"), list):
            signoff["preview"] = {**preview, "changes": [preview_change(item) for item in preview["changes"]]}
        data["signoff"] = signoff
    elif name == "inbox_list":
        rows("items", INBOX_EVENT_FIELDS, message_view)
    elif name == "agents_messages_list" and isinstance(data.get("items"), list):
        data["items"] = [message_item(item) if isinstance(item, dict) else item for item in data["items"]]
    elif name == "agents_activity_list":
        rows("agents", ACTIVITY_AGENT_FIELDS, session_views)
    elif name in {"agents_message_recipients", "broadcast_recipients"}:
        rows("items", RECIPIENT_FIELDS)
    elif name == "tables_rows_list":
        rows("items", TABLE_ROW_FIELDS)
        rows("rows", TABLE_ROW_FIELDS)
    elif name == "stories_list":
        rows("items", STORY_FIELDS)
        rows("stories", STORY_FIELDS)
    elif name == "tasks_get" and isinstance(data.get("task"), dict):
        data["task"] = {**data["task"], **note_views(data["task"])}
    elif name == "tasks_notes_list":
        rows("notes", TASK_NOTE_FIELDS)
        rows("items", TASK_NOTE_FIELDS)
    elif name == "operations_describe" and isinstance(data.get("tools"), list):
        # A multi-tool probe answers availability/permission; the exact schema
        # comes from a single-name call or full detail.
        if len(data["tools"]) > 1:
            data["tools"] = [{key: item for key, item in tool.items() if key != "input_schema"}
                             if isinstance(tool, dict) else tool for tool in data["tools"]]
    elif name in IDEA_WRITES and isinstance(data.get("idea"), dict):
        data["idea"] = select(data["idea"], IDEA_FIELDS)
    elif name in TASK_WRITES and isinstance(data.get("task"), dict):
        task = data["task"]
        data["task"] = select(task, TASK_FIELDS)
        data["task"]["note_count"] = task.get("note_count", len(task.get("notes", [])))
    elif name == "tasks_list" and isinstance(data.get("tasks"), list):
        data["tasks"] = [{**select(task, TASK_FIELDS), "note_count": task.get("note_count", len(task.get("notes", [])))}
                         for task in data["tasks"]]
    elif name in STORY_WRITES and isinstance(data.get("story"), dict):
        story = data["story"]
        data["story"] = select(story, STORY_FIELDS)
        if isinstance(data.get("event"), dict):
            event = data["event"]
            data["event"] = select(event, EVENT_FIELDS)
            event_data = event.get("data_json", {})
            identifiers = select(event_data, ("link_id", "resource_id", "resource_type"))
            if isinstance(event_data.get("link"), dict):
                identifiers["link"] = select(event_data["link"], ("id", "revision", "resource_id", "resource_type"))
            if identifiers:
                data["event"]["data_json"] = identifiers
    elif name == "agents_message_send" and isinstance(data.get("message"), dict):
        data["message"] = select(data["message"], MESSAGE_RECEIPT_FIELDS)
    elif name == "signoffs_input_create" and isinstance(data.get("signoff"), dict):
        data["signoff"] = select(data["signoff"], SIGNOFF_FIELDS)
    elif name in FOCUS_CARD_WRITES and isinstance(data.get("item"), dict):
        data["item"] = select(data["item"], FOCUS_ITEM_FIELDS)
    elif name == "tables_rows_write" and isinstance(data.get("rows"), list):
        data["rows"] = [select(row, ("id", "table_id", "revision", "created_by", "updated_by")) for row in data["rows"]]
        if isinstance(data.get("table"), dict):
            data["table"] = select(data["table"], ("id", "revision", "title", "purpose", "archived"))
    elif name == "agent_config_get" and isinstance(data.get("effective_config"), dict):
        config = data["effective_config"]
        # effective_config_id always equals configuration_revision; one spelling.
        summary = select(config, ("schema_version", "agent_id", "profile_revision", "name", "display_name",
            "status", "runtime", "role", "scope", "assignment_brief", "workspace", "business_id",
            "business_revision", "configuration_revision", "context_refresh_required", "plugins"))
        tools = config.get("tools", [])
        summary["tool_counts"] = dict(Counter(tool["effective_mode"] for tool in tools))
        summary["tool_inventory_omitted"] = True
        if tool_names is not None:
            requested, matched = set(tool_names), set()
            selected = []
            for tool in tools:
                found = requested & {tool["canonical_operation"], *tool.get("exposed_aliases", [])}
                if found:
                    matched.update(found)
                    selected.append(select(tool, ("canonical_operation", "exposed_aliases", "effective_mode", "available", "denial_reason")))
            summary["tools"] = selected
            summary["unknown_tools"] = [item for item in tool_names if item not in matched]
        if detail == "context":
            # The rendered bundle contains authoritative source/revision/hash headers.
            # Do not repeat the separate entries or replace mandatory text with a summary.
            # An explicit sections selection keeps the named rules verbatim and
            # names every omitted section with its own hash.
            summary["mandatory_context"] = (sectioned_context(config["mandatory_context"], sections)
                if sections is not None
                else select(config["mandatory_context"], ("rendered", "bundle_hash", "character_count")))
        summary["skills"] = [select(skill, ("name", "description", "generated_path")) for skill in config.get("skills", [])]
        preparation = config.get("preparation", {})
        summary["preparation"] = select(preparation, ("ready", "status", "projection_drift", "missing_artifacts"))
        summary["preparation"]["runtimes"] = {runtime: select(report, ("ready", "projection_drift"))
            for runtime, report in preparation.get("runtimes", {}).items()}
        data["effective_config"] = summary
        data.pop("work_briefing", None)
    return {**value, "data": elide(data, preserved_keys=_preserved_keys(name, detail)), "response_detail": detail}
