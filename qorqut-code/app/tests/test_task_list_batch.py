"""``tasks_list`` builds a page's views in one batch of reads and filters in SQL, with the same rows and cursors as before.

The reference here is the earlier implementation: every matching Task loaded, filtered in Python, paged, then each row's
view built with its own queries. Every filter and cursor combination must give the same Tasks in the same order, and each
view must equal the one-at-a-time view plus the names it now adds."""
from types import SimpleNamespace as NS
import unittest

from qorqut_operations.contracts import plain
from qorqut_operations.application.plans import page, rows, timestamp
from qorqut_operations.discussion.follows import ThreadItem, follow_state
from qorqut_operations.discussion.threads import message_count
from qorqut_operations.tags.catalog import distinct_labels, item_labels, labels_by_item, matches
from qorqut_operations.tasks import events, lifecycle
from qorqut_operations.tasks.kinds import public_task
from qorqut_operations.tasks.models import snooze_view
from qorqut_operations.tasks.overview import TaskOverview
from qorqut_operations.tasks.schema import CLOSED_STATES, PAUSED_STATES, STATES
from qorqut_operations.tasks.types import attributes, custom_fields
from qorqut_operations.tasks.workers import project_workers

from tests import test_task_change_records as records

ADDED = ("project_name", "assignees", "blockers")


def legacy_view(ctx, task, uow, now):
    project_id, task_id = task["project_id"], task["id"]
    item = ThreadItem(project_id, "task", task_id)
    creator = uow.repository("principals").get(task.get("created_by")) if task.get("created_by") else None
    scope = uow.repository("tasks", project_id=project_id)
    blockers = events.blockers(uow, project_id, task_id)
    unresolved = [scope.get(blocker)["id"] for blocker in blockers
                  if (scope.get(blocker) or {"status": "done"})["status"] not in CLOSED_STATES]
    blocks = [row[0] for row in uow.connection.execute(
        "SELECT DISTINCT t.id FROM relationships r JOIN tasks t ON t.project_id IS r.project_id AND t.id=r.from_id "
        "WHERE r.project_id IS ? AND r.from_type='task' AND r.to_type='task' AND r.to_id=? AND r.role='blocked_by' "
        "AND t.status NOT IN ('done','cancelled') ORDER BY t.id", (project_id, task_id))]
    parent = scope.get(task["parent_id"]) if task.get("parent_id") else None
    children = [dict(row) for row in uow.connection.execute(
        "SELECT id,title,status FROM tasks WHERE project_id IS ? AND parent_id=? ORDER BY created_at,id LIMIT 51", (project_id, task_id))]
    follow = follow_state(uow, item, ctx.principal_id)
    return public_task({
        **{key: value for key, value in task.items() if key not in {"fields_json", "worker_ids_json", "snoozed_until", "snooze_generation"}},
        **snooze_view(task, now),
        "created_by_name": creator.get("display_name", "") if creator else "",
        "created_by_kind": creator.get("kind", "") if creator else "",
        "tags": item_labels(uow, project_id, "task", task_id), "fields": custom_fields(task), **attributes(task),
        "thread_id": item.thread_id, "note_count": message_count(uow, item),
        **project_workers(uow, task),
        "assignee_ids": events.assigned(uow, project_id, task_id), "archived": bool(task.get("archived")),
        "blocked_by": blockers, "unresolved_blockers": sorted(set(unresolved)), "blocks": blocks,
        "parent": {key: parent[key] for key in ("id", "title", "status")} if parent else None,
        "children": children[:50], "children_has_more": len(children) > 50,
        "closed": task["status"] in CLOSED_STATES, "paused": task["status"] in PAUSED_STATES,
        "stalled": lifecycle.stalled(task, now),
        "following": follow["following"], "follow_reason": follow["reason"],
        "notes": [], "notes_next_cursor": None})


