"""The queue of the Learning Applier's workflow runs: when a run is due, when it starts, and what ends it."""
from datetime import datetime, timedelta, timezone
import unittest

from qorqut.experience.runs import Runs, kickoff, schedules, zone_name

from test_experience import SUNDAY, ExperienceCase


def at(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class RunQueueTests(ExperienceCase):
    def due(self, force=True):
        with self.runtime.database.transaction() as uow:
            return self.runs.due(uow, force=force)

    def runs_of(self, kind=None):
        with self.runtime.database.read() as uow:
            return [uow.repository('experience_runs').get(row['id']) for row in uow.connection.execute(
                'SELECT id FROM experience_runs WHERE kind=COALESCE(?,kind) ORDER BY slot, id', (kind,))]

    def finish(self, run_id):
        run = self.row(run_id)
        more = {}
        if run['kind'] == 'extraction':
            more['conversations_end'] = self.ok('experience_conversations', {'run_id': run_id})['end_token']
        return self.ok('experience_run_finish', {'run_id': run_id, 'summary': 'Done.', **more})['run']

    # ----- what is due -------------------------------------------------------------------------------------------------

    def test_the_first_run_is_queued_at_its_time_and_never_for_a_time_long_past(self):
        # Sunday 03:00:30: the daily extraction is due. The weekly review's last time was a week ago: not caught up.
        queued, = self.due()
        run, = self.runs_of()
        self.assertEqual((run['id'], run['kind'], run['state'], run['slot'], run['window_start'], run['window_end']),
                         (queued, 'extraction', 'queued', '2026-11-22T03:00:00.000000Z', '2026-11-21T03:00:00.000000Z',
                          '2026-11-22T03:00:00.000000Z'))
        self.assertEqual(self.due(), [])
        self.now = SUNDAY.replace(hour=6, minute=0, second=10)
        review, = self.due()
        self.assertEqual([(row['kind'], row['slot']) for row in self.runs_of()],
                         [('extraction', '2026-11-22T03:00:00.000000Z'), ('review', '2026-11-22T06:00:00.000000Z')])
        self.assertEqual(self.row(review)['window_start'], '2026-11-15T06:00:00.000000Z')

    def test_a_store_first_looked_at_hours_after_the_time_waits_for_the_next_one(self):
        self.now = SUNDAY.replace(hour=10)
        self.assertEqual(self.due(), [])
        self.now = SUNDAY.replace(hour=3, minute=0, second=5) + timedelta(days=1)
        self.assertEqual(len(self.due()), 1)

    def test_the_schedule_is_looked_at_once_a_minute_and_nothing_is_queued_without_a_learning_applier(self):
        self.assertEqual(len(self.due(force=False)), 1)
        self.now += timedelta(days=1)
        self.tick += 30
        self.assertEqual(self.due(force=False), [])
        with self.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (self.applier.principal_id,))
        self.tick += 100
        self.assertEqual(self.due(force=False), [])

    def test_a_time_missed_later_is_caught_up_and_reads_from_the_end_of_the_last_finished_run(self):
        first, = self.due()
        self.pass_(); self.pass_()
        self.finish(first)
        # The service was down at 03:00 on Monday and Tuesday; it looks again on Tuesday afternoon.
        self.now = SUNDAY.replace(hour=15) + timedelta(days=2)
        second, = self.due()
        run = self.row(second)
        self.assertEqual((run['slot'], run['window_start'], run['window_end']),
                         ('2026-11-24T03:00:00.000000Z', '2026-11-22T03:00:00.000000Z', '2026-11-24T03:00:00.000000Z'))

    def test_a_waiting_run_takes_the_newer_time_and_keeps_the_days_it_waited_through(self):
        self.opener.wake = False                      # the agent is switched off: nothing starts
        first, = self.due()
        self.pass_()
        self.now += timedelta(days=1)
        self.assertEqual(self.due(), [first])
        run, = self.runs_of()
        self.assertEqual((run['state'], run['slot'], run['window_start'], run['window_end']),
                         ('queued', '2026-11-23T03:00:00.000000Z', '2026-11-21T03:00:00.000000Z', '2026-11-23T03:00:00.000000Z'))
        # Never more than a week in one run.
        self.now += timedelta(days=30)
        self.due()
        run, = self.runs_of()
        self.assertEqual((run['window_start'], run['window_end']), ('2026-12-16T03:00:00.000000Z', '2026-12-23T03:00:00.000000Z'))

    def test_the_times_come_from_the_environment_and_a_bad_value_schedules_nothing(self):
        self.assertEqual(schedules({}), {'extraction': '0 3 * * *', 'review': '0 6 * * 0'})
        self.assertEqual(schedules({'QORQUT_EXPERIENCE_EXTRACTION_AT': '30 4 * * *', 'QORQUT_EXPERIENCE_REVIEW_AT': 'off'}),
                         {'extraction': '30 4 * * *'})
        with self.assertLogs('qorqut.experience.runs', 'WARNING'):
            self.assertEqual(schedules({'QORQUT_EXPERIENCE_EXTRACTION_AT': 'at three'}), {'review': '0 6 * * 0'})
        # The owner's time zone decides when 03:00 is.
        runs = Runs(self.runtime, clock=lambda: self.now, monotonic=lambda: self.tick, environ={'QORQUT_EXPERIENCE_REVIEW_AT': 'off'},
                    zone='America/New_York')
        self.now = datetime(2026, 11, 22, 8, 0, 20, tzinfo=timezone.utc)      # 03:00 in New York
        with self.runtime.database.transaction() as uow:
            queued, = runs.due(uow)
        self.assertEqual(self.row(queued)['slot'], '2026-11-22T08:00:00.000000Z')

    def test_the_times_are_in_the_owners_zone_and_an_unset_installation_zone_does_not_make_them_utc(self):
        system = lambda: 'America/La_Paz'
        self.assertEqual(zone_name({}, system), 'America/La_Paz')
        self.assertEqual(zone_name({'TZ': ':Europe/Berlin'}, system), 'Europe/Berlin')
        self.assertEqual(zone_name({'TZ': 'Europe/Berlin', 'QORQUT_TIMEZONE': 'Asia/Tokyo'}, system), 'Asia/Tokyo')
        self.assertEqual(zone_name({'QORQUT_TIMEZONE': 'Asia/Tokyo', 'QORQUT_EXPERIENCE_TIMEZONE': 'America/New_York'}, system),
                         'America/New_York')
        with self.assertLogs('qorqut.experience.runs', 'WARNING'):
            self.assertEqual(zone_name({'QORQUT_EXPERIENCE_TIMEZONE': 'Mars/Olympus'}, lambda: None), 'UTC')
        runs = Runs(self.runtime, clock=lambda: self.now, monotonic=lambda: self.tick,
                    environ={'TZ': 'America/La_Paz', 'QORQUT_EXPERIENCE_REVIEW_AT': 'off'})
        self.assertEqual(runs.zone, 'America/La_Paz')
        self.now = datetime(2026, 11, 22, 7, 0, 20, tzinfo=timezone.utc)      # 03:00 in La Paz
        with self.runtime.database.transaction() as uow:
            queued, = runs.due(uow)
        self.assertEqual(self.row(queued)['slot'], '2026-11-22T07:00:00.000000Z')

    # ----- starting ----------------------------------------------------------------------------------------------------

    def test_a_run_starts_in_a_fresh_conversation_once_the_agent_is_idle(self):
        run, = self.due()
        self.opener.conversation, self.opener.phase = 'yesterday', 'working'
        self.pass_()
        self.assertEqual((self.opener.opened, self.row(run)['state']), ([], 'queued'))
        self.opener.phase = 'idle'
        self.pass_()
        opened, = self.opener.opened
        self.assertEqual((opened['closing'], opened['key']), ('yesterday', f'experience:{run}:1'))
        self.assertTrue(opened['text'].startswith('<qorqut-workflow>\nScheduled workflow: experience extraction.\nRun: ' + run))
        self.assertIn('Period to read: 2026-11-21T03:00:00.000000Z to 2026-11-22T03:00:00.000000Z', opened['text'])
        self.pass_()
        row = self.row(run)
        self.assertEqual((row['state'], row['conversation_id'], row['agent_id'], row['attempts']),
                         ('running', 'conversation-1', self.applier.principal_id, 1))
        shown = self.ok('experience_run_get', ctx=self.runtime.owner)
        self.assertEqual((shown['running']['id'], shown['running']['workflow'], shown['waiting'], shown['time_zone']),
                         (run, 'experience extraction', [], 'UTC'))
        self.assertEqual(shown['schedule']['review'], {'workflow': 'review and application', 'cron': '0 6 * * 0'})

    def test_an_agent_with_no_session_gets_a_new_conversation_and_one_that_is_off_is_left_alone(self):
        run, = self.due()
        self.opener.wake = False
        self.pass_()
        self.assertEqual(self.opener.opened, [])
        self.opener.wake = True
        self.pass_()
        self.assertEqual(self.opener.opened[0]['closing'], '')
        self.assertIn('review and application', kickoff({'kind': 'review', 'id': 'r', 'window_start': '', 'window_end': ''}))

    def test_a_run_that_is_due_while_another_runs_waits_until_that_one_is_finished(self):
        extraction = self.start('extraction')
        review = self.ok('experience_run_request', {'kind': 'review'}, ctx=self.runtime.coordinator)
        self.assertTrue(review['running'])
        self.pass_(); self.pass_()
        self.assertEqual((len(self.opener.opened), self.row(review['run']['id'])['state']), (1, 'queued'))
        self.assertEqual([run['id'] for run in self.ok('experience_run_get')['waiting']], [review['run']['id']])
        self.assertEqual(self.finish(extraction)['state'], 'done')
        self.pass_(); self.pass_()
        self.assertEqual((self.opener.opened[1]['closing'], self.row(review['run']['id'])['state'],
                          self.row(review['run']['id'])['conversation_id']), ('conversation-1', 'running', 'conversation-2'))
        # Asking for a run of a kind that already waits moves that one instead of queuing a second.
        self.now += timedelta(minutes=5)
        one = self.ok('experience_run_request', {'kind': 'extraction'}, ctx=self.runtime.owner)['run']
        self.now += timedelta(minutes=5)
        two = self.ok('experience_run_request', {'kind': 'extraction'}, ctx=self.runtime.owner)['run']
        self.assertEqual((one['id'], two['scheduled_for'], len(self.runs_of('extraction'))), (two['id'], at(self.now), 2))
        self.refused('experience_run_request', {'kind': 'review'}, ctx=self.runtime.worker, code='forbidden')

    # ----- ending ------------------------------------------------------------------------------------------------------

    def test_a_run_whose_conversation_ended_is_started_once_more_and_then_given_up(self):
        run = self.start('extraction')
        self.opener.conversation = None               # the session ended without experience_run_finish
        self.now += timedelta(minutes=5)
        self.pass_()
        self.assertEqual(self.row(run)['state'], 'running')
        self.now += timedelta(minutes=20)
        self.pass_()
        self.assertEqual((self.row(run)['state'], self.row(run)['conversation_id']), ('queued', None))
        self.pass_(); self.pass_()
        self.assertEqual((self.row(run)['state'], self.row(run)['attempts'], self.row(run)['conversation_id']),
                         ('running', 2, 'conversation-2'))
        self.opener.conversation = 'something-else'   # the owner opened another conversation; the run's own is gone
        self.now += timedelta(minutes=21)
        self.pass_()
        row = self.row(run)
        self.assertEqual((row['state'], row['result_json']), ('abandoned', {'reason': 'The conversation ended before the run was finished'}))
        self.refused('experience_observations_submit', {'run_id': run, 'observations': [self.observation(new_cluster={'label': 'Late'})]},
                     code='invalid_transition')

    def test_a_start_that_fails_is_tried_again_later_and_given_up_after_three(self):
        run, = self.due()
        self.opener.fail = 'close_timeout'
        self.pass_(); self.pass_()
        self.assertEqual((self.row(run)['state'], self.row(run)['attempts']), ('queued', 1))
        self.pass_(60)                                # too soon after a failure
        self.assertEqual(self.row(run)['attempts'], 1)
        self.pass_(); self.pass_()
        self.assertEqual((self.row(run)['state'], self.row(run)['attempts']), ('queued', 2))
        self.pass_(130); self.pass_()
        row = self.row(run)
        self.assertEqual((row['state'], row['attempts'], row['result_json']),
                         ('abandoned', 3, {'reason': 'A fresh conversation could not be started', 'code': 'close_timeout'}))

    def test_a_start_cut_short_by_a_restart_adopts_the_fresh_conversation_or_starts_again(self):
        def cut(observed):
            """A run marked running whose conversation was never recorded, and the conversation the agent runs now."""
            run, = self.due()
            self.pass_()
            self.runs._starting = None                # the service stopped before the open reported back
            with self.runtime.database.transaction() as uow:
                uow.repository('experience_runs').update(run, {'conversation_id': None})
                uow.connection.execute(
                    'INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,'
                    "process_started,observed_at,verified_at) VALUES(?,?,'claude',?,1,'managed',1,'started',?,?)",
                    ('binding', self.applier.principal_id, self.opener.conversation, at(observed), at(observed)))
            return run
        run = cut(self.now + timedelta(seconds=5))
        self.pass_()
        self.assertEqual((self.row(run)['state'], self.row(run)['conversation_id']), ('running', 'conversation-1'))

    def test_a_start_cut_short_before_the_fresh_conversation_opened_is_started_again(self):
        run, = self.due()
        self.opener.conversation = 'yesterday'
        with self.runtime.database.transaction() as uow:
            uow.repository('experience_runs').update(run, {'state': 'running', 'agent_id': self.applier.principal_id, 'attempts': 1,
                                                            'started_at': at(self.now), 'seen_at': at(self.now)})
            uow.connection.execute(
                'INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,'
                "process_started,observed_at,verified_at) VALUES(?,?,'claude','yesterday',1,'managed',1,'started',?,?)",
                ('binding', self.applier.principal_id, at(self.now - timedelta(hours=9)), at(self.now - timedelta(hours=9))))
        self.pass_()
        self.assertEqual(self.row(run)['state'], 'queued')
        self.pass_(); self.pass_()
        self.assertEqual((self.opener.opened[0]['closing'], self.row(run)['state'], self.row(run)['attempts']), ('yesterday', 'running', 2))


if __name__ == '__main__':
    unittest.main()
