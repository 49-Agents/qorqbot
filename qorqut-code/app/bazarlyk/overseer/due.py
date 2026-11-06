"""Durable scheduled extraction occurrences on the existing routine scheduler and inbox.

The bounded, model-free Global program ``overseer.extraction_due`` only records that its own scheduled job ran: it reads
no experience, calls no model and starts no workflow. Its declared worker-only output derives one immutable occurrence
from the actual internal job. An existing inbox reconcile hook later groups unassigned occurrences into at most one
pending request per coalescing key and emits that request's ordinary Operations inbox event in the same transaction.
Nothing here starts, resumes or redirects a native conversation, and nothing marks source coverage.
"""

from __future__ import annotations

import json

from qorqut_operations.application.plans import PlanBuilder, apply_records, schema
from qorqut_operations.background.programs import ProgramResult
from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash, new_id, plain, utc_now
from qorqut_operations.storage.repositories import decode_row

from bazarlyk.agents.global_scope import active_role_holder
from bazarlyk.agents.roles import LEARNING_APPLIER_ROLE
from bazarlyk.native_chat import store

from .authority import current_overseer

PROGRAM = "overseer.extraction_due"
OUTPUT = "overseer_due_record"
TABLES = ("overseer_due_occurrences", "overseer_due_requests", "overseer_due_request_occurrences")
REFERENCE_KIND = "extraction_due"
EVENT_SOURCE = "overseer_extraction_due"
EVENT_TYPE = "overseer_extraction_due"
MAX_RECONCILE = 100
MAX_OCCURRENCE_REFS = 100
CAPABILITY = "manage_agentic_layer"

# The exact program arguments: the registered Overseer UUID, its already saved continuing conversation, and an optional
# A002 evidence selection. Installation chooses these values later; no time, timezone or conversation is hardcoded.
ARGUMENTS = schema({
    "agent_id": {"type": "string", "format": "uuid"},
    "conversation_id": {"type": "string", "minLength": 1, "maxLength": 200},
    "selection": {"type": ["object", "null"]},
}, ("agent_id", "conversation_id"))


def extraction_arguments(program_arguments):
    """The module's admitted extraction arguments: the optional A002 selection, never routine target coordinates."""
    selection = program_arguments.get("selection")
    return {"selection": plain(selection)} if selection is not None else {}


def canonical(value):
    """Canonical JSON text: sorted keys, compact separators, UTF-8, no NaN (the existing convention)."""
    return canonical_bytes(value).decode("utf-8")


def reference(row):
    """The exact immutable request reference: provider qorqut, kind extraction_due, request UUID and source hash."""
    return {"provider": "qorqut", "kind": REFERENCE_KIND, "id": row["id"], "revision": row["source_hash"]}


def validate_arguments(arguments):
    """Structural validation only; admission and routine authority are checked where they actually apply."""
    if not isinstance(arguments, dict) or set(arguments) - {"agent_id", "conversation_id", "selection"}:
        raise DomainError("validation_error", "Scheduled extraction accepts agent_id, conversation_id and optional selection")
    agent_id, conversation = arguments.get("agent_id"), arguments.get("conversation_id")
    try:
        import uuid
        if not isinstance(agent_id, str) or str(uuid.UUID(agent_id)) != agent_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise DomainError("validation_error", "Scheduled extraction requires the registered agent UUID") from None
    if not isinstance(conversation, str) or not 1 <= len(conversation) <= 200:
        raise DomainError("validation_error", "Scheduled extraction requires the saved conversation identity")
    selection = arguments.get("selection")
    if selection is not None:
        if not isinstance(selection, dict) or selection.get("mode") not in {"all_authorized", "explicit"}:
            raise DomainError("validation_error", "Select all_authorized or explicit evidence namespaces")
        if set(selection) - {"mode", "namespaces", "agent_ids", "task_ids", "work_types", "workflow_refs"}:
            raise DomainError("validation_error", "Unknown evidence selection field")
    return {"agent_id": agent_id, "conversation_id": conversation,
            **({"selection": plain(selection)} if selection is not None else {})}


class DueProgram:
    """The registered routine program: validates its own scheduled job and returns one derived due record output."""

    def __init__(self, runtime):
        self.runtime = runtime

    def run(self, control, arguments):
        arguments = validate_arguments(arguments)
        with control.service.database.read() as uow:
            values = occurrence_values(self.runtime, control.job, arguments, uow)
        return ProgramResult(
            outputs=({"operation": OUTPUT, "arguments": arguments},),
            data={key: values[key] for key in ("routine_run_id", "job_id", "scheduled_slot", "missed_count")})


def _slot_evidence(uow, run, job_id):
    """The scheduler's own slot row for this job: its reported missed count and cap state, never an invention."""
    slot = uow.connection.execute(
        "SELECT * FROM routine_slots WHERE namespace=? AND routine_id=? AND target_namespace='global'",
        (run["namespace"], run["routine_id"])).fetchone()
    if slot is None:
        raise DomainError("invalid_job", "The scheduled occurrence has no durable slot evidence")
    state = json.loads(slot["state_json"])
    if state.get("job_id") != job_id:
        raise DomainError("invalid_job", "The durable slot does not belong to this scheduled job")
    missed = state.get("missed", 0)
    if type(missed) is not int or missed < 0:
        raise DomainError("invalid_job", "The durable slot holds no bounded missed-slot report")
    return {"slot": slot["slot"], "missed": missed, "capped": bool(state.get("missed_capped"))}


