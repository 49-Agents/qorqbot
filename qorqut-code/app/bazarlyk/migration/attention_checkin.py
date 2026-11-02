"""Exact identification and retirement of the legacy built-in Focus check-in broadcast.

The retired auto-ping UI created one saved Global inbox broadcast from the agent pane. Its identity is frozen from the
baseline dashboard source (``biz-dashboard`` commit ``7e34af9434f6b237419801762d8575948321cb8d``,
``js/agentpane.js``): ``AGPAD_PING_NAME``, ``AGPAD_PING_MESSAGE`` and ``AGPAD_PING_TIMING``. The saved text tells
Ordubashy to keep the withdrawn Focus board current, so the broadcast must not keep recurring after the Attention board
replaces it.

Only that exact built-in record may be retired here: a Global inbox broadcast named exactly ``Focus check-in``, addressed
to exactly the configured Ordubashy, whose whitespace-normalized message equals the frozen built-in wording. Nothing is
selected by name or interval alone. The interval is reported, never compared: the preparation record could not read the
live saved interval, and the plan says not to assume a remembered four hours identifies every saved broadcast. A saved
record whose interval was edited through the ordinary broadcast editor is still the same built-in wording and recipient
set, and is eligible.

Everything else is left untouched:

* an active exact match is eligible to be paused once;
* an exact match that is already paused or cancelled stays in that state;
* a record with the same name but a different delivery, wording, recipient set or (defensively) unknown status is an
  ambiguous report item, never paused automatically;
* every other broadcast is unrelated.

The default call is a report-only dry run. Writes happen only when the caller passes ``apply=True``; each pause is the
existing revision-bound, idempotency-keyed ``broadcasts_pause`` operation through the composed application, so a changed
record refuses the pause and a rerun re-identifies it instead of forcing an obsolete state. After writing, the helper
re-reads one bounded scan and verifies the retired records kept their exact wording and became inactive.

One successful ``broadcasts_list`` call authorizes the owner's Global read; the inventory itself cannot use that
operation's cursors because it lists at most the newest 200 rows before paging. The scan therefore reads every Global
broadcast through the canonical repository projection with the same ``created_at``/``id`` descending keyset, in bounded
pages, so an older exact check-in beyond the first page is still found or the scan fails as incomplete.

Result dictionaries carry broadcast ids, classifications, statuses, revisions, recipient ids, stored timing and counts.
They never carry the stored message body or any other free text.

Nothing runs at import time: there is no CLI, no default live path, no network or service call and no startup or
migration hook. A later release/integration step invokes :func:`reconcile` deliberately with an already composed runtime.
"""
from __future__ import annotations

from collections.abc import Mapping

from qorqut_operations.contracts import DomainError, InvocationContext, canonical_hash, plain

from bazarlyk.management.identity import configured_ordubashy
from bazarlyk.transport.auth import Authentication, human_global_capabilities

# Frozen from baseline ``biz-dashboard/js/agentpane.js`` (``AGPAD_PING_NAME``); the stored name is compared exactly.
LEGACY_BROADCAST_NAME = "Focus check-in"
# Frozen from the same source (``AGPAD_PING_MESSAGE``): the two constants there concatenate to exactly this text.
LEGACY_BROADCAST_MESSAGE = (
    "Scheduled check-in from the owner. Review the items on the Focus board (focus_get) and the open tasks "
    "assigned to me. Take on and complete every item you can. Keep each task's status current: doing for what you "
    "are working on, waiting for what you are waiting on others for, and blocked for what you are waiting on me for. "
    "If you are blocked on an item, reply in that item's thread and state clearly why you are blocked and what would "
    "unblock you. Assign me tasks that I can do to unblock you and put them on my Focus (focus_present). Clear done "
    "tasks from the Focus pane (focus_set_archived)."
)
LEGACY_BROADCAST_DELIVERY = "inbox"

ELIGIBLE = "eligible"
PAUSED = "paused"
CANCELLED = "cancelled"
AMBIGUOUS = "ambiguous"
UNRELATED = "unrelated"
CLASSIFICATIONS = (ELIGIBLE, PAUSED, CANCELLED, AMBIGUOUS, UNRELATED)
CLASSIFICATION_ORDER = {name: position for position, name in enumerate(CLASSIFICATIONS)}

