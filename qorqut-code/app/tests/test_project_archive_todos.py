"""An approved project archive takes the project's Tasks off the Todo boards in the same write."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut_operations.projects import archive
from qorqut_operations.projects.archive import ProjectArchive


class ArchiveTodoTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.project = {'id': 'project', 'name': 'Example project', 'slug': 'example', 'revision': 2}
        self.uow = NS(repository=lambda *_a, **_k: NS(get=lambda *_a, **_k: self.project),
                      connection=NS(execute=lambda *_a: []))
        self.ctx = NS(principal_id='owner')
        self.plan = NS(arguments={'project_id': 'project'})
        patch.object(ProjectArchive, 'guard').start()
        patch.object(archive, 'apply_records', side_effect=lambda *_: self.calls.append('archived')).start()
        self.addCleanup(patch.stopall)

    def test_boards_are_released_after_the_project_is_archived(self):
        service = ProjectArchive(NS(), reset_terminals=lambda *_: self.calls.append('terminals'),
                                  release_todos=lambda *arguments: self.calls.append(('todos', *arguments)))
        result = service.apply(self.ctx, self.plan, self.uow)
        self.assertEqual(self.calls, ['terminals', 'archived', ('todos', self.uow, 'project', 'owner')])
        self.assertEqual(result, {'project_id': 'project', 'archived': True, 'revision': 2})

    def test_host_without_todo_boards_still_archives(self):
        service = ProjectArchive(NS(), reset_terminals=Mock())
        self.assertEqual(service.apply(self.ctx, self.plan, self.uow)['archived'], True)
        self.assertEqual(self.calls, ['archived'])


if __name__ == '__main__':
    unittest.main()