def occurrence_values(runtime, job, arguments, uow, *, job_id=None):
    """Derive the immutable occurrence from the actual job, its linked routine run and the registered program.

    Callers never supply routine identity, a slot, a missed count or a boolean authority claim.
    """
    arguments = validate_arguments(arguments)
    program = runtime.programs.require(PROGRAM, available=False)
    definition = job["definition_ref_json"] or {}
    if definition.get("digest") != program.definition["digest"]:
        raise DomainError("policy_denied", "The scheduled program definition changed before recording its occurrence")
    if job["business_id"] is not None or definition.get("scope") != "global":
        raise DomainError("forbidden", "Scheduled extraction is a Global/root program")
    if definition.get("trigger") != "schedule":
        raise DomainError("forbidden", "Only a scheduled occurrence can record a due extraction request")
    run_id, routine_id = definition.get("routine_run_id"), definition.get("routine_id")
    if not isinstance(run_id, str) or not isinstance(routine_id, str):
        raise DomainError("invalid_job", "A scheduled occurrence requires its linked routine run")
    authority = getattr(runtime, "authority", None)
    if authority is None or authority.worker_id(PROGRAM, uow) != job["principal_id"]:
        raise DomainError("policy_denied", "Scheduled extraction requires its registered system worker")
    authority.require_worker(job["principal_id"], None, uow)
    run = uow.repository("routine_runs", business_id=None).get(run_id, required=True)
    if (run["namespace"] != "global" or run["routine_id"] != routine_id or run["trigger"] != "schedule"
            or run["business_id"] is not None):
        raise DomainError("invalid_job", "The linked routine run is not this Global scheduled occurrence")
    routine = uow.repository("routines", business_id=None).get({"namespace": "global", "id": routine_id}, required=True)
    if routine["job_kind"] != PROGRAM or routine["target"] != "root" or routine["scope"] != "global":
        raise DomainError("forbidden", "The routine no longer registers this Global program for the root target")
    if routine["metadata_json"].get("retired"):
        raise DomainError("forbidden", "The routine is retired")
    if plain(routine["arguments_json"]) != arguments:
        raise DomainError("invalid_job", "The scheduled job arguments differ from the current routine definition")
    evidence = _slot_evidence(uow, run, job_id or job["id"])
    if evidence["slot"] is None or run["slot"] is None:
        raise DomainError("invalid_job", "A scheduled occurrence requires its scheduled slot")
    return {
        "routine_run_id": run_id,
        "job_id": job_id or job["id"],
        "program": PROGRAM,
        "program_digest": program.definition["digest"],
        "routine_namespace": "global",
        "routine_id": routine_id,
        "routine_revision": definition.get("routine_revision") or routine["revision"],
        "scheduled_slot": run["slot"],
        "missed_count": evidence["missed"],
        "missed_capped": 1 if evidence["capped"] else 0,
        "target_agent_id": arguments["agent_id"],
        "target_conversation_id": arguments["conversation_id"],
        "extraction_arguments_json": extraction_arguments(arguments),
        "created_at": utc_now(),
        "blocked_reason": None,
        "blocked_at": None,
    }


# ----- the worker-only due-record output ------------------------------------------------------------------------------

class DueRecord:
    """The declared worker-only output operation. Everything except the echoed program arguments is derived here."""

    def __init__(self, runtime):
        self.runtime = runtime

    def prepare(self, ctx, args, options, uow):
        if ctx.job_id is None or ctx.principal_kind != "system" or ctx.source != "worker":
            raise DomainError("forbidden", "A due record is derived only from its own internal scheduled job")
        job = uow.repository("jobs", business_id=ctx.business_id).get(ctx.job_id, required=True)
        values = occurrence_values(self.runtime, job, args, uow)
        builder = PlanBuilder(ctx, OUTPUT, args, "Record a scheduled Overseer extraction due request")
        existing = uow.connection.execute(
            "SELECT * FROM overseer_due_occurrences WHERE routine_run_id=? OR job_id=?",
            (values["routine_run_id"], values["job_id"])).fetchone()
        if existing is not None:
            # Replay of identical output returns the same occurrence; different content is a conflict.
            frozen = decode_row(existing)
            keys = [key for key in values if key not in {"created_at", "id"}]
            if any(frozen.get(key) != values[key] for key in keys):
                raise DomainError("operation_conflict", "The due occurrence was already recorded with different content")
            return builder.build()
        values["id"] = new_id()
        builder.record("overseer_due_occurrences", values["id"], None, values)
        return builder.build()

    def apply(self, ctx, plan, uow):
        changed = apply_records(ctx, plan, uow)
        row = changed[0] if changed else uow.connection.execute(
            "SELECT * FROM overseer_due_occurrences WHERE job_id=?", (ctx.job_id,)).fetchone()
        if row is None:
            raise DomainError("invalid_plan", "The due occurrence was not recorded")
        return {"schema_version": 1, "occurrence": {
            key: row[key] for key in ("id", "routine_run_id", "job_id", "program", "program_digest", "routine_namespace",
                                      "routine_id", "routine_revision", "scheduled_slot", "missed_count", "missed_capped",
                                      "target_agent_id", "target_conversation_id", "extraction_arguments_json", "created_at")}}


