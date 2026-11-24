"""A browser-action request: Ordubashy asks the owner to do something in one open tab, the owner answers Done or Could not do it.

The browser is a stand-in port that lists scripted tabs and counts how often it is asked; everything else is the real Signoff,
Task and attention-queue code over a real store."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError, Principal, new_id, plain
from qorqut_operations.signoffs import ABSENT, SignoffPorts
from qorqut_operations.signoffs.inputs import BROWSER_ACTION_ANSWERS, validate_form

import private_thread_runtime as chats
import test_signoff_sources as fixture
from qorqut.attention_queue import queue
from qorqut.domains.timeline_owner import ASKS, TimelineOwner
from qorqut.native_chat.user_input import UserInput
from qorqut.signoffs.ports import HostTabs, SignoffDomains

AGENT = "4f6c1a2e-8d35-4b97-a1c0-2e9d7f3b5a64"
TAB, OTHER = f"{AGENT}:{'A' * 32}", f"{AGENT}:{'B' * 32}"
ACTION = {'title': 'Sign in to Mercury', 'explanation': 'Sign in with your usual account.', 'kind': 'browser_action',
          'selection_mode': 'none', 'tab': TAB}


class Tabs:
    """The browser's open tabs. ``check`` and ``view`` count their calls, so a test can see who asked the browser."""

    def __init__(self):
        self.open = {TAB: {'title': 'Mercury sign in', 'url': 'https://mercury.example/login'}}
        self.checks = self.views = 0

    def check(self, application, context, tab_id, uow):
        self.checks += 1
        if tab_id not in self.open:
            raise DomainError('not_found', 'This tab is not open')
        return dict(self.open[tab_id])

    def view(self, tab_id, uow):
        self.views += 1
        tab = self.open.get(tab_id)
        return {'id': tab_id, 'agent_id': tab_id.split(':')[0], 'tab_id': tab_id.split(':')[1], 'available': tab is not None,
                'label': tab['title'] if tab else None, 'agent_label': 'Ordubashy' if tab else None}


class Management:
    """A management domain whose routing would hand every request to the coordinator, so only the kind can refuse it."""

    managed_input_sql = '0'

    def __init__(self, test):
        self.test = test

    def coordinator(self, uow):
        return self.test.coordinator_id

    def auto_route_input(self, context, row, uow):
        return None

    def routing_for(self, uow, signoff_id):
        return {'coordinator_id': self.test.coordinator_id, 'attention_destination': 'management', 'management_allowed': 1}


