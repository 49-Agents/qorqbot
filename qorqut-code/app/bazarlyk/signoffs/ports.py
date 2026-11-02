"""The host's concrete ``SignoffPorts``: the retained v1 domains the Operations Signoff machinery reaches.

The Signoff service, input requests, notes, rejection, notifications and the exact-decision orchestration of the
platform approvals are the Operations engine's. Everything it needs from the product is one of the typed ports below,
implemented over the retained host domains with the v1 queries and vocabulary unchanged. A port supplies vocabulary,
an identity lookup or an additional *narrowing* filter; the engine keeps every scope, decision and application check,
and no port here can grant authority.

``signoff_ports`` is called once by trusted assembly. The Signoff service is constructed before the Application that
names it as stager, and the retained domains are constructed from that Application, so the domain-backed ports resolve
their domain through ``SignoffDomains`` at call time; each is bound exactly once by trusted assembly before traffic.
Every optional domain the host retains gets a real implementation; ``ABSENT`` is used only where a domain is genuinely
not installed in the composition."""

from __future__ import annotations

from qorqut_operations.signoffs import ReferenceTarget, SignoffPorts
from qorqut_operations.storage.assembly import AssemblyError

from bazarlyk.agents.global_peers import marketing_peer
from bazarlyk.domains import account_email_outcome
from bazarlyk.domains.email.decisions import email_context
from bazarlyk.domains.platform_actions.proposals import EXECUTION_STATES, execution_sql, execution_state
from bazarlyk.domains.campaigns.attribution import campaign
from bazarlyk.domains.tasks import ACTIVE_SQL
from qorqut_operations.tasks import CLOSED_STATES
from qorqut_operations.tasks.scope import task_namespace
from bazarlyk.inbox.attention import FIELDS as FOCUS_FIELDS, creation_access, present_created
from bazarlyk.management.identity import configured_ordubashy
from bazarlyk.management.service import MANAGED_INPUT_SQL


class SignoffDomains:
    """The retained domains the Signoff ports reach, bound once each after they are constructed.

    Using a port before its domain is bound is a composition error, never a silent default."""

    NAMES = ('platform_actions', 'email', 'management', 'focus_board', 'whatsapp_send')

    def __init__(self):
        self._bound = {}

    def bind(self, **domains):
        for name, value in domains.items():
            if name not in self.NAMES or value is None:
                raise AssemblyError(f'Unknown or empty Signoff domain: {name}')
            if name in self._bound:
                raise AssemblyError(f'Signoff domain already bound: {name}')
            self._bound[name] = value
        return self

    def require(self):
        """Fail assembly if any domain a port resolves was never bound."""
        missing = [name for name in self.NAMES if name not in self._bound]
        if missing:
            raise AssemblyError('Signoff domains are not bound: ' + ', '.join(missing))
        return self

    def __getattr__(self, name):
        if name.startswith('_') or name not in SignoffDomains.NAMES:
            raise AttributeError(name)
        try:
            return self._bound[name]
        except KeyError:
            raise AssemblyError(f'Signoff domain {name} is used before assembly bound it') from None


class HostExecution:
    """v1 ``platform_actions.proposals``: the execution-state vocabulary of a proposal and its SQL mirror."""

    execution_states = EXECUTION_STATES

    @staticmethod
    def execution_state(row, uow=None):
        return execution_state(row, uow)

    @staticmethod
    def execution_sql(alias):
        return execution_sql(alias)

    @staticmethod
    def outcome(row, uow, *, detail=True):
        """The account-mail result of an approved send, derived from its job (read only; see ``account_email_outcome``):
        the bounded summary in a list row, the full model in a detail read."""
        return account_email_outcome.signoff_outcome(row, uow, detail=detail)


class HostPeers:
    """v1 ``management.identity`` and ``agents.global_peers``: the current coordinator and Marketing peer."""

    @staticmethod
    def configured_ordubashy(uow, *, require_codex):
        return configured_ordubashy(uow, require_codex=require_codex)

    @staticmethod
    def marketing_peer(uow, principal_id):
        return marketing_peer(uow, principal_id)


class HostTasks:
    """The owner attention overview's open-task count: the fixed Task states and the shared snooze rule."""

    @staticmethod
    def open_assigned_count(uow, business_id, principal_id, now):
        closed = set(CLOSED_STATES)
        return sum(task['status'] not in closed for task in uow.connection.execute('''SELECT t.status FROM tasks t
            WHERE t.business_id IS ? AND t.archived=0 AND EXISTS(SELECT 1 FROM relationships r
            WHERE r.business_id IS t.business_id AND r.from_type='task' AND r.from_id=t.id
            AND r.role='assigned_to' AND r.to_type='person' AND r.to_id=?) AND ''' + ACTIVE_SQL,
            (business_id, principal_id, now)))


class HostReferences:
    """v1 ``tasks.operations.NOTE_REFS`` and its read operations: what a conversation note may cite, in the schema's
    order. The engine reads each reference through the named operation as the actor, never directly."""

    targets = {'signoff': ReferenceTarget('signoffs_get', 'signoff_id'),
               'task': ReferenceTarget('tasks_get', 'task_id'),
               'dev_ticket': ReferenceTarget('dev_tickets_get', 'ticket_id')}

    @staticmethod
    def task_namespace(context, arguments):
        return task_namespace(context, arguments)