# ----- coalescing into pending requests -------------------------------------------------------------------------------

def _program(uow, runtime):
    program = runtime.programs.get(PROGRAM)
    if program is None or program.get("unavailable_reason"):
        return None, "program_unavailable"
    return program, None


def _routine_authority(runtime, uow, occurrence):
    """Whether this occurrence's recorded routine/program authority still matches, with one bounded reason code."""
    program, reason = _program(uow, runtime)
    if reason:
        return reason
    if occurrence["program_digest"] != program["digest"]:
        return "program_changed"
    job = uow.connection.execute("SELECT * FROM jobs WHERE id=?", (occurrence["job_id"],)).fetchone()
    if job is None or job["state"] != "succeeded":
        return "occurrence_job_incomplete"
    run = uow.connection.execute("SELECT * FROM routine_runs WHERE id=?", (occurrence["routine_run_id"],)).fetchone()
    if run is None or run["namespace"] != "global" or run["routine_id"] != occurrence["routine_id"]:
        return "routine_run_unavailable"
    routine = uow.connection.execute("SELECT * FROM routines WHERE namespace='global' AND id=?",
                                     (occurrence["routine_id"],)).fetchone()
    if routine is None or json.loads(routine["metadata_json"]).get("retired"):
        return "routine_unavailable"
    if routine["status"] != "active":
        return "routine_inactive"
    if (routine["job_kind"] != PROGRAM or routine["target"] != "root" or routine["scope"] != "global"):
        return "routine_changed"
    stored_routine = json.loads(routine["arguments_json"])
    if (extraction_arguments(stored_routine) != _arguments(occurrence)
            or stored_routine.get("agent_id") != occurrence["target_agent_id"]
            or stored_routine.get("conversation_id") != occurrence["target_conversation_id"]):
        return "routine_arguments_changed"
    return None


def _target_authority(runtime, uow, agent_id, conversation_id):
    """The current role holder, active capable profile and saved continuing conversation, or one bounded reason."""
    if active_role_holder(uow.connection, LEARNING_APPLIER_ROLE) != agent_id:
        return "target_role_changed"
    profile = store.active_profile(uow.connection, agent_id, runtime.paths)
    if profile is None:
        return "target_profile_inactive"
    stored = json.loads(profile["profile_json"])
    if CAPABILITY not in set((stored.get("scope") or {}).get("global_capabilities") or ()):
        return "target_capability_missing"
    session = store.latest_session(uow.connection, agent_id)
    if session is None or session["conversation_id"] != conversation_id:
        return "target_conversation_changed"
    return None


def _live_authority(runtime, uow, agent_id, conversation_id):
    """The current native target/owner/session authority, or one bounded reason code."""
    reason = _target_authority(runtime, uow, agent_id, conversation_id)
    if reason is not None:
        return reason
    owner = uow.connection.execute("SELECT desired_state FROM native_session_owners WHERE agent_id=?",
                                   (agent_id,)).fetchone()
    if owner is not None and owner["desired_state"] in ("paused", "stopped"):
        return "conversation_paused"
    session = store.live_session(uow.connection, agent_id)
    profile = store.active_profile(uow.connection, agent_id, runtime.paths)
    if runtime.native_chat is None or session is None or profile is None:
        return "conversation_offline"
    try:
        status = runtime.native_chat.session_view(profile, session)
    except DomainError:
        return "conversation_offline"
    if (not status.get("can_send") or status.get("conversation_id") != conversation_id
            or (status.get("binding") or {}).get("binding_id") != session["id"]):
        return "conversation_offline"
    return None


def _arguments(occurrence):
    value = occurrence["extraction_arguments_json"]
    return json.loads(value) if isinstance(value, str) else value


def coalescing_key(occurrence):
    arguments = _arguments(occurrence)
    return canonical_hash({"schema_version": 1, "routine_namespace": "global", "routine_id": occurrence["routine_id"],
                           "program_digest": occurrence["program_digest"], "target": {
                               "agent_id": occurrence["target_agent_id"],
                               "conversation_id": occurrence["target_conversation_id"]},
                           "extraction_arguments": arguments})


