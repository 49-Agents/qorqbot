"""The retained v1 domain permission rules, contributed to the Operations policy evaluator as data.

The evaluator itself is ``qorqut_operations.policy``; nothing here resolves a decision on its own. The deployed v1
resolver interleaved these per-domain rules with the generic precedence steps, so they are placed in the three
phases of ``PolicyRules`` exactly where v1 evaluated them:

* ``before_override``: rules that decide before an agent's explicit override is consulted, either because they
  refuse it (Telegram roles, the Ordubashy-only user-input presentation, Global table archive, terminal control, owner-action
  proposals, ``learning-applier`` only operations, the Learning Applier-only Context question tools that follow the caller's current
  ``context_edit`` decision) or because they read the override themselves (email aliases);
* ``after_override``: standing core defaults that sit between the explicit override and the role default;
* ``after_default``: core defaults that apply only when neither an override nor a role default names the operation.

``resolve`` is the evaluator's re-entry (``tasks_notes_list`` reads as ``tasks_get``, ``actions_thread_get`` as
``actions_get``, and so on). New mutations take no fallback: ``actions_correct``, ``actions_withdraw``, ``actions_comment``
and ``tags_create`` are denied until a role default or an override names them. The
human-owner and worker constraints stay in the evaluator's principal-aware resolver."""

from __future__ import annotations

from qorqut_operations.contracts import PolicyDecision
from qorqut_operations.policy.rules import PolicyRules, resolve_operation_policy as evaluate

from bazarlyk.agents.roles import BUSINESS_AGENT_ROLE, GLOBAL_MARKETING_ROLE, LEARNING_APPLIER_ROLE, ORDUBASHY_ROLE
from bazarlyk.domains.email.contract import DEFAULT_APPROVAL as EMAIL_DEFAULTS, LEGACY_ALIAS_TARGETS as EMAIL_LEGACY
from bazarlyk.inbox.defaults import INBOX_OPERATIONS

READS_AS_TASK = frozenset({"tasks_notes_list", "tasks_thread_get"})
SHARED_CONTEXT_READS = frozenset({"context_home", "context_object", "context_expand", "context_find"})
TELEGRAM_AGENT_OPERATIONS = frozenset({"telegram_send", "telegram_send_file", "telegram_history", "telegram_delivery_get"})
TELEGRAM_ROLES = frozenset({ORDUBASHY_ROLE})
# Global tables are a separate namespace from every business. The same eight operations (plus
# archive) serve both; these roles alone may use the Global one, whatever their business defaults.
GLOBAL_TABLE_ROLES = frozenset({GLOBAL_MARKETING_ROLE, ORDUBASHY_ROLE})
GLOBAL_TABLE_OPERATIONS = frozenset({"tables_list", "tables_get", "tables_rows_list", "tables_history", "tables_create",
                                     "tables_update", "tables_rows_write", "tables_history_retry", "tables_archive"})
# Operations whose Global rule differs from the business one, so effective-configuration discovery evaluates both scopes.
GLOBAL_DISCOVERY_OPERATIONS = GLOBAL_TABLE_OPERATIONS | {"whatsapp_send"}
CONTEXT_QUESTION_OPERATIONS = frozenset({"context_questions", "context_question_edit"})
MILESTONE_OPERATIONS = frozenset({"milestones_list", "milestones_create", "milestones_update", "milestones_status_set"})
DEV_AND_INCIDENT_READS = frozenset({"dev_tickets_create", "dev_tickets_agent_list", "dev_tickets_get",
                                    "dev_tickets_follow_up", "dev_tickets_resolve", "system_incidents_agent_list",
                                    "system_incidents_agent_get"})
WORKSPACE_OPERATIONS = frozenset({"workspace_types", "workspace_get", "workspace_present", "workspace_select",
                                  "workspace_attention"})
# business_briefing composes other reads, each authorized again for the caller.
# files_link only builds a link: a path is checked lexically and a document ID through the caller's own
# document read rules. Content is read solely by the owner's viewer, never through this grant.
CORE_DEFAULT_ALLOWED = frozenset({"environment_list", "operations_batch", "operations_describe", "agent_runtime_get",
                                  "working_memory_get", "working_memory_update", "agent_context_get",
                                  "agents_activity_list", "agent_work_update", "business_briefing", "files_link"})


