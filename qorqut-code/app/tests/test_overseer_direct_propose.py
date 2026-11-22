"""A direct owner instruction proposed to the Overseer gets its source scope described by the shipped help text.

The ``instruction_text`` argument of ``propose`` is the owner's words; the help text lookup must not be shadowed by it
(it was, and every first proposal failed with "'str' object is not callable").
"""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut_agentic.behavior.tool_help import text as help_text
from qorqut_agentic.errors import NotFoundError

from qorqut.overseer import direct


class Stop(Exception):
    pass


class DirectProposeTest(unittest.TestCase):
    def test_a_first_proposal_describes_its_source_with_the_help_text_and_keeps_the_owners_words(self):
        @contextmanager
        def read():
            yield SimpleNamespace(connection=None)

        runtime = SimpleNamespace(database=SimpleNamespace(read=read), agentic_layer=None)
        row = {'id': 'input-1', 'request_hash': 'hash', 'conversation_id': 'conv', 'principal_id': 'owner',
               'created_at': '2026-11-22T16:00:00Z', 'agent_id': 'agent', 'provider': 'claude'}
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            raise Stop()

        def missing(*args):
            raise NotFoundError('none')

        ctx = SimpleNamespace(principal_id='agent-principal', allowed_project_ids=())
        scope = {'project_ids': [], 'work_types': [], 'workflow_refs': [], 'conditions': [], 'exceptions': []}
        with patch.object(direct.validation, 'scope'), \
                patch.object(direct, 'current_manager', return_value=(ctx, {})), \
                patch.object(direct, '_instruction', return_value=row), \
                patch.object(direct, 'active_role_holder', return_value='overseer'), \
                patch.object(direct, '_load', side_effect=missing), \
                patch.object(direct, 'source_ref', side_effect=capture):
            with self.assertRaises(Stop):
                direct.propose(runtime, SimpleNamespace(), request_key='key-1', instruction_text='Always answer in a Signoff.',
                               summary='Answer in Signoffs', change_direction='add', scope=scope, target_refs=[])
        self.assertEqual(seen['scope']['description'],
                         help_text('direct-explicit-owner-instruction-in-the-submitting-agent-s'))
        self.assertEqual(seen['content'], {'text': 'Always answer in a Signoff.'})


if __name__ == '__main__':
    unittest.main()
