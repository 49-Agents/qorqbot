"""The retained v1 domain permission rules, contributed to the Operations policy evaluator as data.

The evaluator itself is ``qorqut_operations.policy``; nothing here resolves a decision on its own. The deployed v1
resolver interleaved these per-domain rules with the generic precedence steps, so they are placed in the three
phases of ``PolicyRules`` exactly where v1 evaluated them:

* ``before_override``: rules that decide before an agent's explicit override is consulted, either because they
  refuse it (selected extension roles, the restricted user-input presentation, the CRM and Table-discussion operations that
  may never exceed the existing operation they build on, Global table archive, Project archive, terminal control, owner-action
  proposals, ``learning-applier`` only operations, the Learning Applier-only Context question tools that follow the caller's current
  ``context_edit`` decision);
* ``after_override``: standing core defaults that sit between the explicit override and the role default;
* ``after_default``: core defaults that apply only when neither an override nor a role default names the operation.

``resolve`` is the evaluator's re-entry (``tasks_notes_list`` reads as ``tasks_get``, ``actions_thread_get`` as
``actions_get``, and so on). New mutations take no fallback: ``actions_correct``, ``actions_withdraw``, ``actions_comment``
and ``tags_create`` are denied until a role default or an override names them. The
human-owner and worker constraints stay in the evaluator's principal-aware resolver."""

from __future__ import annotations

from qorqut_context.integration.registration import question_rule, shared_read_decision
from qorqut_operations.contracts import DomainError, PolicyDecision
from qorqut_operations.inbox.policy import INBOX_OPERATIONS
from qorqut_operations.policy.rules import PolicyRules, resolve_operation_policy as evaluate
from qorqut_operations.projects.archive import archive_approval_rule
from qorqut_operations.tables import GLOBAL_TABLE_OPERATIONS, global_tables

from qorqut.agents.roles import LEARNING_APPLIER_ROLE, ORDUBASHY_ROLE

READS_AS_TASK = frozenset({"tasks_notes_list", "tasks_thread_get", "tasks_associated_records"})
# Project records: one existing Project's inventory and explicit links. The handler itself admits only the installation
# owner and the exact active configured Ordubashy, so this standing default grants no other audience.
PROJECT_OPERATIONS = frozenset({"project_record_get", "project_items_list", "project_items_candidates",
                                "project_item_get", "project_items_link", "project_items_unlink",
                                # Move to project: the handler also rechecks the Task tools it stands on in both scopes.
                                "tasks_move_targets", "tasks_move_to_project_preview", "tasks_move_to_project"})
# CRM and Table-discussion operations and the existing operation whose decision each may never exceed. This is a
# prerequisite, not only a fallback: the new name's own override, else role default, else the prerequisite's mode is
# combined with the prerequisite's effective decision, strongest of deny > signoff > allow. An ``allow`` on the new name never
# lifts a denial or Signoff requirement of its prerequisite, and an explicit deny on the new name always wins. Comment
# authority on a Table is Table write authority (not Task comment, schema or archive authority); reading and following
# its discussion is Table read authority. ``crm_activity_record`` pins the Task and needs its comment authority.
CRM_PREREQUISITES = {
    "crm_entries_list": "tables_rows_list",
    "crm_entries_add": "tables_rows_write", "crm_entries_update": "tables_rows_write", "crm_entries_remove": "tables_rows_write",
    "tables_thread_get": "tables_get", "tables_follow": "tables_get",
    "tables_comment": "tables_rows_write",
    "crm_activity_list": "tasks_get",
    "crm_activity_record": "tasks_comment",
}
# The Table-based ones serve the Global Table namespace too, whose rule differs from the business one.
TABLE_PREREQUISITE_OPERATIONS = frozenset(name for name, base in CRM_PREREQUISITES.items() if base in GLOBAL_TABLE_OPERATIONS)
STRENGTH = {"allow": 0, "signoff": 1, "deny": 2}
# Operations whose Global rule differs from the business one, so effective-configuration discovery evaluates both scopes.
GLOBAL_DISCOVERY_OPERATIONS = GLOBAL_TABLE_OPERATIONS | TABLE_PREREQUISITE_OPERATIONS
# Repair-ticket removal, phase 1: the agent ticket/incident tools are gone. The one retained internal scoped
# reader keeps its standing grant so surviving Task, Signoff and Project reference resolution still authorizes;
# the agent MCP caller refuses it as a tool, so the grant exposes no callable agent operation. Phase 2 removes
# the reader, this grant and the remaining owner views with the feature and its data.
INTERNAL_TICKET_READS = frozenset({"dev_tickets_get"})
WORKSPACE_OPERATIONS = frozenset({"workspace_types", "workspace_get", "workspace_present", "workspace_select",
                                  "workspace_attention"})