# ``broadcasts_list`` accepts at most 200 rows. The scan is bounded so a pathological listing fails loudly instead of
# reporting a silently partial identification.
LIST_LIMIT = 200
MAX_PAGES = 50
READ_GLOBAL = "read_global_context"


def normalize_message(value):
    """The comparison form of a stored message: its words and punctuation with whitespace runs collapsed."""
    return " ".join(str(value or "").split())


def classify(broadcast, *, ordubashy_id):
    """Classify one listed Global broadcast, returning ids and stored metadata but never the message body."""
    row = plain(dict(broadcast))
    classification, reasons = _classification(row, ordubashy_id)
    return {
        "broadcast_id": row.get("id"),
        "name": row.get("name"),
        "delivery": row.get("delivery"),
        "status": row.get("status"),
        "revision": row.get("revision"),
        "recipient_ids": sorted({identity for identity, _ in _recipients(row.get("recipients_json")) if identity}),
        "timing": row.get("timing_json"),
        "classification": classification,
        "reasons": reasons,
    }


def report(broadcasts, *, ordubashy_id):
    """The bounded classification report for the Global broadcasts of one scan; it never writes."""
    items = [classify(broadcast, ordubashy_id=ordubashy_id) for broadcast in broadcasts]
    items.sort(key=lambda item: (CLASSIFICATION_ORDER[item["classification"]], str(item["broadcast_id"])))
    return {
        "coordinator_id": ordubashy_id,
        "scanned": len(items),
        "counts": {name: sum(1 for item in items if item["classification"] == name) for name in CLASSIFICATIONS},
        "items": items,
        "eligible": [item["broadcast_id"] for item in items if item["classification"] == ELIGIBLE],
    }


def reconcile(runtime, *, apply=False):
    """Report the legacy Focus check-in candidates, and pause the active exact matches only when ``apply`` is true."""
    runtime.require_authority()
    owner, ordubashy_id, capabilities = _installation_identity(runtime)
    writing = apply is True  # An explicit boolean; any other value stays the safe report-only default.
    if writing and ordubashy_id is None:
        raise DomainError("management_unavailable",
                          "No configured Ordubashy is available to identify the legacy Focus check-in")
    application = runtime.application
    context = InvocationContext(owner, global_capabilities=capabilities, source="dashboard")
    outcome = report(_global_broadcasts(runtime, application, context), ordubashy_id=ordubashy_id)
    if not writing:
        return {**outcome, "apply": False, "applied": [], "verified": []}
    applied = _pause(application, context, [item for item in outcome["items"]
                                            if item["classification"] == ELIGIBLE])
    verified = _verify(runtime, application, context, ordubashy_id, applied) if applied else []
    return {**outcome, "apply": True, "applied": applied, "verified": verified}


def _classification(row, ordubashy_id):
    """The classification of one stored row and the reasons a same-name record could not be matched exactly."""
    if row.get("name") != LEGACY_BROADCAST_NAME:
        return UNRELATED, ["name"]
    if row.get("business_id") is not None:
        return UNRELATED, ["scope"]
    if ordubashy_id is None:
        return AMBIGUOUS, ["no_configured_ordubashy"]
    reasons = []
    if row.get("delivery") != LEGACY_BROADCAST_DELIVERY:
        reasons.append("delivery")
    if normalize_message(row.get("message")) != normalize_message(LEGACY_BROADCAST_MESSAGE):
        reasons.append("wording")
    if not _addressed_only_to(row.get("recipients_json"), ordubashy_id):
        reasons.append("recipients")
    if reasons:
        return AMBIGUOUS, sorted(reasons)
    status = row.get("status")
    if status == "active":
        return ELIGIBLE, []
    if status == "paused":
        return PAUSED, []
    if status == "cancelled":
        return CANCELLED, []
    return AMBIGUOUS, ["status"]


def _recipients(value):
    """The stored recipient entries as ``(agent_id, business_id)`` pairs; a bare identity string is a Global recipient."""
    entries = value if isinstance(value, (list, tuple)) else ()
    pairs = []
    for entry in entries:
        if isinstance(entry, Mapping):
            pairs.append((entry.get("agent_id"), entry.get("business_id")))
        elif isinstance(entry, str):
            pairs.append((entry, None))
    return pairs


