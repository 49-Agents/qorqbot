"""Paged compatibility reads retain full-list search, ordering and scope binding."""
import unittest
import json
from types import SimpleNamespace
from starlette.requests import Request
from qorqut.transport.compatibility import business_task_page, DashboardCompatibility
from qorqut_operations.contracts import DomainError


def task(identity, **fields):
    return dict(id=identity, title=identity, status='open', tags=[], notes=[], **fields)


class BusinessTaskPagesTest(unittest.TestCase):
    def test_pages_follow_due_order_and_search_notes_beyond_first_page(self):
        tasks = [task(str(i), due=f'2026-12-{i+1:02d}') for i in reversed(range(12))]
        tasks[0]['notes'] = [{'text': 'Rare phrase in the last task'}]
        first = business_task_page(tasks, {'limit': '3'}, 'a')
        self.assertEqual([row['id'] for row in first['tasks']], ['0', '1', '2'])
        second = business_task_page(tasks, {'limit': '3', 'cursor': first['next_cursor']}, 'a')
        self.assertEqual([row['id'] for row in second['tasks']], ['3', '4', '5'])
        found = business_task_page(tasks, {'search': 'RARE PHRASE', 'limit': '3'}, 'a')
        self.assertEqual([row['id'] for row in found['tasks']], ['11'])
        tasks[4]['tags'] = ['special-tag']
        self.assertEqual(len(business_task_page(tasks, {'search':'special-tag'}, 'a')['tasks']), 1)

    def test_cursor_is_bound_to_business_search_and_visibility(self):
        tasks = [task(str(i)) for i in range(3)]
        cursor = business_task_page(tasks, {'limit':'1'}, 'a')['next_cursor']
        for scope, query in [('b', {}), ('a', {'search':'x'}), ('a', {'visibility':'all'})]:
            with self.assertRaises(DomainError):
                business_task_page(tasks, {**query, 'cursor':cursor}, scope)

    def test_visibility_closed_and_expired_snoozes(self):
        tasks = [task('active'), task('later', snoozed_until='2099-01-01T00:00:00Z'),
                 task('expired', snoozed_until='2000-01-01T00:00:00Z'), task('closed')]
        tasks[-1]['status'] = 'done'
        self.assertEqual([r['id'] for r in business_task_page(tasks, {}, 'a')['tasks']], ['active','expired'])
        self.assertEqual([r['id'] for r in business_task_page(tasks, {'visibility':'snoozed'}, 'a')['tasks']], ['later'])
        self.assertEqual(len(business_task_page(tasks, {'visibility':'all'}, 'a')['tasks']), 4)

    def test_route_keeps_legacy_shape_and_paged_reads_disable_http_caching(self):
        adapter = DashboardCompatibility.__new__(DashboardCompatibility)
        adapter.runtime = SimpleNamespace(changes=None)
        adapter.collection = lambda *args, **kwargs: [task(str(i)) for i in range(3)]
        adapter.assignees = lambda context: []
        context = SimpleNamespace(business_id='a')
        def request(query):
            return Request({'type':'http', 'method':'GET', 'path':'/api/biz/a/tasks',
                            'query_string':query.encode(), 'headers':[]})
        legacy = adapter.business_get(request('all=1'), context, None, ['tasks'])
        self.assertIsInstance(json.loads(legacy.body), list)
        paged = adapter.business_get(request('all=1&paged=1&limit=1'), context, None, ['tasks'])
        self.assertEqual(len(json.loads(paged.body)['tasks']), 1)
        self.assertTrue(json.loads(paged.body)['next_cursor'])
        self.assertEqual(paged.headers['cache-control'], 'no-store')
        self.assertNotIn('etag', paged.headers)

    def test_invalid_limits_are_rejected(self):
        for value in ['0', '101', 'oops']:
            with self.assertRaises(DomainError):
                business_task_page([], {'limit':value}, 'a')

if __name__ == '__main__':
    unittest.main()