# business_briefing composes other reads, each authorized again for the caller.
# files_link only builds a link: a path is checked lexically and a document ID through the caller's own
# document read rules. Content is read solely by the owner's viewer, never through this grant.
CORE_DEFAULT_ALLOWED = frozenset({"environment_list", "operations_batch", "operations_describe", "agent_runtime_get",
                                  "working_memory_get", "working_memory_update", "agent_context_get",
                                  "agents_activity_list", "agent_work_update", "business_briefing", "files_link", "agentic_access"})



def _extension_policy(name, defaults, overrides, registry, **scope):
    rule = getattr(registry, "extension_policy_rules", {}).get(name)
    return rule(name, defaults, overrides, registry, **scope) if rule else None


def _prerequisites(name, defaults, overrides, registry, *, role_id, scope, resolve):
    base = CRM_PREREQUISITES.get(name)
    if base is None:
        return None
    # Re-enter with the caller's own scope: the helper defaults to business, which would judge a Global call by the
    # business rule. The prerequisite keeps its Global capability, role and explicit-grant rules; nothing carries over.
    required = resolve(base, scope=scope)
    if name in overrides:
        own = PolicyDecision(overrides[name], "agent_override")
    elif name in defaults:
        own = PolicyDecision(defaults[name], "role_default")
    else:
        own = required
    decision = required if STRENGTH[required.mode] > STRENGTH[own.mode] else own
    if decision.mode == "signoff" and not registry.metadata(name).supports_signoff:
        # A requirement inherited from the prerequisite that this operation cannot stage as a Signoff is a refusal.
        return PolicyDecision("deny", "prerequisite_signoff_unsupported")
    return decision


def _user_input(name, defaults, overrides, registry, *, role_id, scope, resolve):
    # Eligible roles are necessary, never sufficient: the handler checks the exact active identity.
    # A role default or explicit grant remains required, and an explicit deny wins.
    if name == "request_user_input" and role_id not in (ORDUBASHY_ROLE, LEARNING_APPLIER_ROLE):
        return PolicyDecision("deny", "user_input_ordubashy_only")


