"""The host's concrete ``SignoffPorts``: the retained v1 domains the Operations Signoff machinery reaches.

The Signoff service, input requests, notes, rejection, notifications and the exact-decision orchestration of the
platform approvals are the Operations engine's. Everything it needs from the product is one of the typed ports below,
implemented over the retained host domains with the v1 queries and vocabulary unchanged. A port supplies vocabulary,
an identity lookup or an additional *narrowing* filter; the engine keeps every scope, decision and application check,
and no port here can grant authority.

``signoff_ports`` is called once by trusted assembly. The Signoff service is constructed before the Application that
names it as stager, and the retained domains are constructed from that Application, so the domain-backed ports resolve
their domain through ``SignoffDomains`` at call time; each is bound exactly once by trusted assembly before traffic.
The platform-action and email domains are not part of the core product: their ports are ``ABSENT`` and the engine
behaves exactly as it does without them. Every optional domain the host retains gets a real implementation."""

from __future__ import annotations

from qorqut_operations.signoffs import ABSENT, ReferenceTarget, SignoffPorts
from qorqut_operations.storage.assembly import AssemblyError

from qorqut.domains.tasks import ACTIVE_SQL
from qorqut_operations.tasks import CLOSED_STATES
from qorqut_operations.tasks.scope import task_namespace
from qorqut.inbox.attention import FIELDS as FOCUS_FIELDS, creation_access, present_created
from qorqut.management.identity import configured_ordubashy
from qorqut.management.service import MANAGED_INPUT_SQL
from .execution import EXECUTION_STATES, execution_sql, execution_state


class SignoffDomains:
    """The retained domains the Signoff ports reach, bound once each after they are constructed.

    Using a port before its domain is bound is a composition error, never a silent default."""

    NAMES = ('management', 'focus_board')

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
    """The execution-state vocabulary of a Signoff and its SQL mirror (see ``execution``)."""

    execution_states = EXECUTION_STATES

    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})

    def outcome(self, row, uow, *, detail=False):
        callback = self.outcomes.get(row['operation_name'])
        return callback(row, uow, detail=detail) if callback else None

    @staticmethod
    def execution_state(row, uow=None):
        return execution_state(row, uow)

    @staticmethod
    def execution_sql(alias):
        return execution_sql(alias)


class HostPeers:
    """v1 ``management.identity``: the current coordinator, the one principal with standing authority."""

    @staticmethod
    def configured_ordubashy(uow):
        return configured_ordubashy(uow)


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


class PrivateSignoffVisibility:
    """Additional visibility narrowing for private effects, including uninstalled providers' history."""

    def __init__(self, contributed):
        from .privacy import PRIVATE_SIGNOFF_OPERATIONS
        self.rules = dict(contributed)
        self.operations = sorted(PRIVATE_SIGNOFF_OPERATIONS | self.rules.keys())

    def narrow(self, context, uow):
        installation = uow.repository('installation').get('service')
        if context.principal_kind == 'human' and installation and installation['owner_principal_id'] == context.principal_id:
            return '', []
        excluded = [name for name in self.operations if name not in self.rules or not self.rules[name](context, uow)]
        return (' AND (s.operation_name NOT IN (' + ','.join('?' for _ in excluded) + '))', excluded) if excluded else ('', [])


class HostInputSources:
    @staticmethod
    def check(application, context, source, uow):
        from qorqut_operations.application.plans import ref
        from qorqut_operations.contracts import DomainError
        service = application.private_conversations
        selected = service.select(context, source['business_id'], uow)
        if source['type'] == 'reference' and source['business_id'] is not None:
            raise DomainError('validation_error', 'Reference threads are Global')
        service.source_check(selected, source['type'], source['id'], uow)
        # A dependency is discussion/workflow metadata, not permission to edit or execute the source's contents.
        # In particular an approval for tasks_update must remain linkable while that edit still requires approval.
        operations = {'task': ('tasks_comment', 'tasks', 'task_id'),
                      'table': ('tables_comment', 'data_tables', 'table_id'),
                      'project': ('project_items_link', 'businesses', 'project_id'),
                      'reference': ('focus_edit', 'focus_cards', 'item_id')}
        operation, table, argument = operations[source['type']]
        # Project and reference writes additionally belong to the owner/configured coordinator.
        if source['type'] in {'project', 'reference'}:
            service.role(selected, uow)
        namespace = 'business:' + selected.business_id if selected.business_id else 'global'
        decision = application.authorize(selected, application.registry.get(operation),
            (ref(table, source['id'], selected.business_id),), uow, namespace=namespace,
            arguments={argument: source['id']})
        if decision.mode != 'allow':
            raise DomainError('forbidden', 'Linking input requires direct write authority over its source')


def signoff_ports(domains: SignoffDomains, private_signoffs=None, outcomes=None) -> SignoffPorts:
    """The one ``SignoffPorts`` of this host: every retained optional integration is enabled with its real domain."""
    return SignoffPorts(execution=HostExecution(outcomes), peers=HostPeers(), tasks=HostTasks(), references=HostReferences(),
                        platform=ABSENT, email=ABSENT,
                        management=HostManagement(domains), focus=HostFocus(domains),
                        narrowing=PrivateSignoffVisibility(private_signoffs or {}), sources=HostInputSources())