class BrowserActionTests(unittest.TestCase):
    invoke = fixture.SignoffSourceTests.invoke
    task = fixture.SignoffSourceTests.task

    def setUp(self):
        self.tabs, self.coordinator_id = Tabs(), None
        ports = lambda **values: SignoffPorts(**{**values, 'tabs': self.tabs, 'management': Management(self)})
        with mock.patch.object(fixture, 'SignoffPorts', ports):
            fixture.SignoffSourceTests.setUp(self)
        self.ordubashy = self.agent('ordubashy')
        self.coordinator_id = self.ordubashy.principal_id
        self.policy.context_for = lambda principal_id, project_id=None, review_project_id=None, **_: replace(
            self.ordubashy if principal_id == self.coordinator_id else self.ctx, project_id=project_id, review_project_id=review_project_id)

    def agent(self, role):
        identity = new_id()
        profile = {'status': 'active', 'role': {'id': role}, 'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': role})
            uow.repository('agent_profiles', project_id=None).insert({'agent_id': identity, 'principal_id': identity,
                'name': role, 'workspace': role, 'profile_json': profile})
        return replace(self.ctx, principal=Principal(identity, 'agent'), source='mcp', profile_revision=1)

    def post(self, ctx=None, key='sign-in-mercury', form=None, **extra):
        return self.app.invoke(ctx or self.ordubashy, 'signoffs_input_create', {'scope': 'global', 'request_key': key,
            'form': {**ACTION, **(form or {})}, **extra}, {'idempotency_key': new_id()})

    def posted(self, ctx=None, **arguments):
        result = self.post(ctx, **arguments)
        self.assertEqual(result.status, 'ok', result.error)
        return plain(result.data)

    def refused(self, result, code):
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.error['code'], code, result.error)
        return result.error

    def respond(self, signoff, **response):
        return self.signoffs.respond(self.ctx, signoff['id'], proposal_revision=signoff['proposal_revision'],
            payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'],
            idempotency_key=new_id(), **response)

    def get(self, signoff):
        return self.invoke('signoffs_get', {'signoff_id': signoff['id']})['signoff']

    # -- the form ---------------------------------------------------------------------------------------------------

    def test_the_stored_form_has_the_fixed_answers_and_is_the_owners_alone(self):
        created = self.posted()
        signoff = created['signoff']
        self.assertEqual((signoff['category'], signoff['input_kind'], signoff['state']), ('input_request', 'browser_action', 'pending'))
        self.assertEqual(signoff['input'], {**ACTION, 'selection_mode': 'single', 'selection_required': True, 'notes_required': False,
            'response_policy': 'owner_only', 'choices': [
                {'id': 'done', 'label': 'Done', 'explanation': 'The owner did what the page needed.'},
                {'id': 'not_done', 'label': 'Could not do it', 'explanation': 'The owner could not do it; the note may say why.'}]})
        self.assertEqual(signoff['input']['choices'], BROWSER_ACTION_ANSWERS['choices'])
        # Sent again under the same key it is the same request, not a conflict.
        again = self.posted()
        self.assertEqual(again['signoff']['id'], signoff['id'])
        self.assertEqual(again['signoff']['input'], signoff['input'])
        # A different tab under the same key is a different question.
        self.tabs.open[OTHER] = {'title': 'Other', 'url': 'https://other.example/'}
        self.refused(self.post(form={'tab': OTHER}), 'request_conflict')

    def test_the_tab_is_required_for_it_and_refused_for_every_other_kind(self):
        without = {key: value for key, value in ACTION.items() if key != 'tab'}
        with self.assertRaises(DomainError) as raised:
            validate_form(without)
        self.assertEqual(raised.exception.code, 'validation_error')
        self.refused(self.app.invoke(self.ordubashy, 'signoffs_input_create', {'scope': 'global', 'request_key': 'no-tab',
            'form': without}, {'idempotency_key': new_id()}), 'validation_error')
        for kind in ('question', 'request'):
            self.refused(self.post(key=kind, form={'kind': kind}), 'validation_error')
        self.refused(self.post(key='review', form={'kind': 'draft_review', 'draft_fields': [{'id': 'a', 'label': 'A', 'value': 'x'}]}),
                     'validation_error')
        self.refused(self.post(key='report', form={'kind': 'acknowledge'}), 'validation_error')
        for tab in (AGENT, f"{AGENT}:{'a' * 32}", f"{AGENT}:{'A' * 31}", f"agent:{'A' * 32}", f"{AGENT.upper()}:{'A' * 32}"):
            with self.subTest(tab=tab):
                self.refused(self.post(key='bad', form={'tab': tab}), 'validation_error')
        self.assertEqual(self.tabs.checks, 0)

    def test_choices_required_notes_and_draft_fields_are_refused(self):
        choices = [{'id': 'a', 'label': 'Yes', 'explanation': 'Yes'}, {'id': 'b', 'label': 'No', 'explanation': 'No'}]
        for form in ({'selection_mode': 'single', 'choices': choices}, {'choices': choices}, {'selection_required': True},
                     {'notes_required': True}, {'draft_fields': [{'id': 'a', 'label': 'A', 'value': 'x'}]},
                     {'previous_review_id': new_id()}):
            with self.subTest(form=form):
                self.refused(self.post(form=form), 'validation_error')
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM signoffs').fetchone()[0], 0)

    # -- who files it, and the tab ----------------------------------------------------------------------------------

    def test_only_ordubashy_files_one(self):
        for ctx in (self.agent('global-worker'), self.ctx):
            error = self.refused(self.post(ctx), 'forbidden')
            self.assertIn('give Ordubashy the tab reference', error['message'])
        self.assertEqual(self.tabs.checks, 0)

    def test_without_the_browser_it_is_unavailable(self):
        self.signoffs.ports = replace(self.signoffs.ports, tabs=ABSENT)
        self.refused(self.post(), 'operation_unavailable')

    def test_a_closed_tab_is_refused_and_nothing_is_filed(self):
        del self.tabs.open[TAB]
        self.refused(self.post(), 'not_found')
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM signoffs').fetchone()[0], 0)

    def test_the_result_tells_the_agent_which_page_the_tab_is(self):
        created = self.posted()
        self.assertEqual(created['tab'], {'id': TAB, 'title': 'Mercury sign in', 'url': 'https://mercury.example/login'})
        self.assertEqual(self.tabs.checks, 2)           # when it is prepared and again where it is written

    # -- what readers get -------------------------------------------------------------------------------------------

    def test_a_detail_read_carries_the_tab_and_a_list_row_never_asks_the_browser(self):
        signoff = self.posted()['signoff']
        self.assertEqual(signoff['tab'], {'id': TAB, 'agent_id': AGENT, 'tab_id': 'A' * 32, 'available': True,
                                          'label': 'Mercury sign in', 'agent_label': 'Ordubashy'})
        before = self.tabs.views
        listing = self.invoke('signoffs_list', {'states': ['pending']})
        self.assertEqual([(row['input_kind'], 'tab' in row) for row in listing['items']], [('browser_action', False)])
        self.signoffs.notifications.overview(self.ctx)
        self.assertEqual(self.tabs.views, before)
        del self.tabs.open[TAB]
        self.assertEqual(self.get(signoff)['tab'], {'id': TAB, 'agent_id': AGENT, 'tab_id': 'A' * 32, 'available': False,
                                                    'label': None, 'agent_label': None})
        self.assertEqual(self.tabs.views, before + 1)
        # Without the browser the stored request stays readable; it simply has no tab to show.
        self.signoffs.ports = replace(self.signoffs.ports, tabs=ABSENT)
        current = self.get(signoff)
        self.assertNotIn('tab', current)
        self.assertEqual(current['input']['tab'], TAB)

    # -- the owner's answer -----------------------------------------------------------------------------------------

    def test_the_owner_answers_done_or_could_not_do_it_through_the_ordinary_response(self):
        for key, choice, notes in (('one', 'done', ''), ('two', 'not_done', 'The code never came.')):
            with self.subTest(choice=choice):
                signoff = self.posted(key=key)['signoff']
                for wrong in (['yes'], ['done', 'not_done']):
                    self.refused(self.respond(signoff, selected_choices=wrong), 'validation_error')
                before = self.tabs.views
                result = self.respond(signoff, selected_choices=[choice], notes=notes)
                self.assertEqual(result.status, 'ok', result.error)
                answered = plain(result.data)['signoff']
                self.assertEqual((answered['state'], answered['decision_state']), ('answered', 'answered'))
                self.assertEqual(answered['decision']['selected_choices'], [choice])
                # A decided request has no tab to open: neither the answer nor a later read asks the browser.
                self.assertNotIn('tab', answered)
                self.assertNotIn('tab', self.get(signoff))
                self.assertEqual(self.tabs.views, before)
                # What the proposer reads back, as for any single-choice question: the choice, and the note when there is one.
                read = self.invoke('signoffs_get', {'signoff_id': signoff['id']})['signoff']['decision']
                for decision in (answered['decision'], read):
                    self.assertEqual((decision['selected_choices'], decision.get('notes', '')), ([choice], notes))

    def test_a_linked_task_needs_input_until_it_is_answered_or_withdrawn(self):
        first = self.posted(key='one', source=self.source)
        self.assertIn('link', first)
        task = self.task()
        self.assertEqual((task['status'], task['work_status']), ('needs_input', 'working'))
        self.assertEqual([row['id'] for row in task['input_requests']], [first['signoff']['id']])
        self.assertEqual(self.respond(first['signoff'], selected_choices=['done']).status, 'ok')
        self.assertEqual(self.task()['status'], 'working')
        second = self.posted(key='two', source=self.source)
        self.assertTrue(self.task()['input_needed'])
        withdrawn = self.app.invoke(self.ordubashy, 'signoffs_input_cancel', {'scope': 'global', 'signoff_id': second['signoff']['id']},
                                    {'idempotency_key': new_id()})
        self.assertEqual(withdrawn.status, 'ok', withdrawn.error)
        self.assertFalse(self.task()['input_needed'])
        self.assertEqual(self.task()['status'], 'working')

    def test_urgent_and_blocking_are_allowed_as_for_a_question(self):
        signoff = self.posted(attention={'notify': True, 'urgent': True, 'blocking': True, 'reason': 'The payout run waits'})['signoff']
        self.assertEqual((signoff['attention']['urgent'], signoff['attention']['blocking']), (1, 1))

    def test_management_cannot_answer_it_for_the_owner(self):
        signoff = self.posted()['signoff']
        result = self.app.invoke(self.ordubashy, 'signoffs_input_answer_delegated', {'scope': 'global', 'signoff_id': signoff['id'],
            'proposal_revision': signoff['proposal_revision'], 'payload_hash': signoff['payload_hash'],
            'expected_status_revision': signoff['status_revision'], 'selected_choices': ['done']}, {'idempotency_key': new_id()})
        self.refused(result, 'forbidden')
        self.assertEqual(self.get(signoff)['state'], 'pending')

    def test_it_is_one_of_the_open_asks_on_its_task(self):
        signoff = self.posted(source=self.source)['signoff']
        self.assertIn("'browser_action'", ASKS)
        with self.db.read() as uow:
            asks = TimelineOwner.asks({'type': 'task', 'id': 'work', 'project_id': None}, uow)
        self.assertEqual([(row['signoff_id'], row['text']) for row in asks], [(signoff['id'], 'Sign in to Mercury')])

    def test_the_host_port_reads_the_tab_as_chat_does_and_its_view_never_raises(self):
        agent = self.coordinator_id
        tab_id = f"{agent}:{'C' * 32}"
        listed = {'C' * 32: {'id': 'C' * 32, 'title': '  Mercury\n sign in ', 'url': 'https://mercury.example/login?next=/pay'}}
        asked = []
        def read(ctx, identity, uow):
            asked.append((ctx.project_id, identity))
            if identity != tab_id:
                raise DomainError('not_found', 'Tab is not open')
            return {'agent': {'display_name': 'Ordubashy'}, 'title': 'Mercury', 'url': 'https://mercury.example/login'}
        user_input = NS(tab=read, _open_tabs=lambda identity: listed if identity == agent else None, tab_label=UserInput.tab_label)
        port = HostTabs(SignoffDomains().bind(user_input=user_input))
        application = NS(current_context=lambda ctx, uow: ctx)
        with self.db.read() as uow:
            scoped = replace(self.ordubashy, project_id=self.project, review_project_id=self.project)
            self.assertEqual(port.check(application, scoped, tab_id, uow), {'title': 'Mercury', 'url': 'https://mercury.example/login'})
            self.assertEqual(asked, [(None, tab_id)])            # a tab is Global, as in Chat
            with self.assertRaises(DomainError) as closed:
                port.check(application, self.ordubashy, TAB, uow)
            self.assertEqual(closed.exception.code, 'not_found')
            self.assertIn('tab-reference', closed.exception.message)
            self.assertEqual(port.view(tab_id, uow), {'id': tab_id, 'agent_id': agent, 'tab_id': 'C' * 32, 'available': True,
                                                      'label': 'Mercury sign in', 'agent_label': 'ordubashy'})
            for gone in (f"{agent}:{'D' * 32}", TAB):          # a closed tab; a browser that does not answer
                self.assertEqual(port.view(gone, uow), {'id': gone, 'agent_id': gone.split(':')[0], 'tab_id': gone.split(':')[1],
                                                        'available': False, 'label': None, 'agent_label': None})


class AttentionQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tabs = Tabs()
        ports = lambda **values: SignoffPorts(**{**values, 'tabs': self.tabs})
        with mock.patch.object(chats, 'SignoffPorts', ports):
            self.runtime = chats.build_runtime(Path(directory.name))
        self.addCleanup(self.runtime.database.close)

    def test_the_queue_row_asks_for_a_browser_action_without_asking_the_browser(self):
        runtime = self.runtime
        signoff = chats.invoke(runtime, 'signoffs_input_create', {'request_key': 'sign-in', 'form': ACTION}, ctx=runtime.coordinator)['signoff']
        views = self.tabs.views
        row = next(item for item in queue(runtime, runtime.owner)['items'] if item.get('signoff_id') == signoff['id'])
        self.assertEqual((row['kind'], row['asks'], row['title']), ('signoff', 'browser_action', 'Sign in to Mercury'))
        self.assertNotIn('to_read', row)
        self.assertEqual(self.tabs.views, views)


if __name__ == '__main__':
    unittest.main()
