"""The Task a read-and-acknowledge report belongs to.

A report is a Signoff (``category='input_request'``, ``form.kind='acknowledge'``) and never stands alone: it is created
with the Task it reports on, and one Task may hold many reports. This association is *not* ``signoff_source_links``: a
source link says a Task or record waits on the owner's input and drives Needs-your-input, so a report never has one.
The report link only says which Task a document belongs to; it never changes the Task, its status or the Signoff.

Scope rule: a Global report names a Global Task, and a report in a project names a Task of that same project. The rule is
enforced when the report is created. The link then follows its Task when the Task is moved to another project (the report
itself stays where it was posted). A report posted before this link existed has no row and reads ``report_task: null``.
"""
from dataclasses import replace

from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

TABLE = 'signoff_report_tasks'
REPORT_TASK = schema({'id': {'type': 'string', 'minLength': 1, 'maxLength': 300},
    'business_id': {'type': ['string', 'null'], 'format': 'uuid'}}, ('id', 'business_id'))
MISSING = ('A report belongs to a Task: name it in report_task as {id, business_id} (business_id null for a Global Task, '
           'the project for a project Task). Name the real Task whose work the report is about; never create a Task only to '
           'hold a report')
SCHEMA = (
    '''CREATE TABLE signoff_report_tasks (
 signoff_id TEXT PRIMARY KEY REFERENCES signoffs(id),
 task_id TEXT NOT NULL CHECK(length(task_id) BETWEEN 1 AND 300),
 task_business_id TEXT REFERENCES businesses(id),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL
)''',
    'CREATE INDEX signoff_report_tasks_task ON signoff_report_tasks(task_id,task_business_id)',
    # Reuse the Signoff clock: attaching or moving a report's Task is a change a Signoff reader must see.
    *('CREATE TRIGGER signoff_report_tasks_' + event + ' AFTER ' + event.upper() + ' ON signoff_report_tasks '
      "BEGIN UPDATE change_sequences SET value=value+1 WHERE name='signoffs'; END" for event in ('insert', 'delete', 'update')),
)


def storage_contribution():
    return StorageContribution('signoff-report-tasks', {TABLE: TableSpec(('signoff_id',))}, schema=SCHEMA)


class ReportTasks:
    def __init__(self, service):
        self.service = service

    def check(self, ctx, task, uow):
        """Refuse a Task the caller may not read, or one outside the report's scope. ``ctx`` is already in the report's scope."""
        if task['business_id'] != ctx.business_id:
            raise DomainError('validation_error', 'A Global report belongs to a Global Task and a project report to a Task of the same '
                'project; post the report in the Task\'s own scope')
        app = self.service.app
        namespace = 'business:' + ctx.business_id if ctx.business_id else 'global'
        descriptor = app.registry.get('tasks_get')
        try:
            # The ordinary authorized Task read: whoever may not read the Task cannot attach a report to it. A closed Task is fine.
            app.authorize(ctx, descriptor, (), uow, namespace=namespace)
            read = descriptor.read(replace(ctx, business_id=task['business_id'], review_business_id=task['business_id']),
                {'task_id': task['id'], 'scope': 'business' if task['business_id'] else 'global'}, None, uow)
        except DomainError as error:
            if error.code in {'forbidden', 'policy_denied', 'not_found', 'invalid_principal', 'record_moved'}:
                raise DomainError('validation_error', 'That Task does not exist or you may not read it; name an existing Task of this scope') from None
            raise
        return read['task']

    def find(self, signoff_id, uow):
        row = uow.connection.execute(f'SELECT * FROM {TABLE} WHERE signoff_id=?', (signoff_id,)).fetchone()
        return dict(row) if row else None

    def link(self, ctx, signoff_id, task, uow):
        uow.connection.execute(f'INSERT INTO {TABLE}(signoff_id,task_id,task_business_id,created_by,created_at) VALUES(?,?,?,?,?)',
            (signoff_id, task['id'], task['business_id'], ctx.principal_id, self.service.clock()))

    def view(self, row, uow):
        """``{type, id, business_id, title}`` of a report's Task; None for a report without one.

        The title is shown while the Task is still in the report's own scope, where the reader of the report already works; a
        Task that has since moved elsewhere, or been removed, shows no title, and its own read decides who may open it."""
        link = self.find(row['id'], uow)
        if not link:
            return None
        title = None
        if link['task_business_id'] == row['review_business_id']:
            task = uow.connection.execute('SELECT title FROM tasks WHERE business_id IS ? AND id=?',
                (link['task_business_id'], link['task_id'])).fetchone()
            title = task['title'] if task else None
        return {'type': 'task', 'id': link['task_id'], 'business_id': link['task_business_id'], 'title': title}