def _owner_approvals(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in {"agent_browser_manage", "website_sign_in"}:
        # Standing owner authority for the configured Ordubashy over his workers' browsers: no per-action Signoff and no role
        # file or override can widen it to another role. The handler still proves the exact configured Ordubashy and an
        # active worker target; an explicit deny override remains respected.
        if role_id != ORDUBASHY_ROLE or overrides.get(name) == "deny":
            return PolicyDecision("deny", "browser_management_role")
        return PolicyDecision("allow", "browser_management_owner_authority")
    if name.startswith("agent_terminal_"):
        if role_id != ORDUBASHY_ROLE or overrides.get(name) == "deny":
            return PolicyDecision("deny", "terminal_control_role")
        # Owner standing authority: Ordubashy's lifecycle actions on workers need no Signoff.
        return PolicyDecision("allow", "terminal_control_owner_authority")
    if name == "owner_action_propose":
        # This is delegation of an exact change, never an owner identity/grant.
        # Even an 'allow' override must not remove the human approval boundary.
        granted = overrides.get(name, defaults.get(name, "deny")) != "deny"
        return PolicyDecision("signoff" if role_id == "ordubashy" and granted else "deny",
                              "ordubashy_owner_approval")


def _learning_applier(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name == "learnings_decide" and role_id != "learning-applier":
        return PolicyDecision("deny", "learning_applier_only")


def _context_questions(name, defaults, overrides, registry, *, role_id, scope, resolve):
    # The rule is Context's; the host supplies the Learning Applier role.
    return question_rule(name, defaults, overrides, registry, role_id=role_id, scope=scope, resolve=resolve,
                         learning_applier_role=LEARNING_APPLIER_ROLE)


def _standing_defaults(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name == "viewbox_place":
        # Every active registered agent may place on its own board; the handler derives the board from the
        # authenticated principal. An explicit deny override was already honored above.
        return PolicyDecision("allow", "core_default")
    if name in INTERNAL_TICKET_READS:
        return PolicyDecision("allow", "core_default")
    if name == "signoffs_input_answer_delegated":
        return PolicyDecision("allow" if role_id == "ordubashy" else "deny", "management_delegated_input")
    if name in INBOX_OPERATIONS:
        return PolicyDecision("allow", "core_default")
    if name in PROJECT_OPERATIONS:
        # An explicit deny override was already honored above; the handler's identity and Project-reach gates still run.
        return PolicyDecision("allow", "core_default")
    shared_read = shared_read_decision(name)
    if shared_read is not None:
        return shared_read
    if name in {"owner_conversation_get", "owner_conversation_send"} and scope == "business" and name not in defaults:
        # Exact active business membership and source reads are checked again by the conversation service.
        return PolicyDecision("allow", "business_owner_channel")


def _declared_default(registry, name):
    try:
        return registry.get(name).default_mode
    except DomainError:
        return None


def _fallback_defaults(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in {'signoffs_source_link', 'signoffs_source_unlink'}:
        # Existing role installations inherit only their input-request authority; the handler checks both endpoints.
        return resolve('signoffs_input_create')
    declared = _declared_default(registry, name)
    if declared is not None:
        # A selected provider package declared this standing mode on its own operation; the mode only applies once
        # the operation's other gates (plugin grant, global capability, scope) establish access.
        return PolicyDecision(declared, "core_default")
    if name in WORKSPACE_OPERATIONS:
        # Presentation state only; the workspace registry still gates each type.
        from qorqut.workspaces import WORKSPACE_ROLES
        return PolicyDecision("allow" if role_id in WORKSPACE_ROLES else "deny", "workspace_role")
    if name in READS_AS_TASK or name == "tasks_follow":
        # Reading a Task's thread, listing its notes and following it are all decided by the Task read grant.
        return resolve("tasks_get")
    if name in {"actions_follow", "actions_thread_get"}:
        # Following an Action and reading its discussion are decided by the Action read grant, as for Tasks.
        return resolve("actions_get")
    if name == "tasks_delegate":
        # Handing a Task to its worker is a Task update; an installed role needs no new grant for it.
        return resolve("tasks_update")
    if name == "tags_list":
        # Listing the shared tag catalog follows the Task read grant; a tag never grants access to anything.
        return resolve("tasks_get")
    if name == "owner_reminders_list" or name.startswith("reminders_"):
        # The narrow Project reminder read and the shared reminder operations follow the existing owner_reminder grant until a
        # role names them (an explicit deny on either name wins).
        return resolve("owner_reminder")
    if name == "agents_icons_list":
        # Static metadata for choosing a worker icon; whoever may list agents may list the icons, with no role republish.
        return resolve("agents_list")
    if name in CORE_DEFAULT_ALLOWED:
        return PolicyDecision("allow", "core_default")


HOST_POLICY_RULES = PolicyRules(
    # ``_prerequisites`` precedes the blanket Global-table rule so a new Table operation is never decided by it alone.
    before_override=(_extension_policy, _user_input, _prerequisites, global_tables, archive_approval_rule, _owner_approvals,
                    _learning_applier, _context_questions),
    after_override=(_standing_defaults,),
    after_default=(_fallback_defaults,),
)


def resolve_operation_policy(operation_name, defaults, overrides, registry, *, role_id=None, scope="business"):
    """The Operations evaluator bound to the host's rules, for effective-configuration inspection."""
    return evaluate(operation_name, defaults, overrides, registry, rules=HOST_POLICY_RULES, role_id=role_id, scope=scope)
