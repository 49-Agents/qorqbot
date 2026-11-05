"""The one automation guard: queue-time admission and the central authority gate of a script's own calls.

Admission (``Guard.admit``) runs in the caller's transaction before a new ``automation.run`` job is returned: the check, the
one-unresolved-run rule and - when the unit is writable - the reservation (the *frozen admission*: the script revisions, input,
phases and limits this run will use) are one atomic unit on every path (manual tools, scheduled routines). A paused, archived or
missing automation is refused. The one-unresolved-run rule holds a successor back while the preceding run is not terminal *or* still
holds an external step whose effect is unresolved, however that run ended.

The gate (``Guard.gate``) decides what a run-bound call may do. It is read fresh on every call, at every approval validation and at
delayed application: a paused, archived or closed run, a restriction or a revoked grant takes effect on the next call. It can

* deny (forbidden operations, restrictions - an explicit empty ``allowed_operations`` allows nothing -, scope mismatch);
* narrow an allowed supported action to its full Signoff (the script asked for ``approval='required'``, or the operation is a
  supported email/WhatsApp action, which is never replaced by a grant);
* turn an owner-approved typed grant (persistent, or run-only on an existing waiting run) into the permission the agent does not
  hold: a baseline deny that is only an ungranted permission becomes allow (or the operation's exact-change Signoff); a Signoff
  requirement the agent's own role asks for becomes allow. Hard owner-only operations (never reach the gate) and mandatory domain
  Signoffs (action and platform proposals) are never lifted.

Resource-limited grants. The engine authorizes a run-bound call twice and tells the gate which stage it is judging through the
trusted ``TargetSet`` it alone constructs (a plain tuple is unresolved and never matches a resource-limited grant): at the preflight
the bound arguments of a supported single-record call must name exactly the granted record (that is the whole check for a read, which
therefore never returns another record), and for a mutation the complete prepared target set - every expected target and every changed
resource - must be exactly the granted record before any effect or approval is applied, again at delayed application, and for a replay.
A match on one resource never authorizes an additional one; unsupported shapes are refused when the grant is approved.
"""

from __future__ import annotations

import json

from qorqut_operations.contracts import DomainError, PolicyDecision, TargetSet, canonical_hash, new_id, plain, utc_now
from qorqut_operations.storage.repositories import decode_row

from . import steps as ledger
from .records import PROGRAM, TERMINAL_JOB_STATES, bounded, encoded, MAX_ARGUMENT_BYTES

# Supported action Signoffs: the full prepared action is approved by the owner; no standing or run-only grant, generic
# question or script call can replace that approval.
ACTION_SIGNOFF_OPERATIONS = frozenset({"fastmail_send", "fastmail_reply", "fastmail_forward", "whatsapp_send"})
# Operations a script can never call, whatever Ordubashy may do: it cannot approve or answer for the owner, cannot post to the
# private owner conversation, cannot change instructions, permissions or its own automation, and cannot start further runs.
FORBIDDEN_OPERATIONS = frozenset({
    "owner_conversation_send", "owner_session_create", "operations_batch",
    "signoffs_respond", "signoffs_reject", "signoffs_amend", "signoffs_retry_apply",
    "signoffs_input_answer_delegated", "signoffs_input_cancel", "signoffs_notification_dismiss",
    "signoffs_notification_snooze", "signoffs_notification_tick", "signoffs_attention_set",
    "agentic_change_propose", "agentic_change_reconcile", "agentic_access"})
FORBIDDEN_PREFIXES = ("automations_", "agentic_change_", "management_", "permissions_", "profile_")
# Read-only automation operations stay available to a script that reads an automation's published record.
ALLOWED_AUTOMATION_READS = frozenset({"automations_get", "automations_list", "automations_history"})
# Baseline denials that are merely an ungranted permission: an owner grant may supply it. Anything else (owner-only, role bars,
# plugin or prerequisite refusals, the automation's own refusals) stays a denial.
UPGRADABLE_DENIALS = frozenset({"fallback", "role_default", "agent_override", "core_default"})