def _addressed_only_to(value, ordubashy_id):
    """True for the built-in recipient set: one Global recipient, exactly the configured Ordubashy."""
    return _recipients(value) == [(ordubashy_id, None)]


def _installation_identity(runtime):
    """The installation owner, the configured Ordubashy and the owner's Global capabilities."""
    owner, _, _ = Authentication(runtime.database, runtime.root).installation_owner()
    with runtime.database.read() as uow:
        return owner, configured_ordubashy(uow, require_codex=False), human_global_capabilities(owner.id, uow)


def _global_broadcasts(runtime, application, context):
    """Every Global broadcast record, under the owner's verified read authority, in bounded keyset pages.

    The existing operation lists at most its own page of the newest rows before it starts cursoring, so following only its
    ``next_cursor`` could stop inside the newest 200 and miss an older record. One successful ``broadcasts_list`` call
    verifies the owner's current Global read authority; the complete inventory then uses that same ``created_at``/``id``
    descending keyset and reads each selected id through the canonical repository projection. The projection keeps the
    stored recipients and timing decoded exactly as the operation does; nothing parses raw JSON here.
    """
    probe = application.invoke(context, "broadcasts_list", {"scope": "global", "limit": LIST_LIMIT})
    if probe.status != "ok":
        raise DomainError("migration_incomplete", "The Global broadcast read was refused",
                          details={"result": probe.to_dict()})
    rows, after, pages = [], None, 0
    with runtime.database.read() as uow:
        repository = uow.repository("broadcasts", business_id=None)
        while True:
            if pages >= MAX_PAGES:
                raise DomainError("migration_incomplete",
                                  "Global broadcast reading did not complete within its bounded page count",
                                  details={"scanned": len(rows), "pages": pages})
            identities = _older_global_ids(uow, after)
            rows.extend(dict(repository.get(identity, required=True)) for identity in identities)
            pages += 1
            if len(identities) < LIST_LIMIT:
                return rows
            after = (rows[-1]["created_at"], rows[-1]["id"])


def _older_global_ids(uow, after):
    """The next page of Global broadcast ids, newest ``(created_at, id)`` first and strictly older than ``after``."""
    clause, parameters = "business_id IS NULL", []
    if after is not None:
        clause += " AND (created_at,id) < (?,?)"
        parameters.extend(after)
    rows = uow.connection.execute(
        "SELECT id FROM broadcasts WHERE " + clause + " ORDER BY created_at DESC,id DESC LIMIT ?",
        [*parameters, LIST_LIMIT]).fetchall()
    return [row["id"] for row in rows]


def _pause(application, context, items):
    """Pause each eligible record once through the existing revision-bound broadcast operation."""
    applied = []
    for item in items:
        identity, revision = item["broadcast_id"], item["revision"]
        outcome = application.invoke(context, "broadcasts_pause",
                                     {"scope": "global", "broadcast_id": identity},
                                     {"expected_revision": revision,
                                      "idempotency_key": canonical_hash(["attention-checkin-v1", identity, revision])})
        if outcome.status != "ok":
            raise DomainError("migration_incomplete",
                              "The legacy Focus check-in changed or could not be paused; rerun after resolving it",
                              details={"broadcast_id": identity, "revision": revision, "applied": list(applied),
                                       "result": outcome.to_dict()})
        applied.append(identity)
    return applied


def _verify(runtime, application, context, ordubashy_id, applied):
    """Re-read one scan and confirm every paused record is still the exact built-in wording, now inactive."""
    current = {item["broadcast_id"]: item
               for item in report(_global_broadcasts(runtime, application, context), ordubashy_id=ordubashy_id)["items"]}
    verified = []
    for identity in applied:
        item = current.get(identity)
        if item is None or item["classification"] not in (PAUSED, CANCELLED):
            raise DomainError("migration_verification_failed",
                              "A paused legacy Focus check-in did not keep its exact identity or inactive state",
                              details={"broadcast_id": identity, "observed": item})
        verified.append(identity)
    return verified