def _source(request_id, occurrence):
    """The immutable canonical JSON a request hands to the module: routine metadata, never invented owner words."""
    return {
        "schema_version": 1,
        "request_id": request_id,
        "occurrence": {
            "id": occurrence["id"], "job_id": occurrence["job_id"],
            "routine_run_id": occurrence["routine_run_id"], "program": occurrence["program"],
            "program_digest": occurrence["program_digest"],
            "routine": {"namespace": "global", "id": occurrence["routine_id"],
                        "revision": occurrence["routine_revision"]},
            "slot": occurrence["scheduled_slot"], "missed_count": occurrence["missed_count"],
            "missed_capped": bool(occurrence["missed_capped"]),
        },
        "target": {"agent_id": occurrence["target_agent_id"],
                   "conversation_id": occurrence["target_conversation_id"]},
        "extraction_arguments": _arguments(occurrence),
    }


def _emit(runtime, uow, row, now):
    """One ordinary Operations inbox event and its required delivery to the actual target UUID."""
    request_reference = reference(row)
    visibility = {"reference": request_reference,
                  "target": {"agent_id": row["target_agent_id"], "conversation_id": row["target_conversation_id"]}}
    event = uow.connection.execute(
        "INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,"
        "visibility_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (None, EVENT_SOURCE, row["id"], 1, None, EVENT_TYPE,
         runtime.resolver.require_agentic_view().runtime_instructions().render('overseer-extraction-due'), canonical(visibility), now)).lastrowid
    uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id,required) VALUES(?,?,1)",
                           (row["target_agent_id"], event))


def reconcile(runtime, uow, *, now=None):
    """Assign at most ``MAX_RECONCILE`` unassigned successful-job occurrences and emit each new request's inbox event.

    Runs inside the caller's host transaction (the existing nudge reconcile hook), so membership and its notification
    commit together. A blocked occurrence stays durable with its bounded reason and is retried on a later pass.
    """
    now = now or utc_now()
    occurrences = uow.connection.execute(
        "SELECT o.* FROM overseer_due_occurrences o JOIN jobs j ON j.id=o.job_id "
        "LEFT JOIN overseer_due_request_occurrences m ON m.occurrence_id=o.id "
        "WHERE m.occurrence_id IS NULL AND j.state='succeeded' ORDER BY o.created_at,o.id LIMIT ?",
        (MAX_RECONCILE,)).fetchall()
    summary = {"schema_version": 1, "assigned": 0, "blocked": 0, "requests": 0, "reasons": {}}
    for occurrence in occurrences:
        reason = _routine_authority(runtime, uow, occurrence) or _target_authority(
            runtime, uow, occurrence["target_agent_id"], occurrence["target_conversation_id"])
        if reason is not None:
            summary["blocked"] += 1
            summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
            if occurrence["blocked_reason"] != reason:
                uow.repository("overseer_due_occurrences").update(
                    occurrence["id"], {"blocked_reason": reason, "blocked_at": now})
            continue
        key = coalescing_key(occurrence)
        pending = uow.connection.execute(
            "SELECT * FROM overseer_due_requests WHERE coalescing_key=? AND state='pending'", (key,)).fetchone()
        if pending is None:
            request_id = new_id()
            source = _source(request_id, occurrence)
            source_text = canonical(source)
            uow.connection.execute(
                "INSERT INTO overseer_due_requests(id,coalescing_key,source_json,source_hash,target_agent_id,"
                "target_conversation_id,state,first_occurrence_id,admission_json,admission_hash,linked_run_id,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,NULL,NULL,NULL,?,?)",
                (request_id, key, source_text, canonical_hash(source), occurrence["target_agent_id"],
                 occurrence["target_conversation_id"], occurrence["id"], now, now))
            pending = uow.connection.execute("SELECT * FROM overseer_due_requests WHERE id=?",
                                             (request_id,)).fetchone()
            summary["requests"] += 1
            _emit(runtime, uow, pending, now)
        uow.connection.execute(
            "INSERT INTO overseer_due_request_occurrences(request_id,occurrence_id,created_at) VALUES(?,?,?)",
            (pending["id"], occurrence["id"], now))
        if occurrence["blocked_reason"] is not None:
            uow.repository("overseer_due_occurrences").update(
                occurrence["id"], {"blocked_reason": None, "blocked_at": None})
        summary["assigned"] += 1
    return summary


# ----- reads -----------------------------------------------------------------------------------------------------------

def _eligibility(runtime, uow, row, session):
    if row["state"] == "linked":
        return {"eligible": False, "reason": "linked"}
    if row["state"] == "reserved":
        return {"eligible": False, "reason": "admission_reserved"}
    reason = _routine_authority(runtime, uow, {"job_id": _first_job(uow, row),
                                               "routine_run_id": _first_run(uow, row),
                                               "routine_id": _first_routine(uow, row),
                                               "program_digest": json.loads(row["source_json"])["occurrence"]["program_digest"],
                                               "extraction_arguments_json": canonical(
                                                   json.loads(row["source_json"])["extraction_arguments"]),
                                               "target_agent_id": row["target_agent_id"],
                                               "target_conversation_id": row["target_conversation_id"]})
    if reason:
        return {"eligible": False, "reason": reason, "blocked_occurrences": _blocked(uow, row["id"])}
    owner = uow.connection.execute("SELECT desired_state FROM native_session_owners WHERE agent_id=?",
                                   (row["target_agent_id"],)).fetchone()
    if owner is not None and owner["desired_state"] in ("paused", "stopped"):
        return {"eligible": False, "reason": "conversation_paused", "blocked_occurrences": _blocked(uow, row["id"])}
    if session is None or session["conversation_id"] != row["target_conversation_id"]:
        return {"eligible": False, "reason": "target_conversation_changed",
                "blocked_occurrences": _blocked(uow, row["id"])}
    # The actual native binding/can_send is checked at admission; a read never starts or resumes a session.
    native = getattr(runtime, "native_chat", None)
    profile = store.active_profile(uow.connection, row["target_agent_id"], runtime.paths)
    if native is None or profile is None:
        return {"eligible": False, "reason": "conversation_offline"}
    try:
        status = native.session_view(profile, session)
    except DomainError:
        return {"eligible": False, "reason": "conversation_offline"}
    if not status.get("can_send"):
        return {"eligible": False, "reason": "conversation_offline"}
    return {"eligible": True, "reason": None}


