"""Private owner–Ordubashy messages link records the way Signoff notes do: same vocabulary, checks and display."""
from pathlib import Path
import tempfile
import unittest

from qorqut_operations.contracts import InvocationOptions, new_id

from test_side_chat_rename import build_runtime, create, invoke, source


def approval(runtime, title='Approve the pricing page'):
    return invoke(runtime, 'signoffs_input_create', {'request_key': new_id(), 'form': {
        'title': title, 'explanation': 'Choose one', 'kind': 'question', 'selection_mode': 'none'}})['signoff']


def messages(runtime, chat, ctx=None):
    return invoke(runtime, 'owner_conversation_get', {'source': source(chat)}, ctx=ctx)['messages']


class PrivateMessageRefsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = build_runtime(Path(directory.name))
        self.addCleanup(self.runtime.database.close)

    def test_the_first_message_and_a_reply_carry_signoff_references_both_sides_read(self):
        runtime = self.runtime
        pricing = approval(runtime)
        chat = create(runtime, 'What is left on this one?', refs=[{'type': 'signoff', 'id': pricing['id']}])
        sent = invoke(runtime, 'owner_conversation_send', {'source': source(chat), 'text': 'This chat and the approval.',
            'refs': [{'type': 'signoff', 'id': chat['id']}, {'type': 'signoff', 'id': pricing['id']}]}, ctx=runtime.coordinator)
        self.assertEqual(sent['message']['refs'], [{'type': 'signoff', 'id': chat['id']}, {'type': 'signoff', 'id': pricing['id']}])
        for ctx in (runtime.owner, runtime.coordinator):
            first, answer = messages(runtime, chat, ctx)
            self.assertEqual(first['refs'], [{'type': 'signoff', 'id': pricing['id']}])
            self.assertEqual(answer['refs'], [{'type': 'signoff', 'id': chat['id']}, {'type': 'signoff', 'id': pricing['id']}])
            self.assertNotIn('refs_json', answer)

    def test_a_message_without_references_reads_an_empty_list(self):
        chat = create(self.runtime)
        self.assertEqual(messages(self.runtime, chat)[0]['refs'], [])

    def test_a_reference_the_sender_cannot_read_is_refused_and_nothing_is_sent(self):
        runtime = self.runtime
        chat = create(runtime)
        result = runtime.application.invoke(runtime.coordinator, 'owner_conversation_send', {'scope': 'global',
            'source': source(chat), 'text': 'See this', 'refs': [{'type': 'signoff', 'id': new_id()}]},
            InvocationOptions(idempotency_key=new_id()))
        self.assertEqual((result.status, result.error['code']), ('error', 'not_found'))
        self.assertEqual(len(messages(runtime, chat)), 1)
        refused = runtime.application.invoke(runtime.owner, 'side_chats_create', {'scope': 'global', 'text': 'About this',
            'refs': [{'type': 'signoff', 'id': new_id()}]}, InvocationOptions(idempotency_key=new_id()))
        self.assertEqual((refused.status, refused.error['code']), ('error', 'not_found'))

    def test_only_known_reference_types_are_accepted(self):
        runtime = self.runtime
        chat = create(runtime)
        result = runtime.application.invoke(runtime.coordinator, 'owner_conversation_send', {'scope': 'global',
            'source': source(chat), 'text': 'See this', 'refs': [{'type': 'calendar', 'id': new_id()}]},
            InvocationOptions(idempotency_key=new_id()))
        self.assertEqual((result.status, result.error['code']), ('error', 'validation_error'))


if __name__ == '__main__':
    unittest.main()
