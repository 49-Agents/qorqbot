"""The coordinator's reminder is an oversight pass: who is free, which delegated work went quiet, what he could hand over."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import qorqut_agentic
from qorqut.agents.attention import Attention
from qorqut_agentic.behavior import attention
from qorqut_agentic.behavior.instructions import from_catalog

CATALOG = Path(qorqut_agentic.__file__).parent / 'resources/defaults/operations/runtime-instructions'
NOW = '2026-11-18T12:00:00.000000Z'
TASK = {'id': 'own', 'business_id': None, 'title': 'Own task', 'status': 'open', 'workers': [],
        'workers_omitted': 0, 'user_needed': False}
FOCUS = {'type': 'task', 'id': 'task-1', 'business_id': None}


class OversightTextTests(unittest.TestCase):
    def setUp(self):
        self.instructions = from_catalog(lambda name: (CATALOG / name).read_text())

    def snapshot(self, team, focus=FOCUS):
        return {'focus': focus, 'focused_for': '2h 0m' if focus else None, 'work': TASK if focus else None,
                'next_step': 'Next', 'intent': '', 'candidates': [TASK], 'more': False, 'team': team}

    def test_the_coordinator_gets_the_oversight_pass_with_team_data(self):
        team = {'available': ['Harbor', 'Cedar'], 'stalled_count': 7, 'blocked_count': 1, 'blocked': [
            {'id': 'stuck', 'business_id': None, 'title': 'Stuck task', 'status': 'blocked', 'workers': ['Harbor'],
             'updated_at': '2026-11-17T08:00:00.000000Z'}], 'stalled': [
            {'id': 'held', 'business_id': 'project', 'title': 'Held task', 'status': 'working', 'workers': ['Scout'],
             'updated_at': '2026-11-18T08:00:00.000000Z'}]}
        for kind, focus in (('focus', FOCUS), ('board_normal', None), ('board_reassess', None)):
            text = attention.render(kind, self.snapshot(team, focus), instructions=self.instructions)
            self.assertTrue(text.startswith('Oversight pass.'), kind)
            self.assertIn('create more agents', text)
            self.assertNotIn('Continue the Task your todo_focus points at', text)
            self.assertNotIn('signoff', text.lower())
            self.assertIn('Available agents (active workers with no open or working Task): ["Harbor","Cedar"]', text)
            self.assertIn('no update for 10 minutes or more', text)
            self.assertIn('"title":"Held task"', text)
            self.assertIn('- more stalled Tasks exist', text)
            self.assertIn('Delegated Tasks that are blocked', text)
            self.assertIn('"title":"Stuck task"', text)
            self.assertNotIn('- more blocked Tasks exist', text)
            self.assertIn('Tasks on your own Todo that you could hand over', text)
            self.assertLess(text.index('Oversight pass.'), text.index('Todo snapshot'))

    def test_an_idle_team_and_no_stalled_work_are_stated(self):
        text = attention.render('focus', self.snapshot({'available': [], 'stalled': [], 'stalled_count': 0}),
                                instructions=self.instructions)
        self.assertIn('Available agents (active workers with no open or working Task): none', text)
        self.assertIn('up to 10 shown): none', text)
        self.assertIn('check whether you can clear the blocker): none', text)

    def test_the_coordinator_is_told_to_move_or_clear_a_focus_he_is_not_acting_on(self):
        text = attention.render('focus', self.snapshot({'available': [], 'stalled': [], 'stalled_count': 0}),
                                instructions=self.instructions)
        self.assertIn('Your todo_focus must point only at the Task you are acting on right now.', text)
        self.assertIn('attention_focus_set', text)
        self.assertIn('attention_focus_clear', text)
        self.assertIn('that clears your todo_focus', text)
        self.assertNotIn('Keep your todo_focus on the Task you are coordinating', text)

    def test_a_focus_held_for_hours_is_named_in_the_coordinators_reminder(self):
        team = {'available': [], 'stalled': [], 'stalled_count': 0}
        held = {**self.snapshot(team), 'focused_for': '4d 1h 29m', 'focus_held_minutes': 5849}
        text = attention.render('focus', held, instructions=self.instructions)
        self.assertIn('This todo_focus has been held for 4d 1h 29m. If you are not acting on this Task right now, '
                      'move it with attention_focus_set or clear it with attention_focus_clear.', text)
        self.assertLess(text.index('has been held for'), text.index('Todo snapshot'))
        for minutes, shown in ((attention.HELD_MINUTES - 1, False), (attention.HELD_MINUTES, True), (None, False)):
            text = attention.render('focus', {**held, 'focus_held_minutes': minutes}, instructions=self.instructions)
            self.assertEqual('has been held for' in text, shown, minutes)
        unfocused = attention.render('board_normal', {**self.snapshot(team, None), 'focus_held_minutes': 5849},
                                     instructions=self.instructions)
        self.assertNotIn('has been held for', unfocused)

    def test_a_workers_reminder_is_unchanged_by_a_long_focus(self):
        worker = attention.render('focus', {**self.snapshot(None), 'focused_for': '4d 1h 29m', 'focus_held_minutes': 5849},
                                  instructions=self.instructions)
        self.assertNotIn('has been held for', worker)
        self.assertNotIn('Task you are acting on right now', worker)
        self.assertIn('Your todo_focus must point only at a Task you are actively working on.', worker)

    def test_a_worker_and_a_blocked_workflow_keep_their_own_text(self):
        worker = attention.render('focus', self.snapshot(None), instructions=self.instructions)
        self.assertIn('Continue the Task your todo_focus points at', worker)
        self.assertNotIn('Oversight pass.', worker)
        self.assertIn('Other board candidates', worker)
        detach = attention.render('workflow_detach', self.snapshot({'available': [], 'stalled': [], 'stalled_count': 0}),
                                  instructions=self.instructions)
        self.assertNotIn('Oversight pass.', detach)


class TeamDataTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE principals (id TEXT, display_name TEXT);
            CREATE TABLE businesses (id TEXT, archived_at TEXT);
            CREATE TABLE crm_entries (record_task_id TEXT, record_business_id TEXT, removed INTEGER);
            CREATE TABLE tasks (id TEXT, business_id TEXT, title TEXT, status TEXT, created_at TEXT, updated_at TEXT, due TEXT,
                snoozed_until TEXT, worker_ids_json TEXT, fields_json TEXT, archived INTEGER DEFAULT 0);''')
        for agent in ('coordinator', 'scout', 'harbor', 'cedar'):
            self.conn.execute('INSERT INTO principals VALUES(?,?)', (agent, agent.title()))
        patch('qorqut.agents.global_scope.active_role_holders', return_value=['cedar', 'harbor', 'scout']).start()
        self.addCleanup(patch.stopall)

    def task(self, identity, status, workers, updated, snoozed=None):
        self.conn.execute('INSERT INTO tasks(id,title,status,updated_at,snoozed_until,worker_ids_json,fields_json) '
                          'VALUES(?,?,?,?,?,?,?)', (identity, identity, status, updated, snoozed, json.dumps(workers), '{}'))

    def test_free_agents_and_quiet_delegated_work(self):
        old, recent = '2026-11-18T09:00:00.000000Z', '2026-11-18T11:55:00.000000Z'
        self.task('quiet', 'working', ['scout'], old)
        self.task('older', 'open', ['scout'], '2026-11-18T07:00:00.000000Z')
        self.task('fresh', 'working', ['scout'], recent)
        self.task('snoozed', 'open', ['scout'], old, '2026-11-19T00:00:00.000000Z')
        self.task('shared', 'working', ['coordinator', 'scout'], old)       # the coordinator holds it himself
        self.task('paused', 'blocked', ['harbor'], old)                     # blocked work keeps nobody busy
        self.task('unnamed', 'open', [], old)
        self.task('owners', 'open', ['owner'], old)                         # a person's Task is not delegated work
        team = Attention.team(None, NS(connection=self.conn), 'coordinator', NOW)
        self.assertEqual(team['available'], ['Cedar', 'Harbor'])
        self.assertEqual([task['id'] for task in team['stalled']], ['older', 'quiet'])
        self.assertEqual(team['stalled'][0]['workers'], ['Scout'])
        self.assertEqual(team['stalled_count'], 2)
        self.assertEqual([task['id'] for task in team['blocked']], ['paused'])      # shown however old the block is
        self.assertEqual(team['blocked_count'], 1)


if __name__ == '__main__':
    unittest.main()