class HostPlatform:
    """v1 ``app.platform_actions``: staging, transition and presentation of platform-action proposals."""

    def __init__(self, domains):
        self.domains = domains

    def staging_values(self, context, plan, old, uow):
        return self.domains.platform_actions.staging_values(context, plan, old, uow)

    def staged(self, context, row, plan, old, uow):
        return self.domains.platform_actions.staged(context, row, plan, old, uow)

    def transition(self, row, updated, uow):
        return self.domains.platform_actions.transition(row, updated, uow)

    def replaceable_after_cancel(self, old, uow):
        return self.domains.platform_actions.replaceable_after_cancel(old, uow)

    def view(self, row, uow=None):
        return self.domains.platform_actions.view(row, uow)

    def enqueue_binding(self, plan):
        return self.domains.platform_actions.enqueue_binding(plan)

    def is_email_submission(self, plan):
        return self.domains.platform_actions.is_email_submission(plan)


class HostEmailDecisions:
    """Email-linked owner questions (v1 ``domains.email.decisions``): who may read them and the thread context."""

    def __init__(self, domains):
        self.domains = domains

    def visibility(self, context, uow):
        return self.domains.email.decisions.visibility(context, uow)

    @staticmethod
    def email_context(row, uow):
        return email_context(row, uow)


class HostCampaigns:
    """Campaign filtering of ``signoffs_list``: the campaign grouping Task must exist and carry the campaign label, and the
    Signoff must be attributed to it. The engine first authorizes ``tasks_get`` for the caller and wraps this predicate in
    its own parentheses."""

    authorizing_operation = 'tasks_get'

    @staticmethod
    def narrow(context, campaign_id, uow):
        campaign(context, campaign_id, uow)
        return ("s.category='platform_action' AND EXISTS (SELECT 1 FROM relationships r "
                "WHERE r.business_id=s.review_business_id AND r.from_type='signoff' AND r.from_id=s.id "
                "AND r.role='campaign_content_of' AND r.to_type='task' AND r.to_id=?)", [campaign_id])


class HostManagement:
    """Management routing (v1 ``management.service``): the managed-input predicate, the one coordinator, the
    automatic route of a management-allowed question and the stored routing row of a Signoff."""

    managed_input_sql = MANAGED_INPUT_SQL

    def __init__(self, domains):
        self.domains = domains

    def coordinator(self, uow):
        return self.domains.management._coordinator(uow)

    def auto_route_input(self, context, row, uow):
        return self.domains.management.auto_route_input(context, row, uow)

    @staticmethod
    def routing_for(uow, signoff_id):
        return uow.connection.execute("SELECT * FROM management_routing WHERE source_type='signoffs' AND source_id=?",
                                      (signoff_id,)).fetchone()


class HostFocus:
    """Owner Focus attention (v1 ``inbox.attention``): the ``focus`` argument schema, Focus access and presentation
    of a created input request, and release of a Signoff's card when it stops awaiting a decision."""

    fields = FOCUS_FIELDS

    def __init__(self, domains):
        self.domains = domains

    @staticmethod
    def creation_access(application, context, focus, uow):
        creation_access(application, context, focus, uow)

    @staticmethod
    def present_created(application, context, focus, source, uow):
        present_created(application, context, focus, source, uow)

    def release_signoff(self, row, updated, uow):
        self.domains.focus_board.release_signoff(row, updated, uow)


class HostWhatsAppVisibility:
    """Personal WhatsApp Signoffs carry the exact recipient and message text, so they are readable only by the installation
    owner and by the agent the binding names right now (and only while it is an active Global Ordubashy). Every other
    reader, proposers that are no longer bound included, sees no such Signoff in any read. A fixed predicate that can only
    remove rows; it grants nothing, and the engine's own scope and policy checks still apply first."""

    OPERATION = 'whatsapp_send'

    def __init__(self, domains):
        self.domains = domains

    def narrow(self, context, uow):
        installation = uow.repository('installation').get('service')
        if (context.principal_kind == 'human' and installation and installation['owner_principal_id'] == context.principal_id):
            return '', []
        if context.principal_kind == 'agent' and context.delegation_assignment_id is None \
                and self.domains.whatsapp_send.bound_agent(uow) == context.principal_id:
            return '', []
        return ' AND (s.operation_name <> ?)', [self.OPERATION]


def signoff_ports(domains: SignoffDomains) -> SignoffPorts:
    """The one ``SignoffPorts`` of this host: every retained optional integration is enabled with its real domain."""
    return SignoffPorts(execution=HostExecution(), peers=HostPeers(), tasks=HostTasks(), references=HostReferences(),
                        platform=HostPlatform(domains), email=HostEmailDecisions(domains),
                        campaigns=HostCampaigns(), management=HostManagement(domains), focus=HostFocus(domains),
                        narrowing=HostWhatsAppVisibility(domains))