def _first_job(uow, row):
    return json.loads(row["source_json"])["occurrence"]["job_id"]


def _first_run(uow, row):
    return json.loads(row["source_json"])["occurrence"]["routine_run_id"]


def _first_routine(uow, row):
    return json.loads(row["source_json"])["occurrence"]["routine"]["id"]


def _blocked(uow, request_id):
    return [{"id": item["id"], "reason": item["blocked_reason"]}
            for item in uow.connection.execute(
                "SELECT o.id,o.blocked_reason FROM overseer_due_occurrences o "
                "JOIN overseer_due_request_occurrences m ON m.occurrence_id=o.id "
                "WHERE m.request_id=? AND o.blocked_reason IS NOT NULL ORDER BY o.created_at,o.id LIMIT 20",
                (request_id,))]


def _occurrence_view(row):
    return {"id": row["id"], "job_id": row["job_id"], "routine_run_id": row["routine_run_id"],
            "routine": {"namespace": row["routine_namespace"], "id": row["routine_id"],
                        "revision": row["routine_revision"]},
            "slot": row["scheduled_slot"], "missed_count": row["missed_count"],
            "missed_capped": bool(row["missed_capped"]), "created_at": row["created_at"],
            "blocked_reason": row["blocked_reason"]}


def request_view(runtime, uow, row, session=None, *, occurrence_limit=20):
    """The bounded read: reference, target, state, arguments, first occurrence, occurrence references and eligibility."""
    session = session if session is not None else store.latest_session(uow.connection, row["target_agent_id"])
    occurrences = uow.connection.execute(
        "SELECT o.* FROM overseer_due_occurrences o JOIN overseer_due_request_occurrences m "
        "ON m.occurrence_id=o.id WHERE m.request_id=? ORDER BY o.created_at,o.id LIMIT ?",
        (row["id"], occurrence_limit + 1)).fetchall()
    total = uow.connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(o.missed_count),0), COALESCE(MAX(o.missed_capped),0) "
        "FROM overseer_due_occurrences o JOIN overseer_due_request_occurrences m ON m.occurrence_id=o.id "
        "WHERE m.request_id=?", (row["id"],)).fetchone()
    source = json.loads(row["source_json"])
    first = uow.connection.execute("SELECT * FROM overseer_due_occurrences WHERE id=?",
                                   (row["first_occurrence_id"],)).fetchone()
    return {
        "schema_version": 1,
        "reference": reference(row),
        "id": row["id"],
        "state": row["state"],
        "target": {"agent_id": row["target_agent_id"], "conversation_id": row["target_conversation_id"]},
        "extraction_arguments": source["extraction_arguments"],
        "first_occurrence": _occurrence_view(first) if first is not None else None,
        "occurrences": [_occurrence_view(item) for item in occurrences[:occurrence_limit]],
        "occurrence_count": total[0],
        "occurrences_truncated": len(occurrences) > occurrence_limit,
        "missed_slots": {"recorded": total[1], "capped": bool(total[2]),
                         "statement": "The routine scheduler reported this many missed slots; occurrences are coalesced "
                                      "into one pending request, not one per slot."},
        "linked_run_id": row["linked_run_id"],
        "eligibility": _eligibility(runtime, uow, row, session),
        "created_at": row["created_at"],
    }