def forbidden(operation: str) -> bool:
    if operation in ALLOWED_AUTOMATION_READS:
        return False
    return operation in FORBIDDEN_OPERATIONS or operation.startswith(FORBIDDEN_PREFIXES)


def grant_id(grant: dict) -> str:
    return canonical_hash({key: grant.get(key) for key in ("operation", "capability", "resource_type", "resource_id")})[:16]


def subject_argument(descriptor, resource_type: str):
    """The one required string argument that names the single ``resource_type`` record a call is about (``task_id`` for ``tasks``,
    ``person_id`` for ``people``), or ``None``. A resource-limited grant is supported only for such single-record calls; every other
    shape is refused when the grant is approved, so it can never be silently broader than it reads."""
    schema = descriptor.input_schema or {}
    properties = schema.get("properties") or {}
    names = [name for name in schema.get("required") or ()
             if name.endswith("_id") and isinstance(properties.get(name), dict) and properties[name].get("type") == "string"]
    if len(names) != 1:
        return None
    stem = names[0][:-3]
    plural = {stem, stem + "s", stem + "es", stem[:-1] + "ies" if stem.endswith("y") else stem}
    return names[0] if resource_type in plural else None


def call_subject(descriptor, resource_type: str, arguments):
    """``(resource_type, resource_id)`` of the one record a supported single-record call names, or ``None``."""
    name = subject_argument(descriptor, resource_type)
    value = (arguments or {}).get(name) if name else None
    return (resource_type, value) if isinstance(value, str) and value else None


def matching_grant(grants, descriptor, targets):
    """The typed grant that applies to this operation at this stage of the call, or ``None``.

    An operation-wide grant (it names no resource) always applies. A resource-limited grant applies only to a trusted ``TargetSet``:
    at the ``call`` stage the bound arguments must name exactly the granted record (the whole of a supported read, and the provisional
    pass of a mutation); at the ``prepared`` stage the complete prepared target set must be exactly the granted record, so a match on
    one resource never authorizes any additional one. An unresolved (plain) target tuple never matches a resource-limited grant."""
    bound = None
    for grant in grants or ():
        if grant.get("operation") != descriptor.name:
            continue
        if "resource_id" not in grant:
            return grant
        if bound is not None or not isinstance(targets, TargetSet):
            continue
        granted = (grant["resource_type"], grant["resource_id"])
        if targets.stage == "call":
            exact = call_subject(descriptor, grant["resource_type"], targets.arguments) == granted
        else:
            resources = {(getattr(getattr(t, "resource", t), "resource_type", None), getattr(getattr(t, "resource", t), "resource_id", None))
                         for t in targets}
            exact = resources == {granted}
        if exact:
            bound = grant
    return bound


def job_row(uow, run_id):
    return decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (run_id,)).fetchone())


def automation_row(uow, automation_id):
    return decode_row(uow.connection.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone())


