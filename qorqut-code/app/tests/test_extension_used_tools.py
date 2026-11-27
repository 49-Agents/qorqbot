"""What an agent used in its current root turn, for the ``if_used``/``unless_used`` conditions of extension hooks, and which
submitted prompts are the owner's own messages. Synthetic hook input for both providers; nothing runs a client."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut.agents import checkpoints
from qorqut.bootstrap import activity_hook as hook
from qorqut.managed_runtime import clients
from qorqut.native_chat import contracts, projection

from test_preparation_modes import Fixture as PreparationFixture

WATCHED = ['crm_entries_update', 'network_follow_up_set']


class UsedTools(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.registration = {'schema_version': 1, 'workspace': str(self.workspace),
            'registration_id': str(uuid4()), 'agent_id': str(uuid4()),
            'service_url': 'http://127.0.0.1:1', 'credential_file': '.qorqut/credential',
            'checkpoint_protocol': {'claude-code': 1, 'codex': 1}, 'read_operations': [],
            'extension_watched_tools': WATCHED}
        self.path = self.workspace / '.qorqut/registration.json'
        self.path.parent.mkdir()
        self.write_registration()
        self.session = str(uuid4())
        self.activity = self.workspace / '.qorqut/activity' / (self.session + '.json')
        self.used_path = self.activity.with_suffix('.used.json')
        policy = NS(activity_reminder_due=lambda **_: False, from_prepared=lambda _: None,
                    completion_wrapper=lambda _, text: text)
        self.addCleanup(patch.stopall)
        patch.object(hook, 'session_owner', return_value='123:456').start()
        patch.object(hook, 'agentic_policy', return_value=policy).start()

    def write_registration(self):
        self.path.write_text(json.dumps(self.registration))

    def send(self, runtime, event, turn=None, **fields):
        value = {'hook_event_name': event, 'session_id': self.session, 'cwd': str(self.workspace), **fields}
        if turn is not None:
            value['turn_id'] = turn
        return hook.record(value, self.path, runtime)

    def prompt(self, runtime, turn=None, text='Please update the CRM.'):
        self.send(runtime, 'UserPromptSubmit', turn, prompt=text)

    def tool(self, runtime, name, turn=None, tool_input=None):
        self.send(runtime, 'PreToolUse', turn, tool_name=name, tool_input=tool_input or {}, tool_use_id=str(uuid4()))

    def used(self):
        return hook.read_used(self.used_path)

    def root(self):
        return hook.read_activity(self.activity)['checkpoint']['root_turn_id']

    def continuation(self, text):
        """The receipt a sent checkpoint correction leaves for the root turn, as checkpoint_stop writes it."""
        hook.write_state(self.activity.with_suffix('.checkpoint.json'), {
            'registration_id': self.registration['registration_id'], 'session_id': self.session,
            'root_turn_id': self.root(), 'turn_id': self.root(), 'claim_id': str(uuid4()),
            'continuation': text, 'continuation_sha256': hashlib.sha256(text.encode()).hexdigest()})

    def test_codex_native_turns_keep_watched_names_and_reset_on_a_new_turn(self):
        first = str(uuid4())
        self.prompt('codex', first)
        self.tool('codex', 'mcp__qorqut__crm_entries_update', first)
        self.tool('codex', 'mcp__qorqut__tasks_get', first)
        self.tool('codex', 'mcp__qorqut__crm_entries_update', first)
        self.assertEqual(self.used(), {'root_turn_id': first, 'used': ['crm_entries_update']})
        self.tool('codex', 'mcp__qorqut__network_follow_up_set', str(uuid4()))  # not the current turn
        self.assertEqual(self.used()['used'], ['crm_entries_update'])
        second = str(uuid4())
        self.prompt('codex', second)
        self.tool('codex', 'mcp__qorqut__network_follow_up_set', second)
        self.assertEqual(self.used(), {'root_turn_id': second, 'used': ['network_follow_up_set']})

    def test_claude_minted_turns_keep_watched_names_and_reset_on_a_new_turn(self):
        self.prompt('claude-code')
        first = self.root()
        self.tool('claude-code', 'mcp__qorqut__crm_entries_update')
        self.assertEqual(self.used(), {'root_turn_id': first, 'used': ['crm_entries_update']})
        self.prompt('claude-code')
        second = self.root()
        self.assertNotEqual(first, second)
        self.tool('claude-code', 'mcp__qorqut__network_follow_up_set')
        self.assertEqual(self.used(), {'root_turn_id': second, 'used': ['network_follow_up_set']})
        # A turn a notification starts after a finished Stop gets its own minted root turn.
        self.send('claude-code', 'Stop', stop_hook_active=False)
        self.tool('claude-code', 'mcp__qorqut__crm_entries_update')
        self.assertNotEqual(self.root(), second)
        self.assertEqual(self.used(), {'root_turn_id': self.root(), 'used': ['crm_entries_update']})

    def test_batch_entries_are_expanded(self):
        for runtime, turn in (('codex', str(uuid4())), ('claude-code', None)):
            with self.subTest(runtime=runtime):
                self.prompt(runtime, turn)
                self.tool(runtime, 'mcp__qorqut__operations_batch', turn, {'operations': [
                    {'operation': 'tasks_get', 'arguments': {}},
                    {'operation': 'network_follow_up_set', 'arguments': {}},
                    {'operation': 'crm_entries_update', 'arguments': {}}]})
                self.assertEqual(self.used()['used'], ['network_follow_up_set', 'crm_entries_update'])

    def test_unwatched_names_and_other_events_are_ignored(self):
        turn = str(uuid4())
        self.prompt('codex', turn)
        for name in ('mcp__qorqut__tasks_get', 'qorqut.tasks_get', 'Bash', 'Read'):
            self.tool('codex', name, turn)
        self.send('codex', 'PostToolUse', turn, tool_name='mcp__qorqut__crm_entries_update', tool_use_id='call-1')
        self.assertEqual(self.used(), {'root_turn_id': turn, 'used': []})

    def test_names_are_read_as_the_dirty_counter_reads_them(self):
        """Joined by ``__`` or by ``.``: the same normalisation as ``substantive``, so either form a client reports counts."""
        for name in ('mcp__qorqut__crm_entries_update', 'qorqut.crm_entries_update', 'crm_entries_update'):
            with self.subTest(name=name):
                turn = str(uuid4())
                self.prompt('codex', turn)
                self.tool('codex', name, turn)
                self.assertEqual(self.used()['used'], ['crm_entries_update'])
                self.assertEqual(hook.operation_name(name), 'crm_entries_update')
        turn = str(uuid4())
        self.prompt('codex', turn)
        self.tool('codex', 'qorqut.operations_batch', turn, {'operations': [{'operation': 'network_follow_up_set'}]})
        self.assertEqual(self.used()['used'], ['network_follow_up_set'])

    def test_a_continuation_after_a_correction_keeps_the_list(self):
        correction = 'Qorqut completion checkpoint: record the work.'
        for runtime in ('codex', 'claude-code'):
            with self.subTest(runtime=runtime):
                first = str(uuid4()) if runtime == 'codex' else None
                self.prompt(runtime, first)
                root = self.root()
                self.tool(runtime, 'mcp__qorqut__crm_entries_update', first)
                self.continuation(correction)
                continued = str(uuid4()) if runtime == 'codex' else None
                self.prompt(runtime, continued, correction)
                self.assertEqual(self.root(), root)
                self.tool(runtime, 'mcp__qorqut__network_follow_up_set', continued)
                self.assertEqual(self.used(), {'root_turn_id': root,
                                               'used': ['crm_entries_update', 'network_follow_up_set']})
                self.prompt(runtime, str(uuid4()) if runtime == 'codex' else None)
                self.assertNotEqual(self.root(), root)

    def test_no_watched_tools_writes_nothing_and_leaves_the_activity_as_before(self):
        turn = str(uuid4())
        def activity(watched):
            if watched is None:
                self.registration.pop('extension_watched_tools', None)
            else:
                self.registration['extension_watched_tools'] = watched
            self.write_registration()
            for name in self.activity.parent.glob('*') if self.activity.parent.exists() else ():
                name.unlink()
            self.prompt('codex', turn)
            self.send('codex', 'PreToolUse', turn, tool_name='mcp__qorqut__crm_entries_update', tool_input={},
                      tool_use_id='call-1')
            return hook.read_activity(self.activity)['checkpoint']
        without = activity(None)
        self.assertFalse(self.used_path.exists())
        self.assertEqual(activity([]), without)
        self.assertFalse(self.used_path.exists())
        self.assertEqual(activity(WATCHED), without)
        self.assertTrue(self.used_path.exists())

    def native(self):
        @contextmanager
        def read():
            yield NS(connection=NS(execute=lambda *_: NS(fetchone=lambda: (1,))))
        runtime = NS(database=NS(read=read), paths=None)
        with patch.object(checkpoints, 'agent_workspace_of', return_value=self.workspace):
            return checkpoints.native_checkpoint(runtime, self.registration['agent_id'], self.session,
                                                 self.registration['registration_id'])

    def test_native_checkpoint_returns_the_used_names_of_the_current_root_turn_only(self):
        for runtime in ('codex', 'claude-code'):
            with self.subTest(runtime=runtime):
                turn = str(uuid4()) if runtime == 'codex' else None
                self.prompt(runtime, turn)
                self.assertEqual(self.native()['used'], [])
                self.tool(runtime, 'mcp__qorqut__crm_entries_update', turn)
                native = self.native()
                self.assertEqual(native['used'], ['crm_entries_update'])
                self.assertEqual(native['root_turn_id'], self.root())
                self.prompt(runtime, str(uuid4()) if runtime == 'codex' else None)
                self.assertEqual(self.native()['used'], [])  # the new root turn starts a new list
                hook.write_state(self.used_path, {'root_turn_id': str(uuid4()), 'used': ['crm_entries_update'],
                                                  'owner_turn': True})
                self.assertEqual((self.native()['used'], self.native()['owner_turn']), ([], False))  # another root turn

    def test_owner_turn_is_kept_per_root_turn_when_asked(self):
        correction = 'Qorqut completion checkpoint: record the work.'
        nudge = projection.marker_text('nudge-12345678', 'inbox_nudge', 'You have 2 inbox events.')
        self.registration.update(extension_watched_tools=[], extension_owner_turn=True)
        self.write_registration()
        for runtime in ('codex', 'claude-code'):
            def turn():
                return str(uuid4()) if runtime == 'codex' else None
            for text, owner in (('I had lunch with Sam Lee yesterday.', True), (nudge, False), ('/role', False)):
                with self.subTest(runtime=runtime, text=text[:20]):
                    self.prompt(runtime, turn(), text)
                    root = self.root()
                    self.assertEqual(self.used(), {'root_turn_id': root, 'used': [], 'owner_turn': owner})
                    self.assertIs(self.native()['owner_turn'], owner)
                    # A continuation after a correction keeps how the root turn began.
                    self.continuation(correction)
                    self.prompt(runtime, turn(), correction)
                    self.assertEqual(self.root(), root)
                    self.assertIs(self.native()['owner_turn'], owner)
        # A Claude turn a notification starts after a finished Stop began with no message.
        self.prompt('claude-code', None, 'I had lunch with Sam Lee yesterday.')
        self.send('claude-code', 'Stop', stop_hook_active=False)
        self.tool('claude-code', 'mcp__qorqut__tasks_get')
        self.assertEqual(self.used(), {'root_turn_id': self.root(), 'used': [], 'owner_turn': False})

    def test_owner_turn_is_not_kept_unless_asked(self):
        self.prompt('codex', str(uuid4()), 'I had lunch with Sam Lee yesterday.')
        self.assertNotIn('owner_turn', self.used())
        self.assertIs(self.native()['owner_turn'], False)


class OwnerPrompt(unittest.TestCase):
    def test_owner_messages_and_the_rest(self):
        correction = 'Qorqut completion checkpoint: record the work.'
        state = {'continuation_sha256': hashlib.sha256(correction.encode()).hexdigest()}
        owner = ['I had lunch with Sam Lee yesterday.', 'Use <b>bold</b> here.', '/model sonnet', 'a < b > c',
                 '/tmp/report.txt is ready', 'Qorqut completion checkpoint: something else.']
        other = [projection.marker_text('nudge-12345678', 'inbox_nudge', 'You have 2 inbox events.'),
                 '\n  ' + projection.marker_text('wake-12345678', 'bootstrap_recovery', 'Continue.'),
                 '<qorqut-handoff>\nThe earlier conversation is summarized below.\n</qorqut-handoff>',
                 '<qorqut-handoff-request>File the handoff.</qorqut-handoff-request>',
                 correction, '/role', '$role', '/model', '/clear', '/HELP', '', '   ', None]
        for text in owner:
            with self.subTest(text=text):
                self.assertTrue(hook.owner_prompt(text, state))
        for text in other:
            with self.subTest(text=text):
                self.assertFalse(hook.owner_prompt(text, state))
        self.assertFalse(hook.owner_prompt(correction, {'continuation': correction}))
        self.assertTrue(hook.owner_prompt(correction, {}))

    def test_an_owner_paste_is_an_owner_message_on_claude(self):
        paste = '<pasted_content id="1a2b">\nI had lunch with Sam Lee yesterday.\n</pasted_content id="1a2b">'
        self.assertTrue(hook.owner_prompt(paste, {}, runtime='claude-code'))
        self.assertFalse(hook.owner_prompt(paste, {}, runtime='codex'))  # Chat unwraps it for Claude only
        wrapped = '<pasted_content id="7">\n' + projection.marker_text('nudge-12345678', 'inbox_nudge', 'x') + '\n</pasted_content id="7">'
        self.assertFalse(hook.owner_prompt(wrapped, {}, runtime='claude-code'))

    def test_the_copied_rules_equal_the_application_rules(self):
        roles = {clients.role_command(client) for client in clients.CLIENTS}
        self.assertEqual(hook.MANAGED_MENU_COMMANDS, contracts.MANAGED_MENU_COMMANDS)
        self.assertEqual(hook.ROLE_COMMANDS, roles)

        def chat(text, runtime):
            """What Chat and the application say, from their own code: Chat's user-entry rule, then the role command and
            the menu-only commands."""
            if runtime == 'claude-code':
                text = projection.unwrap_claude_paste(text)
            if (not text.strip() or projection._INTERRUPT_RECORD.fullmatch(text.strip())
                    or projection.internal_notification(text)):
                return False
            return not (contracts.managed_menu_command(text) or text.split()[0].lower() in roles)

        bodies = ['plain words', 'a <b>tag</b> in the middle', 'ends with a tag <x>', '<x>', '<3 you', '< x>', '<x',
                  '<qorqut-handoff>\nThe earlier conversation.\n</qorqut-handoff>', '<command-name>/compact</command-name>',
                  '<system-reminder>x</system-reminder>', '<local-command-stdout>x</local-command-stdout>',
                  projection.marker_text('nudge-12345678', 'inbox_nudge', 'You have mail.'),
                  '[Request interrupted by user]', '[Request interrupted by user for tool use]',
                  'Say [Request interrupted by user]', '/role', '$role', '/ROLE', '/role now', '/model', '/model sonnet',
                  '/MODEL', '/clear', '/clear now', '/help me', '/status\n', '/compact', '/compact keep the plan',
                  '/tmp/report.txt', '/modelx', '', ' ', '\n']
        spaces = ['', ' ', '\n\t', '\x0b\x0c\r', '\u00a0', '\u2003', '\u3000', '\ufeff', '\u200b']
        texts = [space + body for space in spaces for body in bodies]
        texts += [f'<pasted_content id="{ident}">\n{body}\n</pasted_content id="{ident}">' for ident in ('1a', 'FF')
                  for body in bodies]
        texts += [' \n<pasted_content id="1a">\n<x>\n</pasted_content id="1a">\n ',
                  '<pasted_content id="1a">\nplain\n</pasted_content id="2b">',
                  '<pasted_content id="zz">\nplain\n</pasted_content id="zz">',
                  'before <pasted_content id="1a">\n<x>\n</pasted_content id="1a">',
                  '<pasted_content id="1a">\n/role\n</pasted_content id="1a">']
        for runtime in ('claude-code', 'codex'):
            for text in texts:
                with self.subTest(runtime=runtime, text=text):
                    self.assertEqual(hook.owner_prompt(text, {}, runtime=runtime), chat(text, runtime))


class RegistrationWatchedTools(PreparationFixture):
    def registration(self, terminal):
        return self.build(terminal).files['.qorqut/registration.json']

    def test_with_no_extension_in_effect_the_registration_is_as_before(self):
        from test_extension_hooks import FakeRegistry
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                before = self.registration(terminal)
                registration = json.loads(before)
                self.assertNotIn('extension_watched_tools', registration)
                self.assertNotIn('extension_owner_turn', registration)
                self.resolver.extensions = FakeRegistry({})
                try:
                    self.assertEqual(self.registration(terminal), before)
                finally:
                    del self.resolver.extensions

    def test_the_agents_entries_fill_what_the_activity_hook_notes(self):
        from test_extension_hooks import FakeRegistry
        body = (b"- id: check\n  at: stop\n  unless_used: [network_follow_up_set]\n  text: instructions/a.md\n"
                b"- id: after\n  at: after_tool\n  tools: [crm_entries_update]\n  if_used: [tasks_update]\n"
                b"  text: instructions/a.md\n")
        files = {'hooks.yaml': body, 'instructions/a.md': b'Check it.\n'}
        self.resolver.extensions = FakeRegistry({'network': files.get})
        registration = json.loads(self.registration(False))
        self.assertEqual(registration['extension_watched_tools'], ['network_follow_up_set', 'tasks_update'])
        self.assertIs(registration['extension_owner_turn'], True)
        files['hooks.yaml'] = body.split(b'- id: after')[0].replace(b'stop', b'owner_message').replace(
            b'  unless_used: [network_follow_up_set]\n', b'')
        self.resolver.extensions.marker += 1  # the layer copy changed
        registration = json.loads(self.registration(False))
        self.assertNotIn('extension_watched_tools', registration)
        self.assertNotIn('extension_owner_turn', registration)


if __name__ == '__main__':
    unittest.main()