def list_requests(runtime, ctx, uow, *, limit=20, cursor=None):
    """Current target context only: this agent's pending/reserved requests for its saved continuing conversation."""
    import base64
    session = store.latest_session(uow.connection, ctx.principal_id)
    if session is None:
        return {"schema_version": 1, "items": [], "next_cursor": None, "complete": True,
                "availability": "unavailable", "gaps": [{"code": "conversation_unavailable"}]}
    after = None
    binding = canonical_hash({"operation": "overseer_due_list", "agent_id": ctx.principal_id,
                              "conversation_id": session["conversation_id"]})
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            if decoded.get("binding") != binding or not isinstance(decoded.get("after"), list):
                raise ValueError
            after = tuple(decoded["after"])
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise DomainError("validation_error", "Invalid or differently scoped due-request cursor") from None
    clause = ["target_agent_id=?", "target_conversation_id=?", "state IN ('pending','reserved')"]
    parameters = [ctx.principal_id, session["conversation_id"]]
    if after is not None:
        clause.append("(created_at,id)<?")
        parameters.extend(after)
    found = uow.connection.execute(
        "SELECT * FROM overseer_due_requests WHERE " + " AND ".join(clause) +
        " ORDER BY created_at DESC,id DESC LIMIT ?", [*parameters, limit + 1]).fetchall()
    more = len(found) > limit
    items = [request_view(runtime, uow, row, session) for row in found[:limit]]
    next_cursor = None
    if more:
        last = found[limit - 1]
        next_cursor = base64.urlsafe_b64encode(canonical({"binding": binding, "after": [last["created_at"],
                                                                                       last["id"]]}).encode()).decode()
    return {"schema_version": 1, "items": items, "next_cursor": next_cursor, "complete": not more,
            "availability": "available", "gaps": []}


def get_request(runtime, ctx, uow, reference_value):
    """One request by its exact reference, under the current target context."""
    if (not isinstance(reference_value, dict) or set(reference_value) != {"provider", "kind", "id", "revision"}
            or reference_value.get("provider") != "qorqut" or reference_value.get("kind") != REFERENCE_KIND):
        raise DomainError("validation_error", "overseer_due_get requires an exact extraction_due reference")
    row = uow.connection.execute("SELECT * FROM overseer_due_requests WHERE id=?", (reference_value["id"],)).fetchone()
    if row is None:
        raise DomainError("not_found", "The due request does not exist")
    session = store.latest_session(uow.connection, ctx.principal_id)
    if (row["target_agent_id"] != ctx.principal_id or session is None
            or row["target_conversation_id"] != session["conversation_id"]):
        raise DomainError("forbidden", "The due request targets another agent or conversation")
    if reference(row) != reference_value:
        raise DomainError("stale_revision", "The due request reference changed")
    return request_view(runtime, uow, row, session, occurrence_limit=MAX_OCCURRENCE_REFS)

# ----- admission of a due request into the existing extraction workflow -----------------------------------------------

def bounded_interpretation(interpretation, arguments):
    """The exact bounded interpretation: extraction only, with the saved arguments and a 1-50 word rationale."""
    if (not isinstance(interpretation, dict) or set(interpretation) != {"kind", "arguments", "rationale"}
            or interpretation["kind"] != "extraction"):
        raise DomainError("validation_error", "A scheduled extraction admits an extraction interpretation only")
    if not isinstance(interpretation["arguments"], dict) or plain(interpretation["arguments"]) != plain(arguments):
        raise DomainError("owner_input_required", "The admitted arguments differ from the saved due request")
    rationale = interpretation["rationale"]
    if (not isinstance(rationale, str) or not rationale.strip() or len(rationale.split()) > 50):
        raise DomainError("validation_error", "A bounded 1-50 word rationale is required")
    return plain(interpretation)


def _membership(uow, request_id):
    return [row[0] for row in uow.connection.execute(
        "SELECT occurrence_id FROM overseer_due_request_occurrences WHERE request_id=? ORDER BY occurrence_id",
        (request_id,))]


def _request(uow, request_id):
    row = uow.connection.execute("SELECT * FROM overseer_due_requests WHERE id=?", (request_id,)).fetchone()
    if row is None:
        raise DomainError("owner_input_required", "The due extraction request does not exist")
    return row


def _assert_occurrence_authority(runtime, uow, row):
    """Every occurrence behind this request must still trace to its successful canonical scheduled job and routine."""
    membership = _membership(uow, row["id"])
    if not membership:
        raise DomainError("owner_input_required", "The due request has no recorded occurrence")
    for occurrence_id in membership:
        occurrence = uow.connection.execute("SELECT * FROM overseer_due_occurrences WHERE id=?",
                                            (occurrence_id,)).fetchone()
        if occurrence is None:
            raise DomainError("owner_input_required", "A recorded due occurrence is unavailable")
        reason = _routine_authority(runtime, uow, occurrence)
        if reason is not None:
            raise DomainError("owner_input_required", "The scheduled extraction authority changed: " + reason)
        if (occurrence["target_agent_id"] != row["target_agent_id"]
                or occurrence["target_conversation_id"] != row["target_conversation_id"]):
            raise DomainError("owner_input_required", "The due occurrence targets another agent or conversation")
    return membership