class Guard:
    def __init__(self, runtime):
        self.runtime = runtime

    # -- queue-time admission (``Program.admit``) -----------------------------------------------------------------
    def admit(self, context, arguments, uow):
        """Permit one new logical run, or raise (before writing anything). When the unit is writable the frozen admission is
        reserved on the automation row in the same transaction as the job."""
        automation = automation_row(uow, arguments["automation_id"])
        if automation is None or automation["business_id"] != context.business_id:
            raise DomainError("not_found", "Automation does not exist")
        if automation["state"] == "archived":
            raise DomainError("automation_archived", "Automation is archived", details={"automation_id": automation["id"]})
        if automation["state"] == "paused":
            raise DomainError("automation_paused", "Automation is paused" + (": " + automation["pause_reason"]
                              if automation["pause_reason"] else ""), details={"automation_id": automation["id"]})
        pending = self.unresolved_run(uow, automation["id"])
        if pending is not None:
            raise DomainError("automation_run_pending", "This automation already has an unresolved run",
                              details={"job_id": pending["id"], "reason": pending["state"], "automation_id": automation["id"]})
        from .events import ordubashy
        if ordubashy(uow) is None:
            raise DomainError("management_unavailable", "No active Ordubashy is configured to own automation runs")
        frozen = self.freeze(automation, arguments)
        if getattr(uow, "writable", False):
            uow.connection.execute("UPDATE automations SET admitted_json=? WHERE id=?", (encoded(frozen), automation["id"]))

    @staticmethod
    def unresolved_run(uow, automation_id):
        """The one run of this automation that is not settled (``None`` when there is none): a run that has not reached a terminal
        state, or - however it ended (cancelled, failed, timed out) - a run that still holds an external step whose effect is unresolved.
        The admission of a successor, a script-reference change and the manual start all ask this one question, so no path frees an
        automation while an effect of its preceding run survives."""
        found = uow.connection.execute(
            "SELECT id, state FROM jobs WHERE kind=? AND json_extract(arguments_json,'$.automation_id')=? "
            f"AND state NOT IN ({','.join('?' * len(TERMINAL_JOB_STATES))}) ORDER BY created_at LIMIT 1",
            (PROGRAM, automation_id, *TERMINAL_JOB_STATES)).fetchone()
        return found if found is not None else ledger.unresolved_of_automation(uow, automation_id)

    @staticmethod
    def unstaged_runs(uow, automation_id=None, limit=5):
        """Open runs (of one automation, or any) that were admitted but have not yet frozen their own script snapshot, so their exact
        admitted version still depends on the current catalog or on a retained copy. A run that has staged is immune to edits."""
        scope = "AND json_extract(arguments_json,'$.automation_id')=? " if automation_id is not None else ""
        return uow.connection.execute(
            "SELECT id, json_extract(arguments_json,'$.automation_id') AS automation_id FROM jobs WHERE kind=? "
            f"AND state NOT IN ({','.join('?' * len(TERMINAL_JOB_STATES))},'cancel_requested') "
            f"AND json_extract(result_json,'$.automation.snapshot') IS NULL {scope}ORDER BY created_at LIMIT ?",
            (PROGRAM, *TERMINAL_JOB_STATES, *((automation_id,) if automation_id is not None else ()), limit)).fetchall()

    def freeze(self, automation, arguments):
        """The immutable description of the run being admitted: the exact script revisions (read, never staged here), the
        input, phases and limits. Authority is not frozen - restrictions and grants stay current."""
        layer = getattr(self.runtime, "agentic_layer", None)
        if layer is None:
            raise DomainError("operation_unavailable", "The Agentic layer is not installed on this host")
        from qorqut_agentic.errors import AgenticError
        from qorqut_agentic.schemas import validate_instance
        try:
            described = layer.tools.describe(automation["script_ref"])
        except AgenticError as exc:
            raise DomainError("script_unavailable", f"The automation script is unavailable: {exc}") from exc
        if described["runtime"] != "python":
            raise DomainError("script_unavailable", "Automation scripts run on the python runtime alias only")
        data = bounded(plain(automation["arguments_json"]), MAX_ARGUMENT_BYTES, "Automation input")
        problems = validate_instance(data, described["input_schema"])
        if problems:
            raise DomainError("validation_error", "The automation input does not match the script's input schema",
                              details={"issues": problems[:5]})
        restrictions = plain(automation["restrictions_json"]) or {}
        return {"admission_id": new_id(), "admitted_at": utc_now(), "automation_revision": automation["revision"],
                "trigger": arguments.get("trigger", "manual"), "business_id": automation["business_id"],
                "script_ref": automation["script_ref"], "runtime": described["runtime"],
                "definition_revision": described["definition_revision"], "code_revision": described["code_revision"],
                "tool_limits": plain(described["limits"]), "input": data, "input_digest": canonical_hash(data),
                "phases": plain(automation["phases_json"]),
                "limits": {key: restrictions[key] for key in ("max_calls", "max_run_seconds") if key in restrictions}}

    # -- the run-bound policy gate ---------------------------------------------------------------------------------
    def bound_run(self, run_id, uow):
        """``(job, automation)`` of an open run, or raise ``policy_denied``."""
        job = job_row(uow, run_id)
        if job is None or job["kind"] != PROGRAM or job["state"] in TERMINAL_JOB_STATES or job["state"] == "cancel_requested":
            raise DomainError("policy_denied", "The automation run is not open")
        automation = automation_row(uow, job["arguments_json"]["automation_id"])
        if automation is None or automation["state"] != "active":
            raise DomainError("policy_denied", "The automation is not active")
        return job, automation

    def grants(self, automation, run_id, uow):
        return [*automation["grants_json"], *ledger.run_grants(uow, run_id)]

    @staticmethod
    def own_capabilities(uow, principal_id) -> set:
        row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (principal_id,)).fetchone()
        try:
            return set(json.loads(row["profile_json"])["scope"].get("global_capabilities", ()))
        except (TypeError, KeyError, ValueError):
            return set()

    def capabilities(self, context, uow):
        """The Global capabilities the owner's grants supply to exactly this run (the engine merges them at context refresh)."""
        try:
            job, automation = self.bound_run(context.automation_run_id, uow)
        except DomainError:
            return frozenset()
        return frozenset(grant["capability"] for grant in self.grants(automation, job["id"], uow) if grant.get("capability"))

    def gate(self, context, descriptor, decision, targets, uow):
        run_id = context.automation_run_id
        try:
            job, automation = self.bound_run(run_id, uow)
        except DomainError as exc:
            return PolicyDecision("deny", exc.code)
        name = descriptor.name
        if forbidden(name):
            return PolicyDecision("deny", "automation_forbidden_operation")
        if context.business_id != automation["business_id"]:
            return PolicyDecision("deny", "automation_scope")
        restrictions = automation["restrictions_json"] or {}
        allowed = restrictions.get("allowed_operations")
        if (allowed is not None and name not in allowed) or name in restrictions.get("denied_operations", ()):
            return PolicyDecision("deny", "automation_restricted")
        grant = matching_grant(self.grants(automation, run_id, uow), descriptor, targets)
        needed = descriptor.global_capability
        if (needed and context.business_id is None and needed in context.global_capabilities
                and needed not in self.own_capabilities(uow, context.principal_id)
                and not (grant is not None and grant.get("capability") == needed)):
            return PolicyDecision("deny", "automation_capability_not_granted")
        mode, provenance = decision.mode, decision.provenance
        binding = {**dict(decision.binding), "automation_id": automation["id"]}
        domain_signoff = name in ACTION_SIGNOFF_OPERATIONS or descriptor.signoff_category == "platform_action"
        if mode == "deny":
            if grant is None or provenance not in UPGRADABLE_DENIALS or name in ACTION_SIGNOFF_OPERATIONS:
                return decision
            mode, provenance = ("signoff" if domain_signoff and descriptor.supports_signoff else "allow"), "automation_grant"
            binding["grant"] = grant_id(grant)
        elif mode == "signoff" and grant is not None and not domain_signoff:
            mode, provenance = "allow", "automation_grant"
            binding["grant"] = grant_id(grant)
        if name in ACTION_SIGNOFF_OPERATIONS and mode != "deny":
            # The exact action is always approved by the owner, even where Ordubashy's own mode would allow it.
            if not descriptor.supports_signoff:
                return PolicyDecision("deny", "automation_action_signoff_unsupported")
            return PolicyDecision("signoff", "automation_action_signoff", binding)
        if mode == "allow" and ledger.narrowed(uow, run_id, name):
            # The script asked for this call's full Signoff (approval='required'); only a supported action can honor that.
            if not descriptor.supports_signoff:
                return PolicyDecision("deny", "automation_approval_unsupported")
            return PolicyDecision("signoff", "automation_approval_required", binding)
        return PolicyDecision(mode, provenance, binding)
