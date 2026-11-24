"""A discarded calendar event leaves the owner's attention queue for good."""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from qorqut_operations.contracts import DomainError

from qorqut.attention_queue import queue
from private_thread_runtime import build_runtime


class CalendarDiscardTests(unittest.TestCase):
    """A discarded calendar event leaves the strip for good; the server keeps only recent keys and trusts no provider."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = build_runtime(Path(directory.name))
        self.addCleanup(self.runtime.database.close)

    @staticmethod
    def key(days_ago=0, event='event'):
        day = (date.today() - timedelta(days=days_ago)).isoformat()
        return f'calendar:{day}:' + json.dumps(['conn', 'cal', event, day + 'T09:00:00Z'])

    def store(self):
        owner = self.runtime.owner.principal_id
        return self.runtime.paths.runtime / 'dashboard-preferences' / (hashlib.sha256(owner.encode()).hexdigest() + '.calendar-discarded.json')

    def test_discard_is_saved_returned_and_repeatable(self):
        runtime, first, second = self.runtime, self.key(0, 'a'), self.key(3, 'b')
        assert queue(runtime, runtime.owner)['discarded'] == []
        assert queue(runtime, runtime.owner, {'discarded': [first]})['discarded'] == [first]
        assert queue(runtime, runtime.owner, {'discarded': [first, second]})['discarded'] == sorted([first, second])
        assert queue(runtime, runtime.owner)['discarded'] == sorted([first, second])

    def test_discard_and_viewed_travel_together(self):
        runtime, key = self.runtime, self.key()
        result = queue(runtime, runtime.owner, {'viewed': ['reminder:r:o:1'], 'discarded': [key]})
        assert result['discarded'] == [key]

    def test_malformed_changes_are_refused(self):
        runtime = self.runtime
        for change in ({}, {'other': []}, {'discarded': 'x'}, {'discarded': [5]}, {'discarded': ['signoff:1:2:3']},
                       {'discarded': [self.key(40)]}, {'discarded': ['calendar:soon:x']}, {'discarded': [self.key()] * 201},
                       {'discarded': ['calendar:2026-01-01:' + 'x' * 500]}):
            with self.subTest(change=change):
                with self.assertRaises(DomainError) as caught:
                    queue(runtime, runtime.owner, change)
                assert caught.exception.code == 'validation_error'
        assert not self.store().exists() or json.loads(self.store().read_text()) == []

    def test_entries_past_the_retention_window_are_dropped(self):
        runtime, fresh = self.runtime, self.key(2)
        queue(runtime, runtime.owner, {'discarded': [fresh]})
        old = self.key(45, 'old')
        self.store().write_text(json.dumps(sorted([fresh, old])))
        assert queue(runtime, runtime.owner)['discarded'] == [fresh]
        assert json.loads(self.store().read_text()) == [fresh]

    def test_only_the_owner_reads_or_changes_discards(self):
        runtime = self.runtime
        with self.assertRaises(DomainError):
            queue(runtime, runtime.coordinator, {'discarded': [self.key()]})
        assert not self.store().exists()