def _precheck_pending(runtime, context, ref, interpretation):
    """Refuse an initial admission before it is frozen, recording a bounded routine-authority block."""
    with runtime.database.read() as uow:
        ctx, _ = current_overseer(runtime, context, uow, capability=CAPABILITY)
        row = _request(uow, ref["id"])
        if row["target_agent_id"] != ctx.principal_id:
            raise DomainError("forbidden", "The due request targets another Overseer identity")
        if reference(row) != ref:
            raise DomainError("stale_revision", "The due request reference or source hash changed")
        initial = row["state"] == "pending"
        source = json.loads(row["source_json"])
        bounded_interpretation(interpretation, source["extraction_arguments"])
        blocked = []
        for occurrence_id in _membership(uow, row["id"]):
            occurrence = uow.connection.execute("SELECT * FROM overseer_due_occurrences WHERE id=?",
                                                (occurrence_id,)).fetchone()
            if occurrence is None:
                continue
            reason = _routine_authority(runtime, uow, occurrence)
            if reason is not None:
                blocked.append((occurrence_id, reason))
        live_reason = (_live_authority(runtime, uow, row["target_agent_id"], row["target_conversation_id"])
                       if initial else None)
    if blocked:
        with runtime.database.transaction(operation="overseer.due-blocked") as uow:
            runtime.application.ensure_writes_open(uow)
            for occurrence_id, reason in blocked:
                uow.repository("overseer_due_occurrences").update(
                    occurrence_id, {"blocked_reason": reason, "blocked_at": utc_now()})
        raise DomainError("owner_input_required", "The scheduled extraction authority changed: " + blocked[0][1])
    if live_reason is not None:
        raise DomainError("owner_input_required", "The scheduled extraction cannot start: " + live_reason)


def _freeze_admission(runtime, context, ref, interpretation):
    """Freeze the exact admission (and thereby the membership) in one host transaction before the module is called."""
    with runtime.database.transaction(operation="overseer.due-admission") as uow:
        runtime.application.ensure_writes_open(uow)
        ctx, _ = current_overseer(runtime, context, uow, capability=CAPABILITY)
        row = _request(uow, ref["id"])
        if row["target_agent_id"] != ctx.principal_id:
            raise DomainError("forbidden", "The due request targets another Overseer identity")
        if reference(row) != ref:
            raise DomainError("stale_revision", "The due request reference or source hash changed")
        source = json.loads(row["source_json"])
        arguments = bounded_interpretation(interpretation, source["extraction_arguments"])
        _assert_occurrence_authority(runtime, uow, row)
        membership = _membership(uow, row["id"])
        if row["state"] in ("reserved", "linked"):
            saved = json.loads(row["admission_json"])
            if (saved.get("interpretation") != arguments or saved.get("reference") != ref
                    or saved.get("occurrence_ids") != membership):
                raise DomainError("operation_conflict", "The due request already has a different admission")
            return row, arguments, saved, membership
        for occurrence_id in membership:
            occurrence = uow.connection.execute("SELECT blocked_reason FROM overseer_due_occurrences WHERE id=?",
                                                (occurrence_id,)).fetchone()
            if occurrence is not None and occurrence["blocked_reason"] is not None:
                uow.repository("overseer_due_occurrences").update(
                    occurrence_id, {"blocked_reason": None, "blocked_at": None})
        admission = {"schema_version": 1, "request_id": row["id"], "reference": ref,
                     "interpretation": arguments, "occurrence_ids": membership,
                     "target": {"agent_id": row["target_agent_id"],
                                "conversation_id": row["target_conversation_id"]},
                     "frozen_at": utc_now()}
        encoded = canonical(admission)
        uow.repository("overseer_due_requests").update(
            row["id"], {"state": "reserved", "admission_json": encoded,
                        "admission_hash": canonical_hash(admission)})
        return _request(uow, row["id"]), arguments, admission, membership


class DueInvocationGateway:
    """The trusted resolution of one due request; it validates everything again at the moment the module asks."""

    def __init__(self, runtime, context, request_id):
        self.runtime, self.context, self.request_id = runtime, context, request_id

    def resolve_invocation(self, kind, reference_value):
        if kind != "extraction" or (not isinstance(reference_value, dict)
                                    or reference_value.get("provider") != "qorqut"
                                    or reference_value.get("kind") != REFERENCE_KIND):
            raise DomainError("owner_input_required", "This invocation is not an admitted scheduled extraction")
        with self.runtime.database.read() as uow:
            ctx, _ = current_overseer(self.runtime, self.context, uow, capability=CAPABILITY)
            row = _request(uow, self.request_id)
            if row["target_agent_id"] != ctx.principal_id or reference(row) != reference_value:
                raise DomainError("owner_input_required", "The due request reference is unavailable or changed")
            if row["state"] not in {"reserved", "linked"} or row["admission_json"] is None:
                raise DomainError("owner_input_required", "The due request admission is not frozen")
            admission = json.loads(row["admission_json"])
            _assert_occurrence_authority(self.runtime, uow, row)
            live_reason = _live_authority(self.runtime, uow, row["target_agent_id"],
                                          row["target_conversation_id"])
        if live_reason is not None:
            raise DomainError("owner_input_required",
                              "The continuing Overseer conversation is unavailable: " + live_reason)
        return {"invocation_key": "extraction-due:" + row["id"], "reference": reference_value,
                "allowed_kinds": ["extraction"], "origin": "routine", "owner_principal_id": None,
                "conversation_id": row["target_conversation_id"],
                "executor": {"actor_id": ctx.principal_id, "actor_kind": "agent"},
                "arguments": admission["interpretation"]["arguments"],
                "source_text": canonical(json.loads(row["source_json"])), "source_hash": row["source_hash"]}


