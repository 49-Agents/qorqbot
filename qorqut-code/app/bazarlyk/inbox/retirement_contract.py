"""The fixed contract of the retirement backlog Task and the responsibility handoff notices (owner decision G025).

Runtime copy of the pure migration contract: no database access. The retirement rehearsal builds the rows from these,
and the host's retirement-backlog reader (``bazarlyk/inbox/retirement_backlog.py``) re-derives the same identities and
validates the stored record against the same shapes; a focused test pins the two copies together.

One migration-owned system thread entry on the one Global backlog Task carries the frozen lookup manifest in its immutable
``attribution_json`` under ``MANIFEST_KEY``: identities only (message, event, recipient, business and finding keys), never a
body, credential or alternate copy of a private message. Each moved Task gets one deterministic system notice of its own."""

from qorqut_agentic.behavior.seeds import runtime_text as runtime_seed

import json
import uuid

# The fixed namespace strings the deterministic identities derive from, with the installation identity.
NAMESPACE = "qorqut/global-agents-retirement/v1/backlog"
NOTICE_NAMESPACE = "qorqut/global-agents-retirement/v1/handoff"
MANIFEST_KEY = "global_agents_retirement_backlog"
SCHEMA_VERSION = 1
TITLE = "Review messages left for retired project agents"
# The only finding code this migration captures; any other open finding of the cohort needs a Manager decision.
FINDING_CODE = "work_review_missing"
# Bounds that keep the immutable manifest (and the Task thread that returns it) small. A larger set refuses.
MAX_ENTRIES = 2000
MAX_FINDINGS = 50
SUMMARY_LIMIT = 500
ENTRY_KEYS = ("message_id", "event_id", "recipient_agent_id", "business_id")
FINDING_KEYS = ("agent_id", "check_key")
# Engine-shaped change record of a Task thread's system entry for a change of details without a state change.
NOTICE_CHANGE = '{"details": true, "from": null, "to": null}'
NOTICE_BODY = ("Responsibility for this Task moved to the current Ordubashy when the project agent that held it was retired. "
               "This is a system handoff notice: it is not an instruction or an approval, and the Task was not started, "
               "completed or otherwise changed beyond who is responsible for it.")
SYSTEM_ATTRIBUTION = '{"kind":"system"}'


def canonical(identity):
    """Whether ``identity`` is a canonical lowercase UUID string."""
    try:
        return isinstance(identity, str) and str(uuid.UUID(identity)) == identity
    except ValueError:
        return False


def backlog_ids(installation_id):
    """The Task, its ``assigned_to`` edge and the system thread entry of the one backlog, from the installation identity."""
    base = uuid.uuid5(uuid.UUID(installation_id), NAMESPACE)
    return {name: str(uuid.uuid5(base, name)) for name in ("task", "assigned_to", "system-entry")}


def notice_entry_id(installation_id, business_id, task_id):
    """The one handoff notice entry of a moved Task; Task ids are scoped, so the business is part of the name."""
    base = uuid.uuid5(uuid.UUID(installation_id), NOTICE_NAMESPACE)
    return str(uuid.uuid5(base, f"{business_id}/{task_id}/assignment_changed"))


def manifest(installation_id, cohort, entries, findings):
    """The frozen lookup manifest, sorted deterministically. ``entries`` are dicts with ENTRY_KEYS, ``findings`` dicts with
    FINDING_KEYS."""
    return {"schema_version": SCHEMA_VERSION, "installation_id": installation_id, "cohort_agent_ids": sorted(cohort),
            "entries": sorted(({key: entry[key] for key in ENTRY_KEYS} for entry in entries),
                              key=lambda entry: (entry["event_id"], entry["message_id"])),
            "findings": sorted(({key: found[key] for key in FINDING_KEYS} for found in findings),
                               key=lambda found: (found["agent_id"], found["check_key"]))}


def attribution(value):
    """The stored ``attribution_json`` of the backlog entry: the engine's system marker plus the manifest."""
    return json.dumps({"kind": "system", MANIFEST_KEY: value}, sort_keys=True, separators=(",", ":"))


def body(value):
    """The note of the backlog entry for a manifest: counts and the exact read invocation, never message content."""
    messages, findings = len(value["entries"]), len(value["findings"])
    parts = ["Historical review of messages left for retired project agents."]
    if messages:
        parts.append(
            runtime_seed('retirement-inbox-history', VALUE0=f'{messages}', VALUE1=f'{('s were' if messages != 1 else ' was')}'))
        parts.append(runtime_seed('retirement-inbox-reading'))
    else:
        parts.append("No direct message was left unacknowledged for them.")
    if findings:
        parts.append(
            runtime_seed('retirement-review-history', VALUE0=f'{findings}', VALUE1=f'{('s' if findings != 1 else '')}', VALUE2=f'{('were' if findings != 1 else 'was')}'))
    return "\n\n".join(parts)