def legacy_list(ctx, arguments, uow, now):
    """The earlier ``list_tasks`` (title-and-id search), with the earlier per-row view."""
    clauses, parameters = [], []
    snooze_filter = arguments.get("snooze_filter", "active")
    if snooze_filter != "all":
        active = "(snoozed_until IS NULL OR snoozed_until<=?)"
        clauses.append(active if snooze_filter == "active" else "NOT " + active)
        parameters.append(now)
    if "parent_id" in arguments:
        clauses.append("parent_id IS ?")
        parameters.append(arguments["parent_id"])
    if not arguments.get("include_archived"):
        clauses.append("archived=0")
    states = [name for name in STATES if (not arguments.get("statuses") or name in arguments["statuses"])
              and (arguments.get("include_closed", True) or name not in CLOSED_STATES)
              and (arguments.get("include_paused", True) or name not in PAUSED_STATES)]
    tasks = rows(uow, "tasks", ctx.project_id, where=" AND ".join(clauses), parameters=parameters)
    if "task_type" in arguments:
        tasks = [task for task in tasks if attributes(task)["task_type"] == arguments["task_type"]]
    if "email_conversation_id" in arguments:
        tasks = [task for task in tasks if attributes(task)["email_conversation_id"] == arguments["email_conversation_id"]]
    if arguments.get("tags"):
        wanted = distinct_labels(arguments["tags"])
        labels = labels_by_item(uow, ctx.project_id, "task")
        tasks = [task for task in tasks if matches(labels.get(task["id"], ()), wanted)]
    if arguments.get("search"):
        tasks = [task for task in tasks if arguments["search"].casefold() in (task["title"] + " " + task["id"]).casefold()]
    tasks = [task for task in tasks if task["status"] in states]
    detailed = [legacy_view(ctx, task, uow, now) for task in tasks]
    if "blocked" in arguments:
        detailed = [task for task in detailed if bool(task["unresolved_blockers"]) == arguments["blocked"]]
    selected, cursor = page(detailed, arguments, ctx.project_id, "tasks_list")
    return {"tasks": selected, "next_cursor": cursor}


