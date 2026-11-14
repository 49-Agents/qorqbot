import tempfile
import unittest

from qorqut.dashboard_preferences import MAX_THREAD_PINS, thread_pins
from qorqut_operations.contracts import DomainError

TASK = {'type': 'task', 'id': 'task-1', 'business_id': 'project-1'}
SIGNOFF = {'type': 'signoff', 'id': 'signoff-1', 'business_id': None}


class ThreadPinsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def call(self, change=None, owner='owner'):
        return thread_pins(self.directory.name, owner, change)

    def test_pins_keep_order_and_retries_set_the_same_state(self):
        self.assertEqual(self.call(), {'threads': []})
        self.call({'source': TASK, 'title': ' Launch plan ', 'pinned': True})
        self.call({'source': SIGNOFF, 'title': 'Invoice', 'pinned': True})
        self.call({'source': TASK, 'title': 'Launch plan', 'pinned': True})
        self.assertEqual(self.call()['threads'], [
            {'source': SIGNOFF, 'title': 'Invoice'}, {'source': TASK, 'title': 'Launch plan'}])
        self.call({'source': SIGNOFF, 'title': 'Invoice', 'pinned': False})
        self.call({'source': SIGNOFF, 'title': 'Invoice', 'pinned': False})
        self.assertEqual(self.call()['threads'], [{'source': TASK, 'title': 'Launch plan'}])

    def test_each_owner_has_separate_pins(self):
        self.call({'source': TASK, 'title': 'Launch plan', 'pinned': True})
        self.assertEqual(self.call(owner='other'), {'threads': []})

    def test_invalid_changes_write_nothing(self):
        for change in ({'source': TASK, 'pinned': True}, {'source': {**TASK, 'extra': 1}, 'title': '', 'pinned': True},
                       {'source': {**TASK, 'type': 'Task'}, 'title': '', 'pinned': True},
                       {'source': TASK, 'title': 'x' * 201, 'pinned': True}, {'source': TASK, 'title': '', 'pinned': 1}):
            with self.assertRaises(DomainError) as raised:
                self.call(change)
            self.assertEqual(raised.exception.code, 'validation_error')
        self.assertEqual(self.call(), {'threads': []})

    def test_the_number_of_pins_is_bounded(self):
        for index in range(MAX_THREAD_PINS):
            self.call({'source': {**TASK, 'id': f'task-{index}'}, 'title': '', 'pinned': True})
        with self.assertRaises(DomainError):
            self.call({'source': {**TASK, 'id': 'one-more'}, 'title': '', 'pinned': True})
        self.assertEqual(len(self.call()['threads']), MAX_THREAD_PINS)


if __name__ == '__main__':
    unittest.main()