def _telegram(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in TELEGRAM_AGENT_OPERATIONS:
        # The role is necessary, never sufficient: the operation also requires the exact configured agent for the chat.
        if role_id not in TELEGRAM_ROLES:
            return PolicyDecision("deny", "telegram_role")
        return PolicyDecision(overrides.get(name, defaults.get(name, "deny")),
                              "agent_override" if name in overrides else "role_default")
    if name == "telegram_notify":
        # Trusted registered workers only (they carry no role); an agent notifies through its own chat.
        return PolicyDecision(defaults.get(name, "deny") if role_id is None else "deny", "telegram_worker")


def _user_input(name, defaults, overrides, registry, *, role_id, scope, resolve):
    # The presentation tool belongs to Ordubashy alone; the role is necessary, and the operation also requires the exact configured
    # active agent. A role default or an explicit grant is still required, and an explicit deny wins.
    if name == "request_user_input" and role_id != ORDUBASHY_ROLE:
        return PolicyDecision("deny", "user_input_ordubashy_only")


def _global_tables(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in GLOBAL_TABLE_OPERATIONS and scope == "global":
        # The named roles hold direct Global defaults. Any other eligible Global agent (the runtime checks that it is
        # an active ecosystem agent holding read_global_context) needs an explicit override; role business
        # defaults never carry over, and a missing grant denies.
        default_role = role_id in GLOBAL_TABLE_ROLES
        if name == "tables_archive":
            # An agent never archives a Global table on its own authority: an explicit deny (here or on
            # tables_update, which carries the same effect) wins, and no 'allow' removes the owner's approval.
            grants = (overrides.get(name), overrides.get("tables_update"))
            if "deny" in grants:
                return PolicyDecision("deny", "global_table_archive_owner_approval")
            if default_role or {"allow", "signoff"} & set(grants):
                return PolicyDecision("signoff", "global_table_archive_owner_approval")
            return PolicyDecision("deny", "global_table_grant_required")
        if name in overrides:
            return PolicyDecision(overrides[name], "agent_override")
        return (PolicyDecision("allow", "global_table_role") if default_role
                else PolicyDecision("deny", "global_table_grant_required"))
    if name == "tables_archive":
        return (PolicyDecision(overrides[name], "agent_override") if name in overrides
                else resolve("tables_update"))


def _owner_approvals(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name == "business_archive":
        granted = overrides.get(name, defaults.get(name, "deny")) != "deny"
        return PolicyDecision("signoff" if role_id == "ordubashy" and granted else "deny",
                              "business_archive_owner_approval")
    if name.startswith("agent_terminal_"):
        if role_id != ORDUBASHY_ROLE or overrides.get(name) == "deny":
            return PolicyDecision("deny", "terminal_control_role")
        # Owner standing authority: Ordubashy's lifecycle actions on workers need no Signoff.
        return PolicyDecision("allow", "terminal_control_owner_authority")
    if name == "whatsapp_send":
        # A proposal for the owner's Signoff, never a send. Like owner_action_propose it needs a grant (override or role
        # default), a deny anywhere wins, only the global Ordubashy qualifies, and an 'allow' cannot remove the approval.
        # The operation itself also requires the exact agent bound by config/whatsapp-personal.json.
        granted = overrides.get(name, defaults.get(name, "deny")) != "deny"
        return PolicyDecision("signoff" if role_id == ORDUBASHY_ROLE and scope == "global" and granted else "deny",
                              "whatsapp_owner_approval")
    if name == "owner_action_propose":
        # This is delegation of an exact change, never an owner identity/grant.
        # Even an 'allow' override must not remove the human approval boundary.
        granted = overrides.get(name, defaults.get(name, "deny")) != "deny"
        return PolicyDecision("signoff" if role_id == "ordubashy" and granted else "deny",
                              "ordubashy_owner_approval")


def _learning_applier(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in {"learnings_decide", "learnings_issue_action"} and role_id != "learning-applier":
        return PolicyDecision("deny", "learning_applier_only")


def _context_questions(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name not in CONTEXT_QUESTION_OPERATIONS:
        return None
    if role_id != LEARNING_APPLIER_ROLE:
        return PolicyDecision("deny", "context_questions_role")
    if "deny" in (overrides.get(name), defaults.get(name)):
        return PolicyDecision("deny", "context_questions_denied")
    edit = resolve("context_edit", scope=scope)
    return PolicyDecision("allow" if edit.mode == "allow" else "deny", "context_edit_authority")


def _email_alias_overrides(name, defaults, overrides, registry, *, role_id, scope, resolve):
    canonical_email = EMAIL_LEGACY.get(name, name)
    aliases = {alias for alias, target in EMAIL_LEGACY.items() if target == canonical_email} | {canonical_email}
    email_overrides = [overrides[alias] for alias in aliases if alias in overrides]
    if canonical_email in EMAIL_DEFAULTS and email_overrides:
        mode = "deny" if "deny" in email_overrides else "signoff" if "signoff" in email_overrides else "allow"
        return PolicyDecision(mode, "agent_override")


def _marketing_business_mail(name, defaults, overrides, registry, *, role_id, scope, resolve):
    canonical_email = EMAIL_LEGACY.get(name, name)
    if (role_id == GLOBAL_MARKETING_ROLE and scope == "business"
            and canonical_email in EMAIL_DEFAULTS
            and canonical_email not in {"email_vacation_set", "email_masked_email_set"}):
        # Owner standing business-mail authority. Plugin, active membership and
        # mailbox ownership remain mandatory; private account scope is gated separately.
        return PolicyDecision("allow", "core_default")


def _standing_defaults(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in MILESTONE_OPERATIONS:
        return PolicyDecision(defaults.get(name, "allow") if role_id in {"ordubashy", "global-marketing"}
                              else "deny", "milestone_role")
    if name == "viewbox_place":
        # Every active registered agent may place on its own board; the handler derives the board from the
        # authenticated principal. An explicit deny override was already honored above.
        return PolicyDecision("allow", "core_default")
    if name in DEV_AND_INCIDENT_READS:
        return PolicyDecision("allow", "core_default")
    if name == "plugins_enable_delegated":
        return PolicyDecision("allow" if role_id in {BUSINESS_AGENT_ROLE, ORDUBASHY_ROLE} else "deny",
                              "exact_mailbox_delegation")
    if name == "signoffs_input_answer_delegated":
        return PolicyDecision("allow" if role_id == "ordubashy" else "deny", "management_delegated_input")
    if name in INBOX_OPERATIONS:
        return PolicyDecision("allow", "core_default")
    if name in SHARED_CONTEXT_READS:
        # The shared graph reads are the agent-facing surface of the published Context; no role republish needed.
        return PolicyDecision("allow", "core_default")
    if name in {"owner_conversation_get", "owner_conversation_send"} and scope == "business" and name not in defaults:
        # Exact active business membership and source reads are checked again by the conversation service.
        return PolicyDecision("allow", "business_owner_channel")


def _fallback_defaults(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in EMAIL_DEFAULTS or name in EMAIL_LEGACY:
        # Contract §2 approval defaults (owner ruling 2A): the mode only applies
        # once the plugin grant or account capability establishes access.
        return PolicyDecision(EMAIL_DEFAULTS[EMAIL_LEGACY.get(name, name)], "core_default")
    if name in WORKSPACE_OPERATIONS:
        # Presentation state only; the workspace registry still gates each type.
        from bazarlyk.workspaces import WORKSPACE_ROLES
        return PolicyDecision("allow" if role_id in WORKSPACE_ROLES else "deny", "workspace_role")
    if name in READS_AS_TASK or name == "tasks_follow":
        # Reading a Task's thread, listing its notes and following it are all decided by the Task read grant.
        return resolve("tasks_get")
    if name in {"actions_follow", "actions_thread_get"}:
        # Following an Action and reading its discussion are decided by the Action read grant, as for Tasks.
        return resolve("actions_get")
    if name == "tags_list":
        # Listing the shared tag catalog follows the Task read grant; a tag never grants access to anything.
        return resolve("tasks_get")
    if name == "business_readiness_get":
        return resolve("business_config_get")
    if name == "agents_icons_list":
        # Static metadata for choosing a worker icon; whoever may list agents may list the icons, with no role republish.
        return resolve("agents_list")
    if name in CORE_DEFAULT_ALLOWED:
        return PolicyDecision("allow", "core_default")


HOST_POLICY_RULES = PolicyRules(
    before_override=(_telegram, _user_input, _global_tables, _owner_approvals, _learning_applier, _context_questions,
                     _email_alias_overrides),
    after_override=(_marketing_business_mail, _standing_defaults),
    after_default=(_fallback_defaults,),
)


def resolve_operation_policy(operation_name, defaults, overrides, registry, *, role_id=None, scope="business"):
    """The Operations evaluator bound to the host's rules, for effective-configuration inspection."""
    return evaluate(operation_name, defaults, overrides, registry, rules=HOST_POLICY_RULES, role_id=role_id, scope=scope)