class TaskListBatchTests(unittest.TestCase):
    setUp = records.TaskChangeRecordTests.setUp
    invoke = records.TaskChangeRecordTests.invoke
    create = records.TaskChangeRecordTests.create

    def ok(self, operation, arguments, **options):
        result = self.invoke(operation, arguments, **options)
        self.assertEqual(result.status, 'ok', result.to_dict())
        return plain(result.data)

    def build(self):
        made = {}
        made['parent'] = self.create(title='Launch plan', tags=['Launch', 'Ops'], overview='Coordinate the Café launch')
        for n in range(3):
            made[f'child{n}'] = self.create(title=f'Step {n}', parent_id=made['parent']['id'], tags=['Launch'])
        made['done_blocker'] = self.create(title='Book venue')
        done = self.ok('tasks_complete', {'task_id': made['done_blocker']['id']}, revision=made['done_blocker']['revision'])
        made['open_blocker'] = self.create(title='Sign contract', worker_ids=[self.agent, self.owner])
        made['blocked'] = self.create(title='Send invitations', blocked_by=[made['open_blocker']['id']],
                                      assignee_ids=[self.agent], status_line='Waiting on the contract')
        made['unblocked'] = self.create(title='Print menus', blocked_by=[done['task']['id']], task_type='story')
        made['email'] = self.create(title='Reply to supplier', next_step='Ask about ÉCLAIRS pricing')
        # A historical Email Task: its binding is stored data (creating one needs a resolvable conversation).
        with self.db.transaction() as uow:
            uow.connection.execute("""UPDATE tasks SET fields_json=json_set(fields_json, '$."$task".task_type', 'email',
                '$."$task".email_conversation_id', 'conv-1') WHERE id=?""", (made['email']['id'],))
        made['waiting'] = self.create(title='Await quote', status='waiting')
        made['snoozed'] = self.create(title='Revisit later', snoozed_until='2099-01-01T00:00:00Z')
        commented = self.create(title='Plain record')
        self.ok('tasks_comment', {'task_id': commented['id'], 'text': 'The Straße permit arrived.'}, ctx=self.worker)
        made['commented'] = commented
        return made

    def compare(self, arguments):
        """Walk every page of both implementations; return the Task ids seen."""
        seen, cursor = [], None
        while True:
            args = {**arguments, **({'cursor': cursor} if cursor else {})}
            current = plain(self.ok('tasks_list', args))
            with self.db.read() as uow:
                ctx = self.app.current_context(self.ctx, uow)
                reference = legacy_list(ctx, args, uow, timestamp(self.tasks.clock))
            reference = plain(reference)
            self.assertEqual([task['id'] for task in current['tasks']], [task['id'] for task in reference['tasks']], args)
            self.assertEqual(current['next_cursor'], reference['next_cursor'], args)
            for new, old in zip(current['tasks'], reference['tasks']):
                self.assertEqual({key: value for key, value in new.items() if key not in ADDED},
                                 old, new['title'])
            seen += [task['id'] for task in current['tasks']]
            cursor = current['next_cursor']
            if not cursor:
                return seen

    def test_filters_and_pages_match_the_earlier_listing(self):
        made = self.build()
        cases = [{}, {'limit': 2}, {'limit': 3, 'snooze_filter': 'all'}, {'snooze_filter': 'snoozed'},
                 {'include_closed': False, 'limit': 2}, {'include_paused': False}, {'statuses': ['waiting', 'done']},
                 {'task_type': 'story'}, {'task_type': 'task', 'limit': 2}, {'email_conversation_id': 'conv-1'},
                 {'tags': ['launch']}, {'tags': ['LAUNCH', 'ops']}, {'blocked': True}, {'blocked': False, 'limit': 2},
                 {'blocked': False, 'include_closed': False}, {'parent_id': made['parent']['id'], 'limit': 2}, {'parent_id': None},
                 {'assignee_id': self.agent}, {'assignee_id': None, 'limit': 4}, {'search': 'STEP'},
                 {'search': made['email']['id'][:8]}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                if 'assignee_id' in arguments:
                    # The reference has no assignee clause; compare against an explicit filter of the same rows.
                    listed = self.ok('tasks_list', {**{k: v for k, v in arguments.items() if k != 'limit'}, 'limit': 200})['tasks']
                    everything = self.ok('tasks_list', {'limit': 200})['tasks']
                    expected = [t['id'] for t in everything if (arguments['assignee_id'] in t['assignee_ids']
                                                               if arguments['assignee_id'] else not t['assignee_ids'])]
                    self.assertEqual([t['id'] for t in listed], expected)
                    continue
                self.compare(arguments)

    def test_views_name_what_their_ids_point_at(self):
        made = self.build()
        listed = {task['id']: task for task in self.ok('tasks_list', {'limit': 200, 'snooze_filter': 'all'})['tasks']}
        blocked = listed[made['blocked']['id']]
        self.assertEqual(blocked['project_name'], 'Global')
        self.assertEqual(blocked['assignees'], [{'id': self.agent, 'kind': 'agent', 'name': 'Scout'}])
        self.assertEqual(blocked['blockers'], [{'id': made['open_blocker']['id'], 'title': 'Sign contract', 'status': 'open'}])
        single = self.ok('tasks_get', {'task_id': made['blocked']['id']})['task']
        self.assertEqual({k: v for k, v in single.items() if k not in {'notes', 'notes_next_cursor', 'related_tasks',
                                                                       'related_next_cursor'} and not k.startswith('related')},
                         {k: v for k, v in blocked.items() if k not in {'notes', 'notes_next_cursor'}})

    def test_search_reaches_overview_lines_and_comments(self):
        made = self.build()
        def found(text):
            return {task['id'] for task in self.ok('tasks_list', {'search': text, 'snooze_filter': 'all', 'limit': 200})['tasks']}
        self.assertEqual(found('café'), {made['parent']['id']})
        self.assertEqual(found('CONTRACT'), {made['open_blocker']['id'], made['blocked']['id']})
        self.assertEqual(found('éclairs'), {made['email']['id']})
        self.assertEqual(found('STRASSE'), {made['commented']['id']})  # Unicode case folding: ß matches ss
        self.assertEqual(found('straße permit'), {made['commented']['id']})

    def test_tasks_get_includes_associated_records_or_says_why_not(self):
        task = self.create(title='Story')
        plain_read = self.ok('tasks_get', {'task_id': task['id']})['task']
        self.assertNotIn('associated', plain_read)
        # This host installs no Action or Signoff record domain: the read still answers, naming what is unavailable.
        included = self.ok('tasks_get', {'task_id': task['id'], 'include': ['actions', 'signoffs']})['task']
        self.assertEqual(included['associated'], {'actions': {'unavailable': 'operation_unavailable'},
                                                  'signoffs': {'unavailable': 'operation_unavailable'}})
        refused = self.invoke('tasks_get', {'task_id': task['id'], 'include': ['comments']})
        self.assertEqual(refused.error['code'], 'validation_error')

    def test_overview_rows_name_their_project(self):
        self.create(title='Held', worker_ids=[self.agent])
        overview = TaskOverview(NS(clock=self.tasks.clock, project_input=lambda ctx, tasks, uow: tasks), NS(authorize=lambda *a, **k: None))
        with self.db.read() as uow:
            holders = overview.holders(self.worker, {}, uow, None, uow.repository('principals').get(self.owner))['holders']
        self.assertEqual({task['project_name'] for group in holders for task in group['tasks']}, {'Global'})

    def test_page_reads_do_not_grow_with_rows(self):
        for n in range(30):
            self.create(title=f'Item {n}', tags=['bulk'], worker_ids=[self.agent])
        counted = []
        with self.db.read() as uow:
            uow.connection.set_trace_callback(counted.append)
            ctx = self.app.current_context(self.ctx, uow)
            configuration, _ = self.tasks.configuration(ctx, uow)
            small = self.tasks.list_tasks(ctx, {'limit': 2}, uow, configuration, timestamp(self.tasks.clock))
            few = len(counted)
            counted.clear()
            large = self.tasks.list_tasks(ctx, {'limit': 30}, uow, configuration, timestamp(self.tasks.clock))
            uow.connection.set_trace_callback(None)
        self.assertEqual((len(small['tasks']), len(large['tasks'])), (2, 30))
        self.assertLessEqual(len(counted), few + 2, 'a larger page should not add queries per row')


if __name__ == '__main__':
    unittest.main()
