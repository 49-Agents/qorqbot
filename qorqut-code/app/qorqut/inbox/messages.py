"""Host side of the direct messages; the message flow itself is Operations' ``Messages``.

``MessageHost`` is the adapter the flow calls at fixed points: owner-directed contacts, the delegated worker's assignment
binding and attribution, forwarding of an owner message to Ordubashy, terminal delivery and the retirement handoff read.
``Messages`` is that flow with this adapter bound, so ``InboxService`` constructs it from the inbox alone."""
from qorqut.agents.contact import owner_directed_target
from qorqut_operations.contracts import DomainError
from qorqut_operations.inbox import Messages as OperationsMessages
from .owner_forwarding import forward_owner_message


# The most characters of one message from a permanent worker to Ordubashy.
WORKER_MESSAGE_LIMIT = 1200


class MessageHost:
    def __init__(self, inbox):
        self.inbox, self.app = inbox, inbox.app

    def owner_directed_target(self, ctx, identity, uow):
        return owner_directed_target(ctx, identity, uow)

    def binding(self, ctx, uow):
        """The running assignment of a delegated worker, refused unless it and its binding are the current ones."""
        if ctx.delegation_assignment_id is None:
            return None
        assignment = uow.repository('operational_assignments').get(ctx.delegation_assignment_id)
        binding = uow.repository('operational_delegation_bindings').get(ctx.delegation_binding_id)
        if (assignment is None or assignment['parent_principal_id'] != ctx.principal_id
                or assignment['state'] != 'running'
                or assignment['execution_generation'] != ctx.delegation_generation
                or binding is None or binding['assignment_id'] != assignment['id']
                or binding['execution_generation'] != ctx.delegation_generation
                or binding['revoked_at'] is not None):
            raise DomainError('forbidden', 'Worker message requires its current assignment binding')
        return assignment

    def validate_recipient(self, ctx, recipient, parent, binding, uow):
        if binding is None:
            return
        linked_threads = set(binding['links_json'].get('message_threads', ()))
        if parent and parent['delegation_assignment_id'] != binding['id'] and parent['thread_id'] not in linked_threads:
            raise DomainError('forbidden', 'Worker may reply only within a thread bound to its assignment')
        if recipient not in binding['internal_recipients_json']:
            raise DomainError('forbidden', 'Message recipient is outside the approved assignment')

    def message_limit(self, ctx, recipient, uow):
        """A permanent worker's message to Ordubashy is at most ``WORKER_MESSAGE_LIMIT`` characters; nobody else is bounded.

        Every such message is read in full by the coordinator, so a long report costs him context on each one. The
        result of finished work is the Task's closing result, and evidence belongs in the Task or a linked file."""
        from qorqut.agents.worker_access import is_worker
        from qorqut.management.identity import configured_ordubashy
        if ctx.principal_kind == 'agent' and recipient == configured_ordubashy(uow) and is_worker(uow.connection, ctx.principal_id):
            return WORKER_MESSAGE_LIMIT
        return None

    def attribution(self, ctx, parent, uow):
        assignment_id = ctx.delegation_assignment_id
        generation = ctx.delegation_generation
        binding_id = ctx.delegation_binding_id
        plan_revision = None
        if assignment_id is not None:
            assignment = uow.repository('operational_assignments').get(assignment_id, required=True)
            plan_revision = assignment['plan_revision']
        elif parent and parent['delegation_assignment_id'] is not None:
            assignment_id = parent['delegation_assignment_id']
            generation = parent['delegation_generation']
            plan_revision = parent['delegation_plan_revision']
        return assignment_id, generation, binding_id, plan_revision

    def history_scope(self, ctx, thread_id, uow):
        if ctx.delegation_assignment_id is None:
            return [], []
        assignment = uow.repository('operational_assignments').get(ctx.delegation_assignment_id, required=True)
        if thread_id in set(assignment['links_json'].get('message_threads', ())):
            return ['(delegation_assignment_id=? OR thread_id=?)'], [ctx.delegation_assignment_id, thread_id]
        return ['delegation_assignment_id=?'], [ctx.delegation_assignment_id]

    def forward_owner_message(self, uow, *, kind, source_id, sender_id, worker_id, body, accepted_at):
        forward_owner_message(uow, kind=kind, source_id=source_id, sender_id=sender_id, worker_id=worker_id,
                              body=body, accepted_at=accepted_at)

    def retirement_backlog(self, messages, ctx, args, uow):
        from .retirement_backlog import read
        return read(messages, ctx, args, uow)

    def validate_terminal(self, ctx, recipient, message, uow):
        if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in message):
            raise DomainError('validation_error', 'Terminal messages cannot contain control characters')
        controller = getattr(self.app, 'terminal_control', None)
        if controller is None:
            raise DomainError('operation_unavailable', 'Terminal message delivery is unavailable; use inbox delivery')
        target = controller.message_target(ctx, recipient, uow)
        binding = controller.binding(target)
        controller.backend.validate('ping', binding['terminal'])

    def queue_terminal(self, ctx, row, uow):
        self.app.terminal_control.queue_message(ctx, row, uow)

    def terminal_projection(self, row, uow):
        terminal = uow.connection.execute(
            "SELECT id,state,detail FROM terminal_actions WHERE id=? AND json_extract(binding_json,'$.message_id')=?",
            (row['id'], row['id'])).fetchone()
        if terminal:
            return {'action_id': terminal['id'], 'state': terminal['state'], 'detail': terminal['detail']}
        return None


class Messages(OperationsMessages):
    def __init__(self, inbox):
        super().__init__(inbox, host=MessageHost(inbox))