def admit_due_extraction(runtime, context, invocation, interpretation, operation_id):
    """Admit one due request as an extraction workflow, with the same immutable source on every retry."""
    ref = invocation.get("reference") if isinstance(invocation, dict) else None
    if (not isinstance(ref, dict) or set(ref) != {"provider", "kind", "id", "revision"}
            or ref.get("provider") != "qorqut" or ref.get("kind") != REFERENCE_KIND):
        raise DomainError("validation_error", "A canonical extraction_due reference is required")
    try:
        import uuid
        if str(uuid.UUID(ref["id"])) != ref["id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DomainError("validation_error", "The due reference id must be a canonical UUID") from None
    _precheck_pending(runtime, context, ref, interpretation)
    row, arguments, admission, _ = _freeze_admission(runtime, context, ref, interpretation)
    if row["state"] == "linked":
        with runtime.database.read() as uow:
            current_overseer(runtime, context, uow, capability=CAPABILITY)
            reason = _live_authority(runtime, uow, row["target_agent_id"], row["target_conversation_id"])
        if reason is not None:
            raise DomainError("owner_input_required", "The continuing Overseer conversation is unavailable: " + reason)
        return {"state": "linked", "kind": "extraction", "run_id": row["linked_run_id"],
                "request_id": row["id"], "reference": reference(row), "recovered": True}
    gateway = DueInvocationGateway(runtime, context, row["id"])
    # A later authority refusal leaves any frozen/reserved admission and membership intact: an earlier attempt may
    # already have created the run. The same invocation key recovers it once authority is restored.
    result = runtime.agentic_layer.overseer.start_run(
        "extraction", {"reference": ref}, operation_id=operation_id, gateway=gateway, arguments=arguments["arguments"])
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise DomainError("invalid_result", "The extraction workflow did not link its run")
    with runtime.database.transaction(operation="overseer.due-link") as uow:
        runtime.application.ensure_writes_open(uow)
        ctx, _ = current_overseer(runtime, context, uow, capability=CAPABILITY)
        current = _request(uow, row["id"])
        if current["target_agent_id"] != ctx.principal_id or reference(current) != ref:
            raise DomainError("stale_revision", "The due request changed before its run was linked")
        if current["state"] == "linked":
            if current["linked_run_id"] != run_id:
                raise DomainError("operation_conflict", "The due request is linked to another run")
        else:
            if current["state"] != "reserved" or current["admission_hash"] != canonical_hash(admission):
                raise DomainError("operation_conflict", "The due request admission changed before linking")
            uow.repository("overseer_due_requests").update(
                current["id"], {"state": "linked", "linked_run_id": run_id})
    return {"state": "linked", "kind": "extraction", "run_id": run_id, "request_id": row["id"],
            "reference": reference(row), "invocation": result.get("invocation")}


# ----- inbox visibility (the host notification context, not an MCP registration) ---------------------------------------

def request_visible(app, ctx, request_id, uow):
    """Whether this stored due request may be shown to this exact inbox reader right now.

    The background nudge reader is not an MCP call: identity, role, capability, ordinary policy and the saved
    conversation are checked here, while the actual tool invocation still requires the real full registration.
    """
    row = uow.connection.execute("SELECT * FROM overseer_due_requests WHERE id=?", (request_id,)).fetchone()
    if row is None or ctx.principal_id != row["target_agent_id"] or ctx.business_id is not None:
        return False
    if active_role_holder(uow.connection, LEARNING_APPLIER_ROLE) != ctx.principal_id:
        return False
    profile = store.active_profile(uow.connection, ctx.principal_id)
    if profile is None:
        return False
    stored = json.loads(profile["profile_json"])
    if CAPABILITY not in set((stored.get("scope") or {}).get("global_capabilities") or ()):
        return False
    session = store.latest_session(uow.connection, ctx.principal_id)
    if session is None or session["conversation_id"] != row["target_conversation_id"]:
        return False
    descriptor = app.registry.get("overseer_due_get")
    if descriptor is None:
        return False
    try:
        return app.policy.resolve(ctx, descriptor, (), uow).mode == "allow"
    except DomainError:
        return False


def request_reference(app, ctx, request_id, uow):
    """The exact reference to expose on a visible inbox item, or None when it is not provably visible now."""
    if not request_visible(app, ctx, request_id, uow):
        return None
    return reference(uow.connection.execute("SELECT * FROM overseer_due_requests WHERE id=?",
                                            (request_id,)).fetchone())


# ----- program registration -------------------------------------------------------------------------------------------

def register_program(runtime, programs):
    """Register the one bounded scheduled program on the existing ProgramRegistry."""
    from qorqut_operations.background.programs import Program
    programs.register(Program(
        PROGRAM, 1, ARGUMENTS, DueProgram(runtime).run,
        permissions={OUTPUT: "allow"}, outputs=(OUTPUT,), resource_types=TABLES,
        scope_kinds=("global",), global_capabilities=(CAPABILITY,), timeout_seconds=120))
